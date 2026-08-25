"""Thermal derating (Phase 10) — verified against EN 1999-1-2:2007 Table 1a/2.

The derating data used throughout is the real Eurocode 9 table for
EN AW-6061-T6, 0,2% proof strength ratios k_0,2,theta for up to 2 hours
thermal exposure:

    20 C  100 C  150 C  200 C  250 C  300 C  350 C  550 C
    1,00  0,95   0,91   0,79   0,55   0,31   0,10   0

and modulus E_al,theta from Table 2 (70000 MPa at 20 C reference), giving
ratios 1,00 / 0,97 / 0,93 / 0,86 / 0,78 / 0,68 / 0,54 / 0.

The point of the limit state: a structure hot enough to matter must NOT be
gated on its room-temperature yield.
"""

import math

import pytest

from design_engine.fea import (FeaError, derate_factor, effective_material,
                               validate_case)

# EN 1999-1-2:2007 Table 1a, row EN AW-6061 (T6)
K02_6061_T6 = [[20, 1.00], [100, 0.95], [150, 0.91], [200, 0.79],
               [250, 0.55], [300, 0.31], [350, 0.10], [550, 0.0]]
# EN 1999-1-2:2007 Table 2, E_al,theta / 70000
KE_6XXX = [[20, 1.00], [50, 0.99], [100, 0.97], [150, 0.93], [200, 0.86],
           [250, 0.78], [300, 0.68], [350, 0.54], [400, 0.40], [550, 0.0]]

SOURCE = ("EN 1999-1-2:2007 Table 1a (k_0,2,theta, EN AW-6061 T6) and "
          "Table 2 (E_al,theta), up to 2 hours thermal exposure")


def _mat(**over):
    m = {"name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
         "source": "OnlineMetals product pages"}
    m.update(over)
    return m


def _case(mat, limit_state):
    return {
        "material": mat,
        "mesh": {"max_size_mm": 5.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, 100]}],
        "limit_state": limit_state,
    }


def test_derate_factor_hits_table_points_exactly():
    for temp, expect in K02_6061_T6:
        assert derate_factor(K02_6061_T6, temp, "ctx") == pytest.approx(expect)


def test_derate_factor_interpolates_linearly_between_points():
    # midway 200->250 C: (0,79 + 0,55)/2 = 0,67
    assert derate_factor(K02_6061_T6, 225, "ctx") == pytest.approx(0.67)
    # quarter of the way 100->150: 0,95 + 0.25*(0,91-0,95) = 0,94
    assert derate_factor(K02_6061_T6, 112.5, "ctx") == pytest.approx(0.94)


def test_derate_factor_refuses_to_extrapolate():
    """Silently extending measured material data past its range invents
    material behaviour — the exact class of quiet-wrong this engine refuses."""
    with pytest.raises(FeaError, match="outside the derating curve"):
        derate_factor(K02_6061_T6, 600, "ctx")
    with pytest.raises(FeaError, match="outside the derating curve"):
        derate_factor(K02_6061_T6, -40, "ctx")


def test_effective_material_derates_both_yield_and_modulus():
    eff = effective_material(_mat(
        service_temp_C=250, yield_derate_curve=K02_6061_T6,
        E_derate_curve=KE_6XXX, derate_source=SOURCE))
    assert eff["k_yield"] == pytest.approx(0.55)
    assert eff["yield_MPa_effective"] == pytest.approx(276 * 0.55)   # 151.8
    assert eff["k_E"] == pytest.approx(0.78)
    assert eff["E_MPa_effective"] == pytest.approx(68900 * 0.78)
    # room-temperature values are preserved alongside, not overwritten
    assert eff["yield_MPa_room"] == 276


def test_no_service_temp_means_no_derating():
    eff = effective_material(_mat())
    assert eff["k_yield"] == 1.0 and eff["k_E"] == 1.0
    assert eff["yield_MPa_effective"] == 276
    assert eff["E_MPa_effective"] == 68900


def test_service_temp_without_curve_is_refused():
    """Otherwise the temperature is silently ignored and the part is gated
    at full room-temperature strength — an unsafe pass."""
    with pytest.raises(FeaError, match="no derating curve"):
        validate_case(_case(_mat(service_temp_C=250),
                            {"name": "yield_von_mises", "required_SF": 2.0}))


def test_curve_without_service_temp_is_refused():
    with pytest.raises(FeaError, match="no service_temp_C"):
        validate_case(_case(_mat(yield_derate_curve=K02_6061_T6,
                                 derate_source=SOURCE),
                            {"name": "yield_von_mises", "required_SF": 2.0}))


def test_derating_requires_a_source():
    with pytest.raises(FeaError, match="derate_source"):
        validate_case(_case(_mat(service_temp_C=250,
                                 yield_derate_curve=K02_6061_T6),
                            {"name": "yield_von_mises", "required_SF": 2.0}))


def test_thermal_limit_state_requires_temperature_and_curve():
    with pytest.raises(FeaError, match="requires"):
        validate_case(_case(_mat(),
                            {"name": "thermal_derated_yield", "required_SF": 2.0}))


def test_non_monotonic_or_out_of_range_curves_are_refused():
    bad_order = [[20, 1.0], [200, 0.8], [100, 0.9]]
    with pytest.raises(FeaError, match="strictly increase"):
        validate_case(_case(_mat(service_temp_C=100,
                                 yield_derate_curve=bad_order,
                                 derate_source=SOURCE),
                            {"name": "thermal_derated_yield", "required_SF": 2.0}))
    bad_factor = [[20, 1.0], [200, 1.4]]
    with pytest.raises(FeaError, match="outside"):
        validate_case(_case(_mat(service_temp_C=100,
                                 yield_derate_curve=bad_factor,
                                 derate_source=SOURCE),
                            {"name": "thermal_derated_yield", "required_SF": 2.0}))


def test_valid_thermal_case_passes_validation():
    validate_case(_case(
        _mat(service_temp_C=200, yield_derate_curve=K02_6061_T6,
             E_derate_curve=KE_6XXX, derate_source=SOURCE),
        {"name": "thermal_derated_yield", "required_SF": 2.0}))


def test_derating_materially_changes_the_allowable():
    """The whole reason the limit state exists: at 250 C a 276 MPa alloy is
    a 151.8 MPa alloy, so a part sitting at 100 MPa goes from SF 2.76 (pass
    against a 2.0 gate) to SF 1.52 (fail). Same geometry, same load."""
    stress = 100.0
    room = 276 / stress
    hot = effective_material(_mat(
        service_temp_C=250, yield_derate_curve=K02_6061_T6,
        derate_source=SOURCE))["yield_MPa_effective"] / stress
    assert room == pytest.approx(2.76)
    assert hot == pytest.approx(1.518)
    assert room >= 2.0 > hot
