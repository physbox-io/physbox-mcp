"""
A command is waited on for as long as its app says it is still working.

Apps send a WORKING beat every 2 s while a command runs, and the hub's timeout
counts silence, not total time: a Mesh scene of cloth took half a minute to
load and every load was reported as timed out while it ran. Without beats the
wait is what it always was.

Read from the source rather than imported, because importing the server starts
a websocket hub (see test_docs.py).
"""

import ast
import asyncio
import time
from pathlib import Path
from typing import Any

SERVER = Path(__file__).resolve().parent.parent / "physbox_mcp" / "server.py"
NAMES = {"_alive", "MAX_WAIT", "note_alive", "await_reply", "command_timeout_error"}


def _ns():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    body = [n for n in tree.body if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in NAMES)
            or (isinstance(n, (ast.Assign, ast.AnnAssign)) and any(getattr(t, "id", None) in NAMES for t in (n.targets if isinstance(n, ast.Assign) else [n.target])))]
    ns: dict = {"asyncio": asyncio, "time": time, "Any": Any}
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVER), "exec"), ns)
    return ns


def test_beats_keep_a_long_command_waited_on():
    ns = _ns()

    async def run():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        async def app():
            for _ in range(6):
                await asyncio.sleep(0.25)
                ns["note_alive"]("a")
            fut.set_result("done")

        asyncio.create_task(app())
        return await ns["await_reply"](fut, "a", "LOAD_PRESET", 0.6)

    assert asyncio.run(run()) == "done"
    assert ns["_alive"] == {}


def test_silence_still_times_out_at_the_timeout():
    ns = _ns()

    async def run():
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        loop.call_later(2.0, lambda: fut.done() or fut.set_result("late"))
        t0 = time.monotonic()
        try:
            await ns["await_reply"](fut, "b", "SCREENSHOT", 0.5)
        except RuntimeError as e:
            return str(e), time.monotonic() - t0
        return "no timeout", 0.0

    msg, took = asyncio.run(run())
    assert 'Timeout waiting for "SCREENSHOT"' in msg
    assert took < 1.0
