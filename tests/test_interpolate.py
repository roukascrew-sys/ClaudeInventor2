"""Submodel displacement interpolation, written here because ccx's is unusable.

The acceptance test is a PATCH TEST. C3D10 elements carry quadratic fields
exactly, so imposing a quadratic displacement field on the nodes and
interpolating anywhere inside must return that field to machine precision.

The linear patch test is deliberately NOT treated as sufficient, though not
for the reason first written here: a linear field does NOT survive mid-side
scrambling, and the measurement said so. The real reason is completeness
order - a scheme can reproduce linear fields exactly and still not be
quadratic, which a corner-only interpolation demonstrates. Two tests below
pin both halves: that scrambling breaks the quadratic reproduction, and that
linear reproduction does not imply quadratic reproduction.
"""

import numpy as np
import pytest

from design_engine.interpolate import (INSIDE_TOL, InterpolationError,
                                       barycentric, boundary_cards,
                                       interpolate, read_frd, shape_c3d10)

# A single reference tetrahedron with straight edges. Corners first, then the
# mid-side nodes of edges 1-2, 2-3, 1-3, 1-4, 2-4, 3-4 - CalculiX C3D10 order.
C = [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 3.0, 0.0), (0.0, 0.0, 4.0)]
MID = [tuple((np.array(C[a]) + np.array(C[b])) / 2.0)
       for a, b in [(0, 1), (1, 2), (0, 2), (0, 3), (1, 3), (2, 3)]]
NODES = C + MID
TAGS = list(range(1, 11))
COORDS = dict(zip(TAGS, NODES))
ELEMENTS = [(1, TAGS)]

INSIDE = {"a": (0.4, 0.5, 0.6), "b": (0.2, 0.2, 0.2), "c": (0.1, 1.0, 1.5)}


def _field(fn):
    return {t: fn(np.array(COORDS[t])) for t in TAGS}


def _carry(fn, points=INSIDE):
    return interpolate(coords=COORDS, elements=ELEMENTS, disp=_field(fn),
                       points=points)


# ------------------------------------------------------------- patch tests
def test_a_linear_field_is_reproduced_exactly():
    def lin(p):
        return (1.0 + 2.0 * p[0], -3.0 + 0.5 * p[1], 7.0 * p[2] - p[0])

    got = _carry(lin)
    assert got["carried"] == 3
    for label, p in INSIDE.items():
        assert got["values"][label] == pytest.approx(lin(np.array(p)), abs=1e-10)


def test_a_QUADRATIC_field_is_reproduced_exactly():
    """The test that matters. C3D10 is a quadratic element, so this must be
    exact - and unlike the linear case it cannot pass with the mid-side nodes
    in the wrong order."""
    def quad(p):
        x, y, z = p
        return (x * x + 2.0 * y * z, y * y - x * z, 3.0 * z * z + x * y)

    got = _carry(quad)
    for label, p in INSIDE.items():
        assert got["values"][label] == pytest.approx(quad(np.array(p)), abs=1e-9)


def test_the_quadratic_patch_test_actually_catches_a_node_ordering_error():
    """Proof that the previous test has teeth.

    Permuting two mid-side nodes must break the quadratic reproduction. If
    this test ever passes, the quadratic patch test above has stopped being
    evidence of anything.
    """
    def quad(p):
        x, y, z = p
        return (x * x + 2.0 * y * z, y * y - x * z, 3.0 * z * z + x * y)

    scrambled = list(TAGS)
    scrambled[4], scrambled[5] = scrambled[5], scrambled[4]   # swap two mids
    got = interpolate(coords=COORDS, elements=[(1, scrambled)],
                      disp=_field(quad), points=INSIDE)
    worst = max(abs(np.array(got["values"][k]) - quad(np.array(p))).max()
                for k, p in INSIDE.items())
    assert worst > 1e-3, "a mid-side swap must break the quadratic field"


