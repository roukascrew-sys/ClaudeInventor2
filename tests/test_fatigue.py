"""Fatigue life against a sourced S-N curve (roadmap B1).

Fatigue at 98,000 rpm is on the vault's list of what the jetpack design does
not cover, under the heading "those are what actually kill jetpack pilots".
B2 made it sharper: a mode sits 0.4% from the shaft frequency, so the frame is
driven at resonance, and at 1633 Hz it accumulates 5.88 million cycles an hour.

The tests that matter are the refusals, because every one of them corresponds
to a way of producing a confident number that is wrong:

  - assuming an endurance limit that aluminium does not have
  - extrapolating an S-N curve past its data
  - computing life from a stress peak that sits on a singularity
  - inventing a cycle count or cycle shape the caller never stated
"""

import math

import pytest

from design_engine.fatigue import (FatigueError, SNCurve, cycles_from_exposure,
                                   miner_damage, stress_range_from_ratio)

SRC = "EN 1999-1-3:2007 curve SHAPE; category value is a test fixture, not a real detail"


def _curve(**over):
    kw = {"name": "test-detail", "detail_category_MPa": 40.0, "slope_m": 3.4,
          "source": SRC, "endurance_limit_MPa": None}
    kw.update(over)
    return SNCurve(**kw)


# ------------------------------------------------------------- the refusals
def test_an_unstated_endurance_limit_is_refused():
    """Aluminium has NO endurance limit and steel does. Defaulting either way
    silently decides whether the part can last forever."""
    with pytest.raises(FatigueError, match="must be stated explicitly"):
        SNCurve("no-limit-stated", 40.0, 3.4, SRC)


def test_stating_none_is_accepted_and_means_life_is_always_finite():
    c = _curve(endurance_limit_MPa=None)
    assert c.endurance_limit_MPa is None
    assert c.to_dict()["has_endurance_limit"] is False
    # even a small range gives a finite answer, until the curve runs out of data
    assert math.isfinite(c.allowable_cycles(20.0))


def test_a_stated_endurance_limit_is_honoured():
    c = _curve(endurance_limit_MPa=15.0)
    assert c.allowable_cycles(14.0) == math.inf
    assert math.isfinite(c.allowable_cycles(40.0))


def test_unsourced_fatigue_data_is_refused():
    """Same rule the engine already applies to E, yield and derating curves."""
    with pytest.raises(FatigueError, match="source"):
        SNCurve("unsourced", 40.0, 3.4, "   ", endurance_limit_MPa=None)


def test_extrapolation_beyond_the_curve_is_refused():
    """'Very long life' and 'infinite life' are different claims and only one
    of them is safe. Beyond the data the curve cannot tell them apart."""
    c = _curve()
    with pytest.raises(FatigueError, match="will not extrapolate"):
        c.allowable_cycles(5.0)


def test_the_refusal_says_whether_an_endurance_limit_exists():
    """A reader hitting the extrapolation refusal needs to know whether a very
    small range would have been safe anyway."""
    with pytest.raises(FatigueError, match="NO endurance limit"):
        _curve(endurance_limit_MPa=None).allowable_cycles(5.0)


def test_the_low_cycle_regime_is_refused_not_silently_answered():
    """Below ~1e4 cycles plastic strain governs and stress-life does not
    apply. Returning a number there would be answering a different question."""
    with pytest.raises(FatigueError, match="low-cycle regime"):
        _curve().allowable_cycles(400.0)


def test_a_zero_stress_range_is_not_a_load_cycle():
    with pytest.raises(FatigueError):
        _curve().allowable_cycles(0.0)


# ------------------------------------------------------------- the arithmetic
def test_the_reference_point_reproduces_itself():
    """At the detail category the curve must return its reference life. This
    is the one point where the answer is fixed by definition."""
    c = _curve(detail_category_MPa=40.0, reference_cycles=2e6)
    assert c.allowable_cycles(40.0) == pytest.approx(2e6, rel=1e-9)


def test_the_power_law_has_the_stated_slope():
    """Halving the range must multiply life by 2**m."""
    c = _curve(slope_m=3.4)
    n1 = c.allowable_cycles(40.0)
    n2 = c.allowable_cycles(20.0)
    assert n2 / n1 == pytest.approx(2.0 ** 3.4, rel=1e-9)


def test_allowable_range_inverts_allowable_cycles():
    c = _curve()
    for rng in (20.0, 33.0, 55.0):
        assert c.allowable_range(c.allowable_cycles(rng)) == pytest.approx(
            rng, rel=1e-9)


def test_fully_reversed_loading_doubles_the_range():
    """R = -1 is the case most often got wrong. At a slope of 3.4 the factor
    of 2 in range is a factor of 10.6 in life."""
    assert stress_range_from_ratio(50.0, -1.0) == 100.0
    assert stress_range_from_ratio(50.0, 0.0) == 50.0
    c = _curve()
    assert (c.allowable_cycles(50.0) / c.allowable_cycles(100.0)
            == pytest.approx(2.0 ** 3.4, rel=1e-9))


def test_a_constant_stress_is_not_a_fatigue_case():
    with pytest.raises(FatigueError, match="no cycling"):
        stress_range_from_ratio(50.0, 1.0)


# --------------------------------------------------------------- the exposure
def test_cycles_per_hour_at_the_jetpack_shaft_frequency():
    """The number that makes resonance a structural problem rather than a
    comfort one: 98,000 rpm is 1633.3 Hz, and an hour of that is 5.88 million
    cycles. Ten minutes is a million."""
    assert cycles_from_exposure(98000 / 60.0, 1.0) == pytest.approx(5.88e6,
                                                                    rel=0.001)
    assert cycles_from_exposure(98000 / 60.0, 1 / 6.0) == pytest.approx(9.8e5,
                                                                        rel=0.01)


# ------------------------------------------------------------------- Miner
def test_miner_sums_damage_across_blocks():
    c = _curve()
    n = c.allowable_cycles(40.0)
    out = miner_damage(c, [(40.0, n / 2), (40.0, n / 4)])
    assert out["damage"] == pytest.approx(0.75, rel=1e-9)
    assert out["survives"] is True


def test_miner_predicts_failure_at_unit_damage():
    c = _curve()
    n = c.allowable_cycles(40.0)
    assert miner_damage(c, [(40.0, n)])["survives"] is False


def test_blocks_below_a_stated_endurance_limit_do_no_damage():
    c = _curve(endurance_limit_MPa=15.0)
    out = miner_damage(c, [(14.0, 1e12)])
    assert out["damage"] == 0.0
    assert out["blocks"][0]["allowable_cycles"] == "inf"


def test_miner_names_its_own_limitation():
    """Linear damage ignores sequence effects and can be non-conservative. The
    result says so rather than presenting itself as a prediction."""
    out = miner_damage(_curve(), [(40.0, 1000.0)])
    assert "non-conservative" in out["method"]
    assert out["curve"]["source"] == SRC


def test_an_empty_spectrum_is_refused():
    with pytest.raises(FatigueError):
        miner_damage(_curve(), [])
