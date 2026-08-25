"""Evaluator, cache, Pareto, optimisers, failure memory, robustness.

Everything here runs on cheap analytic stages so the suite stays fast; the
real-engine integration is tested separately in test_inventor_integration.py.
The behaviours asserted are the ones that make the layer trustworthy rather
than merely functional:

  - a cache never returns a result computed under different inputs
  - a stage that raises yields UNKNOWN, not INVALID
  - an infeasible candidate never dominates a feasible one
  - untrustworthy (numerical) failures do not steer the search
"""

import math
import random

import pytest

from design_engine.inventor import (AnalyticStage, CallableStage, Candidate,
                                    Constraint, DesignSpace, DesignVariable,
                                    EvalContext, EvaluationCache, Evaluator,
                                    EvolutionarySearch, FailureClass,
                                    FailureMemory, FailureRecord, Fidelity,
                                    Objective, Op, OptimizationConfig,
                                    OptimizationRun, RandomSearch,
                                    RequirementSet, RuleStage, Sense,
                                    StageResult, Status, VarType, archetypes,
                                    dominates, non_dominated_sort,
                                    pareto_front, robustness, sensitivity,
                                    tolerance_perturbation, total_violation)


# ------------------------------------------------------------------ fixtures
def make_space():
    return DesignSpace(name="beam", variables=[
        DesignVariable("w", VarType.CONTINUOUS, lo=5.0, hi=40.0, step=0.5, units="mm"),
        DesignVariable("h", VarType.CONTINUOUS, lo=5.0, hi=60.0, step=0.5, units="mm"),
    ])


def make_reqs(sf_min=2.0):
    return RequirementSet(
        name="beam",
        constraints=[Constraint("strength", "sf.yield_von_mises", Op.GE,
                                sf_min, source="stated design gate")],
        objectives=[Objective("mass", "mass_kg", Sense.MIN, units="kg"),
                    Objective("stiffness", "section_I_mm4", Sense.MAX, units="mm4")])


def analytic(values, ctx):
    """Cantilever, 500 N at 300 mm, 6061-ish. Hand-checkable closed form."""
    w, h = values["w"], values["h"]
    I = w * h ** 3 / 12.0
    Z = I / (h / 2.0) if h else 0.0
    stress = (500.0 * 300.0 / Z) if Z else float("inf")
    return {"section_I_mm4": I,
            "mass_kg": w * h * 300.0 * 1e-9 * 2700.0,
            "sf.yield_von_mises": (276.0 / stress) if stress > 0 else float("inf")}


def make_evaluator(cache=None, space=None, reqs=None):
    space = space or make_space()
    reqs = reqs or make_reqs()
    ctx = EvalContext(space=space, requirements=reqs)
    return Evaluator([RuleStage(), AnalyticStage(analytic, name="beam")],
                     ctx, cache=cache or EvaluationCache())


# ------------------------------------------------------------------ evaluator
def test_evaluator_produces_metrics_and_tags_fidelity():
    ev = make_evaluator()
    c = Candidate(values={"w": 20.0, "h": 40.0})
    ev.evaluate(c)
    I = 20.0 * 40.0 ** 3 / 12.0
    assert c.result.metrics["section_I_mm4"] == pytest.approx(I)
    assert c.result.metric_fidelity["mass_kg"] is Fidelity.L0_ANALYTIC
    assert c.result.max_fidelity is Fidelity.L0_ANALYTIC
    assert c.status is Status.VALID


def test_stage_exception_is_unknown_not_invalid():
    """We did not learn the design is bad; we failed to find out."""
    def boom(values, ctx):
        raise RuntimeError("solver exploded")
    space, reqs = make_space(), make_reqs()
    ev = Evaluator([AnalyticStage(boom, name="boom")],
                   EvalContext(space=space, requirements=reqs))
    c = Candidate(values={"w": 10.0, "h": 10.0})
    ev.evaluate(c)
    assert c.result.stages[0].status is Status.UNKNOWN
    assert c.status is Status.UNKNOWN
    f = c.result.stages[0].failures[0]
    assert f.failure_class is FailureClass.NUMERICAL and f.trustworthy is False


