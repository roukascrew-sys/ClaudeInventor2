"""Submodelling: answer a local question without resolving the whole part.

WHY THIS EXISTS
A1 stalled trying to converge one peak. The frame is 1280 mm long; the peak
sits at a 19 mm junction. Refining the whole part until that junction is
resolved reached a 6.1 GB working set and crashed CalculiX with 0xC0000005,
and almost none of those elements were anywhere near the question being asked.

    Becker, Gantner, Innerberger & Praetorius (arXiv:2101.11407) put it
    plainly for goal-oriented adaptivity: to approximate a goal functional
    accurately it "is not necessary (and might even waste computational time)"
    to approximate the solution accurately over the whole domain.

Full goal-oriented adaptive FEM needs a dual problem, an a posteriori error
estimator, and a contractive iterative solver. CalculiX here runs a
single-threaded direct solve, so that machinery does not describe this engine.
Submodelling is the affordable subset of the same idea and needs none of it:

    1. solve the whole part on a mesh that fits in memory
    2. cut a small region out around the peak
    3. re-solve that region finely, with the global displacements imposed on
       the surfaces where the cut was made
    4. the interior of the region is now resolved; the rest was never asked

THE SOLVER DOES THE INTERPOLATION, NOT THIS MODULE
CalculiX 2.23 supports `*SUBMODEL` natively (verified against the shipped
binary). The submodel deck names the global run's `job.frd` and the driven
node set, and ccx interpolates displacements from the global mesh onto them.
Hand-rolling that interpolation would be inventing numerics this project has
no reason to own.

THE TWO WAYS TO GET THIS WRONG
Both produce a confident number, which is the failure mode this engine exists
to refuse.

  CUTTING TOO CLOSE   The imposed displacements come from the COARSE solve.
                      If the cut passes through the stress concentration, the
                      submodel is driven by exactly the values the coarse mesh
                      got wrong, and refining inside it converges neatly to
                      the wrong answer. Saint-Venant is the whole reason
                      submodelling works, and it needs distance to work in.
                      `standoff_elements` is therefore required, not defaulted.

  DRIVING FREE FACES  Only the surfaces created BY the cut carry imposed
                      displacements. A face that was part of the original
                      exterior must stay traction-free: clamping it to
                      interpolated displacements would stiffen the region and
                      quietly lower the peak.

WHAT THIS MODULE DOES NOT DO
It does not decide whether the peak is worth refining at all. A peak on a
geometric singularity has no finite value to converge to, so a submodel around
it is an expensive way to produce a rising number. `plan()` refuses that case
through `singularity.require_refinable()` before anything is cut or meshed.
"""

from __future__ import annotations

import math

from .singularity import require_refinable

#: How far the cut must stand off from the feature, in multiples of the
#: characteristic feature size. NOT a sourced standard - submodelling practice
#: says "far enough that Saint-Venant applies" and leaves the number to
#: judgement. Treat this as an ESTIMATE that must be justified per problem,
#: which is why it is not a default anywhere: `standoff_elements` has to be
#: stated by the caller the same way `endurance_limit_MPa` does in fatigue.py.
SUGGESTED_STANDOFF = 3.0

#: Tolerance for deciding a node lies on a cutting plane, in mm.
PLANE_TOL_MM = 1e-6


class SubmodelError(ValueError):
    """A submodel that would answer the wrong question."""


