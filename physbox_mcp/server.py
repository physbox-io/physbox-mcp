"""
PhysBox: MCP - Model Context Protocol Server for Flux, Volt, Mesh, and Etch.
Connects LLMs to browser-based simulations via WebSocket relay.
"""

import os
import sys
import json
import random
import string
import time
import base64
import urllib.request
import asyncio
import threading
from pathlib import Path
from typing import Any
from fastmcp import FastMCP
try:
    from fastmcp import Image
except ImportError:
    from fastmcp.utilities.types import Image
import websockets

from . import cloud

# ── Configuration & Constants ──────────────────────────────────────────────────

MCP_PORT = int(os.environ.get("MCP_PORT", "3141"))
MCP_WS_PORT = int(os.environ.get("MCP_WS_PORT", "3142"))

APPS = {
    "process": {"port": 5173, "name": "Flux"},
    "circuit": {"port": 5174, "name": "Volt"},
    "physics": {"port": 5175, "name": "Mesh"},
    "etch":    {"port": 5176, "name": "Etch"},
}

P  = APPS["process"]["port"]
C  = APPS["circuit"]["port"]
Ph = APPS["physics"]["port"]
Et = APPS["etch"]["port"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)

def compact_dict(**kwargs) -> dict:
    """Return a dictionary containing only non-None values."""
    return {k: v for k, v in kwargs.items() if v is not None}

def _deliver_export(result: Any, out_dir: str | None) -> dict:
    """Turn an export reply into something an agent can actually hold.

    The browser hands back whole files as base64 — an STL or a G-code program is
    megabytes, and megabytes of base64 returned through an MCP tool is the agent's
    entire context spent on characters it cannot read. So the payloads never leave
    this function: with out_dir they are decoded to disk and the caller gets paths,
    without it the caller gets names and byte counts and knows to ask again with a
    directory. Either way `base64` is stripped from what is returned.
    """
    if not isinstance(result, dict):
        return {"summary": result, "warnings": [], "files": []}
    if result.get("ok") is False:
        raise RuntimeError(result.get("error") or "Export failed in the app with no reason given.")

    files = result.get("files") or []
    out: list[dict] = []
    target = Path(out_dir).expanduser() if out_dir else None
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)

    for f in files:
        name = str(f.get("name") or "export.bin")
        try:
            data = base64.b64decode(f.get("base64") or "")
        except Exception:
            data = b""
        # The app only encodes the bytes when it was told they would be kept, so
        # with no out_dir there is nothing to decode and the size it reported is
        # the only true one. Falling back to len(data) there would report 0.
        size = f.get("bytes")
        if not isinstance(size, int):
            size = len(data)
        if target is not None:
            # basename only: the app names these files, and a name with a path in
            # it has no business deciding where a write lands.
            path = target / Path(name).name
            path.write_bytes(data)
            out.append({"name": name, "path": str(path), "bytes": len(data)})
        else:
            out.append({"name": name, "bytes": size})

    delivered = {
        "summary": result.get("summary"),
        "warnings": result.get("warnings") or [],
        "files": out,
    }
    if target is None and out:
        delivered["note"] = (
            "File contents were not returned (they are far too large for a tool result). "
            "Call again with out_dir set to a directory to write them to disk."
        )
    return delivered

# Where an app's own docs live, when the app is checked out beside us. The key
# is the MCP's app id; the value is the repo directory, which is not always the
# same word — Mesh's tools are all `physics_*`.
APP_REPO_DIR = {"physics": "mesh", "circuit": "volt"}

def load_mcp_docs(app_id: str) -> dict:
    """
    An app's agent-facing documentation, preferring the app's own copy.

    The bundled copy under `mcp-docs/` is a *fallback* for an installed server
    with no app checkout, and it is last for that reason. It used to sit second,
    ahead of the app's own file, which meant a developer editing the app's
    `mcp-docs.json` — the thing both apps' CLAUDE.md tells them to edit — was
    editing a file the server then ignored. The two drifted for months and
    nothing said so, because a tool whose docs are missing falls back to the
    inline description in `get_doc` and keeps working.
    """
    current_dir = Path(__file__).parent.resolve()
    repo = APP_REPO_DIR.get(app_id, app_id)
    possible_paths = [
        current_dir / ".." / ".." / repo / "mcp-docs.json",
        Path.home() / repo / "mcp-docs.json",
        current_dir / "mcp-docs" / f"{app_id}.json",
    ]
    for path in possible_paths:
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error reading docs for {app_id} from {path}: {e}", file=sys.stderr)
    return {}

physics_docs = load_mcp_docs("physics")
process_docs = load_mcp_docs("process")
circuit_docs = load_mcp_docs("circuit")
etch_docs    = load_mcp_docs("etch")
cloud_docs   = load_mcp_docs("cloud")

def get_reference_docs(docs: dict) -> dict:
    """Everything in an app's mcp-docs.json EXCEPT `tools` (each tool's text is
    already surfaced via its own @mcp.tool description, so repeating it here
    would just be dead weight). This is what *_get_schema returns.

    Before this, *_get_schema returned only the `schema` key's contents (as
    the top-level result), which silently dropped `gotchas`/`overview`/
    `workflow`/`examples` even when a doc file had them (mcp-docs.json's
    physics/circuit docs both do, physics alone has 17 `gotchas` entries) —
    no MCP tool ever returned those keys, so an agent had no way to see that
    guidance short of reading the raw JSON file off disk, which nothing
    prompts it to do.

    `schema`'s own fields (geomTypes, nodeFields, etc.) are kept flattened at
    the top level, matching the pre-existing response shape exactly (callers
    already do e.g. result["geomTypes"]) — the other doc sections are added
    as sibling keys alongside them, so this is purely additive.
    """
    schema_fields = docs.get("schema", {})
    extra_sections = {k: v for k, v in docs.items() if k not in ("tools", "schema")}
    return {**schema_fields, **extra_sections}

def get_doc(docs: dict, tool_name: str, fallback: str = "") -> str:
    """Extract tool description string from app documentation with fallback."""
    return docs.get("tools", {}).get(tool_name, fallback)

# ── Multi-Client Relay & WebSocket Hub State ──────────────────────────────────

is_primary = False
peer_clients: set = set()
peer_pending_requests: dict = {}  # req_id -> (peer_ws, req_id)
peer_local_pending: dict = {}      # req_id -> (mcp_loop, fut)
peer_ws = None
peer_ws_loop = None

async def broadcast_app_status(port: int, connected: bool):
    if not peer_clients:
        return
    msg = json.dumps({"event": "APP_STATUS", "port": port, "connected": connected})
    dead = set()
    for p_ws in list(peer_clients):
        try:
            await p_ws.send(msg)
        except Exception:
            dead.add(p_ws)
    for p_ws in dead:
        peer_clients.discard(p_ws)

# ── Connection Pool ───────────────────────────────────────────────────────────

class AppConnection:
    def __init__(self, port: int):
        self.port = port
        self.ws = None
        self.ws_loop = None
        self.pending: dict[str, asyncio.Future] = {}
        self.connected = False
        # One port, many tabs. Before this, every tab of an app registered on the
        # same AppConnection and each one clobbered `ws`, so a command went to
        # whichever tab had spoken last and a reply came back from whichever tab
        # answered first. That is fine until it isn't: a body created in tab A is
        # invisible to the next command if tab B answers it, and a save answered
        # by a stale tab writes stale data over good data. So keep every tab as
        # its own session and deliberately talk to exactly one of them.
        #
        # Insertion-ordered by design: the last key is the most recently connected
        # session, which is the default target. A reconnecting sessionId is popped
        # before it is re-inserted so it moves to the end rather than doubling up.
        self.sessions: dict[str, dict] = {}
        # None means "no one has chosen", which is the normal case and is why a
        # single-tab agent never has to think about sessions at all.
        self.selected_session: str | None = None
        # Which session each in-flight request went to, so that a tab closing can
        # fail exactly its own requests instead of everyone's.
        self.pending_session: dict[str, str] = {}

    def start(self):
        pass

    def active_session_id(self) -> str | None:
        """The session `send` will talk to: the pinned one if it is still here,
        otherwise the most recently connected. Returns None when no tab is open."""
        if self.selected_session and self.selected_session in self.sessions:
            return self.selected_session
        if self.selected_session:
            # The pinned tab went away. Drop the pin rather than keep erroring, so
            # the default rule takes over silently.
            self.selected_session = None
        if not self.sessions:
            return None
        return next(reversed(self.sessions))

    def add_session(self, session_id: str, ws, ws_loop, info: dict) -> None:
        now = _now_ms()
        self.sessions.pop(session_id, None)
        self.sessions[session_id] = {
            "ws": ws,
            "ws_loop": ws_loop,
            "label": info.get("label") or "unnamed",
            "href": info.get("href"),
            "startedAt": info.get("startedAt"),
            "connectedAt": now,
            "lastSeen": now,
        }
        self.ws = ws
        self.ws_loop = ws_loop
        self.connected = True

    def drop_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)
        if self.selected_session == session_id:
            self.selected_session = None
        active = self.active_session_id()
        if active is None:
            self.connected = False
            self.ws = None
            self.ws_loop = None
        else:
            entry = self.sessions[active]
            self.ws = entry["ws"]
            self.ws_loop = entry["ws_loop"]

    def touch(self, session_id: str | None) -> None:
        entry = self.sessions.get(session_id) if session_id else None
        if entry:
            entry["lastSeen"] = _now_ms()

    def describe_sessions(self) -> list[dict]:
        active = self.active_session_id()
        return [
            {
                "sessionId": sid,
                "label": entry["label"],
                "href": entry["href"],
                "startedAt": entry["startedAt"],
                "connectedAt": entry["connectedAt"],
                "lastSeen": entry["lastSeen"],
                "selected": sid == active,
                "pinned": sid == self.selected_session,
            }
            for sid, entry in self.sessions.items()
        ]

    async def send(self, cmd: str, payload: dict | None = None, timeout: float = 10.0) -> Any:
        global is_primary, peer_ws, peer_ws_loop
        
        if is_primary:
            session_id = self.active_session_id()
            if session_id is None:
                raise RuntimeError(
                    f"App on port {self.port} is not connected. Open the app in your browser!"
                )
            entry = self.sessions[session_id]
            msg_id = "".join(random.choices(string.ascii_letters, k=8))
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self.pending[msg_id] = fut
            self.pending_session[msg_id] = session_id
            # The envelope wins over the payload. Spread the other way round, a
            # tool argument called "id" (update_component's component id) landed
            # on top of the request id: the app answered under the component's
            # name, nothing matched the pending future, and every call to those
            # tools timed out after ten seconds having already applied its edit.
            data = {**(payload or {}), "cmd": cmd, "id": msg_id}

            # One socket, not a broadcast — see the note on `sessions`.
            asyncio.run_coroutine_threadsafe(entry["ws"].send(json.dumps(data)), entry["ws_loop"])

            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self.pending.pop(msg_id, None)
                self.pending_session.pop(msg_id, None)
                raise RuntimeError(f'Timeout waiting for "{cmd}" response ({timeout}s)')
        else:
            if not self.connected or peer_ws is None or peer_ws_loop is None:
                raise RuntimeError(
                    f"App on port {self.port} is not connected or Primary MCP Hub is unavailable. Open the app in your browser!"
                )
            msg_id = "".join(random.choices(string.ascii_letters, k=8))
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            peer_local_pending[msg_id] = (loop, fut)
            
            forward_data = {
                "event": "FORWARD_CMD",
                "id": msg_id,
                "port": self.port,
                "cmd": cmd,
                "payload": payload,
            }
            asyncio.run_coroutine_threadsafe(peer_ws.send(json.dumps(forward_data)), peer_ws_loop)
            
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                peer_local_pending.pop(msg_id, None)
                raise RuntimeError(f'Timeout waiting for "{cmd}" response ({timeout}s)')

_connections: dict[int, AppConnection] = {}

def get_conn(port: int) -> AppConnection:
    if port not in _connections:
        _connections[port] = AppConnection(port)
    return _connections[port]

# ── Session control ───────────────────────────────────────────────────────────
#
# Session state lives on the primary only: it is the process holding the browser
# sockets, so anywhere else it would be a copy that goes stale. A secondary peer
# therefore asks the primary rather than keeping its own table, and it asks over
# the FORWARD_CMD channel that already exists — these two pseudo-commands are
# recognised by the primary's handler and answered locally instead of being
# passed on to a tab. The double underscores are there so they can never collide
# with a real browser command name.
LIST_SESSIONS_CMD = "__list_sessions__"
USE_SESSION_CMD   = "__use_session__"
SESSION_CONTROL_CMDS = (LIST_SESSIONS_CMD, USE_SESSION_CMD)

def local_list_sessions() -> list[dict]:
    """Every browser session on every known app port, primary-side."""
    out = []
    for app_id, app in APPS.items():
        conn = _connections.get(app["port"])
        sessions = conn.describe_sessions() if conn else []
        out.append({
            "id": app_id,
            "name": app["name"],
            "port": app["port"],
            "sessions": sessions,
        })
    return out

def local_use_session(port: int, session_id: str) -> dict:
    conn = _connections.get(int(port))
    known = list(conn.sessions.keys()) if conn else []
    if session_id not in known:
        raise RuntimeError(
            f"No session '{session_id}' on port {port}. "
            f"Open sessions there: {', '.join(known) if known else '(none — open the app in your browser)'}"
        )
    conn.selected_session = session_id
    entry = conn.sessions[session_id]
    return {"port": int(port), "sessionId": session_id, "label": entry["label"], "href": entry["href"]}