def test_linear_reproduction_does_not_imply_quadratic_reproduction():
    """Why the quadratic patch test is the acceptance criterion.

    A first attempt at this file asserted that a linear field survives
    mid-side scrambling, so the linear test proved nothing. That was wrong and
    the measurement said so - permuting two mid-side nodes moves their
    positions, so a linear field breaks too.

    The real reason the quadratic test is required is completeness order: a
    scheme can reproduce linear fields exactly and still not be quadratic. A
    corner-only linear interpolation demonstrates it - exact on a linear
    field, wrong on a quadratic one. Passing the linear test is therefore no
    evidence that C3D10's mid-side terms are right.
    """
    def lin(p):
        return (1.0 + 2.0 * p[0], -3.0 + 0.5 * p[1], 7.0 * p[2] - p[0])

    def quad(p):
        x, y, z = p
        return (x * x + 2.0 * y * z, y * y - x * z, 3.0 * z * z + x * y)

    def corner_only(fn, point):
        bary = barycentric(point, C)
        return sum(b * np.array(fn(np.array(C[i]))) for i, b in enumerate(bary))

    for p in INSIDE.values():
        assert corner_only(lin, p) == pytest.approx(lin(np.array(p)), abs=1e-10)

    worst = max(abs(corner_only(quad, p) - np.array(quad(np.array(p)))).max()
                for p in INSIDE.values())
    assert worst > 1e-3, "a linear scheme must fail a quadratic field"


# ------------------------------------------------------------- the refusals
def test_a_point_outside_every_element_is_reported_not_extrapolated():
    """The refusal this module exists for. A driven node with no element to
    inherit from has no displacement, and inventing one would drive the
    submodel from a fiction that nothing downstream could detect."""
    got = _carry(lambda p: (1.0, 2.0, 3.0), points={"far": (50.0, 50.0, 50.0)})
    assert got["values"] == {}
    assert got["carried"] == 0
    assert got["outside"][0]["label"] == "far"


def test_some_carried_and_some_outside_is_reported_as_both():
    got = _carry(lambda p: (p[0], 0.0, 0.0),
                 points={"in": (0.2, 0.2, 0.2), "out": (-9.0, 0.0, 0.0)})
    assert set(got["values"]) == {"in"}
    assert [o["label"] for o in got["outside"]] == ["out"]
    assert got["requested"] == 2 and got["carried"] == 1


def test_a_node_missing_its_displacement_is_refused():
    """A partially-written results file must not yield a partial answer."""
    disp = _field(lambda p: (1.0, 1.0, 1.0))
    del disp[7]
    with pytest.raises(InterpolationError, match="carries no displacement"):
        interpolate(coords=COORDS, elements=ELEMENTS, disp=disp, points=INSIDE)


def test_missing_inputs_are_refused():
    with pytest.raises(InterpolationError, match="all required"):
        interpolate(coords=COORDS, elements=ELEMENTS, disp={}, points=INSIDE)


# --------------------------------------------------------------- geometry
def test_a_corner_gets_its_own_displacement():
    disp = {t: (float(t), 0.0, 0.0) for t in TAGS}
    got = interpolate(coords=COORDS, elements=ELEMENTS, disp=disp,
                      points={"c1": C[0], "c2": C[1]})
    assert got["values"]["c1"][0] == pytest.approx(1.0, abs=1e-9)
    assert got["values"]["c2"][0] == pytest.approx(2.0, abs=1e-9)


def test_shape_functions_are_a_partition_of_unity():
    """They must sum to 1 everywhere, or the interpolation adds or loses
    magnitude depending where the point sits."""
    for bary in ([0.25, 0.25, 0.25, 0.25], [1.0, 0, 0, 0], [0.1, 0.2, 0.3, 0.4]):
        assert shape_c3d10(np.array(bary)).sum() == pytest.approx(1.0, abs=1e-12)


def test_barycentric_of_a_corner_is_a_unit_vector():
    assert barycentric(C[0], C) == pytest.approx([1, 0, 0, 0], abs=1e-12)
    assert barycentric(C[3], C) == pytest.approx([0, 0, 0, 1], abs=1e-12)


