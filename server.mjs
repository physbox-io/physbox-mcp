#!/usr/bin/env node
/**
 * MCP server for local web apps: Flux, Volt, Mesh.
 *
 * Modes:
 *   HTTP (default)  node server.mjs              # listens on MCP_PORT (default 3141)
 *   HTTP custom     node server.mjs --port=4000
 *   stdio           node server.mjs --stdio       # for MCP clients that spawn the process
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { WebSocket, WebSocketServer } from "ws";
import http from "http";
import fs from "fs";
import path from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

function loadMcpDocs(appId) {
  const possiblePaths = [
    path.join(__dirname, "..", appId, "mcp-docs.json"),
    path.join(__dirname, "physbox_mcp", "mcp-docs", `${appId}.json`),
    path.join("/home/boab", appId, "mcp-docs.json")
  ];
  for (const p of possiblePaths) {
    try {
      if (fs.existsSync(p)) {
        return JSON.parse(fs.readFileSync(p, "utf8"));
      }
    } catch (e) {
      console.error(`Error reading docs for ${appId} from ${p}:`, e);
    }
  }
  return {};
}

const physicsDocs = loadMcpDocs("physics");
const processDocs = loadMcpDocs("process");
const circuitDocs = loadMcpDocs("circuit");

// ── App registry ─────────────────────────────────────────────────────────────

const APPS = {
  process: { port: 5173, name: "Flux" },
  circuit: { port: 5174, name: "Volt" },
  physics: { port: 5175, name: "Mesh" },
};

// ── Connection pool ───────────────────────────────────────────────────────────

const connections = new Map(); // port → AppConnection

class AppConnection {
  constructor(port) {
    this.port = port;
    this.ws = null;
    this.pending = new Map(); // id → { resolve, reject, timer }
  }

  get connected() {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  setWebSocket(ws) {
    if (this.ws) {
      try { this.ws.close(); } catch {}
    }
    this.ws = ws;

    ws.on("message", (raw) => {
      let msg;
      try { msg = JSON.parse(raw.toString()); } catch { return; }
      if (msg.event === "RESULT" || msg.event === "ERROR") {
        const p = this.pending.get(msg.id);
        if (p) {
          clearTimeout(p.timer);
          this.pending.delete(msg.id);
          if (msg.event === "ERROR") p.reject(new Error(msg.error));
          else p.resolve(msg.data);
        }
      }
    });

    ws.on("close", () => {
      if (this.ws === ws) {
        this.ws = null;
      }
      // Reject any pending calls
      for (const [id, p] of this.pending) {
        clearTimeout(p.timer);
        p.reject(new Error("WebSocket closed"));
      }
      this.pending.clear();
    });

    ws.on("error", () => {});
  }

  send(cmd, payload = {}, timeoutMs = 10000) {
    if (!this.connected) {
      return Promise.reject(
        new Error(`App on port ${this.port} is not connected. Open the app in your browser!`)
      );
    }
    const id = Math.random().toString(36).slice(2, 10);
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Timeout waiting for "${cmd}" response (${timeoutMs}ms)`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify({ cmd, id, ...payload }));
    });
  }
}

function getConn(port) {
  if (!connections.has(port)) {
    connections.set(port, new AppConnection(port));
  }
  return connections.get(port);
}

// ── WebSocket Server ──────────────────────────────────────────────────────────

const wsPort = parseInt(process.env.MCP_WS_PORT ?? "3142");
const wss = new WebSocketServer({ port: wsPort });
console.error(`MCP WebSocket Server listening on ws://localhost:${wsPort}`);

wss.on("connection", (ws) => {
  ws.on("message", (raw) => {
    try {
      const msg = JSON.parse(raw.toString());
      if (msg.event === "HELLO") {
        const appKey = msg.app; // e.g. "process", "circuit", "physics"
        const appInfo = APPS[appKey];
        if (appInfo) {
          const conn = getConn(appInfo.port);
          conn.setWebSocket(ws);
          console.error(`Registered browser connection for ${appInfo.name} on port ${appInfo.port}`);
          ws.send(JSON.stringify({ event: "CONNECTED", role: "browser" }));
        } else {
          console.error(`Unknown app connected: ${appKey}`);
        }
      }
    } catch (e) {
      console.error("Error processing initial message:", e);
    }
  });
});

// ── HTTP probe ────────────────────────────────────────────────────────────────

function probePort(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://localhost:${port}`, { timeout: 1500 }, (res) => {
      resolve({ open: true, status: res.statusCode });
      res.resume();
    });
    req.on("error", () => resolve({ open: false }));
    req.on("timeout", () => { req.destroy(); resolve({ open: false }); });
  });
}

// ── Tool definitions ─────────────────────────────────────────────────────────

const TOOLS = [
  // ── Universal ──────────────────────────────────────────────────
  {
    name: "detect_apps",
    description: "Check which of the three local apps are currently running.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "send_command",
    description: "Send an arbitrary JSON command to any app and return the result.",
    inputSchema: {
      type: "object",
      required: ["port", "cmd"],
      properties: {
        port: { type: "number" },
        cmd:  { type: "string" },
        payload: { type: "object" },
      },
    },
  },
  // ── Process Expert ──────────────────────────────────────────────
  {
    name: "process_get_state",
    description: processDocs?.tools?.process_get_state || "Return full Process Expert state.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_get_metrics",
    description: processDocs?.tools?.process_get_metrics || "Return per-node simulation metrics.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_start",
    description: processDocs?.tools?.process_start || "Start simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_stop",
    description: processDocs?.tools?.process_stop || "Stop simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_reset",
    description: processDocs?.tools?.process_reset || "Reset simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_list_presets",
    description: processDocs?.tools?.process_list_presets || "List presets.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_load_preset",
    description: processDocs?.tools?.process_load_preset || "Load named preset.",
    inputSchema: {
      type: "object",
      required: ["preset"],
      properties: { preset: { type: "string" } },
    },
  },
  {
    name: "process_get_library",
    description: processDocs?.tools?.process_get_library || "Get user saved library.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_save_library",
    description: processDocs?.tools?.process_save_library || "Save current canvas.",
    inputSchema: {
      type: "object",
      required: ["name"],
      properties: { name: { type: "string" } },
    },
  },
  {
    name: "process_set_nodes",
    description: processDocs?.tools?.process_set_nodes || "Set canvas nodes.",
    inputSchema: {
      type: "object",
      required: ["nodes"],
      properties: { nodes: { type: "array" } },
    },
  },
  {
    name: "process_set_edges",
    description: processDocs?.tools?.process_set_edges || "Set canvas edges.",
    inputSchema: {
      type: "object",
      required: ["edges"],
      properties: { edges: { type: "array" } },
    },
  },
  {
    name: "process_run_headless",
    description: processDocs?.tools?.process_run_headless || "Run simulation headlessly.",
    inputSchema: {
      type: "object",
      required: ["ticks"],
      properties: { ticks: { type: "number" } },
    },
  },
  {
    name: "process_get_history",
    description: processDocs?.tools?.process_get_history || "Get simulation history logs.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "process_run_monte_carlo",
    description: processDocs?.tools?.process_run_monte_carlo || "Run Monte Carlo simulation.",
    inputSchema: {
      type: "object",
      properties: {
        runs: { type: "number" },
        ticks: { type: "number" },
      },
    },
  },
  {
    name: "process_run_optimizer",
    description: processDocs?.tools?.process_run_optimizer || "Run optimizer parameter sweep.",
    inputSchema: {
      type: "object",
      required: ["targetMetric", "params"],
      properties: {
        targetMetric: { type: "string" },
        mode: { type: "string" },
        ticks: { type: "number" },
        strategy: { type: "string" },
        populationSize: { type: "number" },
        generations: { type: "number" },
        mutationRate: { type: "number" },
        params: { type: "array" },
      },
    },
  },
  {
    name: "process_get_schema",
    description: processDocs?.tools?.process_get_schema || "Return Process Expert schema.",
    inputSchema: { type: "object", properties: {} },
  },

  // ── Circuit Expert ──────────────────────────────────────────────
  {
    name: "circuit_get_state",
    description: circuitDocs?.tools?.circuit_get_state || "Return Circuit Expert state.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_get_components",
    description: circuitDocs?.tools?.circuit_get_components || "Get components.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_run_sim",
    description: circuitDocs?.tools?.circuit_run_sim || "Run SPICE simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_stop_sim",
    description: circuitDocs?.tools?.circuit_stop_sim || "Stop SPICE simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_toggle_probe",
    description: circuitDocs?.tools?.circuit_toggle_probe || "Toggle probe mode.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_load_preset",
    description: circuitDocs?.tools?.circuit_load_preset || "Load Circuit preset.",
    inputSchema: {
      type: "object",
      required: ["preset"],
      properties: { preset: { type: "string" } },
    },
  },
  {
    name: "circuit_set_nodes",
    description: circuitDocs?.tools?.circuit_set_nodes || "Set components.",
    inputSchema: {
      type: "object",
      required: ["nodes"],
      properties: { nodes: { type: "array" } },
    },
  },
  {
    name: "circuit_set_edges",
    description: circuitDocs?.tools?.circuit_set_edges || "Set wires.",
    inputSchema: {
      type: "object",
      required: ["edges"],
      properties: { edges: { type: "array" } },
    },
  },
  {
    name: "circuit_get_schema",
    description: circuitDocs?.tools?.circuit_get_schema || "Return Circuit Expert schema.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_get_waveforms",
    description: circuitDocs?.tools?.circuit_get_waveforms || "Return component waveforms.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "circuit_upload_audio",
    description: "Upload audio samples to a microphone node. Values should be numbers in range [-1, 1]. Either values or pwlData must be provided.",
    inputSchema: {
      type: "object",
      required: ["nodeId"],
      properties: {
        nodeId: { type: "string", description: "ID of the microphone node" },
        values: { type: "array", items: { type: "number" }, description: "Flat list of normalized audio samples" },
        sampleRate: { type: "number", description: "Sample rate in Hz (default 8000), used only if values is provided" },
        pwlData: {
          type: "array",
          items: {
            type: "object",
            required: ["t", "v"],
            properties: {
              t: { type: "number", description: "Timestamp in seconds" },
              v: { type: "number", description: "Voltage value" }
            }
          },
          description: "Direct PWL points array"
        }
      }
    }
  },
  {
    name: "circuit_download_audio",
    description: "Download audio waveforms from a speaker node, optionally interpolated to a specific sample rate.",
    inputSchema: {
      type: "object",
      required: ["nodeId"],
      properties: {
        nodeId: { type: "string", description: "ID of the speaker node" },
        sampleRate: { type: "number", description: "Desired sample rate in Hz (default 8000)" },
        acCouple: { type: "boolean", description: "Remove DC offset (default true if set on node)" },
        normalize: { type: "boolean", description: "Scale peak to 0.8 (default true if set on node)" },
        voltageScale: { type: "number", description: "Full-scale voltage scaling factor (default 5.0)" }
      }
    }
  },

  // ── Physics Sim ─────────────────────────────────────────────────
  {
    name: "physics_get_state",
    description: physicsDocs?.tools?.physics_get_state || "Return Physics Sim state.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_get_scene",
    description: physicsDocs?.tools?.physics_get_scene || "Return physics scene graph.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_get_scene_summary",
    description: physicsDocs?.tools?.physics_get_scene_summary || "Return a lightweight scene summary (no raw mesh vertex/face arrays).",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_play",
    description: physicsDocs?.tools?.physics_play || "Start physics simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_stop",
    description: physicsDocs?.tools?.physics_stop || "Stop physics simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_toggle_play",
    description: physicsDocs?.tools?.physics_toggle_play || "Toggle play/pause.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_reset",
    description: physicsDocs?.tools?.physics_reset || "Reset physics simulation.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_list_presets",
    description: physicsDocs?.tools?.physics_list_presets || "List Physics presets.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_load_preset",
    description: physicsDocs?.tools?.physics_load_preset || "Load Physics preset.",
    inputSchema: {
      type: "object",
      required: ["preset"],
      properties: { preset: { type: "string" } },
    },
  },
  {
    name: "physics_set_environment",
    description: physicsDocs?.tools?.physics_set_environment || "Set environment parameters.",
    inputSchema: {
      type: "object",
      properties: {
        gravityZ:      { type: "number" },
        windX:         { type: "number" },
        windY:         { type: "number" },
        density:       { type: "number" },
        floorFriction: { type: "number" },
      },
    },
  },
  {
    name: "physics_update_scene",
    description: physicsDocs?.tools?.physics_update_scene || "Replace scene graph.",
    inputSchema: {
      type: "object",
      required: ["sceneGraph"],
      properties: { sceneGraph: { type: "array" } },
    },
  },
  {
    name: "physics_get_schema",
    description: physicsDocs?.tools?.physics_get_schema || "Return Physics Sim schema.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_build_scene",
    description: physicsDocs?.tools?.physics_build_scene || "Build scene from body descriptors.",
    inputSchema: {
      type: "object",
      required: ["bodies"],
      properties: { bodies: { type: "array" } },
    },
  },
  {
    name: "physics_run_headless",
    description: physicsDocs?.tools?.physics_run_headless || "Run simulation headlessly.",
    inputSchema: {
      type: "object",
      properties: { ticks: { type: "number" } },
    },
  },
  {
    name: "physics_get_history",
    description: physicsDocs?.tools?.physics_get_history || "Return the complete array of physical telemetry history up to 5000 frames.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_get_telemetry",
    description: physicsDocs?.tools?.physics_get_telemetry || "Return only the latest single frame of simulation telemetry.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_get_note_cards",
    description: physicsDocs?.tools?.physics_get_note_cards || "Return the current array of note card overlays.",
    inputSchema: { type: "object", properties: {} },
  },
  {
    name: "physics_set_note_cards",
    description: physicsDocs?.tools?.physics_set_note_cards || "Replace the note card overlays.",
    inputSchema: {
      type: "object",
      required: ["noteCards"],
      properties: {
        noteCards: { type: "array", description: "Array of note card objects" }
      }
    }
  },
];

// ── Tool handler ──────────────────────────────────────────────────────────────

async function handleTool(name, args) {
  const P = APPS.process.port;
  const C = APPS.circuit.port;
  const Ph = APPS.physics.port;

  switch (name) {

    case "detect_apps": {
      const results = await Promise.all(
        Object.entries(APPS).map(async ([id, app]) => {
          const probe = await probePort(app.port);
          const conn = getConn(app.port);
          const wsOk = conn.connected;
          return { id, name: app.name, port: app.port, httpOpen: probe.open, wsConnected: wsOk };
        })
      );
      return results;
    }

    case "send_command": {
      const conn = getConn(args.port);
      return conn.send(args.cmd, args.payload ?? {});
    }

    // ── Process ────────────────────────────────────────────────────
    case "process_get_state":    return getConn(P).send("GET_STATE");
    case "process_get_metrics":  return getConn(P).send("GET_METRICS");
    case "process_start":        return getConn(P).send("START_SIM");
    case "process_stop":         return getConn(P).send("STOP_SIM");
    case "process_reset":        return getConn(P).send("RESET_SIM");
    case "process_list_presets": return getConn(P).send("LIST_PRESETS");
    case "process_get_library":  return getConn(P).send("GET_LIBRARY");
    case "process_load_preset":  return getConn(P).send("LOAD_PRESET", { preset: args.preset });
    case "process_save_library": return getConn(P).send("SAVE_LIBRARY", { name: args.name });
    case "process_set_nodes":    return getConn(P).send("SET_NODES", { nodes: args.nodes });
    case "process_set_edges":    return getConn(P).send("SET_EDGES", { edges: args.edges });
    case "process_run_headless": return getConn(P).send("RUN_HEADLESS", { ticks: args.ticks }, 30000);
    case "process_get_history":  return getConn(P).send("GET_HISTORY");
    case "process_run_monte_carlo": return getConn(P).send("RUN_MONTE_CARLO", { runs: args.runs, ticks: args.ticks }, 60000);
    case "process_run_optimizer":   return getConn(P).send("RUN_OPTIMIZER", { targetMetric: args.targetMetric, mode: args.mode, ticks: args.ticks, params: args.params, strategy: args.strategy, populationSize: args.populationSize, generations: args.generations, mutationRate: args.mutationRate }, 60000);
    case "process_get_schema":   return processDocs.schema || {};

    // ── Circuit ────────────────────────────────────────────────────
    case "circuit_get_state":      return getConn(C).send("GET_STATE");
    case "circuit_get_components": return getConn(C).send("GET_COMPONENTS");
    case "circuit_run_sim":        return getConn(C).send("RUN_SIM");
    case "circuit_stop_sim":       return getConn(C).send("STOP_SIM");
    case "circuit_toggle_probe":   return getConn(C).send("TOGGLE_PROBE");
    case "circuit_load_preset":    return getConn(C).send("LOAD_PRESET", { preset: args.preset });
    case "circuit_set_nodes":      return getConn(C).send("SET_NODES", { nodes: args.nodes });
    case "circuit_set_edges":      return getConn(C).send("SET_EDGES", { edges: args.edges });
    case "circuit_get_schema":     return circuitDocs.schema || {};
    case "circuit_get_waveforms":  return getConn(C).send("GET_WAVEFORMS");
    case "circuit_upload_audio":
      return getConn(C).send("UPLOAD_AUDIO", {
        nodeId: args.nodeId,
        values: args.values,
        sampleRate: args.sampleRate,
        pwlData: args.pwlData
      });
    case "circuit_download_audio":
      return getConn(C).send("GET_SPEAKER_AUDIO", {
        nodeId: args.nodeId,
        sampleRate: args.sampleRate,
        acCouple: args.acCouple,
        normalize: args.normalize,
        voltageScale: args.voltageScale
      });

    // ── Physics ────────────────────────────────────────────────────
    case "physics_get_state":      return getConn(Ph).send("GET_STATE");
    case "physics_get_scene":      return getConn(Ph).send("GET_SCENE");
    case "physics_get_scene_summary": return getConn(Ph).send("GET_SCENE_SUMMARY");
    case "physics_play":           return getConn(Ph).send("PLAY");
    case "physics_stop":           return getConn(Ph).send("STOP");
    case "physics_toggle_play":    return getConn(Ph).send("TOGGLE_PLAY");
    case "physics_reset":          return getConn(Ph).send("RESET");
    case "physics_list_presets":   return getConn(Ph).send("LIST_PRESETS");
    case "physics_load_preset":    return getConn(Ph).send("LOAD_PRESET", { preset: args.preset });
    case "physics_set_environment":
      return getConn(Ph).send("SET_ENVIRONMENT", {
        gravityZ: args.gravityZ, windX: args.windX, windY: args.windY,
        density: args.density, floorFriction: args.floorFriction,
      });
    case "physics_update_scene":
      // Blocks until any SCAD bodies finish compiling and a final MJCF recompile
      // settles, so scenes with several scad meshes need more than the default timeout.
      return getConn(Ph).send("UPDATE_SCENE", { sceneGraph: args.sceneGraph }, 60000);
    case "physics_get_schema":     return physicsDocs.schema || {};
    case "physics_build_scene":    return getConn(Ph).send("BUILD_SCENE", { bodies: args.bodies }, 60000);
    case "physics_run_headless":   return getConn(Ph).send("RUN_HEADLESS", { ticks: args.ticks }, 30000);
    case "physics_get_history":    return getConn(Ph).send("GET_HISTORY");
    case "physics_get_telemetry":  return getConn(Ph).send("GET_TELEMETRY");
    case "physics_get_note_cards": return getConn(Ph).send("GET_NOTE_CARDS");
    case "physics_set_note_cards": return getConn(Ph).send("SET_NOTE_CARDS", { noteCards: args.noteCards });

    default:
      throw new Error(`Unknown tool: ${name}`);
  }
}

// ── MCP server ────────────────────────────────────────────────────────────────

let mcpVersion = "0.0.0";
try {
  const tomlContent = fs.readFileSync(path.join(__dirname, "pyproject.toml"), "utf8");
  const match = tomlContent.match(/version\s*=\s*["']([^"']+)["']/);
  if (match) {
    mcpVersion = match[1];
  }
} catch (e) {
  // ignore
}

const server = new Server(
  { name: "physbox-mcp", version: mcpVersion },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: TOOLS }));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;
  try {
    const result = await handleTool(name, args ?? {});
    return {
      content: [{ type: "text", text: JSON.stringify(result, null, 2) }],
    };
  } catch (err) {
    return {
      content: [{ type: "text", text: `Error: ${err.message}` }],
      isError: true,
    };
  }
});

// Eagerly open connections in the background (non-blocking)
for (const app of Object.values(APPS)) {
  getConn(app.port);
}

// ── Transport ─────────────────────────────────────────────────────────────────

const useStdio = process.argv.includes("--stdio");
const portArg  = process.argv.find(a => a.startsWith("--port="));
const httpPort = portArg ? parseInt(portArg.split("=")[1]) : parseInt(process.env.MCP_PORT ?? "3141");

if (useStdio) {
  const transport = new StdioServerTransport();
  await server.connect(transport);
} else {
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  const httpServer = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/mcp") {
      transport.handleRequest(req, res);
    } else if (req.method === "GET" && req.url === "/mcp") {
      transport.handleRequest(req, res);
    } else if (req.method === "DELETE" && req.url === "/mcp") {
      transport.handleRequest(req, res);
    } else if (req.url === "/health") {
      res.writeHead(200).end(JSON.stringify({ ok: true, apps: Object.keys(APPS) }));
    } else {
      res.writeHead(404).end();
    }
  });
  await server.connect(transport);
  httpServer.listen(httpPort, () => {
    console.error(`physbox-mcp listening on http://localhost:${httpPort}/mcp`);
  });
}