def test_evaluator_stops_at_first_blocking_stage():
    """No reason to run an expensive model on a design already ruled out."""
    from design_engine.inventor import FeasibilityRule
    space = DesignSpace(
        name="s",
        variables=[DesignVariable("w", VarType.CONTINUOUS, lo=1.0, hi=10.0),
                   DesignVariable("h", VarType.CONTINUOUS, lo=1.0, hi=10.0)],
        rules=[FeasibilityRule("h_gt_w", lambda v: v["h"] > v["w"])])
    calls = []

    def counted(values, ctx):
        calls.append(1)
        return {"mass_kg": 1.0, "sf.yield_von_mises": 5.0, "section_I_mm4": 1.0}

    ev = Evaluator([RuleStage(), AnalyticStage(counted, name="a")],
                   EvalContext(space=space, requirements=make_reqs()))
    ev.evaluate(Candidate(values={"w": 9.0, "h": 2.0}))     # violates the rule
    assert calls == []
    ev.evaluate(Candidate(values={"w": 2.0, "h": 9.0}))
    assert len(calls) == 1


def test_fidelity_of_a_result_is_its_weakest_stage():
    space, reqs = make_space(), make_reqs()

    def cheap(values, ctx):
        return {"mass_kg": 1.0}

    def rich(cand, ctx):
        return StageResult("rich", Fidelity.L3_HIGH_FEA, Status.VALID,
                           metrics={"sf.yield_von_mises": 9.0, "section_I_mm4": 1.0})

    ev = Evaluator([AnalyticStage(cheap, name="cheap"),
                    CallableStage(rich, "rich", Fidelity.L3_HIGH_FEA)],
                   EvalContext(space=space, requirements=reqs))
    c = Candidate(values={"w": 10.0, "h": 10.0})
    ev.evaluate(c)
    assert c.result.max_fidelity is Fidelity.L3_HIGH_FEA
    assert c.result.fidelity is Fidelity.L0_ANALYTIC          # weakest wins
    assert c.result.metric_fidelity["sf.yield_von_mises"] is Fidelity.L3_HIGH_FEA


def test_max_fidelity_ceiling_skips_expensive_stages():
    space, reqs = make_space(), make_reqs()
    ran = []

    def expensive(cand, ctx):
        ran.append(1)
        return StageResult("exp", Fidelity.L3_HIGH_FEA, Status.VALID, metrics={})

    ev = Evaluator([AnalyticStage(analytic, name="a"),
                    CallableStage(expensive, "exp", Fidelity.L3_HIGH_FEA)],
                   EvalContext(space=space, requirements=reqs))
    ev.evaluate(Candidate(values={"w": 20.0, "h": 40.0}),
                max_fidelity=Fidelity.L1_GEOMETRY)
    assert ran == []
    ev.evaluate(Candidate(values={"w": 21.0, "h": 40.0}),
                max_fidelity=Fidelity.L3_HIGH_FEA)
    assert len(ran) == 1


# ---------------------------------------------------------------------- cache
def test_cache_hit_avoids_recomputation():
    calls = []

    def counted(values, ctx):
        calls.append(1)
        return analytic(values, ctx)

    space, reqs = make_space(), make_reqs()
    ev = Evaluator([AnalyticStage(counted, name="a")],
                   EvalContext(space=space, requirements=reqs))
    for _ in range(4):
        ev.evaluate(Candidate(values={"w": 20.0, "h": 40.0}))
    assert len(calls) == 1
    assert ev.cache.hits == 3 and ev.cache.misses == 1


