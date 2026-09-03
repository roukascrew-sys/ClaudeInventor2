"""Half-model symmetry, and above all the cases it must REFUSE.

A symmetry cut is silent when wrong. Impose zero normal displacement on a face
that is not a plane of symmetry and CalculiX returns a converged, plausible,
wrong answer with no residual to inspect. So most of this file is negative
tests: the value of the feature is in what it declines to do.
"""

import pytest

from design_engine.symmetry import (Plane, SymmetryError, assert_static_only,
                                    check_constraints, check_loads, cut_half,
                                    halve_shared_loads, mirror_force,
                                    mirror_selector, parse, plane_nodes,
                                    verify, volume_balance)

X0 = Plane(axis="x", at=0.0, keep="+")


def test_parse_returns_none_when_no_symmetry_is_asked_for():
    """Solving the whole part is always correct, so absence must be the
    default rather than an error."""
    assert parse(None) is None
    assert parse({}) is None


@pytest.mark.parametrize("spec, match", [
    ({"axis": "w"}, "not one of"),
    ({"axis": "x", "keep": "both"}, "keep"),
    ({"axis": "x", "at": "middle"}, "must be a number"),
    ("x=0", "must be a mapping"),
])
def test_a_malformed_plane_is_refused(spec, match):
    with pytest.raises(SymmetryError, match=match):
        parse(spec)


def test_the_held_dof_is_normal_to_the_plane():
    """The symmetry condition holds motion THROUGH the plane and leaves motion
    within it free. Clamping the in-plane DOFs would restrain the model far
    more than its mirror image ever did."""
    assert Plane("x", 0.0).dof == 1
    assert Plane("y", 0.0).dof == 2
    assert Plane("z", 0.0).dof == 3


@pytest.mark.parametrize("analysis", ["buckle", "frequency"])
def test_symmetry_is_refused_for_eigenvalue_analyses(analysis):
    """The critical buckling mode of a symmetric frame is frequently
    ANTI-symmetric - sidesway is the textbook case - and a symmetry-constrained
    half model cannot represent it. It would return a higher, non-critical
    factor and look healthy doing it."""
    with pytest.raises(SymmetryError, match="ANTI-symmetric"):
        assert_static_only(analysis, X0)


def test_eigenvalue_analyses_are_fine_without_symmetry():
    assert assert_static_only("buckle", None) is None
    assert assert_static_only("static", X0) is None


def test_a_position_on_the_symmetry_axis_mirrors():
    assert mirror_selector({"axis": "x", "at": -330.0, "tol": 30.0}, X0) == {
        "axis": "x", "at": 330.0, "tol": 30.0}


def test_a_position_off_the_symmetry_axis_is_unchanged():
    where = {"axis": "z", "at": 190.075, "tol": 1.0}
    assert mirror_selector(where, X0) == where


def test_min_and_max_faces_swap_on_the_symmetry_axis():
    assert mirror_selector({"axis": "x", "at": "min"}, X0)["at"] == "max"
    assert mirror_selector({"axis": "x", "at": "max"}, X0)["at"] == "min"


def test_a_cylinder_about_the_symmetry_axis_is_its_own_mirror():
    """It runs through the plane, and its centre is given in the two
    coordinates the reflection does not touch."""
    cyl = {"cylinder": {"axis": "x", "center": [0.0, 400.0], "r": 3.25}}
    assert mirror_selector(cyl, X0) == cyl


def test_a_cylinder_across_the_symmetry_axis_cannot_be_mirrored():
    """Refused rather than guessed: its extent along x is not stated, so
    whether it is symmetric cannot be established from the selector."""
    with pytest.raises(SymmetryError, match="cannot be mirrored"):
        mirror_selector(
            {"cylinder": {"axis": "z", "center": [0.0, 0.0], "r": 5.0}}, X0)


def test_the_normal_force_component_flips_and_the_rest_do_not():
    """This is what makes the load check a real one. Equal normal components
    at mirrored positions are an ANTI-symmetric pair, which a half model
    cannot represent at all."""
    assert mirror_force([100.0, 200.0, 300.0], X0) == [-100.0, 200.0, 300.0]


def _load(at, force, axis="x", tol=30.0):
    return {"where": {"axis": axis, "at": at, "tol": tol},
            "force_total_N": list(force)}


def test_a_mirrored_pair_is_symmetric():
    out = check_loads([_load(-330.0, [0, 0, 397]), _load(330.0, [0, 0, 397])],
                      X0)
    assert out["symmetric"] and out["shared"] == []


def test_an_unpaired_load_is_not_symmetric():
    out = check_loads([_load(-330.0, [0, 0, 397])], X0)
    assert not out["symmetric"]
    assert "no load mirrors it" in out["unmatched"][0]["why"]


def test_equal_normal_components_at_mirrored_positions_are_anti_symmetric():
    """Both pushing +x is a pair that PULLS the structure sideways. The
    symmetric partner of +100x at -330 is -100x at +330."""
    out = check_loads([_load(-330.0, [100, 0, 0]), _load(330.0, [100, 0, 0])],
                      X0)
    assert not out["symmetric"]


def test_a_load_straddling_the_plane_is_shared_and_gets_halved():
    """The whole top face is half in each model half. force_total_N is a
    TOTAL, so the kept half carries half of it."""
    face = {"where": {"axis": "z", "at": "max"},
            "force_total_N": [0.0, 2000.0, 0.0]}
    out = check_loads([face], X0)
    assert out["symmetric"] and out["shared"] == [0]

    halved = halve_shared_loads([face], X0, out["shared"])
    assert halved[0]["force_total_N"] == [0.0, 1000.0, 0.0]
    assert halved[0]["_halved_by_symmetry"] is True
    assert face["force_total_N"] == [0.0, 2000.0, 0.0]


