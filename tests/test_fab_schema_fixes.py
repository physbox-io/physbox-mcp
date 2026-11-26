"""
Regression tests for server.py & mcp-docs/physics.json fabrication/schema audit fixes.

Extracts functions from server.py via AST (like test_heartbeat.py and test_timeout_message.py)
so importing does not start a background WebSocket hub.
"""

import ast
import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "physbox_mcp" / "server.py"
BUNDLED_PHYSICS = ROOT / "physbox_mcp" / "mcp-docs" / "physics.json"


def _load_server_fns(*names: str, conn_result: Any = None, is_primary: bool = True):
    """Compile selected functions from server.py with a stubbed get_conn."""
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    sent: list[tuple[str, dict | None, float]] = []

    class FakeConn:
        async def send(self, cmd: str, payload: dict | None = None, timeout: float = 10.0):
            sent.append((cmd, payload, timeout))
            return conn_result if conn_result is not None else {"ok": True}

    fake_conn = FakeConn()
    wanted = set(names) | {"compact_dict", "_deliver_export"}
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            # Strip @mcp.tool(...) decorators so we get the raw async function
            copy = ast.AsyncFunctionDef(**{**node.__dict__, "decorator_list": []}) if isinstance(node, ast.AsyncFunctionDef) else ast.FunctionDef(**{**node.__dict__, "decorator_list": []})
            body.append(copy)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if getattr(t, "id", None) in {"WS_MAX_MESSAGE", "_IMAGE_TYPES"}:
                    body.append(node)

    ns: dict[str, Any] = {
        "Any": Any,
        "Path": Path,
        "base64": base64,
        "Ph": 5175,
        "Et": 5176,
        "is_primary": is_primary,
        "get_conn": lambda _port: fake_conn,
        "_local_path": lambda p: Path(p),
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(SERVER), "exec"), ns)
    return ns, sent


def test_deliver_export_preserves_molded_and_options_from():
    ns, _ = _load_server_fns(
        "physics_export_cast",
        "physics_export_relief",
        conn_result={
            "ok": True,
            "molded": {"label": "bracket", "ids": ["bracket_1"]},
            "optionsFrom": "the Relief Carve dialog",
            "summary": {"lines": 10},
            "warnings": [],
            "files": [{"name": "out.stl", "bytes": 128}],
        },
    )
    cast_res = asyncio.run(ns["physics_export_cast"](parting_from_base_mm="auto"))
    assert cast_res["molded"] == {"label": "bracket", "ids": ["bracket_1"]}

    relief_res = asyncio.run(ns["physics_export_relief"]())
    assert relief_res["optionsFrom"] == "the Relief Carve dialog"


