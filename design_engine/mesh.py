"""STEP -> second-order tetrahedral mesh via gmsh.

Element choice (v0): **C3D10 quadratic tets**, not C3D4 linear tets. Linear
tets are overstiff and under-predict peak stress, which is non-conservative
for a stress-based gate — the wrong direction to be wrong in. (Standard FEA
guidance; see e.g. the CalculiX ccx manual §6.2 on C3D4 accuracy warnings.)

Node-ordering note: gmsh's 10-node tet lists the last two midside nodes in
the opposite order from Abaqus/CalculiX C3D10 (gmsh: ...n03, n23, n13 —
Abaqus: ...n03, n13, n23), so we swap the final two connectivity entries when
writing the solver deck. This is verified end-to-end by the Phase 4 analytic
test: a wrong ordering produces distorted/invalid elements and cannot
reproduce the closed-form displacement and stress.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


class MeshError(RuntimeError):
    pass


def mesh_step(step_path: str | Path, max_size_mm: float,
              min_size_mm: float | None = None) -> dict:
    """Mesh a STEP file. Returns {'node_tags', 'coords', 'connectivity'}.

    connectivity rows are 10 node tags in **CalculiX C3D10 order** (the
    gmsh->Abaqus midside swap is already applied here).
    """
    import gmsh

    if max_size_mm <= 0:
        raise MeshError(f"max_size_mm must be > 0, got {max_size_mm}")
    step_path = Path(step_path)
    if not step_path.is_file():
        raise MeshError(f"STEP file not found: {step_path}")

    if gmsh.isInitialized():
        gmsh.finalize()
    gmsh.initialize()
    try:
        gmsh.option.setNumber("General.Terminal", 0)
        gmsh.open(str(step_path))
        gmsh.option.setNumber("Mesh.MeshSizeMax", max_size_mm)
        if min_size_mm:
            gmsh.option.setNumber("Mesh.MeshSizeMin", min_size_mm)
        gmsh.option.setNumber("Mesh.Optimize", 1)
        gmsh.model.mesh.generate(3)
        gmsh.model.mesh.setOrder(2)

        node_tags, coords, _ = gmsh.model.mesh.getNodes()
        coords = np.asarray(coords, dtype=float).reshape(-1, 3)
        node_tags = np.asarray(node_tags, dtype=np.int64)

        elem_tags, elem_nodes = gmsh.model.mesh.getElementsByType(11)  # tet10
        if len(elem_tags) == 0:
            raise MeshError("gmsh produced no 10-node tetrahedra")
        conn = np.asarray(elem_nodes, dtype=np.int64).reshape(-1, 10).copy()
        conn[:, [8, 9]] = conn[:, [9, 8]]  # gmsh -> Abaqus/ccx midside swap

        # boundary triangulation (6-node tris): needed for consistent
        # surface-load assembly; order per tri: 3 corners then 3 midsides
        tri_tags, tri_nodes = gmsh.model.mesh.getElementsByType(9)
        tri6 = (np.asarray(tri_nodes, dtype=np.int64).reshape(-1, 6)
                if len(tri_tags) else np.empty((0, 6), dtype=np.int64))
    finally:
        gmsh.finalize()

    mesh = {"node_tags": node_tags, "coords": coords, "connectivity": conn,
            "tri6": tri6}
    mesh["quality"] = check_element_quality(mesh, max_size_mm)
    return mesh


# 4-point Gauss rule for tetrahedra, plus the 4 corners. Checking only corner
# volumes is not enough for C3D10: on curved faces the midside nodes are
# projected onto the surface, which can invert an element whose corners are
# still fine — exactly the case a coarse mesh on a thin bore wall produces.
_A, _B = 0.5854101966249685, 0.1381966011250105
_CHECK_POINTS = np.array([
    (0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0),
    (_A, _B, _B), (_B, _A, _B), (_B, _B, _A), (_B, _B, _B),
])


def _c3d10_shape_derivatives(g: float, h: float, r: float) -> np.ndarray:
    """d(N_i)/d(g,h,r) for the 10-node tet in Abaqus/CalculiX node order.

    Returns a (3, 10) array. L1..L4 are the barycentric coordinates.
    """
    L1, L2, L3, L4 = 1.0 - g - h - r, g, h, r
    d = np.zeros((3, 10))
    # d/dg
    d[0] = [-(4 * L1 - 1), 4 * L2 - 1, 0.0, 0.0,
            4 * (L1 - L2), 4 * L3, -4 * L3, -4 * L4, 4 * L4, 0.0]
    # d/dh
    d[1] = [-(4 * L1 - 1), 0.0, 4 * L3 - 1, 0.0,
            -4 * L2, 4 * L2, 4 * (L1 - L3), -4 * L4, 0.0, 4 * L4]
    # d/dr
    d[2] = [-(4 * L1 - 1), 0.0, 0.0, 4 * L4 - 1,
            -4 * L2, 0.0, -4 * L3, 4 * (L1 - L4), 4 * L2, 4 * L3]
    return d


def check_element_quality(mesh: dict, max_size_mm: float) -> dict:
    """Reject meshes CalculiX would reject, with an actionable message.

    Evaluates the isoparametric Jacobian determinant of every C3D10 element at
    its corners and Gauss points. A non-positive determinant means the element
    is inverted or degenerate; ccx aborts on these with a bare
    'nonpositive jacobian determinant in element N', which says nothing about
    what to change. Raises MeshError naming the count and the likely cause.
    """
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    conn = mesh["connectivity"]
    xyz = np.stack([np.array([coords[int(t)] for t in row]) for row in conn])
    dets = np.empty((len(conn), len(_CHECK_POINTS)))
    for k, (g, h, r) in enumerate(_CHECK_POINTS):
        dN = _c3d10_shape_derivatives(g, h, r)      # (3, 10)
        J = np.einsum("in,enj->eij", dN, xyz)       # (elements, 3, 3)
        dets[:, k] = np.linalg.det(J)
    min_per_elem = dets.min(axis=1)
    bad = np.flatnonzero(min_per_elem <= 0.0)
    stats = {"elements": int(len(conn)),
             "min_jacobian": float(min_per_elem.min()),
             "degenerate_elements": int(len(bad))}
    if len(bad):
        raise MeshError(
            f"degenerate_mesh: {len(bad)} of {len(conn)} elements have a "
            f"non-positive Jacobian (worst {min_per_elem.min():.4g}); CalculiX "
            f"would abort on these. The mesh size ({max_size_mm} mm) is too "
            f"coarse for the smallest feature — reduce case.mesh.max_size_mm "
            f"below the thinnest wall/radius, or thicken that feature.")
    return stats


def _axis_mask(mesh: dict, where: dict) -> np.ndarray:
    """Boolean mask for one axis-aligned window: {'axis': 'x|y|z',
    'at': 'min'|'max'|float, 'tol': mm (default 0.01)}."""
    allowed = {"axis", "at", "tol"}
    extra = set(where) - allowed
    if extra:
        raise MeshError(f"selector has unexpected keys {sorted(extra)} — allowed: {sorted(allowed)}")
    axis = {"x": 0, "y": 1, "z": 2}.get(where.get("axis"))
    if axis is None:
        raise MeshError(f"selector axis must be x|y|z, got {where.get('axis')!r}")
    at = where.get("at")
    col = mesh["coords"][:, axis]
    if at == "min":
        target = col.min()
    elif at == "max":
        target = col.max()
    elif isinstance(at, (int, float)) and not isinstance(at, bool):
        target = float(at)
    else:
        raise MeshError(f"selector 'at' must be 'min', 'max' or a number, got {at!r}")
    tol = where.get("tol", 0.01)
    return np.abs(col - target) <= tol


def planar_face_candidates(mesh: dict, axis: str,
                           min_tris: int = 4) -> list[dict]:
    """Coordinate values along `axis` that carry a real planar boundary face.

    Exists because 'at': 'max' is a *coordinate extremum*, not a face: on a
    part whose extremum is a curved tangent (e.g. a hinge knuckle barrel
    protruding past its flat leaf), 'max' selects a tangent sliver carrying no
    complete boundary triangle, while the flat face the user meant sits at a
    smaller coordinate. Used to turn that into an actionable error instead of
    a bare 'matched 0 nodes' / 'no boundary triangles'.

    Returns [{'at', 'nodes', 'triangles'}] sorted by triangle count, richest
    first — a face carrying many complete triangles is a real planar face.
    """
    idx = {"x": 0, "y": 1, "z": 2}.get(axis)
    if idx is None:
        raise MeshError(f"axis must be x|y|z, got {axis!r}")
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    buckets: dict[float, dict] = {}
    for row in mesh["tri6"]:
        vals = [coords[int(n)][idx] for n in row]
        lo, hi = min(vals), max(vals)
        if hi - lo > 1e-6:          # triangle is not flat in this axis
            continue
        key = round((lo + hi) / 2.0, 4)
        b = buckets.setdefault(key, {"at": key, "nodes": set(), "triangles": 0})
        b["triangles"] += 1
        b["nodes"].update(int(n) for n in row)
    out = [{"at": b["at"], "nodes": len(b["nodes"]), "triangles": b["triangles"]}
           for b in buckets.values() if b["triangles"] >= min_tris]
    return sorted(out, key=lambda d: -d["triangles"])


def describe_axis_options(mesh: dict, axis: str, limit: int = 4) -> str:
    """Human-readable planar-face suggestions for an axis, for error messages."""
    cands = planar_face_candidates(mesh, axis)
    if not cands:
        return f"no flat boundary face found along {axis}"
    bits = [f"{axis}={c['at']:g} ({c['triangles']} tris)" for c in cands[:limit]]
    return "flat faces along %s: %s" % (axis, ", ".join(bits))


def _cylinder_mask(mesh: dict, spec: dict) -> np.ndarray:
    """Mask for nodes on a cylindrical surface (e.g. a bore wall).

    {'axis': 'x|y|z', 'center': [a, b], 'r': mm, 'tol': mm (default 0.05),
     'half': [da, db] optional}

    'center' is in the plane perpendicular to 'axis', in that plane's two
    remaining coordinates in x,y,z order (axis 'z' -> center is [x, y]).
    'half' keeps only the nodes whose outward radial direction has a positive
    dot product with the given in-plane vector — the loaded half of a bore,
    which is closer to how a pin actually bears than wrapping the full circle.

    IMPORTANT (documented, not silently assumed): selecting a bore surface
    lets you APPLY A LOAD to it, but the load applied is still a uniform
    traction over the selected patch. Real pin bearing is a contact problem
    with a roughly cosine pressure distribution and a contact patch that
    depends on clearance and load. This is a modelling simplification, not
    contact mechanics; treat resulting local bore stresses as indicative.
    """
    allowed = {"axis", "center", "r", "tol", "half"}
    extra = set(spec) - allowed
    if extra:
        raise MeshError(
            f"cylinder selector has unexpected keys {sorted(extra)} — "
            f"allowed: {sorted(allowed)}")
    idx = {"x": 0, "y": 1, "z": 2}.get(spec.get("axis"))
    if idx is None:
        raise MeshError(
            f"cylinder selector axis must be x|y|z, got {spec.get('axis')!r}")
    center = spec.get("center")
    if not (isinstance(center, list) and len(center) == 2
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in center)):
        raise MeshError("cylinder selector 'center' must be [a, b] numbers")
    r = spec.get("r")
    if not isinstance(r, (int, float)) or isinstance(r, bool) or r <= 0:
        raise MeshError(f"cylinder selector 'r' must be > 0, got {r!r}")
    tol = spec.get("tol", 0.05)
    perp = [i for i in (0, 1, 2) if i != idx]
    da = mesh["coords"][:, perp[0]] - float(center[0])
    db = mesh["coords"][:, perp[1]] - float(center[1])
    radius = np.sqrt(da ** 2 + db ** 2)
    mask = np.abs(radius - float(r)) <= tol
    half = spec.get("half")
    if half is not None:
        if not (isinstance(half, list) and len(half) == 2):
            raise MeshError("cylinder selector 'half' must be [da, db]")
        hn = np.hypot(float(half[0]), float(half[1]))
        if hn == 0:
            raise MeshError("cylinder selector 'half' must be a nonzero vector")
        with np.errstate(invalid="ignore", divide="ignore"):
            dot = (da * float(half[0]) + db * float(half[1])) / (radius * hn)
        mask &= np.nan_to_num(dot, nan=-1.0) > 0.0
    return mask


def _sub_mask(mesh: dict, where: dict) -> np.ndarray:
    """One selector term: a planar axis window or a cylindrical surface."""
    if "cylinder" in where:
        extra = set(where) - {"cylinder"}
        if extra:
            raise MeshError(
                f"cylinder selector takes no sibling keys, got {sorted(extra)}")
        return _cylinder_mask(mesh, where["cylinder"])
    return _axis_mask(mesh, where)


def select_nodes(mesh: dict, where: dict) -> np.ndarray:
    """Node tags matching a selector.

    Single-axis window: {'axis': 'x|y|z', 'at': 'min'|'max'|float,
    'tol': mm (default 0.01)}.

    Cylindrical surface: {'cylinder': {'axis', 'center', 'r', 'tol', 'half'}}
    — for bore walls and other round surfaces that no axis window can reach.
    See _cylinder_mask for the important modelling caveat about bearing loads.

    Compound (AND) window: {'all': [selector, selector, ...]} — inner selectors
    may be either axis windows or cylinder selectors, and are intersected.
    Needed for a load or constraint on an interior strip of a face rather than
    a whole face, e.g. a mid-span loading patch on a beam's top face: the top
    face alone (y='max') is one plane; the load patch is that plane
    intersected with a narrow z-window around the load point.

    NOTE on 'at': 'min'/'max': these are coordinate extrema, NOT faces. If the
    part's extremum along that axis is a curved tangent (a protruding round
    boss or barrel), 'max' selects a sliver there rather than the flat face you
    probably meant. planar_face_candidates() lists the real flat faces along an
    axis, and load assembly reports them when a selection carries no face.

    Raises MeshError if the selection (or, for 'all', the intersection) is
    empty — an empty selection is always a spec error, never a silent no-op.
    """
    if "all" in where:
        extra = set(where) - {"all"}
        if extra:
            raise MeshError(
                f"compound selector 'all' takes no other keys, got {sorted(extra)}")
        subs = where["all"]
        if not isinstance(subs, list) or len(subs) < 2:
            raise MeshError(
                "selector 'all' must be a list of 2 or more axis selectors")
        mask = np.logical_and.reduce([_sub_mask(mesh, w) for w in subs])
    else:
        mask = _sub_mask(mesh, where)
    tags = mesh["node_tags"][mask]
    if len(tags) == 0:
        raise MeshError(f"selector {where} matched 0 nodes")
    return tags
