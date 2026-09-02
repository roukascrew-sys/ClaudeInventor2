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


def test_report_warns_when_robustness_fidelity_is_below_the_constraint():
    """Caught in a real run: the headline safety factor came from FEA
    (SF 2.56) while the robustness sweep re-evaluated the same metric with the
    cheap analytic model (SF ~7.1). Same metric name, different model, 3x
    apart. The report must say so or a reader will believe the sweep
    validated the solver result."""
    from design_engine.inventor import explain_candidate, render_text
    from design_engine.inventor.analysis import RobustnessResult
    reqs = make_reqs(sf_min=2.5)
    c = _cand(1.0, 10.0)
    c.result.metrics["sf.yield_von_mises"] = 2.56
    c.result.metric_fidelity["sf.yield_von_mises"] = Fidelity.L3_HIGH_FEA
    c.result.apply_requirements(reqs)
    rb = RobustnessResult(
        samples=40, nominal_feasible=True, failure_rate=0.0,
        failing_classes={},
        metric_stats={"sf.yield_von_mises": {"mean": 7.14, "stdev": 0.29,
                                             "min": 6.48, "max": 7.77, "n": 40}},
        worst_case={"sf.yield_von_mises": 6.48}, perturbations=["h+/-1.0"],
        fidelity=int(Fidelity.L0_ANALYTIC))
    exp = explain_candidate(c, reqs, [c], role="balanced", robustness=rb)
    text = render_text(exp, reqs)
    assert "WARNING" in text
    assert "does NOT show that the" in text

    # and NO warning when the sweep ran at the same fidelity as the claim
    rb_same = RobustnessResult(
        samples=40, nominal_feasible=True, failure_rate=0.0, failing_classes={},
        metric_stats={"sf.yield_von_mises": {"mean": 2.6, "stdev": 0.1,
                                             "min": 2.5, "max": 2.7, "n": 40}},
        worst_case={"sf.yield_von_mises": 2.5}, perturbations=["h+/-1.0"],
        fidelity=int(Fidelity.L3_HIGH_FEA))
    text2 = render_text(explain_candidate(c, reqs, [c], robustness=rb_same), reqs)
    assert "WARNING" not in text2


def test_failed_promotion_cannot_leave_a_design_looking_validated():
    """The most dangerous failure mode this layer can have.

    Caught in a real jetpack promotion: the L3 solver stage errored out, but
    `sf.yield_von_mises` was already present from the L0 beam model, so the
    constraint evaluated VALID *at L0 fidelity* and the candidate reported
    VALID - with no solver run and no part materialised. The run printed
    "3 solved in 0.0s ... PASS".

    A stage that RAN and could not answer must degrade the result to UNKNOWN.
    """
    def l0(values, ctx):
        return {"mass_kg": 1.0, "section_I_mm4": 1.0,
                "sf.yield_von_mises": 9.0}          # optimistic cheap estimate

    def broken_solver(cand, ctx):
        raise RuntimeError("solver could not be reached")

    reqs = make_reqs(sf_min=2.0)
    ev = Evaluator([AnalyticStage(l0, name="l0"),
                    CallableStage(broken_solver, "fea", Fidelity.L3_HIGH_FEA)],
                   EvalContext(space=make_space(), requirements=reqs))
    c = Candidate(values={"w": 20.0, "h": 40.0})

    # screening only: the expensive stage never runs, so the cheap answer stands
    ev.evaluate(c, max_fidelity=Fidelity.L1_GEOMETRY)
    assert c.status is Status.VALID
    assert c.result.max_fidelity is Fidelity.L0_ANALYTIC

    # promotion: the solver stage RAN and failed -> must not still read VALID
    ev.evaluate(c, max_fidelity=Fidelity.L3_HIGH_FEA)
    assert c.status is Status.UNKNOWN
    assert not c.feasible
    assert any(not f.trustworthy for f in c.result.failures)
    # and it must not be recommendable
    assert pareto_front([c], reqs.objectives) == []


