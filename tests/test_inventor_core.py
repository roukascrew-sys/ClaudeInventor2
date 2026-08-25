"""Domain primitives: requirements, constraints, objectives, variables, space.

Assertions are against hand-computed values, not against whatever the code
returns. The behaviours under test are the ones the whole layer depends on:

  - a missing metric is UNKNOWN, never a pass
  - a mandatory constraint blocks on UNKNOWN as well as INVALID
  - dependency-aware sampling never produces an out-of-range value
  - inactive variables are excluded from candidate identity
"""

import math
import random

import pytest

from design_engine.inventor import (Candidate, Constraint, DesignSpace,
                                    DesignVariable, FeasibilityRule, Objective,
                                    Op, Preference, ReqError, RequirementSet,
                                    Sense, Status, VarType, values_digest)


# --------------------------------------------------------------- constraints
def test_constraint_margin_and_normalized_margin():
    c = Constraint(name="mass", metric="mass_kg", op=Op.LE, bound=2.0,
                   units="kg", source="stated budget")
    r = c.evaluate({"mass_kg": 1.5})
    assert r.status is Status.VALID
    assert r.margin == pytest.approx(0.5)
    assert r.normalized_margin == pytest.approx(0.25)      # 0.5 / 2.0
    bad = c.evaluate({"mass_kg": 3.0})
    assert bad.status is Status.INVALID
    assert bad.margin == pytest.approx(-1.0)
    assert bad.normalized_margin == pytest.approx(-0.5)


def test_ge_constraint_direction():
    c = Constraint(name="sf", metric="sf.yield_von_mises", op=Op.GE, bound=2.0,
                   source="AISC-style allowable")
    assert c.evaluate({"sf.yield_von_mises": 3.0}).margin == pytest.approx(1.0)
    assert c.evaluate({"sf.yield_von_mises": 1.0}).status is Status.INVALID


def test_between_constraint():
    c = Constraint(name="band", metric="x", op=Op.BETWEEN, lo=1.0, hi=3.0,
                   source="interface spec")
    assert c.evaluate({"x": 2.0}).status is Status.VALID
    assert c.evaluate({"x": 2.0}).margin == pytest.approx(1.0)
    assert c.evaluate({"x": 3.5}).status is Status.INVALID


def test_missing_metric_is_unknown_not_pass():
    """The single most important rule in the constraint system."""
    c = Constraint(name="sf", metric="sf.thermal_derated_yield", op=Op.GE,
                   bound=3.0, source="engineering judgment")
    r = c.evaluate({"mass_kg": 1.0})
    assert r.status is Status.UNKNOWN
    assert r.status is not Status.VALID
    assert r.blocking is True          # mandatory + unknown must block


def test_none_valued_metric_is_unknown():
    c = Constraint(name="sf", metric="sf.x", op=Op.GE, bound=2.0, source="s")
    assert c.evaluate({"sf.x": None}).status is Status.UNKNOWN


def test_advisory_constraint_does_not_block():
    c = Constraint(name="nice", metric="m", op=Op.LE, bound=1.0,
                   severity="advisory")
    assert c.evaluate({"m": 5.0}).status is Status.INVALID
    assert c.evaluate({"m": 5.0}).blocking is False


def test_mandatory_constraint_requires_a_source():
    with pytest.raises(ReqError, match="source"):
        Constraint(name="sf", metric="m", op=Op.GE, bound=2.0)


# --------------------------------------------------------------- objectives
def test_objective_loss_directions():
    assert Objective("m", "m", Sense.MIN).loss(3.0) == 3.0
    assert Objective("m", "m", Sense.MAX).loss(3.0) == -3.0
    assert Objective("m", "m", Sense.TARGET, target=5.0).loss(3.0) == 2.0
    rng = Objective("m", "m", Sense.RANGE, lo=1.0, hi=4.0)
    assert rng.loss(2.0) == 0.0 and rng.loss(0.0) == 1.0 and rng.loss(6.0) == 2.0


def test_objective_loss_of_missing_is_none():
    assert Objective("m", "m", Sense.MIN).loss(None) is None