class SubmodelRegion:
    """The box cut out of the part, and the rule that made it defensible.

    Sized from the FEATURE, not from the part and not from the mesh. A region
    scaled to the mesh would shrink as the mesh refines, walking the cut
    boundary into the concentration exactly as the answer started to matter.
    """

    def __init__(self, centre, feature_mm: float, standoff_elements: float,
                 mesh_mm: float, source: str = ""):
        if len(tuple(centre)) != 3:
            raise SubmodelError("centre needs three coordinates")
        if not (feature_mm > 0):
            raise SubmodelError(
                f"feature_mm must be > 0, got {feature_mm}. This is the "
                f"characteristic size of the thing being resolved - a fillet "
                f"radius, a hole diameter, a junction thickness - and the "
                f"standoff is measured in multiples of it")
        if not (mesh_mm > 0):
            raise SubmodelError(f"mesh_mm must be > 0, got {mesh_mm}")
        if standoff_elements is None:
            raise SubmodelError(
                "standoff_elements is required and has no default. It decides "
                "whether the cut boundary sits in the coarse-solve's accurate "
                "region or inside the stress concentration, which decides "
                f"whether the answer means anything. {SUGGESTED_STANDOFF} is a "
                f"common rule of thumb, NOT a sourced standard - state it, and "
                f"say why in `source`")
        if standoff_elements < 1.0:
            raise SubmodelError(
                f"standoff_elements={standoff_elements} puts the cut boundary "
                f"within one feature size of the feature. The imposed "
                f"displacements come from the COARSE solve; cutting there "
                f"drives the submodel with the values the coarse mesh got "
                f"wrong, and refining inside converges to the wrong answer")

        self.centre = tuple(float(v) for v in centre)
        self.feature_mm = float(feature_mm)
        self.standoff_elements = float(standoff_elements)
        self.mesh_mm = float(mesh_mm)
        self.source = source
        #: Half-width of the cut box. Feature plus its standoff, on every side.
        self.half_mm = self.feature_mm * (1.0 + self.standoff_elements)

    @property
    def bounds(self) -> tuple:
        """(xmin, ymin, zmin, xmax, ymax, zmax) of the cutting box."""
        cx, cy, cz = self.centre
        h = self.half_mm
        return (cx - h, cy - h, cz - h, cx + h, cy + h, cz + h)

    def contains(self, point, tol: float = 0.0) -> bool:
        lo = self.bounds[:3]
        hi = self.bounds[3:]
        return all(lo[i] - tol <= float(point[i]) <= hi[i] + tol
                   for i in range(3))

    def on_cut_plane(self, point, tol: float = PLANE_TOL_MM) -> bool:
        """Does this point lie on one of the six planes of the cut box?

        Points outside the box are not on its boundary. A point that is
        outside on one axis but flush on another would otherwise read as a
        cut-plane node and get driven, which is the error that stiffens the
        model.
        """
        if not self.contains(point, tol=tol):
            return False
        lo = self.bounds[:3]
        hi = self.bounds[3:]
        return any(abs(float(point[i]) - lo[i]) <= tol
                   or abs(float(point[i]) - hi[i]) <= tol
                   for i in range(3))

    def elements_across_feature(self, mesh_mm: float | None = None) -> float:
        """How many elements span the feature at this mesh size.

        The number that says whether a refinement is worth running: a fillet
        resolved by one element is not resolved.
        """
        return self.feature_mm / float(mesh_mm or self.mesh_mm)

    def to_dict(self) -> dict:
        return {"centre_mm": [round(v, 4) for v in self.centre],
                "feature_mm": self.feature_mm,
                "standoff_elements": self.standoff_elements,
                "half_width_mm": round(self.half_mm, 4),
                "bounds_mm": [round(v, 4) for v in self.bounds],
                "source": self.source}


def driven_nodes(mesh: dict, region: SubmodelRegion,
                 tol: float = PLANE_TOL_MM) -> list:
    """Node tags to drive with global displacements: the cut surfaces only.

    A node qualifies when it lies on a plane of the cutting box. Nodes on the
    ORIGINAL exterior of the part do not qualify and must stay traction-free -
    imposing interpolated displacements on a free surface stiffens the region
    and lowers the peak, which is wrong in the unsafe direction.

    KNOWN LIMITATION, stated rather than hidden: a node is classified by
    geometry alone, so an original part face that happens to be coplanar with
    a cut plane will be driven. `coplanar_risk()` reports that case instead of
    letting it pass silently.
    """
    return sorted(
        int(tag) for tag, xyz in zip(mesh["node_tags"], mesh["coords"])
        if region.on_cut_plane(xyz, tol=tol))


