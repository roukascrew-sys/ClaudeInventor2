"""Modal analysis and the resonance_separation gate (roadmap B2).

The gap this closes: the jetpack frame carries four turbines at 98,000 rpm —
about 1633 Hz — bolted to a structure whose natural frequencies had never been
computed. Every static result on that frame rested on the unexamined
assumption that no mode sits near the excitation.

Most of these tests are fast because they drive the parser directly with
captured solver output. The one that needs the real solver is the closed-form
verification, and it is the important one: a modal solve that gets the UNITS
wrong returns confident, plausible, wrong numbers, and the only way to catch
that is to check it against an answer computed independently.
"""

import math
import textwrap

import pytest

from design_engine.fea import (FeaError, ValidationTools, _write_inp,
                               parse_eigenfrequencies, validate_case)

STEEL = {"name": "S235JR", "E_MPa": 210000.0, "nu": 0.3, "yield_MPa": 235.0,
         "density_kg_m3": 7850.0,
         "source": "EN 10025-2 nominal values, t<=16mm"}


def _case(**over):
    case = {"material": dict(STEEL),
            "mesh": {"max_size_mm": 5.0},
            "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
            "loads": [],
            "limit_state": {"name": "resonance_separation", "required_SF": 0.2,
                            "excitation_hz": 1633.0, "harmonics": 2}}
    case.update(over)
    return case


# ------------------------------------------------------------------ the deck
def test_density_is_converted_to_the_consistent_mass_unit(tmp_path):
    """THE UNIT TRAP. This deck is mm/N/MPa, so the consistent mass unit is the
    tonne, not the kilogram. Feeding kg/m^3 straight in makes every frequency
    wrong by sqrt(1e12) = 1e6, and the solver reports it without complaint."""
    mesh = {"node_tags": [1], "coords": [(0.0, 0.0, 0.0)],
            "connectivity": [[1] * 10]}
    inp = tmp_path / "job.inp"
    _write_inp(inp, mesh, _case(), [[1]], [], analysis="frequency", n_modes=4)
    text = inp.read_text()
    assert "*DENSITY" in text
    # 7850 kg/m^3 -> 7.85e-9 t/mm^3
    density_line = text.split("*DENSITY\n")[1].splitlines()[0]
    assert float(density_line) == pytest.approx(7.85e-9, rel=1e-9)


def test_a_static_deck_still_carries_no_density():
    """Density was previously REJECTED as a non-FEA property, and for a static
    stress solve under force boundary conditions that was right. Only the modal
    step needs a mass matrix."""
    mesh = {"node_tags": [1], "coords": [(0.0, 0.0, 0.0)],
            "connectivity": [[1] * 10]}
    import tempfile
    from pathlib import Path
    p = Path(tempfile.mkdtemp()) / "job.inp"
    case = _case(limit_state={"name": "yield_von_mises", "required_SF": 2.0},
                 loads=[{"where": {"axis": "z", "at": "max"},
                         "force_total_N": [0, 0, 100]}])
    _write_inp(p, mesh, case, [[1]], [], analysis="static")
    assert "*DENSITY" not in p.read_text()
    assert "*STATIC" in p.read_text()


def test_the_frequency_step_applies_no_load(tmp_path):
    """Free vibration depends on stiffness, mass and restraint only. A *CLOAD
    on the step would imply a dependence the solve does not have."""
    mesh = {"node_tags": [1], "coords": [(0.0, 0.0, 0.0)],
            "connectivity": [[1] * 10]}
    inp = tmp_path / "job.inp"
    _write_inp(inp, mesh, _case(), [[1]], [], analysis="frequency", n_modes=4)
    text = inp.read_text()
    assert "*FREQUENCY" in text
    assert "*CLOAD" not in text


