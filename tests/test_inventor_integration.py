"""Integration: the inventor layer driving the REAL engineering engine.

These tests build real CadQuery solids and, where marked, run real CalculiX.
They are the guard against the failure mode the brief warns about most
loudly — a beautiful set of abstractions that does not actually touch the
engineering code.

The FEA test is marked `slow` and skipped when the solver is absent, so the
default suite stays fast, but it is a genuine end-to-end run when it does
execute: geometry -> PartStore -> mesh -> deck -> ccx -> safety factor -> gate.
"""

import math
import shutil

import pytest

from design_engine import DesignEngine
from design_engine.inventor import (AnalyticStage, Candidate, Constraint,
                                    CostStage, DesignSpace, DesignVariable,
                                    EvalContext, EvaluationCache, Evaluator,
                                    EvolutionarySearch, FeaStage,
                                    FeasibilityRule, Fidelity, GeometryStage,
                                    Objective, Op, OptimizationConfig,
                                    OptimizationRun, RequirementSet, RuleStage,
                                    Sense, Status, VarType,
                                    machining_cost_model)

AL = {"name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
      "source": "OnlineMetals product pages, reused from the ladder build"}


# --------------------------------------------------------------- fixtures
def plate_space():
    return DesignSpace(name="plate", variables=[
        DesignVariable("length", VarType.CONTINUOUS, lo=60.0, hi=120.0,
                       step=5.0, units="mm"),
        DesignVariable("width", VarType.CONTINUOUS, lo=30.0, hi=60.0,
                       step=5.0, units="mm"),
        DesignVariable("thick", VarType.CONTINUOUS, lo=4.0, hi=14.0,
                       step=1.0, units="mm"),
        DesignVariable("bossed", VarType.CATEGORICAL, values=[False, True]),
        DesignVariable("boss_d", VarType.CONTINUOUS, lo=10.0, hi=24.0, step=2.0,
                       units="mm", active_if=lambda v: v.get("bossed") is True),
    ], rules=[FeasibilityRule("boss_fits",
                              lambda v: (not v.get("bossed"))
                              or v["boss_d"] < v["width"] - 8.0)])


def plate_spec(values, ctx):
    """Real design_engine spec. `bossed` changes the FEATURE LIST."""
    feats = [{"op": "box", "x": values["length"], "y": values["width"],
              "z": values["thick"], "at": [0, 0, 0]}]
    if values.get("bossed"):
        feats.append({"op": "cylinder", "d": values["boss_d"], "h": 6.0,
                      "at": [0, 0, values["thick"]], "mode": "union"})
    return {"name": "opt-plate", "units": "mm", "density_kg_m3": 2700,
            "features": feats}


def plate_analytic(values, ctx):
    """Simply supported centre load, 800 N. Hand-checkable."""
    w, t, L = values["width"], values["thick"], values["length"]
    Z = w * t ** 2 / 6.0
    stress = (800.0 * L / 4.0) / Z if Z else float("inf")
    return {"analytic_stress_MPa": stress,
            "sf.yield_von_mises": (276.0 / stress) if stress > 0 else float("inf")}


def plate_reqs():
    return RequirementSet(
        name="plate",
        constraints=[
            Constraint("strength", "sf.yield_von_mises", Op.GE, 1.5,
                       source="stated test gate"),
            Constraint("envelope", "bbox_x_mm", Op.LE, 120.0, units="mm",
                       source="stated envelope"),
        ],
        objectives=[Objective("mass", "mass_kg", Sense.MIN, units="kg")])


# ------------------------------------------------ real geometry integration
def test_geometry_stage_builds_real_solids(tmp_path):
    """L1 must actually invoke CadQuery and return exact OCC properties."""
    space, reqs = plate_space(), plate_reqs()
    ev = Evaluator([RuleStage(), AnalyticStage(plate_analytic, name="a"),
                    GeometryStage(spec_builder=plate_spec)],
                   EvalContext(space=space, requirements=reqs))
    c = Candidate(values=space.resolve({"length": 100.0, "width": 50.0,
                                        "thick": 10.0, "bossed": False}))
    ev.evaluate(c)
    assert c.status is Status.VALID
    # exact volume of a 100x50x10 box
    assert c.result.metrics["volume_mm3"] == pytest.approx(100 * 50 * 10)
    assert c.result.metrics["mass_kg"] == pytest.approx(50000 * 1e-9 * 2700)
    assert c.result.metrics["bbox_x_mm"] == pytest.approx(100.0)
    assert c.result.metric_fidelity["mass_kg"] is Fidelity.L1_GEOMETRY
    assert c.spec is not None and c.spec_digest


