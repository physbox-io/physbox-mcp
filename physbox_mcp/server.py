#!/usr/bin/env python3
"""
MCP server (Python) for local web apps:
  Process Expert  → ws://localhost:5173/mcp
  Circuit Expert  → ws://localhost:5174/mcp
  Physics Sim     → ws://localhost:5175/mcp

Usage:
  venv/bin/python -m physbox_mcp.server                  # HTTP on port 3141 (default)
  venv/bin/python -m physbox_mcp.server --port=4000      # HTTP on custom port
  MCP_PORT=4000 venv/bin/python -m physbox_mcp.server
  venv/bin/python -m physbox_mcp.server --stdio          # stdio mode for clients that spawn the process

Claude Code (auto-configured via .claude/mcp.json):
  claude                                     # Claude Code picks up the MCP automatically
  claude mcp add physbox-mcp -- venv/bin/python -m physbox_mcp.server --stdio  # or add manually
"""

import asyncio
import json
import os
import random
import string
import sys
import urllib.request
import urllib.error
from typing import Any

import base64

from fastmcp import FastMCP
from fastmcp.utilities.types import Image
import websockets
import threading

# ── App registry ──────────────────────────────────────────────────────────────

APPS = {
    "process": {"port": 5173, "name": "Flux"},
    "circuit": {"port": 5174, "name": "Volt"},
    "physics": {"port": 5175, "name": "Mesh"},
}

# ── Connection pool ───────────────────────────────────────────────────────────

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
        if not self.connected or self.ws is None or self.ws_loop is None:
            raise RuntimeError(
                f"App on port {self.port} is not connected. Open the app in your browser!"
            )
        msg_id = "".join(random.choices(string.ascii_letters, k=8))
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self.pending[msg_id] = fut
        data = {"cmd": cmd, "id": msg_id, **(payload or {})}
        
        # Safely schedule the WebSocket send in its own loop thread
        asyncio.run_coroutine_threadsafe(self.ws.send(json.dumps(data)), self.ws_loop)
        
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self.pending.pop(msg_id, None)
            raise RuntimeError(f'Timeout waiting for "{cmd}" response ({timeout}s)')


_connections: dict[int, AppConnection] = {}


def load_mcp_docs(app_id: str) -> dict:
    current_dir = os.path.dirname(os.path.abspath(__file__))
    possible_paths = [
        os.path.join(current_dir, "..", "..", app_id, "mcp-docs.json"),
        os.path.join(current_dir, "mcp-docs", f"{app_id}.json"),
        os.path.join("/home/boab", app_id, "mcp-docs.json")
    ]
    for path in possible_paths:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error reading docs for {app_id} from {path}: {e}", file=sys.stderr)
    return {}


physics_docs = load_mcp_docs("physics")
process_docs = load_mcp_docs("process")
circuit_docs = load_mcp_docs("circuit")


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


# ── WebSocket Server (Thread-Safe Background Event Loop) ──────────────────────

async def ws_handler(ws):
    conn = None
    ws_loop = asyncio.get_running_loop()
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if msg.get("event") == "HELLO":
                app_key = msg.get("app")
                app_info = APPS.get(app_key)
                if app_info:
                    conn = get_conn(app_info["port"])
                    conn.ws = ws
                    conn.ws_loop = ws_loop
                    conn.connected = True
                    print(f"Registered browser connection for {app_info['name']} on port {app_info['port']}", file=sys.stderr)
                    await ws.send(json.dumps({"event": "CONNECTED", "role": "browser"}))
                else:
                    print(f"Unknown app connected: {app_key}", file=sys.stderr)

            elif msg.get("event") in ("RESULT", "ERROR"):
                if conn:
                    fut = conn.pending.pop(msg.get("id", ""), None)
                    if fut and not fut.done():
                        # Resolve/reject thread-safely back on the FastMCP thread's loop
                        mcp_loop = fut.get_loop()
                        if msg["event"] == "ERROR":
                            err_msg = msg.get("error", "unknown")
                            mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError(err_msg))
                        else:
                            mcp_loop.call_soon_threadsafe(fut.set_result, msg.get("data"))
    except Exception as e:
        print(f"WS error: {e}", file=sys.stderr)
    finally:
        if conn:
            conn.connected = False
            conn.ws = None
            conn.ws_loop = None
            # Cancel any pending futures on disconnection
            for fut in list(conn.pending.values()):
                if not fut.done():
                    mcp_loop = fut.get_loop()
                    mcp_loop.call_soon_threadsafe(fut.set_exception, RuntimeError("WebSocket disconnected"))
            conn.pending.clear()


