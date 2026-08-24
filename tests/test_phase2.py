"""Phase 2 contract tests — FRACAS queries on the action log."""

import pytest

from design_engine import DesignEngine, SpecError

SPEC = {
    "name": "query-plate",
    "units": "mm",
    "features": [{"op": "box", "x": 20, "y": 30, "z": 10}],
}


@pytest.fixture()
def eng(tmp_path):
    return DesignEngine(tmp_path / "data")


def _build_history(eng):
    """1 create + 2 good edits + 1 failed edit; returns the final gid."""
    gid = eng.create_part(SPEC, reason="base")["geometry_id"]
    gid2 = eng.edit_part(gid, {"features.0.y": 40}, reason="widen")["new_geometry_id"]
    gid3 = eng.edit_part(gid2, {"features.0.z": 12}, reason="thicken")["new_geometry_id"]
    with pytest.raises(SpecError):
        eng.edit_part(gid3, {"features.0.q": 1}, reason="bad edit on purpose")
    return gid3


def test_version_history_ordered(eng):
    _build_history(eng)
    hist = eng.log.version_history("P0001")
    assert [r["geometry_version"] for r in hist] == [
        "P0001@v1", "P0001@v2", "P0001@v3"]
    assert [r["action"] for r in hist] == ["create_part", "edit_part", "edit_part"]


def test_lineage_walks_to_root(eng):
    gid3 = _build_history(eng)
    chain = eng.log.lineage(gid3)
    assert [r["geometry_version"] for r in chain] == [
        "P0001@v3", "P0001@v2", "P0001@v1"]
    assert chain[-1]["linked_parent_id"] is None


def test_failures_and_mode_counts(eng):
    _build_history(eng)
    with pytest.raises(SpecError):
        eng.create_part({"name": "bad", "units": "mm",
                         "features": [{"op": "warp", "x": 1}]},
                        reason="second failure, different mode")
    all_fails = eng.log.failures()
    assert len(all_fails) == 2
    assert all("SpecError" in r["failure_mode"] for r in all_fails)
    # substring filter distinguishes the two modes
    assert len(eng.log.failures("unknown op")) == 1
    assert len(eng.log.failures("not found")) == 0  # bad-path text differs
    counts = eng.log.failure_mode_counts()
    assert sum(n for _, n in counts) == 2
    assert all(n >= 1 for _, n in counts)


def test_pending_actions_detects_interrupted_run(eng):
    assert eng.log.pending_actions() == []
    orphan = eng.log.open_action("design", "create_part", reason="simulated crash")
    pending = eng.log.pending_actions()
    assert [r["id"] for r in pending] == [orphan]


def test_edit_rows_carry_full_properties(eng):
    """Report generation reads the log alone — edit rows must be self-sufficient."""
    import json
    _build_history(eng)
    last = eng.log.version_history("P0001")[-1]
    details = json.loads(last["details_json"])
    assert details["properties"]["volume_mm3"] == pytest.approx(20 * 40 * 12)
    assert details["parent"] == "P0001@v2"