def test_widened_annotations_and_payload_mappings():
    ns, sent = _load_server_fns(
        "physics_export_cast",
        "physics_set_cast_prep",
        "physics_add_hardware",
        "physics_export_machining",
        "physics_add_simulation",
        "physics_set_impact",
        "physics_carve_scene",
        "physics_timber_breakdown",
        "physics_machine_frame_job",
    )

    # 1. parting_from_base_mm accepts 'auto' (float | str | None)
    assert "str" in str(ns["physics_export_cast"].__annotations__["parting_from_base_mm"])
    assert "str" in str(ns["physics_set_cast_prep"].__annotations__["parting_from_base_mm"])
    asyncio.run(ns["physics_set_cast_prep"](parting_from_base_mm="auto"))
    assert sent[-1][1]["partingFromBaseMm"] == "auto"

    # 2. physics_add_hardware widens size and converts numeric sizes to str
    assert "int" in str(ns["physics_add_hardware"].__annotations__["size"])
    assert "float" in str(ns["physics_add_hardware"].__annotations__["size"])
    asyncio.run(ns["physics_add_hardware"](kind="bearing", size=608))
    assert sent[-1][1]["size"] == "608"

    # 3. setups accepts list[str] in physics_export_machining & physics_add_simulation
    assert "list[str]" in str(ns["physics_export_machining"].__annotations__["setups"])
    assert "list[str]" in str(ns["physics_add_simulation"].__annotations__["setups"])
    asyncio.run(ns["physics_export_machining"](setups=["+z", "+x"]))
    assert sent[-1][1]["setups"] == ["+z", "+x"]

    # 4. physics_set_impact supports clear_pierce -> pierceImpulseNs: None
    asyncio.run(ns["physics_set_impact"](id="plate", clear_pierce=True))
    assert "pierceImpulseNs" in sent[-1][1] and sent[-1][1]["pierceImpulseNs"] is None

    # 5. physics_add_simulation forwards relief/laser/metal pass-through params
    asyncio.run(ns["physics_add_simulation"](
        kind="relief",
        roughing_strategy="adaptive",
        roughing_enabled=False,
        vertical_exaggeration=1.5,
        joint_mode="slot",
        add_gating=False,
        cope_drag_height_mm=120.0,
    ))
    sim_payload = sent[-1][1]
    assert sim_payload["roughingStrategy"] == "adaptive"
    assert sim_payload["roughingEnabled"] is False
    assert sim_payload["verticalExaggeration"] == 1.5
    assert sim_payload["jointMode"] == "slot"
    assert sim_payload["addGating"] is False
    assert sim_payload["copeDragHeightMm"] == 120.0

    # 6. physics_carve_scene accepts side str/int + setup, clearanceMm, toolStickoutMm, finishingToolType
    assert "str" in str(ns["physics_carve_scene"].__annotations__["side"])
    asyncio.run(ns["physics_carve_scene"](
        side="A",
        setup=1,
        clearanceMm=60.0,
        toolStickoutMm=25.0,
        finishingToolType="ball_nose",
    ))
    carve_payload = sent[-1][1]
    assert carve_payload["side"] == "A"
    assert carve_payload["setup"] == 1
    assert carve_payload["clearanceMm"] == 60.0
    assert carve_payload["toolStickoutMm"] == 25.0
    assert carve_payload["finishingToolType"] == "ball_nose"

    # 7. physics_timber_breakdown forwards stop_margin_mm
    asyncio.run(ns["physics_timber_breakdown"](stop_margin_mm=15.0))
    assert sent[-1][1]["stopMarginMm"] == 15.0

    # 8. physics_machine_frame_job forwards bounds
    asyncio.run(ns["physics_machine_frame_job"](bounds={"minX": 0, "minY": 0, "maxX": 100, "maxY": 80}))
    assert sent[-1][1]["bounds"] == {"minX": 0, "minY": 0, "maxX": 100, "maxY": 80}


def test_import_profile_secondary_peer_ws_max_message_guard(tmp_path: Path):
    ns, _ = _load_server_fns("physics_import_profile", is_primary=False)
    big_svg = tmp_path / "huge.svg"
    # Exceed WS_MAX_MESSAGE - 64 KiB
    big_svg.write_bytes(b"<svg>" + b"a" * (ns["WS_MAX_MESSAGE"]) + b"</svg>")
    with pytest.raises(RuntimeError, match="limit for commands relayed from a secondary MCP session"):
        asyncio.run(ns["physics_import_profile"](path=str(big_svg)))


