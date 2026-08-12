# PhysBox: MCP

PhysBox: MCP is a Model Context Protocol (MCP) server that enables LLMs and MCP clients (such as Claude Code or Claude Desktop) to interact programmatically with the three simulation web applications in the browser:

| Application | Production URL | Description |
|---|---|---|
| **Volt** | [volt.physbox.io](https://volt.physbox.io) | SPICE circuit simulation (powered by NgSpice WASM in browser) |
| **Mesh** | [mesh.physbox.io](https://mesh.physbox.io) | Rigid-body physics simulation & OpenSCAD parametric CAD (powered by MuJoCo WASM) |
| **Etch** | [etch.physbox.io](https://etch.physbox.io) | 2D vector studio, CNC toolpathing & WebSerial GRBL controller |
| **Flux** (Coming Soon) | [flux.physbox.io](https://flux.physbox.io) | Discrete-event / system-dynamics simulation |

All communication is handled via JSON over WebSockets directly to the web app in your browser—no browser automation or DOM scraping is needed.

### 🧠 The Only AI-Native Physical World Model
Unlike static code generation or offline file outputs, PhysBox gives AI agents an **interactive ground-truth world model**. LLMs execute 53 typed tool calls to observe spatial kinematics, circuit node waveforms, and G-code safety warnings, allowing them to self-correct designs iteratively.

---

## 💬 Example Agent Prompts for Claude Code & Cursor

Paste any of these prompts directly into your MCP-connected agent:

* **Volt (Electronics):**
  > *"Optimize the RC filter values in the active Volt schematic to achieve a 1 kHz cutoff frequency, then execute `circuit_run_sim` and plot the magnitude response."*
* **Mesh (3D Physics & CAD):**
  > *"Write an OpenSCAD parametric mounting bracket in Mesh with 4mm screw holes, run a MuJoCo collision stability check using `physics_check_collisions`, and export the Z-up STL."*
* **Etch (CNC & Laser):**
  > *"Import this vector SVG logo, apply 2-pass 3mm stepdown feeds for Plywood, check end mill safety warnings, and generate GRBL G-code."*

---

## How It Works

PhysBox: MCP functions as a local companion server that establishes a WebSocket relay on port `3142`. 

When you open any of the simulation web apps, they connect directly to this WebSocket relay. When an MCP client executes a tool call, the command flows from the client to the companion server, gets forwarded to the active browser tab, and the results flow back.

```
MCP Client (e.g. Claude Desktop)
  └── spawns → physbox-mcp (stdio)
                 └── WebSocket Server (ws://localhost:3142)
                                ├── Flux
                                ├── Volt
                                └── Mesh
```

---

## Installation

Install the companion server directly from PyPI:

```bash
pip install physbox-mcp
```

---

## Usage & Setup

### 1. Open the Web Applications
Launch or access the simulation web applications in your web browser:
*   **Flux:** [flux.physbox.io](https://flux.physbox.io)
*   **Volt:** [volt.physbox.io](https://volt.physbox.io)
*   **Mesh:** [mesh.physbox.io](https://mesh.physbox.io)

As soon as a page finishes loading, it automatically registers with the companion WebSocket server.

### 2. Configure Your MCP Client

#### For Claude Desktop
Add the following block to your Claude Desktop configuration file (typically located at `AppData/Roaming/Claude/claude_desktop_config.json` on Windows or `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS):

```json
{
  "mcpServers": {
    "physbox-mcp": {
      "command": "physbox-mcp",
      "args": ["--stdio"]
    }
  }
}
```

#### For Claude Code / Local Workspace (`.mcp.json`)
Add a `.mcp.json` file to your project root (or update your global configuration at `~/.claude/mcp.json`):

```json
{
  "mcpServers": {
    "physbox-mcp": {
      "type": "stdio",
      "command": "physbox-mcp",
      "args": ["--stdio"]
    }
  }
}
```

> **Developing from source?** If running directly from a cloned repository or virtual environment, use your Python interpreter:
> ```json
> {
>   "mcpServers": {
>     "physbox-mcp": {
>       "type": "stdio",
>       "command": "python",
>       "args": ["-m", "physbox_mcp.server", "--stdio"]
>     }
>   }
> }
> ```
> *(Replace `"python"` with the path to your virtual environment's Python executable if needed, e.g. `/path/to/venv/bin/python`)*.

#### For Google Antigravity IDE

On Windows/WSL setups, Antigravity IDE reads configuration from the global configuration directory. Because the global directory (`~/.gemini/config/`) may be write-restricted, you should link it to the writable `~/.gemini/antigravity/` folder:

1. In PowerShell, create a **Hard Link** from the global configuration target to the writable user directory:
   ```powershell
   # Delete the empty placeholder file if it exists
   Remove-Item -Path "$env:USERPROFILE\.gemini\config\mcp_config.json" -Force -ErrorAction SilentlyContinue
   
   # Create a Hard Link to the writable copy
   New-Item -ItemType HardLink -Path "$env:USERPROFILE\.gemini\config\mcp_config.json" -Target "$env:USERPROFILE\.gemini\antigravity\mcp_config.json"
   ```

2. Add the `physbox-mcp` WSL configuration to your `mcp_config.json` (replacing `<your-wsl-distro>` with your WSL distribution such as `Ubuntu-20.04`):
   ```json
   {
     "mcpServers": {
       "physbox-mcp": {
         "command": "C:\\Windows\\system32\\wsl.exe",
         "args": [
           "-d",
           "<your-wsl-distro>",
           "physbox-mcp",
           "--stdio"
         ]
       }
     }
   }
   ```
   *(If developing from source in WSL, pass your virtual environment's Python binary and `-m physbox_mcp.server` in `args` instead).*

3. **Restart the IDE** (or close and reload the agent chat session) to register the MCP tools natively.


### 3. Run the Companion Server Manually (Optional)
If you are running the server in HTTP mode rather than Stdio, you can run:

```bash
# Starts HTTP server listening on port 3141 (default)
physbox-mcp
```

Or configure custom port parameters:
```bash
physbox-mcp --port=4000
```

---

## Development & Contribution
For instructions on local development, modifying schemas, extending tool definitions, and manual builds, please refer to [README_DEV.md](README_DEV.md).