# --------------------------------------------------------------- the parser
_DAT = textwrap.dedent("""\

                            S T E P       1


         E I G E N V A L U E   O U T P U T

     MODE NO    EIGENVALUE                       FREQUENCY
                                         REAL PART            IMAGINARY PART
                               (RAD/TIME)      (CYCLES/TIME     (RAD/TIME)

          1   0.1723545E+07   0.1312839E+04   0.2089444E+03   0.0000000E+00
          2   0.1723545E+07   0.1312839E+04   0.2089444E+03   0.0000000E+00
          3   0.6617394E+08   0.8134737E+04   0.1294687E+04   0.0000000E+00

         P A R T I C I P A T I O N   F A C T O R S

    MODE NO.   X-COMPONENT     Y-COMPONENT     Z-COMPONENT
          1  -0.3501715E-03   0.7004463E-03  -0.3502748E-03
          2  -0.6066341E-03   0.5966309E-07   0.6065745E-03
          3   0.1226201E-06   0.4906250E-06   0.1226924E-06
""")


def test_parser_reads_the_cycles_per_time_column(tmp_path):
    dat = tmp_path / "job.dat"
    dat.write_text(_DAT)
    assert parse_eigenfrequencies(dat) == pytest.approx(
        [208.9444, 208.9444, 1294.687], rel=1e-6)


def test_parser_ignores_the_participation_factor_table(tmp_path):
    """Those rows also start with a mode number and carry floats. Parsing the
    whole file found 12 'modes' where 6 were asked for, and their columns read
    as frequencies looked exactly like imaginary rigid-body modes."""
    dat = tmp_path / "job.dat"
    dat.write_text(_DAT)
    assert len(parse_eigenfrequencies(dat)) == 3      # not 6


def test_an_imaginary_frequency_is_a_refusal_not_a_zero(tmp_path):
    """A negative eigenvalue means the structure is unrestrained in that
    direction. Reporting it as a 0 Hz mode would be a quiet lie about a model
    unfit to answer the question.

    Built by line surgery rather than str.replace: the fixture is dedented, so
    a replacement written with the original indentation silently matches
    nothing and the test then asserts nothing at all.
    """
    lines = _DAT.splitlines()
    idx = next(i for i, l in enumerate(lines)
               if l.split() and l.split()[0] == "1" and "E+" in l)
    lines[idx] = ("      1  -0.5566452E+11   0.0000000E+00   0.0000000E+00"
                  "   0.2359333E+06")
    dat = tmp_path / "job.dat"
    dat.write_text("\n".join(lines))
    assert "0.2359333E+06" in dat.read_text(), "fixture surgery did not apply"

    with pytest.raises(FeaError, match="rigid_body_mode"):
        parse_eigenfrequencies(dat)


def test_a_missing_dat_file_is_named_not_silently_empty(tmp_path):
    with pytest.raises(FeaError, match="no .dat file"):
        parse_eigenfrequencies(tmp_path / "absent.dat")


# ------------------------------------------------------------- case validity
def test_resonance_separation_is_an_accepted_limit_state():
    validate_case(_case())


def test_excitation_frequency_is_required(tmp_path):
    """A separation margin is meaningless without the thing being separated
    FROM. The refusal lands in `fea_modal` rather than `validate_case`, and
    fires before the solver is touched, so this needs no solve."""
    from design_engine import DesignEngine

    eng = DesignEngine(tmp_path / "data")
    gid = eng.create_part(
        {"name": "no-excitation", "units": "mm",
         "features": [{"op": "box", "x": 10.0, "y": 10.0, "z": 60.0}]},
        reason="excitation requirement check")["geometry_id"]

    case = _case()
    del case["limit_state"]["excitation_hz"]
    with pytest.raises(FeaError, match="excitation_hz"):
        eng.validation.fea_modal(gid, case, reason="no excitation stated")