def test_cache_key_changes_with_design_space():
    """A widened bound is a different problem; the old answer must not be
    served for it."""
    c = Candidate(values={"w": 20.0, "h": 40.0})
    stage = AnalyticStage(analytic, name="a")
    ctx1 = EvalContext(space=make_space(), requirements=make_reqs())
    space2 = DesignSpace(name="beam", variables=[
        DesignVariable("w", VarType.CONTINUOUS, lo=5.0, hi=99.0, step=0.5),
        DesignVariable("h", VarType.CONTINUOUS, lo=5.0, hi=60.0, step=0.5)])
    ctx2 = EvalContext(space=space2, requirements=make_reqs())
    assert EvaluationCache.key(c, stage, ctx1) != EvaluationCache.key(c, stage, ctx2)


def test_cache_key_changes_with_stage_config():
    c = Candidate(values={"w": 20.0, "h": 40.0})
    ctx = EvalContext(space=make_space(), requirements=make_reqs())
    a = AnalyticStage(analytic, name="a", version="v1")
    b = AnalyticStage(analytic, name="a", version="v2")
    assert EvaluationCache.key(c, a, ctx) != EvaluationCache.key(c, b, ctx)


def test_cache_key_includes_engineering_code_digest():
    from design_engine.inventor import CODE_DIGEST
    assert CODE_DIGEST and len(CODE_DIGEST) == 12


def test_cache_persists_across_instances(tmp_path):
    db = tmp_path / "cache.sqlite"
    calls = []

    def counted(values, ctx):
        calls.append(1)
        return analytic(values, ctx)

    for _ in range(2):
        ev = make_evaluator(cache=EvaluationCache(db))
        ev.stages = [RuleStage(), AnalyticStage(counted, name="beam")]
        ev.evaluate(Candidate(values={"w": 20.0, "h": 40.0}))
    assert len(calls) == 1        # second Evaluator read it back from disk


def test_identical_designs_reached_differently_share_a_cache_entry():
    ev = make_evaluator()
    a = Candidate(values={"w": 20.0, "h": 40.0})
    b = Candidate(values={"h": 40.0, "w": 20.0})
    ev.evaluate(a)
    ev.evaluate(b)
    assert a.candidate_id == b.candidate_id
    assert ev.cache.hits == 2      # rules + analytic both hit


# --------------------------------------------------------------------- pareto
def test_dominance_basics():
    assert dominates([1.0, 1.0], [2.0, 2.0])
    assert dominates([1.0, 2.0], [1.0, 3.0])
    assert not dominates([1.0, 3.0], [2.0, 2.0])       # mutually non-dominated
    assert not dominates([1.0, 1.0], [1.0, 1.0])       # equal is not dominance


def _cand(mass, I, feasible=True):
    c = Candidate(values={"w": mass, "h": I})
    c.result.metrics = {"mass_kg": mass, "section_I_mm4": I}
    c.result.status = Status.VALID if feasible else Status.INVALID
    return c


def test_pareto_front_excludes_dominated():
    """mass is minimised, stiffness maximised, so:
       light  = (1.0 kg, I=10)     light but flexible
       stiff  = (4.0 kg, I=900)    heavy but stiff      -> trade, both survive
       dud    = (4.0 kg, I=800)    same mass, less stiff -> dominated by stiff
    """
    objs = make_reqs().objectives
    light, stiff, dud = _cand(1.0, 10.0), _cand(4.0, 900.0), _cand(4.0, 800.0)
    front = pareto_front([light, stiff, dud], objs)
    assert light in front and stiff in front
    assert dud not in front


def test_a_design_better_on_every_axis_dominates_outright():
    objs = make_reqs().objectives
    better, worse = _cand(1.0, 10.0), _cand(2.0, 5.0)
    assert pareto_front([better, worse], objs) == [better]


def test_infeasible_never_enters_the_frontier():
    objs = make_reqs().objectives
    great_but_illegal = _cand(0.001, 1e9, feasible=False)
    ok = _cand(5.0, 10.0)
    front = pareto_front([great_but_illegal, ok], objs)
    assert front == [ok]


