"""
Agent-facing documentation loading and hierarchical reference lookup.

Pure logic (no sockets or FastMCP instance), so `tests/test_docs.py` can import
and exercise `get_doc`, `get_reference_docs`, and `get_reference_section`
directly without starting the WebSocket hub.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Where an app's own docs live, when the app is checked out beside us. The key
# is the MCP's app id; the value is the repo directory, which is not always the
# same word — Mesh's tools are all `physics_*`.
APP_REPO_DIR = {"physics": "mesh", "circuit": "volt"}


def load_mcp_docs(app_id: str, current_dir: Path | None = None) -> dict:
    """
    An app's agent-facing documentation, preferring the app's own copy.

    The bundled copy under `mcp-docs/` is a *fallback* for an installed server
    with no app checkout, and it is last for that reason. It used to sit second,
    ahead of the app's own file, which meant a developer editing the app's
    `mcp-docs.json` — the thing both apps' CLAUDE.md tells them to edit — was
    editing a file the server then ignored. The two drifted for months and
    nothing said so, because a tool whose docs are missing falls back to the
    inline description in `get_doc` and keeps working.
    """
    if current_dir is None:
        current_dir = Path(__file__).parent.resolve()
    repo = APP_REPO_DIR.get(app_id, app_id)
    possible_paths: list[Path] = []
    if ".claude/worktrees/" in current_dir.as_posix():
        wt_name = current_dir.parent.name
        possible_paths.append(
            Path.home() / repo / ".claude" / "worktrees" / wt_name / "mcp-docs.json"
        )
    possible_paths.extend(
        [
            current_dir / ".." / ".." / repo / "mcp-docs.json",
            Path.home() / repo / "mcp-docs.json",
            current_dir / "mcp-docs" / f"{app_id}.json",
        ]
    )
    for path in possible_paths:
        try:
            if path.exists():
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            print(f"Error reading docs for {app_id} from {path}: {e}", file=sys.stderr)
    return {}


def get_reference_docs(docs: dict) -> dict:
    """Everything in an app's mcp-docs.json EXCEPT `tools` (each tool's text is
    already surfaced via its own @mcp.tool description, so repeating it here
    would just be dead weight). This is what *_get_schema returns.

    `schema`'s own fields (geomTypes, nodeFields, etc.) are kept flattened at
    the top level, matching the pre-existing response shape exactly (callers
    already do e.g. result["geomTypes"]) — the other doc sections are added
    as sibling keys alongside them, so this is purely additive.
    """
    schema_fields = docs.get("schema", {})
    extra_sections = {k: v for k, v in docs.items() if k not in ("tools", "schema")}
    return {**schema_fields, **extra_sections}


def get_reference_section(docs: dict, section: str | None) -> dict:
    """One part of get_reference_docs (or a tool's detail subsection), or an index.

    Mesh's reference runs to about 75 KB, more than an agent can read in one
    tool result, so the bare call returns the overview, workflow, coreGotchas,
    a list of every section with its size, and a map of available dot-path
    subsections. `section` fetches a top-level section, a dot-path subsection
    (e.g. 'gotchas.collision', 'nodeFields.features', 'tools.physics_cut.hardware',
    or 'physics_cut.hardware'), '<section>.all', or 'all'.
    """
    full = get_reference_docs(docs)
    tools = docs.get("tools", {})
    section = section.strip() if isinstance(section, str) and section.strip() else None

    if section == "all":
        return full

    if not section:
        sizes = {k: len(json.dumps(v, ensure_ascii=False)) for k, v in full.items()}
        subsections: dict[str, list[str]] = {}
        for k in sorted(full):
            v = full[k]
            if isinstance(v, dict) and v:
                subsections[k] = list(v.keys())
        for t in sorted(tools):
            entry = tools[t]
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("details"), dict)
                and entry["details"]
            ):
                subsections[f"tools.{t}"] = list(entry["details"].keys())
        result: dict[str, Any] = {
            "overview": full.get("overview"),
            "workflow": full.get("workflow"),
        }
        if "coreGotchas" in full:
            result["coreGotchas"] = full["coreGotchas"]
        elif isinstance(full.get("gotchas"), dict) and "core" in full["gotchas"]:
            result["coreGotchas"] = full["gotchas"]["core"]
        result["sections"] = dict(sorted(sizes.items()))
        if subsections:
            result["subsections"] = subsections
        result["hint"] = (
            "Call again with section=<name> or a dotted path like "
            "section='gotchas.collision', section='nodeFields.features', or "
            "section='tools.physics_cut.hardware' (pass '<name>.all' for a whole "
            "multi-part section, or section='all' for everything)."
        )
        return result

    normalized = (
        f"tools.{section}"
        if (section in tools or section.split(".", 1)[0] in tools)
        else section
    )
    if normalized in ("tools", "tools.all"):
        if normalized == "tools.all":
            return {"tools": tools}
        return {
            "tools": {
                t: {
                    k: len(json.dumps(v, ensure_ascii=False))
                    for k, v in entry["details"].items()
                }
                for t, entry in sorted(tools.items())
                if isinstance(entry, dict)
                and isinstance(entry.get("details"), dict)
                and entry["details"]
            },
            "hint": (
                "Call with section='tools.<tool>.<subtopic>' for one subtopic, "
                "or section='tools.<tool>' for a tool's overview."
            ),
        }

    if normalized.startswith("tools."):
        rest = normalized[len("tools.") :]
        tool_name, subtopic = (rest.split(".", 1) + [None])[:2]
        if tool_name not in tools:
            return {
                "error": f"No tool {tool_name!r}.",
                "toolsWithDetails": sorted(
                    t
                    for t, v in tools.items()
                    if isinstance(v, dict) and v.get("details")
                ),
            }
        entry = tools[tool_name]
        if isinstance(entry, str) or subtopic == "all":
            return {f"tools.{tool_name}": entry}
        details = entry.get("details") if isinstance(entry, dict) else None
        if not isinstance(details, dict) or not details:
            return {f"tools.{tool_name}": entry}
        if subtopic is None:
            if len(json.dumps(entry, ensure_ascii=False)) > 4500:
                return {
                    f"tools.{tool_name}.summary": entry.get("summary"),
                    "subsections": {
                        f"tools.{tool_name}.{k}": len(json.dumps(v, ensure_ascii=False))
                        for k, v in details.items()
                    },
                    "hint": (
                        f"Call with section='tools.{tool_name}.<subtopic>' for one "
                        f"subtopic, or section='tools.{tool_name}.all' for everything."
                    ),
                }
            return {f"tools.{tool_name}": entry}
        if subtopic in details:
            return {f"tools.{tool_name}.{subtopic}": details[subtopic]}
        return {
            "error": f"No subsection {subtopic!r} under tools.{tool_name}.",
            "subsections": [f"tools.{tool_name}.{k}" for k in details],
        }

    if section in full:
        val = full[section]
        if (
            isinstance(val, dict)
            and val
            and len(json.dumps(val, ensure_ascii=False)) > 5500
        ):
            items = list(val.items())
            first_key, first_val = items[0]
            sub_sizes = {
                f"{section}.{k}": len(json.dumps(v, ensure_ascii=False))
                for k, v in items[1:]
            }
            return {
                f"{section}.{first_key}": first_val,
                "subsections": sub_sizes,
                "hint": (
                    f"'{section}.{first_key}' is shown above. Call with "
                    f"section='{section}.<subtopic>' for another topic, or "
                    f"section='{section}.all' for the whole section."
                ),
            }
        return {section: val}

    if "." in section:
        top, rest = section.split(".", 1)
        parent = full.get(top)
        if isinstance(parent, dict):
            if rest == "all":
                return {top: parent}
            if rest in parent:
                return {section: parent[rest]}
            cur: Any = parent
            for part in rest.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            if cur is not None:
                return {section: cur}
            for sub_val in parent.values():
                if isinstance(sub_val, dict) and rest in sub_val:
                    return {section: sub_val[rest]}
            return {
                "error": f"No subsection {rest!r} under {top!r}.",
                "subsections": [f"{top}.{k}" for k in parent],
            }

    return {"error": f"No section {section!r}.", "sections": sorted(full)}


def get_doc(docs: dict, tool_name: str, fallback: str = "") -> str:
    """Extract tool description string from app documentation with fallback.

    Supports both flat string descriptions and hierarchical dicts of the form
    `{"summary": "...", "details": {"subtopic": "..."}}`.
    """
    entry = docs.get("tools", {}).get(tool_name)
    if entry is None:
        return fallback
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        summary = entry.get("summary") or fallback
        details = entry.get("details")
        if isinstance(details, dict) and details:
            subtopics = ", ".join(f'"tools.{tool_name}.{k}"' for k in details)
            return (
                f"{summary}\n\n"
                f"More detail via physics_get_schema(section=...): {subtopics}."
            )
        return summary
    return fallback
