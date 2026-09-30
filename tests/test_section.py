"""Cross-section resultants, checked against closed-form answers.

A section stress is only worth having if it is RIGHT, and the whole reason
`weld_static` exists is that the quantity it replaces could not be verified at
all. So none of these tests assert that the code agrees with itself: each one
puts a known force and a known moment through a section of known area and
checks the stress the module reports against N/A and M*c/I computed by hand.
"""

import math

import numpy as np
import pytest

from design_engine.section import SectionError, cut, resultants

# Kuhn subdivision: six tets exactly tile a cube, sharing the 0-6 diagonal.
_KUHN = ((0, 1, 2, 6), (0, 2, 3, 6), (0, 3, 7, 6),
         (0, 7, 4, 6), (0, 4, 5, 6), (0, 5, 1, 6))
_EDGES = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))


def box_mesh(nx, ny, nz, x0=0.0, x1=100.0, y0=-10.0, y1=10.0,
             z0=-5.0, z1=5.0):
    """A structured C3D10 mesh of a rectangular box.

    Hand-built rather than meshed with gmsh so the test needs no CAD kernel and
    no STEP file, and so the exact cross-sectional area is known by
    construction rather than measured.
    """
    xs = np.linspace(x0, x1, nx + 1)
    ys = np.linspace(y0, y1, ny + 1)
    zs = np.linspace(z0, z1, nz + 1)

    coords = []
    tag_of = {}

    def node(p):
        key = tuple(round(v, 9) for v in p)
        if key not in tag_of:
            tag_of[key] = len(coords) + 1
            coords.append(list(key))
        return tag_of[key]

    grid = {}
    for i, x in enumerate(xs):
        for j, y in enumerate(ys):
            for k, z in enumerate(zs):
                grid[(i, j, k)] = node((x, y, z))

    conn = []
    off = ((0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
           (0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1))
    for i in range(nx):
        for j in range(ny):
            for k in range(nz):
                c = [grid[(i + a, j + b, k + c2)] for a, b, c2 in off]
                for tet in _KUHN:
                    t = [c[v] for v in tet]
                    xyz = [coords[n - 1] for n in t]
                    mids = []
                    for a, b in _EDGES:
                        mids.append(node(tuple(
                            (xyz[a][d] + xyz[b][d]) / 2.0 for d in range(3))))
                    conn.append(t + mids)

    return {"node_tags": np.array(sorted(tag_of.values()), dtype=np.int64),
            "coords": np.array(coords, dtype=float),
            "connectivity": np.array(conn, dtype=np.int64)}


def tip_force(mesh, fx, fy, fz, at_x=100.0):
    """A single nodal force at (at_x, 0, 0), as CalculiX's FORC block gives it."""
    coords = np.asarray(mesh["coords"])
    tags = np.asarray(mesh["node_tags"])
    hit = np.nonzero((np.abs(coords[:, 0] - at_x) < 1e-9)
                     & (np.abs(coords[:, 1]) < 1e-9)
                     & (np.abs(coords[:, 2]) < 1e-9))[0]
    assert hit.size == 1, f"expected exactly one tip node, found {hit.size}"
    return {int(tags[hit[0]]): (fx, fy, fz)}


# --------------------------------------------------------------- geometry
def test_area_of_a_known_section_is_exact():
    m = box_mesh(10, 4, 2)
    sec = cut(m, [53.7, 0, 0], [1, 0, 0])
    # 20 mm x 10 mm, cut clear of any node plane.
    assert sec["area_mm2"] == pytest.approx(200.0, rel=1e-9)
    assert sec["centroid_mm"] == pytest.approx([53.7, 0.0, 0.0], abs=1e-9)


def test_area_is_independent_of_the_normal_sign():
    m = box_mesh(10, 4, 2)
    a = cut(m, [53.7, 0, 0], [1, 0, 0])["area_mm2"]
    b = cut(m, [53.7, 0, 0], [-1, 0, 0])["area_mm2"]
    assert a == pytest.approx(b, rel=1e-12)


def test_an_oblique_cut_has_the_expected_larger_area():
    m = box_mesh(20, 4, 2)
    n = [1.0, 0.0, 1.0]
    sec = cut(m, [50.0, 0, 0], n)
    # The plane meets the z faces before the x faces, so the cut is a
    # parallelogram of width 20 mm and slant length 10*sqrt(2).
    assert sec["area_mm2"] == pytest.approx(20.0 * 10.0 * math.sqrt(2), rel=1e-9)


def test_a_normal_that_is_not_unit_length_is_normalised():
    m = box_mesh(10, 4, 2)
    a = cut(m, [53.7, 0, 0], [1, 0, 0])["area_mm2"]
    b = cut(m, [53.7, 0, 0], [7.5, 0, 0])["area_mm2"]
    assert a == pytest.approx(b, rel=1e-12)


def test_a_plane_that_misses_the_part_is_refused():
    m = box_mesh(6, 2, 2)
    with pytest.raises(SectionError, match="intersects no element"):
        cut(m, [500.0, 0, 0], [1, 0, 0])


def test_a_zero_normal_is_refused():
    m = box_mesh(4, 2, 2)
    with pytest.raises(SectionError, match="zero length"):
        cut(m, [50.0, 0, 0], [0, 0, 0])


def test_inconsistent_connectivity_is_refused():
    m = box_mesh(4, 2, 2)
    m["connectivity"] = m["connectivity"].copy()
    m["connectivity"][0, 0] = 10 ** 9
    with pytest.raises(SectionError, match="not in node_tags"):
        cut(m, [53.7, 0, 0], [1, 0, 0])


# ------------------------------------------------------------- resultants
def test_pure_tension_gives_N_over_A():
    m = box_mesh(10, 4, 2)
    forc = tip_force(m, 1000.0, 0.0, 0.0)
    r = resultants(m, forc, [53.7, 0, 0], [1, 0, 0])

    assert r["axial_N"] == pytest.approx(1000.0, rel=1e-9)
    assert r["shear_N"] == pytest.approx(0.0, abs=1e-9)
    # 1000 N over 200 mm^2.
    assert r["sigma_membrane_MPa"] == pytest.approx(5.0, rel=1e-9)
    assert r["tau_average_MPa"] == pytest.approx(0.0, abs=1e-12)
    # The load acts through the centroid, so there is no bending to add.
    assert r["sigma_extreme_MPa"] == pytest.approx(5.0, rel=1e-6)


def test_the_resultant_does_not_depend_on_where_the_section_is_cut():
    """The reason this limit state converges, stated as a test.

    Two different planes through the same prismatic bar carry the same force,
    because equilibrium says so and not because the mesh happens to agree. A
    von Mises peak has no such property - it is whatever the nearest notch and
    the local element size make it.
    """
    m = box_mesh(20, 4, 2)
    forc = tip_force(m, 1000.0, 0.0, 0.0)
    a = resultants(m, forc, [21.3, 0, 0], [1, 0, 0])
    b = resultants(m, forc, [78.9, 0, 0], [1, 0, 0])
    assert a["axial_N"] == pytest.approx(b["axial_N"], rel=1e-12)
    assert a["sigma_membrane_MPa"] == pytest.approx(
        b["sigma_membrane_MPa"], rel=1e-9)


def test_the_resultant_does_not_depend_on_the_mesh_density():
    forc_coarse = None
    vals = []
    for n in (8, 16):
        m = box_mesh(n, 4, 2)
        r = resultants(m, tip_force(m, 1000.0, 0.0, 0.0),
                       [53.7, 0, 0], [1, 0, 0])
        vals.append(r["sigma_membrane_MPa"])
    assert vals[0] == pytest.approx(vals[1], rel=1e-9)
    assert vals[0] == pytest.approx(5.0, rel=1e-9)


def test_transverse_load_gives_the_average_shear():
    m = box_mesh(10, 4, 2)
    r = resultants(m, tip_force(m, 0.0, 0.0, 1000.0), [53.7, 0, 0], [1, 0, 0])
    assert r["axial_N"] == pytest.approx(0.0, abs=1e-9)
    assert r["shear_N"] == pytest.approx(1000.0, rel=1e-9)
    # V/A, the SECTION AVERAGE. A solid rectangle's peak shear is 1.5x this,
    # and `weld_static` records that its pairing is not conservative in tau.
    assert r["tau_average_MPa"] == pytest.approx(5.0, rel=1e-9)


def test_bending_extreme_fibre_converges_to_Mc_over_I():
    """P at the tip, section at x = 53.7: M = P * 46.3, I = 20*10^3/12.

    The fitted extreme fibre is evaluated at cut-facet CENTROIDS, which lie
    inside the true extreme fibre, so a coarse mesh under-reads and refinement
    must close the gap. Both are asserted: the value, and the direction of the
    error.
    """
    P = 1000.0
    lever = 100.0 - 53.7
    I = 20.0 * 10.0 ** 3 / 12.0          # about the y axis, bending in z
    exact = P * lever * 5.0 / I

    errs = []
    for nz in (4, 16):
        m = box_mesh(10, 4, nz)
        r = resultants(m, tip_force(m, 0.0, 0.0, P), [53.7, 0, 0], [1, 0, 0])
        assert r["bending_resolved"] is True
        assert r["axial_N"] == pytest.approx(0.0, abs=1e-9)
        errs.append(abs(abs(r["sigma_extreme_MPa"]) - exact) / exact)

    assert errs[1] < errs[0], (
        f"refining z from 4 to 16 must close the gap to Mc/I, got {errs}")
    assert errs[1] < 0.05, (
        f"at nz=16 the extreme fibre is still {errs[1]:.1%} from Mc/I "
        f"= {exact:.4f} MPa")


def test_bending_sign_follows_the_applied_moment():
    P = 1000.0
    m = box_mesh(10, 4, 8)
    up = resultants(m, tip_force(m, 0.0, 0.0, P), [53.7, 0, 0], [1, 0, 0])
    dn = resultants(m, tip_force(m, 0.0, 0.0, -P), [53.7, 0, 0], [1, 0, 0])
    assert up["sigma_extreme_MPa"] == pytest.approx(
        -dn["sigma_extreme_MPa"], rel=1e-9)
    # The extreme fibre sits on a face, not in the middle of the section.
    assert abs(up["sigma_extreme_at_mm"][2]) > 3.0


def test_combined_tension_and_bending_superpose():
    m = box_mesh(10, 4, 16)
    t = resultants(m, tip_force(m, 2000.0, 0.0, 0.0), [53.7, 0, 0], [1, 0, 0])
    b = resultants(m, tip_force(m, 0.0, 0.0, 500.0), [53.7, 0, 0], [1, 0, 0])
    both = resultants(m, tip_force(m, 2000.0, 0.0, 500.0),
                      [53.7, 0, 0], [1, 0, 0])
    assert both["sigma_membrane_MPa"] == pytest.approx(
        t["sigma_membrane_MPa"], rel=1e-9)
    # Linear elasticity: the peak of the sum is the sum of the peaks here,
    # because both contributions reach their maximum on the same fibre.
    expect = t["sigma_membrane_MPa"] + abs(b["sigma_extreme_MPa"])
    assert abs(both["sigma_extreme_MPa"]) == pytest.approx(expect, rel=1e-6)


def test_a_section_with_no_force_on_either_side_says_so():
    m = box_mesh(8, 2, 2)
    r = resultants(m, {}, [53.7, 0, 0], [1, 0, 0])
    assert r["axial_N"] == 0.0
    assert "free_body_warning" in r
    assert "not evidence of a zero stress" in r["free_body_warning"]


def test_flipping_the_normal_flips_the_sign_of_the_axial_force():
    m = box_mesh(10, 4, 2)
    forc = tip_force(m, 1000.0, 0.0, 0.0)
    a = resultants(m, forc, [53.7, 0, 0], [1, 0, 0])
    b = resultants(m, forc, [53.7, 0, 0], [-1, 0, 0])
    # +x keeps the loaded tip; -x keeps the other half, which carries the
    # equal and opposite reaction. Here the reaction was never supplied, so the
    # far side sums to nothing - and the module says so rather than reporting
    # a quiet zero.
    assert a["axial_N"] == pytest.approx(1000.0, rel=1e-9)
    assert b["nodes_with_force"] == 0
    assert "free_body_warning" in b


# ------------------------------------------------------------ field check
def test_the_field_cross_check_is_reported_when_stress_is_supplied():
    m = box_mesh(8, 2, 2)
    # A uniform uniaxial field: sxx = 5 MPa everywhere, nothing else.
    stress = {int(t): (5.0, 0.0, 0.0, 0.0, 0.0, 0.0)
              for t in m["node_tags"]}
    r = resultants(m, tip_force(m, 1000.0, 0.0, 0.0), [53.7, 0, 0], [1, 0, 0],
                   stress=stress)
    fc = r["field_check"]
    assert fc["available"] is True
    assert fc["sigma_perp_MPa"] == pytest.approx(5.0, rel=1e-9)
    assert fc["tau_MPa"] == pytest.approx(0.0, abs=1e-9)
    assert fc["max_combined_MPa"] == pytest.approx(5.0, rel=1e-9)
    # Integrating the same uniform field over the same area must return the
    # resultant the free body already gave. If these two ever disagree on a
    # uniform field, the area or the traction is wrong.
    assert fc["integrated_axial_N"] == pytest.approx(1000.0, rel=1e-9)


def test_the_field_check_is_absent_when_no_stress_is_supplied():
    m = box_mesh(6, 2, 2)
    r = resultants(m, tip_force(m, 1000.0, 0.0, 0.0), [53.7, 0, 0], [1, 0, 0])
    assert "field_check" not in r
