"""Phase 6 contract tests — interactive 3D viewer, Production-phase only."""

import base64
import json
import re

import pytest

from design_engine import DesignEngine
from design_engine.production import SignOffRequired
from design_engine.viewer import tessellate

PLATE = {"name": "viewer-plate", "units": "mm", "density_kg_m3": 7850,
         "features": [{"op": "box", "x": 40, "y": 30, "z": 10},
                      {"op": "hole", "d": 8, "at": [0, 0]}]}

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("p6v") / "data")


@pytest.fixture(scope="module")
def signed_gid(eng):
    gid = eng.create_part(PLATE, reason="viewer test plate")["geometry_id"]
    run = eng.run_fea_static(gid, {
        "material": dict(S235),
        "mesh": {"max_size_mm": 6.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, 2000]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": 2.0},
    }, reason="validate before viewer")
    assert run["result"] == "pass"
    eng.sign_off(gid, "Gideon", "approve viewer-plate v1 for prototype release")
    return gid


def test_tessellation_is_a_closed_shell(eng):
    """The display mesh must be a watertight, correct-genus shell.

    OpenCascade triangulates each face independently, so vertices on shared
    face boundaries are duplicated per face; merge by position first. Then
    every edge must be shared by exactly two triangles (watertight), and
    Euler's formula must give V - E + F = 2 - 2g = 0 for this genus-1 solid
    (one through-hole). A tessellation that dropped or double-covered the
    bore would fail both checks.
    """
    mesh = tessellate(PLATE)
    assert mesh["triangles"] > 50

    pos = mesh["positions"]
    key_of, remap = {}, []
    for i in range(0, len(pos), 3):
        key = (pos[i], pos[i + 1], pos[i + 2])
        remap.append(key_of.setdefault(key, len(key_of)))

    tris = [tuple(remap[i] for i in mesh["indices"][t:t + 3])
            for t in range(0, len(mesh["indices"]), 3)]
    edges = {}
    for t in tris:
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            e = (min(a, b), max(a, b))
            edges[e] = edges.get(e, 0) + 1
    assert set(edges.values()) == {2}, "mesh is not watertight"
    V, E, F = len(key_of), len(edges), len(tris)
    assert V - E + F == 0, f"expected genus 1, got V-E+F={V - E + F}"


def test_viewer_requires_sign_off(eng):
    gid = eng.create_part({**PLATE, "name": "unsigned-plate"},
                          reason="unsigned")["geometry_id"]
    with pytest.raises(SignOffRequired, match="sign_off_missing"):
        eng.generate_viewer(gid, reason="premature viewer")
    row = eng.log.rows(action="generate_viewer", result="fail")[-1]
    assert "sign_off_missing" in row["failure_mode"]


def test_viewer_is_self_contained_and_log_sourced(eng, signed_gid):
    eng.generate_bom(signed_gid, {"quantity": 5, "lines": [
        {"ref": "screws", "kind": "catalog", "sku": "boltdepot-23106",
         "per_assembly": 4}]}, reason="BOM for viewer panel", budget_usd=10.0)
    out = eng.generate_viewer(signed_gid, reason="release review viewer")
    doc = open(out["viewer_path"], encoding="utf-8").read()

    # self-contained: nothing in the document loads from the network.
    # (The minified three.js body contains strings like `a.src=t`, so check
    # real markup attributes rather than the substring "src=".)
    for tag in re.findall(r"<(?:script|link|img|iframe)\b[^>]*>", doc,
                          re.IGNORECASE):
        assert not re.search(r'(src|href)\s*=', tag, re.IGNORECASE), tag
    assert not re.search(r'(href|src)\s*=\s*["\']https?://', doc)
    # NB: the vendored three.js body contains strings like "fetch(" and
    # "@import" inside FileLoader/shader code this viewer never calls, so a
    # substring scan cannot prove zero requests. That is verified empirically
    # by loading the page and reading the browser network log (see
    # docs/VIEWER_VERIFICATION.md); this test covers the markup contract.
    assert "THREE" in doc and "WebGLRenderer" in doc      # renderer inlined
    assert out["size_kb"] > 500                            # three.js is in there

    # the payload carries only log-sourced facts
    b64 = re.search(r'atob\("([A-Za-z0-9+/=]+)"\)', doc).group(1)
    payload = json.loads(base64.b64decode(b64))
    assert payload["geometry_id"] == signed_gid
    assert payload["sign_off"]["signed_off_by"] == "Gideon"
    assert payload["validation"]["limit_state"] == "yield_von_mises"
    assert payload["validation"]["material"]["source"].startswith("EN 10025-2")
    assert payload["sourcing"]["as_specified_total_usd"] == pytest.approx(4.20)
    assert payload["sourcing"]["budget_label"] == "within-budget"
    assert payload["properties"]["spec_digest"] == \
        payload["sign_off"]["spec_digest"]

    row = eng.log.rows(action="generate_viewer", result="pass")[-1]
    assert json.loads(row["details_json"])["triangles"] == \
        payload["mesh"]["triangles"]


def test_viewer_refuses_after_spec_tamper(eng, signed_gid):
    spec_path = (eng.parts.root / signed_gid.split("@")[0]
                 / f"v{signed_gid.split('@v')[1]}" / "spec.json")
    spec = json.loads(spec_path.read_text())
    spec["features"][0]["x"] = 41
    spec_path.write_text(json.dumps(spec, indent=2))
    try:
        with pytest.raises(SignOffRequired, match="sign_off_invalid"):
            eng.generate_viewer(signed_gid, reason="viewer after tamper")
    finally:
        spec["features"][0]["x"] = 40
        spec_path.write_text(json.dumps(spec, indent=2))
