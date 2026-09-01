"""Submodelling (roadmap NEXT #4).

A1 is blocked because refining a 1280 mm frame to resolve a 19 mm junction hit
a 6.1 GB working set. Submodelling answers the local question instead — but it
has two failure modes that both produce a confident number, and those are what
is tested here:

  - cutting so close that the imposed displacements come from the part of the
    coarse solve that was wrong
  - driving original free surfaces, which stiffens the region and lowers the
    peak in the unsafe direction

The solver does the interpolation (CalculiX `*SUBMODEL`, verified present in
the shipped 2.23 binary), so none of that arithmetic is tested here. What is
tested is that the engine refuses to ask it the wrong question.
"""

from pathlib import Path

import pytest

from design_engine.mesh import mesh_step

from design_engine.singularity import RefinementRefused
from design_engine.submodel import (SUGGESTED_STANDOFF,
                                    SubmodelError, SubmodelRegion, converged,
                                    coplanar_risk, driven_nodes, plan,
                                    refinement_ladder)

CLEAN = {"verdict": "clean", "reason": "well clear of every edge"}
SINGULAR = {"verdict": "singular", "reason": "1.28 mm from a 270-degree edge"}
UNKNOWN = {"verdict": "unknown", "reason": "geometry could not be analysed"}


def _region(**kw):
    base = dict(centre=(0.0, 0.0, 0.0), feature_mm=10.0,
                standoff_elements=3.0, mesh_mm=3.2)
    base.update(kw)
    return SubmodelRegion(**base)


# ----------------------------------------------------------- the gate first
def test_a_singular_peak_is_refused_before_anything_is_cut():
    """The expensive discovery. Cutting and meshing a region around a peak
    that cannot converge spends the budget to produce a rising number."""
    with pytest.raises(RefinementRefused, match="submodel refinement"):
        plan(SINGULAR, (0, 0, 0), feature_mm=10.0, standoff_elements=3.0,
             mesh_mm=3.2)


def test_an_unknown_classification_is_also_refused():
    with pytest.raises(RefinementRefused):
        plan(UNKNOWN, (0, 0, 0), feature_mm=10.0, standoff_elements=3.0,
             mesh_mm=3.2)


def test_a_clean_peak_yields_a_region():
    r = plan(CLEAN, (1, 2, 3), feature_mm=10.0, standoff_elements=3.0,
             mesh_mm=3.2)
    assert isinstance(r, SubmodelRegion)
    assert r.centre == (1.0, 2.0, 3.0)


# ------------------------------------------------------- cutting too close
def test_the_standoff_has_no_default():
    """Same rule as the S-N endurance limit: a value that decides whether the
    answer means anything must be stated, not inherited."""
    with pytest.raises(SubmodelError, match="no default"):
        _region(standoff_elements=None)


def test_a_standoff_inside_one_feature_size_is_refused():
    """The cut would sit in the concentration, driven by the coarse values
    that are wrong exactly there."""
    with pytest.raises(SubmodelError, match="within one feature size"):
        _region(standoff_elements=0.5)


def test_the_suggested_standoff_is_flagged_as_a_rule_of_thumb_not_a_standard():
    assert SUGGESTED_STANDOFF == 3.0
    with pytest.raises(SubmodelError) as e:
        _region(standoff_elements=None)
    assert "NOT a sourced standard" in str(e.value)


def test_the_box_grows_with_the_feature_not_the_mesh():
    """A region scaled to the mesh would shrink as the mesh refines, walking
    the cut into the concentration exactly as the answer started to matter."""
    coarse = _region(mesh_mm=8.0)
    fine = _region(mesh_mm=0.5)
    assert coarse.half_mm == fine.half_mm
    assert _region(feature_mm=20.0).half_mm == 2 * _region(feature_mm=10.0).half_mm


@pytest.mark.parametrize("bad", [0.0, -5.0])
def test_a_non_positive_feature_size_is_refused(bad):
    with pytest.raises(SubmodelError, match="feature_mm"):
        _region(feature_mm=bad)


