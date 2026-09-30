"""EN 1999-1-1 (Eurocode 9) 8.6.3.4 - the HAZ connection check.

WHAT THIS LIMIT STATE IS FOR
On 2026-09-30 the static gate stopped reporting a safety factor whose
denominator was a stress at a re-entrant corner, because that stress has no
converged value. That left the project with no limit state defined at a weld at
all. 8.6.3.4 supplies one, and the reason it works where the peak did not is
that it is written on a CROSS SECTION: a section resultant is fixed by
equilibrium, so it converges.

WHAT IS TESTED HERE
That the resistance is refused without a citation, that the clause's own
validity limits are enforced rather than footnoted, and that the two Eurocode
HAZ quantities are kept apart. 6.1.6 softens the PROOF strength for a member
check; 8.6.3.4 is written against the ULTIMATE strength for a connection. The
project has already been bitten once by reading one clause's number into
another's formula.

The resistance VALUES are not embedded, for the same reason the S-N detail
categories and the 6.1.6 softening factors are not. The numbers in these
fixtures are EN 1999-1-1 Table 3.2's 6061 T6/T651 sheet-and-plate row, used as
a fixture; the extruded row of the same alloy carries different HAZ values and
the engine cannot tell which product form a spec describes.
"""

import math

import pytest

from design_engine.fea import FeaError, validate_case
from design_engine.weld import (WeldError, WeldResistance, check_haz,
                                sections_from_case)

F_U_HAZ_SRC = ("EN 1999-1-1:2007+A1:2009 Table 3.2, 6061 T6/T651 sheet and "
               "plate, 12,5 < t <= 80 mm: f_u,haz = 175 N/mm2")
GAMMA_SRC = ("EN 1999-1-1:2007+A1:2009 Table 8.1, recommended value for "
             "welded connections; no National Annex applied")


def _res(**kw):
    base = dict(f_u_haz_MPa=175.0, source=F_U_HAZ_SRC,
                gamma_Mw=1.25, gamma_Mw_source=GAMMA_SRC,
                process="MIG", thickness_mm=12.0)
    base.update(kw)
    return base


def _sections():
    return [{"name": "toe_lower", "role": "HAZ T",
             "point_mm": [22.225, 0.0, 199.6], "normal": [1.0, 0.0, 0.0]},
            {"name": "fusion_lower", "role": "HAZ F",
             "point_mm": [24.0, 0.0, 199.6], "normal": [1.0, 0.0, 0.0]}]


# ----------------------------------------------------------- the resistance
def test_an_unsourced_haz_strength_is_refused():
    with pytest.raises(WeldError, match="source"):
        WeldResistance(_res(source="  "))


def test_a_missing_haz_strength_is_refused():
    spec = _res()
    del spec["f_u_haz_MPa"]
    with pytest.raises(WeldError, match="f_u_haz_MPa"):
        WeldResistance(spec)


def test_the_refusal_says_which_eurocode_quantity_is_wanted():
    """The distinction that this project has already got wrong once.

    6.1.6 gives rho_o,haz on the PROOF strength for a member; 8.6.3.4 is
    written against f_u,haz, the ULTIMATE. A message that just said 'required'
    would let the member number be pasted in here without a word.
    """
    spec = _res()
    del spec["f_u_haz_MPa"]
    with pytest.raises(WeldError) as e:
        WeldResistance(spec)
    assert "ULTIMATE" in str(e.value)
    assert "6.1.6" in str(e.value)


def test_an_unsourced_partial_factor_is_refused():
    with pytest.raises(WeldError, match="gamma_Mw_source"):
        WeldResistance(_res(gamma_Mw_source=""))


def test_a_partial_factor_below_one_is_refused():
    with pytest.raises(WeldError, match="below 1,0"):
        WeldResistance(_res(gamma_Mw=0.9))


def test_an_unknown_key_is_refused():
    with pytest.raises(WeldError, match="unexpected keys"):
        WeldResistance(_res(f_w_MPa=190.0))


