"""
The agent-facing documentation, held to the tools it describes.

Every tool is registered as `@mcp.tool(description=get_doc(docs, "name", fallback))`,
so a tool whose documentation is missing does not fail — it quietly falls back to
the short inline string and keeps working. That is the right behaviour at runtime
and it is why nobody noticed the app copies and the bundled copies drifting apart:
seven of Etch's sheet and layer tools were documented here and never in Etch's own
`mcp-docs.json`, and an agent reading that file could not learn sheets exist.

These tests are the thing that was missing. They read the source rather than
importing the server, because importing it starts a websocket hub.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "physbox_mcp" / "server.py"
BUNDLED = ROOT / "physbox_mcp" / "mcp-docs"

# The docs variable each app's tools are registered against, and the file that
# variable is loaded from.
APPS = {
    "physics_docs": "physics",
    "process_docs": "process",
    "circuit_docs": "circuit",
    "etch_docs": "etch",
}

#: Every `get_doc(<var>, "<tool>", ...)` in the server, as (docs variable, tool name).
DOC_CALL = re.compile(r"get_doc\(\s*(\w+)\s*,\s*[\"']([\w.]+)[\"']")


def registered():
    """Which tools ask which docs file for their description."""
    source = SERVER.read_text(encoding="utf-8")
    found: dict[str, set[str]] = {}
    for var, tool in DOC_CALL.findall(source):
        found.setdefault(var, set()).add(tool)
    return found


def bundled(app_id: str) -> dict:
    path = BUNDLED / f"{app_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


@pytest.mark.parametrize("docs_var,app_id", sorted(APPS.items()))
def test_every_registered_tool_is_documented(docs_var, app_id):
    """
    A tool added without a docs entry silently ships its one-line fallback.

    That is a real loss and an invisible one: the tool works, so nothing
    complains, and the agent simply never learns what it is for.
    """
    tools = registered().get(docs_var, set())
    if not tools:
        pytest.skip(f"no tools registered against {docs_var}")
    documented = set(bundled(app_id).get("tools", {}))
    missing = sorted(tools - documented)
    assert not missing, (
        f"{app_id}: registered but undocumented in mcp-docs/{app_id}.json: {missing}. "
        f"Add an entry, or the agent gets the short inline fallback instead."
    )


@pytest.mark.parametrize("docs_var,app_id", sorted(APPS.items()))
def test_no_documentation_for_tools_that_do_not_exist(docs_var, app_id):
    """
    The other direction: docs for a tool nobody registers.

    Harmless at runtime and misleading to read — it describes a command that
    will not answer.
    """
    tools = registered().get(docs_var, set())
    if not tools:
        pytest.skip(f"no tools registered against {docs_var}")
    documented = set(bundled(app_id).get("tools", {}))
    # `list_sessions` and `use_session` are documented under physics but are
    # global tools with no app prefix; they are registered, just not per-app.
    stray = sorted(documented - tools)
    assert not stray, (
        f"{app_id}: documented but never registered: {stray}."
    )


@pytest.mark.parametrize(
    "app_id,repo",
    # The MCP's app id is not always the repo's name: Mesh's tools are all
    # `physics_*` and Volt's are all `circuit_*`.
    [("etch", "etch"), ("physics", "mesh"), ("circuit", "volt"), ("process", "process")],
)
def test_bundled_copy_matches_the_app_when_it_is_checked_out(app_id, repo):
    """
    The bundled copy is a fallback for an installed server with no app beside
    it. When the app *is* checked out, the two must agree — otherwise whichever
    one a reader happens to open is a coin flip, which is exactly how seven
    tools ended up documented in one file and not the other.
    """
    app_file = Path.home() / repo / "mcp-docs.json"
    if not app_file.exists():
        pytest.skip(f"{repo} is not checked out beside this server")

    app = json.loads(app_file.read_text(encoding="utf-8"))
    ours = bundled(app_id)
    app_tools = set(app.get("tools", {}))
    our_tools = set(ours.get("tools", {}))

    assert not our_tools - app_tools, (
        f"{app_id}: bundled docs describe tools the app's own file does not: "
        f"{sorted(our_tools - app_tools)}"
    )
    assert not app_tools - our_tools, (
        f"{app_id}: the app documents tools the bundled copy does not: "
        f"{sorted(app_tools - our_tools)}"
    )