def handle_session_control(cmd: str, payload: dict | None) -> Any:
    payload = payload or {}
    if cmd == LIST_SESSIONS_CMD:
        return local_list_sessions()
    return local_use_session(payload.get("port"), payload.get("sessionId"))

async def send_session_control(cmd: str, payload: dict | None = None, timeout: float = 10.0) -> Any:
    """Run a session-control command wherever the sessions actually are. On the
    primary that is right here; on a peer it is a FORWARD_CMD like any other,
    which is why this does not go through AppConnection.send — there is no port
    to be connected to, and a peer must be able to list sessions even when the
    app it is asking about has none."""
    if is_primary:
        return handle_session_control(cmd, payload)
    if peer_ws is None or peer_ws_loop is None:
        raise RuntimeError("Primary MCP Hub is unavailable, so browser sessions cannot be listed.")
    msg_id = "".join(random.choices(string.ascii_letters, k=8))
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    peer_local_pending[msg_id] = (loop, fut)
    forward_data = {
        "event": "FORWARD_CMD",
        "id": msg_id,
        "port": (payload or {}).get("port"),
        "cmd": cmd,
        "payload": payload,
    }
    asyncio.run_coroutine_threadsafe(peer_ws.send(json.dumps(forward_data)), peer_ws_loop)
    try:
        return await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        peer_local_pending.pop(msg_id, None)
        raise RuntimeError(f'Timeout waiting for "{cmd}" response ({timeout}s)')

def probe_port(port: int) -> dict:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}", timeout=1.5) as r:
            return {"open": True, "status": r.status}
    except Exception:
        return {"open": False}

# ── WebSocket Bridge (Primary Hub or Secondary Peer Client) ───────────────────

async def ws_handler(ws):
    conn = None
    is_peer = False
    peer_id = None
    session_id = None
    ws_loop = asyncio.get_running_loop()
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            event = msg.get("event")

            if event == "HELLO":
                app_key = msg.get("app")
                app_info = APPS.get(app_key)
                if app_info:
                    conn = get_conn(app_info["port"])
                    # A tab that predates the session protocol sends no sessionId.
                    # Give it one derived from its own socket so it is a session
                    # like any other — it just cannot survive a reload, which is
                    # exactly what "old client" means here.
                    session_id = msg.get("sessionId") or f"conn-{id(ws):x}"
                    conn.add_session(session_id, ws, ws_loop, {
                        "label": msg.get("label") or (None if msg.get("sessionId") else "unknown (old client)"),
                        "href": msg.get("href"),
                        "startedAt": msg.get("startedAt"),
                    })
                    print(f"Registered browser session {session_id} for {app_info['name']} on port {app_info['port']} ({len(conn.sessions)} open)", file=sys.stderr)
                    # A signed-in tab can offer its own session as a fallback
                    # credential for the cloud tools, so an agent needs no setup at
                    # all while the app is open. Deliberately the last source
                    # `cloud.resolve_token` tries: this socket is unauthenticated,
                    # so anything reaching it could have offered a token too.
                    cloud.set_browser_token(msg.get("token"))
                    await ws.send(json.dumps({"event": "CONNECTED", "role": "browser"}))
                    await broadcast_app_status(app_info["port"], True)
                else:
                    print(f"Unknown app connected: {app_key}", file=sys.stderr)

            elif event == "HELLO_PEER":
                is_peer = True
                peer_id = msg.get("peer_id")
                peer_clients.add(ws)
                print(f"Registered secondary MCP peer instance ({peer_id})", file=sys.stderr)
                status = {port: conn.connected for port, conn in _connections.items()}
                await ws.send(json.dumps({"event": "PEER_CONNECTED", "peer_id": peer_id, "apps": status}))

            elif event == "FORWARD_CMD":
                req_id = msg.get("id")
                port = msg.get("port")
                cmd = msg.get("cmd")
                payload = msg.get("payload")
                
                if cmd in SESSION_CONTROL_CMDS:
                    # Never reaches a tab: the peer is asking the primary about its
                    # own session table, so answer it here.
                    try:
                        await ws.send(json.dumps({
                            "event": "PEER_RESULT", "id": req_id,
                            "data": handle_session_control(cmd, payload), "error": None,
                        }))
                    except Exception as e:
                        await ws.send(json.dumps({"event": "PEER_RESULT", "id": req_id, "error": str(e)}))
                    continue

                target_conn = get_conn(port) if port else None
                target_session = target_conn.active_session_id() if target_conn else None
                if target_session is None:
                    await ws.send(json.dumps({
                        "event": "PEER_RESULT",
                        "id": req_id,
                        "error": f"App on port {port} is not connected. Open the app in your browser!"
                    }))
                else:
                    entry = target_conn.sessions[target_session]
                    peer_pending_requests[req_id] = (ws, req_id)
                    cmd_data = {**(payload or {}), "cmd": cmd, "id": req_id}  # envelope wins; see AppConnection.send
                    asyncio.run_coroutine_threadsafe(entry["ws"].send(json.dumps(cmd_data)), entry["ws_loop"])

            elif event in ("RESULT", "ERROR"):
                msg_id = msg.get("id", "")
                if conn:
                    conn.touch(session_id)
                    conn.pending_session.pop(msg_id, None)
                    fut = conn.pending.pop(msg_id, None)
                    if fut and not fut.done():
                        mcp_loop = fut.get_loop()
                        if event == "ERROR":
                            err_msg = msg.get("error", "unknown")
                            mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError(err_msg))
                        else:
                            mcp_loop.call_soon_threadsafe(fut.set_result, msg.get("data"))
                
                peer_item = peer_pending_requests.pop(msg_id, None)
                if peer_item:
                    p_ws, orig_id = peer_item
                    if p_ws in peer_clients:
                        await p_ws.send(json.dumps({
                            "event": "PEER_RESULT",
                            "id": orig_id,
                            "data": msg.get("data"),
                            "error": msg.get("error") if event == "ERROR" else None
                        }))

    except Exception as e:
        print(f"Error in WebSocket handler on port {MCP_WS_PORT}: {e}", file=sys.stderr)
    finally:
        if is_peer and ws in peer_clients:
            peer_clients.remove(ws)
            dead_reqs = [req_id for req_id, (p_ws, _) in peer_pending_requests.items() if p_ws is ws]
            for req_id in dead_reqs:
                peer_pending_requests.pop(req_id, None)
                
        if conn and session_id and conn.sessions.get(session_id, {}).get("ws") is ws:
            conn.drop_session(session_id)
            # Only the requests that went to *this* tab are dead; another tab's
            # in-flight work is none of this socket's business. If that leaves no
            # tabs at all the port really is down, and the peers are told so.
            dead = [mid for mid, sid in conn.pending_session.items() if sid == session_id]
            for mid in dead:
                conn.pending_session.pop(mid, None)
                fut = conn.pending.pop(mid, None)
                if fut and not fut.done():
                    fut.get_loop().call_soon_threadsafe(fut.set_exception, RuntimeError("WebSocket disconnected"))
            if not conn.sessions:
                asyncio.create_task(broadcast_app_status(conn.port, False))
                for fut in list(conn.pending.values()):
                    if not fut.done():
                        mcp_loop = fut.get_loop()
                        mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError("WebSocket disconnected"))
                conn.pending.clear()
                conn.pending_session.clear()

async def run_peer_client_loop():
    global peer_ws, peer_ws_loop
    peer_ws_url = f"ws://localhost:{MCP_WS_PORT}"
    try:
        async with websockets.connect(peer_ws_url, max_size=32 * 1024 * 1024) as ws:
            peer_ws = ws
            peer_ws_loop = asyncio.get_running_loop()
            peer_id = "".join(random.choices(string.ascii_letters, k=8))
            await ws.send(json.dumps({"event": "HELLO_PEER", "peer_id": peer_id}))
            print(f"Connected to Primary MCP Hub at {peer_ws_url}", file=sys.stderr)
            
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                
                event = msg.get("event")
                if event == "PEER_CONNECTED":
                    apps = msg.get("apps", {})
                    for p_str, conn_state in apps.items():
                        get_conn(int(p_str)).connected = bool(conn_state)
                
                elif event == "APP_STATUS":
                    port = msg.get("port")
                    if port in _connections:
                        _connections[port].connected = bool(msg.get("connected"))
                
                elif event == "PEER_RESULT":
                    msg_id = msg.get("id")
                    item = peer_local_pending.pop(msg_id, None)
                    if item:
                        mcp_loop, fut = item
                        if not fut.done():
                            if "error" in msg and msg["error"] is not None:
                                mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError(msg["error"]))
                            else:
                                mcp_loop.call_soon_threadsafe(fut.set_result, msg.get("data"))
    except Exception as e:
        print(f"Secondary MCP Peer connection closed/failed: {e}", file=sys.stderr)
    finally:
        peer_ws = None
        peer_ws_loop = None
        for msg_id, (mcp_loop, fut) in list(peer_local_pending.items()):
            if not fut.done():
                mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError("Primary MCP Hub disconnected"))
        peer_local_pending.clear()

def start_ws_bridge():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_bridge_loop():
        global is_primary
        while True:
            try:
                # Loopback, not 0.0.0.0. This hub has no origin check and no shared
                # secret, and it can drive a machine with a spinning cutter in it —
                # it had no business being reachable from the LAN even before a
                # credential could arrive over it.
                #
                # More so now that the machine tools can start motion rather than
                # only trim it. The arming gate in the app is what stands between
                # an agent and a moving axis; this is what stands between anyone
                # on the network and the agent's side of that gate. Both are
                # needed — the gate answers "may this agent move the machine",
                # not "whose agent is this".
                async with websockets.serve(ws_handler, "127.0.0.1", MCP_WS_PORT, max_size=32 * 1024 * 1024):
                    is_primary = True
                    print(f"MCP Primary WebSocket Hub listening on ws://localhost:{MCP_WS_PORT}", file=sys.stderr)
                    await asyncio.Future()
            except OSError:
                is_primary = False
                print(f"Port {MCP_WS_PORT} in use. Running as Secondary MCP Peer connected to Primary Hub.", file=sys.stderr)
                await run_peer_client_loop()
                await asyncio.sleep(1.0)
            except Exception as e:
                print(f"Unexpected error in WS bridge loop: {e}", file=sys.stderr)
                await asyncio.sleep(2.0)

    loop.run_until_complete(run_bridge_loop())

threading.Thread(target=start_ws_bridge, daemon=True).start()


# ── MCP Server Initialization ─────────────────────────────────────────────────

mcp = FastMCP(
    "physbox-mcp",
    instructions=(
        "PhysBox: MCP - Model Context Protocol server for Volt (5174), Mesh (5175), "
        "Etch (5176), and Flux (Beta) (5173). "
        "Call detect_apps first to confirm which apps are running."
    ),
)

# ── Universal Tools ───────────────────────────────────────────────────────────

@mcp.tool()
async def detect_apps() -> list[dict]:
    """
    Check which of the three local apps are currently running.
    Returns port, app name, HTTP status, and WebSocket connection status.
    Call this first to discover which apps are active.
    """
    # Sessions live on the primary, so on a peer this is a round trip. It is a
    # best-effort one: if the primary is unreachable the app list is still worth
    # returning, just without the session columns.
    try:
        by_port = {entry["port"]: entry["sessions"] for entry in await send_session_control(LIST_SESSIONS_CMD)}
    except Exception:
        # Falls back to whatever this process knows, which on a primary that has
        # not finished binding its port yet is the right answer anyway, and on a
        # peer with no hub is an honest empty list.
        by_port = {entry["port"]: entry["sessions"] for entry in local_list_sessions()}

    results = []
    for app_id, app in APPS.items():
        probe = probe_port(app["port"])
        conn = get_conn(app["port"])
        sessions = by_port.get(app["port"], [])
        selected = next((s["sessionId"] for s in sessions if s.get("selected")), None)
        pinned = any(s.get("pinned") for s in sessions)
        row = {
            "id": app_id,
            "name": app["name"],
            "port": app["port"],
            "httpOpen": probe["open"],
            "wsConnected": conn.connected,
            "sessionCount": len(sessions),
            "selectedSession": selected,
        }
        if len(sessions) > 1 and not pinned:
            # Say so rather than let an agent assume its commands are going where
            # it is looking. Two tabs with no choice made is exactly the situation
            # that used to produce silently split state.
            row["note"] = (
                f"{len(sessions)} browser tabs are open on port {app['port']} and none has been chosen; "
                f"commands go to the most recent one ({selected}). "
                "Call list_sessions to see them and use_session to pin one."
            )
        results.append(row)
    return results

@mcp.tool()
async def send_command(port: int, cmd: str, payload: dict | None = None) -> Any:
    """
    Send an arbitrary JSON command to any app and return the result.
    port: 5173 (process), 5174 (circuit), 5175 (physics).
    """
    return await get_conn(port).send(cmd, payload)

@mcp.tool(description=get_doc(physics_docs, "list_sessions", (
    "List every browser tab (session) currently connected to each app, with its sessionId, "
    "label, page url, when the tab started and when it connected, and which one commands are "
    "currently being sent to. Commands go to exactly one tab per app — the pinned one, or the "
    "most recently connected if none is pinned. Use this when more than one tab is open and you "
    "need to know which one you are driving, then use_session to pin a different one."
)))
async def list_sessions() -> Any:
    return await send_session_control(LIST_SESSIONS_CMD)

