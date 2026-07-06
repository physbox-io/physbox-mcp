#!/usr/bin/env bash
# Sets up both the Node.js and Python environments for the MCP server.
# Run once from ~/expt_mcp/
set -e
cd "$(dirname "$0")"

echo "=== Installing Node.js dependencies ==="
npm install

echo ""
echo "=== Creating Python venv (requires Python 3.12+) ==="
python3.12 -m venv venv
source venv/bin/activate

echo "=== Installing Python dependencies ==="
pip install --upgrade pip
pip install -r requirements.txt

deactivate
echo ""
echo "Setup complete."
echo ""
echo "── How to use ─────────────────────────────────────────────────────────────"
echo ""
echo "1. Start each app's Vite dev server (each has the MCP bridge plugin built in):"
echo "     cd ~/process  && npm run dev   # → http://localhost:5173"
echo "     cd ~/circuit/frontend && npm run dev   # → http://localhost:5174"
echo "     cd ~/physics  && npm run dev   # → http://localhost:5175"
echo ""
echo "2. Open each app in a browser (the WebSocket bridge connects when the page loads)."
echo ""
echo "3. Run the MCP server (pick one):"
echo "   Node.js:  node ~/expt_mcp/server.mjs"
echo '   Python:   ~/expt_mcp/venv/bin/python ~/expt_mcp/physbox_mcp/server.py'
echo ""
echo "── Claude Code MCP config ──────────────────────────────────────────────────"
echo '  Node.js: { "command": "node", "args": ["'$(realpath server.mjs)'"] }'
echo '  Python:  { "command": "'$(realpath venv/bin/python)'", "args": ["'$(realpath physbox_mcp/server.py)'"] }'
