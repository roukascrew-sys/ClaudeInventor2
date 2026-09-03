"""The equation solver is a choice, and it has to be a recorded one.

Until 2026-09-03 this deck writer emitted no `SOLVER=` card at all, so every
run in the project's history used CalculiX's default DIRECT factorisation. A
direct solve stores the factor, and its fill-in - not the matrix - is what has
been ending runs here: 442,725 / 528,439 / 642,603-node solves all died at
0xC0000005 reaching for 7-9 GB.

An iterative solver trades that memory for iterations. The trade is only worth
taking if the answer is the same, and CalculiX does NOT fail loudly when PCG
under-converges: it returns a plausible displacement field and exits clean. So
the deck-level tests below pin what is emitted, and the solve-level one pins
that the answers agree.
"""

import math

import pytest

from design_engine import DesignEngine, _DEFAULT_CCX
from design_engine.fea import FeaError, _SOLVERS, _write_inp

AL = {"name": "6061-T6", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
      "source": "MMPDS-2023 nominal, solver fixture only"}
W = H = 40.0
L = 400.0
P = 2000.0
#: Euler-Bernoulli tip deflection, the independent check on all three solvers.
D_THEORY = P * L ** 3 / (3.0 * AL["E_MPa"] * (W * H ** 3 / 12.0))


def _mesh():
    """Two tetrahedra is enough: these tests read the deck, not the answer."""
    return {"node_tags": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            "coords": [(0, 0, 0), (1, 0, 0), (0, 1, 0), (0, 0, 1),
                       (0.5, 0, 0), (0.5, 0.5, 0), (0, 0.5, 0),
                       (0, 0, 0.5), (0.5, 0, 0.5), (0, 0.5, 0.5)],
            "connectivity": [[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]]}


def _case():
    return {"material": dict(AL), "mesh": {"max_size_mm": 5.0},
            "constraints": [{"where": {"axis": "z", "at": "min"},
                             "dof": [1, 2, 3]}],
            "loads": [{"where": {"axis": "z", "at": "max"},
                       "force_total_N": [0.0, P, 0.0]}],
            "limit_state": {"name": "yield_von_mises", "required_SF": 1.0}}


def _deck(tmp_path, **kw):
    path = tmp_path / "job.inp"
    _write_inp(path, _mesh(), _case(), [[1]], [], **kw)
    return path.read_text(encoding="utf-8")


def test_the_default_deck_carries_no_solver_card(tmp_path):
    """Every deck this project has ever written looked like this, so the
    default must stay byte-identical or no recorded result reproduces."""
    text = _deck(tmp_path)
    assert "*STATIC\n" in text or text.rstrip().endswith("*STATIC")
    assert "SOLVER=" not in text


@pytest.mark.parametrize("name, card", [
    ("iterative_scaling", "*STATIC, SOLVER=ITERATIVE SCALING"),
    ("iterative_cholesky", "*STATIC, SOLVER=ITERATIVE CHOLESKY"),
])
def test_an_iterative_solver_is_requested_on_the_static_card(tmp_path, name, card):
    assert card in _deck(tmp_path, solver=name)


def test_an_unknown_solver_is_refused(tmp_path):
    """Silently falling back to the default would mean a run recorded as
    iterative was actually direct."""
    with pytest.raises(FeaError, match="unknown solver"):
        _deck(tmp_path, solver="pardiso_but_misspelled")


def test_every_named_solver_can_actually_be_written(tmp_path):
    for name in _SOLVERS:
        _deck(tmp_path, solver=name)


@pytest.mark.skipif(not _DEFAULT_CCX.is_file(),
                    reason=f"CalculiX not installed at {_DEFAULT_CCX}")
def test_the_iterative_solvers_agree_with_the_direct_one(tmp_path):
    """The trade is worthless unless the answer survives it.

    Measured 2026-09-03 on a 164,640-node mesh of this beam: tip displacement
    identical to 0.000% for both iterative solvers, peak stress within 0.056%
    (cholesky) and 0.001% (scaling), at 12.3% of the direct solver's memory.
    This runs a coarse version of that so the suite stays quick.
    """
    eng = DesignEngine(tmp_path / "data")
    gid = eng.create_part({
        "name": "solver-agreement-beam", "units": "mm", "density_kg_m3": 2700,
        "features": [{"op": "box", "x": W, "y": H, "z": L}],
    }, reason="beam for a direct-vs-iterative solver agreement check"
    )["geometry_id"]

    case = _case()
    case["mesh"]["max_size_mm"] = 10.0
    out = {}
    for name in ("direct", "iterative_cholesky", "iterative_scaling"):
        out[name] = eng.validation.fea_static(
            gid, case, reason=f"solver agreement check using {name}",
            solver=name)
        assert out[name]["solver_equations"] == name

    base = out["direct"]
    # Sanity: the direct solve must itself be near the closed form, or the
    # agreement below would only prove the solvers are consistently wrong.
    assert base["max_displacement_mm"] == pytest.approx(D_THEORY, rel=0.08)

    for name in ("iterative_cholesky", "iterative_scaling"):
        assert out[name]["max_displacement_mm"] == pytest.approx(
            base["max_displacement_mm"], rel=1e-3), (
            f"{name} disagrees on displacement, which is what an "
            f"under-converged PCG looks like")
        assert out[name]["max_von_mises_MPa"] == pytest.approx(
            base["max_von_mises_MPa"], rel=0.02)


@pytest.mark.skipif(not _DEFAULT_CCX.is_file(),
                    reason=f"CalculiX not installed at {_DEFAULT_CCX}")
def test_the_solver_choice_reaches_the_log_not_just_the_return(tmp_path):
    """A number obtained iteratively must be distinguishable from a direct one
    forever, not only in the process that produced it."""
    import json
    eng = DesignEngine(tmp_path / "data")
    gid = eng.create_part({
        "name": "solver-provenance-beam", "units": "mm",
        "density_kg_m3": 2700,
        "features": [{"op": "box", "x": W, "y": H, "z": L}],
    }, reason="beam for checking the solver choice is logged")["geometry_id"]

    case = _case()
    case["mesh"]["max_size_mm"] = 12.0
    r = eng.validation.fea_static(gid, case,
                                  reason="logging check, iterative cholesky",
                                  solver="iterative_cholesky")
    row = [x for x in eng.log.rows(action="fea_static")
           if x["id"] == r["action_id"]][0]
    assert json.loads(row["details_json"])["solver_equations"] == \
        "iterative_cholesky"