def test_a_degenerate_element_is_skipped_not_divided_by():
    flat = [(0, 0, 0), (1, 0, 0), (2, 0, 0), (3, 0, 0)]     # collinear
    assert np.isnan(barycentric((0.5, 0, 0), flat)).any()


def test_a_point_on_a_face_is_inside_within_tolerance():
    """Driven nodes sit ON cut surfaces by construction, so this is the normal
    case rather than an edge case."""
    got = _carry(lambda p: (p[0] + p[1] + p[2], 0.0, 0.0),
                 points={"on_face": (0.0, 1.0, 1.0)})
    assert got["carried"] == 1
    assert got["values"]["on_face"][0] == pytest.approx(2.0, abs=1e-9)


# ------------------------------------------------------------ deck output
def test_boundary_cards_impose_all_three_translations():
    cards = boundary_cards({7: (0.1, -0.2, 0.3)})
    assert cards[0] == "*BOUNDARY"
    assert "7, 1, 1, 0.1" in cards[1]
    assert len([c for c in cards if c.startswith("7,")]) == 3


def test_an_empty_displacement_set_is_refused():
    with pytest.raises(InterpolationError, match="unrestrained"):
        boundary_cards({})


def test_cards_are_ordered_so_a_deck_diff_is_readable():
    cards = boundary_cards({9: (0, 0, 0), 2: (0, 0, 0)})
    assert cards[1].startswith("2,") and cards[4].startswith("9,")


# ----------------------------------------------------------------- reading
def test_a_missing_results_file_is_named(tmp_path):
    with pytest.raises(InterpolationError, match="no results file"):
        read_frd(tmp_path / "nope.frd")


def test_a_results_file_without_a_mesh_is_refused(tmp_path):
    """Displacements alone cannot be interpolated: without the elements there
    is nothing to locate a point in."""
    p = tmp_path / "job.frd"
    p.write_text("    1C\n -4  DISP        4    1\n"
                 " -1         1 1.00000E+000 0.00000E+000 0.00000E+000\n",
                 encoding="ascii")
    with pytest.raises(InterpolationError, match="no nodes|no C3D10"):
        read_frd(p)


# ---------------------------------------------------- the real-file regression
def test_concatenated_frd_floats_parse_with_a_bounded_exponent(tmp_path):
    """A real line from a real solve, and the bug every synthetic test missed.

    frd values run together with no separator. An unbounded exponent quantifier
    greedily eats the next field's leading digit: "-4.71940E+002" followed by
    "9.03567E-001" parses as -4.7194e+29. That produced barycentric
    coordinates of order 1e15 and interpolated displacements to match, on a
    file where every unit test still passed.
    """
    p = tmp_path / "job.frd"
    p.write_text(
        "    2C                             1                             1\n"
        " -1    106891-4.71940E+0029.03567E-0012.16875E+002\n"
        "    3C                             1                             1\n"
        " -1         1    6    0    1\n"
        " -2    106891    106891    106891    106891    106891    106891"
        "    106891    106891    106891    106891\n"
        " -4  DISP        4    1\n"
        " -1    106891 1.00000E+000 0.00000E+000 0.00000E+000\n",
        encoding="ascii")
    got = read_frd(p)
    assert got["coords"][106891] == pytest.approx(
        (-471.940, 0.903567, 216.875), abs=1e-6)


def test_a_two_digit_exponent_build_still_parses(tmp_path):
    """Older ccx builds print two-digit exponents with a leading space on
    positives. The bounded quantifier has to accept both."""
    p = tmp_path / "job.frd"
    p.write_text(
        "    2C                             1                             1\n"
        " -1         7-1.25000E+01 3.50000E+00 2.00000E+00\n"
        "    3C                             1                             1\n"
        " -1         1    6    0    1\n"
        " -2         7         7         7         7         7         7"
        "         7         7         7         7\n"
        " -4  DISP        4    1\n"
        " -1         7 1.00000E+00 0.00000E+00 0.00000E+00\n",
        encoding="ascii")
    got = read_frd(p)
    assert got["coords"][7] == pytest.approx((-12.5, 3.5, 2.0), abs=1e-9)
