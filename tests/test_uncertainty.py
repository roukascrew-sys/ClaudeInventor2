"""Verdicts across a sourced range (roadmap NOW #3).

The jetpack frame's HAZ softening factor has four defensible values from four
sources, and until now one of them was silently chosen. These tests pin the
rule that makes that visible: if the verdict changes anywhere in the sourced
set, the answer is UNKNOWN rather than the nominal one.

The values are the real ones, because the regression that matters is the
frame's own.
"""

import pytest

from design_engine.uncertainty import (SourcedRange, SourcedValue,
                                       UncertaintyError, required_value,
                                       verdict_across)

# The four sourced values for rho_o,haz on welded 6061-T6, least severe first.
EC9 = SourcedValue(0.500, "EN 1999-1-1 (Eurocode 9) s6.1.6 via European "
                          "Aluminium: 0.2% proof in the HAZ is half the base "
                          "material for EN-AW 6082-T6", "Eurocode 9")
F5356 = SourcedValue(0.475, "6061-T6 MIG, 5356 filler: 19 ksi vs 40 ksi "
                            "parent", "5356 filler")
F4043 = SourcedValue(0.450, "6061-T6 MIG, 4043 filler: 18 ksi vs 40 ksi "
                            "parent", "4043 filler")
ASWELD = SourcedValue(0.375, "6061-T6 as-welded HAZ: 15 ksi vs 40 ksi parent",
                      "as-welded")


def _haz_range(nominal=0.500):
    return SourcedRange("rho_o,haz", [EC9, F5356, F4043, ASWELD],
                        nominal=nominal)


# ------------------------------------------------------------- the refusals
def test_a_value_without_a_source_is_refused():
    """Same rule as E, yield, the derating curves and the S-N categories. An
    uncited value in a sensitivity set makes the sensitivity look wider or
    narrower than the evidence supports."""
    with pytest.raises(UncertaintyError, match="source is required"):
        SourcedValue(0.42, "   ")


def test_a_nominal_outside_the_sourced_set_is_refused():
    """The important one. 0.4375 is the midpoint of 0.375 and 0.500 and looks
    balanced; no source supports it."""
    with pytest.raises(UncertaintyError, match="not one of the sourced"):
        SourcedRange("rho_o,haz", [EC9, ASWELD], nominal=0.4375)


def test_an_averaged_nominal_is_refused_even_though_it_looks_reasonable():
    """Averaging disagreeing sources produces a number that looks balanced
    and that nobody measured.

    Note the four real values are a trap for this test: their mean is 0.450,
    which IS one of them, so the refusal would not fire and the test would
    prove nothing. Three of them mean 0.4416..., which no source supports.
    """
    trio = [EC9, F5356, ASWELD]
    avg = sum(v.value for v in trio) / 3
    assert avg not in {v.value for v in trio}
    with pytest.raises(UncertaintyError, match="average of disagreeing"):
        SourcedRange("rho_o,haz", trio, nominal=avg)


def test_a_duplicated_value_is_refused():
    """Two sources agreeing belongs in the source text. A duplicated value
    silently weights that point twice."""
    twin = SourcedValue(0.500, "a second reference agreeing with Eurocode 9")
    with pytest.raises(UncertaintyError, match="appears twice"):
        SourcedRange("rho_o,haz", [EC9, twin], nominal=0.500)


def test_an_empty_range_is_refused():
    with pytest.raises(UncertaintyError, match="no values"):
        SourcedRange("rho_o,haz", [], nominal=0.5)


def test_a_bare_float_is_refused_because_it_carries_no_citation():
    with pytest.raises(UncertaintyError, match="must be a"):
        SourcedRange("rho_o,haz", [0.5, 0.375], nominal=0.5)


# ------------------------------------------------------------ the ordering
def test_most_and_least_severe_are_identified_not_assumed_from_input_order():
    r = SourcedRange("rho_o,haz", [F4043, EC9, ASWELD, F5356], nominal=0.500)
    assert r.most_severe.value == 0.375
    assert r.least_severe.value == 0.500
    assert r.span == pytest.approx(0.125)


# -------------------------------------------------------------- the verdict
def test_a_verdict_that_holds_everywhere_is_a_verdict():
    v = verdict_across(_haz_range(), lambda rho: (True, {"sf": 9.0 * rho}))
    assert v["verdict"] == "pass"
    assert "most severe" in v["reason"]
    assert v["checked"] == 4