def test_incomplete_objective_vector_is_excluded():
    objs = make_reqs().objectives
    partial = Candidate(values={})
    partial.result.metrics = {"mass_kg": 0.1}     # no section_I_mm4
    partial.result.status = Status.VALID
    complete = _cand(5.0, 10.0)
    front = pareto_front([partial, complete], objs)
    # an unbeatable mass must NOT win on a partial vector
    assert front == [complete]


def test_non_dominated_sort_layers():
    objs = make_reqs().objectives
    best, mid, worst = _cand(1.0, 10.0), _cand(2.0, 5.0), _cand(3.0, 1.0)
    fronts = non_dominated_sort([worst, mid, best], objs)
    assert best in fronts[0]
    assert worst in fronts[-1]


def test_archetypes_are_derived_not_hardcoded():
    reqs = make_reqs()
    light, stiff = _cand(1.0, 10.0), _cand(4.0, 900.0)
    arch = archetypes([light, stiff], reqs)
    assert arch["best_mass"] is light
    assert arch["best_stiffness"] is stiff
    assert "balanced" in arch
    # single-objective problems must NOT invent a "balanced" archetype
    single = RequirementSet(name="s",
                            objectives=[Objective("mass", "mass_kg", Sense.MIN)])
    assert "balanced" not in archetypes([light, stiff], single)


# ------------------------------------------------------------------ optimisers
def test_total_violation_counts_unknown_as_violation():
    reqs = make_reqs()
    c = Candidate(values={})
    c.result.metrics = {"mass_kg": 1.0, "section_I_mm4": 1.0}
    c.result.apply_requirements(reqs)          # sf metric missing -> UNKNOWN
    assert total_violation(c) == pytest.approx(1.0)


def test_random_search_is_reproducible_and_deduplicates():
    space, reqs = make_space(), make_reqs()
    a = RandomSearch(space, reqs, OptimizationConfig(seed=7)).ask(20)
    b = RandomSearch(space, reqs, OptimizationConfig(seed=7)).ask(20)
    assert [c.candidate_id for c in a] == [c.candidate_id for c in b]
    assert len({c.candidate_id for c in a}) == len(a)


def test_evolutionary_search_improves_on_random_baseline():
    """The baseline must actually be beaten, not assumed to be beaten."""
    space, reqs = make_space(), make_reqs()

    def best_mass(run):
        feas = [c for c in run.all_candidates if c.feasible]
        return min((c.result.metrics["mass_kg"] for c in feas), default=float("inf"))

    cfg = OptimizationConfig(population=24, generations=8, seed=3)
    rnd = OptimizationRun(RandomSearch(space, reqs, cfg), make_evaluator(),
                          reqs, cfg).run()
    evo = OptimizationRun(EvolutionarySearch(space, reqs, cfg), make_evaluator(),
                          reqs, cfg).run()
    assert best_mass(evo) <= best_mass(rnd)


def test_optimizer_never_returns_an_infeasible_design_as_best():
    space, reqs = make_space(), make_reqs(sf_min=3.0)
    cfg = OptimizationConfig(population=20, generations=6, seed=11)
    run = OptimizationRun(EvolutionarySearch(space, reqs, cfg),
                          make_evaluator(reqs=reqs), reqs, cfg).run()
    for c in run.front():
        assert c.feasible
        assert c.result.metrics["sf.yield_von_mises"] >= 3.0


def test_run_summary_and_checkpoint_resume(tmp_path):
    space, reqs = make_space(), make_reqs()
    cfg = OptimizationConfig(population=10, generations=3, seed=5)
    run = OptimizationRun(EvolutionarySearch(space, reqs, cfg), make_evaluator(),
                          reqs, cfg, workdir=tmp_path).run()
    s = run.summary()
    assert s["evaluations"] > 0 and s["unique_candidates"] > 0
    path = run.checkpoint()
    assert path.is_file()

    cfg2 = OptimizationConfig(population=10, generations=1, seed=5)
    fresh = OptimizationRun(EvolutionarySearch(space, reqs, cfg2),
                            make_evaluator(), reqs, cfg2)
    replayed = fresh.resume_from(path)
    assert replayed == len(run.all_candidates)
    # resumed optimiser must not re-propose already-seen designs
    known = {c.candidate_id for c in run.all_candidates}
    assert all(c.candidate_id not in known for c in fresh.optimizer.ask(10))