def test_topology_change_alters_the_built_geometry():
    """`bossed` adds a real feature, so volume must actually increase."""
    space, reqs = plate_space(), plate_reqs()
    ev = Evaluator([RuleStage(), AnalyticStage(plate_analytic, name="a"),
                    GeometryStage(spec_builder=plate_spec)],
                   EvalContext(space=space, requirements=reqs))
    base = {"length": 100.0, "width": 50.0, "thick": 10.0}
    plain = Candidate(values=space.resolve({**base, "bossed": False}))
    boss = Candidate(values=space.resolve({**base, "bossed": True, "boss_d": 20.0}))
    ev.evaluate(plain)
    ev.evaluate(boss)
    added = math.pi * (20.0 / 2) ** 2 * 6.0
    assert (boss.result.metrics["volume_mm3"]
            - plain.result.metrics["volume_mm3"]) == pytest.approx(added, rel=1e-3)
    assert plain.spec_digest != boss.spec_digest
    assert len(boss.spec["features"]) == len(plain.spec["features"]) + 1


def test_unbuildable_spec_is_invalid_not_unknown():
    """A spec the engine refuses is genuine infeasibility. Contrast with a
    kernel failure, which is UNKNOWN."""
    def bad_spec(values, ctx):
        # a hole placed in empty space - geometry.py refuses this outright
        return {"name": "bad", "units": "mm", "density_kg_m3": 2700,
                "features": [{"op": "box", "x": 20, "y": 20, "z": 5},
                             {"op": "hole", "d": 3, "at": [500.0, 500.0],
                              "face": ">Z"}]}
    space, reqs = plate_space(), plate_reqs()
    ev = Evaluator([GeometryStage(spec_builder=bad_spec)],
                   EvalContext(space=space, requirements=reqs))
    c = Candidate(values=space.resolve({"length": 100.0, "width": 50.0,
                                        "thick": 10.0, "bossed": False}))
    ev.evaluate(c)
    assert c.result.stages[0].status is Status.INVALID
    assert c.result.stages[0].failures[0].failure_class.value == "geometric"


def test_cost_stage_sees_upstream_geometry_metrics():
    """Regression: the in-progress result must be visible to later stages.
    This was broken once - cost silently returned nothing because it read an
    empty metrics dict, which wiped out the entire Pareto front."""
    space, reqs = plate_space(), plate_reqs()
    cost = machining_cost_model(rate_usd_per_hr=75.0, setup_min=10.0,
                                min_per_cm3_removed=0.5, stock_usd_per_kg=13.0,
                                stock_density_kg_m3=2700,
                                basis="test assumption, not a quotation")
    ev = Evaluator([GeometryStage(spec_builder=plate_spec),
                    CostStage(cost, name="cost")],
                   EvalContext(space=space, requirements=reqs))
    c = Candidate(values=space.resolve({"length": 100.0, "width": 50.0,
                                        "thick": 10.0, "bossed": False}))
    ev.evaluate(c)
    assert c.result.metrics.get("cost_usd") is not None
    assert c.result.metrics["cost_usd"] > 0