# ------------------------------------------------- the validity of the table
def test_thickness_beyond_the_tabulated_range_is_refused():
    """The crossbeam this engine was built for is 15,875 mm.

    Table 3.2's HAZ columns are stated up to 15 mm. The project's own vault
    recorded that as a caveat in prose on 2026-08-28 and then went on using the
    factor anyway for a month. A caveat that cannot refuse anything is not a
    caveat.
    """
    with pytest.raises(WeldError, match="outside its stated validity"):
        WeldResistance(_res(thickness_mm=15.875))


def test_a_process_other_than_mig_is_refused():
    with pytest.raises(WeldError, match="outside its stated validity"):
        WeldResistance(_res(process="TIG"))


def test_the_refusal_names_the_condition_that_failed():
    with pytest.raises(WeldError) as e:
        WeldResistance(_res(process="TIG", thickness_mm=40.0))
    msg = str(e.value)
    assert "'TIG' is not MIG" in msg
    assert "40 mm exceeds 15 mm" in msg


def test_a_sourced_reduction_factor_admits_a_joint_outside_the_range():
    r = WeldResistance(_res(
        thickness_mm=15.875, reduction_factor=0.9,
        reduction_factor_source="test fixture, NOT a validated value"))
    assert r.f_u_haz_effective_MPa == pytest.approx(157.5)
    assert r.outside_validity  # recorded even though it was admitted


def test_an_unsourced_reduction_factor_is_refused():
    with pytest.raises(WeldError, match="reduction_factor_source"):
        WeldResistance(_res(thickness_mm=15.875, reduction_factor=0.9))


def test_a_reduction_factor_above_one_is_refused():
    with pytest.raises(WeldError, match=r"\(0, 1\]"):
        WeldResistance(_res(thickness_mm=15.875, reduction_factor=1.4,
                            reduction_factor_source="x"))


# --------------------------------------------------------------- the check
def test_the_design_resistance_is_f_u_haz_over_gamma_mw():
    r = WeldResistance(_res())
    assert r.design_MPa == pytest.approx(175.0 / 1.25)
    assert r.design_MPa == pytest.approx(140.0)


def test_the_shear_resistance_follows_8_6_2_3():
    """f_v,haz = f_u,haz / sqrt(3)."""
    r = WeldResistance(_res())
    assert r.shear_design_MPa == pytest.approx(140.0 / math.sqrt(3.0))


def test_pure_tension_reduces_to_the_tension_check():
    """(8.42)/(8.43) with tau = 0 must give back (8.38)/(8.39)."""
    r = WeldResistance(_res())
    c = check_haz(140.0, 0.0, r)
    assert c["combined_MPa"] == pytest.approx(140.0)
    assert c["utilisation"] == pytest.approx(1.0)
    assert c["passes"] is True


def test_pure_shear_reaches_utilisation_one_at_f_v_haz():
    """sqrt(3) * f_v,haz = f_u,haz, so the combined form is self-consistent."""
    r = WeldResistance(_res())
    c = check_haz(0.0, r.shear_design_MPa, r)
    assert c["utilisation"] == pytest.approx(1.0)


def test_the_check_fails_just_past_the_resistance():
    r = WeldResistance(_res())
    assert check_haz(140.1, 0.0, r)["passes"] is False
    assert check_haz(139.9, 0.0, r)["passes"] is True


def test_compression_counts_the_same_as_tension():
    """The clause squares sigma; it does not take a signed value."""
    r = WeldResistance(_res())
    assert (check_haz(-120.0, 10.0, r)["combined_MPa"]
            == pytest.approx(check_haz(120.0, 10.0, r)["combined_MPa"]))


def test_a_zero_stress_reports_an_infinite_safety_factor_not_a_crash():
    r = WeldResistance(_res())
    assert check_haz(0.0, 0.0, r)["safety_factor"] == math.inf


# -------------------------------------------------------------- the sections
def test_no_sections_is_refused():
    with pytest.raises(WeldError, match="at least one section"):
        sections_from_case([])


def test_an_unknown_role_is_refused():
    s = _sections()
    s[0]["role"] = "somewhere near the weld"
    with pytest.raises(WeldError, match="HAZ F"):
        sections_from_case(s)