def test_an_unshared_load_is_not_halved():
    """Its mirror partner is simply absent from the kept half, and that
    absence is correct - not something to compensate for."""
    loads = [_load(-330.0, [0, 0, 397]), _load(330.0, [0, 0, 397])]
    out = halve_shared_loads(loads, X0, [])
    assert [x["force_total_N"] for x in out] == [[0, 0, 397], [0, 0, 397]]


def test_a_shared_load_pushing_through_the_plane_is_refused():
    """The plane forbids normal displacement, so a normal load there is an
    anti-symmetric problem wearing a symmetric selector."""
    out = check_loads([{"where": {"axis": "z", "at": "max"},
                        "force_total_N": [500.0, 0.0, 0.0]}], X0)
    assert not out["symmetric"]
    assert "forbids" in out["unmatched"][0]["why"]


def test_mirrored_restraints_are_symmetric():
    cons = [{"where": {"axis": "x", "at": -100.0}, "dof": [1, 2, 3]},
            {"where": {"axis": "x", "at": 100.0}, "dof": [1, 2, 3]}]
    assert check_constraints(cons, X0)["symmetric"]


def test_an_unpaired_restraint_is_not_symmetric():
    cons = [{"where": {"axis": "x", "at": -100.0}, "dof": [1, 2, 3]}]
    out = check_constraints(cons, X0)
    assert not out["symmetric"]
    assert "no restraint mirrors it" in out["unmatched"][0]["why"]


def test_a_mirrored_restraint_holding_different_dofs_is_not_symmetric():
    cons = [{"where": {"axis": "x", "at": -100.0}, "dof": [1, 2, 3]},
            {"where": {"axis": "x", "at": 100.0}, "dof": [3]}]
    assert not check_constraints(cons, X0)["symmetric"]


@pytest.fixture()
def cq():
    return pytest.importorskip("cadquery")


def test_a_centred_box_balances(cq):
    out = volume_balance(cq.Workplane("XY").box(60, 40, 400), X0)
    assert out["balanced"] and out["difference_pct"] < 1e-6


def test_an_off_centre_part_does_not_balance(cq):
    """The check that catches the mistake people actually make."""
    solid = cq.Workplane("XY").box(60, 40, 400).translate((7.0, 0, 0))
    out = volume_balance(solid, X0)
    assert not out["balanced"]
    assert out["positive_mm3"] > out["negative_mm3"]


def test_a_one_sided_feature_does_not_balance(cq):
    """Same envelope, a hole on one side only - which a bounding box would
    miss entirely, and a volume does not."""
    solid = (cq.Workplane("XY").box(60, 40, 400)
             .faces(">Z").workplane().center(20, 0).hole(10))
    assert not volume_balance(solid, X0)["balanced"]


def test_a_plane_outside_the_part_is_refused(cq):
    with pytest.raises(SymmetryError, match="does not pass through"):
        volume_balance(cq.Workplane("XY").box(60, 40, 400),
                       Plane(axis="x", at=500.0))


def test_the_cut_keeps_half_and_reports_the_factor(cq):
    kept, rep = cut_half(cq.Workplane("XY").box(60, 40, 400), X0)
    assert rep["fraction_kept"] == pytest.approx(0.5, abs=1e-6)
    assert rep["extensive_factor"] == 2.0


def _case(loads, cons):
    return {"loads": loads, "constraints": cons}


def test_verify_passes_a_genuinely_symmetric_model(cq):
    ev = verify(cq.Workplane("XY").box(60, 40, 400),
                _case([{"where": {"axis": "z", "at": "max"},
                        "force_total_N": [0.0, 2000.0, 0.0]}],
                      [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}]),
                X0)
    assert ev["geometry"]["balanced"]
    assert ev["loads"]["symmetric"] and ev["constraints"]["symmetric"]


def test_verify_names_every_failing_count_not_just_the_first(cq):
    """A reader fixing one problem should not have to re-run to find the next."""
    solid = cq.Workplane("XY").box(60, 40, 400).translate((7.0, 0, 0))
    with pytest.raises(SymmetryError) as caught:
        verify(solid,
               _case([{"where": {"axis": "x", "at": -100.0, "tol": 5.0},
                       "force_total_N": [0.0, 100.0, 0.0]}],
                     [{"where": {"axis": "x", "at": -50.0}, "dof": [1, 2, 3]}]),
               X0)
    msg = str(caught.value)
    assert "symmetry_not_demonstrated" in msg
    assert "geometry is not symmetric" in msg
    assert "loads are not symmetric" in msg
    assert "restraints are not symmetric" in msg


def test_the_refusal_explains_why_it_is_a_refusal_and_not_a_warning(cq):
    solid = cq.Workplane("XY").box(60, 40, 400).translate((7.0, 0, 0))
    with pytest.raises(SymmetryError, match="converged, plausible, WRONG"):
        verify(solid, _case([], []), X0)


def test_plane_nodes_finds_only_the_cut_face():
    mesh = {"node_tags": [1, 2, 3, 4],
            "coords": [(0.0, 1.0, 2.0), (1e-9, 5.0, 6.0),
                       (3.0, 0.0, 0.0), (-2.0, 0.0, 0.0)]}
    assert plane_nodes(mesh, X0) == [1, 2]
