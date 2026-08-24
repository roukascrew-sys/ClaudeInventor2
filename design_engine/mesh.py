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

    return {"node_tags": node_tags, "coords": coords, "connectivity": conn,
            "tri6": tri6}


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
