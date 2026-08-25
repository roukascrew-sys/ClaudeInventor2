"""Phase 1 contract tests.

Numeric assertions are against hand-computed analytic values:
- box 20x30x10 with a d=5 through hole: V = 6000 - pi*2.5^2*10 = 5803.65046 mm^3
- edit y 30->40: V = 8000 - 196.34954 = 7803.65046 mm^3
- stackup bore 10.0 +0.1/-0.1 (+) vs pin 9.8 +/-0.05 (-):
  nominal 0.2, worst-case [0.05, 0.35], RSS half-width sqrt(0.1^2+0.05^2)=0.1118034
"""

import math

import pytest

from design_engine import DesignEngine, PartNotFound, SpecError

BOX_SPEC = {
    "name": "test-plate",
    "units": "mm",
    "density_kg_m3": 7850,
    "features": [
        {"op": "box", "x": 20, "y": 30, "z": 10},
        {"op": "hole", "d": 5, "at": [0, 0]},
    ],
}

HOLE_VOL = math.pi * 2.5**2 * 10  # 196.3495408...


@pytest.fixture()
def eng(tmp_path):
    return DesignEngine(tmp_path / "data")


def test_create_part_geometry_and_log(eng):
    out = eng.create_part(BOX_SPEC, reason="contract test: initial plate")
    assert out["geometry_id"] == "P0001@v1"
    vol = out["properties"]["volume_mm3"]
    assert vol == pytest.approx(6000 - HOLE_VOL, rel=1e-6)
    # mass estimate: V * 1e-9 m^3 * 7850 kg/m^3
    assert out["properties"]["mass_kg_estimate"] == pytest.approx(
        (6000 - HOLE_VOL) * 1e-9 * 7850, rel=1e-6)
    with open(out["step_file_path"], encoding="utf-8", errors="replace") as fh:
        assert "ISO-10303" in fh.read(100)
    rows = eng.log.rows(action="create_part")
    assert len(rows) == 1
    assert rows[0]["result"] == "pass"
    assert rows[0]["geometry_version"] == "P0001@v1"
    assert rows[0]["reason"] == "contract test: initial plate"


def test_reason_is_mandatory_and_refusal_is_logged(eng):
    with pytest.raises(ValueError, match="reason"):
        eng.create_part(BOX_SPEC, reason="   ")
    rows = eng.log.rows(action="create_part", result="fail")
    assert len(rows) == 1
    assert "reason" in rows[0]["failure_mode"]


def test_invalid_spec_fails_loud_and_logged(eng):
    bad = {"name": "bad", "units": "mm",
           "features": [{"op": "teleport", "x": 1}]}
    with pytest.raises(SpecError, match="unknown op"):
        eng.create_part(bad, reason="contract test: bad op")
    assert eng.log.rows(action="create_part", result="fail")


def test_edit_part_diff_lineage_and_volume(eng):
    created = eng.create_part(BOX_SPEC, reason="base for edit test")
    out = eng.edit_part(created["geometry_id"], {"features.0.y": 40},
                        reason="widen plate for clearance")
    assert out["new_geometry_id"] == "P0001@v2"
    assert out["properties"]["volume_mm3"] == pytest.approx(
        8000 - HOLE_VOL, rel=1e-6)
    assert out["diff"] == [{"path": "features.0.y", "old": 30, "new": 40}]
    # v1 untouched on disk
    assert eng.get_part("P0001@v1")["spec"]["features"][0]["y"] == 30
    # log lineage: edit row links to the create row
    create_row = eng.log.rows(action="create_part", result="pass")[0]
    edit_row = eng.log.rows(action="edit_part", result="pass")[0]
    assert edit_row["linked_parent_id"] == create_row["id"]


def test_edit_rejects_noop_and_bad_path(eng):
    gid = eng.create_part(BOX_SPEC, reason="base")["geometry_id"]
    with pytest.raises(ValueError, match="identical"):
        eng.edit_part(gid, {"features.0.y": 30}, reason="noop should fail")
    with pytest.raises(SpecError, match="not found"):
        eng.edit_part(gid, {"features.9.y": 40}, reason="bad path")
    with pytest.raises(PartNotFound):
        eng.edit_part("P9999@v1", {"features.0.y": 40}, reason="missing part")