def test_parallel_evaluation_matches_serial():
    space, reqs = make_space(), make_reqs()
    cands = [Candidate(values={"w": 10.0 + i, "h": 30.0}) for i in range(12)]
    serial = make_evaluator().evaluate_many([Candidate(values=dict(c.values)) for c in cands],
                                            workers=1)
    parallel = make_evaluator().evaluate_many([Candidate(values=dict(c.values)) for c in cands],
                                              workers=4)
    assert ([round(c.result.metrics["mass_kg"], 9) for c in serial]
            == [round(c.result.metrics["mass_kg"], 9) for c in parallel])


def test_unsafe_stage_forces_serial_execution():
    """A stage that mutates shared engine state must not be threaded."""
    def unsafe(cand, ctx):
        return StageResult("u", Fidelity.L2_COARSE_FEA, Status.VALID, metrics={})
    stage = CallableStage(unsafe, "u", Fidelity.L2_COARSE_FEA, thread_safe=False)
    ev = Evaluator([AnalyticStage(analytic, name="a"), stage],
                   EvalContext(space=make_space(), requirements=make_reqs()))
    out = ev.evaluate_many([Candidate(values={"w": 10.0 + i, "h": 30.0})
                            for i in range(6)], workers=4)
    assert len(out) == 6      # completed correctly via the serial fallback


# ------------------------------------------------------- failure-informed search
def test_failure_memory_ignores_untrustworthy_failures():
    space = make_space()
    mem = FailureMemory(space, min_observations=1)
    c = Candidate(values={"w": 5.0, "h": 5.0})
    c.result.failures = [FailureRecord(FailureClass.NUMERICAL,
                                       message="mesh refused", trustworthy=False)]
    mem.observe(c)
    assert mem.counts.get(FailureClass.NUMERICAL, 0) == 0
    assert mem.discarded_untrustworthy == 1


def test_failure_memory_learns_the_direction_of_a_real_failure():
    """Thin beams fail yield; the memory should implicate `h` and say which
    way is safer."""
    space = make_space()
    mem = FailureMemory(space, min_observations=3)
    for h in (6.0, 7.0, 8.0, 9.0):
        c = Candidate(values={"w": 20.0, "h": h})
        c.result.failures = [FailureRecord(FailureClass.YIELD, metric="sf.yield_von_mises")]
        mem.observe(c)
    for h in (40.0, 45.0, 50.0, 55.0):
        c = Candidate(values={"w": 20.0, "h": h})
        mem.observe(c)
    ev = mem.evidence(FailureClass.YIELD)
    assert ev and ev[0][0] == "h"
    assert ev[0][1] < 0                       # failing h is lower than healthy h
    assert ev[0][2]["direction"] == "higher is safer"


def test_sensitivity_ranks_the_dominant_variable():
    """Section modulus goes as h^2, so h must outrank w for stress."""
    space = make_space()
    rng = random.Random(0)
    hist = []
    for _ in range(120):
        vals = space.sample(rng)
        c = Candidate(values=vals)
        c.result.metrics = analytic(vals, None)
        hist.append(c)
    s = sensitivity(hist, space, ["sf.yield_von_mises"])
    ranked = [r["variable"] for r in s["sf.yield_von_mises"]]
    assert ranked[0] == "h"
    assert all(r["n"] == 120 for r in s["sf.yield_von_mises"])


