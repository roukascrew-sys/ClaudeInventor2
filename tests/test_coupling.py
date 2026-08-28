"""Declared couplings between validators (Declared Couplings, Phase 2).

The proposal named its own acceptance test:

    "the first thing it should do is refuse a result the engine currently
     reports without hesitation. If it doesn't refuse something the engine
     currently reports without hesitation, it hasn't earned the refactor."

That is `test_the_acceptance_test_fatigue_becomes_unknown` at the bottom. The
rest guards the properties that make the refusal trustworthy rather than
merely noisy:

  - a stage that declares nothing behaves exactly as before (additive)
  - an unmet dependency is UNKNOWN and untrustworthy, never INVALID — the
    design may be fine, the model was incomplete
  - a cycle is refused, not silently ordered
  - staleness changes the cache key, so a changed upstream fact cannot serve
    a stale downstream result
"""

import pytest

from design_engine.inventor import facts as F
from design_engine.inventor.candidate import (Candidate, FailureClass, Fidelity,
                                              StageResult, Status)
from design_engine.inventor.coupling import (CouplingError, CouplingGraph,
                                             FactStore)
from design_engine.inventor.evaluate import (EvalContext, EvaluationCache,
                                             Evaluator)
from design_engine.inventor.requirements import (Objective, RequirementSet,
                                                 Sense)
from design_engine.inventor.space import DesignSpace, DesignVariable, VarType


class FakeStage:
    """A stage that records that it ran, so 'did not run' is observable."""

    def __init__(self, name, *, consumes=(), produces=(), invalidates=(),
                 fidelity=Fidelity.L0_ANALYTIC, metrics=None, status=Status.VALID):
        self.name = name
        self.fidelity = fidelity
        self.consumes = frozenset(consumes)
        self.produces = frozenset(produces)
        self.invalidates = frozenset(invalidates)
        self._metrics = metrics or {}
        self._status = status
        self.ran = 0

    def config_digest(self):
        return self.name

    def run(self, cand, ctx):
        self.ran += 1
        return StageResult(stage=self.name, fidelity=self.fidelity,
                           status=self._status, metrics=dict(self._metrics))


class PlainStage(FakeStage):
    """Declares nothing at all — the pre-Phase-2 shape."""

    def __init__(self, name, **kw):
        super().__init__(name, **kw)
        del self.consumes, self.produces, self.invalidates


def _ctx():
    space = DesignSpace("coupling-test",
                        [DesignVariable("t", VarType.CONTINUOUS, lo=1.0, hi=10.0)])
    # a RequirementSet refuses to exist without an objective — rightly: with
    # none there is nothing to optimise and it is just a checker
    reqs = RequirementSet(name="coupling",
                          objectives=[Objective("light", "mass_kg", Sense.MIN)])
    return EvalContext(space=space, requirements=reqs)


def _cand():
    return Candidate(values={"t": 5.0})


# ------------------------------------------------------------- vocabulary
def test_the_fact_vocabulary_is_closed():
    """A typo in an open vocabulary is not an error — it is a dependency that
    silently never matches, which is the failure this whole feature exists to
    end."""
    with pytest.raises(F.FactError, match="unknown fact"):
        F.validate(["modal.frequencie"], "test")       # missing the s


def test_a_bare_string_is_refused_not_split_into_characters():
    with pytest.raises(F.FactError, match="single string"):
        F.validate("modal.frequencies", "test")


def test_declaring_nothing_is_legal():
    assert F.validate(None, "test") == frozenset()
    assert F.validate([], "test") == frozenset()


def test_facts_nothing_produces_carry_their_reason():
    """'No validator produces this' and 'someone forgot to declare it' look
    identical inside the graph. Only the first is a modelling gap."""
    for fact in F.UNPRODUCED_TODAY:
        assert F.why_unproduced(fact), f"{fact} is a known gap with no reason given"
    assert "damping" in F.why_unproduced("dynamics.amplification")


# ---------------------------------------------------------------- the graph
def test_a_stage_declaring_nothing_is_unaffected():
    """The additive promise: existing stages keep working untouched."""
    g = CouplingGraph([PlainStage("a"), PlainStage("b")])
    assert g.declared() is False
    assert g.order() == ["a", "b"]
    assert g.unsatisfiable() == {}


