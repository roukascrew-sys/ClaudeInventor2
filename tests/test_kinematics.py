"""Kinematics layer (Phase 8) — verified against closed-form door reactions.

A door of weight W, width w, hung on two hinges separated vertically by d,
with its centre of mass at w/2 from the hinge line:

  total vertical reaction            = W
  moment-free (spherical) pin couple = W*w/(2*d)   horizontal, equal/opposite
  moment-carrying (revolute) joints  = W*w/2       total joint moment,
                                       and NO horizontal force couple

Both are correct; they are different idealisations, and which one is chosen
changes the answer qualitatively — a revolute pair hides as an internal
moment the horizontal pull that a real hinge leaf actually carries. The tool
exposes both and these tests pin both against the closed form.

Chrono lives in a separate conda env by necessity (conda-only package), so
these tests skip cleanly when that env is absent rather than failing.
"""

import math

import pytest

from design_engine import DesignEngine
from design_engine.geometry import SpecError
from design_engine.kinematics import (ChronoUnavailable, KinematicsError,
                                      chrono_available)

pytestmark = pytest.mark.skipif(
    not chrono_available()[0],
    reason=f"chrono env unavailable: {chrono_available()[1]}")

G = 9810.0          # mm/s^2
DENSITY = 500.0     # kg/m^3, a light door leaf
W_MM, H_MM, T_MM = 800.0, 2000.0, 40.0
D_MM = 1400.0       # hinge vertical separation


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("kin") / "data")


@pytest.fixture(scope="module")
def door(eng):
    """A door slab plus a fixed ground block, as a jointed assembly."""
    slab = eng.create_part({
        "name": "door-slab", "units": "mm", "density_kg_m3": DENSITY,
        "features": [{"op": "box", "x": W_MM, "y": T_MM, "z": H_MM}],
    }, reason="door leaf for kinematics verification")["geometry_id"]
    post = eng.create_part({
        "name": "door-post", "units": "mm", "density_kg_m3": 7850,
        "features": [{"op": "box", "x": 60, "y": 60, "z": H_MM}],
    }, reason="fixed frame post")["geometry_id"]
    props = eng.get_part(slab)["properties"]
    mass = props["mass_kg_estimate"]
    # box is centred in x/y with base at z=0, so shifting +w/2 in x puts the
    # hinge line at x=0 and the centre of mass at x=w/2 -- the classic setup.
    return {"slab": slab, "post": post, "mass_kg": mass,
            "weight_N": mass * (G / 1000.0)}


def _assembly(eng, door, joint_type, name):
    return eng.create_assembly({
        "name": name, "units": "mm",
        "components": [
            {"ref": "post", "geometry_id": door["post"], "at": [0, 0, 0]},
            {"ref": "leaf", "geometry_id": door["slab"],
             "at": [W_MM / 2.0, 0, 0]},
        ],
        "joints": [
            {"id": "lower", "type": joint_type, "between": ["leaf", "post"],
             "at": [0, 0, H_MM / 2.0 - D_MM / 2.0], "axis": [0, 0, 1]},
            {"id": "upper", "type": joint_type, "between": ["leaf", "post"],
             "at": [0, 0, H_MM / 2.0 + D_MM / 2.0], "axis": [0, 0, 1]},
        ],
        "chains": [{"name": "leaf-to-post-gap",
                    "requirement_mm": {"min": 1.0},
                    "terms": [{"desc": "gap", "nominal": 3.0, "tol_plus": 0.5,
                               "tol_minus": 0.5, "sense": 1}]}],
    }, reason=f"door on two {joint_type} joints")["assembly_id"]


def _case(limit_name, allowable):
    return {
        "gravity_mm_s2": [0, 0, -G],
        "analysis": "static",
        "fixed": ["post"],
        "limit_state": {"name": limit_name, "allowable": allowable,
                        "source": "test allowable, chosen to bracket the "
                                  "closed-form value"},
    }


def test_spherical_pins_reproduce_the_classic_force_couple(eng, door):
    """Force-only pins: H = W*w/(2d), equal and opposite, no joint moment."""
    aid = _assembly(eng, door, "spherical", "door-spherical")
    out = eng.run_kinematics(aid, _case("joint_reaction_force", 500.0),
                             reason="verify against H = W*w/(2d)")
    W = door["weight_N"]
    H_exact = W * (W_MM / 1000.0) / (2 * (D_MM / 1000.0))

    by_id = {r["joint_id"]: r for r in out["reactions"]}
    fx = [by_id["lower"]["force_N"][0], by_id["upper"]["force_N"][0]]
    fz = [by_id["lower"]["force_N"][2], by_id["upper"]["force_N"][2]]

    assert abs(fx[0]) == pytest.approx(H_exact, rel=1e-3)
    assert abs(fx[1]) == pytest.approx(H_exact, rel=1e-3)
    assert fx[0] * fx[1] < 0                       # opposed: a genuine couple
    assert sum(fx) == pytest.approx(0.0, abs=1e-6)  # horizontal equilibrium
    assert abs(sum(fz)) == pytest.approx(W, rel=1e-6)   # carries the weight
    for r in out["reactions"]:                      # pins carry no moment
        assert r["torque_magnitude_Nm"] == pytest.approx(0.0, abs=1e-9)