def _assembly_spec(gid, req):
    return {
        "name": "pin-in-bore",
        "units": "mm",
        "components": [{"geometry_id": gid, "at": [0, 0, 0]}],
        "chains": [{
            "name": "axial-clearance",
            "requirement_mm": req,
            "terms": [
                {"desc": "bore depth", "nominal": 10.0,
                 "tol_plus": 0.10, "tol_minus": 0.10, "sense": 1},
                {"desc": "pin length", "nominal": 9.8,
                 "tol_plus": 0.05, "tol_minus": 0.05, "sense": -1},
            ],
        }],
    }


def test_stackup_worst_case_and_rss(eng):
    gid = eng.create_part(BOX_SPEC, reason="stackup component")["geometry_id"]
    aid = eng.create_assembly(_assembly_spec(gid, {"min": 0.05}),
                              reason="stackup test rig")["assembly_id"]
    out = eng.check_tolerance_stackup(aid)
    chain = out["report"]["chains"][0]
    assert chain["nominal_mm"] == pytest.approx(0.2)
    assert chain["worst_case_mm"]["min"] == pytest.approx(0.05)
    assert chain["worst_case_mm"]["max"] == pytest.approx(0.35)
    rss_half = math.sqrt(0.10**2 + 0.05**2)
    assert chain["rss_mm"]["min"] == pytest.approx(0.2 - rss_half)
    assert chain["rss_mm"]["max"] == pytest.approx(0.2 + rss_half)
    # worst-case min exactly meets the 0.05 requirement -> margin 0, pass
    assert out["worst_case_mm"] == pytest.approx(0.0)
    assert chain["result"] == "pass"
    assert eng.log.rows(action="check_tolerance_stackup")[-1]["result"] == "pass"


def test_stackup_violation_is_a_fail_row(eng):
    gid = eng.create_part(BOX_SPEC, reason="stackup component")["geometry_id"]
    aid = eng.create_assembly(_assembly_spec(gid, {"min": 0.10}),
                              reason="violating rig")["assembly_id"]
    out = eng.check_tolerance_stackup(aid)
    assert out["worst_case_mm"] == pytest.approx(-0.05)
    row = eng.log.rows(action="check_tolerance_stackup")[-1]
    assert row["result"] == "fail"
    assert "tolerance_stackup_violation" in row["failure_mode"]
    assert "axial-clearance" in row["failure_mode"]


def test_assembly_rejects_unknown_component(eng):
    with pytest.raises(PartNotFound):
        eng.create_assembly(_assembly_spec("P0042@v1", {"min": 0.05}),
                            reason="should fail: component doesn't exist")
    assert eng.log.rows(action="create_assembly", result="fail")


def test_hole_that_removes_nothing_is_refused(eng):
    """A hole whose 'at' point misses the material must not silently no-op.

    Caught in real use: a shoe bracket was built, validated and nearly signed
    off carrying a bolt hole that did not exist. The face's local (u, v) axes
    are face-relative and can be mirrored (on a '>Y' face u runs along -X),
    so an 'at' that looks right in world coordinates can land in empty space.
    Same principle as rejecting unknown spec keys: silence is the danger.
    """
    spec = {
        "name": "missed-hole", "units": "mm",
        "features": [
            {"op": "box", "x": 76.2, "y": 60.0, "z": 6.35},
            {"op": "box", "x": 76.2, "y": 6.35, "z": 70.0,
             "at": [0, 26.825, 0], "mode": "union"},
            # (y=0, z=55) on the '>X' face is empty space: the upstand is at
            # y 23.65..30, and the sole only reaches z=6.35
            {"op": "hole", "d": 8.5, "at": [0.0, 55.0], "face": ">X"},
        ],
    }
    with pytest.raises(SpecError, match="removed no material"):
        eng.create_part(spec, reason="hole that misses the solid")
    assert eng.log.rows(action="create_part", result="fail")

    # the same bracket with the hole placed on the face it actually meets
    # ('>Y', whose u axis is mirrored: at=[u, v] -> world x=-u, z=v) builds
    good = dict(spec)
    good["name"] = "hit-hole"
    good["features"] = spec["features"][:2] + [
        {"op": "hole", "d": 8.5, "at": [0.0, 55.0], "face": ">Y"}]
    out = eng.create_part(good, reason="hole placed on the correct face")
    assert out["properties"]["volume_mm3"] > 0
