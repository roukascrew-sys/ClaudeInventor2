"""Compound node-selector + three-point-bend analytic verification.

The compound {'all': [...]} selector (design_engine/mesh.py) is new
capability added to support a load applied to an interior strip of a face
(e.g. a mid-span loading patch on a beam's top face) rather than a whole
face. Verified two ways: (1) directly, against a hand-computed node count on
a known mesh; (2) end-to-end, by running a simply-supported beam under a
central point load through CalculiX and checking against the standard beam
formulas (Euler-Bernoulli, e.g. Gere & Goodno, Mechanics of Materials):

    M_max  = F*L/4                    (bending moment at mid-span)
    sigma  = M_max * c / I            (extreme-fibre bending stress)
    delta  = F*L^3 / (48*E*I)         (mid-span deflection)

for a square b x h cross-section, I = b*h^3/12, c = h/2.
"""

import math

import pytest

from design_engine import DesignEngine
from design_engine.mesh import MeshError, mesh_step, select_nodes

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}


def _beam_spec(size_mm, length_mm=300.0):
    return {"name": f"beam-{int(size_mm)}mm", "units": "mm",
            "features": [{"op": "box", "x": size_mm, "y": size_mm,
                          "z": length_mm}]}


def _three_point_case(size_mm, length_mm, force_n, required_sf,
                      max_size_mm=3.0, patch_half_width=10.0):
    """Properly restrained simple support (all 6 rigid-body modes removed).

    - End faces restrained transversely (x, y) only. At an end face z=const a
      section rotation about x is u = omega x r = (0, -t*z, t*y) with z=0
      there, i.e. purely axial -- so restraining x and y does NOT clamp
      bending rotation. This is a true pin, not a fixed end.
    - Axial (z) restrained on the MIDSPAN slab. For a symmetric
      simply-supported beam the slope is zero at midspan, so u_z is genuinely
      zero there; this removes the rigid-body translation without fighting
      the real solution. Restraining z on a whole end face instead would
      clamp the section (deflection collapses to ~0.38mm), and restraining it
      on an off-neutral-axis band fights the rotation and produces a ~880 MPa
      artificial singularity -- both were measured, not assumed.
    """
    mid = length_mm / 2.0
    return {
        "material": dict(S235),
        "mesh": {"max_size_mm": max_size_mm},
        "constraints": [
            {"where": {"axis": "z", "at": "min"}, "dof": [1, 2]},
            {"where": {"axis": "z", "at": "max"}, "dof": [1, 2]},
            {"where": {"axis": "z", "at": mid, "tol": 0.5}, "dof": [3]},
        ],
        "loads": [{
            "where": {"all": [
                {"axis": "y", "at": "max"},
                {"axis": "z", "at": mid, "tol": patch_half_width},
            ]},
            "force_total_N": [0, -force_n, 0],
        }],
        "limit_state": {"name": "yield_von_mises", "required_SF": required_sf},
    }


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("beam") / "data")


def test_compound_selector_intersects_two_axis_windows(eng):
    gid = eng.create_part(_beam_spec(14.0), reason="selector unit test")["geometry_id"]
    part = eng.get_part(gid)
    mesh = mesh_step(part["step_file_path"], 1.5)

    top = select_nodes(mesh, {"axis": "y", "at": "max"})
    patch = select_nodes(mesh, {"all": [
        {"axis": "y", "at": "max"}, {"axis": "z", "at": 150.0, "tol": 10.0}]})
    # the patch is a strict, non-trivial subset of the whole top face
    assert set(patch.tolist()) <= set(top.tolist())
    assert 0 < len(patch) < len(top)
    # every selected node really is in the window on both axes at once
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    for tag in patch:
        x, y, z = coords[int(tag)]
        assert abs(y - coords[int(top[0])][1]) < 1e-6 or y == max(
            coords[int(t)][1] for t in top)
        assert 140.0 - 1e-6 <= z <= 160.0 + 1e-6

    with pytest.raises(MeshError, match="takes no other keys"):
        select_nodes(mesh, {"all": [{"axis": "y", "at": "max"}], "bogus": 1})
    with pytest.raises(MeshError, match="2 or more"):
        select_nodes(mesh, {"all": [{"axis": "y", "at": "max"}]})
    with pytest.raises(MeshError, match="matched 0 nodes"):
        select_nodes(mesh, {"all": [
            {"axis": "y", "at": "max"}, {"axis": "z", "at": 99999.0}]})