def test_revolute_joints_carry_the_moment_instead(eng, door):
    """Moment-carrying joints: total joint moment = W*w/2, no force couple."""
    aid = _assembly(eng, door, "revolute", "door-revolute")
    out = eng.run_kinematics(aid, _case("joint_reaction_torque", 500.0),
                             reason="verify against total moment = W*w/2")
    W = door["weight_N"]
    M_exact = W * (W_MM / 1000.0) / 2.0

    total_moment = sum(r["torque_magnitude_Nm"] for r in out["reactions"])
    assert total_moment == pytest.approx(M_exact, rel=1e-3)
    fx = [r["force_N"][0] for r in out["reactions"]]
    assert all(abs(v) < 1e-6 for v in fx)          # no horizontal couple
    fz = sum(r["force_N"][2] for r in out["reactions"])
    assert abs(fz) == pytest.approx(W, rel=1e-6)


def test_gate_fails_and_records_a_referencable_failure(eng, door):
    aid = _assembly(eng, door, "spherical", "door-overloaded")
    W = door["weight_N"]
    H_exact = W * (W_MM / 1000.0) / (2 * (D_MM / 1000.0))
    out = eng.run_kinematics(
        aid, _case("joint_reaction_force", H_exact / 2.0),
        reason="allowable deliberately set below the computed reaction")
    assert out["result"] == "fail"
    assert isinstance(out["failure_id"], int)
    row = [r for r in eng.log.rows(action="run_kinematics")
           if r["id"] == out["failure_id"]][0]
    assert row["result"] == "fail"
    assert "joint_reaction_force" in row["failure_mode"]
    assert "exceeds allowable" in row["failure_mode"]


def test_motion_case_and_joint_validation(eng, door):
    aid = _assembly(eng, door, "spherical", "door-validation")

    bad_limit = _case("joint_reaction_force", 500.0)
    bad_limit["limit_state"].pop("source")
    with pytest.raises(KinematicsError, match="does not accept unsourced"):
        eng.run_kinematics(aid, bad_limit, reason="uncited allowable")

    unnamed = _case("joint_reaction_force", 500.0)
    unnamed["limit_state"]["name"] = "vibes"
    with pytest.raises(KinematicsError, match="must name its limit state"):
        eng.run_kinematics(aid, unnamed, reason="unnamed limit state")

    nothing_fixed = _case("joint_reaction_force", 500.0)
    nothing_fixed["fixed"] = []
    with pytest.raises(KinematicsError, match="required"):
        eng.run_kinematics(aid, nothing_fixed, reason="nothing held fixed")

    extra = _case("joint_reaction_force", 500.0)
    extra["turbo"] = True
    with pytest.raises(KinematicsError, match="unexpected keys"):
        eng.run_kinematics(aid, extra, reason="typo'd key")

    # a jointless assembly is a parts list, not a mechanism
    plain = eng.create_assembly({
        "name": "no-joints", "units": "mm",
        "components": [{"ref": "a", "geometry_id": door["slab"],
                        "at": [0, 0, 0]}],
        "chains": [{"name": "c", "requirement_mm": {"min": 0.0},
                    "terms": [{"desc": "d", "nominal": 1.0, "tol_plus": 0.1,
                               "tol_minus": 0.1, "sense": 1}]}],
    }, reason="assembly without joints")["assembly_id"]
    with pytest.raises(KinematicsError, match="declares no joints"):
        eng.run_kinematics(plain, _case("joint_reaction_force", 500.0),
                           reason="no joints to solve")


def test_bad_joint_specs_are_refused_at_assembly_creation(eng, door):
    def make(joints):
        return eng.create_assembly({
            "name": "bad-joints", "units": "mm",
            "components": [
                {"ref": "post", "geometry_id": door["post"], "at": [0, 0, 0]},
                {"ref": "leaf", "geometry_id": door["slab"], "at": [0, 0, 0]}],
            "joints": joints,
            "chains": [{"name": "c", "requirement_mm": {"min": 0.0},
                        "terms": [{"desc": "d", "nominal": 1.0,
                                   "tol_plus": 0.1, "tol_minus": 0.1,
                                   "sense": 1}]}],
        }, reason="should be refused")

    with pytest.raises(SpecError, match="type must be one of"):
        make([{"type": "magnetic", "between": ["leaf", "post"], "at": [0, 0, 0]}])
    with pytest.raises(SpecError, match="unknown component ref"):
        make([{"type": "revolute", "between": ["leaf", "ghost"], "at": [0, 0, 0]}])
    with pytest.raises(SpecError, match="cannot join a component to itself"):
        make([{"type": "revolute", "between": ["leaf", "leaf"], "at": [0, 0, 0]}])
    with pytest.raises(SpecError, match="non-zero vector"):
        make([{"type": "revolute", "between": ["leaf", "post"],
               "at": [0, 0, 0], "axis": [0, 0, 0]}])


def test_massless_part_is_refused(eng, door):
    """A multibody solve cannot proceed on unknown mass."""
    nodens = eng.create_part({
        "name": "no-density", "units": "mm",
        "features": [{"op": "box", "x": 50, "y": 50, "z": 50}],
    }, reason="part without density")["geometry_id"]
    aid = eng.create_assembly({
        "name": "massless-rig", "units": "mm",
        "components": [{"ref": "post", "geometry_id": door["post"], "at": [0, 0, 0]},
                       {"ref": "blk", "geometry_id": nodens, "at": [100, 0, 0]}],
        "joints": [{"id": "j", "type": "revolute", "between": ["blk", "post"],
                    "at": [0, 0, 0], "axis": [0, 0, 1]}],
        "chains": [{"name": "c", "requirement_mm": {"min": 0.0},
                    "terms": [{"desc": "d", "nominal": 1.0, "tol_plus": 0.1,
                               "tol_minus": 0.1, "sense": 1}]}],
    }, reason="rig with a massless component")["assembly_id"]
    with pytest.raises(KinematicsError, match="density_kg_m3"):
        eng.run_kinematics(aid, _case("joint_reaction_force", 500.0),
                           reason="mass is unknown")
