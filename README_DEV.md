# PhysBox: MCP — Developer & Contributor Guide

This repository contains the companion Model Context Protocol (MCP) server for the **PhysBox** suite of local simulation applications. It facilitates seamless communication between LLMs/MCP clients (e.g., Claude Code, Claude Desktop) and locally-running web apps via a WebSocket bridge.

---

## Architecture Overview

Browsers can only run as WebSocket clients, not servers. The companion server acts as a central broker:
1. It hosts an MCP server (via stdio or HTTP).
2. It hosts a WebSocket relay server on port `3142`.
3. When the user opens a PhysBox web application (e.g., Physics Sim) in their browser, the web application connects to the WebSocket relay.
4. When Claude executes an MCP tool, the command is forwarded over the WebSocket connection to the browser tab, which processes the physics/circuit/process simulation and returns the results back through the relay.

```
MCP Client (e.g. Claude Code)
  └── spawns → physbox-mcp (stdio)
                 └── WebSocket Server (ws://localhost:3142)
                                ├── Flux (Port 5173)
                                ├── Volt (Port 5174)
                                └── Mesh (Port 5175)
```

---

## Local Development Setup

### 1. Prerequisite Repositories
The full workspace is structured with the following sibling repositories:
*   `~/physics` — The rigid-body physics simulator app (MuJoCo WASM).
*   `~/circuit` — The SPICE circuit simulator app (NgSpice WASM).
*   `~/process` — The discrete-event / system-dynamics simulation app.
*   `~/expt_mcp` — This repository (the companion MCP bridge).

### 2. Installing Dependencies
To set up both the Node.js and Python environments, run:
```bash
./setup.sh
```
This script will run `npm install` and create a local Python virtual environment (`venv/`) installing all dependencies listed in `requirements.txt`.

To install the Python package in editable mode for local testing:
```bash
source venv/bin/activate
pip install -e .
```

---

## Syncing MCP Documentation (`mcp-docs/`)

Each simulation app defines its own authoritative schemas and scripting APIs in a file named `mcp-docs.json` at its repository root. 

Since users should be able to run `physbox-mcp` standalone (without cloning the entire simulation codebases), fallback copies of these schema JSON files are committed to this repository:
*   `physbox_mcp/mcp-docs/physics.json` (synced from `~/physics/mcp-docs.json`)
*   `physbox_mcp/mcp-docs/circuit.json` (synced from `~/circuit/mcp-docs.json`)
*   `physbox_mcp/mcp-docs/process.json` (synced from `~/process/mcp-docs.json`)
*   `physbox_mcp/mcp-docs/chemistry.json` (synced from `~/chemistry/mcp-docs.json`)

**Important:** When editing or adding tools inside any of the simulation apps, you must copy their updated `mcp-docs.json` into the `physbox_mcp/mcp-docs/` folder here so the MCP server reflects the updated schemas.

---

## WebSocket JSON Relay Protocol

The WebSocket relay server listens at:
```
ws://localhost:3142
```
All messages exchanged are raw JSON objects.

*   **Command** (controller → browser):
    ```json
    { "cmd": "RUN_HEADLESS", "id": "abc123", "ticks": 600 }
    ```
*   **Result** (browser → controller):
    ```json
    { "event": "RESULT", "cmd": "RUN_HEADLESS", "id": "abc123", "data": { "success": true } }
    ```
*   **Error** (browser → controller):
    ```json
    { "event": "ERROR", "cmd": "RUN_HEADLESS", "id": "abc123", "error": "Compilation failed" }
    ```

The `id` field is echoed back to correlate concurrent requests.

---

## Extending the Bridge

To add a new tool or command:
1.  **Browser side:** Inside the target app's repository, open `src/hooks/useMCPBridge.ts` (or equivalent client bridge hook). Add a new `case` to the message handler `switch` statement.
2.  **Schema update:** Add description and parameters to `mcp-docs.json` in the app's repo, and copy it to `physbox_mcp/mcp-docs/` in this repo.
3.  **Server side:**
    *   **Python:** Register the new `@mcp.tool()` in [server.py](file:///wsl.localhost/Ubuntu-20.04/home/boab/expt_mcp/physbox_mcp/server.py).
    *   **Node.js:** Register the new tool in `server.mjs`.

---

## CI/CD Workflow & PyPI Releases

The project uses GitHub Actions to automate publishing releases to the Python Package Index (PyPI).

### Workflow Trigger
The CI/CD pipeline is defined in `.github/workflows/publish.yml` and is triggered whenever:
*   A new release is published in the GitHub repository.
*   A tag starting with `v` (e.g., `v2.0.0`) is pushed to the repository.

### Release Steps
1.  **Checkout & Setup:** The runner checks out the repository and sets up Python 3.12.
2.  **Build:** Builds the python package (source distribution and wheel binary) using the standard `build` package:
    ```bash
    python -m build
    ```
3.  **Publish:** Uploads the distributions in `dist/` directly to PyPI.

### Trusted Publishing (OIDC)
Publishing is configured using PyPI's **Trusted Publishers** (OpenID Connect). This eliminates the need to configure or store static PyPI API tokens in GitHub Secrets.

To set up Trusted Publishing:
1.  Log in to your PyPI account.
2.  Navigate to **Account Settings** > **Publishers** > **Add Publisher**.
3.  Choose **GitHub** and fill in the details:
    *   **GitHub Repository Owner:** (your GitHub organization/username)
    *   **Repository Name:** `physbox-mcp` (or your repository name)
    *   **Workflow name:** `publish.yml`
    *   **Environment name:** (optional, or leave blank if not using environment-gated deployments)
4.  Save the publisher. PyPI will now verify the cryptographic ID token supplied by GitHub Actions when publishing.
