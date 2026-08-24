"""Phase 3 contract tests — condensed HTML report, generated from the log only."""

import base64
import re

import pytest

from design_engine import DesignEngine, SpecError

# smallest valid PNG (1x1, transparent)
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

SPEC = {
    "name": "report-plate",
    "units": "mm",
    "density_kg_m3": 7850,
    "features": [{"op": "box", "x": 20, "y": 30, "z": 10}],
}


@pytest.fixture()
def eng(tmp_path):
    return DesignEngine(tmp_path / "data")


def _scenario(eng):
    """Realistic log: create, edit, a failure, stackup pass+fail, validation row."""
    gid = eng.create_part(SPEC, reason="initial plate")["geometry_id"]
    gid2 = eng.edit_part(gid, {"features.0.y": 40},
                         reason="widen for clearance")["new_geometry_id"]
    with pytest.raises(SpecError):
        eng.create_part({"name": "bad", "units": "mm",
                         "features": [{"op": "warp"}]}, reason="doomed")
    aid = eng.create_assembly({
        "name": "rig", "units": "mm",
        "components": [{"geometry_id": gid2, "at": [0, 0, 0]}],
        "chains": [{
            "name": "gap", "requirement_mm": {"min": 0.10},
            "terms": [
                {"desc": "bore", "nominal": 10.0, "tol_plus": 0.10,
                 "tol_minus": 0.10, "sense": 1},
                {"desc": "pin", "nominal": 9.8, "tol_plus": 0.05,
                 "tol_minus": 0.05, "sense": -1},
            ]}],
    }, reason="stackup rig")["assembly_id"]
    eng.check_tolerance_stackup(aid)  # worst margin -0.05 -> fail row

    # simulated Phase 4 validation run with a diagnostic image
    art_dir = eng.root / "artifacts"
    art_dir.mkdir()
    (art_dir / "stress.png").write_bytes(PNG_1PX)
    vid = eng.log.open_action("validation", "fea_static", geometry_version=gid2,
                              reason="simulated run for report test")
    eng.log.close_action(vid, "pass", details={
        "safety_factor": 2.41, "limit_state": "yield_von_mises",
        "artifacts": ["artifacts/stress.png"]})
    return gid2


def test_report_contents(eng):
    gid2 = _scenario(eng)
    out = eng.generate_report()
    doc = out.read_text(encoding="utf-8")
    assert out.name == "report.html"
    # current state: latest version, not the old one, in the parts table
    assert gid2 in doc
    assert "V = 8000.0 mm³" in doc
    # exact diff from the edit row
    assert "features.0.y: 30 → 40" in doc
    # stackup numbers + violation
    assert "[0.05, 0.35]" in doc
    assert "tolerance_stackup_violation" in doc
    assert "-0.05" in doc
    # validation row + embedded image, base64, self-contained
    assert "yield_von_mises" in doc and "safety_factor=2.41" in doc
    assert "data:image/png;base64," in doc
    assert "http://" not in doc and "https://" not in doc
    # failure section carries the bad-spec failure
    assert "unknown op" in doc
    # production gate messaging until sign-off exists
    assert "No sign-offs recorded" in doc
    # report generation itself hit the log as a passed action
    rows = eng.log.rows(action="generate_report")
    assert len(rows) == 1 and rows[0]["result"] == "pass"


def test_report_flags_missing_artifact(eng):
    gid = eng.create_part(SPEC, reason="base")["geometry_id"]
    vid = eng.log.open_action("validation", "fea_static", geometry_version=gid)
    eng.log.close_action(vid, "pass", details={"artifacts": ["artifacts/gone.png"]})
    doc = eng.generate_report().read_text(encoding="utf-8")
    assert "MISSING ARTIFACT" in doc
    assert "gone.png" in doc


def test_report_flags_interrupted_runs(eng):
    eng.create_part(SPEC, reason="base")
    orphan = eng.log.open_action("design", "edit_part", reason="crash simulation")
    doc = eng.generate_report().read_text(encoding="utf-8")
    assert "never" in doc and str(orphan) in doc


def test_report_is_regenerable_and_escapes_html(eng):
    eng.create_part(SPEC, reason="<script>alert(1)</script> reason")
    first = eng.generate_report().read_text(encoding="utf-8")
    second = eng.generate_report().read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in first  # escaped, not executed
    assert "&lt;script&gt;" in first
    # regenerating reflects the previous generate_report row (log grew by one)
    assert re.search(r"\d+ logged actions", second)
    assert first != second
