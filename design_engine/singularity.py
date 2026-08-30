"""Find geometric stress singularities before a peak stress is trusted.

WHY THIS EXISTS
`P0047@v1` reported FEA SF 3.844 and it was wrong — not by a little, but
categorically. Its peak von Mises sat 1.28 mm off the edge where the doubler
pad's underside met the spine wall, a sharp 270-degree re-entrant corner
produced by unioning boxes with no blend. Linear elasticity has no finite
stress at such a corner: Williams' angular eigenfunction expansion gives
sigma ~ r**(lambda-1) with lambda ~ 0.5445, so the reported peak measured how
finely that corner happened to be meshed and nothing else.

    M. L. Williams, "Stress Singularities Resulting from Various Boundary
    Conditions in Angular Corners of Plates in Extension", Journal of Applied
    Mechanics 19 (1952) 526-528.

The existing stress-outlier gate read 1.633 on that run — comfortably inside
its "physically sound" band — because it compares the peak against the bulk
field and so detects a peak DECOUPLED from its surroundings, which is what a
constraint singularity looks like. A geometric singularity is fed by the
surrounding field rather than decoupled from it, so the ratio stays low while
the stress is still unbounded. The two checks answer different questions and
neither substitutes for the other.

WHY THIS WORKS ON THE CAD SOLID AND NOT ON THE MESH
The obvious implementation — dihedral angles between adjacent boundary
triangles — cannot work. A concave fillet is tessellated into flat facets, and
every facet junction on it is slightly re-entrant: a 10 mm fillet meshed at
3.2 mm produces interior angles near 198 degrees, which any threshold low
enough to catch a real corner would also flag. The mesh cannot distinguish a
sharp corner from a coarsely tessellated smooth one.

The CAD solid can. A fillet does not merely soften an edge, it REPLACES it with
a tangent-continuous blend, so the sharp edge is gone from the topology
entirely. Testing the solid asks the question the mesh cannot answer.

WHAT THIS DOES NOT DO
It reports geometry, never a safety factor, and it never silently rescales a
stress. A singular peak is not "pessimistic but safe" the way a constraint
artifact is — it is meaningless, and the honest response is to say so and let
the caller decide, not to substitute a number of our own invention.
"""

from __future__ import annotations

import math

import cadquery as cq

# Two faces meeting within this of 180 degrees are treated as tangent-
# continuous: a blend, not an edge. Fillets built by OCC come out at well
# under a degree, so this is generous. Raising it hides real corners.
TANGENT_TOL_DEG = 5.0

# How far from a singular edge a peak still counts as "on" it, in element
# widths. The singular field is a local phenomenon a few elements across; at
# 3.2 mm this is a 6.4 mm radius, and the P0047 peak sat 1.28 mm out.
DEFAULT_RADIUS_ELEMENTS = 2.0

# Samples along each edge. Whether two faces meet convexly or concavely can
# change along a curved edge, so one midpoint sample is not always enough.
_SAMPLES = 3
_OFFSET = 1e-3          # how far to step off the edge to probe a face


class Singularity(dict):
    """A sharp re-entrant edge, with enough detail to find it in CAD."""

    def __repr__(self) -> str:      # pragma: no cover - debugging aid
        return (f"<Singularity {self['interior_angle_deg']:.1f}deg at "
                f"{[round(c, 2) for c in self['point']]}>")


def _adjacent_faces(solid) -> dict:
    """Map each edge to the faces that carry it.

    Keyed by the underlying OCC shape's hash rather than by identity: CadQuery
    hands back fresh Python wrappers around the same topological entity, so
    `is` and `==` both fail to group them.

    `TopoDS_Shape.__hash__`, not `HashCode(...)` — the latter is absent from
    this OCP build. Verified on P0047@v1: 66 groups for 66 edges, 64 of them
    manifold pairs and 2 seam singletons on the lug bores. Hashing `TShape()`
    instead collapses the whole solid into two groups and is wrong.
    """
    by_edge: dict = {}
    for face in solid.Faces():
        for edge in face.Edges():
            by_edge.setdefault(hash(edge.wrapped), []).append((edge, face))
    return by_edge