def test_duplicate_section_names_are_refused():
    s = _sections()
    s[1]["name"] = s[0]["name"]
    with pytest.raises(WeldError, match="duplicate names"):
        sections_from_case(s)


def test_a_zero_normal_is_refused():
    s = _sections()
    s[0]["normal"] = [0.0, 0.0, 0.0]
    with pytest.raises(WeldError, match="defines no plane"):
        sections_from_case(s)


def test_both_roles_survive_validation():
    out = sections_from_case(_sections())
    assert [s["role"] for s in out] == ["HAZ T", "HAZ F"]


# ----------------------------------------------------------- the case gate
AL = {"name": "6061-T6511", "E_MPa": 68900.0, "nu": 0.33,
      "density_kg_m3": 2700.0, "yield_MPa": 276.0,
      "source": "test fixture"}

WELD = [{"name": "spine_pad", "factor": 0.5, "extent_mm": 25.0,
         "source": "test fixture, not a validated 6061 MIG value",
         "lines": [[[22.225, -50.0, 199.6], [22.225, 50.0, 199.6]]]}]


def _case(**over):
    ls = {"name": "weld_static", "required_SF": 1.0,
          "resistance": _res(), "sections": _sections()}
    ls.update(over.pop("limit_state", {}))
    case = {"material": dict(AL), "mesh": {"max_size_mm": 5.0},
            "constraints": [{"where": {"axis": "z", "at": "min"},
                             "dof": [1, 2, 3]}],
            "loads": [{"where": {"axis": "z", "at": "max"},
                       "force_total_N": [0.0, 0.0, 2000.0]}],
            "weld": WELD, "limit_state": ls}
    case.update(over)
    return case


def test_a_well_formed_weld_static_case_validates():
    validate_case(_case())


def test_weld_static_is_an_accepted_limit_state():
    """It is in allowed_states, and a near-miss name is not."""
    case = _case()
    case["limit_state"] = {"name": "weld_fatigue", "required_SF": 1.0}
    with pytest.raises(FeaError) as e:
        validate_case(case)
    assert "weld_static" in str(e.value)


def test_the_case_gate_refuses_an_unsourced_resistance():
    bad = _res()
    bad["source"] = ""
    with pytest.raises(FeaError, match="source"):
        validate_case(_case(limit_state={"resistance": bad}))


def test_the_case_gate_refuses_a_missing_sections_block():
    case = _case()
    del case["limit_state"]["sections"]
    with pytest.raises(FeaError, match="at least one section"):
        validate_case(case)


def test_a_haz_check_with_no_declared_weld_is_refused():
    """A connection check on a part the model does not know is welded."""
    case = _case()
    del case["weld"]
    with pytest.raises(FeaError, match="requires case.weld"):
        validate_case(case)


def test_a_required_sf_below_one_is_refused():
    """gamma_Mw already carries the margin; going under 1,0 cancels part of it."""
    with pytest.raises(FeaError, match="partial-factor"):
        validate_case(_case(limit_state={"required_SF": 0.8}))


def test_a_required_sf_above_one_is_allowed():
    """Stacking a house margin on top of the code factor is conservative.

    It is allowed, and `fea_static` records that it stacked, so the number is
    never mistaken for a bare Eurocode utilisation.
    """
    validate_case(_case(limit_state={"required_SF": 2.0}))


def test_the_thickness_refusal_reaches_the_case_gate():
    bad = _res(thickness_mm=15.875)
    with pytest.raises(FeaError, match="outside its stated validity"):
        validate_case(_case(limit_state={"resistance": bad}))


def test_other_limit_states_still_reject_the_weld_keys():
    """`resistance` and `sections` mean nothing to yield_von_mises.

    They are in _LIMIT_KEYS so weld_static can carry them, which would let them
    ride silently on any other limit state if nothing objected. Nothing did
    before this test.
    """
    case = _case(limit_state={"name": "yield_von_mises"})
    with pytest.raises(FeaError):
        validate_case(case)
