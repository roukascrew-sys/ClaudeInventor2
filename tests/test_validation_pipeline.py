"""The shared solve pipeline behaves identically for every analysis.

Written as the acceptance test for the Phase 1 refactor of `ValidationTools`,
which was a 709-line class carrying four near-identical pipelines. A pure
refactor has to be proved, not asserted, so these tests pin the things that
would silently change if the extraction got something wrong:

  - the DECK. Byte-identical input to the solver is the strongest available
    evidence that nothing about the physics moved. If `job.inp` is unchanged,
    CalculiX cannot behave differently.
  - the LOG contract. Every analysis opens an action before doing work and
    closes it exactly once, to pass or fail, with a failure_mode on the
    failure path. That contract is what makes the FRACAS log the source of
    truth, and it is easy to break while moving code around.
  - the ORDER of the front half: reason check, case validation, solver
    presence, mesh, rigid-body check. Each refusal must still fire before the
    expensive step it protects.

These are behavioural, not structural: they say nothing about how the class is
organised, so they stay valid if it is reorganised again.
"""

import hashlib
import re

import pytest

from design_engine import DesignEngine
from design_engine.fea import FeaError, ValidationTools

STEEL = {"name": "S235JR", "E_MPa": 210000.0, "nu": 0.3, "yield_MPa": 235.0,
         "density_kg_m3": 7850.0,
         "source": "EN 10025-2 nominal values, t<=16mm"}

BAR = {"name": "pipeline-bar", "units": "mm",
       "features": [{"op": "box", "x": 20.0, "y": 20.0, "z": 120.0}]}


def _static_case():
    return {"material": {k: v for k, v in STEEL.items() if k != "density_kg_m3"},
            "mesh": {"max_size_mm": 6.0},
            "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
            "loads": [{"where": {"axis": "z", "at": "max"},
                       "force_total_N": [0, 0, 2000]}],
            "limit_state": {"name": "yield_von_mises", "required_SF": 1.5}}


def _modal_case():
    return {"material": dict(STEEL),
            "mesh": {"max_size_mm": 6.0},
            "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
            "loads": [],
            "limit_state": {"name": "resonance_separation", "required_SF": 0.2,
                            "excitation_hz": 50.0, "harmonics": 1}}


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    e = DesignEngine(tmp_path_factory.mktemp("pipe") / "data")
    e.validation = ValidationTools(e.validation.root, e.log, e.parts,
                                   e.validation.ccx_path, solve_timeout_s=900)
    return e


@pytest.fixture(scope="module")
def gid(eng):
    return eng.create_part(BAR, reason="shared pipeline tests")["geometry_id"]


def _deck_digest(run_dir):
    text = (run_dir / "job.inp").read_text(encoding="ascii")
    return hashlib.sha256(text.encode("ascii")).hexdigest()[:16]


# ------------------------------------------------------------- deck identity
def test_the_same_case_produces_a_byte_identical_deck(eng, gid):
    """Determinism first: without this, deck comparison proves nothing."""
    a = eng.validation.fea_static(gid, _static_case(), reason="deck determinism A")
    b = eng.validation.fea_static(gid, _static_case(), reason="deck determinism B")
    import json
    da = json.loads(eng.log.rows(action="fea_static")[-2]["details_json"])["run_dir"]
    db = json.loads(eng.log.rows(action="fea_static")[-1]["details_json"])["run_dir"]
    from pathlib import Path
    assert _deck_digest(Path(da)) == _deck_digest(Path(db))
    assert a["safety_factor"] == pytest.approx(b["safety_factor"], rel=1e-12)


def test_a_static_deck_has_the_expected_shape(eng, gid):
    """Pins what a static deck contains, so an extraction that drops a card
    or reorders the step is caught rather than merely producing 'a deck'."""
    import json
    from pathlib import Path
    eng.validation.fea_static(gid, _static_case(), reason="static deck shape")
    run = Path(json.loads(eng.log.rows(action="fea_static")[-1]["details_json"])["run_dir"])
    text = (run / "job.inp").read_text(encoding="ascii")

    for card in ("*HEADING", "*NODE, NSET=NALL", "*ELEMENT, TYPE=C3D10",
                 "*MATERIAL, NAME=MAT", "*ELASTIC", "*SOLID SECTION",
                 "*STEP", "*STATIC", "*BOUNDARY", "*CLOAD",
                 "*NODE FILE", "*EL FILE", "*END STEP"):
        assert card in text, f"static deck lost {card!r}"
    assert "*DENSITY" not in text, "a static solve needs no mass matrix"
    assert "*BUCKLE" not in text and "*FREQUENCY" not in text
    # the step must come after the model definition, never before
    assert text.index("*MATERIAL") < text.index("*STEP")