def test_a_verdict_that_fails_everywhere_is_also_a_verdict():
    v = verdict_across(_haz_range(), lambda rho: (False, {"sf": 1.0 * rho}))
    assert v["verdict"] == "fail"
    assert "No defensible choice of source rescues this" in v["reason"]


def test_a_verdict_that_changes_inside_the_range_is_UNKNOWN():
    """The whole point.

    A frame that passes at 0.500 and fails at 0.375 has not passed. Both
    values are defensible, so reporting the nominal verdict would hide that
    the answer was chosen rather than measured.
    """
    v = verdict_across(_haz_range(), lambda rho: (rho >= 0.46, {"sf": rho}))
    assert v["verdict"] == "unknown"
    assert "undisclosed choice between sources" in v["reason"]
    # names the ADJACENT pair it turns between, not the two extremes: that
    # says where the answer lives and which source you would have to rule out
    assert "0.45 (4043 filler)" in v["reason"]
    assert "0.475 (5356 filler)" in v["reason"]
    assert "0.375" not in v["reason"], "the extremes are not the crossing"


def test_a_non_monotonic_verdict_says_so_rather_than_naming_one_crossing():
    """If the verdict flips more than once the parameter is not behaving
    monotonically, and quoting a single crossing would misrepresent it."""
    flip = {0.375: True, 0.450: False, 0.475: True, 0.500: False}
    v = verdict_across(_haz_range(), lambda rho: (flip[rho], {}))
    assert v["verdict"] == "unknown"
    assert "changes 3 times" in v["reason"]
    assert "not monotonic" in v["reason"]


def test_every_row_carries_the_source_that_justified_it():
    """A sensitivity table without citations cannot be audited."""
    v = verdict_across(_haz_range(), lambda rho: (True, {}))
    assert all(r["source"].strip() for r in v["evaluated"])
    assert {r["label"] for r in v["evaluated"]} == {
        "Eurocode 9", "5356 filler", "4043 filler", "as-welded"}


def test_a_single_source_range_says_it_is_not_a_sensitivity_check():
    """One value can never flip, so a 'pass' here proves nothing about
    robustness. Saying so is the difference between a check and a ritual."""
    only = SourcedRange("rho_o,haz", [EC9], nominal=0.500)
    v = verdict_across(only, lambda rho: (True, {}))
    assert v["verdict"] == "pass"
    assert "NOT a sensitivity check" in v["caveat"]


def test_rows_are_ordered_most_severe_first():
    v = verdict_across(_haz_range(), lambda rho: (True, {}))
    assert [r["value"] for r in v["evaluated"]] == [0.375, 0.450, 0.475, 0.500]


# ------------------------------------------------------- the required value
def test_the_value_needed_to_pass_is_derived_not_guessed():
    """SF is linear in a strength-reduction factor, so the crossing point is
    arithmetic rather than a search."""
    got = required_value(sf_at_reference=2.317, reference_value=0.500,
                         required_sf=3.0)
    assert got["required"] == pytest.approx(0.6474, abs=0.001)


def test_the_required_value_states_the_assumption_it_rests_on():
    """Linearity is true for a strength factor and false for anything that
    moves the stress field. The number must not travel without the condition."""
    got = required_value(2.317, 0.500, 3.0)
    assert "linear" in got["assumption"]
    assert "stiffness, load path or geometry" in got["assumption"]


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_non_positive_reference_is_refused(bad):
    with pytest.raises(UncertaintyError):
        required_value(2.0, bad, 3.0)


# --------------------------------------------------- the jetpack regression
def test_the_jetpack_frame_verdict_is_UNKNOWN_across_its_own_sourced_range():
    """The finding this module was built to make visible.

    Safety factor scales linearly with rho_o,haz where the peak sits inside
    the HAZ, which it does. Against the frame's own 3.0 gate, every sourced
    value fails - so this particular case is not even the interesting
    'unknown', it is a clean fail that a single chosen value was obscuring.
    """
    rng = _haz_range()
    sf_per_rho = 2.317 / 0.500          # measured at the nominal factor

    v = verdict_across(rng, lambda rho: (rho * sf_per_rho >= 3.0,
                                         {"sf": round(rho * sf_per_rho, 3)}))
    assert v["verdict"] == "fail"
    sfs = {r["label"]: r["sf"] for r in v["evaluated"]}
    assert sfs["Eurocode 9"] == pytest.approx(2.317, abs=0.01)
    assert sfs["as-welded"] == pytest.approx(1.738, abs=0.01)

    needed = required_value(2.317, 0.500, 3.0)["required"]
    assert needed > rng.least_severe.value, (
        "the frame needs a HAZ factor more generous than ANY source supports")