def test_order_follows_the_declared_dependencies():
    consumer = FakeStage("fatigue", consumes=["field.stress_peak"])
    producer = FakeStage("static", produces=["field.stress_peak"])
    # listed consumer-first; the graph must still put the producer first
    assert CouplingGraph([consumer, producer]).order() == ["static", "fatigue"]


def test_an_unconstrained_graph_keeps_the_listed_order():
    """No declarations must reproduce the existing ladder exactly, or this
    silently reorders every search that already works."""
    names = ["rules", "analytic", "geometry", "cost", "fea"]
    g = CouplingGraph([PlainStage(n) for n in names])
    assert g.order() == names


def test_a_cycle_is_refused_with_the_path_named():
    """Thermal -> modulus -> deflection -> contact -> thermal is a real loop.
    A DAG cannot express it and picking an order would invent an answer."""
    a = FakeStage("thermal", consumes=["field.stress_peak"],
                  produces=["material.effective"])
    b = FakeStage("structural", consumes=["material.effective"],
                  produces=["field.stress_peak"])
    with pytest.raises(CouplingError, match="cyclic coupling"):
        CouplingGraph([a, b]).order()


def test_the_cycle_message_suggests_the_honest_remedy():
    a = FakeStage("thermal", consumes=["field.stress_peak"],
                  produces=["material.effective"])
    b = FakeStage("structural", consumes=["material.effective"],
                  produces=["field.stress_peak"])
    with pytest.raises(CouplingError, match="fixed-point"):
        CouplingGraph([a, b]).order()


def test_unsatisfiable_dependencies_are_reported_before_anything_runs():
    g = CouplingGraph([FakeStage("fatigue", consumes=["dynamics.amplification"])])
    assert g.unsatisfiable() == {"fatigue": ["dynamics.amplification"]}
    rep = g.report()
    assert "damping" in rep["engine_gaps"]["dynamics.amplification"]


# ------------------------------------------------------------- the fact store
def test_a_changed_fact_changes_the_digest():
    """Staleness is computed. This is what stops a changed mass model serving
    a cached modal result."""
    s = FactStore()
    s.establish("geometry.mass_kg", 3.905)
    before = s.digest_of(["geometry.mass_kg"])
    s.establish("geometry.mass_kg", 36.35)      # attached mass now included
    assert s.digest_of(["geometry.mass_kg"]) != before


def test_consuming_nothing_gives_an_empty_digest():
    """So a stage that declares nothing keeps the exact cache key it had
    before this feature existed, and its cached results stay valid."""
    assert FactStore().digest_of([]) == ""


def test_an_invalidated_fact_stops_satisfying_its_consumers():
    s = FactStore()
    s.establish("modal.frequencies", [209.0])
    assert s.missing(["modal.frequencies"]) == []
    s.retract("modal.frequencies")
    assert s.missing(["modal.frequencies"]) == ["modal.frequencies"]


def test_an_unserialisable_fact_still_gets_a_digest():
    """A CAD solid will not serialise. The digest degrades to 'this fact
    exists' rather than raising — conservative, not wrong."""
    class Solid:
        pass
    s = FactStore()
    s.establish("geometry.solid", Solid())
    assert s.digest_of(["geometry.solid"])


# --------------------------------------------------------- evaluator wiring
def test_a_satisfied_dependency_lets_the_stage_run():
    producer = FakeStage("static", produces=["field.stress_peak"],
                         metrics={"field.stress_peak": 65.3})
    consumer = FakeStage("fatigue", consumes=["field.stress_peak"])
    ev = Evaluator([producer, consumer], _ctx(), cache=EvaluationCache())
    ev.evaluate(_cand())
    assert consumer.ran == 1


def test_an_unmet_dependency_stops_the_stage_running_at_all():
    """Not 'runs and is ignored' — does not run. A validator asked to answer
    without its inputs is how one ends up contradicting another."""
    consumer = FakeStage("fatigue", consumes=["dynamics.amplification"])
    ev = Evaluator([consumer], _ctx(), cache=EvaluationCache())
    cand = ev.evaluate(_cand())
    assert consumer.ran == 0
    assert cand.result.stages[0].status is Status.UNKNOWN


def test_an_unmet_dependency_is_unknown_and_untrustworthy_not_invalid():
    """The design may be perfectly good; the MODEL was incomplete. Marking it
    INVALID would teach failure-informed search to avoid a fine region."""
    consumer = FakeStage("fatigue", consumes=["dynamics.amplification"])
    ev = Evaluator([consumer], _ctx(), cache=EvaluationCache())
    cand = ev.evaluate(_cand())
    sr = cand.result.stages[0]
    assert sr.status is Status.UNKNOWN
    assert sr.status is not Status.INVALID
    f = sr.failures[0]
    assert f.failure_class is FailureClass.UNKNOWN
    assert f.trustworthy is False, "must stay out of failure-informed search"


