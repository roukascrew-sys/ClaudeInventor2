"""weld_static end to end, against an analytic section stress.

The same 10 x 10 x 100 mm bar the solver itself was first verified on, for the
same reason: sigma = F/A is known in closed form, so the number coming out of
EN 1999-1-1 8.6.3.4 can be checked rather than believed.

  F = 1000 N over 100 mm^2  ->  sigma = 10 MPa, tau = 0
  sqrt(10^2 + 0) = 10 MPa  vs  f_u,haz/gamma_Mw = 175/1,25 = 140 MPa
  -> utilisation 0,0714, SF 14,0

The point of the exercise is not the margin. It is that the number is a SECTION
resultant: it is fixed by equilibrium, it does not move when the mesh changes,
and it exists at a weld - none of which is true of the von Mises peak this
limit state was built to replace.
"""

import json

import pytest

from design_engine import DesignEngine

BAR = {"name": "weld-static-bar", "units": "mm",
       "features": [{"op": "box", "x": 10, "y": 10, "z": 100}]}

# EN AW-6061 numbers are used as the FIXTURE here on a bar that is not a real
# 6061 part; what is being verified is the arithmetic and the plumbing, not a
# design. The bar is 10 mm, inside Table 3.2's 15 mm MIG validity, so no
# reduction factor is involved and none is invented.
AL = {"name": "6061-T6 (test fixture)", "E_MPa": 68900.0, "nu": 0.33,
      "yield_MPa": 240.0,
      "source": "EN 1999-1-1:2007+A1:2009 Table 3.2, 6061 T6/T651, f_o = 240"}

RESISTANCE = {
    "f_u_haz_MPa": 175.0,
    "source": ("EN 1999-1-1:2007+A1:2009 Table 3.2, 6061 T6/T651 sheet and "
               "plate, 12,5 < t <= 80 mm: f_u,haz = 175 N/mm2"),
    "gamma_Mw": 1.25,
    "gamma_Mw_source": ("EN 1999-1-1:2007+A1:2009 Table 8.1, recommended "
                        "value; no National Annex applied"),
    "process": "MIG",
    "thickness_mm": 10.0,
}

WELD = [{"name": "girth", "factor": 0.5, "extent_mm": 25.0,
         "source": ("test fixture standing in for a sourced rho_o,haz; the "
                    "6.1.6 member softening is not what this gate uses"),
         "lines": [[[0.0, 5.0, 50.0], [10.0, 5.0, 50.0]]]}]

#: Clear of any obvious mesh plane, and clear of the load and restraint faces
#: so Saint-Venant has room - though a section resultant does not need it.
CUT_Z = 53.7
AREA = 100.0
RD = 175.0 / 1.25


def _case(force_z, required_sf=1.0):
    return {
        "material": dict(AL),
        "mesh": {"max_size_mm": 5.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, force_z]}],
        "weld": WELD,
        "limit_state": {
            "name": "weld_static",
            "required_SF": required_sf,
            "resistance": dict(RESISTANCE),
            "sections": [{"name": "girth_toe", "role": "HAZ T",
                          "point_mm": [5.0, 5.0, CUT_Z],
                          "normal": [0.0, 0.0, 1.0]}],
        },
    }


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("ws") / "data")


@pytest.fixture(scope="module")
def gid(eng):
    return eng.create_part(BAR, reason="weld_static analytic bar")["geometry_id"]


@pytest.fixture(scope="module")
def run(eng, gid):
    return eng.run_fea_static(
        gid, _case(1000), reason="verify EN 1999-1-1 8.6.3.4 against sigma=F/A")


def _row(eng, action_id):
    for r in eng.log.rows(action="fea_static"):
        if r["id"] == action_id:
            return r
    raise AssertionError(f"no fea_static row with id {action_id}")


def _details(eng, run):
    return json.loads(_row(eng, run["action_id"])["details_json"])


# ------------------------------------------------------------- the numbers
def test_the_section_area_matches_the_bar(eng, run):
    """A flat-facet cut of a prismatic box must recover 10 x 10 mm."""
    ws = _details(eng, run)["weld_static"]
    sec = ws["sections"][0]["resultant"]
    assert sec["area_mm2"] == pytest.approx(AREA, rel=2e-3)


def test_the_transmitted_force_is_the_applied_load(eng, run):
    """Free-body equilibrium, straight off the solver's nodal forces."""
    sec = _details(eng, run)["weld_static"]["sections"][0]["resultant"]
    assert sec["axial_N"] == pytest.approx(1000.0, rel=1e-4)
    assert sec["shear_N"] == pytest.approx(0.0, abs=1.0)