def coplanar_risk(solid_bounds, region: SubmodelRegion,
                  tol: float = 1e-3) -> list:
    """Cut planes that coincide with the part's own bounding faces.

    Where they coincide, `driven_nodes` cannot tell a cut surface from an
    original free surface, and would drive both. Returns the offending planes
    so a caller can move the region or refuse.
    """
    lo_p, hi_p = solid_bounds[:3], solid_bounds[3:]
    lo_r, hi_r = region.bounds[:3], region.bounds[3:]
    hits = []
    for i, axis in enumerate("xyz"):
        for name, a, b in (("min", lo_r[i], lo_p[i]), ("max", hi_r[i], hi_p[i])):
            if abs(a - b) <= tol:
                hits.append({"axis": axis, "side": name, "at_mm": round(a, 4)})
    return hits


def plan(classification: dict | None, centre, feature_mm: float,
         standoff_elements: float, mesh_mm: float, *,
         source: str = "") -> SubmodelRegion:
    """Decide a submodel region, or refuse before anything is cut.

    The gate comes FIRST and is not optional. Refining around a peak that sits
    on a geometric singularity produces a number that rises with every hour
    spent on it, and the cost of discovering that after the fact is the whole
    solver budget.
    """
    require_refinable(classification, context="submodel refinement")
    return SubmodelRegion(centre, feature_mm, standoff_elements, mesh_mm,
                          source=source)


def cut_region(solid, region: SubmodelRegion, min_reduction: float = 0.05):
    """Intersect the part with the region box. Returns (cut_solid, report).

    The CAD kernel is imported INSIDE this function on purpose. Everything
    else in this module is stdlib, so region sizing, node classification and
    the convergence verdict keep working when OCP will not load — which is not
    hypothetical on this machine.

    Two refusals, both cases where the cut silently answers a different
    question than the caller thinks:

      EMPTY       The region does not intersect the part at all. Meshing that
                  produces nothing, and a solve of nothing reports success.
      NO SAVING   The region swallows (nearly) the whole part. The "submodel"
                  is then the original problem with imposed boundary
                  displacements bolted on, which is strictly worse than just
                  solving it - and it would still be reported as a submodel.
    """
    import cadquery as cq                          # noqa: PLC0415 - see above

    cx, cy, cz = region.centre
    side = 2.0 * region.half_mm
    box = cq.Workplane("XY").box(side, side, side).translate((cx, cy, cz))

    shape = solid.val() if hasattr(solid, "val") else solid
    whole = shape.Volume()
    cut = shape.intersect(box.val())
    kept = cut.Volume()

    if kept <= 0:
        raise SubmodelError(
            f"the region at {region.centre} does not intersect the part. A "
            f"mesh of nothing solves successfully and reports nothing - check "
            f"the peak coordinates came from this geometry")
    if kept >= whole * (1.0 - min_reduction):
        raise SubmodelError(
            f"the region keeps {kept / whole * 100:.1f}% of the part "
            f"({kept:.4g} of {whole:.4g} mm^3). A submodel that is the whole "
            f"part is the original solve with boundary displacements bolted "
            f"on - strictly worse, and it would still be reported as a "
            f"submodel. Shrink feature_mm or the standoff")

    return cut, {"whole_volume_mm3": round(whole, 4),
                 "submodel_volume_mm3": round(kept, 4),
                 "fraction_kept": round(kept / whole, 6),
                 "region": region.to_dict()}


def solid_bounds(solid) -> tuple:
    """(xmin, ymin, zmin, xmax, ymax, zmax) of a part, for `coplanar_risk`."""
    shape = solid.val() if hasattr(solid, "val") else solid
    bb = shape.BoundingBox()
    return (bb.xmin, bb.ymin, bb.zmin, bb.xmax, bb.ymax, bb.zmax)


