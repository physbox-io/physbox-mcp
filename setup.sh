#!/usr/bin/env bash
# Sets up the Python environment for PhysBox: MCP.
set -e
cd "$(dirname "$0")"

echo "=== Creating Python venv (requires Python 3.12+) ==="
rm -rf venv
python3.12 -m venv venv
source venv/bin/activate

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt
pip install -e .

deactivate
echo ""
echo "Setup complete."
