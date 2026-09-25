"""
How many tools there are, held against the number the website advertises.

physbox.io says the count in twenty-odd places — the navigation drawer of every
page, the meta description, the MCP ribbon, the comparison table and a sentence
that breaks it down. Nothing ever checked it, so it went stale twice: 81 while
the server was on 151, then 151 while the server was on 220. Each time the gap
was most of the number.

A count cannot be derived on the website's side — the server is not there. So it
is derived here, where the tools are, and the figures the site prints live in
this file next to it. Adding a tool now fails CI until the site is updated,
which is the only moment anyone is in a position to do it.

Update both together: the numbers below, and `~/physbox_static` (`grep -rn
'<the old total>' *.html`).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "physbox_mcp" / "server.py"

#: Total tools the server exposes — "229 Native MCP Tools" on every page.
SITE_TOTAL = 231

#: The breakdown in the sentence under "229 Tools. One pip install."
SITE_SIMULATOR = 217  # etch, circuit, physics and process, less the two below
SITE_CLOUD = 10  # the run archive and the account: physbox_*
SITE_CROSS_APP = 4  # detect_apps, send_command, list_sessions, use_session

#: The four that are about the relay rather than about one simulator. Two of
#: them are filed in physics.json for historical reasons, so they are named
#: here rather than inferred from a prefix.
CROSS_APP = {"detect_apps", "send_command", "list_sessions", "use_session"}

TOOL = re.compile(r"^\s*@mcp\.tool\b", re.M)


def tool_names() -> list[str]:
    """Every `@mcp.tool`-decorated function, in source order."""
    lines = SERVER.read_text(encoding="utf-8").splitlines()
    names = []
    for i, line in enumerate(lines):
        if not line.lstrip().startswith("@mcp.tool"):
            continue
        for candidate in lines[i + 1 : i + 30]:
            match = re.match(r"\s*(?:async )?def (\w+)", candidate)
            if match:
                names.append(match.group(1))
                break
    return names


def test_every_tool_has_a_distinct_name():
    names = tool_names()
    assert len(names) == len(set(names)), "a tool name is registered twice"


def test_total_matches_the_website():
    names = tool_names()
    assert len(names) == SITE_TOTAL, (
        f"the server exposes {len(names)} tools and physbox.io says {SITE_TOTAL}. "
        "Update SITE_* here and the numbers in physbox_static/*.html together."
    )


def test_breakdown_matches_the_website():
    names = set(tool_names())
    cloud = {n for n in names if n.startswith("physbox_")}
    simulator = names - cloud - CROSS_APP

    assert len(simulator) == SITE_SIMULATOR
    assert len(cloud) == SITE_CLOUD
    assert CROSS_APP <= names
    assert len(CROSS_APP) == SITE_CROSS_APP
    assert SITE_SIMULATOR + SITE_CLOUD + SITE_CROSS_APP == SITE_TOTAL