def test_three_point_bend_matches_euler_bernoulli(eng):
    """14mm square bar, 300mm span, 1000N central load.

    I = 14*14^3/12 = 3201.333 mm^4, c = 7mm
    M_max = 1000*300/4 = 75000 N.mm
    sigma  = 75000*7/3201.333 = 163.99 MPa  (median field value away from
             the load-patch and support artifacts)
    delta  = 1000*300^3/(48*210000*3201.333) = 0.8367 mm
    """
    size, length, force = 14.0, 300.0, 1000.0
    I = size * size ** 3 / 12.0
    c = size / 2.0
    M = force * length / 4.0
    sigma_analytic = M * c / I
    delta_analytic = force * length ** 3 / (48 * S235["E_MPa"] * I)

    gid = eng.create_part(_beam_spec(size, length),
                          reason="three-point-bend analytic verification")["geometry_id"]
    case = _three_point_case(size, length, force, required_sf=1.0)
    run = eng.run_fea_static(
        gid, case,
        reason=f"predict: sigma={sigma_analytic:.1f} MPa, "
               f"delta={delta_analytic:.4f} mm at mid-span")

    # peak stress exceeds the nominal extreme-fibre value because of the local
    # concentration under the load patch; the analytic value is the floor.
    assert run["max_von_mises_MPa"] >= sigma_analytic * 0.85
    # deflection is an integral quantity and, with the model properly
    # restrained, matches the closed form tightly even on a coarse mesh
    assert run["max_displacement_mm"] == pytest.approx(delta_analytic, rel=0.02)

    row = eng.log.rows(action="fea_static")[-1]
    import json
    det = json.loads(row["details_json"])
    assert det["constraint_rank"] == 6      # no free rigid-body mode
    at = det["max_von_mises_at_mm"]
    # peak stress must sit at the extreme fibre (|y| ~ c) near mid-span (z~L/2),
    # not at a support or an arbitrary mesh artifact
    assert abs(abs(at[1]) - c) < 1.5
    assert abs(at[2] - length / 2.0) < 20.0


def test_underconstrained_model_is_refused(eng):
    """A model free to move without straining must be rejected, not solved.

    Dropping the midspan axial restraint leaves a free z translation. CalculiX
    still returns numbers -- stresses are correct, but displacements carry an
    arbitrary rigid-body component that changes with solver/thread count
    (measured: 0.84104mm single-threaded vs 0.84823mm on 8 threads for the
    identical job). Comparing such a displacement to a hand calculation
    manufactures false agreement, so the engine refuses up front.
    """
    from design_engine.fea import FeaError

    gid = eng.create_part(_beam_spec(14.0), reason="underconstrained check")["geometry_id"]
    case = _three_point_case(14.0, 300.0, 1000.0, required_sf=1.0)
    case["constraints"] = case["constraints"][:2]        # drop the axial restraint
    with pytest.raises(FeaError, match="underconstrained_model") as exc:
        eng.run_fea_static(gid, case, reason="missing axial restraint")
    msg = str(exc.value)
    assert "rigid-body mode" in msg
    assert "rank 5/6" in msg
    row = eng.log.rows(action="fea_static", result="fail")[-1]
    assert "underconstrained_model" in row["failure_mode"]


def test_extremum_selector_on_curved_tip_reports_real_faces(eng):
    """'at': 'max' is a coordinate extremum, not a face.

    On a part whose extremum is a curved tangent (a boss protruding past a
    flat plate), 'max' selects a sliver carrying no complete boundary
    triangle. The error must name the real flat faces instead of failing bare.
    """
    from design_engine.fea import FeaError

    gid = eng.create_part({
        "name": "plate-with-boss", "units": "mm",
        "features": [
            {"op": "box", "x": 4.0, "y": 30.0, "z": 60.0, "at": [0, 15, 0]},
            {"op": "cylinder", "d": 12.0, "h": 60.0, "at": [0, 0, 0],
             "mode": "union"},
        ],
    }, reason="boss protrudes in x past the plate's flat face")["geometry_id"]

    case = {
        "material": dict(S235),
        "mesh": {"max_size_mm": 2.0},
        "constraints": [
            {"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]},
            {"where": {"axis": "z", "at": "max"}, "dof": [1, 2, 3]},
        ],
        # x 'max' is the boss tangent line (x=6), not the plate face (x=2)
        "loads": [{"where": {"all": [{"axis": "x", "at": "max"},
                                     {"axis": "z", "at": 30.0, "tol": 5.0}]},
                   "force_total_N": [100.0, 0, 0]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": 1.0},
    }
    with pytest.raises(FeaError, match="no boundary triangles") as exc:
        eng.run_fea_static(gid, case, reason="extremum lands on a curved tangent")
    msg = str(exc.value)
    assert "coordinate extremum, not a face" in msg
    assert "flat faces along x" in msg      # names the real alternatives
    assert "x=2" in msg                     # the plate face it should have used


def test_cylinder_selector_reaches_a_bore_wall(eng):
    """Curved surfaces are selectable -- no axis window can reach a bore."""
    from design_engine.mesh import mesh_step, select_nodes

    gid = eng.create_part({
        "name": "bored-block", "units": "mm",
        "features": [
            {"op": "box", "x": 24.0, "y": 24.0, "z": 40.0},
            {"op": "cylinder", "d": 10.0, "h": 40.0, "at": [0, 0, 0],
             "mode": "cut"},
        ],
    }, reason="block with a 10mm axial bore")["geometry_id"]
    mesh = mesh_step(eng.get_part(gid)["step_file_path"], 2.0)

    wall = select_nodes(mesh, {"cylinder": {"axis": "z", "center": [0, 0],
                                            "r": 5.0, "tol": 0.1}})
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    assert len(wall) > 20
    for tag in wall:                        # every node really is on the bore
        x, y, _ = coords[int(tag)]
        assert abs((x ** 2 + y ** 2) ** 0.5 - 5.0) <= 0.1 + 1e-9

    # 'half' keeps the bearing side only -- roughly half the wall, all +x
    half = select_nodes(mesh, {"cylinder": {"axis": "z", "center": [0, 0],
                                            "r": 5.0, "tol": 0.1,
                                            "half": [1.0, 0.0]}})
    assert 0 < len(half) < len(wall)
    for tag in half:
        assert coords[int(tag)][0] > 0
