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


def _mesh_once(step_path: str | Path, max_size_mm: float,
              min_size_mm: float | None = None,
              refine: dict | None = None) -> dict:
    """Mesh a STEP file. Returns {'node_tags', 'coords', 'connectivity'}.

    connectivity rows are 10 node tags in **CalculiX C3D10 order** (the
    gmsh->Abaqus midside swap is already applied here).

    `refine` grades the mesh instead of sizing it uniformly:

        {"centre": (x, y, z), "radius": mm, "size": mm}

    WHY GRADING IS NOT A LUXURY HERE. A submodel has two requirements that
    pull against each other under a uniform mesh. The cut boundary has to
    stand well off from the stress being read, or the peak is an artefact of
    the imposed displacements - measured 2026-09-01, +150.6% between rungs
    with every peak sitting ON a driven node. And the feature has to be
    resolved, which for a 1 mm blend means sub-millimetre elements. Node count
    grows with the CUBE of region size, so satisfying both uniformly put a
    12 mm box at 478,512 nodes and a 2400 s timeout at 4,437 MB.

    Grading decouples them: fine inside the ball, coarse at the cut. The
    region can then be enlarged without paying for it everywhere.
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

        if refine:
            for key in ("centre", "radius", "size"):
                if key not in refine:
                    raise MeshError(
                        f"refine.{key} is required; got {sorted(refine)}")
            fine = float(refine["size"])
            rad = float(refine["radius"])
            if fine <= 0 or rad <= 0:
                raise MeshError(
                    f"refine.size and refine.radius must be > 0, got "
                    f"{fine} and {rad}")
            if fine > max_size_mm:
                raise MeshError(
                    f"refine.size {fine} is COARSER than max_size_mm "
                    f"{max_size_mm}, so the 'refinement' would coarsen the "
                    f"region it is supposed to resolve")
            cx, cy, cz = (float(v) for v in refine["centre"])
            # A Ball field: `size` inside the radius, `max_size_mm` outside,
            # blended over Thickness so the transition does not itself create
            # badly shaped elements.
            fid = gmsh.model.mesh.field.add("Ball")
            gmsh.model.mesh.field.setNumber(fid, "XCenter", cx)
            gmsh.model.mesh.field.setNumber(fid, "YCenter", cy)
            gmsh.model.mesh.field.setNumber(fid, "ZCenter", cz)
            gmsh.model.mesh.field.setNumber(fid, "Radius", rad)
            gmsh.model.mesh.field.setNumber(fid, "Thickness", rad)
            gmsh.model.mesh.field.setNumber(fid, "VIn", fine)
            gmsh.model.mesh.field.setNumber(fid, "VOut", max_size_mm)
            gmsh.model.mesh.field.setAsBackgroundMesh(fid)
            # Without these the CAD's own curvature and point sizes fight the
            # field and the grading silently does not happen.
            gmsh.option.setNumber("Mesh.MeshSizeExtendFromBoundary", 0)
            gmsh.option.setNumber("Mesh.MeshSizeFromPoints", 0)
            gmsh.option.setNumber("Mesh.MeshSizeFromCurvature", 0)
        # Optimises the LINEAR tets, which is not the same thing as the
        # high-order repair below: it runs before setOrder(2) and so cannot
        # undo an inversion that curving introduces.
        gmsh.option.setNumber("Mesh.Optimize", 1)
        gmsh.model.mesh.generate(3)
        gmsh.model.mesh.setOrder(2)

        # REPAIR CURVED ELEMENTS, then let check_element_quality judge.
        #
        # setOrder(2) inserts midside nodes and projects them onto the CAD
        # surface. Where curvature is high relative to the element, that
        # projection can push a midside node far enough to invert the element -
        # so a mesh whose linear tets were all sound acquires a negative
        # Jacobian purely from being curved, and refining can create the
        # problem rather than remove it.
        #
        # Measured on the jetpack junction 2026-09-30: the 0.4 mm ladder rung
        # produced ONE non-positive element in 1,329,310 (worst -0.131), which
        # discarded the whole mesh. A marginal negative on 0.000075% of
        # elements is the signature of curving, not of a mesh too coarse for
        # its features - the 0.8 mm rung immediately before it was fine.
        #
        # gmsh's "HighOrderFast" pass exists for exactly this. It is attempted
        # rather than assumed: the gate below still has the final say, so a
        # failed or unavailable optimiser degrades to the previous behaviour
        # instead of quietly passing a bad mesh.
        try:
            gmsh.option.setNumber("Mesh.HighOrderOptimize", 4)  # fast curving
            gmsh.model.mesh.optimize("HighOrderFast", force=True)
        except Exception:                        # pragma: no cover - gmsh build
            pass

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
    mesh["quality"] = check_element_quality(
        mesh, float(refine["size"]) if refine else max_size_mm)
    return mesh


#: Deterministic size perturbations tried when a mesh is refused for CURVED
#: high-order elements. Deterministic, not random, so the same request always
#: produces the same mesh - the evaluation cache keys on the request, and a
#: mesh that varied run to run would make a cached safety factor meaningless.
#:
#: Measured on the jetpack junction 2026-09-30, base 1.6 mm, ball at 0.40 mm:
#:
#:   0.40  refused  1 bad of   988,697  worst -0.6854
#:   0.41  refused  2 bad of   931,610  worst -0.228
#:   0.39  refused  1 bad of 1,050,139  worst -0.01425
#:   0.42  MESHED   0 bad of   881,852  worst +0.01369
#:   0.38  refused  3 bad of 1,115,644  worst -0.8486
#:
#: The bad count goes 1, 2, 1, 0, 3 with no trend. Refinement does not
#: monotonically improve it, which is what distinguishes a stochastic
#: tetrahedralisation artefact from a mesh genuinely too coarse for a feature -
#: and it is why retrying the SAME size would be pointless while retrying a
#: nudged one works.
_RETRY_FACTORS = (1.0, 1.05, 0.95, 1.10, 0.90)


def mesh_step(step_path: str | Path, max_size_mm: float,
              min_size_mm: float | None = None,
              refine: dict | None = None) -> dict:
    """Mesh, retrying a nudged size when curving inverts a few elements.

    A single inverted curved element out of a million is not a resolution
    failure and cannot be refined away - see `_RETRY_FACTORS` for the
    measurement. It is also not something to tolerate: CalculiX aborts on a
    non-positive Jacobian, so a "tolerant" gate would trade a loud refusal for
    a failed solve, or worse a silent wrong answer. So the mesh is REPAIRED by
    asking gmsh for a slightly different size, and the gate keeps its veto.

    Only the curving class is retried. A mesh genuinely too coarse for a thin
    feature is refused on the first attempt, because nudging the size by 5%
    will not fix it and five attempts would just cost five times as long to say
    so.

    `quality["size_requested"]`, `["size_used"]` and `["retries"]` record what
    actually happened. Nothing here is allowed to be silent: a caller that
    recorded the requested size while the mesh was built at another would put a
    wrong number in the log, and the log is the source of truth.
    """
    target = float(refine["size"]) if refine else float(max_size_mm)
    last: MeshError | None = None
    for attempt, factor in enumerate(_RETRY_FACTORS):
        scale = float(factor)
        size = max_size_mm * scale
        ref = dict(refine) if refine else None
        if ref is not None:
            ref["size"] = float(ref["size"]) * scale
        try:
            mesh = _mesh_once(step_path, size, min_size_mm, ref)
        except MeshError as exc:
            last = exc
            # Only a curving refusal is worth another attempt; anything else
            # (too coarse, no tets, a bad selector) is deterministic.
            if not getattr(exc, "curving", False):
                raise
            continue
        q = mesh["quality"]
        q["size_requested"] = target
        q["size_used"] = float(ref["size"]) if ref else size
        q["retries"] = attempt
        return mesh
    raise MeshError(
        f"degenerate_mesh_after_retries: {len(_RETRY_FACTORS)} sizes tried "
        f"around {target:g} mm (factors {list(_RETRY_FACTORS)}) and every one "
        f"produced a non-positive Jacobian. This is no longer a nudge away from "
        f"working. Last refusal: {last}") from last



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
        # The remedy depends on WHICH failure this is, and the two point in
        # opposite directions. Guessing sent a reader the wrong way on
        # 2026-09-30: one bad element in 1,329,310 at the 0.4 mm ladder rung
        # was reported as "too coarse for the smallest feature - reduce
        # max_size_mm", when refining from 0.8 mm is what produced it.
        #
        # A few marginal negatives are curved high-order elements that
        # `optimize("HighOrderFast")` could not straighten. Many, or badly
        # negative, is a mesh genuinely too coarse for a thin feature - the
        # earlier real cases were -445.1, -16.02 and -12.8 across dozens of
        # elements.
        frac = len(bad) / max(1, len(conn))
        worst = float(min_per_elem.min())
        curving = frac < 1e-3 and worst > -1.0
        if curving:
            cause = (f"only {len(bad)} of {len(conn)} elements ({frac * 100:.5f}%) "
                     f"and the worst is {worst:.4g}, marginally negative. That is "
                     f"the signature of a CURVED high-order element that midside "
                     f"projection inverted, not of a mesh too coarse for its "
                     f"features - gmsh's HighOrderFast pass has already been "
                     f"tried and could not straighten it. Refining further is as "
                     f"likely to create another one as to remove this one. "
                     f"Perturb the mesh size slightly (a few percent, in either "
                     f"direction), or relax the local curvature")
        else:
            cause = (f"{len(bad)} of {len(conn)} elements ({frac * 100:.3f}%), "
                     f"worst {worst:.4g}. The mesh size ({max_size_mm} mm) is too "
                     f"coarse for the smallest feature - reduce "
                     f"case.mesh.max_size_mm below the thinnest wall/radius, or "
                     f"thicken that feature")
        err = MeshError(
            f"degenerate_mesh: {cause}. CalculiX would abort on these, so the "
            f"mesh is refused rather than solved.")
        # `mesh_step` retries a nudged size for the curving class ONLY. Carried
        # on the exception rather than re-derived by the caller, so the
        # classification and the decision cannot drift apart.
        err.curving = bool(curving)
        err.degenerate_elements = int(len(bad))
        err.min_jacobian = worst
        raise err
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