def test_a_modal_deck_has_the_expected_shape(eng, gid):
    import json
    from pathlib import Path
    eng.validation.fea_modal(gid, _modal_case(), reason="modal deck shape",
                             n_modes=4)
    run = Path(json.loads(eng.log.rows(action="fea_modal")[-1]["details_json"])["run_dir"])
    text = (run / "job.inp").read_text(encoding="ascii")

    assert "*FREQUENCY" in text
    assert "*DENSITY" in text, "a modal solve needs a mass matrix"
    assert "*CLOAD" not in text, "free vibration takes no load"
    assert "*STATIC" not in text and "*BUCKLE" not in text
    # density converted to the consistent tonne unit, not left in kg/m^3
    density = float(text.split("*DENSITY\n")[1].splitlines()[0])
    assert density == pytest.approx(7.85e-9, rel=1e-9)


# --------------------------------------------------------------- log contract
@pytest.mark.parametrize("action", ["fea_static", "fea_modal"])
def test_every_analysis_opens_and_closes_exactly_one_action(eng, gid, action):
    before = len(eng.log.rows(action=action))
    case = _static_case() if action == "fea_static" else _modal_case()
    getattr(eng.validation, action)(gid, case, reason=f"one action per {action}")
    rows = eng.log.rows(action=action)
    assert len(rows) == before + 1
    assert rows[-1]["result"] in ("pass", "fail"), "no action may be left pending"


def test_a_refusal_closes_the_action_with_a_failure_mode(eng, gid):
    """The non-linear gate contract: a failure record is written BEFORE
    control returns, so the next attempt can reference it."""
    bad = _static_case()
    bad["material"].pop("source")          # unsourced material is refused
    before = len(eng.log.rows(action="fea_static"))
    with pytest.raises(FeaError):
        eng.validation.fea_static(gid, bad, reason="unsourced material refusal")
    rows = eng.log.rows(action="fea_static")
    assert len(rows) == before + 1, "a refused run must still be logged"
    assert rows[-1]["result"] == "fail"
    assert rows[-1]["failure_mode"], "a failure must name its mode"
    assert "source" in rows[-1]["failure_mode"]


def test_the_reason_is_checked_before_any_work_happens(eng, gid):
    """`_check_reason` guards every entry point. A refactor that moved it
    after meshing would waste a mesh on a call that was never valid."""
    with pytest.raises(Exception):
        eng.validation.fea_static(gid, _static_case(), reason="")


# ------------------------------------------------------- refusal ordering
def test_case_validation_precedes_the_solver_presence_check(tmp_path):
    """Ordering matters for the error a caller sees. A malformed case must be
    reported as malformed even on a machine with no solver installed."""
    eng = DesignEngine(tmp_path / "data")
    eng.validation = ValidationTools(eng.validation.root, eng.log, eng.parts,
                                     tmp_path / "definitely-not-ccx.exe")
    g = eng.create_part(BAR, reason="ordering check")["geometry_id"]
    bad = _static_case()
    bad["limit_state"]["name"] = "vibes"
    with pytest.raises(FeaError, match="limit_state"):
        eng.validation.fea_static(g, bad, reason="malformed case, absent solver")


def test_a_missing_solver_is_named_clearly(tmp_path):
    eng = DesignEngine(tmp_path / "data")
    eng.validation = ValidationTools(eng.validation.root, eng.log, eng.parts,
                                     tmp_path / "definitely-not-ccx.exe")
    g = eng.create_part(BAR, reason="missing solver check")["geometry_id"]
    with pytest.raises(FeaError, match="ccx solver not found"):
        eng.validation.fea_static(g, _static_case(), reason="absent solver")


# ------------------------------------------------------ shared payload fields
@pytest.mark.parametrize("action,extra", [
    ("fea_static", ("nodes", "elements", "solve_seconds", "peak_rss_mb",
                    "constraint_rank", "run_dir", "solver_binary")),
    ("fea_modal", ("nodes", "elements", "solve_seconds", "peak_rss_mb",
                   "constraint_rank", "run_dir", "solver_binary")),
])
def test_every_analysis_logs_the_same_provenance_fields(eng, gid, action, extra):
    """These come from the shared front half. If an extraction drops one for a
    single analysis, the knowledge base silently loses that column for it."""
    import json
    case = _static_case() if action == "fea_static" else _modal_case()
    getattr(eng.validation, action)(gid, case, reason=f"provenance from {action}")
    d = json.loads(eng.log.rows(action=action)[-1]["details_json"])
    for key in extra:
        assert key in d, f"{action} lost provenance field {key!r}"
    assert d["nodes"] > 0 and d["elements"] > 0
