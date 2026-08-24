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


def select_nodes(mesh: dict, where: dict) -> np.ndarray:
    """Node tags on an axis-aligned plane: {'axis': 'x|y|z', 'at': 'min'|'max'|float,
    'tol': mm (default 0.01)}. Raises MeshError if the selection is empty."""
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
    mask = np.abs(col - target) <= tol
    tags = mesh["node_tags"][mask]
    if len(tags) == 0:
        raise MeshError(
            f"selector {where} matched 0 nodes ({where['axis']}={target}, tol={tol})")
    return tags