def test_the_section_stress_matches_F_over_A(eng, run):
    sec = _details(eng, run)["weld_static"]["sections"][0]["resultant"]
    assert sec["sigma_membrane_MPa"] == pytest.approx(10.0, rel=3e-3)
    assert sec["tau_average_MPa"] == pytest.approx(0.0, abs=0.02)


def test_the_utilisation_follows_8_43(eng, run):
    ws = _details(eng, run)["weld_static"]
    assert ws["clause"].startswith("EN 1999-1-1")
    chk = ws["sections"][0]["check"]
    assert chk["design_resistance_MPa"] == pytest.approx(RD)
    assert chk["combined_MPa"] == pytest.approx(10.0, rel=5e-3)
    assert ws["utilisation"] == pytest.approx(10.0 / RD, rel=5e-3)


def test_the_gate_passes_and_reports_the_section_safety_factor(eng, run):
    assert run["result"] == "pass"
    assert run["safety_factor"] == pytest.approx(RD / 10.0, rel=5e-3)


def test_the_allowable_returned_is_the_eurocode_resistance(eng, run):
    """Not the parent yield, and not the 6.1.6 softened proof strength."""
    assert run["allowable_MPa"] == pytest.approx(RD)


# ------------------------------------------------- what it does NOT gate on
def test_the_von_mises_peak_is_still_recorded_but_is_not_the_gate(eng, run):
    """Everything diagnostic survives; only the decision moved.

    The peak, the outlier ratio and the singularity verdict are all still in
    the log - they are how a later reader sees what the field did. What no
    longer happens is a PASS being decided by dividing an allowable by that
    peak.
    """
    d = _details(eng, run)
    assert d["max_von_mises_MPa"] > 0
    assert d["singularity"] is not None
    assert d["weld_haz"] is not None
    # The peak at the loaded face is higher than the section stress, and the
    # safety factor does not come from it.
    assert run["safety_factor"] != pytest.approx(
        run["allowable_MPa"] / d["max_von_mises_MPa"], rel=1e-3)


def test_the_result_records_what_is_not_checked(eng, run):
    """A HAZ check is not a connection design, and the log says so itself."""
    ws = _details(eng, run)["weld_static"]
    assert "8.6.3.3" in ws["not_checked"]
    assert "(8.33)" in ws["not_checked"]


def test_the_result_records_how_sigma_and_tau_were_paired(eng, run):
    """The clause does not prescribe the pairing, so the engine states its own.

    Conservative in sigma, NOT conservative in tau. A reader who cannot see
    that from the log cannot tell whether a stocky section in near-pure shear
    was checked properly.
    """
    ws = _details(eng, run)["weld_static"]
    assert "extreme-fibre" in ws["stress_pairing"]
    assert "not conservative in tau" in ws["stress_pairing"]


def test_the_governing_section_is_named(eng, run):
    ws = _details(eng, run)["weld_static"]
    assert ws["governing_section"] == "girth_toe"
    assert ws["governing_role"] == "HAZ T"


# ------------------------------------------------------------- the failure
def test_an_overloaded_joint_fails_as_a_strength_failure(eng, gid):
    """100 kN over 100 mm^2 is 1000 MPa against a 140 MPa resistance.

    And it must be recorded as a STRENGTH failure, not as the gate refusing:
    the section resultant converges, so unlike the singular-peak case there is
    a real answer here and it is a bad one.
    """
    out = eng.run_fea_static(
        gid, _case(100_000), reason="weld_static gate must fail an overload")
    assert out["result"] == "fail"
    assert out["safety_factor"] < 1.0
    row = _row(eng, out["action_id"])
    mode = row["failure_mode"]
    assert mode.startswith("weld_static:")
    assert "8.6.3.4" in mode
    assert "STRENGTH failure" in mode
    assert "gate refusal" in mode
    # The singular-peak refusal is a different mechanism and must not fire
    # here: weld_static is not in its list of limit states, and the section
    # resultant it gates on converges.
    assert "gate_undefined" not in json.loads(row["details_json"])


def test_a_stacked_house_margin_is_recorded_as_stacked(eng, gid):
    """required_SF above 1,0 sits ON TOP of gamma_Mw, and the log says so."""
    out = eng.run_fea_static(
        gid, _case(1000, required_sf=2.0),
        reason="weld_static with a house margin above the code factor")
    ws = json.loads(_row(eng, out["action_id"])["details_json"])["weld_static"]
    assert "stacked_margin" in ws
    assert "gamma_Mw=1.25" in ws["stacked_margin"]