def test_requirement_set_rejects_duplicate_names_and_empty_objectives():
    o = Objective("mass", "mass_kg", Sense.MIN)
    with pytest.raises(ReqError, match="at least one objective"):
        RequirementSet(name="r", objectives=[])
    with pytest.raises(ReqError, match="duplicate"):
        RequirementSet(name="r", objectives=[o],
                       preferences=[Preference("mass", "mass_kg", Sense.MIN)])


def test_requirement_set_digest_is_stable_and_sensitive():
    def build(bound):
        return RequirementSet(
            name="r",
            constraints=[Constraint("m", "mass_kg", Op.LE, bound, source="s")],
            objectives=[Objective("mass", "mass_kg", Sense.MIN)])
    assert build(2.0).digest() == build(2.0).digest()
    assert build(2.0).digest() != build(2.5).digest()


# ------------------------------------------------------------ design space
def _space():
    return DesignSpace(name="s", variables=[
        DesignVariable("t", VarType.CONTINUOUS, lo=1.5, hi=8.0, step=0.5,
                       units="mm", path="features.0.z"),
        DesignVariable("n", VarType.INTEGER, lo=1, hi=5),
        DesignVariable("mat", VarType.CATEGORICAL, values=["6061", "7075"]),
    ])


def test_quantize_snaps_to_step_and_clamps():
    v = _space().by_name("t")
    assert v.quantize(3.3, {}) == pytest.approx(3.5)
    assert v.quantize(99.0, {}) == pytest.approx(8.0)
    assert v.quantize(-5.0, {}) == pytest.approx(1.5)


def test_sampling_respects_bounds_and_types():
    sp, rng = _space(), random.Random(0)
    for _ in range(200):
        vals = sp.sample(rng)
        assert 1.5 <= vals["t"] <= 8.0
        assert vals["n"] in (1, 2, 3, 4, 5) and isinstance(vals["n"], int)
        assert vals["mat"] in ("6061", "7075")


def test_dependent_bounds_are_honoured():
    """Titanium allows a thinner wall than aluminium — the classic case where
    a flat parameter dict silently generates impossible designs."""
    sp = DesignSpace(name="dep", variables=[
        DesignVariable("mat", VarType.CATEGORICAL, values=["alu", "ti"]),
        DesignVariable("wall", VarType.CONTINUOUS,
                       bounds_from=lambda v: (1.0, 4.0) if v["mat"] == "ti" else (2.5, 8.0)),
    ])
    rng = random.Random(1)
    for _ in range(200):
        vals = sp.sample(rng)
        lo, hi = (1.0, 4.0) if vals["mat"] == "ti" else (2.5, 8.0)
        assert lo <= vals["wall"] <= hi


def test_inactive_variable_is_excluded_from_identity():
    """Two designs differing only in a variable neither uses are the SAME
    design and must share a cache entry."""
    sp = DesignSpace(name="cond", variables=[
        DesignVariable("ribbed", VarType.CATEGORICAL, values=[False, True]),
        DesignVariable("rib_h", VarType.CONTINUOUS, lo=1.0, hi=10.0,
                       active_if=lambda v: v.get("ribbed") is True),
    ])
    a = sp.resolve({"ribbed": False, "rib_h": 3.0})
    b = sp.resolve({"ribbed": False, "rib_h": 9.0})
    assert "rib_h" not in a and "rib_h" not in b
    assert values_digest(a) == values_digest(b)
    c = sp.resolve({"ribbed": True, "rib_h": 3.0})
    assert "rib_h" in c and values_digest(c) != values_digest(a)


def test_derived_variable_is_computed_not_sampled():
    sp = DesignSpace(name="der", variables=[
        DesignVariable("w", VarType.CONTINUOUS, lo=10.0, hi=20.0),
        DesignVariable("h", VarType.CONTINUOUS, lo=10.0, hi=20.0),
        DesignVariable("area", VarType.DERIVED,
                       compute=lambda v: v["w"] * v["h"]),
    ])
    vals = sp.resolve({"w": 12.0, "h": 5.0})
    assert vals["area"] == pytest.approx(12.0 * 10.0)   # h clamped to lo=10
    assert [v.name for v in sp.searchable] == ["w", "h"]