def test_physics_docs_parameter_parity_and_contradiction_fixes():
    docs = json.loads(BUNDLED_PHYSICS.read_text(encoding="utf-8"))
    tools = docs["tools"]

    # Snake-case + clear_* docs
    assert "weld_to" in tools["physics_set_constraint"]
    assert "clear_weld" in tools["physics_set_constraint"]
    assert "clear_connect" in tools["physics_set_constraint"]
    assert "clear_break" in tools["physics_set_constraint"]
    assert "`weldTo`" not in tools["physics_set_constraint"]

    assert "crumple_torque_nm" in tools["physics_set_crumple"]
    assert "clear: true" in tools["physics_set_crumple"]
    assert "`crumpleTorqueNm`" not in tools["physics_set_crumple"]

    impact_blob = json.dumps(tools["physics_set_impact"])
    for key in ("shatter_impulse_ns", "dent_yield_ns", "pierce_impulse_ns", "clear_shatter", "clear_dent", "clear_pierce"):
        assert key in impact_blob
    assert "shatterImpulseNs" not in impact_blob
    assert "dentYieldNs" not in impact_blob

    relief_blob = json.dumps(tools["physics_export_relief"])
    for key in ("roughing_tool_dia_mm", "finishing_tool_dia_mm", "finishing_tool_type", "carve_depth_mm", "roughing_strategy", "roughing_enabled"):
        assert key in relief_blob
    assert "roughingToolDiaMm" not in relief_blob

    mach_blob = json.dumps(tools["physics_export_machining"])
    assert "tool_dia_mm" in mach_blob and "finishing_tool_type" in mach_blob
    assert "toolDiaMm" not in mach_blob

    cast_blob = json.dumps(tools["physics_export_cast"])
    assert "parting_from_base_mm" in cast_blob
    assert "partingFromBaseMm" not in cast_blob

    # Batch params documented
    assert "`cuts:" in tools["physics_cut"]["summary"]
    assert "`roundings:" in tools["physics_round_edges"]["summary"]
    assert "`patterns:" in tools["physics_apply_pattern"]["summary"]

    # Headless 30s -> 45s contradiction resolved
    headless_blob = json.dumps(tools["physics_run_headless"])
    assert "waits up to 30 s" not in headless_blob
    assert "waits up to 45 s" in headless_blob

    # Low-severity doc omissions fixed
    assert "`name`" in tools["physics_set_reference_image"]["summary"]
    assert "`valueMm`" in tools["physics_lattice_dimension"]
    state_blob = json.dumps(tools["physics_get_state"])
    for key in ("gravityZ", "windX", "windY", "floorFriction", "floorBounce"):
        assert key in state_blob


def test_new_fab_and_flexure_params_and_docs():
    ns, sent = _load_server_fns(
        "physics_split_for_print",
        "physics_export_cut",
        "physics_check_dfm",
        "physics_cut",
    )

    asyncio.run(ns["physics_split_for_print"](body_id="hull", name="HullSection", apply=True))
    assert sent[-1][1]["name"] == "HullSection"

    asyncio.run(ns["physics_export_cut"](kind="panels", master_id="lid", include_children=False))
    assert sent[-1][1]["includeChildren"] is False

    asyncio.run(ns["physics_check_dfm"](process="print", master_id="a,b", bed_mm=[250, 250, 250], include_children=False))
    assert sent[-1][1]["bedMm"] == [250, 250, 250]
    assert sent[-1][1]["includeChildren"] is False

    asyncio.run(ns["physics_cut"](id="plate", shape="countersink", size="M4", angleDeg=82))
    assert sent[-1][1]["angleDeg"] == 82

    docs = json.loads(BUNDLED_PHYSICS.read_text(encoding="utf-8"))
    tools = docs["tools"]
    assert "`name`" in json.dumps(tools["physics_split_for_print"])
    assert "include_children" in json.dumps(tools["physics_export_cut"])
    dfm_blob = json.dumps(tools["physics_check_dfm"])
    for key in ("bed_mm", "include_children", "bronze", "aluminium", "pewter", "zinc", "iron", "air-gap"):
        assert key in dfm_blob
    assert "countersink" in json.dumps(tools["physics_cut"])
    assert "angleDeg" in json.dumps(tools["physics_cut"])
    hw_blob = json.dumps(tools["physics_add_hardware"])
    for key in ("butt_hinge", "hasp_latch", "handle", "wood_screw", "corner_bracket"):
        assert key in hw_blob
    mech_blob = json.dumps(tools["physics_build_mechanism"])
    for key in ("compliant_gripper", "parallel_flexure", "cantilever_pawl", "monolithicPrint"):
        assert key in mech_blob
    assert "flexure" in docs["schema"]["nodeFields"]["features"]