def test_the_refusal_names_the_missing_fact_and_why():
    consumer = FakeStage("fatigue", consumes=["dynamics.amplification"])
    ev = Evaluator([consumer], _ctx(), cache=EvaluationCache())
    msg = ev.evaluate(_cand()).result.stages[0].failures[0].message
    assert "unmet_dependency" in msg
    assert "dynamics.amplification" in msg
    assert "damping" in msg, "a known engine gap must explain itself"


def test_a_stage_that_returned_unknown_establishes_nothing():
    """Otherwise an UNKNOWN upstream silently satisfies a downstream
    dependency, which is the original bug wearing a new hat."""
    producer = FakeStage("modal", produces=["modal.frequencies"],
                         status=Status.UNKNOWN)
    consumer = FakeStage("fatigue", consumes=["modal.frequencies"])
    ev = Evaluator([producer, consumer], _ctx(), cache=EvaluationCache())
    ev.evaluate(_cand())
    assert consumer.ran == 0


def test_a_cyclic_declaration_fails_at_construction_not_mid_population():
    """A programming error should surface when the evaluator is wired up, not
    part-way through evaluating a population."""
    a = FakeStage("thermal", consumes=["field.stress_peak"],
                  produces=["material.effective"])
    b = FakeStage("structural", consumes=["material.effective"],
                  produces=["field.stress_peak"])
    with pytest.raises(CouplingError):
        Evaluator([a, b], _ctx(), cache=EvaluationCache())


def test_the_resolution_is_reported_for_the_log():
    """'Was this computed with the attached mass?' must be a query, not an
    assumption — the same move as the vault_query receipt."""
    ev = Evaluator([FakeStage("modal", consumes=["model.attached_mass"])],
                   _ctx(), cache=EvaluationCache())
    rep = ev.coupling_report
    assert rep is not None
    assert rep["unsatisfiable"] == {"modal": ["model.attached_mass"]}
    assert "32.45 kg" in rep["engine_gaps"]["model.attached_mass"]


def test_an_undeclared_run_reports_no_coupling_at_all():
    """Runs not using the feature stay clean in the log."""
    ev = Evaluator([PlainStage("a")], _ctx(), cache=EvaluationCache())
    assert ev.coupling_report is None


# ------------------------------------------------------- THE ACCEPTANCE TEST
def test_the_acceptance_test_fatigue_becomes_unknown():
    """The proposal's own bar: the first thing Phase 2 does must be to refuse
    a result the engine currently reports without hesitation.

    `fea_fatigue` today takes its stress ratio R from the caller and computes a
    life, while the modal solve sits in the same process holding the finding
    that the frame is driven at resonance. Both are correct on their own terms
    and together they are wrong.

    Declaring that fatigue depends on `dynamics.amplification` — which nothing
    produces, because damping has never been measured — turns that confident
    number into a declared UNKNOWN that names exactly what is missing.
    """
    static = FakeStage("static", produces=["field.stress_peak"],
                       metrics={"field.stress_peak": 65.34})
    modal = FakeStage("modal", produces=["modal.frequencies"],
                      metrics={"modal.frequencies": [1639.4]})
    fatigue = FakeStage("fatigue",
                        consumes=["field.stress_peak", "modal.frequencies",
                                  "dynamics.amplification"])

    ev = Evaluator([static, modal, fatigue], _ctx(), cache=EvaluationCache())

    # the gap is visible before a single solve
    assert ev.coupling_report["unsatisfiable"] == {
        "fatigue": ["dynamics.amplification"]}

    cand = ev.evaluate(_cand())

    # the two facts it CAN get were established; the third was not
    assert static.ran == 1 and modal.ran == 1
    assert fatigue.ran == 0, "fatigue must not produce a life it cannot justify"

    sr = [s for s in cand.result.stages if s.stage == "fatigue"][0]
    assert sr.status is Status.UNKNOWN
    assert sr.provenance["unmet"] == ["dynamics.amplification"]
    assert cand.result.status is Status.UNKNOWN, (
        "a stage that ran and returned UNKNOWN must degrade the whole result")
