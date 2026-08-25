"""Mass properties / thrust-line alignment (Phase 10).

Numeric assertions are against hand-computed rigid-body statics, not against
whatever the code happens to return:

  - two equal masses at x = -100 and +300 -> CG at x = +100
  - a single 1000 N vertical thruster whose line sits 50 mm from the CG
    produces a 50 N*m pitching moment (1000 N x 0.05 m) and a 50 mm offset
  - a 100 kg system needs 5000/981 mm of CG shift to trim 50 N*m:
    shift = M / (m*g) = 50 / (100*9.80665) m = 50.98 mm
"""

import math

import pytest

from design_engine import DesignEngine
from design_engine.massprops import MassPropsError

PLATE = {
    "name": "mp-plate", "units": "mm", "density_kg_m3": 1000.0,
    # 100 x 100 x 100 mm at the origin -> V = 1e6 mm^3 -> 1.0 kg at 1000 kg/m^3
    # box is centred in x,y and starts at z=0, so its COM is (0, 0, 50)
    "features": [{"op": "box", "x": 100, "y": 100, "z": 100}],
}


@pytest.fixture()
def eng(tmp_path):
    return DesignEngine(tmp_path / "data")


@pytest.fixture()
def two_block_assembly(eng):
    gid = eng.create_part(PLATE, reason="mass properties test block")["geometry_id"]
    aid = eng.create_assembly({
        "name": "two-blocks", "units": "mm",
        "components": [
            {"geometry_id": gid, "at": [-100, 0, 0], "ref": "left"},
            {"geometry_id": gid, "at": [300, 0, 0], "ref": "right"},
        ],
        # the engine requires every assembly to declare its tolerance chains;
        # this one is trivial but real (the 400mm gap between the two blocks)
        "chains": [{
            "name": "block-gap",
            "requirement_mm": {"min": 0.0},
            "terms": [
                {"desc": "centre spacing", "nominal": 400.0,
                 "tol_plus": 0.5, "tol_minus": 0.5, "sense": 1},
                {"desc": "block width", "nominal": 100.0,
                 "tol_plus": 0.1, "tol_minus": 0.1, "sense": -1},
            ],
        }],
    }, reason="two equal blocks for a hand-checkable CG")["assembly_id"]
    return aid


def _case(**over):
    case = {
        "thrust": [{"name": "t1", "force_N": [0, 0, 1000], "at_mm": [100, 0, 0]}],
        "limit_states": [
            {"name": "thrust_cg_alignment", "max_offset_mm": 25.0},
            {"name": "thrust_to_weight", "min_ratio": 1.0},
        ],
    }
    case.update(over)
    return case


def test_cg_of_two_equal_masses_is_their_midpoint(eng, two_block_assembly):
    out = eng.check_mass_properties(
        two_block_assembly, _case(), reason="hand-checkable two-block CG")
    # each block 1.0 kg; COMs at x = -100+0 and 300+0 -> CG x = 100
    assert out["total_mass_kg"] == pytest.approx(2.0)
    assert out["centre_of_mass_mm"][0] == pytest.approx(100.0)
    assert out["centre_of_mass_mm"][2] == pytest.approx(50.0)


def test_thrust_through_the_cg_has_zero_offset(eng, two_block_assembly):
    out = eng.check_mass_properties(
        two_block_assembly, _case(), reason="thrust line straight through CG")
    assert out["thrust_cg_offset_mm"] == pytest.approx(0.0, abs=1e-6)
    assert out["result"] == "pass"


def test_offset_thrust_line_is_measured_and_gated(eng, two_block_assembly):
    """Thruster moved 50 mm off the CG: offset must read 50 mm and the
    25 mm gate must fail with the magnitude in the log."""
    out = eng.check_mass_properties(
        two_block_assembly,
        _case(thrust=[{"name": "t1", "force_N": [0, 0, 1000],
                       "at_mm": [150, 0, 0]}]),
        reason="thruster deliberately 50mm aft of the CG")
    assert out["thrust_cg_offset_mm"] == pytest.approx(50.0)
    assert out["result"] == "fail"
    row = [r for r in eng.log.rows(action="check_mass_properties")
           if r["id"] == out["failure_id"]][0]
    assert "thrust_cg_alignment" in row["failure_mode"]
    assert "50.0" in row["failure_mode"]