def _interior_angle_deg(edge, face_a, edge_in_b, face_b, t: float) -> float | None:
    """Material-side angle where two faces meet, at parameter `t` along edge.

    Returns 90 for the outside edge of a box, 270 for the inside corner of an
    L, 180 for a tangent blend. None when the geometry cannot be probed there.

    The convexity test is the sign of `into_b . n_a`, where `into_b` points
    from the edge into the body of face B: if face B lies on the OUTWARD side
    of face A, the pair folds back on itself and the corner is re-entrant.

    `into_b` is `n_b x t`, where `t` is the edge's tangent taken in the
    orientation the edge carries WITHIN face B. That is a topological fact
    about the boundary, so it needs no boundary lookup and holds for any face
    shape. Two simpler approaches were tried first and both fail:

      - stepping a small distance off the edge and asking whether the point is
        still on face B. Deciding that needs the face's BOUNDARY, and
        projecting onto the underlying unbounded surface says yes to points
        well outside it. Both directions "succeed", the sign becomes whichever
        was tried first, and 6 of a plain box's 12 convex edges came back as
        270-degree re-entrant corners.
      - pointing at face B's centroid. Exact for a convex face, wrong for one
        with a hole in it: the jetpack pad's underside has the spine passing
        through it, so its centroid lies in the hole and the direction points
        away from the face. That silently lost the very junction edge this
        module was written to catch.
    """
    try:
        m = edge.positionAt(t)
        n_a = face_a.normalAt(m)
        n_b = face_b.normalAt(m)
        tangent = edge.tangentAt(t).normalized()
    except Exception:                       # noqa: BLE001 - probe failed, say so
        return None

    dot = max(-1.0, min(1.0, n_a.dot(n_b)))
    between = math.degrees(math.acos(dot))

    if _reversed_in_face(edge_in_b):
        tangent = tangent.multiply(-1.0)
    try:
        into_b = n_b.cross(tangent).normalized()
    except Exception:                       # noqa: BLE001
        return None

    return (180.0 + between) if into_b.dot(n_a) > 0 else (180.0 - between)


def _reversed_in_face(edge) -> bool:
    """Does this edge run against its own direction inside its face?"""
    from OCP.TopAbs import TopAbs_REVERSED
    return edge.wrapped.Orientation() == TopAbs_REVERSED


def sharp_concave_edges(solid, tangent_tol_deg: float = TANGENT_TOL_DEG
                        ) -> list[Singularity]:
    """Every edge where material folds back on itself at a non-tangent angle.

    These are the places linear elasticity returns an unbounded stress. A
    convex edge is not one of them — stress is finite at the outside corner of
    a box — and neither is a tangent-continuous blend, which is precisely what
    filleting a corner produces.
    """
    out: list[Singularity] = []
    for pairs in _adjacent_faces(solid).values():
        if len(pairs) != 2:
            # A seam edge (one face) or a non-manifold junction (three or
            # more). Neither is the box-union corner this looks for, and
            # guessing at their dihedral would invent a result.
            continue
        edge, face_a = pairs[0]
        edge_in_b, face_b = pairs[1]
        worst = None
        for i in range(_SAMPLES):
            t = (i + 0.5) / _SAMPLES
            ang = _interior_angle_deg(edge, face_a, edge_in_b, face_b, t)
            if ang is None:
                continue
            if worst is None or ang > worst[0]:
                worst = (ang, t)
        if worst is None:
            continue
        angle, t = worst
        if angle > 180.0 + tangent_tol_deg:
            p = edge.positionAt(t)
            a, b = edge.startPoint(), edge.endPoint()
            out.append(Singularity(
                interior_angle_deg=round(angle, 2),
                point=[round(p.x, 4), round(p.y, 4), round(p.z, 4)],
                p0=[a.x, a.y, a.z],
                p1=[b.x, b.y, b.z],
                length_mm=round(edge.Length(), 4),
                # Williams: sigma ~ r**(lambda-1). Reported so the severity of
                # a corner is visible, not just its existence.
                singularity_exponent=round(_williams_exponent(angle), 4)))
    out.sort(key=lambda s: -s["interior_angle_deg"])
    return out


