"""
PhysBox: MCP - Model Context Protocol Server for Flux, Volt, Mesh, and Etch.
Connects LLMs to browser-based simulations via WebSocket relay.
"""

import os
import sys
import json
import random
import string
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

def compact_dict(**kwargs) -> dict:
    """Return a dictionary containing only non-None values."""
    return {k: v for k, v in kwargs.items() if v is not None}

def load_mcp_docs(app_id: str) -> dict:
    current_dir = Path(__file__).parent.resolve()
    possible_paths = [
        current_dir / ".." / ".." / app_id / "mcp-docs.json",
        current_dir / "mcp-docs" / f"{app_id}.json",
        Path.home() / app_id / "mcp-docs.json"
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

    def start(self):
        pass

    async def send(self, cmd: str, payload: dict | None = None, timeout: float = 10.0) -> Any:
        global is_primary, peer_ws, peer_ws_loop
        
        if is_primary:
            if not self.connected or self.ws is None or self.ws_loop is None:
                raise RuntimeError(
                    f"App on port {self.port} is not connected. Open the app in your browser!"
                )
            msg_id = "".join(random.choices(string.ascii_letters, k=8))
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            self.pending[msg_id] = fut
            # The envelope wins over the payload. Spread the other way round, a
            # tool argument called "id" (update_component's component id) landed
            # on top of the request id: the app answered under the component's
            # name, nothing matched the pending future, and every call to those
            # tools timed out after ten seconds having already applied its edit.
            data = {**(payload or {}), "cmd": cmd, "id": msg_id}
            
            asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(data)), self.ws_loop)
            
            try:
                return await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                self.pending.pop(msg_id, None)
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
                    conn.ws = ws
                    conn.ws_loop = ws_loop
                    conn.connected = True
                    print(f"Registered browser connection for {app_info['name']} on port {app_info['port']}", file=sys.stderr)
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
                
                target_conn = get_conn(port) if port else None
                if not target_conn or not target_conn.connected or target_conn.ws is None:
                    await ws.send(json.dumps({
                        "event": "PEER_RESULT",
                        "id": req_id,
                        "error": f"App on port {port} is not connected. Open the app in your browser!"
                    }))
                else:
                    peer_pending_requests[req_id] = (ws, req_id)
                    cmd_data = {**(payload or {}), "cmd": cmd, "id": req_id}  # envelope wins; see AppConnection.send
                    asyncio.run_coroutine_threadsafe(target_conn.ws.send(json.dumps(cmd_data)), target_conn.ws_loop)

            elif event in ("RESULT", "ERROR"):
                msg_id = msg.get("id", "")
                if conn:
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
                
        if conn and conn.ws is ws:
            conn.connected = False
            conn.ws = None
            conn.ws_loop = None
            asyncio.create_task(broadcast_app_status(conn.port, False))
            for fut in list(conn.pending.values()):
                if not fut.done():
                    mcp_loop = fut.get_loop()
                    mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError("WebSocket disconnected"))
            conn.pending.clear()

async def run_peer_client_loop():
    global peer_ws, peer_ws_loop
    peer_ws_url = f"ws://localhost:{MCP_WS_PORT}"
    try:
        async with websockets.connect(peer_ws_url) as ws:
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
                async with websockets.serve(ws_handler, "127.0.0.1", MCP_WS_PORT):
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
    results = []
    for app_id, app in APPS.items():
        probe = probe_port(app["port"])
        conn = get_conn(app["port"])
        results.append({
            "id": app_id,
            "name": app["name"],
            "port": app["port"],
            "httpOpen": probe["open"],
            "wsConnected": conn.connected,
        })
    return results

@mcp.tool()
async def send_command(port: int, cmd: str, payload: dict | None = None) -> Any:
    """
    Send an arbitrary JSON command to any app and return the result.
    port: 5173 (process), 5174 (circuit), 5175 (physics).
    """
    return await get_conn(port).send(cmd, payload)

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
async def physics_get_history() -> Any:
    return await get_conn(Ph).send("GET_HISTORY")

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

@mcp.tool(description=get_doc(physics_docs, "physics_delete_object", "Delete a body from the scene"))
async def physics_delete_object(id: str) -> Any:
    return await get_conn(Ph).send("DELETE_OBJECT", {"targetId": id}, timeout=60.0)

@mcp.tool(description=get_doc(physics_docs, "physics_probe_sculpt", "Find the surface nearest some points"))
async def physics_probe_sculpt(id: str, at: list[Any]) -> Any:
    return await get_conn(Ph).send("PROBE_SCULPT", {"targetId": id, "at": at})

@mcp.tool(description=get_doc(physics_docs, "physics_undo_sculpt", "Undo the last sculpt stroke"))
async def physics_undo_sculpt(id: str) -> Any:
    return await get_conn(Ph).send("UNDO_SCULPT", {"targetId": id}, timeout=60.0)

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
) -> Any:
    payload = compact_dict(
        targetId=id, shape=shape, at=at, normal=normal,
        diameterMm=diameterMm, widthMm=widthMm, lengthMm=lengthMm, depthMm=depthMm,
    )
    return await get_conn(Ph).send("BODY_CUT", payload, timeout=60.0)

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

@mcp.tool(description=get_doc(etch_docs, "etch_combine", "Union, subtract, intersect or exclude two or more shapes into one path"))
async def etch_combine(elementIds: list[str], op: str) -> Any:
    # Order is the operation, not a detail: elementIds[0] is the base, and for
    # 'subtract' it is the shape being cut into. Passed through as sent.
    return await get_conn(Et).send("COMBINE", {"elementIds": elementIds, "op": op})

@mcp.tool(description=get_doc(etch_docs, "etch_fill_region", "Fill the region of the drawing enclosing a point, as a hatched shape"))
async def etch_fill_region(x: float, y: float, layerId: str | None = None) -> Any:
    # The paint bucket by coordinate: the browser rasterises the sheet and
    # traces the region, which on a large drawing is a second or two of work.
    payload: dict[str, Any] = {"x": x, "y": y}
    if layerId:
        payload["layerId"] = layerId
    return await get_conn(Et).send("FILL_REGION", payload, timeout=60.0)

@mcp.tool(description=get_doc(etch_docs, "etch_make_test_grid", "Generate a material test grid, replacing the open document"))
async def etch_make_test_grid(options: dict | None = None) -> Any:
    # Longer than the default: the grid's labels are vectorized before the reply
    # comes back, and a font that has to be fetched makes that a slow call.
    return await get_conn(Et).send("MAKE_TEST_GRID", {"options": options or {}}, timeout=60.0)

@mcp.tool(description=get_doc(etch_docs, "etch_machine_status", "Report the connected machine's state, position and live trim"))
async def etch_machine_status() -> Any:
    return await get_conn(Et).send("MACHINE_STATUS")

@mcp.tool(description=get_doc(etch_docs, "etch_machine_trim", "Trim feed, power or rapid speed on the running machine"))
async def etch_machine_trim(
    feed: Any | None = None,
    power: Any | None = None,
    rapid: int | None = None,
) -> Any:
    # Steps, not targets: GRBL has no "set the feed to 87%" command, and the
    # browser end rejects anything else rather than accepting it and doing
    # nothing. There is no start, resume or jog here on purpose — a machine
    # begins moving when the person beside it says so, not when an agent does.
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