@mcp.tool(description=get_doc(physics_docs, "use_session", (
    "Pin one browser tab as the target for every subsequent command on that app's port, so work "
    "stays in one tab instead of following whichever tab connected last. port is the app port "
    "(5173 Flux, 5174 Volt, 5175 Mesh, 5176 Etch); session_id comes from list_sessions. The pin "
    "clears itself if that tab closes, and selection falls back to the most recent tab."
)))
async def use_session(port: int, session_id: str) -> Any:
    return await send_session_control(USE_SESSION_CMD, {"port": port, "sessionId": session_id})

@mcp.tool(description=get_doc(physics_docs, "physics_export_cast", (
    "Export the current Mesh scene as a casting pattern: pattern/mould geometry plus gating, sized "
    "for the chosen metal's shrinkage. Returns a summary, any warnings, and the list of files "
    "produced — NOT their contents. Pass out_dir to actually get the files: each is written there "
    "and the returned path tells you where. Without out_dir you only learn the names and sizes. "
    "method is 'sand' (a reusable pattern rammed in green sand, which must draw) or 'lost-pla' (a "
    "pattern invested in plaster and burnt out, which may have undercuts). parting_from_base_mm "
    "places the parting line and riser options apply to sand only (default: chosen automatically). "
    "sprue_dia_mm and riser_dia_mm of 0 mean size them from the part."
)))
async def physics_export_cast(
    metal: str = "aluminium",
    method: str = "sand",
    parting_from_base_mm: float | None = None,
    add_gating: bool = True,
    sprue_dia_mm: float = 0,
    riser_dia_mm: float = 0,
    add_riser: bool = True,
    out_dir: str | None = None,
) -> Any:
    payload = compact_dict(
        metal=metal,
        method=method,
        partingFromBaseMm=parting_from_base_mm,
        addGating=add_gating,
        sprueDiaMm=sprue_dia_mm,
        riserDiaMm=riser_dia_mm,
        addRiser=add_riser,
        # With nowhere to put the file there is no reason to encode a megabyte
        # of mesh, push it through a socket and throw it away at this end.
        includeFiles=out_dir is not None,
    )
    # 180s because this is real geometry work in a worker, not a state read: a
    # slicing pass over a dense mesh will blow straight through the usual 60.
    result = await get_conn(Ph).send("EXPORT_CAST", payload, timeout=180.0)
    return _deliver_export(result, out_dir)

@mcp.tool(description=get_doc(physics_docs, "physics_export_machining", (
    "Export the current Mesh scene as a CNC machining program (G-code and setup sheets) for the "
    "given number of setups. Returns a summary, any warnings, and the list of files produced — "
    "NOT their contents. Pass out_dir to actually get the files: each is written there and the "
    "returned path tells you where. Without out_dir you only learn the names and sizes. "
    "stock_thickness_mm of 0 means take it from the part."
)))
async def physics_export_machining(
    sides: int = 1,
    stock_thickness_mm: float = 0,
    tool_dia_mm: float = 3.0,
    material: str = "aluminium",
    out_dir: str | None = None,
) -> Any:
    payload = compact_dict(
        sides=sides,
        stockThicknessMm=stock_thickness_mm,
        toolDiaMm=tool_dia_mm,
        material=material,
        includeFiles=out_dir is not None,
    )
    result = await get_conn(Ph).send("EXPORT_MACHINING", payload, timeout=180.0)
    return _deliver_export(result, out_dir)

# ── PhysBox: Flux (Process) Tools ──────────────────────────────────────────────

@mcp.tool(description=get_doc(process_docs, "process_get_state", "Return PhysBox: Flux state"))
async def process_get_state() -> Any:
    return await get_conn(P).send("GET_STATE")

@mcp.tool(description=get_doc(process_docs, "process_get_metrics", "Return PhysBox: Flux metrics"))
async def process_get_metrics() -> Any:
    return await get_conn(P).send("GET_METRICS")

@mcp.tool(description=get_doc(process_docs, "process_start", "Start simulation"))
async def process_start() -> Any:
    return await get_conn(P).send("START_SIM")

@mcp.tool(description=get_doc(process_docs, "process_stop", "Stop simulation"))
async def process_stop() -> Any:
    return await get_conn(P).send("STOP_SIM")

@mcp.tool(description=get_doc(process_docs, "process_reset", "Reset simulation"))
async def process_reset() -> Any:
    return await get_conn(P).send("RESET_SIM")

@mcp.tool(description=get_doc(process_docs, "process_list_presets", "List Flux presets"))
async def process_list_presets() -> Any:
    return await get_conn(P).send("LIST_PRESETS")

@mcp.tool(description=get_doc(process_docs, "process_load_preset", "Load Flux preset"))
async def process_load_preset(preset: str) -> Any:
    return await get_conn(P).send("LOAD_PRESET", {"preset": preset})

@mcp.tool(description=get_doc(process_docs, "process_get_library", "Get diagram library"))
async def process_get_library() -> Any:
    return await get_conn(P).send("GET_LIBRARY")

@mcp.tool(description=get_doc(process_docs, "process_save_library", "Save current diagram"))
async def process_save_library(name: str) -> Any:
    return await get_conn(P).send("SAVE_LIBRARY", {"name": name})

@mcp.tool(description=get_doc(process_docs, "process_set_nodes", "Set canvas nodes"))
async def process_set_nodes(nodes: list[Any]) -> Any:
    return await get_conn(P).send("SET_NODES", {"nodes": nodes})

@mcp.tool(description=get_doc(process_docs, "process_set_edges", "Set canvas edges"))
async def process_set_edges(edges: list[Any]) -> Any:
    return await get_conn(P).send("SET_EDGES", {"edges": edges})

@mcp.tool(description=get_doc(process_docs, "process_run_headless", "Run headless simulation"))
async def process_run_headless(ticks: int) -> Any:
    return await get_conn(P).send("RUN_HEADLESS", {"ticks": ticks}, timeout=30.0)

@mcp.tool(description=get_doc(process_docs, "process_get_history", "Get simulation logs"))
async def process_get_history() -> Any:
    return await get_conn(P).send("GET_HISTORY")

@mcp.tool(description=get_doc(process_docs, "process_run_monte_carlo", "Run Monte Carlo simulation"))
async def process_run_monte_carlo(runs: int = 100, ticks: int = 3600) -> Any:
    return await get_conn(P).send("RUN_MONTE_CARLO", {"runs": runs, "ticks": ticks}, timeout=60.0)

@mcp.tool(description=get_doc(process_docs, "process_run_optimizer", "Run optimizer sweep"))
async def process_run_optimizer(
    targetMetric: str,
    params: list[Any],
    mode: str = "maximize",
    ticks: int = 1200,
    strategy: str = "grid_sweep",
    populationSize: int = 12,
    generations: int = 6,
    mutationRate: float = 0.25,
) -> Any:
    return await get_conn(P).send(
        "RUN_OPTIMIZER",
        {
            "targetMetric": targetMetric,
            "mode": mode,
            "ticks": ticks,
            "params": params,
            "strategy": strategy,
            "populationSize": populationSize,
            "generations": generations,
            "mutationRate": mutationRate,
        },
        timeout=60.0
    )

@mcp.tool(description=get_doc(process_docs, "process_get_schema", "Return PhysBox: Flux schema"))
async def process_get_schema() -> Any:
    return get_reference_docs(process_docs)

# ── PhysBox: Volt (Circuit) Tools ─────────────────────────────────────────────

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_state", "Return PhysBox: Volt state"))
async def circuit_get_state() -> Any:
    return await get_conn(C).send("GET_STATE")

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_components", "Get components list"))
async def circuit_get_components() -> Any:
    return await get_conn(C).send("GET_COMPONENTS")

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_edges", "Get wires/edges list"))
async def circuit_get_edges() -> Any:
    return await get_conn(C).send("GET_EDGES")

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_summary", "Return lightweight circuit summary"))
async def circuit_get_summary() -> Any:
    return await get_conn(C).send("GET_SUMMARY")

@mcp.tool(description=get_doc(circuit_docs, "circuit_run_sim", "Run SPICE simulation"))
async def circuit_run_sim() -> Any:
    return await get_conn(C).send("RUN_SIM")

@mcp.tool(description=get_doc(circuit_docs, "circuit_stop_sim", "Stop SPICE simulation"))
async def circuit_stop_sim() -> Any:
    return await get_conn(C).send("STOP_SIM")

@mcp.tool(description=get_doc(circuit_docs, "circuit_toggle_probe", "Toggle probe mode"))
async def circuit_toggle_probe() -> Any:
    return await get_conn(C).send("TOGGLE_PROBE")

@mcp.tool(description=get_doc(circuit_docs, "circuit_list_presets", "List built-in and user circuit presets"))
async def circuit_list_presets() -> Any:
    return await get_conn(C).send("LIST_PRESETS")

@mcp.tool(description=get_doc(circuit_docs, "circuit_load_preset", "Load Volt preset"))
async def circuit_load_preset(preset: str) -> Any:
    return await get_conn(C).send("LOAD_PRESET", {"preset": preset})

@mcp.tool(description=get_doc(circuit_docs, "circuit_save_preset", "Save active circuit canvas as user preset"))
async def circuit_save_preset(name: str, noteCard: str | None = None, recommendedSimLength: float | None = None) -> Any:
    payload = compact_dict(name=name, noteCard=noteCard, recommendedSimLength=recommendedSimLength)
    return await get_conn(C).send("SAVE_PRESET", payload)

@mcp.tool(description=get_doc(circuit_docs, "circuit_delete_preset", "Delete user circuit preset by key"))
async def circuit_delete_preset(key: str) -> Any:
    return await get_conn(C).send("DELETE_PRESET", {"key": key})

@mcp.tool(description=get_doc(circuit_docs, "circuit_set_nodes", "Set circuit components"))
async def circuit_set_nodes(nodes: list[Any]) -> Any:
    return await get_conn(C).send("SET_NODES", {"nodes": nodes})

@mcp.tool(description=get_doc(circuit_docs, "circuit_set_edges", "Set circuit wires"))
async def circuit_set_edges(edges: list[Any]) -> Any:
    return await get_conn(C).send("SET_EDGES", {"edges": edges})

@mcp.tool(description=get_doc(circuit_docs, "circuit_set_circuit", "Set both components and wires and optionally run SPICE simulation"))
async def circuit_set_circuit(nodes: list[Any], edges: list[Any], runSim: bool = True) -> Any:
    return await get_conn(C).send("SET_CIRCUIT", {"nodes": nodes, "edges": edges, "runSim": runSim})

@mcp.tool(description=get_doc(circuit_docs, "circuit_validate_circuit", "Validate circuit nodes and wire connections for missing nodes or ground errors."))
async def circuit_validate_circuit(nodes: list[Any] | None = None, edges: list[Any] | None = None) -> Any:
    payload = compact_dict(nodes=nodes, edges=edges)
    return await get_conn(C).send("VALIDATE_CIRCUIT", payload)

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_schema", "Return PhysBox: Volt schema"))
async def circuit_get_schema() -> Any:
    return get_reference_docs(circuit_docs)

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_waveforms", "Return component waveforms"))
async def circuit_get_waveforms() -> Any:
    return await get_conn(C).send("GET_WAVEFORMS")

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_history", "Return simulation waveform history"))
async def circuit_get_history() -> Any:
    return await get_conn(C).send("GET_HISTORY")

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_screenshot", "Capture current rendered frame of circuit canvas as a PNG image."))
async def circuit_get_screenshot() -> Any:
    result = await get_conn(C).send("SCREENSHOT")
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error") if isinstance(result, dict) else "Unknown error"
        raise RuntimeError(f"Screenshot failed: {error}")
    data_url = result.get("dataUrl", "")
    if "," not in data_url:
        raise ValueError("Invalid image data URL returned from simulator")
    b64 = data_url.split(",", 1)[1]
    return Image(data=base64.b64decode(b64), format="png")

@mcp.tool(description=get_doc(circuit_docs, "circuit_upload_audio", "Upload audio samples to a microphone node."))
async def circuit_upload_audio(
    nodeId: str,
    values: list[float] | None = None,
    sampleRate: float = 8000.0,
    pwlData: list[dict] | None = None
) -> Any:
    payload = compact_dict(
        nodeId=nodeId,
        values=values,
        sampleRate=sampleRate,
        pwlData=pwlData
    )
    return await get_conn(C).send("UPLOAD_AUDIO", payload)

@mcp.tool(description=get_doc(circuit_docs, "circuit_download_audio", "Download audio waveforms from a speaker node."))
async def circuit_download_audio(
    nodeId: str,
    sampleRate: float = 8000.0,
    acCouple: bool | None = None,
    normalize: bool | None = None,
    voltageScale: float | None = None
) -> Any:
    payload = compact_dict(
        nodeId=nodeId,
        sampleRate=sampleRate,
        acCouple=acCouple,
        normalize=normalize,
        voltageScale=voltageScale
    )
    return await get_conn(C).send("GET_SPEAKER_AUDIO", payload)

@mcp.tool(description=get_doc(circuit_docs, "circuit_reset", "Reset the simulation to t=0"))
async def circuit_reset() -> Any:
    return await get_conn(C).send("RESET")