def start_ws_server():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    ws_port = int(os.environ.get("MCP_WS_PORT", "3142"))
    
    async def serve():
        async with websockets.serve(ws_handler, "0.0.0.0", ws_port):
            print(f"MCP WebSocket Server listening on ws://localhost:{ws_port}", file=sys.stderr)
            await asyncio.Future()
            
    loop.run_until_complete(serve())


threading.Thread(target=start_ws_server, daemon=True).start()

# ── MCP server ────────────────────────────────────────────────────────────────

mcp = FastMCP(
    "physbox-mcp",
    instructions=(
        "PhysBox: MCP - Model Context Protocol server for Flux (5173), Volt (5174), Mesh (5175). "
        "Call detect_apps first to confirm which apps are running."
    ),
)

P  = APPS["process"]["port"]
C  = APPS["circuit"]["port"]
Ph = APPS["physics"]["port"]


# ── Universal ─────────────────────────────────────────────────────────────────

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


# ── Process Expert ────────────────────────────────────────────────────────────

@mcp.tool(description=process_docs.get("tools", {}).get("process_get_state", "Return Process Expert state"))
async def process_get_state() -> Any:
    return await get_conn(P).send("GET_STATE")

@mcp.tool(description=process_docs.get("tools", {}).get("process_get_metrics", "Return Process Expert metrics"))
async def process_get_metrics() -> Any:
    return await get_conn(P).send("GET_METRICS")

@mcp.tool(description=process_docs.get("tools", {}).get("process_start", "Start simulation"))
async def process_start() -> Any:
    return await get_conn(P).send("START_SIM")

@mcp.tool(description=process_docs.get("tools", {}).get("process_stop", "Stop simulation"))
async def process_stop() -> Any:
    return await get_conn(P).send("STOP_SIM")

@mcp.tool(description=process_docs.get("tools", {}).get("process_reset", "Reset simulation"))
async def process_reset() -> Any:
    return await get_conn(P).send("RESET_SIM")

@mcp.tool(description=process_docs.get("tools", {}).get("process_list_presets", "List Process presets"))
async def process_list_presets() -> Any:
    return await get_conn(P).send("LIST_PRESETS")

@mcp.tool(description=process_docs.get("tools", {}).get("process_load_preset", "Load Process preset"))
async def process_load_preset(preset: str) -> Any:
    return await get_conn(P).send("LOAD_PRESET", {"preset": preset})

@mcp.tool(description=process_docs.get("tools", {}).get("process_get_library", "Get diagram library"))
async def process_get_library() -> Any:
    return await get_conn(P).send("GET_LIBRARY")

@mcp.tool(description=process_docs.get("tools", {}).get("process_save_library", "Save current diagram"))
async def process_save_library(name: str) -> Any:
    return await get_conn(P).send("SAVE_LIBRARY", {"name": name})

@mcp.tool(description=process_docs.get("tools", {}).get("process_set_nodes", "Set canvas nodes"))
async def process_set_nodes(nodes: list) -> Any:
    return await get_conn(P).send("SET_NODES", {"nodes": nodes})

@mcp.tool(description=process_docs.get("tools", {}).get("process_set_edges", "Set canvas edges"))
async def process_set_edges(edges: list) -> Any:
    return await get_conn(P).send("SET_EDGES", {"edges": edges})

@mcp.tool(description=process_docs.get("tools", {}).get("process_run_headless", "Run headless simulation"))
async def process_run_headless(ticks: int) -> Any:
    return await get_conn(P).send("RUN_HEADLESS", {"ticks": ticks}, timeout=30.0)

@mcp.tool(description=process_docs.get("tools", {}).get("process_get_history", "Get simulation logs"))
async def process_get_history() -> Any:
    return await get_conn(P).send("GET_HISTORY")

