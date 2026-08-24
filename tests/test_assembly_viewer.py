"""Assembly viewer — Production phase, gated on EVERY component."""

import base64
import json
import re

import pytest

from design_engine import DesignEngine
from design_engine.production import SignOffRequired

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}


def _case(sf=1.5):
    return {
        "material": dict(S235), "mesh": {"max_size_mm": 4.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, 400]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": sf},
    }


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("asmv") / "data")


@pytest.fixture(scope="module")
def built(eng):
    """Two parts, both validated; only the first is signed initially."""
    a = eng.create_part({"name": "plate-a", "units": "mm",
                         "density_kg_m3": 7850,
                         "features": [{"op": "box", "x": 30, "y": 20, "z": 60}]},
                        reason="assembly component A")["geometry_id"]
    b = eng.create_part({"name": "post-b", "units": "mm",
                         "density_kg_m3": 7850,
                         "features": [{"op": "cylinder", "d": 12, "h": 60}]},
                        reason="assembly component B")["geometry_id"]
    for gid in (a, b):
        assert eng.run_fea_static(gid, _case(), reason="validate component"
                                  )["result"] == "pass"
    asm = eng.create_assembly({
        "name": "two-part-rig", "units": "mm",
        "components": [{"geometry_id": a, "at": [0, 0, 0]},
                       {"geometry_id": b, "at": [40, 0, 0]}],
        "chains": [{"name": "gap", "requirement_mm": {"min": 0.05},
                    "terms": [
                        {"desc": "bore", "nominal": 12.2, "tol_plus": 0.1,
                         "tol_minus": 0.0, "sense": 1},
                        {"desc": "post", "nominal": 12.0, "tol_plus": 0.0,
                         "tol_minus": 0.02, "sense": -1}]}],
    }, reason="rig for assembly viewer test")["assembly_id"]
    eng.check_tolerance_stackup(asm)
    eng.sign_off(a, "Gideon", "approve plate-a for the rig")
    return {"a": a, "b": b, "asm": asm}


def test_one_unsigned_component_refuses_the_whole_render(eng, built):
    """The gate applies per component: a partial release renders nothing."""
    with pytest.raises(SignOffRequired, match="sign_off_missing") as exc:
        eng.generate_assembly_viewer(built["asm"], reason="B is not signed yet")
    assert built["b"] in str(exc.value)
    row = eng.log.rows(action="generate_assembly_viewer", result="fail")[-1]
    assert "sign_off_missing" in row["failure_mode"]


def test_assembly_viewer_renders_when_all_signed(eng, built):
    eng.sign_off(built["b"], "Gideon", "approve post-b for the rig")
    out = eng.generate_assembly_viewer(built["asm"],
                                       reason="release review of the rig")
    doc = open(out["viewer_path"], encoding="utf-8").read()

    # self-contained: no markup loads anything from the network
    for tag in re.findall(r"<(?:script|link|img|iframe)\b[^>]*>", doc, re.I):
        assert not re.search(r"(src|href)\s*=", tag, re.I), tag
    assert not re.search(r'(href|src)\s*=\s*["\']https?://', doc)
    assert out["size_kb"] > 500                      # three.js inlined

    b64 = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', doc).group(1)
    payload = json.loads(base64.b64decode(b64))
    assert payload["kind"] == "assembly"
    assert [c["geometry_id"] for c in payload["components"]] == [
        built["a"], built["b"]]
    assert [c["at"] for c in payload["components"]] == [[0, 0, 0], [40, 0, 0]]
    # every component carries its own signature, and the stackup comes along
    for c in payload["components"]:
        assert c["sign_off"]["signed_off_by"] == "Gideon"
        assert c["properties"]["spec_digest"] == c["sign_off"]["spec_digest"]
        assert c["mesh"]["triangles"] > 0
    assert payload["stackup"]["chains"][0]["name"] == "gap"

    row = eng.log.rows(action="generate_assembly_viewer", result="pass")[-1]
    det = json.loads(row["details_json"])
    assert det["triangles"] == sum(c["mesh"]["triangles"]
                                   for c in payload["components"])


def test_tampering_with_one_component_refuses_the_assembly(eng, built):
    gid = built["b"]
    spec_path = (eng.parts.root / gid.split("@")[0]
                 / f"v{gid.split('@v')[1]}" / "spec.json")
    spec = json.loads(spec_path.read_text())
    spec["features"][0]["d"] = 12.5           # silent post-sign-off change
    spec_path.write_text(json.dumps(spec, indent=2))
    try:
        with pytest.raises(SignOffRequired, match="sign_off_invalid"):
            eng.generate_assembly_viewer(built["asm"], reason="after tamper")
    finally:
        spec["features"][0]["d"] = 12
        spec_path.write_text(json.dumps(spec, indent=2))
    eng.generate_assembly_viewer(built["asm"], reason="after restore")