# ------------------------------------------------------------------ robustness
def test_robustness_detects_a_fragile_optimum():
    """A design sitting exactly on its constraint boundary must show a much
    worse failure fraction than one with real margin."""
    reqs = make_reqs(sf_min=2.0)
    ev = make_evaluator(reqs=reqs)
    perts = [tolerance_perturbation("h", 1.5)]

    def rate(h):
        c = Candidate(values={"w": 20.0, "h": h})
        ev.evaluate(c)
        return robustness(c, ev, perts, samples=40, seed=1).failure_rate

    marginal_h = None
    for h in [x / 2 for x in range(20, 120)]:
        c = Candidate(values={"w": 20.0, "h": h})
        ev.evaluate(c)
        if c.feasible:
            marginal_h = h
            break
    assert marginal_h is not None
    assert rate(marginal_h) > rate(marginal_h + 10.0)


def test_robustness_reports_sample_count_not_a_reliability_figure():
    ev = make_evaluator()
    c = Candidate(values={"w": 20.0, "h": 40.0})
    ev.evaluate(c)
    r = robustness(c, ev, [tolerance_perturbation("h", 1.0)], samples=15, seed=2)
    assert r.samples <= 15 and 0.0 <= r.failure_rate <= 1.0
    assert "h+/-1.0" in r.perturbations   # ASCII label: consoles are cp1252 on Windows
    assert r.fidelity == int(Fidelity.L1_GEOMETRY)


def test_stage_refusal_is_invalid_not_unknown():
    """A design-space rule violation DEFINITIVELY establishes infeasibility.

    Evaluation stops at the rule stage, so no metrics exist and every
    constraint reads UNKNOWN. That must not downgrade a known refusal into
    "we could not tell" - UNKNOWN is reserved for genuine ignorance.
    """
    from design_engine.inventor import FeasibilityRule
    space = DesignSpace(
        name="s",
        variables=[DesignVariable("w", VarType.CONTINUOUS, lo=1.0, hi=10.0),
                   DesignVariable("h", VarType.CONTINUOUS, lo=1.0, hi=10.0)],
        rules=[FeasibilityRule("h_gt_w", lambda v: v["h"] > v["w"])])
    ev = Evaluator([RuleStage(), AnalyticStage(analytic, name="a")],
                   EvalContext(space=space, requirements=make_reqs()))
    c = Candidate(values={"w": 9.0, "h": 2.0})
    ev.evaluate(c)
    assert c.result.stages[0].status is Status.INVALID
    assert c.status is Status.INVALID           # not UNKNOWN
    assert not c.feasible
    # and the optimiser must still see it as violating, so it steers away
    assert total_violation(c) > 0


def test_genuine_ignorance_still_reports_unknown():
    """The counterpart: when nothing refused but a gate could not be
    evaluated, the answer really is UNKNOWN."""
    def partial(values, ctx):
        return {"mass_kg": 1.0, "section_I_mm4": 1.0}      # no sf metric
    ev = Evaluator([AnalyticStage(partial, name="p")],
                   EvalContext(space=make_space(), requirements=make_reqs()))
    c = Candidate(values={"w": 10.0, "h": 10.0})
    ev.evaluate(c)
    assert c.status is Status.UNKNOWN


def test_repeated_identical_runs_are_bit_identical():
    """Determinism regression guard.

    During integration a run of the same config in the same process produced
    wildly different feasible counts on consecutive invocations. That was
    traced to status being computed from missing metrics, but the symptom was
    non-determinism, so it gets a permanent test: same seed, same config, same
    process, same answer. If search ever becomes non-reproducible again this
    fails immediately rather than silently degrading a design study.
    """
    space, reqs = make_space(), make_reqs()
    fingerprints = []
    for _ in range(3):
        cfg = OptimizationConfig(population=16, generations=4, seed=99)
        run = OptimizationRun(RandomSearch(space, reqs, cfg),
                              make_evaluator(), reqs, cfg).run()
        fingerprints.append([
            (c.candidate_id, c.status.value,
             round(c.result.metrics.get("mass_kg", -1), 9))
            for c in run.all_candidates])
    assert fingerprints[0] == fingerprints[1] == fingerprints[2]