# --------------------------------------------------- driving the right faces
def test_a_node_on_a_cut_plane_is_driven():
    r = _region()                       # half width 40 mm about the origin
    assert r.on_cut_plane((40.0, 0.0, 0.0))
    assert r.on_cut_plane((-40.0, 10.0, 10.0))


def test_a_node_inside_the_region_is_not_driven():
    """Interior nodes are what the submodel is for. Driving them would impose
    the coarse answer everywhere and resolve nothing."""
    assert not _region().on_cut_plane((0.0, 0.0, 0.0))


def test_a_node_outside_the_box_is_not_driven_even_if_flush_on_one_axis():
    """The subtle one. A point beyond the box on x but exactly on the y plane
    reads as 'on a plane' to a naive per-axis test, and driving it clamps
    material that is not in the submodel at all."""
    r = _region()
    assert not r.on_cut_plane((999.0, 40.0, 0.0))


def test_driven_nodes_picks_the_cut_surface_out_of_a_mesh():
    mesh = {"node_tags": [1, 2, 3, 4],
            "coords": [(40.0, 0.0, 0.0),      # on +x cut plane -> driven
                       (0.0, 0.0, 0.0),       # interior         -> free
                       (0.0, -40.0, 5.0),     # on -y cut plane  -> driven
                       (100.0, 0.0, 0.0)]}    # outside          -> free
    assert driven_nodes(mesh, _region()) == [1, 3]



def test_coplanar_part_faces_are_reported_not_silently_driven():
    """The stated limitation. Classification is geometric, so an original free
    face lying in a cut plane cannot be distinguished from the cut itself —
    and driving a free face stiffens the region and LOWERS the peak."""
    r = _region()                                  # box spans -40..+40
    part_bounds = (-40.0, -200.0, -200.0, 200.0, 200.0, 200.0)
    hits = coplanar_risk(part_bounds, r)
    assert {"axis": "x", "side": "min", "at_mm": -40.0} in hits


def test_no_coplanar_risk_when_the_region_is_interior():
    part_bounds = (-500.0, -500.0, -500.0, 500.0, 500.0, 500.0)
    assert coplanar_risk(part_bounds, _region()) == []


# --------------------------------------------------------------- the deck




# ------------------------------------------------------------- the ladder
def test_a_single_mesh_size_is_not_a_convergence_study():
    with pytest.raises(SubmodelError, match="at least two"):
        refinement_ladder(3.2, _region(), steps=1)


def test_the_ladder_refuses_to_start_when_even_the_finest_rung_cannot_resolve():
    """Two elements across a feature is already generous. Below that the study
    measures the mesh rather than converging."""
    with pytest.raises(SubmodelError, match="not resolved at any rung"):
        refinement_ladder(8.0, _region(feature_mm=1.0), steps=2, factor=2.0)


def test_the_ladder_is_bounded_and_monotonically_finer():
    ladder = refinement_ladder(3.2, _region(), steps=3, factor=2.0)
    assert ladder == [3.2, 1.6, 0.8]
    assert len(ladder) == 3, "bounded on purpose"


# --------------------------------------------------------- the convergence
def test_two_rungs_cannot_declare_convergence():
    """One difference cannot distinguish convergence from a slow monotonic
    climb up a singularity — which is exactly the case this engine keeps
    getting wrong."""
    v = converged([100.0, 101.0], tol_pct=2.0)
    assert v["converged"] is None
    assert "at least three" in v["reason"]


def test_a_settling_sequence_converges():
    v = converged([100.0, 110.0, 111.0], tol_pct=2.0)
    assert v["converged"] is True
    assert v["change_pct"] == pytest.approx(0.909, abs=0.01)


