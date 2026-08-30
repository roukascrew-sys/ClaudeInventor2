"""Interpolate a solved displacement field onto arbitrary points.

WHY THIS EXISTS
Submodelling needs the global solve's displacements imposed on the cut
surfaces of a small region. CalculiX can do that itself with `*SUBMODEL`, and
on this project's meshes it costs 88-182 ms per driven node and scales
superlinearly - on the real geometry it did not finish inside a 900 s timeout.
That put the only affordable route to mesh convergence out of reach.

So the interpolation is written here. It is not novel numerics: locating the
element containing a point and evaluating its shape functions is the oldest
arithmetic in finite elements. What matters is that it REFUSES rather than
extrapolates, because a driven node falling outside the global mesh has no
displacement to inherit, and inventing one silently drives the submodel with a
fiction that nothing downstream can detect.

THE ACCEPTANCE TEST IS A PATCH TEST
C3D10 elements carry quadratic fields exactly. So if this code is right,
imposing any quadratic displacement field on a mesh's nodes and interpolating
anywhere inside must return that field to machine precision.

A LINEAR field is not a sufficient test, and it is the one that feels
convincing. The reason is completeness order, not permutation: a scheme can
reproduce linear fields exactly and still not be quadratic - a corner-only
interpolation does exactly that. So passing a linear patch test is no evidence
that the mid-side terms or the node ordering are right, and node ordering is
the easiest thing here to get wrong. Both claims are tested.

WHAT IT DOES NOT DO
It does not check that the global solve was any good. Interpolating a
converged field and interpolating a garbage field are the same arithmetic and
this module cannot tell them apart - `classify_peak` and `blend_resolution`
are the checks that can.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

#: Barycentric slack when deciding a point lies inside a tetrahedron. A driven
#: node sits ON a cut surface by construction, so it lands exactly on element
#: faces and rounding pushes it fractionally outside. Generous enough to
#: absorb that, tight enough that a genuinely external point is still refused.
INSIDE_TOL = 1e-6

#: frd float fields run together with no separator, and the exponent must be
#: BOUNDED. A real line from a real solve:
#:
#:     -1    106891-4.71940E+0029.03567E-0012.16875E+002
#:
#: is -471.940, 0.903567, 216.875. An unbounded `\d+` exponent greedily eats
#: the next field's leading digit and yields -4.7194e+29 - which then produced
#: barycentric coordinates of 1e15 and interpolated displacements to match.
#: Every synthetic test passed; only a real results file caught it.
#:
#: `{2,3}` accepts this build's three-digit exponents and classic two-digit
#: ones, where positives carry a leading space so digits never abut. Same
#: pattern as `fea._FRD_FLOAT`, deliberately duplicated rather than imported:
#: this module must not depend on the solver layer.
_FLOAT = re.compile(r"[-+]?\d\.\d+E[-+]\d{2,3}")


class InterpolationError(ValueError):
    """A field that cannot honestly be carried to the requested points."""


def read_frd(path) -> dict:
    """Nodes, C3D10 connectivity and nodal displacements, in ONE pass.

    One pass because these files are large - the jetpack global solve produces
    394 MB - and walking it three times to collect three blocks is three times
    the I/O for nothing.
    """
    path = Path(path)
    if not path.is_file():
        raise InterpolationError(f"no results file at {path}")

    coords: dict[int, tuple] = {}
    elements: list[tuple] = []
    disp: dict[int, tuple] = {}
    block = None
    pending = None

    with path.open("r", encoding="ascii", errors="replace") as fh:
        for line in fh:
            if line.startswith("    2C"):
                block = "nodes"
                continue
            if line.startswith("    3C"):
                block = "elements"
                continue
            if line.startswith(" -4  DISP"):
                block = "disp"
                continue
            if line.startswith(" -4") or line.startswith(" -3"):
                block = None            # some other result block, or block end
                continue

            if block == "nodes" and line.startswith(" -1"):
                tag = int(line[3:13])
                v = _FLOAT.findall(line[13:])
                if len(v) >= 3:
                    coords[tag] = (float(v[0]), float(v[1]), float(v[2]))
            elif block == "elements":
                if line.startswith(" -1"):
                    pending = int(line.split()[1])
                elif line.startswith(" -2") and pending is not None:
                    tags = [int(t) for t in line[3:].split()]
                    if len(tags) == 10:
                        elements.append((pending, tags))
                    pending = None
            elif block == "disp" and line.startswith(" -1"):
                tag = int(line[3:13])
                v = _FLOAT.findall(line[13:])
                if len(v) >= 3:
                    disp[tag] = (float(v[0]), float(v[1]), float(v[2]))

    if not coords:
        raise InterpolationError(f"{path}: no nodes found")
    if not elements:
        raise InterpolationError(
            f"{path}: no C3D10 elements found. Interpolation needs the global "
            f"MESH, not only its results")
    if not disp:
        raise InterpolationError(
            f"{path}: no DISP block. The global solve must be written with "
            f"*NODE FILE, U or there is nothing to carry")
    return {"coords": coords, "elements": elements, "disp": disp}


def shape_c3d10(bary) -> np.ndarray:
    """The ten quadratic tetrahedron shape functions at barycentric `bary`.

    CalculiX/Abaqus C3D10 ordering: nodes 1-4 are the corners, then the
    mid-side nodes of edges 1-2, 2-3, 1-3, 1-4, 2-4, 3-4 in that order.
    """
    l1, l2, l3, l4 = bary
    return np.array([
        l1 * (2.0 * l1 - 1.0),
        l2 * (2.0 * l2 - 1.0),
        l3 * (2.0 * l3 - 1.0),
        l4 * (2.0 * l4 - 1.0),
        4.0 * l1 * l2,
        4.0 * l2 * l3,
        4.0 * l1 * l3,
        4.0 * l1 * l4,
        4.0 * l2 * l4,
        4.0 * l3 * l4,
    ])


def barycentric(point, corners) -> np.ndarray:
    """Barycentric coordinates of a point in a straight-edged tetrahedron.

    Uses the four CORNER nodes only. For a tetrahedron with straight edges -
    what gmsh produces away from curved surfaces - the geometric map is affine,
    so this is exact and needs no iteration. On elements whose mid-side nodes
    are pulled onto a curved boundary it is a first-order approximation to the
    true natural coordinates, and `interpolate` says so in its result rather
    than leaving the caller to assume otherwise.
    """
    p0, p1, p2, p3 = (np.asarray(c, dtype=float) for c in corners[:4])
    m = np.column_stack([p0 - p3, p1 - p3, p2 - p3])
    try:
        l = np.linalg.solve(m, np.asarray(point, dtype=float) - p3)
    except np.linalg.LinAlgError:
        return np.array([np.nan] * 4)          # degenerate element
    return np.array([l[0], l[1], l[2], 1.0 - l.sum()])


class _Grid:
    """Uniform bucket grid over element bounding boxes.

    A linear scan over 215,000 elements per driven node is why ccx's own
    interpolation is unusable here. The grid turns that into a handful of
    candidates. It is a dozen lines because these elements are small and
    roughly uniform; a KD-tree would be more general and no faster on this
    shape of data.
    """

    def __init__(self, coords, elements, target_per_cell: float = 2.0):
        pts = np.array([coords[t] for _, tags in elements for t in tags[:4]])
        self.lo = pts.min(axis=0)
        span = np.maximum(pts.max(axis=0) - self.lo, 1e-9)
        n = max(1, int(round((len(elements) / target_per_cell) ** (1 / 3))))
        self.n = np.array([n, n, n])
        self.cell = span / self.n
        self.buckets: dict[tuple, list] = {}
        for idx, (_, tags) in enumerate(elements):
            c = np.array([coords[t] for t in tags[:4]])
            lo, hi = self._cell_of(c.min(axis=0)), self._cell_of(c.max(axis=0))
            for i in range(lo[0], hi[0] + 1):
                for j in range(lo[1], hi[1] + 1):
                    for k in range(lo[2], hi[2] + 1):
                        self.buckets.setdefault((i, j, k), []).append(idx)

    def _cell_of(self, p):
        raw = ((np.asarray(p) - self.lo) / self.cell).astype(int)
        return np.clip(raw, 0, self.n - 1)

    def candidates(self, point) -> list:
        return self.buckets.get(tuple(self._cell_of(point)), [])


def interpolate(field=None, points=None, coords=None, elements=None,
                disp=None, tol: float = INSIDE_TOL) -> dict:
    """Carry a solved nodal field onto `points`.

    `field` is the dict from `read_frd`, or the three parts may be passed
    directly. `points` maps a label to (x, y, z).

    A point landing in no element is reported in `outside` and is ABSENT from
    `values` - never extrapolated. Driving a submodel from an invented
    displacement is the failure this module exists to avoid, and nothing
    downstream could detect it.
    """
    if field is not None:
        coords, elements, disp = field["coords"], field["elements"], field["disp"]
    if not coords or not elements or not disp:
        raise InterpolationError("coords, elements and disp are all required")
    points = points or {}

    grid = _Grid(coords, elements)
    values, outside, degenerate = {}, [], 0

    for label, p in points.items():
        p = np.asarray(p, dtype=float)
        found = None
        for idx in grid.candidates(p):
            _, tags = elements[idx]
            bary = barycentric(p, [coords[t] for t in tags[:4]])
            if np.isnan(bary).any():
                degenerate += 1
                continue
            if bary.min() >= -tol:
                found = (tags, bary)
                break
        if found is None:
            outside.append({"label": label,
                            "point": [round(float(v), 4) for v in p]})
            continue
        tags, bary = found
        weights = shape_c3d10(np.clip(bary, 0.0, 1.0))
        u = np.zeros(3)
        for w, t in zip(weights, tags):
            if t not in disp:
                raise InterpolationError(
                    f"node {t} appears in the mesh but carries no "
                    f"displacement; the results file is incomplete")
            u += w * np.asarray(disp[t])
        values[label] = tuple(float(v) for v in u)

    return {"values": values, "outside": outside,
            "requested": len(points), "carried": len(values),
            "degenerate_elements_skipped": degenerate,
            "note": ("barycentric coordinates come from the corner nodes: "
                     "exact for straight-edged tetrahedra, first-order on "
                     "elements whose mid-side nodes lie on a curved boundary")}


def boundary_cards(values, dofs=(1, 2, 3)) -> list:
    """`*BOUNDARY` lines imposing the interpolated displacements.

    This replaces `*SUBMODEL` outright: rather than asking the solver to
    interpolate, the deck states the answer. Node numbers are the SUBMODEL's,
    so `values` must be keyed by submodel node tag.
    """
    if not values:
        raise InterpolationError(
            "no displacements to impose: the submodel would be unrestrained, "
            "and a solve with rigid-body modes reports nothing about the "
            "structure")
    out = ["*BOUNDARY"]
    for tag in sorted(values):
        u = values[tag]
        for dof in dofs:
            out.append(f"{tag}, {dof}, {dof}, {u[dof - 1]:.9g}")
    return out