@mcp.tool(description=process_docs.get("tools", {}).get("process_run_monte_carlo", "Run Monte Carlo simulation"))
async def process_run_monte_carlo(runs: int = 100, ticks: int = 3600) -> Any:
    return await get_conn(P).send("RUN_MONTE_CARLO", {"runs": runs, "ticks": ticks}, timeout=60.0)

@mcp.tool(description=process_docs.get("tools", {}).get("process_run_optimizer", "Run optimizer sweep"))
async def process_run_optimizer(
    targetMetric: str,
    params: list,
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

@mcp.tool(description=process_docs.get("tools", {}).get("process_get_schema", "Return Process Expert schema"))
async def process_get_schema() -> Any:
    return process_docs.get("schema", {})


# ── Circuit Expert ────────────────────────────────────────────────────────────

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_get_state", "Return Circuit Expert state"))
async def circuit_get_state() -> Any:
    return await get_conn(C).send("GET_STATE")

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_get_components", "Get components list"))
async def circuit_get_components() -> Any:
    return await get_conn(C).send("GET_COMPONENTS")

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_run_sim", "Run SPICE simulation"))
async def circuit_run_sim() -> Any:
    return await get_conn(C).send("RUN_SIM")

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_stop_sim", "Stop SPICE simulation"))
async def circuit_stop_sim() -> Any:
    return await get_conn(C).send("STOP_SIM")

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_toggle_probe", "Toggle probe mode"))
async def circuit_toggle_probe() -> Any:
    return await get_conn(C).send("TOGGLE_PROBE")

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_load_preset", "Load Circuit preset"))
async def circuit_load_preset(preset: str) -> Any:
    return await get_conn(C).send("LOAD_PRESET", {"preset": preset})

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_set_nodes", "Set circuit components"))
async def circuit_set_nodes(nodes: list) -> Any:
    return await get_conn(C).send("SET_NODES", {"nodes": nodes})

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_set_edges", "Set circuit wires"))
async def circuit_set_edges(edges: list) -> Any:
    return await get_conn(C).send("SET_EDGES", {"edges": edges})

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_get_schema", "Return Circuit Expert schema"))
async def circuit_get_schema() -> Any:
    return circuit_docs.get("schema", {})

@mcp.tool(description=circuit_docs.get("tools", {}).get("circuit_get_waveforms", "Return component waveforms"))
async def circuit_get_waveforms() -> Any:
    return await get_conn(C).send("GET_WAVEFORMS")

@mcp.tool(description="Upload audio samples to a microphone node. Values should be numbers in range [-1, 1]. Either values or pwlData must be provided.")
async def circuit_upload_audio(
    nodeId: str,
    values: list[float] | None = None,
    sampleRate: float = 8000.0,
    pwlData: list[dict] | None = None
) -> Any:
    return await get_conn(C).send("UPLOAD_AUDIO", {
        "nodeId": nodeId,
        "values": values,
        "sampleRate": sampleRate,
        "pwlData": pwlData
    })

@mcp.tool(description="Download audio waveforms from a speaker node, optionally interpolated to a specific sample rate")
async def circuit_download_audio(
    nodeId: str,
    sampleRate: float = 8000.0,
    acCouple: bool | None = None,
    normalize: bool | None = None,
    voltageScale: float | None = None
) -> Any:
    payload = {"nodeId": nodeId, "sampleRate": sampleRate}
    if acCouple is not None: payload["acCouple"] = acCouple
    if normalize is not None: payload["normalize"] = normalize
    if voltageScale is not None: payload["voltageScale"] = voltageScale
    return await get_conn(C).send("GET_SPEAKER_AUDIO", payload)


# ── Physics Sim ───────────────────────────────────────────────────────────────

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_state", "Return Physics Sim state"))
async def physics_get_state() -> Any:
    return await get_conn(Ph).send("GET_STATE")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_scene", "Return physics scene graph"))
async def physics_get_scene() -> Any:
    return await get_conn(Ph).send("GET_SCENE")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_scene_summary", "Return a lightweight scene summary"))
async def physics_get_scene_summary() -> Any:
    return await get_conn(Ph).send("GET_SCENE_SUMMARY")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_play", "Start physics simulation"))
async def physics_play() -> Any:
    return await get_conn(Ph).send("PLAY")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_stop", "Stop physics simulation"))
async def physics_stop() -> Any:
    return await get_conn(Ph).send("STOP")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_toggle_play", "Toggle play/pause"))
async def physics_toggle_play() -> Any:
    return await get_conn(Ph).send("TOGGLE_PLAY")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_reset", "Reset physics simulation"))
async def physics_reset() -> Any:
    return await get_conn(Ph).send("RESET")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_list_presets", "List Physics presets"))
async def physics_list_presets() -> Any:
    return await get_conn(Ph).send("LIST_PRESETS")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_load_preset", "Load Physics preset"))
async def physics_load_preset(preset: str) -> Any:
    return await get_conn(Ph).send("LOAD_PRESET", {"preset": preset})

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_set_environment", "Set environment parameters"))
async def physics_set_environment(
    gravityZ: float | None = None,
    windX: float | None = None,
    windY: float | None = None,
    density: float | None = None,
    floorFriction: float | None = None,
) -> Any:
    payload = {}
    if gravityZ     is not None: payload["gravityZ"]      = gravityZ
    if windX        is not None: payload["windX"]         = windX
    if windY        is not None: payload["windY"]         = windY
    if density      is not None: payload["density"]       = density
    if floorFriction is not None: payload["floorFriction"] = floorFriction
    return await get_conn(Ph).send("SET_ENVIRONMENT", payload)

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_update_scene", "Replace scene graph"))
async def physics_update_scene(sceneGraph: list) -> Any:
    # Blocks until any SCAD bodies finish compiling and a final MJCF recompile
    # settles, so scenes with several scad meshes need more than the default 10s.
    return await get_conn(Ph).send("UPDATE_SCENE", {"sceneGraph": sceneGraph}, timeout=60.0)

@mcp.tool(description=physics_docs.get("tools", {}).get(
    "physics_get_screenshot",
    "Capture the current rendered frame of the Physics Sim's 3D viewport as a PNG image."
))
async def physics_get_screenshot() -> Any:
    result = await get_conn(Ph).send("SCREENSHOT")
    if not isinstance(result, dict) or not result.get("ok"):
        error = result.get("error") if isinstance(result, dict) else "Unknown error"
        raise RuntimeError(f"Screenshot failed: {error}")
    data_url = result["dataUrl"]
    # data_url looks like "data:image/png;base64,AAAA..."
    b64 = data_url.split(",", 1)[1]
    return Image(data=base64.b64decode(b64), format="png")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_schema", "Return Physics Sim schema"))
async def physics_get_schema() -> Any:
    return physics_docs.get("schema", {})

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_build_scene", "Build scene from bodies"))
async def physics_build_scene(bodies: list) -> Any:
    # Blocks until any SCAD bodies finish compiling and a final MJCF recompile
    # settles, so scenes with several scad meshes need more than the default 10s.
    return await get_conn(Ph).send("BUILD_SCENE", {"bodies": bodies}, timeout=60.0)

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_run_headless", "Run simulation headlessly"))
async def physics_run_headless(ticks: int = 300) -> Any:
    return await get_conn(Ph).send("RUN_HEADLESS", {"ticks": ticks}, timeout=30.0)

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_history", "Return the complete array of physical telemetry history up to 5000 frames"))
async def physics_get_history() -> Any:
    return await get_conn(Ph).send("GET_HISTORY")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_telemetry", "Return only the latest single frame of simulation telemetry"))
async def physics_get_telemetry() -> Any:
    return await get_conn(Ph).send("GET_TELEMETRY")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_get_note_cards", "Return the current array of note card overlays"))
async def physics_get_note_cards() -> Any:
    return await get_conn(Ph).send("GET_NOTE_CARDS")

@mcp.tool(description=physics_docs.get("tools", {}).get("physics_set_note_cards", "Replace the note card overlays"))
async def physics_set_note_cards(noteCards: list) -> Any:
    return await get_conn(Ph).send("SET_NOTE_CARDS", {"noteCards": noteCards})


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    use_stdio = "--stdio" in sys.argv
    port_arg  = next((a for a in sys.argv if a.startswith("--port=")), None)
    http_port = int(port_arg.split("=")[1]) if port_arg else int(os.environ.get("MCP_PORT", "3141"))

    if use_stdio:
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="http", host="localhost", port=http_port)

if __name__ == "__main__":
    main()