@mcp.tool(description=get_doc(circuit_docs, "circuit_update_component", "Update one component in place"))
async def circuit_update_component(id: str, updates: dict) -> Any:
    # Sent as nodeId, not id: the envelope already owns "id", and a payload key
    # of the same name is exactly what used to collide with it.
    return await get_conn(C).send("UPDATE_COMPONENT", {"nodeId": id, "updates": updates})

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_pcb_layout", "Place and route the board, and return what came out"))
async def circuit_get_pcb_layout(
    includePads: bool = False,
    includeTraces: bool = False,
    component: str | None = None,
    options: dict | None = None,
) -> Any:
    payload: dict[str, Any] = {"includePads": includePads, "includeTraces": includeTraces}
    if component:
        payload["component"] = component
    if options:
        payload["options"] = options
    return await get_conn(C).send("GET_PCB_LAYOUT", payload, timeout=120.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_pcb_preview", "Render the laid-out board as a PNG, from either face"))
async def circuit_get_pcb_preview(
    view: str = "copper",
    padNumbers: bool = True,
    pxPerMm: float = 12,
    options: dict | None = None,
) -> Any:
    payload: dict[str, Any] = {"view": view, "padNumbers": padNumbers, "pxPerMm": pxPerMm}
    if options:
        payload["options"] = options
    result = await get_conn(C).send("GET_PCB_PREVIEW", payload, timeout=120.0)
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error") if isinstance(result, dict) else "Unknown error"
        raise RuntimeError(f"PCB preview failed: {error}")
    data_url = result.get("dataUrl", "")
    if "," not in data_url:
        raise ValueError("Invalid image data URL returned from Volt")
    b64 = data_url.split(",", 1)[1]
    return Image(data=base64.b64decode(b64), format="png")

@mcp.tool(description=get_doc(circuit_docs, "circuit_delete_component", "Delete components and the wires attached to them"))
async def circuit_delete_component(id: str | list[str]) -> Any:
    # Sent as nodeIds, not id: the envelope already owns "id", the same
    # collision circuit_update_component works around.
    ids = id if isinstance(id, list) else [id]
    return await get_conn(C).send("DELETE_COMPONENT", {"nodeIds": ids})

@mcp.tool(description=get_doc(circuit_docs, "circuit_define_mcu", "Define an MCU's pins, package geometry and program"))
async def circuit_define_mcu(
    nodeId: str,
    presetKey: str | None = None,
    pins: list[Any] | None = None,
    geometry: dict[str, Any] | None = None,
    code: str | None = None,
    label: str | None = None,
) -> Any:
    # Only the arguments actually given are forwarded: Volt reads an absent
    # `pins` as "keep what the part has" and an empty list as "a part with no
    # pins", which it refuses. Sending None for everything unset would turn
    # every omitted argument into the second of those.
    payload: dict[str, Any] = {"nodeId": nodeId}
    for key, value in (
        ("presetKey", presetKey),
        ("pins", pins),
        ("geometry", geometry),
        ("code", code),
        ("label", label),
    ):
        if value is not None:
            payload[key] = value
    return await get_conn(C).send("DEFINE_MCU", payload)

@mcp.tool(description=get_doc(circuit_docs, "circuit_list_mcu_presets", "List the built-in MCU presets"))
async def circuit_list_mcu_presets() -> Any:
    return await get_conn(C).send("LIST_MCU_PRESETS")

# ── Volt: the machine ─────────────────────────────────────────────────────────
#
# Driving the CNC that mills the board, not just designing it.
#
# Everything that can move an axis is refused by the app unless the person at the
# machine has armed it — a click in Volt's own UI, which is deliberately not
# reachable from here. `circuit_machine_arm` exists to say so clearly rather than
# leave an agent guessing why motion is refused.
#
# Reading state, trimming a running cut, pausing, cancelling and e-stopping are
# never gated. A permission system that could stand between someone and stopping
# a machine would be worse than none.
#
# The timeouts below are longer than the default ten seconds because these calls
# wait on physical motion: a probe descends slowly by design, a mesh probe is one
# descent per point, and a homing cycle crosses the whole bed twice.

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_status", "Report the milling machine's state, position and whether it is armed"))
async def circuit_machine_status() -> Any:
    return await get_conn(C).send("MACHINE_STATUS")

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_settings", "The controller's $$ settings, as read on connect"))
async def circuit_machine_settings() -> Any:
    return await get_conn(C).send("MACHINE_SETTINGS")

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_list_devices", "List the Tekno Boxes paired to this account"))
async def circuit_machine_list_devices() -> Any:
    return await get_conn(C).send("MACHINE_LIST_DEVICES", timeout=20.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_arm", "Explains that only the person at the machine can allow Claude to move it"))
async def circuit_machine_arm() -> Any:
    # Cannot arm, by design. The whole safety story rests on a person deciding
    # when the machine may move, so this reports the refusal rather than
    # offering a way round it.
    return await get_conn(C).send("MACHINE_ARM")

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_disarm", "Hand back permission to move the machine, stopping anything running"))
async def circuit_machine_disarm() -> Any:
    return await get_conn(C).send("MACHINE_DISARM")

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_connect", "Open the link to the machine over USB or WiFi"))
async def circuit_machine_connect(transport: str | None = None, deviceId: str | None = None) -> Any:
    # USB opens a native port-picker in the browser that only the person at the
    # keyboard can answer, so this can sit waiting on a human.
    return await get_conn(C).send(
        "MACHINE_CONNECT", compact_dict(transport=transport, deviceId=deviceId), timeout=60.0
    )

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_disconnect", "Close the machine link"))
async def circuit_machine_disconnect() -> Any:
    return await get_conn(C).send("MACHINE_DISCONNECT", timeout=20.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_jog", "Move the tool by a relative distance in mm"))
async def circuit_machine_jog(
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    feedRate: float | None = None,
) -> Any:
    return await get_conn(C).send(
        "MACHINE_JOG", compact_dict(x=x, y=y, z=z, feedRate=feedRate), timeout=60.0
    )

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_home", "Run the homing cycle against the limit switches"))
async def circuit_machine_home() -> Any:
    # A homing cycle crosses the bed twice at a searching feed.
    return await get_conn(C).send("MACHINE_HOME", timeout=180.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_unlock", "Clear GRBL's alarm lockout"))
async def circuit_machine_unlock() -> Any:
    return await get_conn(C).send("MACHINE_UNLOCK", timeout=20.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_goto_origin", "Lift, then travel to the work origin"))
async def circuit_machine_goto_origin(safeZMm: float | None = None) -> Any:
    return await get_conn(C).send("MACHINE_GOTO_ORIGIN", compact_dict(safeZMm=safeZMm), timeout=120.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_zero_xy", "Set the current XY position as the work origin"))
async def circuit_machine_zero_xy() -> Any:
    return await get_conn(C).send("MACHINE_ZERO_XY", timeout=30.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_zero_z", "Set work Z0 by probing the copper, or a touch plate"))
async def circuit_machine_zero_z(
    touchPlateMm: float | None = None,
    surfaceOffsetMm: float | None = None,
) -> Any:
    # Two stabs — a fast one to find the surface and a slow one to measure it —
    # and the slow one is slow on purpose.
    return await get_conn(C).send(
        "MACHINE_ZERO_Z", compact_dict(touchPlateMm=touchPlateMm, surfaceOffsetMm=surfaceOffsetMm),
        timeout=300.0,
    )

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_probe_surface", "Probe a grid across the board and keep the height map"))
async def circuit_machine_probe_surface(cols: int | None = None, rows: int | None = None) -> Any:
    # One slow descent per point, plus a verification re-probe. A 6x6 grid is
    # thirty-seven probes and takes minutes.
    return await get_conn(C).send(
        "MACHINE_PROBE_SURFACE", compact_dict(cols=cols, rows=rows), timeout=1800.0
    )

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_frame_job", "Trace the board outline with the spindle off"))
async def circuit_machine_frame_job(
    safeZMm: float | None = None,
    feedRate: float | None = None,
) -> Any:
    return await get_conn(C).send(
        "MACHINE_FRAME_JOB", compact_dict(safeZMm=safeZMm, feedRate=feedRate), timeout=600.0
    )

@mcp.tool(description=get_doc(circuit_docs, "circuit_mill_pcb", "Mill the board on the canvas: isolation, drilling and profiling"))
async def circuit_mill_pcb(options: dict | None = None) -> Any:
    # Returns once the job is under way, not once it has finished — a board is
    # tens of minutes of cutting, and holding a tool call open for that would
    # time out long before it ended. Poll circuit_machine_status.
    return await get_conn(C).send("MILL_PCB", compact_dict(options=options), timeout=300.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_pause", "Feed hold: stop without losing position"))
async def circuit_machine_pause() -> Any:
    return await get_conn(C).send("MACHINE_PAUSE", timeout=30.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_resume", "Pick a paused job back up"))
async def circuit_machine_resume() -> Any:
    return await get_conn(C).send("MACHINE_RESUME", timeout=60.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_cancel", "Stop the job and drop the rest of the program"))
async def circuit_machine_cancel() -> Any:
    return await get_conn(C).send("MACHINE_CANCEL", timeout=30.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_estop", "Emergency stop: soft-reset the controller and stop the spindle"))
async def circuit_machine_estop() -> Any:
    # Never gated and never refused. Kept short deliberately: if this one is
    # slow to answer, the answer is not worth waiting for.
    return await get_conn(C).send("MACHINE_ESTOP", timeout=15.0)

@mcp.tool(description=get_doc(circuit_docs, "circuit_machine_trim", "Trim feed, spindle or rapid on the running job"))
async def circuit_machine_trim(
    feed: Any = None,
    spindle: Any = None,
    rapid: int | None = None,
) -> Any:
    # Steps, not targets — GRBL has no command to set a figure. The app rejects
    # anything else rather than silently doing nothing with it.
    return await get_conn(C).send(
        "MACHINE_TRIM", compact_dict(feed=feed, spindle=spindle, rapid=rapid), timeout=30.0
    )

@mcp.tool(description=get_doc(circuit_docs, "circuit_get_note_cards", "Return note cards"))
async def circuit_get_note_cards() -> Any:
    return await get_conn(C).send("GET_NOTE_CARDS")

@mcp.tool(description=get_doc(circuit_docs, "circuit_set_note_cards", "Replace note cards"))
async def circuit_set_note_cards(noteCards: list[Any]) -> Any:
    return await get_conn(C).send("SET_NOTE_CARDS", {"noteCards": noteCards})

# ── PhysBox: Mesh (Physics) Tools ─────────────────────────────────────────────

@mcp.tool(description=get_doc(physics_docs, "physics_validate_scad", "Validate OpenSCAD code and return compilation result or errors"))
async def physics_validate_scad(scad: str) -> Any:
    # Two minutes, not the default ten seconds. OpenSCAD is the slowest thing in
    # the app and the cost is in CIRCLES: a ring at $fn=60 compiles in under a
    # second, the same ring at $fn=360 takes seven, and a plate with eight holes
    # at $fn=180 takes half a minute. At the default the reply came back as a
    # timeout, which reads exactly like the code being rejected — and a coarser
    # version of the same design "worked", which is the wrong lesson entirely.
    return await get_conn(Ph).send("VALIDATE_SCAD", {"scad": scad}, timeout=120.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_state", "Return PhysBox: Mesh state"))
async def physics_get_state() -> Any:
    return await get_conn(Ph).send("GET_STATE")

@mcp.tool(description=get_doc(physics_docs, "physics_get_scene", "Return physics scene graph"))
async def physics_get_scene() -> Any:
    return await get_conn(Ph).send("GET_SCENE")

@mcp.tool(description=get_doc(physics_docs, "physics_get_scene_summary", "Return a lightweight scene summary"))
async def physics_get_scene_summary() -> Any:
    return await get_conn(Ph).send("GET_SCENE_SUMMARY")

@mcp.tool(description=get_doc(physics_docs, "physics_play", "Start physics simulation"))
async def physics_play() -> Any:
    return await get_conn(Ph).send("PLAY")

@mcp.tool(description=get_doc(physics_docs, "physics_stop", "Stop physics simulation"))
async def physics_stop() -> Any:
    return await get_conn(Ph).send("STOP")

@mcp.tool(description=get_doc(physics_docs, "physics_toggle_play", "Toggle play/pause"))
async def physics_toggle_play() -> Any:
    return await get_conn(Ph).send("TOGGLE_PLAY")

@mcp.tool(description=get_doc(physics_docs, "physics_reset", "Reset physics simulation"))
async def physics_reset() -> Any:
    return await get_conn(Ph).send("RESET")

@mcp.tool(description=get_doc(physics_docs, "physics_list_presets", "List Mesh presets"))
async def physics_list_presets() -> Any:
    return await get_conn(Ph).send("LIST_PRESETS")

@mcp.tool(description=get_doc(physics_docs, "physics_load_preset", "Load Mesh preset"))
async def physics_load_preset(preset: str) -> Any:
    return await get_conn(Ph).send("LOAD_PRESET", {"preset": preset})

@mcp.tool(description=get_doc(physics_docs, "physics_save_preset", "Save active physics scene as a user preset"))
async def physics_save_preset(name: str) -> Any:
    return await get_conn(Ph).send("SAVE_PRESET", {"preset": name})

@mcp.tool(description=get_doc(physics_docs, "physics_delete_preset", "Delete a user physics preset"))
async def physics_delete_preset(preset: str) -> Any:
    return await get_conn(Ph).send("DELETE_PRESET", {"preset": preset})

@mcp.tool(description=get_doc(physics_docs, "physics_check_collisions", "Check for initial axis-aligned bounding box overlaps/interpenetrations between scene bodies at t=0."))
async def physics_check_collisions() -> Any:
    return await get_conn(Ph).send("CHECK_COLLISIONS")

@mcp.tool(description=get_doc(physics_docs, "physics_measure", "Measure a distance or an angle in the drawn scene, snapping to real features"))
async def physics_measure(
    start: list[float],
    end: list[float],
    corner: list[float] | None = None,
    snap: bool = True,
    withinMm: float = 3.0,
) -> Any:
    # start/end rather than from/to: `from` is a Python keyword, and a parameter
    # called from_ is a wart every caller would have to see.
    payload = compact_dict(**{"from": start, "to": end, "corner": corner, "snap": snap, "withinMm": withinMm})
    return await get_conn(Ph).send("MEASURE", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_import_stl", "Import a binary or ASCII STL file into the 3D physics simulation as a parametric OpenSCAD node, raw mesh, or primitive."))
async def physics_import_stl(
    stlData: str,
    name: str = "imported_stl",
    importMode: str = "scad_parametric",
    pos: list[float] | None = None,
    scale: float | list[float] | None = None,
    dynamic: bool = True
) -> Any:
    payload = compact_dict(
        stlData=stlData,
        name=name,
        importMode=importMode,
        pos=pos,
        scale=scale,
        dynamic=dynamic
    )
    return await get_conn(Ph).send("IMPORT_STL", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_set_environment", "Set environment parameters"))
async def physics_set_environment(
    gravityZ: float | None = None,
    windX: float | None = None,
    windY: float | None = None,
    density: float | None = None,
    floorFriction: float | None = None,
) -> Any:
    payload = compact_dict(
        gravityZ=gravityZ,
        windX=windX,
        windY=windY,
        density=density,
        floorFriction=floorFriction,
    )
    return await get_conn(Ph).send("SET_ENVIRONMENT", payload)

@mcp.tool(description=get_doc(physics_docs, "physics_update_scene", "Replace scene graph"))
async def physics_update_scene(sceneGraph: list[Any]) -> Any:
    return await get_conn(Ph).send("UPDATE_SCENE", {"sceneGraph": sceneGraph}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_camera",
    "Return the 3D viewport camera's current position and look-at target, in MuJoCo world space. "
    "Reflects whatever is actually on screen right now, including manual orbiting/panning done by "
    "a human in the browser — not just the last SET_CAMERA call."))
async def physics_get_camera() -> Any:
    return await get_conn(Ph).send("GET_CAMERA")

@mcp.tool(description=get_doc(physics_docs, "physics_set_camera",
    "Point the 3D viewport camera at an explicit position/target (both MuJoCo world-space [x,y,z], "
    "same convention as every other pos field), or pass preset='perspective'|'topDown' to reset to "
    "a built-in view. Use this instead of rotating a body to try to line it up with a fixed camera — "
    "the default 'perspective' view is a diagonal 3/4 pose, not aligned to any single world axis, so "
    "no combination of body pos/euler will make it look 'front-on' to the camera."))
async def physics_set_camera(
    position: list[float] | None = None,
    target: list[float] | None = None,
    preset: str | None = None,
) -> Any:
    payload = compact_dict(preset=preset, position=position, target=target)
    return await get_conn(Ph).send("SET_CAMERA", payload)

@mcp.tool(description=get_doc(physics_docs, "physics_get_screenshot", "Capture current rendered frame of 3D viewport as a PNG image."))
async def physics_get_screenshot() -> Any:
    result = await get_conn(Ph).send("SCREENSHOT")
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error") if isinstance(result, dict) else "Unknown error"
        raise RuntimeError(f"Screenshot failed: {error}")
    data_url = result.get("dataUrl", "")
    if "," not in data_url:
        raise ValueError("Invalid image data URL returned from simulator")
    b64 = data_url.split(",", 1)[1]
    return Image(data=base64.b64decode(b64), format="png")

@mcp.tool(description=get_doc(physics_docs, "physics_get_schema", "Return PhysBox: Mesh schema"))
async def physics_get_schema() -> Any:
    return get_reference_docs(physics_docs)

@mcp.tool(description=get_doc(physics_docs, "physics_build_scene", "Build scene from bodies"))
async def physics_build_scene(bodies: list[Any]) -> Any:
    return await get_conn(Ph).send("BUILD_SCENE", {"bodies": bodies}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_run_headless", "Run simulation headlessly"))
async def physics_run_headless(ticks: int = 300, stride: int = 1, bodies: list[str] | None = None) -> Any:
    payload: dict[str, Any] = {"ticks": ticks, "stride": stride}
    if bodies:
        payload["bodies"] = bodies
    return await get_conn(Ph).send("RUN_HEADLESS", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_history", "Return telemetry history"))
async def physics_get_history(
    since_time: float | None = None,
    last: int | None = None,
    stride: int | None = None,
    max_frames: int | None = None,
    bodies: list[str] | None = None,
    include: list[str] | None = None,
) -> Any:
    payload: dict[str, Any] = {}
    if since_time is not None:
        payload["sinceTime"] = since_time
    if last is not None:
        payload["last"] = last
    if stride is not None:
        payload["stride"] = stride
    if max_frames is not None:
        payload["maxFrames"] = max_frames
    if bodies:
        payload["bodies"] = bodies
    if include:
        payload["include"] = include
    return await get_conn(Ph).send("GET_HISTORY", payload)

@mcp.tool(description=get_doc(physics_docs, "physics_get_telemetry", "Return latest single frame telemetry"))
async def physics_get_telemetry() -> Any:
    return await get_conn(Ph).send("GET_TELEMETRY")

@mcp.tool(description=get_doc(physics_docs, "physics_get_objects", "Return scene objects"))
async def physics_get_objects() -> Any:
    return await get_conn(Ph).send("GET_OBJECTS")

@mcp.tool(description=get_doc(physics_docs, "physics_get_object", "Return single object by ID"))
async def physics_get_object(id: str) -> Any:
    return await get_conn(Ph).send("GET_OBJECT", {"targetId": id})

@mcp.tool(description=get_doc(physics_docs, "physics_update_object", "Update single object by ID"))
async def physics_update_object(id: str, updates: dict) -> Any:
    return await get_conn(Ph).send("UPDATE_OBJECT", {"targetId": id, "updates": updates}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_set_color", "Set a body's base colour"))
async def physics_set_color(id: str, rgba: list[float], geomName: str | None = None) -> Any:
    return await get_conn(Ph).send("SET_COLOR", {"targetId": id, "rgba": rgba, "geomName": geomName}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_paint", "Brush colour onto part of a geom's surface"))
async def physics_paint(
    id: str,
    at: list[Any],
    rgba: list[float],
    radius: float = 0.008,
    geomName: str | None = None,
    flow: float = 1.0,
    erase: bool = False,
) -> Any:
    return await get_conn(Ph).send(
        "PAINT",
        {"targetId": id, "at": at, "rgba": rgba, "radius": radius, "geomName": geomName, "flow": flow, "erase": erase},
        timeout=60.0,
    )

@mcp.tool(description=get_doc(physics_docs, "physics_clear_paint", "Remove brushed paint"))
async def physics_clear_paint(id: str | None = None, geomName: str | None = None) -> Any:
    return await get_conn(Ph).send("CLEAR_PAINT", {"targetId": id, "geomName": geomName})

@mcp.tool(description=get_doc(physics_docs, "physics_create_sculpt", "Add a sculptable body from a base shape"))
async def physics_create_sculpt(
    name: str | None = None,
    base: str = "sphere",
    pos: list[float] | None = None,
) -> Any:
    payload = compact_dict(name=name, base=base, pos=pos)
    return await get_conn(Ph).send("CREATE_SCULPT", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_set_sculpt_base", "Replace a sculpt's base shape"))
async def physics_set_sculpt_base(id: str, base: str) -> Any:
    return await get_conn(Ph).send("SET_SCULPT_BASE", {"targetId": id, "base": base}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_sculpt", "Brush a sculpt body's surface"))
async def physics_sculpt(
    id: str,
    at: list[Any],
    brush: str = "draw",
    radius: float = 0.04,
    strength: float = 0.5,
    invert: bool = False,
    symmetry: str | None = None,
    detail: float | None = None,
    dynamicTopology: bool | None = None,
    delta: list[float] | None = None,
) -> Any:
    payload = compact_dict(
        targetId=id,
        at=at,
        brush=brush,
        radius=radius,
        strength=strength,
        invert=invert,
        symmetry=symmetry,
        detail=detail,
        dynamicTopology=dynamicTopology,
        delta=delta,
    )
    return await get_conn(Ph).send("SCULPT", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_add_object", "Add one body to the scene"))
async def physics_add_object(body: dict[str, Any]) -> Any:
    # ADD_OBJECT compiles any scad or boolean the body carries before it
    # answers, the same as build_scene does, so it gets build_scene's timeout
    # rather than the default ten seconds.
    return await get_conn(Ph).send("ADD_OBJECT", {"body": body}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_delete_object", "Delete a body from the scene"))
async def physics_delete_object(id: str) -> Any:
    return await get_conn(Ph).send("DELETE_OBJECT", {"targetId": id}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_set_constraint", "Weld or pin a body, and set what breaks the weld"))
async def physics_set_constraint(
    id: str,
    weld_to: str | None = None,
    connect_to: str | None = None,
    break_force_n: float | None = None,
    break_torque_nm: float | None = None,
    break_hold_steps: int | None = None,
    clear_weld: bool = False,
    clear_connect: bool = False,
    clear_break: bool = False,
) -> Any:
    # Omitted means "leave it alone" and null means "clear it", which one
    # optional argument cannot say on its own — hence the explicit clear_* flags.
    payload: dict[str, Any] = {"targetId": id}
    if clear_weld:
        payload["weldTo"] = None
    elif weld_to is not None:
        payload["weldTo"] = weld_to
    if clear_connect:
        payload["connectTo"] = None
    elif connect_to is not None:
        payload["connectTo"] = connect_to
    if clear_break:
        payload["breakForceN"] = None
        payload["breakTorqueNm"] = None
        payload["breakHoldSteps"] = None
    else:
        if break_force_n is not None:
            payload["breakForceN"] = break_force_n
        if break_torque_nm is not None:
            payload["breakTorqueNm"] = break_torque_nm
        if break_hold_steps is not None:
            payload["breakHoldSteps"] = break_hold_steps
    return await get_conn(Ph).send("SET_CONSTRAINT", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_set_impact", "What a body does when it is hit hard"))
async def physics_set_impact(
    id: str,
    shatter_impulse_ns: float | None = None,
    shatter_pieces: int | None = None,
    shatter_pattern: str | None = None,
    shatter_spread: float | None = None,
    shatter_seed: int | None = None,
    shatter_depth: int | None = None,
    dent_yield_ns: float | None = None,
    dent_depth_per_ns: float | None = None,
    dent_radius: float | None = None,
    dent_max_depth: float | None = None,
    pierce_impulse_ns: float | None = None,
    deform_collision: bool | None = None,
    geom_name: str | None = None,
    clear_shatter: bool = False,
    clear_dent: bool = False,
) -> Any:
    payload: dict[str, Any] = {"targetId": id}
    if clear_shatter:
        payload["shatterImpulseNs"] = None
    elif shatter_impulse_ns is not None:
        payload["shatterImpulseNs"] = shatter_impulse_ns
    for key, value in (
        ("shatterPieces", shatter_pieces),
        ("shatterPattern", shatter_pattern),
        ("shatterSpread", shatter_spread),
        ("shatterSeed", shatter_seed),
        ("shatterDepth", shatter_depth),
    ):
        if value is not None:
            payload[key] = value
    if clear_dent:
        payload["dentYieldNs"] = None
    elif dent_yield_ns is not None:
        payload["dentYieldNs"] = dent_yield_ns
    for key, value in (
        ("dentDepthPerNs", dent_depth_per_ns),
        ("dentRadius", dent_radius),
        ("dentMaxDepth", dent_max_depth),
        ("pierceImpulseNs", pierce_impulse_ns),
        ("deformCollision", deform_collision),
        ("geomName", geom_name),
    ):
        if value is not None:
            payload[key] = value
    return await get_conn(Ph).send("SET_IMPACT", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_set_crumple", "A joint that folds when overloaded and stays folded"))
async def physics_set_crumple(
    id: str,
    joint_name: str | None = None,
    crumple_torque_nm: float | None = None,
    crumple_range_deg: list[float] | None = None,
    crumple_damping_after: float | None = None,
    clear: bool = False,
) -> Any:
    payload: dict[str, Any] = {"targetId": id}
    if joint_name is not None:
        payload["jointName"] = joint_name
    if clear:
        payload["crumpleTorqueNm"] = None
    elif crumple_torque_nm is not None:
        payload["crumpleTorqueNm"] = crumple_torque_nm
    if crumple_range_deg is not None:
        payload["crumpleRangeDeg"] = crumple_range_deg
    if crumple_damping_after is not None:
        payload["crumpleDampingAfter"] = crumple_damping_after
    return await get_conn(Ph).send("SET_CRUMPLE", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_breaks", "What has sheared off during this run"))
async def physics_get_breaks() -> Any:
    return await get_conn(Ph).send("GET_BREAKS", {})

@mcp.tool(description=get_doc(physics_docs, "physics_restore_break", "Put a broken weld back"))
async def physics_restore_break(key: str | None = None) -> Any:
    return await get_conn(Ph).send("RESTORE_BREAK", {"key": key} if key else {}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_probe_sculpt", "Find the surface nearest some points"))
async def physics_probe_sculpt(id: str, at: list[Any]) -> Any:
    return await get_conn(Ph).send("PROBE_SCULPT", {"targetId": id, "at": at})

@mcp.tool(description=get_doc(physics_docs, "physics_undo_sculpt", "Undo the last sculpt stroke"))
async def physics_undo_sculpt(id: str) -> Any:
    return await get_conn(Ph).send("UNDO_SCULPT", {"targetId": id}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_sculpt_cut", "Cut a hole through a sculpt, or a piece off it"))
async def physics_sculpt_cut(
    id: str,
    polygon: list[list[float]],
    direction: list[float] | None = None,
    mode: str | None = None,
    cap: bool | None = None,
) -> Any:
    payload = compact_dict(targetId=id, polygon=polygon, direction=direction, mode=mode, cap=cap)
    return await get_conn(Ph).send("SCULPT_CUT", payload, timeout=90.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_sculpt", "Return a sculpt body's mesh statistics"))
async def physics_get_sculpt(id: str) -> Any:
    return await get_conn(Ph).send("GET_SCULPT", {"targetId": id})

# --- Lattice modelling -------------------------------------------------------
#
# The counterpart to sculpting, and the easier of the two to drive without a
# screen: a lattice's whole state is points on a grid, so a coordinate can be
# said rather than probed for, and it means the same thing on the next call.
# Everything here is millimetres in the body's own frame.

@mcp.tool(description=get_doc(physics_docs, "physics_create_lattice", "Add a grid-modelled body"))
async def physics_create_lattice(
    name: str | None = None,
    pos: list[float] | None = None,
    sizeMm: float = 40.0,
    edit: bool = False,
) -> Any:
    payload = compact_dict(name=name, pos=pos, sizeMm=sizeMm, edit=edit)
    return await get_conn(Ph).send("CREATE_LATTICE", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_lattice", "Read a lattice body's faces back"))
async def physics_get_lattice(id: str) -> Any:
    return await get_conn(Ph).send("GET_LATTICE", {"targetId": id})

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_faces", "Draw faces on a lattice body"))
async def physics_lattice_faces(id: str, faces: list[Any], mirror: str | None = None) -> Any:
    payload = compact_dict(targetId=id, faces=faces, mirror=mirror)
    return await get_conn(Ph).send("LATTICE_FACES", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_extrude", "Push a face out along its axis"))
async def physics_lattice_extrude(
    id: str,
    face: list[Any],
    distanceMm: float,
    axis: str | None = None,
    mirror: str | None = None,
) -> Any:
    payload = compact_dict(targetId=id, face=face, distanceMm=distanceMm, axis=axis, mirror=mirror)
    return await get_conn(Ph).send("LATTICE_EXTRUDE", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_inset", "Shrink a face inside itself"))
async def physics_lattice_inset(id: str, face: list[Any], amountMm: float, mirror: str | None = None) -> Any:
    payload = compact_dict(targetId=id, face=face, amountMm=amountMm, mirror=mirror)
    return await get_conn(Ph).send("LATTICE_INSET", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_bevel", "Cut the corners off a face"))
async def physics_lattice_bevel(id: str, face: list[Any], amountMm: float, mirror: str | None = None) -> Any:
    payload = compact_dict(targetId=id, face=face, amountMm=amountMm, mirror=mirror)
    return await get_conn(Ph).send("LATTICE_BEVEL", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_bevel_edges", "Chamfer or round edges of the solid"))
async def physics_lattice_bevel_edges(
    id: str,
    edges: list[Any],
    radiusMm: float,
    mode: str = "chamfer",
    mirror: str | None = None,
    loop: bool = False,
) -> Any:
    payload = compact_dict(targetId=id, edges=edges, radiusMm=radiusMm, mode=mode, mirror=mirror, loop=loop)
    return await get_conn(Ph).send("LATTICE_BEVEL_EDGES", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_circle", "Place a circle or regular polygon as one face"))
async def physics_lattice_circle(
    id: str,
    centre: list[float],
    diameterMm: float,
    axis: str = "z",
    sides: int = 0,
    mirror: str | None = None,
) -> Any:
    payload = compact_dict(targetId=id, centre=centre, diameterMm=diameterMm, axis=axis, sides=sides, mirror=mirror)
    return await get_conn(Ph).send("LATTICE_CIRCLE", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_revolve", "Sweep a profile about an axis, the way a lathe does"))
async def physics_lattice_revolve(
    id: str,
    profile: list[Any],
    axis: str = "z",
    degrees: float = 360.0,
    closed: bool = False,
    throughMm: list[float] | None = None,
    segments: int = 0,
) -> Any:
    payload = compact_dict(
        targetId=id, profile=profile, axis=axis, degrees=degrees,
        closed=closed, throughMm=throughMm, segments=segments,
    )
    return await get_conn(Ph).send("LATTICE_REVOLVE", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_bridge", "Join two faces with a band of quads"))
async def physics_lattice_bridge(id: str, faceA: list[Any], faceB: list[Any]) -> Any:
    return await get_conn(Ph).send("LATTICE_BRIDGE", {"targetId": id, "faceA": faceA, "faceB": faceB}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_delete_faces", "Remove faces from a lattice body"))
async def physics_lattice_delete_faces(id: str, faces: list[Any], mirror: str | None = None) -> Any:
    payload = compact_dict(targetId=id, faces=faces, mirror=mirror)
    return await get_conn(Ph).send("LATTICE_DELETE_FACES", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_sharpen", "Keep edges sharp under smoothing"))
async def physics_lattice_sharpen(
    id: str,
    edges: list[Any],
    sharp: bool = True,
    mirror: str | None = None,
    loop: bool = False,
) -> Any:
    payload = compact_dict(targetId=id, edges=edges, sharp=sharp, mirror=mirror, loop=loop)
    return await get_conn(Ph).send("LATTICE_SHARPEN", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_smooth", "Set a lattice body's smoothing level"))
async def physics_lattice_smooth(id: str, level: int) -> Any:
    return await get_conn(Ph).send("LATTICE_SMOOTH", {"targetId": id, "level": level}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_orient", "Turn every face outwards"))
async def physics_lattice_orient(id: str) -> Any:
    return await get_conn(Ph).send("LATTICE_ORIENT", {"targetId": id}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_cut", "Cut a hole, slot or dish out of a body"))
async def physics_cut(
    id: str,
    shape: str = "hole",
    at: list[float] | None = None,
    normal: list[float] | None = None,
    diameterMm: float | None = None,
    widthMm: float | None = None,
    lengthMm: float | None = None,
    depthMm: float | None = None,
    threadPitchMm: float | None = None,
) -> Any:
    payload = compact_dict(
        targetId=id, shape=shape, at=at, normal=normal,
        diameterMm=diameterMm, widthMm=widthMm, lengthMm=lengthMm, depthMm=depthMm,
        threadPitchMm=threadPitchMm,
    )
    return await get_conn(Ph).send("BODY_CUT", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_get_edges", "List a body's edges that can be rounded or bevelled"))
async def physics_get_edges(id: str) -> Any:
    return await get_conn(Ph).send("GET_EDGES", {"targetId": id}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_round_edges", "Round (fillet) or bevel (chamfer) edges of a body"))
async def physics_round_edges(
    id: str,
    edges: Any = "all",
    sizeMm: float | None = None,
    mode: str = "round",
    remove: bool | None = None,
) -> Any:
    payload = compact_dict(targetId=id, edges=edges, sizeMm=sizeMm, mode=mode, remove=remove)
    return await get_conn(Ph).send("ROUND_EDGES", payload, timeout=120.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_dimension", "Set the size or place of part of a lattice shape"))
async def physics_lattice_dimension(
    id: str,
    corners: list[Any],
    axis: str,
    valueMm: float,
    mode: str = "size",
) -> Any:
    payload = compact_dict(targetId=id, corners=corners, axis=axis, mode=mode, valueMm=valueMm)
    return await get_conn(Ph).send("LATTICE_DIMENSION", payload, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_combine", "Merge other bodies into this one with a boolean"))
async def physics_combine(id: str, withIds: list[str], op: str = "union") -> Any:
    payload = compact_dict(targetId=id, withIds=withIds, op=op)
    return await get_conn(Ph).send("COMBINE_BODIES", payload, timeout=120.0)

@mcp.tool(description=get_doc(physics_docs, "physics_lattice_wall", "Thicken a lattice surface into a shell"))
async def physics_lattice_wall(id: str, thicknessMm: float) -> Any:
    return await get_conn(Ph).send("LATTICE_WALL", {"targetId": id, "thicknessMm": thicknessMm}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_undo_lattice", "Undo the last lattice operation"))
async def physics_undo_lattice(id: str) -> Any:
    return await get_conn(Ph).send("UNDO_LATTICE", {"targetId": id}, timeout=60.0)

# ── Mesh: the machine ─────────────────────────────────────────────────────────
#
# Same command set as Volt's, same arming gate, same rule: the app refuses
# anything that moves an axis until the person at the machine has allowed it,
# and `physics_machine_arm` exists to say so rather than to do it.
#
# What differs is what is being cut. A solid is machined from several sides with
# the part re-fixtured between them, so `physics_carve_scene` runs one side; and
# `physics_machine_resume_from_line` has no equivalent in the other apps because
# only here is a single job three hours long.

@mcp.tool(description=get_doc(physics_docs, "physics_machine_status", "Report the machine's state, position and whether it is armed"))
async def physics_machine_status() -> Any:
    return await get_conn(Ph).send("MACHINE_STATUS")

@mcp.tool(description=get_doc(physics_docs, "physics_machine_settings", "The controller's $$ settings, as read on connect"))
async def physics_machine_settings() -> Any:
    return await get_conn(Ph).send("MACHINE_SETTINGS")

@mcp.tool(description=get_doc(physics_docs, "physics_machine_list_devices", "List the Tekno Boxes paired to this account"))
async def physics_machine_list_devices() -> Any:
    return await get_conn(Ph).send("MACHINE_LIST_DEVICES", timeout=20.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_arm", "Explains that only the person at the machine can allow Claude to move it"))
async def physics_machine_arm() -> Any:
    return await get_conn(Ph).send("MACHINE_ARM")

@mcp.tool(description=get_doc(physics_docs, "physics_machine_disarm", "Hand back permission to move the machine, stopping anything running"))
async def physics_machine_disarm() -> Any:
    return await get_conn(Ph).send("MACHINE_DISARM")

@mcp.tool(description=get_doc(physics_docs, "physics_machine_connect", "Open the link to the machine over USB or WiFi"))
async def physics_machine_connect(transport: str | None = None, deviceId: str | None = None) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_CONNECT", compact_dict(transport=transport, deviceId=deviceId), timeout=60.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_disconnect", "Close the machine link"))
async def physics_machine_disconnect() -> Any:
    return await get_conn(Ph).send("MACHINE_DISCONNECT", timeout=20.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_jog", "Move the tool by a relative distance in mm"))
async def physics_machine_jog(
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    feedRate: float | None = None,
) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_JOG", compact_dict(x=x, y=y, z=z, feedRate=feedRate), timeout=60.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_home", "Run the homing cycle against the limit switches"))
async def physics_machine_home() -> Any:
    return await get_conn(Ph).send("MACHINE_HOME", timeout=180.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_unlock", "Clear GRBL's alarm lockout"))
async def physics_machine_unlock() -> Any:
    return await get_conn(Ph).send("MACHINE_UNLOCK", timeout=20.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_goto_origin", "Lift, then travel to the work origin"))
async def physics_machine_goto_origin(safeZMm: float | None = None) -> Any:
    return await get_conn(Ph).send("MACHINE_GOTO_ORIGIN", compact_dict(safeZMm=safeZMm), timeout=120.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_zero_xy", "Set the current XY position as the work origin"))
async def physics_machine_zero_xy() -> Any:
    return await get_conn(Ph).send("MACHINE_ZERO_XY", timeout=30.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_zero_z", "Set work Z0, by touch plate or where the tool stands"))
async def physics_machine_zero_z(
    touchPlateMm: float | None = None,
    here: bool | None = None,
    offsetMm: float | None = None,
    searchDepthMm: float | None = None,
    feedRate: float | None = None,
) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_ZERO_Z",
        compact_dict(
            touchPlateMm=touchPlateMm, here=here, offsetMm=offsetMm,
            searchDepthMm=searchDepthMm, feedRate=feedRate,
        ),
        timeout=300.0,
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_zero_all", "Set X, Y and Z at once where the tool stands"))
async def physics_machine_zero_all() -> Any:
    return await get_conn(Ph).send("MACHINE_ZERO_ALL", timeout=30.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_probe_surface", "Probe a grid across the stock"))
async def physics_machine_probe_surface(
    cols: int | None = None,
    rows: int | None = None,
    bounds: dict | None = None,
) -> Any:
    # One slow descent per point; a 5x5 grid takes minutes.
    return await get_conn(Ph).send(
        "MACHINE_PROBE_SURFACE", compact_dict(cols=cols, rows=rows, bounds=bounds), timeout=1800.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_frame_job", "Trace the stock outline with nothing cutting"))
async def physics_machine_frame_job(
    safeZMm: float | None = None,
    guidePower: float | None = None,
) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_FRAME_JOB", compact_dict(safeZMm=safeZMm, guidePower=guidePower), timeout=600.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_carve_scene", "Machine one side of the scene that is loaded"))
async def physics_carve_scene(
    side: int | None = None,
    toolDiaMm: float | None = None,
    sides: int | None = None,
    stockThicknessMm: float | None = None,
    material: str | None = None,
    acceptUnreachable: bool | None = None,
) -> Any:
    # Returns once the job is under way, not once it has finished — a carve runs
    # for hours. Poll physics_machine_status.
    return await get_conn(Ph).send(
        "CARVE_SCENE",
        compact_dict(
            side=side, toolDiaMm=toolDiaMm, sides=sides,
            stockThicknessMm=stockThicknessMm, material=material,
            acceptUnreachable=acceptUnreachable,
        ),
        timeout=300.0,
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_pause", "Feed hold: stop without losing position"))
async def physics_machine_pause() -> Any:
    return await get_conn(Ph).send("MACHINE_PAUSE", timeout=30.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_resume", "Pick a paused job back up"))
async def physics_machine_resume() -> Any:
    return await get_conn(Ph).send("MACHINE_RESUME", timeout=120.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_preview_resume", "What a resume from a line would do, without doing it"))
async def physics_machine_preview_resume(fromLine: int, options: dict | None = None) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_PREVIEW_RESUME", compact_dict(fromLine=fromLine, options=options), timeout=30.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_resume_from_line", "Restart a job that ended badly, part way through"))
async def physics_machine_resume_from_line(fromLine: int, options: dict | None = None) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_RESUME_FROM_LINE", compact_dict(fromLine=fromLine, options=options), timeout=120.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_machine_cancel", "Stop the job and drop the rest of the program"))
async def physics_machine_cancel() -> Any:
    return await get_conn(Ph).send("MACHINE_CANCEL", timeout=30.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_estop", "Emergency stop: soft-reset the controller and stop the spindle"))
async def physics_machine_estop() -> Any:
    # Kept short deliberately: if this one is slow to answer, the answer is not
    # worth waiting for.
    return await get_conn(Ph).send("MACHINE_ESTOP", timeout=15.0)

@mcp.tool(description=get_doc(physics_docs, "physics_machine_trim", "Trim feed, spindle or rapid on the running job"))
async def physics_machine_trim(
    feed: Any = None,
    spindle: Any = None,
    rapid: int | None = None,
) -> Any:
    return await get_conn(Ph).send(
        "MACHINE_TRIM", compact_dict(feed=feed, spindle=spindle, rapid=rapid), timeout=30.0
    )

@mcp.tool(description=get_doc(physics_docs, "physics_get_note_cards", "Return note cards"))
async def physics_get_note_cards() -> Any:
    return await get_conn(Ph).send("GET_NOTE_CARDS")

@mcp.tool(description=get_doc(physics_docs, "physics_set_note_cards", "Replace note cards"))
async def physics_set_note_cards(noteCards: list[Any]) -> Any:
    return await get_conn(Ph).send("SET_NOTE_CARDS", {"noteCards": noteCards})

# ── PhysBox: Etch Tools ───────────────────────────────────────────────────────

@mcp.tool(description=get_doc(etch_docs, "etch_get_state", "Return full PhysBox: Etch state"))
async def etch_get_state() -> Any:
    return await get_conn(Et).send("GET_STATE")

@mcp.tool(description=get_doc(etch_docs, "etch_set_document", "Replace document in PhysBox: Etch"))
async def etch_set_document(document: dict) -> Any:
    return await get_conn(Et).send("SET_DOCUMENT", {"document": document})

@mcp.tool(description=get_doc(etch_docs, "etch_set_svg", "Set/import raw SVG XML onto canvas"))
async def etch_set_svg(svg: str) -> Any:
    return await get_conn(Et).send("SET_SVG", {"svg": svg})

@mcp.tool(description=get_doc(etch_docs, "etch_export_svg", "Export active design as SVG XML string"))
async def etch_export_svg() -> Any:
    return await get_conn(Et).send("EXPORT_SVG")

@mcp.tool(description=get_doc(etch_docs, "etch_list_presets", "List built-in vector manufacturing presets"))
async def etch_list_presets() -> Any:
    return await get_conn(Et).send("LIST_PRESETS")

@mcp.tool(description=get_doc(etch_docs, "etch_load_preset", "Load vector preset by key"))
async def etch_load_preset(preset: str) -> Any:
    return await get_conn(Et).send("LOAD_PRESET", {"presetId": preset})

@mcp.tool(description=get_doc(etch_docs, "etch_add_element", "Add a new vector shape or element"))
async def etch_add_element(element: dict) -> Any:
    return await get_conn(Et).send("ADD_ELEMENT", {"element": element})

@mcp.tool(description=get_doc(etch_docs, "etch_list_clipart", "List the built-in vector clip-art symbols"))
async def etch_list_clipart() -> Any:
    return await get_conn(Et).send("LIST_CLIPART")

@mcp.tool(description=get_doc(etch_docs, "etch_add_clipart", "Place a clip-art symbol on the canvas by id"))
async def etch_add_clipart(
    symbolId: str,
    x: float | None = None,
    y: float | None = None,
    size: float | None = None,
    rotation: float | None = None,
    layerId: str | None = None,
) -> Any:
    return await get_conn(Et).send("ADD_CLIPART", compact_dict(
        symbolId=symbolId, x=x, y=y, size=size, rotation=rotation, layerId=layerId
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_add_image", "Import a raster image as vector, halftone, scanline or shade"))
async def etch_add_image(
    image: str,
    options: dict | None = None,
    layerId: str | None = None,
) -> Any:
    # Longer than the default 10s: the bytes have to cross the bridge and the
    # tracer runs on the browser's main thread, and a timeout here would leave
    # the import half-done rather than not done.
    return await get_conn(Et).send("ADD_IMAGE", compact_dict(
        image=image, options=options, layerId=layerId
    ), timeout=60.0)

@mcp.tool(description=get_doc(etch_docs, "etch_combine", "Union, subtract, intersect or exclude two or more shapes into one path; or join/unjoin separate pieces into one part"))
async def etch_combine(elementIds: list[str], op: str) -> Any:
    # Order is the operation, not a detail: elementIds[0] is the base, and for
    # 'subtract' it is the shape being cut into. Passed through as sent.
    # 'join' and 'unjoin' ride on the same tool (one id is enough for them) so
    # the tool count the website advertises does not move for them.
    return await get_conn(Et).send("COMBINE", {"elementIds": elementIds, "op": op})

@mcp.tool(description=get_doc(etch_docs, "etch_fill_region", "Fill the region of the drawing enclosing a point, as a hatched shape"))
async def etch_fill_region(x: float, y: float, layerId: str | None = None) -> Any:
    # The paint bucket by coordinate: the browser rasterises the sheet and
    # traces the region, which on a large drawing is a second or two of work.
    payload: dict[str, Any] = {"x": x, "y": y}
    if layerId:
        payload["layerId"] = layerId
    return await get_conn(Et).send("FILL_REGION", payload, timeout=60.0)

@mcp.tool(description=get_doc(etch_docs, "etch_erase", "Mask part of one layer out of the job without changing the drawing"))
async def etch_erase(
    points: list[dict],
    width: float | None = None,
    layerId: str | None = None,
    name: str | None = None,
) -> Any:
    # The eraser by coordinate: a centreline in mm and a width, landing as the
    # same `erase` element the tool draws. Nothing underneath is edited, which
    # is why this exists rather than "just rewrite the path" — a traced photo
    # rewritten to drop a corner has lost the corner for good.
    return await get_conn(Et).send("ERASE", compact_dict(
        points=points, width=width, layerId=layerId, name=name
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_list_sheets", "List the sheets open in the job, and say which one is being edited"))
async def etch_list_sheets() -> Any:
    return await get_conn(Et).send("LIST_SHEETS")

@mcp.tool(description=get_doc(etch_docs, "etch_select_sheet", "Switch to another sheet of the job — every other etch tool acts on the selected one"))
async def etch_select_sheet(sheetId: str | None = None, index: int | None = None) -> Any:
    # By id, or by position for the common "go to sheet 3". Everything else in
    # this app edits whatever sheet is open, so this is the call that decides
    # where the next twenty are aimed.
    return await get_conn(Et).send("SELECT_SHEET", compact_dict(sheetId=sheetId, index=index))

@mcp.tool(description=get_doc(etch_docs, "etch_new_sheet", "Add a sheet to the job — blank, or a copy of the one open"))
async def etch_new_sheet(name: str | None = None, duplicate: bool | None = None) -> Any:
    # duplicate=True is the one to reach for on a layered piece: sheet two is
    # sheet one with the middle changed, and rebuilding its frame and pin holes
    # by hand is both work and a chance to be a millimetre out.
    return await get_conn(Et).send("NEW_SHEET", compact_dict(name=name, duplicate=duplicate))

@mcp.tool(description=get_doc(etch_docs, "etch_close_sheet", "Close a sheet of the job (never the last one)"))
async def etch_close_sheet(sheetId: str | None = None) -> Any:
    return await get_conn(Et).send("CLOSE_SHEET", compact_dict(sheetId=sheetId))

@mcp.tool(description=get_doc(etch_docs, "etch_add_registration", "Add pin holes for stacking sheets, placed from the stock so every sheet matches"))
async def etch_add_registration(
    count: int | None = None,
    diameterMm: float | None = None,
    insetMm: float | None = None,
) -> Any:
    # Positions come from the stock by a rule, not from the drawing: run it on
    # every sheet of a layered piece and the holes land on the same millimetre,
    # which is what hand-placed circles get wrong.
    return await get_conn(Et).send("ADD_REGISTRATION", compact_dict(
        count=count, diameterMm=diameterMm, insetMm=insetMm
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_make_ornament", "Draw a decorative pattern — guilloche, maze, animal print or vine scrollwork"))
async def etch_make_ornament(
    kind: str,
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    options: dict | None = None,
) -> Any:
    # One tool for the four, because they take the same three things: which
    # one, where, and its own settings. The settings are described by the
    # generator itself and validated against that list, so a typo comes back
    # naming the options it does have rather than being quietly ignored.
    return await get_conn(Et).send("MAKE_ORNAMENT", compact_dict(
        kind=kind, x=x, y=y, width=width, height=height, options=options
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_make_living_hinge", "Cut a living hinge — rows of slits that let a flat sheet bend"))
async def etch_make_living_hinge(
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    axis: str | None = None,
    slitLengthMm: float | None = None,
    bridgeMm: float | None = None,
    pitchMm: float | None = None,
) -> Any:
    # The layout rules are the reason this is a tool rather than a few hundred
    # `etch_add_element` calls: alternate rows must be offset half a period or
    # the panel does not bend at all, and a slit that reaches the edge of the
    # region is a split the panel tears along on the first fold.
    return await get_conn(Et).send("MAKE_LIVING_HINGE", compact_dict(
        x=x, y=y, width=width, height=height, axis=axis,
        slitLengthMm=slitLengthMm, bridgeMm=bridgeMm, pitchMm=pitchMm
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_make_perforation", "Fill a region with holes — a grille, a vent, a diffuser"))
async def etch_make_perforation(
    x: float | None = None,
    y: float | None = None,
    width: float | None = None,
    height: float | None = None,
    lattice: str | None = None,
    shape: str | None = None,
    sizeMm: float | None = None,
    slotLengthMm: float | None = None,
    pitchMm: float | None = None,
    ramp: str | None = None,
) -> Any:
    # It reports the web — the material between two neighbouring holes — because
    # that is the number that decides whether the panel survives being cut, and
    # it is not visible in any of the settings that produced it.
    return await get_conn(Et).send("MAKE_PERFORATION", compact_dict(
        x=x, y=y, width=width, height=height, lattice=lattice, shape=shape,
        sizeMm=sizeMm, slotLengthMm=slotLengthMm, pitchMm=pitchMm, ramp=ramp
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_update_layer", "Change one layer's settings in place — operation, holding, depth, overrides"))
async def etch_update_layer(layerId: str, updates: dict) -> Any:
    # Layer settings were only reachable by sending the whole `layers` array
    # through etch_set_document, which discards whatever changed on the canvas
    # in between. Holding lives here too: `tabs` on a router, `bridges` on a
    # laser.
    return await get_conn(Et).send("UPDATE_LAYER", {"layerId": layerId, "updates": updates})

@mcp.tool(description=get_doc(etch_docs, "etch_make_test_grid", "Generate a material test grid, replacing the open document"))
async def etch_make_test_grid(options: dict | None = None) -> Any:
    # Longer than the default: the grid's labels are vectorized before the reply
    # comes back, and a font that has to be fetched makes that a slow call.
    return await get_conn(Et).send("MAKE_TEST_GRID", {"options": options or {}}, timeout=60.0)

# ── Etch: the machine ─────────────────────────────────────────────────────────
#
# Same command set as Volt's and Mesh's, same arming gate. Trim used to be the
# only machine command here, and the comment above it said why: a machine begins
# moving when the person beside it says so. That rule is kept — by the gate in
# the app, which is what makes the rest of these safe to serve.
#
# The one command with no counterpart in the other two is the guide spot. Most
# machines this app drives are lasers, and putting a visible dot on the material
# is how a job gets set up: "is that the corner?" is a question an operator can
# answer, and reading them coordinates is not.

@mcp.tool(description=get_doc(etch_docs, "etch_machine_status", "Report the machine's state, position and whether it is armed"))
async def etch_machine_status() -> Any:
    return await get_conn(Et).send("MACHINE_STATUS")

@mcp.tool(description=get_doc(etch_docs, "etch_machine_settings", "The controller's $$ settings, as read on connect"))
async def etch_machine_settings() -> Any:
    return await get_conn(Et).send("MACHINE_SETTINGS")

@mcp.tool(description=get_doc(etch_docs, "etch_machine_list_devices", "List the Tekno Boxes paired to this account"))
async def etch_machine_list_devices() -> Any:
    return await get_conn(Et).send("MACHINE_LIST_DEVICES", timeout=20.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_arm", "Explains that only the person at the machine can allow Claude to move it"))
async def etch_machine_arm() -> Any:
    return await get_conn(Et).send("MACHINE_ARM")

@mcp.tool(description=get_doc(etch_docs, "etch_machine_disarm", "Hand back permission to move the machine, stopping anything running"))
async def etch_machine_disarm() -> Any:
    return await get_conn(Et).send("MACHINE_DISARM")

@mcp.tool(description=get_doc(etch_docs, "etch_machine_connect", "Open the link to the machine over USB or WiFi"))
async def etch_machine_connect(transport: str | None = None, deviceId: str | None = None) -> Any:
    return await get_conn(Et).send(
        "MACHINE_CONNECT", compact_dict(transport=transport, deviceId=deviceId), timeout=60.0
    )

@mcp.tool(description=get_doc(etch_docs, "etch_machine_disconnect", "Close the machine link"))
async def etch_machine_disconnect() -> Any:
    return await get_conn(Et).send("MACHINE_DISCONNECT", timeout=20.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_jog", "Move the head by a relative distance in mm"))
async def etch_machine_jog(
    x: float | None = None,
    y: float | None = None,
    z: float | None = None,
    feedRate: float | None = None,
) -> Any:
    return await get_conn(Et).send(
        "MACHINE_JOG", compact_dict(x=x, y=y, z=z, feedRate=feedRate), timeout=60.0
    )

@mcp.tool(description=get_doc(etch_docs, "etch_machine_home", "Run the homing cycle against the limit switches"))
async def etch_machine_home() -> Any:
    return await get_conn(Et).send("MACHINE_HOME", timeout=180.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_unlock", "Clear GRBL's alarm lockout"))
async def etch_machine_unlock() -> Any:
    return await get_conn(Et).send("MACHINE_UNLOCK", timeout=20.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_goto_origin", "Lift, then travel to the work origin"))
async def etch_machine_goto_origin(safeZMm: float | None = None) -> Any:
    return await get_conn(Et).send("MACHINE_GOTO_ORIGIN", compact_dict(safeZMm=safeZMm), timeout=120.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_zero_xy", "Set the current XY position as the work origin"))
async def etch_machine_zero_xy() -> Any:
    return await get_conn(Et).send("MACHINE_ZERO_XY", timeout=30.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_zero_z", "Set work Z0 where the tool stands, allowing for a shim"))
async def etch_machine_zero_z(shimThicknessMm: float | None = None) -> Any:
    return await get_conn(Et).send(
        "MACHINE_ZERO_Z", compact_dict(shimThicknessMm=shimThicknessMm), timeout=60.0
    )

@mcp.tool(description=get_doc(etch_docs, "etch_machine_guide_spot", "Light the laser at pointer power so the head can be seen"))
async def etch_machine_guide_spot(on: bool | None = None, power: float | None = None) -> Any:
    return await get_conn(Et).send(
        "MACHINE_GUIDE_SPOT", compact_dict(on=on, power=power), timeout=30.0
    )

@mcp.tool(description=get_doc(etch_docs, "etch_machine_probe_surface", "Probe a grid across the bed, for a CNC job"))
async def etch_machine_probe_surface(
    cols: int | None = None,
    rows: int | None = None,
    bounds: dict | None = None,
) -> Any:
    return await get_conn(Et).send(
        "MACHINE_PROBE_SURFACE", compact_dict(cols=cols, rows=rows, bounds=bounds), timeout=1800.0
    )

@mcp.tool(description=get_doc(etch_docs, "etch_machine_frame_job", "Trace the stock outline with the guide beam lit"))
async def etch_machine_frame_job(
    safeZMm: float | None = None,
    guidePower: float | None = None,
) -> Any:
    return await get_conn(Et).send(
        "MACHINE_FRAME_JOB", compact_dict(safeZMm=safeZMm, guidePower=guidePower), timeout=600.0
    )

@mcp.tool(description=get_doc(etch_docs, "etch_run_job", "Cut the document that is open"))
async def etch_run_job(options: dict | None = None) -> Any:
    # Returns once the job is streaming, not once it has finished.
    return await get_conn(Et).send("RUN_JOB", compact_dict(options=options), timeout=300.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_pause", "Feed hold: stop without losing position"))
async def etch_machine_pause() -> Any:
    return await get_conn(Et).send("MACHINE_PAUSE", timeout=30.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_resume", "Pick a paused job back up"))
async def etch_machine_resume() -> Any:
    return await get_conn(Et).send("MACHINE_RESUME", timeout=60.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_cancel", "Stop the job and drop the rest of the program"))
async def etch_machine_cancel() -> Any:
    return await get_conn(Et).send("MACHINE_CANCEL", timeout=30.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_estop", "Emergency stop: soft-reset the controller and kill the beam"))
async def etch_machine_estop() -> Any:
    # Kept short deliberately: if this one is slow to answer, the answer is not
    # worth waiting for.
    return await get_conn(Et).send("MACHINE_ESTOP", timeout=15.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_trim", "Trim feed, power or rapid speed on the running machine"))
async def etch_machine_trim(
    feed: Any | None = None,
    power: Any | None = None,
    rapid: int | None = None,
) -> Any:
    # Steps, not targets: GRBL has no "set the feed to 87%" command, and the
    # browser end rejects anything else rather than accepting it and doing
    # nothing.
    #
    # Ungated, like the other two apps' trim: it changes how hard a job someone
    # already chose to run is cutting, and cannot start one.
    return await get_conn(Et).send("MACHINE_TRIM", compact_dict(
        feed=feed, power=power, rapid=rapid
    ))

@mcp.tool(description=get_doc(etch_docs, "etch_list_capabilities", "List tools, materials, image modes and layer operations available"))
async def etch_list_capabilities() -> Any:
    return await get_conn(Et).send("LIST_CAPABILITIES")

@mcp.tool(description=get_doc(etch_docs, "etch_generate_gcode", "Generate GRBL/Marlin G-code toolpath"))
async def etch_generate_gcode(options: dict | None = None) -> Any:
    return await get_conn(Et).send("GENERATE_GCODE", {"options": options or {}})

@mcp.tool(description=get_doc(etch_docs, "etch_get_summary", "Return a lightweight document summary"))
async def etch_get_summary() -> Any:
    return await get_conn(Et).send("GET_SUMMARY")

@mcp.tool(description=get_doc(etch_docs, "etch_validate_document", "Check the document for faults before machining"))
async def etch_validate_document() -> Any:
    return await get_conn(Et).send("VALIDATE_DOCUMENT")

@mcp.tool(description=get_doc(etch_docs, "etch_update_element", "Update one element in place"))
async def etch_update_element(id: str, updates: dict) -> Any:
    return await get_conn(Et).send("UPDATE_ELEMENT", {"elementId": id, "updates": updates})  # not "id": see circuit_update_component

@mcp.tool(description=get_doc(etch_docs, "etch_save_preset", "Save the document as a user preset"))
async def etch_save_preset(name: str) -> Any:
    return await get_conn(Et).send("SAVE_PRESET", {"name": name})

@mcp.tool(description=get_doc(etch_docs, "etch_delete_preset", "Delete a saved user preset"))
async def etch_delete_preset(name: str) -> Any:
    return await get_conn(Et).send("DELETE_PRESET", {"name": name})

@mcp.tool(description=get_doc(etch_docs, "etch_get_screenshot", "Capture the canvas as a PNG"))
async def etch_get_screenshot(scale: float = 2.0) -> Any:
    return await get_conn(Et).send("SCREENSHOT", {"scale": scale}, timeout=30.0)

@mcp.tool(description=get_doc(etch_docs, "etch_get_note_card", "Return the document's note card markdown"))
async def etch_get_note_card() -> Any:
    return await get_conn(Et).send("GET_NOTE_CARD")

@mcp.tool(description=get_doc(etch_docs, "etch_set_note_card", "Set the document's note card markdown"))
async def etch_set_note_card(markdown: str) -> Any:
    return await get_conn(Et).send("SET_NOTE_CARD", {"markdown": markdown})

@mcp.tool(description=get_doc(etch_docs, "etch_get_schema", "Return PhysBox: Etch schema"))
async def etch_get_schema() -> Any:
    return get_reference_docs(etch_docs)

# ── Entry point ───────────────────────────────────────────────────────────────

# ── PhysBox Cloud Tools (account, no browser required) ────────────────────────
#
# Everything above this line drives a tab over the local WebSocket hub and needs
# nothing but the app being open. These read the user's PhysBox account over HTTPS
# instead, which is what lets an agent answer a question about a job that finished
# last month with nothing running.
#
# They are the Pro half. The API decides that, not this file: a free account gets a
# 403 and cloud.py turns it into a sentence saying the archive was never recording,
# rather than an empty list that reads as "you never cut that".

@mcp.tool(description=get_doc(cloud_docs, "physbox_whoami", "Report the PhysBox account and tier these cloud tools are authenticated as"))
async def physbox_whoami() -> Any:
    return await cloud.get("/api/tokens/whoami")

@mcp.tool(description=get_doc(cloud_docs, "physbox_list_runs", "List archived machine runs across every app and machine"))
async def physbox_list_runs(
    app: str | None = None,
    device: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> Any:
    return await cloud.get("/api/runs", {
        "app_id": app,
        "device_id": device,
        "since": since,
        "until": until,
        "status": status,
        "limit": limit,
    })

@mcp.tool(description=get_doc(cloud_docs, "physbox_find_runs", "Search archived runs by job name, document or recorded settings"))
async def physbox_find_runs(
    query: str,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
) -> Any:
    # The same route as the list, with `q` set. Kept as its own tool because the
    # question it answers is a different question, and an agent picking tools from
    # descriptions should not have to notice that a filter exists.
    return await cloud.get("/api/runs", {
        "q": query,
        "since": since,
        "until": until,
        "limit": limit,
    })

@mcp.tool(description=get_doc(cloud_docs, "physbox_get_run", "Fetch one run in full, including its sample trace"))
async def physbox_get_run(run_id: str) -> Any:
    return await cloud.get(f"/api/runs/{run_id}")

@mcp.tool(description=get_doc(cloud_docs, "physbox_runs_summary", "Aggregate the archive: run counts, cutting time, failures, materials"))
async def physbox_runs_summary(
    app: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> Any:
    return await cloud.get("/api/runs/summary", {"app_id": app, "since": since, "until": until})

@mcp.tool(description=get_doc(cloud_docs, "physbox_get_run_gcode", "Fetch the program a run cut, when it was stored server-side"))
async def physbox_get_run_gcode(run_id: str) -> Any:
    return await cloud.get(f"/api/runs/{run_id}/gcode")

@mcp.tool(description=get_doc(cloud_docs, "physbox_list_documents", "List the account's cloud-saved documents"))
async def physbox_list_documents(app: str | None = None) -> Any:
    return await cloud.get("/api/documents", {"app_id": app})

@mcp.tool(description=get_doc(cloud_docs, "physbox_get_document", "Fetch one cloud document, current or at an earlier revision"))
async def physbox_get_document(document_id: str, revision: int | None = None) -> Any:
    if revision is not None:
        return await cloud.get(f"/api/documents/{document_id}/revisions/{revision}")
    return await cloud.get(f"/api/documents/{document_id}")

@mcp.tool(description=get_doc(cloud_docs, "physbox_document_revisions", "List the saved revisions of one cloud document"))
async def physbox_document_revisions(document_id: str) -> Any:
    return await cloud.get(f"/api/documents/{document_id}/revisions")

@mcp.tool(description=get_doc(cloud_docs, "physbox_set_token", "Save a PhysBox read token to this machine's user config"))
async def physbox_set_token(token: str) -> Any:
    # Writes the user's own credential where the next session will find it, so the
    # setup step happens once instead of every time. The token is never echoed
    # back — only where it went.
    if not token.startswith("pbx_"):
        raise ValueError("A PhysBox API token starts with 'pbx_'. Mint one at https://physbox.io/history.html.")
    path = cloud.write_credentials_file(token.strip())
    return {"saved": True, "path": str(path)}


def main():
    use_stdio = "--stdio" in sys.argv
    port_arg  = next((a for a in sys.argv if a.startswith("--port=")), None)
    http_port = int(port_arg.split("=")[1]) if port_arg else MCP_PORT

    if use_stdio:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host="localhost", port=http_port)

if __name__ == "__main__":
    main()
