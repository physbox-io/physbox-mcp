"""
PhysBox: MCP - Model Context Protocol Server for Flux, Volt, and Mesh.
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

# ── Configuration & Constants ──────────────────────────────────────────────────

MCP_PORT = int(os.environ.get("MCP_PORT", "3141"))
MCP_WS_PORT = int(os.environ.get("MCP_WS_PORT", "3142"))

APPS = {
    "process": {"port": 5173, "name": "Flux"},
    "circuit": {"port": 5174, "name": "Volt"},
    "physics": {"port": 5175, "name": "Mesh"},
}

P  = APPS["process"]["port"]
C  = APPS["circuit"]["port"]
Ph = APPS["physics"]["port"]

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
            data = {"cmd": cmd, "id": msg_id, **(payload or {})}
            
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
                    cmd_data = {"cmd": cmd, "id": req_id, **(payload or {})}
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
                async with websockets.serve(ws_handler, "0.0.0.0", MCP_WS_PORT):
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
        "PhysBox: MCP - Model Context Protocol server for Volt (5174), Mesh (5175), and Flux (Beta) (5173). "
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

# ── PhysBox: Mesh (Physics) Tools ─────────────────────────────────────────────

@mcp.tool(description=get_doc(physics_docs, "physics_validate_scad", "Validate OpenSCAD code and return compilation result or errors"))
async def physics_validate_scad(scad: str) -> Any:
    return await get_conn(Ph).send("VALIDATE_SCAD", {"scad": scad})

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

@mcp.tool(description=get_doc(physics_docs, "physics_get_note_cards", "Return note cards"))
async def physics_get_note_cards() -> Any:
    return await get_conn(Ph).send("GET_NOTE_CARDS")

@mcp.tool(description=get_doc(physics_docs, "physics_set_note_cards", "Replace note cards"))
async def physics_set_note_cards(noteCards: list[Any]) -> Any:
    return await get_conn(Ph).send("SET_NOTE_CARDS", {"noteCards": noteCards})

# ── Entry point ───────────────────────────────────────────────────────────────

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
