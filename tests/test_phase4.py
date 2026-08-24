"""Phase 4 contract tests — CalculiX integration + safety-margin gate.

Analytic reference: 10 x 10 x 100 mm bar, S235JR (E=210000 MPa, nu=0.3,
yield 235 MPa — EN 10025-2 nominal, t<=16), encastre at z=0, axial tension.

  F = 1000 N  -> sigma = F/A = 1000/100  = 10 MPa,  u_tip = sigma*L/E = 4.7619e-3 mm
  F = 100 kN  -> sigma = 1000 MPa        -> SF = 0.235 vs yield: guaranteed gate fail

Median von Mises is the solver-correctness check (robust against the known
local artifacts at the load face and the encastre base); the gate thresholds
are chosen with wide margins so they test logic, not mesh luck.
"""

import json
import math

import pytest

from design_engine import DesignEngine
from design_engine.fea import FeaError
from design_engine.mesh import MeshError

BAR = {
    "name": "test-bar",
    "units": "mm",
    "features": [{"op": "box", "x": 10, "y": 10, "z": 100}],
}

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}


def _case(force_z, required_sf):
    return {
        "material": dict(S235),
        "mesh": {"max_size_mm": 5.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, force_z]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": required_sf},
    }


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("p4") / "data")


@pytest.fixture(scope="module")
def bar_gid(eng):
    return eng.create_part(BAR, reason="analytic verification bar")["geometry_id"]


@pytest.fixture(scope="module")
def pass_run(eng, bar_gid):
    return eng.run_fea_static(bar_gid, _case(1000, 2.0),
                              reason="verify solver against sigma=F/A=10 MPa")


def test_solver_matches_analytic(eng, pass_run):
    # median nodal von Mises ~ F/A away from load/constraint artifacts
    assert pass_run["median_von_mises_MPa"] == pytest.approx(10.0, rel=0.05)
    # tip displacement sigma*L/E (integral quantity, tight tolerance)
    assert pass_run["max_displacement_mm"] == pytest.approx(10 * 100 / 210000,
                                                            rel=0.05)


def test_gate_pass_row_and_artifact(eng, pass_run):
    assert pass_run["result"] == "pass"
    assert pass_run["failure_id"] is None
    assert pass_run["safety_factor"] >= 2.0
    row = eng.log.rows(action="fea_static")[-1]
    assert row["result"] == "pass" and row["phase"] == "validation"
    details = json.loads(row["details_json"])
    assert details["limit_state"] == "yield_von_mises"
    assert details["material"]["source"].startswith("EN 10025-2")
    png = eng.root / details["artifacts"][0]
    assert png.is_file() and png.stat().st_size > 1000


def test_gate_fail_writes_failure_record_then_edit_references_it(eng, bar_gid):
    out = eng.run_fea_static(bar_gid, _case(100000, 1.5),
                             reason="overload: sigma=1000 MPa must fail the gate")
    assert out["result"] == "fail"
    assert isinstance(out["failure_id"], int)
    # max vM >= nominal 1000 MPa -> SF <= 0.235; encastre Poisson-restraint
    # concentration on this mesh stays well under 2x -> SF > 0.1175
    assert 0.1175 < out["safety_factor"] <= 0.236
    # the failure record: mode + magnitude, written before control returned
    row = [r for r in eng.log.rows(action="fea_static") if r["id"] == out["failure_id"]][0]
    assert row["result"] == "fail"
    assert "yield_von_mises" in row["failure_mode"]
    assert "SF=" in row["failure_mode"] and "MPa" in row["failure_mode"]
    # Design references the failure — the not-blind retry contract
    edited = eng.edit_part(bar_gid, {"features.0.x": 12},
                           reason="thicken bar to address overload failure",
                           addresses_failure_id=out["failure_id"])
    edit_row = eng.log.rows(action="edit_part", result="pass")[-1]
    assert json.loads(edit_row["details_json"])["addresses_failure_id"] == out["failure_id"]
    assert edited["new_geometry_id"].endswith("@v2")
    # dangling failure reference is refused
    with pytest.raises(ValueError, match="does not reference"):
        eng.edit_part(bar_gid, {"features.0.x": 13}, reason="bad link",
                      addresses_failure_id=999999)


def test_degenerate_mesh_is_named_not_dumped_on_the_solver(eng):
    """A mesh too coarse for the feature must fail with an actionable reason.

    10x10 bar with a 6.6 mm axial bore leaves 1.7 mm walls; at 4 mm elements
    the midside nodes projected onto the bore invert elements. CalculiX aborts
    with a bare 'nonpositive jacobian determinant in element N'. The engine
    must catch this first and say what to change.
    """
    gid = eng.create_part(
        {"name": "thin-wall-tube", "units": "mm",
         "features": [{"op": "box", "x": 10, "y": 10, "z": 100},
                      {"op": "hole", "d": 6.6, "at": [0, 0]}]},
        reason="deliberately thin-walled bore")["geometry_id"]
    case = _case(1000, 2.0)
    case["mesh"]["max_size_mm"] = 4.0
    with pytest.raises(MeshError, match="degenerate_mesh") as exc:
        eng.run_fea_static(gid, case, reason="coarse mesh on a thin wall")
    assert "max_size_mm" in str(exc.value)      # names the knob to turn
    row = eng.log.rows(action="fea_static", result="fail")[-1]
    assert "degenerate_mesh" in row["failure_mode"]
    # and it never reached the solver
    assert "nonpositive jacobian" not in (row["failure_mode"] or "")


def test_case_validation_rejects_garbage(eng, bar_gid):
    good = _case(1000, 2.0)
    bad_extra = _case(1000, 2.0); bad_extra["turbo"] = True
    bad_mat = _case(1000, 2.0); bad_mat["material"].pop("source")
    bad_limit = _case(1000, 2.0); bad_limit["limit_state"]["name"] = "vibes"
    for bad, msg in ((bad_extra, "unexpected keys"),
                     (bad_mat, "source"),
                     (bad_limit, "limit state|limit_state")):
        with pytest.raises(FeaError):
            eng.run_fea_static(bar_gid, bad, reason="must be rejected")
    # empty node selection is a hard error, logged
    bad_sel = _case(1000, 2.0)
    bad_sel["constraints"][0]["where"] = {"axis": "z", "at": 424.2}
    with pytest.raises(MeshError, match="matched 0 nodes"):
        eng.run_fea_static(bar_gid, bad_sel, reason="selector off in space")
    fails = eng.log.rows(action="fea_static", result="fail")
    assert any("matched 0 nodes" in (r["failure_mode"] or "") for r in fails)