def test_density_is_required_for_a_modal_solve(tmp_path):
    """A natural frequency is sqrt(stiffness/mass); there is no mass matrix
    without density, and the engine will not assume one."""
    from design_engine import DesignEngine

    eng = DesignEngine(tmp_path / "data")
    gid = eng.create_part(
        {"name": "no-density", "units": "mm",
         "features": [{"op": "box", "x": 10.0, "y": 10.0, "z": 60.0}]},
        reason="density requirement check")["geometry_id"]

    mat = {k: v for k, v in STEEL.items() if k != "density_kg_m3"}
    with pytest.raises(FeaError, match="density_kg_m3"):
        eng.validation.fea_modal(gid, _case(material=mat),
                                 reason="no density stated")


def test_a_required_separation_above_one_is_refused():
    """required_SF here is a FRACTION (0.2 = 20% clear). A value above 1.0 is
    a stress-ratio habit applied to the wrong kind of gate."""
    with pytest.raises(FeaError, match="FRACTIONAL separation"):
        validate_case(_case(limit_state={
            "name": "resonance_separation", "required_SF": 3.0,
            "excitation_hz": 1633.0}))


def test_loads_are_refused_on_a_free_vibration_case():
    with pytest.raises(FeaError, match="free vibration takes no loads"):
        validate_case(_case(loads=[{"where": {"axis": "z", "at": "max"},
                                    "force_total_N": [0, 0, 100]}]))


def test_empty_loads_are_allowed_ONLY_for_resonance_separation():
    validate_case(_case())                       # fine
    with pytest.raises(FeaError, match="non-empty list required"):
        validate_case(_case(loads=[],
                            limit_state={"name": "yield_von_mises",
                                         "required_SF": 2.0}))


def test_constraints_are_still_required_for_a_modal_case():
    """An unrestrained body has rigid-body modes at 0 Hz and no meaningful
    separation margin."""
    with pytest.raises(FeaError, match="non-empty list required"):
        validate_case(_case(constraints=[]))


# ------------------------------------------------- closed-form verification
def _solver_available():
    from pathlib import Path
    return (Path(__file__).parent.parent / "tools" / "CalculiX-2.23.0-win-x64"
            / "bin" / "ccx.exe").is_file()


@pytest.mark.slow
@pytest.mark.skipif(not _solver_available(), reason="CalculiX not installed")
def test_modal_matches_the_euler_bernoulli_closed_form(tmp_path):
    """A cantilever's natural frequencies have an exact analytic answer, so
    this checks the whole chain — density units, deck, solver, parser — against
    something computed independently of all of it.

        f_n = (beta_n^2 / 2pi) * sqrt(E I / (rho A L^4))

    with beta_1 = 1.875104 and beta_2 = 4.694091.
    """
    from design_engine import DesignEngine

    eng = DesignEngine(tmp_path / "data")
    eng.validation = ValidationTools(
        eng.validation.root, eng.log, eng.parts, eng.validation.ccx_path,
        solve_timeout_s=900)

    b = h = 10.0
    L = 200.0
    gid = eng.create_part(
        {"name": "modal-cantilever", "units": "mm",
         "features": [{"op": "box", "x": b, "y": h, "z": L}]},
        reason="closed-form modal verification")["geometry_id"]

    out = eng.validation.fea_modal(
        gid, _case(mesh={"max_size_mm": 3.0},
                   limit_state={"name": "resonance_separation",
                                "required_SF": 0.2, "excitation_hz": 50.0,
                                "harmonics": 1}),
        reason="verify natural frequencies against Euler-Bernoulli", n_modes=6)

    freqs = out["mode_frequencies_hz"]
    I = b * h ** 3 / 12.0
    A = b * h
    rho_t = STEEL["density_kg_m3"] * 1e-12       # tonne/mm^3
    k = math.sqrt(STEEL["E_MPa"] * I / (rho_t * A * L ** 4))
    f1 = (1.875104 ** 2 / (2 * math.pi)) * k
    f2 = (4.694091 ** 2 / (2 * math.pi)) * k

    # 1st bending: within 1%. If the density units were wrong this would be
    # out by a factor of ~1e6, so the tolerance is not what makes it pass.
    assert freqs[0] == pytest.approx(f1, rel=0.01)

    # A square section bends identically in two planes, so modes pair up.
    assert freqs[1] == pytest.approx(freqs[0], rel=0.01)

    # 2nd bending sits slightly BELOW Euler-Bernoulli: the analytic model
    # neglects shear deformation and rotary inertia, which matter more as the
    # mode's wavelength shortens. Below, not above, is the physically correct
    # direction of that error.
    assert freqs[2] == pytest.approx(f2, rel=0.03)
    assert freqs[2] < f2