def test_promotion_spends_solver_time_where_the_answer_is_in_doubt():
    """Promotion used to sort by constraint violation, which is 0 for every
    feasible candidate, so it took an arbitrary slice. In a real jetpack run
    that picked the two chunkiest frontier members - screened at SF 14.7 and
    22.6, never in doubt - and both blew the 600s solver timeout. Two
    expensive solves, nothing learned.
    """
    reqs = make_reqs(sf_min=2.0)

    def cand_with(mass, I, sf):
        c = Candidate(values={"w": mass, "h": I})
        c.result.metrics = {"mass_kg": mass, "section_I_mm4": I,
                            "sf.yield_von_mises": sf}
        c.result.apply_requirements(reqs)
        return c

    marginal = cand_with(3.0, 300.0, 2.05)     # right on the gate: in doubt
    obvious = cand_with(9.0, 900.0, 22.0)      # nowhere near it
    light = cand_with(1.0, 100.0, 8.0)

    cfg = OptimizationConfig(population=4, generations=1, seed=0)
    run = OptimizationRun(RandomSearch(make_space(), reqs, cfg),
                          make_evaluator(), reqs, cfg)
    run.all_candidates = [obvious, light, marginal]
    order = run._promotion_order([obvious, light, marginal],
                                 [obvious, light, marginal])
    assert order[0] is marginal, "the design closest to its gate must go first"
    assert set(id(c) for c in order) == {id(obvious), id(light), id(marginal)}


# ------------------------------------------------- outsourced numerics (scipy/pymoo)
# These pin the behaviour of the three pieces handed to scipy and pymoo on
# 2026-09-02. The point of each is the same: the library must give the answer
# the hand-rolled code gave, or a strictly better-defined one — never a
# different one that merely looks plausible.

def _hv_hand_rolled_2d(points, ref):
    """The exact 2-D sweep that `hypervolume` used before it was delegated.

    Kept here as an ORACLE, not as dead code: it is the only independent
    check that swapping in pymoo did not quietly change a number the vault
    already records.
    """
    hv, prev_y = 0.0, ref[1]
    for x, y in sorted(set(points)):
        if x >= ref[0] or y >= ref[1]:
            continue
        if y < prev_y:
            hv += (ref[0] - x) * (prev_y - y)
            prev_y = y
    return hv


def test_hypervolume_agrees_with_the_hand_rolled_sweep():
    """pymoo must reproduce the previous implementation exactly in 2-D."""
    import random as _r

    from design_engine.inventor.pareto import hypervolume
    objs = make_reqs().objectives
    rng = _r.Random(11)
    for _ in range(60):
        cands = [_cand(rng.uniform(0.5, 5.0), rng.uniform(1.0, 900.0))
                 for _ in range(rng.randint(1, 12))]
        vecs = [tuple(c.result.objective_vector(objs)) for c in cands]
        ref = [max(v[i] for v in vecs) + 1.0 for i in range(2)]
        assert hypervolume(cands, objs, ref) == pytest.approx(
            _hv_hand_rolled_2d(vecs, ref), abs=1e-9)


def test_hypervolume_now_answers_for_three_objectives():
    """Used to return None for >2 objectives, so `hv or 0.0` in the benchmark
    script silently scored every three-objective run as zero."""
    from design_engine.inventor.pareto import hypervolume
    reqs = RequirementSet(
        name="three",
        objectives=[Objective("mass", "mass_kg", Sense.MIN),
                    Objective("stiffness", "section_I_mm4", Sense.MAX),
                    Objective("cost", "cost", Sense.MIN)])
    a, b = _cand(1.0, 10.0), _cand(2.0, 500.0)
    a.result.metrics["cost"] = 5.0
    b.result.metrics["cost"] = 3.0
    hv = hypervolume([a, b], reqs.objectives, [10.0, 10.0, 10.0])
    assert hv is not None and hv > 0.0


def test_hypervolume_refuses_a_mismatched_reference_point():
    """Silently padding or truncating the reference would produce a number
    that is not a hypervolume of anything."""
    from design_engine.inventor.pareto import hypervolume
    objs = make_reqs().objectives
    with pytest.raises(ValueError, match="meaningless"):
        hypervolume([_cand(1.0, 10.0)], objs, [1.0])


def test_non_dominated_sort_keeps_unrankable_candidates_last():
    """A candidate with no objective vector cannot be ranked, but selection
    still has to place it. Dropping it would silently shrink the population."""
    objs = make_reqs().objectives
    good = _cand(1.0, 10.0)
    blind = Candidate(values={"w": 1.0, "h": 1.0})
    blind.result.metrics = {"mass_kg": 0.1}        # no section_I_mm4
    blind.result.status = Status.VALID
    fronts = non_dominated_sort([blind, good], objs)
    assert good in fronts[0]
    assert fronts[-1] == [blind]
    assert sum(len(f) for f in fronts) == 2        # nothing lost