def _williams_exponent(interior_angle_deg: float) -> float:
    """Strength of the stress singularity at a re-entrant corner.

    Solves the symmetric Williams characteristic equation
    `sin(lambda*a) + lambda*sin(a) = 0` for the opening angle `a`, and returns
    `1 - lambda`, the exponent p in sigma ~ r**-p. Zero at 180 degrees (no
    singularity) and about 0.4555 at 270 degrees, the sharp re-entrant corner
    that started this.

    Bisection rather than a closed form: the equation is transcendental, and a
    fitted approximation would invent precision the reference does not give.
    """
    a = math.radians(interior_angle_deg)
    if a <= math.pi:
        return 0.0

    def f(lam):
        return math.sin(lam * a) + lam * math.sin(a)

    lo, hi = 1e-9, 1.0
    if f(lo) * f(hi) > 0:
        return 0.0
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if f(lo) * f(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return max(0.0, 1.0 - (lo + hi) / 2.0)


def classify_peak(solid, peak_xyz, mesh_size_mm: float,
                  radius_elements: float = DEFAULT_RADIUS_ELEMENTS,
                  edges: list | None = None) -> dict:
    """Does this peak stress sit on a geometric singularity?

    `edges` may be passed in when already computed, so a caller running this
    per-solve does not re-analyse unchanged topology.

    The verdict deliberately has three values, not two. `unknown` is returned
    when the geometry could not be analysed at all, because reporting "clean"
    on an analysis that did not happen is the failure this module exists to
    prevent.
    """
    if edges is None:
        try:
            edges = sharp_concave_edges(solid)
        except Exception as exc:            # noqa: BLE001 - never break a solve
            return {"verdict": "unknown", "singular_edges": 0,
                    "reason": f"geometry could not be analysed: "
                              f"{type(exc).__name__}: {exc}"}

    radius = radius_elements * float(mesh_size_mm)
    near = sorted(
        ({**s, "distance_mm": round(_edge_distance(peak_xyz, s), 4)}
         for s in edges),
        key=lambda s: s["distance_mm"])
    hits = [s for s in near if s["distance_mm"] <= radius]

    if not edges:
        return {"verdict": "clean", "singular_edges": 0,
                "reason": "no sharp re-entrant edges in the solid; the peak "
                          "is a finite stress and may be converged"}
    if not hits:
        return {"verdict": "clean", "singular_edges": len(edges),
                "nearest_mm": near[0]["distance_mm"],
                "reason": (f"{len(edges)} sharp re-entrant edge(s) present but "
                           f"the peak is {near[0]['distance_mm']:.2f} mm from "
                           f"the nearest, beyond the {radius:.2f} mm "
                           f"({radius_elements:g} element) radius")}

    worst = max(hits, key=lambda s: s["singularity_exponent"])
    return {
        "verdict": "singular",
        "singular_edges": len(edges),
        "matched": hits[:5],
        "nearest_mm": hits[0]["distance_mm"],
        "interior_angle_deg": worst["interior_angle_deg"],
        "singularity_exponent": worst["singularity_exponent"],
        "reason": (
            f"peak is {hits[0]['distance_mm']:.2f} mm from a "
            f"{worst['interior_angle_deg']:.0f}-degree re-entrant edge "
            f"(within {radius:.2f} mm = {radius_elements:g} elements). Linear "
            f"elasticity has no finite stress there: sigma ~ r**"
            f"-{worst['singularity_exponent']:.3f}, so this peak measures the "
            f"mesh, not the structure, and refining will only raise it. Fillet "
            f"the edge or evaluate the stress away from it - do not treat the "
            f"safety factor derived from this peak as converged."),
    }


#: Elements wanted across a blend before its peak stress means anything.
#: NOT a sourced standard - notch-stress practice commonly asks for far more
#: (the IIW notch method meshes a 1 mm reference radius with elements of about
#: r/6). Three is the point below which the blend is not represented AT ALL
#: rather than the point at which it is well resolved, so it is a floor, not
#: a target. Flagged as an estimate wherever it is reported.
MIN_BLEND_ELEMENTS = 3.0


def blend_resolution(radius_mm: float, mesh_size_mm: float) -> dict:
    """Is the mesh fine enough for a blend of this radius to mean anything?

    WHY THIS EXISTS, and it is the awkward part of fixing a singularity.
    Blending a sharp re-entrant corner makes the peak stress FINITE, so
    `classify_peak` stops objecting - correctly, because the geometry really
    is clean. But a 1 mm blend meshed at 3 mm has a third of an element across
    it: the peak is now bounded and still meaningless, and the check that used
    to catch it no longer fires.

    Fixing the geometry therefore moves the problem rather than removing it,
    from "unbounded" to "under-resolved". Those need different remedies -
    a fillet versus a finer mesh - and only the second one is honest here.
    """
    if radius_mm <= 0 or mesh_size_mm <= 0:
        return {"resolved": None, "reason": "radius and mesh size must be > 0"}
    n = float(radius_mm) / float(mesh_size_mm)
    return {
        "elements_across_blend": round(n, 3),
        "radius_mm": float(radius_mm),
        "mesh_size_mm": float(mesh_size_mm),
        "minimum": MIN_BLEND_ELEMENTS,
        "resolved": bool(n >= MIN_BLEND_ELEMENTS),
        "reason": (
            f"{n:.2f} element(s) across a {radius_mm:g} mm blend at "
            f"{mesh_size_mm:g} mm mesh"
            + ("" if n >= MIN_BLEND_ELEMENTS else
               f" - below {MIN_BLEND_ELEMENTS:g}, so the blend is barely "
               f"represented and its peak stress measures the mesh. The "
               f"geometry is clean; the discretisation is not. Needs about "
               f"{radius_mm / MIN_BLEND_ELEMENTS:.2f} mm elements locally")),
    }


class RefinementRefused(RuntimeError):
    """A refinement campaign that could not converge, refused before it ran."""


#: The only verdict that permits refinement. Written as a set of the ALLOWED
#: values rather than a test against the refused ones, so a verdict added to
#: `classify_peak` later defaults to REFUSED until someone decides otherwise.
#: The opposite spelling — `if verdict == "singular": refuse` — silently
#: permits `unknown`, which is the exact failure this module exists to prevent.
_REFINABLE_VERDICTS = frozenset({"clean"})


def refinement_permitted(classification: dict | None) -> dict:
    """May a refinement campaign be started against this peak?

    WHY THIS IS A GATE AND NOT ADVICE
    At a sharp re-entrant corner the elastic stress is unbounded, so peak von
    Mises has no limit to converge to. A refinement loop aimed at it does not
    fail: every step genuinely reduces discretisation error and genuinely
    raises the peak, and from inside the loop that is indistinguishable from
    slow convergence. It reports steady progress forever.

    That is expensive here specifically. A 2.8 mm solve on this frame already
    reached a 6.1 GB working set and crashed CalculiX with 0xC0000005, so a
    campaign spent on a peak that cannot converge does not merely waste time —
    it consumes the whole available budget and returns a number that rose with
    every hour of it.

    THREE VERDICTS, TWO OF WHICH REFUSE
    `unknown` refuses along with `singular`. An analysis that did not happen
    tells us nothing about whether the goal is bounded, and spending hours on
    that assumption is precisely the bet this engine does not take. Refusing
    costs a geometry re-check; permitting costs the budget.

    Returns the decision rather than raising, so a caller that wants to report
    rather than abort can. Use `require_refinable()` for the hard gate.
    """
    verdict = (classification or {}).get("verdict")
    if verdict in _REFINABLE_VERDICTS:
        return {"permitted": True, "verdict": verdict,
                "reason": (classification.get("reason")
                           or "peak is clear of every sharp re-entrant edge")}

    if verdict == "singular":
        return {"permitted": False, "verdict": verdict,
                "reason": classification.get("reason", "peak sits on a "
                                             "geometric singularity"),
                "remedy": ("blend the edge, or measure the stress away from "
                           "it. Refining will only raise this peak")}

    if verdict == "unknown":
        return {"permitted": False, "verdict": "unknown",
                "reason": (classification.get("reason")
                           or "the geometry could not be analysed"),
                "remedy": ("re-run the geometry check. UNKNOWN is not a pass: "
                           "an analysis that did not happen is not evidence "
                           "that the peak can converge")}

    return {"permitted": False, "verdict": verdict,
            "reason": (f"no usable singularity classification "
                       f"(verdict={verdict!r}); refinement cannot be shown to "
                       f"converge, so it is refused"),
            "remedy": "call classify_peak() on the solid first"}


def require_refinable(classification: dict | None, *,
                      context: str = "mesh refinement") -> dict:
    """The hard gate. Raise unless this peak can converge.

    Implemented as code rather than left as a step to remember, for the same
    reason the production sign-off token is: an instruction that lives only in
    prose is unfalsifiable, and this one has already been skipped once — SF
    3.844 was reported, passed every check in the engine, and was measured
    1.28 mm from a 270-degree corner.

    Returns the classification unchanged when permitted, so it can be used
    inline as a precondition.
    """
    decision = refinement_permitted(classification)
    if not decision["permitted"]:
        raise RefinementRefused(
            f"{context} refused — {decision['reason']} "
            f"[{decision.get('remedy', '')}]".rstrip(" []"))
    return classification


def _edge_distance(pt, sing) -> float:
    """Distance from a point to a singular edge.

    Point-to-SEGMENT, not point-to-sample-point: a 1280 mm crossbeam edge
    sampled at its midpoint would read as 600 mm away from a peak sitting
    directly on one end of it.
    """
    a = sing.get("p0")
    b = sing.get("p1")
    if a is None or b is None:
        return math.dist(pt, sing["point"])
    ax, ay, az = a
    bx, by, bz = b
    dx, dy, dz = bx - ax, by - ay, bz - az
    L2 = dx * dx + dy * dy + dz * dz
    if L2 == 0:
        return math.dist(pt, a)
    t = ((pt[0] - ax) * dx + (pt[1] - ay) * dy + (pt[2] - az) * dz) / L2
    t = max(0.0, min(1.0, t))
    return math.dist(pt, (ax + t * dx, ay + t * dy, az + t * dz))