@pytest.mark.slow
@pytest.mark.skipif(not _solver_available(), reason="CalculiX not installed")
def test_a_mode_inside_the_separation_band_fails_the_gate(tmp_path):
    """The gate must actually bite. Excite the cantilever at its own first
    natural frequency and the run has to fail with the clash named."""
    from design_engine import DesignEngine

    eng = DesignEngine(tmp_path / "data")
    eng.validation = ValidationTools(
        eng.validation.root, eng.log, eng.parts, eng.validation.ccx_path,
        solve_timeout_s=900)
    gid = eng.create_part(
        {"name": "modal-clash", "units": "mm",
         "features": [{"op": "box", "x": 10.0, "y": 10.0, "z": 200.0}]},
        reason="resonance gate check")["geometry_id"]

    out = eng.validation.fea_modal(
        gid, _case(mesh={"max_size_mm": 3.0},
                   limit_state={"name": "resonance_separation",
                                "required_SF": 0.2, "excitation_hz": 209.0,
                                "harmonics": 1}),
        reason="excite the frame at its own first mode", n_modes=4)

    assert out["result"] == "fail"
    assert out["clashes"], "a mode sitting on the excitation must be reported"
    assert out["safety_factor"] < 0.2
    row = eng.log.rows(action="fea_modal", result="fail")[-1]
    assert "resonance_separation" in row["failure_mode"]


# ===========================================================================
# A HARMONIC IS NOT CHECKED IF NO MODE WAS COMPUTED NEAR IT.
#
# The clash search walks the modes that exist. Modes above the highest one
# computed were never asked for, so finding no clash near a harmonic proves
# nothing unless the spectrum actually reaches past that harmonic's band.
#
# Action 228 on the jetpack frame is the case in point: 20 modes topping out
# at 1668.49 Hz, recorded against harmonics at 1633.3 / 3266.7 / 4900.0 Hz
# whose 20% bands reach 1960.0 / 3920.0 / 5880.0 Hz. All three were logged as
# checked. None was covered — and even harmonic 1 was only partly seen, so its
# four clashes are a lower bound. That run failed on the clashes it did find,
# which is the only reason the hole never showed up as a false pass.
#
# Test cantilever, 10 x 10 x 200 mm S235JR: modes 209.02, 209.02, 1294.95,
# 1294.95 Hz with n_modes=4.
# ===========================================================================

@pytest.fixture(scope="module")
def modal_bar(tmp_path_factory):
    if not _solver_available():
        pytest.skip("CalculiX not installed")
    from design_engine import DesignEngine
    eng = DesignEngine(tmp_path_factory.mktemp("cov") / "data")
    eng.validation = ValidationTools(
        eng.validation.root, eng.log, eng.parts, eng.validation.ccx_path,
        solve_timeout_s=900)
    gid = eng.create_part(
        {"name": "coverage-bar", "units": "mm",
         "features": [{"op": "box", "x": 10.0, "y": 10.0, "z": 200.0}]},
        reason="harmonic coverage checks")["geometry_id"]
    return eng, gid


def _modal(eng, gid, exc, harmonics, reason, n_modes=4):
    return eng.validation.fea_modal(
        gid, _case(mesh={"max_size_mm": 3.0},
                   limit_state={"name": "resonance_separation",
                                "required_SF": 0.2, "excitation_hz": exc,
                                "harmonics": harmonics}),
        reason=reason, n_modes=n_modes)


