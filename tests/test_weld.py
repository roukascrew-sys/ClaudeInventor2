"""Heat-affected zones (roadmap B3).

The jetpack frame is described throughout as a welded weldment, and every
safety factor ever computed for it used the parent-metal allowable — 276 MPa
for 6061-T6511, off a supplier product page. Welding a 6xxx alloy destroys the
T6 temper locally, so that is a strength which does not exist at the joints.

As with the S-N detail categories, the softening VALUES are not embedded: they
depend on alloy, temper, process, joint type and thickness, and a wrong one is
worse than an absent one. What is tested here is that the engine demands them
properly and applies them where they actually bite.
"""

import pytest

from design_engine.weld import (HeatAffectedZone, WeldError, WeldMap,
                                from_case)

SRC = ("EN 1999-1-1:2007 s6.1.6 SHAPE; the factor here is a test fixture, "
       "not a validated 6061 MIG value")


def _zone(**kw):
    base = dict(name="z", factor=0.5, extent_mm=25.0, source=SRC,
                lines=[[[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]])
    base.update(kw)
    return HeatAffectedZone(**base)


# ------------------------------------------------------------- the refusals
def test_an_unsourced_softening_factor_is_refused():
    """Same rule as E, yield, the derating curves and the S-N categories."""
    with pytest.raises(WeldError, match="source"):
        _zone(source="   ")


def test_a_factor_of_one_is_refused_as_a_placeholder():
    """1.0 asserts welding costs no strength, which is not true of 6xxx
    aluminium. A joint with genuinely no HAZ should declare no zone."""
    with pytest.raises(WeldError, match="not true of 6xxx"):
        _zone(factor=1.0)


@pytest.mark.parametrize("bad", [0.0, -0.2, 1.5])
def test_a_factor_outside_zero_to_one_is_refused(bad):
    with pytest.raises(WeldError, match="must be in"):
        _zone(factor=bad)


def test_a_zone_with_no_extent_is_refused():
    """It would soften nothing and silently pass."""
    with pytest.raises(WeldError, match="extent_mm"):
        _zone(extent_mm=0.0)


def test_a_zone_with_no_weld_lines_is_refused():
    """The engine cannot guess where the welds are. A spec that unions two
    boxes says nothing about whether the junction is welded or machined."""
    with pytest.raises(WeldError, match="no weld lines"):
        _zone(lines=[])


def test_a_zero_length_weld_line_is_refused():
    with pytest.raises(WeldError, match="zero-length"):
        _zone(lines=[[[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]])


def test_a_malformed_weld_line_is_refused():
    with pytest.raises(WeldError):
        _zone(lines=[[[0.0, 0.0], [1.0, 0.0]]])


# --------------------------------------------------------------- geometry
def test_a_point_on_the_weld_is_in_the_zone():
    assert _zone().contains([50.0, 0.0, 0.0])


def test_a_point_beyond_the_extent_is_parent_metal():
    z = _zone(extent_mm=25.0)
    assert z.contains([50.0, 24.0, 0.0])
    assert not z.contains([50.0, 26.0, 0.0])


def test_distance_is_to_the_segment_not_its_midpoint():
    """A 240 mm weld run sampled at its midpoint would read 120 mm away from a
    peak sitting on one end of it."""
    z = _zone(lines=[[[0.0, 0.0, 0.0], [240.0, 0.0, 0.0]]])
    assert z.distance_to([240.0, 0.0, 0.0]) == pytest.approx(0.0)
    assert z.distance_to([300.0, 0.0, 0.0]) == pytest.approx(60.0)


def test_the_nearest_of_several_weld_lines_governs():
    z = _zone(lines=[[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]],
                     [[0.0, 100.0, 0.0], [10.0, 100.0, 0.0]]])
    assert z.distance_to([5.0, 95.0, 0.0]) == pytest.approx(5.0)


# ------------------------------------------------------------- the allowable
def test_parent_metal_outside_every_zone():
    wm = WeldMap([_zone()])
    r = wm.allowable_at([0.0, 500.0, 0.0], 276.0)
    assert r["in_haz"] is False
    assert r["allowable_MPa"] == 276.0
    assert r["basis"] == "parent metal"


def test_the_allowable_is_softened_inside_a_zone():
    wm = WeldMap([_zone(factor=0.5)])
    r = wm.allowable_at([50.0, 0.0, 0.0], 276.0)
    assert r["in_haz"] is True
    assert r["allowable_MPa"] == pytest.approx(138.0)
    assert "EN 1999-1-1" in r["basis"], "the answer must carry its source"


def test_overlapping_zones_take_the_WORST_softening_not_the_first():
    """A T-joint welded both sides, or a repair over an original run. Taking
    whichever was declared first would depend on list order."""
    mild = _zone(name="mild", factor=0.8)
    severe = _zone(name="severe", factor=0.45)
    for order in ([mild, severe], [severe, mild]):
        r = WeldMap(order).allowable_at([50.0, 0.0, 0.0], 276.0)
        assert r["zone"] == "severe"
        assert r["allowable_MPa"] == pytest.approx(276.0 * 0.45)


def test_a_part_with_no_welds_is_unaffected():
    """The feature must be inert for machined and bonded parts."""
    r = WeldMap([]).allowable_at([0.0, 0.0, 0.0], 276.0)
    assert r["allowable_MPa"] == 276.0
    assert r["in_haz"] is False
    assert r["nearest_haz_mm"] is None


def test_a_near_miss_reports_how_close_it_came():
    """A peak 2 mm outside the HAZ is a different situation from one 200 mm
    outside, and only the first is worth a second look."""
    r = WeldMap([_zone(extent_mm=25.0)]).allowable_at([50.0, 27.0, 0.0], 276.0)
    assert r["in_haz"] is False
    assert r["nearest_haz_mm"] == pytest.approx(27.0)


# --------------------------------------------------------------- case block
def test_a_case_without_a_weld_block_gives_an_empty_map():
    assert from_case(None).zones == []
    assert from_case([]).zones == []


def test_unknown_keys_in_a_weld_block_are_refused():
    with pytest.raises(WeldError, match="unexpected keys"):
        from_case([{"factor": 0.5, "extent_mm": 25.0, "source": SRC,
                    "lines": [[[0, 0, 0], [1, 0, 0]]], "turbo": True}])


def test_a_weld_block_missing_a_required_field_is_refused():
    with pytest.raises(WeldError, match="missing"):
        from_case([{"factor": 0.5, "lines": [[[0, 0, 0], [1, 0, 0]]]}])


# --------------------------------------------------- the jetpack regression
def test_both_jetpack_peaks_fall_inside_the_spine_pad_HAZ():
    """The finding this module produced.

    The frame's four spine/pad junction welds run along Y at |x| = 22.225,
    z = 199.6 and 250.4. BOTH recorded peaks — the sharp P0047 and the
    filleted P0048 — sit within a 25 mm HAZ of them, so both safety factors
    were computed against a parent-metal strength that does not exist there.

    The factor below is a fixture, NOT a sourced 6061 MIG value, so this test
    asserts the geometric fact (the peaks are in the zone) and the sensitivity,
    never a corrected safety factor.
    """
    welds = WeldMap([HeatAffectedZone(
        "spine-pad", factor=0.5, extent_mm=25.0, source=SRC,
        lines=[[[-22.225, -9.525, 199.6], [-22.225, 9.525, 199.6]],
               [[22.225, -9.525, 199.6], [22.225, 9.525, 199.6]],
               [[-22.225, -9.525, 250.4], [-22.225, 9.525, 250.4]],
               [[22.225, -9.525, 250.4], [22.225, 9.525, 250.4]]])])

    for peak in ([-23.505, 4.014, 199.6], [29.513, -0.024, 199.225]):
        assert welds.allowable_at(peak, 251.16)["in_haz"] is True

    # sensitivity, not a result: at a 0.5 factor the filleted frame's
    # SF 4.633 would fall below its own 3.0 gate
    softened = welds.allowable_at([29.513, -0.024, 199.225], 251.16)
    assert softened["allowable_MPa"] / 54.206986 < 3.0


# =========================================================================
# Reading f_o,haz instead of reconstructing it from rho_o,haz.
#
# EN 1999-1-1 Table 3.2b prints both, and the ratio is DERIVED from the
# strength: 115/240 = 0,479 -> 0,48. Multiplying the published ratio by a
# parent value therefore repeats a division the table has already done,
# against whatever parent figure the case happens to carry.
#
# This project did exactly that for a month: rho_o,haz = 0,48 times 276 MPa
# from a supplier page, giving 132,5 N/mm2 for a quantity the same row
# prints as 115. Reading the printed value removes the step that went wrong.
# =========================================================================

PROOF_SRC = ("EN 1999-1-1:2007+A1:2009 Table 3.2b, 6061 T6 EP/ET/ER-B, "
             "t < 25 mm: f_o,haz = 115 N/mm2")
FN4 = {"alloy_family": "precipitation_hardening", "process": "MIG",
       "thickness_mm": 15.875, "temper": "T6511"}


def _proof_zone(**kw):
    base = dict(name="z", extent_mm=25.0, source=PROOF_SRC, proof_MPa=115.0,
                lines=[[[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]])
    base.update(kw)
    return HeatAffectedZone(**base)


def test_an_absolute_proof_strength_ignores_the_parent_value():
    """The whole point: 115 N/mm2 is 115 N/mm2 whatever the case carries."""
    z = _proof_zone()
    assert z.allowable(240.0) == pytest.approx(115.0)
    assert z.allowable(276.0) == pytest.approx(115.0)
    assert z.allowable(1.0) == pytest.approx(115.0)


def test_a_ratio_still_multiplies_the_parent():
    """`factor` stays supported - some alloys publish only a ratio."""
    z = _proof_zone(proof_MPa=None, factor=0.48)
    assert z.allowable(240.0) == pytest.approx(115.2)


def test_giving_both_a_ratio_and_a_strength_is_refused():
    with pytest.raises(WeldError, match="exactly one"):
        _proof_zone(factor=0.48)


def test_giving_neither_is_refused():
    with pytest.raises(WeldError, match="exactly one"):
        _proof_zone(proof_MPa=None)


def test_the_refusal_says_which_one_to_prefer():
    with pytest.raises(WeldError) as e:
        _proof_zone(proof_MPa=None)
    assert "Prefer proof_MPa" in str(e.value)
    assert "DERIVED" in str(e.value)


def test_a_negative_proof_strength_is_refused():
    with pytest.raises(WeldError, match="must be > 0"):
        _proof_zone(proof_MPa=-5.0)


def test_a_ratio_passed_as_a_strength_is_not_silently_accepted_as_one():
    """0,48 MPa is a legal number and an absurd proof strength.

    The engine cannot tell them apart, so this test records what it does: it
    accepts it. The refusal that catches this in practice is the REQUIRED
    source, which has to name a row - and no row prints 0,48 N/mm2.
    """
    assert _proof_zone(proof_MPa=0.48).allowable(240.0) == pytest.approx(0.48)


# ---------------------------------------------- the footnote 4 reduction
def test_the_thickness_reduction_applies_to_an_absolute_strength():
    """115 x 0,8 = 92 N/mm2 at 15,875 mm - the crossbeam."""
    z = _proof_zone(reduction=dict(FN4))
    assert z.reduction_factor == pytest.approx(0.8)
    assert z.allowable(240.0) == pytest.approx(92.0)


def test_the_thickness_reduction_applies_to_a_ratio_too():
    """Footnote 4 reduces 'the HAZ values AND rho-factors' - both."""
    z = _proof_zone(proof_MPa=None, factor=0.48, reduction=dict(FN4))
    assert z.allowable(240.0) == pytest.approx(240.0 * 0.48 * 0.8)


def test_below_15mm_there_is_no_reduction():
    z = _proof_zone(reduction={**FN4, "thickness_mm": 14.9})
    assert z.allowable(240.0) == pytest.approx(115.0)


def test_an_incomplete_reduction_block_is_refused():
    with pytest.raises(WeldError, match="missing"):
        _proof_zone(reduction={"process": "MIG", "thickness_mm": 20.0})


def test_an_unknown_reduction_key_is_refused():
    with pytest.raises(WeldError, match="unexpected keys"):
        _proof_zone(reduction={**FN4, "filler": "5356"})


# ------------------------------------------------------------- governing
def test_overlapping_zones_are_ranked_by_allowable_not_by_factor():
    """A zone carrying an absolute strength has no factor to compare.

    Ranking a mixed set by factor alone would silently ignore those zones,
    which is how the softest weld stops governing.
    """
    line = [[[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]]
    soft = HeatAffectedZone(name="absolute", extent_mm=25.0, source=PROOF_SRC,
                            proof_MPa=92.0, lines=line)
    mild = HeatAffectedZone(name="ratio", extent_mm=25.0, source=PROOF_SRC,
                            factor=0.48, lines=line)
    wm = WeldMap([mild, soft])
    r = wm.allowable_at([50.0, 0.0, 0.0], 240.0)
    # 0,48 x 240 = 115,2 against 92 - the absolute zone governs.
    assert r["zone"] == "absolute"
    assert r["allowable_MPa"] == pytest.approx(92.0)


def test_the_basis_says_the_strength_was_read_not_reconstructed():
    wm = WeldMap([_proof_zone(reduction=dict(FN4))])
    r = wm.allowable_at([50.0, 0.0, 0.0], 240.0)
    assert "read directly rather than reconstructed" in r["basis"]
    assert "thickness/process reduction" in r["basis"]
    assert r["proof_MPa"] == 115.0
    assert r["factor"] is None


# ------------------------------------------------------------- from_case
def test_a_case_can_declare_an_absolute_haz_strength():
    wm = from_case([{"name": "w", "proof_MPa": 115.0, "reduction": dict(FN4),
                     "extent_mm": 25.0, "source": PROOF_SRC,
                     "lines": [[[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]]}])
    assert wm.allowable_at([50.0, 0.0, 0.0], 240.0)["allowable_MPa"] == \
        pytest.approx(92.0)


def test_a_case_with_neither_factor_nor_proof_is_refused():
    with pytest.raises(WeldError, match="exactly one"):
        from_case([{"name": "w", "extent_mm": 25.0, "source": PROOF_SRC,
                    "lines": [[[0.0, 0.0, 0.0], [100.0, 0.0, 0.0]]]}])