def test_full_search_over_real_geometry(tmp_path):
    """A complete optimisation where every candidate is a real solid."""
    space, reqs = plate_space(), plate_reqs()
    ctx = EvalContext(space=space, requirements=reqs)
    ev = Evaluator([RuleStage(), AnalyticStage(plate_analytic, name="a"),
                    GeometryStage(spec_builder=plate_spec)],
                   ctx, cache=EvaluationCache(tmp_path / "c.sqlite"))
    cfg = OptimizationConfig(population=12, generations=4, seed=3,
                             screen_fidelity=Fidelity.L1_GEOMETRY)
    run = OptimizationRun(EvolutionarySearch(space, reqs, cfg), ev, reqs, cfg,
                          workdir=tmp_path).run()
    front = run.front()
    assert front, "search produced no feasible design over real geometry"
    for c in front:
        assert c.feasible
        assert c.result.metrics["sf.yield_von_mises"] >= 1.5
        assert c.result.metrics["bbox_x_mm"] <= 120.0
        assert c.spec is not None            # a real, buildable spec exists


# ------------------------------------------------------- real solver (slow)
def _solver_available() -> bool:
    from pathlib import Path
    from design_engine import _DEFAULT_CCX
    return Path(_DEFAULT_CCX).is_file()


@pytest.mark.slow
@pytest.mark.skipif(not _solver_available(), reason="CalculiX not installed")
def test_fea_stage_runs_the_real_solver_and_overrides_the_estimate(tmp_path):
    """End-to-end: search proposes, CalculiX decides.

    Also asserts the promotion contract - the FEA-derived safety factor
    replaces the analytic one and carries L3 fidelity, so a screened estimate
    can never be mistaken for a solved result.
    """
    space, reqs = plate_space(), plate_reqs()
    engine = DesignEngine(tmp_path / "data")

    def case(cand, ctx):
        return {
            "material": dict(AL),
            "mesh": {"max_size_mm": 6.0},
            "constraints": [
                {"where": {"axis": "x", "at": "min"}, "dof": [1, 2, 3]},
                {"where": {"axis": "x", "at": "max"}, "dof": [2, 3]},
            ],
            "loads": [{"where": {"axis": "z", "at": "max"},
                       "force_total_N": [0.0, 0.0, -800.0]}],
            "limit_state": {"name": "yield_von_mises", "required_SF": 1.5},
        }

    ctx = EvalContext(space=space, requirements=reqs, engine=engine)
    ev = Evaluator([RuleStage(), AnalyticStage(plate_analytic, name="a"),
                    GeometryStage(spec_builder=plate_spec),
                    FeaStage(case, analysis="static", mesh_mm=6.0,
                             fidelity=Fidelity.L3_HIGH_FEA)],
                   ctx, cache=EvaluationCache(tmp_path / "c.sqlite"))

    c = Candidate(values=space.resolve({"length": 100.0, "width": 50.0,
                                        "thick": 12.0, "bossed": False}))
    ev.evaluate(c, max_fidelity=Fidelity.L1_GEOMETRY)
    analytic_sf = c.result.metrics["sf.yield_von_mises"]
    assert c.result.metric_fidelity["sf.yield_von_mises"] is Fidelity.L0_ANALYTIC

    ev.evaluate(c, max_fidelity=Fidelity.L3_HIGH_FEA)
    assert c.geometry_id, "promotion must materialise a real part"
    fea_sf = c.result.metrics["sf.yield_von_mises"]
    # the authoritative value replaced the estimate and is tagged as such
    assert c.result.metric_fidelity["sf.yield_von_mises"] is Fidelity.L3_HIGH_FEA
    assert isinstance(fea_sf, float) and fea_sf > 0

    # the run is in the FRACAS log like any other engineering action
    rows = [r for r in engine.log.rows(action="fea_static")
            if r["geometry_version"] == c.geometry_id]
    assert rows, "FEA must be recorded in the audit log"

    # second evaluation of the same candidate must hit cache, not re-solve
    hits_before = ev.cache.hits
    c2 = Candidate(values=dict(c.values))
    ev.evaluate(c2, max_fidelity=Fidelity.L3_HIGH_FEA)
    assert ev.cache.hits > hits_before
    assert c2.result.metrics["sf.yield_von_mises"] == pytest.approx(fea_sf)
    # and it must NOT have created a second part for identical geometry
    rows_after = [r for r in engine.log.rows(action="fea_static")]
    assert len([r for r in rows_after if r["result"] != "pending"]) == len(
        [r for r in rows if r["result"] != "pending"])