def _last(eng):
    import json
    return json.loads(eng.log.rows(action="fea_modal")[-1]["details_json"])


def test_a_covered_harmonic_with_no_clash_still_passes(modal_bar):
    """The gate must not become unusable. 600 Hz: band 480–720, and the
    spectrum reaches 1294.95, so the clearance is genuinely established."""
    eng, gid = modal_bar
    out = _modal(eng, gid, 600.0, 1, "a harmonic the spectrum covers")
    assert out["result"] == "pass"
    d = _last(eng)
    assert d["highest_mode_hz"] == pytest.approx(1294.95, abs=1.0)
    assert [c["covered"] for c in d["harmonic_coverage"]] == [True]
    assert "coverage_undefined" not in d


def test_a_harmonic_above_the_spectrum_is_refused_not_passed(modal_bar):
    """5000 Hz is 3.9x the highest computed mode, so no clash CAN be found.

    Before 2026-10-01 this returned a pass: no clash among the modes present,
    therefore clear. It is not clear, it is unexamined.
    """
    eng, gid = modal_bar
    out = _modal(eng, gid, 5000.0, 1, "a harmonic far above the spectrum")
    assert out["result"] == "fail"
    d = _last(eng)
    assert d["clashes"] == [], "there is no clash to find up there"
    assert [c["covered"] for c in d["harmonic_coverage"]] == [False]
    cu = d["coverage_undefined"]
    assert cu["reason"] == "spectrum_stops_below_harmonic_band"
    assert cu["uncovered"][0]["band_upper_hz"] == pytest.approx(6000.0)
    assert "UNDETERMINED" in cu["note"]


def test_the_refusal_names_the_frequency_the_solve_must_reach(modal_bar):
    """A refusal that does not say what would satisfy it is a dead end."""
    eng, gid = modal_bar
    _modal(eng, gid, 5000.0, 1, "refusal message check")
    mode = eng.log.rows(action="fea_modal")[-1]["failure_mode"]
    assert mode.startswith("resonance_separation_undetermined")
    assert "6000.0 Hz" in mode
    assert "n_modes" in mode
    # It is the GATE that is inapplicable, not the structure that failed.
    assert "NOT" in mode and "the structure failing" in mode


def test_a_real_clash_still_reports_as_a_clash_and_flags_the_lower_bound(modal_bar):
    """600 Hz with 2 harmonics: h2 = 1200 Hz clashes with the 1294.95 mode
    (7.9%), AND its band reaches 1440 Hz, past the spectrum. Both are true and
    the message has to carry both without letting either hide the other."""
    eng, gid = modal_bar
    out = _modal(eng, gid, 600.0, 2, "a clash on an uncovered harmonic")
    assert out["result"] == "fail"
    d = _last(eng)
    assert d["clashes"], "harmonic 2 at 1200 Hz sits 7.9% from mode 3"
    assert [c["covered"] for c in d["harmonic_coverage"]] == [True, False]
    mode = eng.log.rows(action="fea_modal")[-1]["failure_mode"]
    assert mode.startswith("resonance_separation:")
    assert "LOWER BOUND" in mode


def test_coverage_is_decided_by_the_band_edge_not_the_excitation(modal_bar):
    """The band reaches 1.2x the harmonic, so a harmonic BELOW the highest
    mode can still be uncovered. 1100 Hz < 1294.95, but its band ends at
    1320 Hz, which is above it."""
    eng, gid = modal_bar
    out = _modal(eng, gid, 1100.0, 1, "excitation under the top mode, band over it")
    d = _last(eng)
    assert d["excitation_hz"] < d["highest_mode_hz"]
    assert d["harmonic_coverage"][0]["band_upper_hz"] == pytest.approx(1320.0)
    assert d["harmonic_coverage"][0]["covered"] is False
    assert out["result"] == "fail"