def test_sensitivity_reports_a_p_value_alongside_the_coefficient():
    space, reqs = make_space(), make_reqs()
    ev = make_evaluator(space=space, reqs=reqs)
    hist = []
    for w in range(10, 40, 3):
        for h in range(20, 50, 5):
            c = Candidate(values={"w": float(w), "h": float(h)})
            ev.evaluate(c)
            hist.append(c)
    rows = sensitivity(hist, space, ["sf.yield_von_mises"])["sf.yield_von_mises"]
    assert rows, "expected at least one ranked variable"
    for r in rows:
        assert 0.0 <= r["p_value"] <= 1.0
        assert -1.0 <= r["spearman"] <= 1.0
    # h drives bending stress as h^2; it must be the strongest AND significant
    top = rows[0]
    assert top["variable"] == "h"
    assert top["p_value"] < 0.01
    # ordering is by effect size, not by p-value
    assert [abs(r["spearman"]) for r in rows] == sorted(
        (abs(r["spearman"]) for r in rows), reverse=True)


def test_sensitivity_stays_silent_on_a_variable_that_never_moved():
    """A constant column has no ranking, so it has no rank correlation. It
    must be omitted, not reported as 0.0 — which would read as 'measured, and
    it does not matter'."""
    space, reqs = make_space(), make_reqs()
    ev = make_evaluator(space=space, reqs=reqs)
    hist = []
    for h in range(20, 60, 2):
        c = Candidate(values={"w": 20.0, "h": float(h)})   # w never varies
        ev.evaluate(c)
        hist.append(c)
    rows = sensitivity(hist, space, ["sf.yield_von_mises"])["sf.yield_von_mises"]
    assert {r["variable"] for r in rows} == {"h"}


def test_robustness_stratifies_when_every_perturbation_can():
    ev = make_evaluator()
    c = Candidate(values={"w": 20.0, "h": 40.0})
    ev.evaluate(c)
    r = robustness(c, ev, [tolerance_perturbation("h", 1.0),
                           tolerance_perturbation("w", 0.5)], samples=12, seed=3)
    assert r.sampling == "latin_hypercube"
    assert r.to_dict()["sampling"] == "latin_hypercube"


def test_robustness_falls_back_when_a_perturbation_has_no_quantile():
    """The `apply(values, rng)` contract must keep working untouched. A
    perturbation that cannot be written as an inverse CDF — a topology flip,
    a correlated pair — must not be refused, and must not be silently
    stratified as if it could be."""
    from design_engine.inventor.analysis import Perturbation
    ev = make_evaluator()
    c = Candidate(values={"w": 20.0, "h": 40.0})
    ev.evaluate(c)

    def _flip(values, rng):
        out = dict(values)
        out["h"] = float(out["h"]) + rng.choice([-2.0, 2.0])
        return out

    r = robustness(c, ev, [tolerance_perturbation("h", 1.0),
                           Perturbation("flip", _flip)], samples=8, seed=4)
    assert r.sampling == "independent_random"
    assert r.samples <= 8


def test_robustness_is_reproducible_under_a_seed_in_both_modes():
    ev = make_evaluator(reqs=make_reqs(sf_min=2.0))
    c = Candidate(values={"w": 20.0, "h": 18.5})     # near the boundary
    ev.evaluate(c)
    perts = [tolerance_perturbation("h", 1.5), tolerance_perturbation("w", 1.0)]
    for strat in (True, False):
        a = robustness(c, ev, perts, samples=16, seed=9, stratified=strat)
        b = robustness(c, ev, perts, samples=16, seed=9, stratified=strat)
        assert a.failure_rate == b.failure_rate
        assert a.sampling == b.sampling


def test_stratified_sampling_does_not_bias_the_failure_fraction():
    """LHS must estimate the SAME quantity, more precisely — not a different
    one. Measured across seeds so a lucky draw cannot carry it."""
    import statistics as _s
    ev = make_evaluator(reqs=make_reqs(sf_min=2.0))
    c = Candidate(values={"w": 20.0, "h": 18.5})
    ev.evaluate(c)
    perts = [tolerance_perturbation("h", 1.5), tolerance_perturbation("w", 1.0)]

    def means(strat):
        return [robustness(c, ev, perts, samples=16, seed=s,
                           stratified=strat).failure_rate for s in range(20)]
    lhs, indep = means(True), means(False)
    assert _s.fmean(lhs) == pytest.approx(_s.fmean(indep), abs=0.06)
    # and it should be no noisier; the measured ratio is ~0.5, this is loose
    # on purpose so a scipy QMC implementation change does not fail the suite
    assert _s.pstdev(lhs) <= _s.pstdev(indep) + 1e-9