def submodel_deck_fragment(global_frd_path, driven: list,
                           step: int = 1) -> dict:
    """The `*SUBMODEL` lines for a submodel deck, split by where they go.

    `TYPE=NODE` with an explicit node set: CalculiX reads the global results
    and interpolates displacement onto each listed node. All three
    translational DOFs are driven, because a cut surface transmits the full
    displacement vector - driving a subset would leave the region free to
    slide in the untouched direction.

    Returned as `{"before_step": [...], "inside_step": [...]}` because the two
    halves are not adjacent in the deck and must not be concatenated blindly:
    the set and the `*SUBMODEL` card are model data, and `*BOUNDARY, SUBMODEL`
    is a step card. The `*NSET` comes first because CalculiX resolves a set
    name at the point of use.
    """
    if not driven:
        raise SubmodelError(
            "no driven nodes: the submodel would be unrestrained, and a solve "
            "with rigid-body modes reports nothing about the structure. Check "
            "the region actually intersects the part")
    before = ["*NSET, NSET=NDRIVEN"]
    before += [", ".join(str(t) for t in driven[i:i + 8])
               for i in range(0, len(driven), 8)]
    before += [f"*SUBMODEL, TYPE=NODE, INPUT={global_frd_path}", "NDRIVEN"]
    return {"before_step": before,
            "inside_step": [f"*BOUNDARY, SUBMODEL, STEP={int(step)}",
                            "NDRIVEN, 1, 3"]}


def refinement_ladder(coarse_mm: float, region: SubmodelRegion,
                      steps: int = 3, factor: float = 2.0) -> list:
    """Successively finer mesh sizes for the submodel.

    Bounded on purpose. An unbounded ladder is how a convergence study becomes
    the thing that never finishes, and the point of submodelling is that the
    region is small enough for a few steps to be affordable.
    """
    if steps < 2:
        raise SubmodelError(
            f"steps={steps}: convergence needs at least two mesh sizes to "
            f"compare. One solve is a result, not a convergence study")
    if factor <= 1.0:
        raise SubmodelError(f"factor must be > 1, got {factor}")
    ladder = [float(coarse_mm) / (factor ** i) for i in range(int(steps))]
    finest = ladder[-1]
    if region.elements_across_feature(finest) < 2.0:
        raise SubmodelError(
            f"even the finest rung ({finest:.3g} mm) puts only "
            f"{region.elements_across_feature(finest):.1f} elements across a "
            f"{region.feature_mm:g} mm feature. A feature spanned by fewer "
            f"than two elements is not resolved at any rung, so the study "
            f"would measure the mesh rather than converge")
    return ladder


def converged(peaks: list, tol_pct: float) -> dict:
    """Has the peak stopped moving as the submodel mesh refines?

    Reports the change between the last two rungs, and does NOT declare
    convergence from a single pair - two rungs give one difference, and one
    difference cannot distinguish convergence from a slow monotonic climb up a
    singularity.
    """
    if len(peaks) < 3:
        return {"converged": None, "reason":
                f"{len(peaks)} rung(s): at least three are needed. Two give "
                f"one difference, and one difference cannot tell convergence "
                f"from a slow climb"}
    a, b = float(peaks[-2]), float(peaks[-1])
    if a == 0:
        return {"converged": None, "reason": "previous peak was zero"}
    change = abs(b - a) / abs(a) * 100.0
    prev = abs(a - float(peaks[-3])) / abs(float(peaks[-3])) * 100.0 \
        if float(peaks[-3]) else math.inf
    # STRICTLY shrinking, not merely non-growing. Under uniform refinement a
    # singular peak grows as h**-p, which gives successive changes a CONSTANT
    # ratio - equal percentage steps. `change <= prev` accepts that and calls
    # a climb converged as soon as the steps happen to fall under tolerance.
    # Equal steps are not evidence of a limit, so they are not accepted.
    return {"converged": bool(change <= tol_pct and change < prev),
            "change_pct": round(change, 4),
            "previous_change_pct": (round(prev, 4) if math.isfinite(prev)
                                    else None),
            "peaks": [float(p) for p in peaks],
            "reason": (f"last step moved {change:.3f}% "
                       f"(previous step {prev:.3f}%), tolerance {tol_pct}%")}
