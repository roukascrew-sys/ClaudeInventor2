"""Elastic buckling (Phase 9) — verified against the Euler closed form.

For a prismatic column, P_cr = pi^2 * E * I / (K*L)^2, with K the effective
length factor set by the end conditions:

    pinned-pinned   K = 1.0
    fixed-pinned    K = 0.6992   (P_cr = 2.046 * pi^2*E*I/L^2)

In a 3D solid model these map onto face restraints exactly as established by
the beam work in this project: restraining x,y on an end face is a PIN (a
section rotation there is purely axial, so it is not clamped), while adding
z across that whole face makes it FIXED (it prevents that axial motion and so
clamps the rotation).

A buckling factor is a load multiplier, so the critical load is
factor * applied_load and the factor itself is the safety factor against
elastic instability.
"""

import math

import pytest

from design_engine import DesignEngine
from design_engine.fea import FeaError

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}

SIDE, LENGTH = 10.0, 1000.0          # slender square column
I_MM4 = SIDE * SIDE ** 3 / 12.0
EULER_PP = math.pi ** 2 * S235["E_MPa"] * I_MM4 / LENGTH ** 2      # K=1
EULER_FP = 2.046 * EULER_PP                                        # K=0.6992


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("buck") / "data")


@pytest.fixture(scope="module")
def column(eng):
    return eng.create_part({
        "name": "euler-column", "units": "mm", "density_kg_m3": 7850,
        "features": [{"op": "box", "x": SIDE, "y": SIDE, "z": LENGTH}],
    }, reason="slender column for Euler buckling verification")["geometry_id"]


def _case(ref_load_N, required_sf, fixed_base=True, mesh_mm=6.0):
    base_dof = [1, 2, 3] if fixed_base else [1, 2]
    return {
        "material": dict(S235),
        "mesh": {"max_size_mm": mesh_mm},
        "constraints": [
            {"where": {"axis": "z", "at": "min"}, "dof": base_dof},
            {"where": {"axis": "z", "at": "max"}, "dof": [1, 2]},
        ],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, -ref_load_N]}],
        "limit_state": {"name": "elastic_buckling", "required_SF": required_sf},
    }


def test_buckling_matches_euler_fixed_pinned(eng, column):
    """P_cr = factor * applied load must match 2.046 * pi^2*E*I/L^2."""
    ref = 1000.0
    out = eng.run_fea_buckling(
        column, _case(ref, required_sf=1.0),
        reason=(f"Euler verification: fixed base, pinned top. Predicted "
                f"P_cr={EULER_FP:.1f} N -> factor {EULER_FP / ref:.3f}"))
    assert out["result"] == "pass"
    p_cr = out["safety_factor"] * ref
    assert p_cr == pytest.approx(EULER_FP, rel=0.02)
    # a square section buckles identically about both axes: modes 1 and 2 are
    # a degenerate pair, which is a real physical check on the mode content
    factors = sorted(f for f in out["buckling_factors"] if f > 0)
    assert factors[1] == pytest.approx(factors[0], rel=0.01)
    # the built-in scaling self-check must have confirmed load-multiplier
    # semantics: halving the reference load doubles the factor
    assert out["scaling_ratio"] == pytest.approx(2.0, abs=0.04)


def test_gate_fails_when_load_exceeds_critical(eng, column):
    """A reference load above P_cr gives a factor < 1 and must fail the gate."""
    ref = EULER_FP * 1.5
    out = eng.run_fea_buckling(
        column, _case(ref, required_sf=1.0),
        reason=f"load {ref:.0f} N deliberately above P_cr={EULER_FP:.0f} N")
    assert out["result"] == "fail"
    assert out["safety_factor"] < 1.0
    row = [r for r in eng.log.rows(action="fea_buckling")
           if r["id"] == out["failure_id"]][0]
    assert "elastic_buckling" in row["failure_mode"]
    assert "critical load" in row["failure_mode"]


def test_buckling_requires_its_own_limit_state(eng, column):
    bad = _case(1000.0, required_sf=1.0)
    bad["limit_state"]["name"] = "yield_von_mises"
    with pytest.raises(FeaError, match="requires limit_state 'elastic_buckling'"):
        eng.run_fea_buckling(column, bad, reason="wrong limit state")


def test_underconstrained_buckling_model_is_refused(eng, column):
    """The rigid-body-mode check applies to buckling too."""
    bad = _case(1000.0, required_sf=1.0, fixed_base=False)   # no axial restraint
    with pytest.raises(FeaError, match="underconstrained_model"):
        eng.run_fea_buckling(column, bad, reason="no axial restraint anywhere")