def test_invalid_choice_is_repaired_after_a_dependency_change():
    sp = DesignSpace(name="rep", variables=[
        DesignVariable("proc", VarType.CATEGORICAL, values=["mill", "cast"]),
        DesignVariable("finish", VarType.CATEGORICAL,
                       values_from=lambda v: (["as-machined", "anodised"]
                                              if v["proc"] == "mill" else ["as-cast"])),
    ])
    vals = sp.resolve({"proc": "cast", "finish": "anodised"})
    assert vals["finish"] == "as-cast"


def test_feasibility_rules_and_unevaluatable_rule_is_a_violation():
    sp = DesignSpace(
        name="r",
        variables=[DesignVariable("a", VarType.CONTINUOUS, lo=0.0, hi=10.0),
                   DesignVariable("b", VarType.CONTINUOUS, lo=0.0, hi=10.0)],
        rules=[FeasibilityRule("a_lt_b", lambda v: v["a"] < v["b"]),
               FeasibilityRule("explodes", lambda v: v["missing"] > 0)])
    assert "a_lt_b" not in sp.violations({"a": 1.0, "b": 2.0})
    assert "a_lt_b" in sp.violations({"a": 5.0, "b": 2.0})
    # a rule that raises must count as violated, never as satisfied
    assert "explodes" in sp.violations({"a": 1.0, "b": 2.0})


def test_to_spec_changes_only_maps_variables_with_paths():
    sp = _space()
    assert sp.to_spec_changes({"t": 4.0, "n": 2, "mat": "6061"}) == {"features.0.z": 4.0}


def test_space_digest_changes_with_bounds():
    a = DesignSpace(name="s", variables=[DesignVariable("t", VarType.CONTINUOUS, lo=1.0, hi=2.0)])
    b = DesignSpace(name="s", variables=[DesignVariable("t", VarType.CONTINUOUS, lo=1.0, hi=3.0)])
    assert a.digest() != b.digest()


# ---------------------------------------------------------------- candidate
def test_candidate_identity_is_content_addressed():
    a = Candidate(values={"t": 3.0, "n": 2})
    b = Candidate(values={"n": 2, "t": 3.0})       # key order must not matter
    assert a.candidate_id == b.candidate_id


def test_candidate_lineage_is_preserved():
    root = Candidate(values={"t": 3.0})
    kid = root.child({"t": 4.0}, "mutate", "increase thickness after yield fail")
    assert kid.parent_id == root.candidate_id
    assert kid.generation == root.generation + 1
    assert kid.operator == "mutate" and "yield" in kid.reason


def test_apply_requirements_blocks_on_hard_failure_regardless_of_objectives():
    """Principle 2: a great objective value cannot buy off a safety gate."""
    reqs = RequirementSet(
        name="r",
        constraints=[Constraint("sf", "sf.yield_von_mises", Op.GE, 2.0,
                                source="stated gate")],
        objectives=[Objective("mass", "mass_kg", Sense.MIN)])
    c = Candidate(values={"t": 1.0})
    c.result.metrics = {"mass_kg": 0.001, "sf.yield_von_mises": 0.5}
    c.result.apply_requirements(reqs)
    assert c.status is Status.INVALID and not c.feasible


def test_apply_requirements_unknown_when_a_gate_was_not_evaluated():
    reqs = RequirementSet(
        name="r",
        constraints=[Constraint("sf", "sf.yield_von_mises", Op.GE, 2.0, source="s")],
        objectives=[Objective("mass", "mass_kg", Sense.MIN)])
    c = Candidate(values={})
    c.result.metrics = {"mass_kg": 1.0}
    c.result.apply_requirements(reqs)
    assert c.status is Status.UNKNOWN and not c.feasible


def test_objective_vector_is_none_when_incomplete():
    objs = [Objective("mass", "mass_kg", Sense.MIN),
            Objective("cost", "cost_usd", Sense.MIN)]
    c = Candidate(values={})
    c.result.metrics = {"mass_kg": 1.0}
    assert c.result.objective_vector(objs) is None
    c.result.metrics["cost_usd"] = 5.0
    assert c.result.objective_vector(objs) == [1.0, 5.0]