def test_pilot_trim_authority_is_a_checkable_argument(eng, two_block_assembly):
    """M = F x d = 1000 N x 0.05 m = 50 N*m; required CG shift = M/(m*g)."""
    out = eng.check_mass_properties(
        two_block_assembly,
        _case(thrust=[{"name": "t1", "force_N": [0, 0, 1000],
                       "at_mm": [150, 0, 0]}],
              point_masses=[{"name": "ballast", "mass_kg": 98.0,
                             "at_mm": [100, 0, 50],
                             "source": "test fixture, stated assumption"}],
              pilot={"mass_kg": 98.0, "max_cg_shift_mm": 150.0}),
        reason="trim authority check")
    trim = out["pilot_trim"]
    assert trim["pitch_moment_Nm"] == pytest.approx(50.0)
    # total mass now 100 kg -> shift = 50 / (100*9.80665) m = 50.98 mm
    assert trim["required_cg_shift_mm"] == pytest.approx(50.985, abs=0.01)
    assert trim["within_authority"] is True


def test_thrust_to_weight_gate_fails_when_underpowered(eng, two_block_assembly):
    out = eng.check_mass_properties(
        two_block_assembly,
        _case(thrust=[{"name": "t1", "force_N": [0, 0, 10],
                       "at_mm": [100, 0, 0]}]),
        reason="deliberately underpowered")
    assert out["thrust_to_weight"] < 1.0
    assert out["result"] == "fail"
    row = [r for r in eng.log.rows(action="check_mass_properties")
           if r["id"] == out["failure_id"]][0]
    assert "thrust_to_weight" in row["failure_mode"]


def test_point_mass_without_source_is_refused(eng, two_block_assembly):
    with pytest.raises(MassPropsError, match="source"):
        eng.check_mass_properties(
            two_block_assembly,
            _case(point_masses=[{"name": "pilot", "mass_kg": 90.0,
                                 "at_mm": [0, 0, 900]}]),
            reason="unsourced mass must be refused")


def test_point_masses_move_the_cg(eng, two_block_assembly):
    """The pilot dominates a wearable vehicle's CG and is not geometry —
    if point masses did not move the CG the whole check would be theatre."""
    out = eng.check_mass_properties(
        two_block_assembly,
        _case(point_masses=[{"name": "pilot", "mass_kg": 98.0,
                             "at_mm": [0, 0, 0],
                             "source": "stated design pilot mass"}]),
        reason="pilot mass dominates the CG")
    # 2 kg at x=100 plus 98 kg at x=0 -> x_cg = 200/100 = 2.0 mm
    assert out["total_mass_kg"] == pytest.approx(100.0)
    assert out["centre_of_mass_mm"][0] == pytest.approx(2.0)


def test_unknown_limit_state_is_refused(eng, two_block_assembly):
    with pytest.raises(MassPropsError, match="must be one of"):
        eng.check_mass_properties(
            two_block_assembly,
            _case(limit_states=[{"name": "vibes", "max_offset_mm": 5}]),
            reason="gate must name a real limit state")


def test_non_parallel_thrusters_resolve_to_a_true_resultant(eng, two_block_assembly):
    """Two symmetric canted thrusters: the lateral components cancel, the
    resultant is purely vertical and passes through the CG at x=100."""
    out = eng.check_mass_properties(
        two_block_assembly,
        _case(thrust=[
            {"name": "l", "force_N": [300, 0, 1000], "at_mm": [100, 0, 0]},
            {"name": "r", "force_N": [-300, 0, 1000], "at_mm": [100, 0, 0]},
        ]),
        reason="canted pair, lateral components must cancel")
    assert out["thrust_resultant_N"][0] == pytest.approx(0.0)
    assert out["thrust_magnitude_N"] == pytest.approx(2000.0)
    assert out["thrust_cg_offset_mm"] == pytest.approx(0.0, abs=1e-6)
