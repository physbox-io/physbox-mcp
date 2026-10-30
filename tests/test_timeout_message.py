"""
A command that timed out may still have run.

The app is handed the command before the wait starts, and it carries on when
this side gives up: a slow build lands seconds later. A bare "Timeout waiting
for ..." read as "nothing happened", and an agent retrying it built the scene
twice. These tests hold the message to saying otherwise, and every app-command
wait to using it.

Read from the source rather than imported, because importing the server starts
a websocket hub (see test_docs.py).
"""

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "physbox_mcp" / "server.py"
BUNDLED_PHYSICS = ROOT / "physbox_mcp" / "mcp-docs" / "physics.json"


def _tree():
    return ast.parse(SERVER.read_text(encoding="utf-8"))


def _timeout_error_fn():
    tree = _tree()
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "command_timeout_error")
    ns: dict = {}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SERVER), "exec"), ns)
    return ns["command_timeout_error"]


def test_the_message_says_the_command_may_have_run_and_to_check_first():
    msg = str(_timeout_error_fn()("BUILD_SCENE", 60.0))
    assert 'Timeout waiting for "BUILD_SCENE" response (60.0s)' in msg
    assert "MAY STILL HAVE RUN" in msg
    assert "still be running" in msg
    assert "Do not simply retry" in msg
    assert "physics_get_scene_summary" in msg


def test_every_app_command_wait_reports_it():
    """AppConnection.send waits in two places, primary and peer; both must say it."""
    tree = _tree()
    conn = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "AppConnection")
    send = next(n for n in conn.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "send")
    handlers = [
        h for h in ast.walk(send)
        if isinstance(h, ast.ExceptHandler) and h.type is not None and "TimeoutError" in ast.dump(h.type)
    ]
    assert len(handlers) == 2
    for h in handlers:
        raised = [r for r in ast.walk(h) if isinstance(r, ast.Raise)]
        assert raised and all(r.exc is not None and "command_timeout_error" in ast.dump(r.exc) for r in raised)


def test_the_bundled_physics_docs_tell_agents_the_same():
    docs = json.loads(BUNDLED_PHYSICS.read_text(encoding="utf-8"))
    assert any("timed out" in g.lower() and "may still have run" in g.lower() for g in docs["gotchas"])