def test_a_steady_climb_does_not_count_as_converged():
    """The case that matters. Both steps are 1.0% — comfortably inside a 2%
    tolerance — but they are not SHRINKING. Under uniform refinement a
    singular peak grows as h**-p, which gives successive changes a constant
    ratio, so equal steps are the signature of the thing this engine keeps
    mistaking for convergence."""
    v = converged([100.0, 101.0, 102.01], tol_pct=2.0)
    assert v["change_pct"] == pytest.approx(v["previous_change_pct"], abs=0.01)
    assert v["change_pct"] < 2.0, "inside tolerance, and still not converged"
    assert v["converged"] is False


def test_the_verdict_carries_the_numbers_that_produced_it():
    v = converged([100.0, 110.0, 111.0], tol_pct=2.0)
    assert v["peaks"] == [100.0, 110.0, 111.0]
    assert v["previous_change_pct"] == pytest.approx(10.0)


# =========================================================== orchestration
# These run the real solver. The cheap tests above pin the refusals; these
# pin that the thing actually works end to end and agrees with the parent
# solve where the parent is trustworthy.
from design_engine import DesignEngine                      # noqa: E402
from design_engine.fea import FeaError, ValidationTools     # noqa: E402
from design_engine.fea import _parse_frd, von_mises         # noqa: E402

STEEL = {"name": "S235JR", "E_MPa": 210000.0, "nu": 0.3, "yield_MPa": 235.0,
         "source": "EN 10025-2 nominal values, t<=16mm"}

# 20 x 20 x 120, z from 0 to 120. Fixed at z=0, loaded at z=120, so a
# mid-span region at z=60 contains neither boundary condition.
BAR = {"name": "submodel-bar", "units": "mm",
       "features": [{"op": "box", "x": 20.0, "y": 20.0, "z": 120.0}]}

STANDOFF_SRC = ("test fixture: 3 feature-lengths, the common rule of thumb. "
                "NOT a sourced standard")


def _case():
    return {"material": dict(STEEL),
            "mesh": {"max_size_mm": 6.0},
            "constraints": [{"where": {"axis": "z", "at": "min"},
                             "dof": [1, 2, 3]}],
            "loads": [{"where": {"axis": "z", "at": "max"},
                       "force_total_N": [500, 0, 0]}],
            "limit_state": {"name": "yield_von_mises", "required_SF": 1.5}}


@pytest.fixture(scope="module")
def solved(tmp_path_factory):
    """A real global solve, shared by the orchestration tests."""
    eng = DesignEngine(tmp_path_factory.mktemp("sub") / "data")
    eng.validation = ValidationTools(eng.validation.root, eng.log, eng.parts,
                                     eng.validation.ccx_path,
                                     solve_timeout_s=900)
    if not eng.validation.ccx_path.is_file():
        pytest.skip("CalculiX not installed in this working copy")
    gid = eng.create_part(BAR, reason="submodel orchestration fixture")["geometry_id"]
    res = eng.validation.fea_static(gid, _case(),
                                    reason="global solve to drive a submodel")
    return eng, gid, res





def test_a_region_containing_a_load_is_refused_not_silently_doubled(solved):
    """The load's effect is already carried by the driven displacements,
    because the global solve that produced them had it applied. Applying it
    again inside the submodel counts it twice."""
    eng, gid, res = solved
    with pytest.raises(FeaError, match="already carried by the driven"):
        eng.validation.fea_submodel(
            gid, _case(), res, reason="region over the loaded end",
            feature_mm=4.0, standoff_elements=3.0,
            standoff_source=STANDOFF_SRC, centre=(0.0, 0.0, 118.0),
            ladder_steps=2)


def test_a_region_containing_a_constraint_is_refused(solved):
    eng, gid, res = solved
    with pytest.raises(FeaError, match="already carried by the driven"):
        eng.validation.fea_submodel(
            gid, _case(), res, reason="region over the fixed end",
            feature_mm=4.0, standoff_elements=3.0,
            standoff_source=STANDOFF_SRC, centre=(0.0, 0.0, 2.0),
            ladder_steps=2)


def test_a_singular_global_peak_is_refused_before_any_solver_time(solved):
    eng, gid, res = solved
    poisoned = dict(res)
    poisoned["singularity"] = {"verdict": "singular",
                               "reason": "1.28 mm from a 270-degree edge"}
    with pytest.raises(RefinementRefused):
        eng.validation.fea_submodel(
            gid, _case(), poisoned, reason="must refuse before cutting",
            feature_mm=4.0, standoff_elements=3.0,
            standoff_source=STANDOFF_SRC, centre=(0.0, 0.0, 60.0))


def test_a_missing_global_frd_is_named(solved, tmp_path):
    eng, gid, res = solved
    orphan = dict(res)
    orphan["run_dir"] = str(tmp_path / "nowhere")
    with pytest.raises(FeaError, match="global results not found"):
        eng.validation.fea_submodel(
            gid, _case(), orphan, reason="missing parent results",
            feature_mm=4.0, standoff_elements=3.0,
            standoff_source=STANDOFF_SRC, centre=(0.0, 0.0, 60.0))



def test_a_region_swallowing_the_whole_part_is_refused(solved):
    """Then it is not a submodel, it is the original solve with boundary
    displacements bolted on — strictly worse, and still reported as a
    submodel."""
    eng, gid, res = solved
    with pytest.raises(SubmodelError, match="keeps"):
        eng.validation.fea_submodel(
            gid, _case(), res, reason="region covers everything",
            feature_mm=200.0, standoff_elements=3.0,
            standoff_source=STANDOFF_SRC, centre=(0.0, 0.0, 60.0))




def test_the_cheap_refusals_still_fire_before_the_solver_limitation(solved):
    """Ordering. A region containing a load is wrong regardless of whether
    the solver could interpolate, and must be reported as that - not masked
    by the interpolation refusal behind it."""
    eng, gid, res = solved
    with pytest.raises(FeaError, match="already carried by the driven"):
        eng.validation.fea_submodel(
            gid, _case(), res, reason="load inside the region",
            feature_mm=4.0, standoff_elements=3.0,
            standoff_source=STANDOFF_SRC, centre=(0.0, 0.0, 118.0),
            ladder_steps=3)


def test_the_submodel_now_solves_with_our_own_interpolation(solved):
    """The point of the whole exercise.

    ccx's *SUBMODEL cost 88-182 ms per driven node and did not finish inside
    900 s on the real geometry. The deck now states the displacements outright
    instead of asking the solver to work them out, so this runs.
    """
    eng, gid, res = solved
    out = eng.validation.fea_submodel(
        gid, _case(), res, reason="solve with the local interpolation",
        feature_mm=4.0, standoff_elements=3.0, standoff_source=STANDOFF_SRC,
        centre=(0.0, 0.0, 60.0), ladder_steps=3, tol_pct=5.0)
    assert len(out["rungs"]) == 3
    assert all(r["max_von_mises_MPa"] > 0 for r in out["rungs"])
    assert all(r["driven_nodes"] > 0 for r in out["rungs"])


def test_a_study_that_does_not_converge_closes_FAIL_not_unknown(solved):
    """The log's vocabulary is pass|fail and nothing else.

    This line closed with "unknown" until 2026-09-01 and close_action refuses
    that - the *SUBMODEL refusal returned before ever reaching it, so the bug
    sat behind a guard. Removing the guard is what ran the code.

    The failure_mode has to distinguish the two failures it could mean: the
    STUDY did not converge; the PART did not fail anything.
    """
    eng, gid, res = solved
    out = eng.validation.fea_submodel(
        gid, _case(), res, reason="tolerance too tight to ever converge",
        feature_mm=4.0, standoff_elements=3.0, standoff_source=STANDOFF_SRC,
        centre=(0.0, 0.0, 60.0), ladder_steps=3, tol_pct=1e-9)
    row = eng.log._conn.execute(
        "SELECT result, failure_mode FROM actions WHERE id = ?",
        (out["action_id"],)).fetchone()
    assert row["result"] == "fail"
    assert "convergence_not_established" in row["failure_mode"]
    assert "not the part failing a limit state" in row["failure_mode"]
