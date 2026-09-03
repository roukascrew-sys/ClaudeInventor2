"""Half-model symmetry: cut the part at a plane and solve what remains.

A symmetric structure solved as a half model costs less than half as much. Node
count halves, and a direct sparse factorisation's fill-in grows faster than
linearly with bandwidth, so memory falls by MORE than half - which is the
constraint that has been ending runs on this machine.

The whole method rests on one assumption, and it is silent when wrong. Cut an
asymmetric model at a plane, impose zero normal displacement on the cut face,
and CalculiX returns a perfectly converged answer to a question nobody asked:
it has been told the other half is a mirror image, and it believes it. There is
no residual to inspect, no warning, and the stresses look entirely plausible.

So nothing here trusts the caller. `verify()` demands evidence on all three
counts before a cut is permitted - geometry, loads and restraints - and raises
otherwise. That is the same stance as `SourcedRange` refusing an unsupported
nominal and `refinement_permitted` refusing a singular peak: the cheap answer
is only allowed once the assumption behind it is shown to hold.

WHAT A HALF MODEL DOES AND DOES NOT CHANGE

  stress, strain      unchanged. They are local, and the kept half sees the
                      same field it saw in the whole model.
  displacement        unchanged, for the same reason.
  mass, volume        HALVED. Any mass property read off a half model must be
                      doubled, and `cut_half` reports the factor so a caller
                      cannot silently forget.
  applied load        halved, because half the load is on the discarded side.
                      Equilibrium still balances: the symmetry plane carries
                      the reaction that the missing half used to supply.
  buckling, modal     NOT SUPPORTED here. A half model with symmetry
                      constraints can only represent symmetric mode shapes, and
                      the first buckling or vibration mode of a symmetric
                      structure is very often ANTI-symmetric - so a half model
                      silently misses the mode that matters. Refused rather
                      than approximated; see `assert_static_only`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

AXES = {"x": 0, "y": 1, "z": 2}

#: DOF numbers CalculiX uses, 1-based, matching the deck writer's convention.
_DOF = {"x": 1, "y": 2, "z": 3}


class SymmetryError(ValueError):
    """A symmetry cut that could not be shown to be valid, refused before use."""


@dataclass(frozen=True)
class Plane:
    """The mirror plane, and which side survives the cut."""

    axis: str
    at: float
    keep: str = "+"

    @property
    def index(self) -> int:
        return AXES[self.axis]

    @property
    def dof(self) -> int:
        """The DOF held at zero on the cut face: motion NORMAL to the plane."""
        return _DOF[self.axis]

    def side_of(self, coord: float) -> int:
        """+1, -1, or 0 for a point on the plane."""
        d = float(coord) - self.at
        return 0 if d == 0.0 else (1 if d > 0 else -1)

    def keeps(self, coord: float) -> bool:
        s = self.side_of(coord)
        return s == 0 or (s > 0 if self.keep == "+" else s < 0)

    def mirror(self, coord: float) -> float:
        return 2.0 * self.at - float(coord)

    def to_dict(self) -> dict:
        return {"axis": self.axis, "at": self.at, "keep": self.keep,
                "held_dof": self.dof}


def parse(spec: dict | None) -> Plane | None:
    """Read `case["symmetry"]`. None means no symmetry was asked for."""
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise SymmetryError("case.symmetry must be a mapping")
    axis = str(spec.get("axis", "")).lower()
    if axis not in AXES:
        raise SymmetryError(
            f"symmetry axis {spec.get('axis')!r} is not one of {sorted(AXES)}")
    keep = str(spec.get("keep", "+"))
    if keep not in ("+", "-"):
        raise SymmetryError(f"symmetry keep must be '+' or '-', got {keep!r}")
    try:
        at = float(spec.get("at", 0.0))
    except (TypeError, ValueError):
        raise SymmetryError(
            f"symmetry at must be a number, got {spec.get('at')!r}") from None
    return Plane(axis=axis, at=at, keep=keep)


def assert_static_only(analysis: str, plane: Plane | None) -> None:
    """Refuse symmetry on an eigenvalue analysis.

    A half model constrained on its symmetry plane can only produce SYMMETRIC
    mode shapes, and the critical buckling mode of a symmetric column or frame
    is frequently anti-symmetric - sidesway is the textbook case. The half
    model would return a higher, non-critical factor and look entirely healthy
    doing it, which is worse than not running at all.
    """
    if plane is not None and analysis in ("buckle", "frequency"):
        raise SymmetryError(
            f"symmetry is not supported for a {analysis} analysis. A half "
            f"model with symmetry constraints can only represent symmetric "
            f"modes, and the critical mode of a symmetric structure is often "
            f"ANTI-symmetric, so the run would silently miss it and report a "
            f"higher factor. Solve the whole part for eigenvalue problems")


# ------------------------------------------------------------------ mirroring
def mirror_selector(where: Any, plane: Plane) -> Any:
    """The selector that picks the mirror image of what `where` picks.

    Returns a selector, or raises when the mirror image cannot be determined -
    which is itself the answer: an unmirrorable selector cannot be shown
    symmetric, so it cannot be waved through.
    """
    if not isinstance(where, dict):
        raise SymmetryError(f"cannot mirror selector {where!r}")

    out: dict = {}
    for key, val in where.items():
        if key in ("all", "any"):
            if not isinstance(val, (list, tuple)):
                raise SymmetryError(f"selector {key!r} must hold a list")
            out[key] = [mirror_selector(v, plane) for v in val]
        elif key == "cylinder":
            out[key] = _mirror_cylinder(val, plane)
        elif key == "axis":
            out[key] = val
        elif key == "at":
            out[key] = val               # fixed up below, once axis is known
        else:
            out[key] = val

    if out.get("axis") == plane.axis and "at" in out:
        at = out["at"]
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            out["at"] = plane.mirror(at)
        elif at in ("min", "max"):
            # "the min face" mirrors to "the max face" and vice versa.
            out["at"] = "max" if at == "min" else "min"
        else:
            raise SymmetryError(
                f"cannot mirror a selector at {at!r} on the symmetry axis")
    return out


def _mirror_cylinder(cyl: Any, plane: Plane) -> dict:
    if not isinstance(cyl, dict) or "axis" not in cyl:
        raise SymmetryError(f"cannot mirror cylinder selector {cyl!r}")
    axis = str(cyl["axis"]).lower()
    if axis == plane.axis:
        # A cylinder whose AXIS is the symmetry axis runs through the plane and
        # is its own mirror image: its centre is given in the other two
        # coordinates, which the reflection does not touch.
        return dict(cyl)
    raise SymmetryError(
        f"a cylinder about {axis!r} cannot be mirrored about {plane.axis!r} "
        f"without knowing its extent along {plane.axis!r}; state the "
        f"symmetry explicitly or solve the whole part")


def mirror_force(force: Sequence[float], plane: Plane) -> list[float]:
    """Mirror a force vector: the NORMAL component flips, the rest do not.

    This is what makes the check a real one. Two equal loads at mirrored
    positions are only a symmetric pair if their normal components oppose -
    equal normal components at mirrored positions are an ANTI-symmetric pair,
    which a half model cannot represent at all.
    """
    out = [float(f) for f in force]
    out[plane.index] = -out[plane.index]
    return out


# ------------------------------------------------------------------- evidence
def _close(a: Any, b: Any, tol: float = 1e-9) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b)) <= tol * max(1.0, abs(float(a)),
                                                     abs(float(b)))
    if isinstance(a, dict) and isinstance(b, dict):
        return (set(a) == set(b)
                and all(_close(a[k], b[k], tol) for k in a))
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b))
    return a == b


def check_loads(loads: Sequence[dict], plane: Plane) -> dict:
    """Every load must have a mirror partner, or be its own mirror image.

    A load whose selector is its OWN mirror image is SHARED between the halves
    - either because it sits on the plane, or, far more commonly, because it
    selects a face that straddles it. `{"axis": "z", "at": "max"}` picks the
    whole top face of a beam, and half of that face is in each half.

    `force_total_N` is a TOTAL over the selected nodes, so a shared load must
    be halved for the kept model; `halve_shared_loads` does it. Getting this
    wrong is not subtle and not safe: a 60x40x400 cantilever solved half-model
    without the halving returned exactly 2.000x the displacement and 2.000x the
    peak stress of the whole model.

    A shared load is allowed only if it has no component NORMAL to the plane.
    A normal load there would have to be resisted by a displacement the
    symmetry condition forbids, which makes it an anti-symmetric problem that
    a half model cannot represent.
    """
    unmatched, shared = [], []
    remaining = list(range(len(loads)))

    for i, load in enumerate(loads):
        if i not in remaining:
            continue
        try:
            want_where = mirror_selector(load["where"], plane)
        except SymmetryError as exc:
            unmatched.append({"index": i, "why": str(exc)})
            remaining.remove(i)
            continue

        if _close(want_where, load["where"]):
            # Self-mirroring: the load sits on the plane.
            normal = float(load["force_total_N"][plane.index])
            if abs(normal) > 1e-9:
                unmatched.append({
                    "index": i,
                    "why": (f"load lies on the symmetry plane but has a "
                            f"{plane.axis}-component of {normal:g} N, which "
                            f"the plane's zero-normal-displacement condition "
                            f"forbids")})
            else:
                shared.append(i)
            remaining.remove(i)
            continue

        want_force = mirror_force(load["force_total_N"], plane)
        partner = next(
            (j for j in remaining
             if j != i
             and _close(loads[j]["where"], want_where)
             and _close(loads[j]["force_total_N"], want_force)), None)
        if partner is None:
            unmatched.append({
                "index": i,
                "why": (f"no load mirrors it: expected one selecting "
                        f"{want_where} with force {want_force}")})
            remaining.remove(i)
        else:
            remaining.remove(i)
            remaining.remove(partner)

    return {"symmetric": not unmatched, "unmatched": unmatched,
            "shared": shared, "n_loads": len(loads)}


def check_constraints(constraints: Sequence[dict], plane: Plane) -> dict:
    """Every restraint must have a mirror partner, or be its own mirror."""
    unmatched = []
    remaining = list(range(len(constraints)))

    for i, con in enumerate(constraints):
        if i not in remaining:
            continue
        try:
            want = mirror_selector(con["where"], plane)
        except SymmetryError as exc:
            unmatched.append({"index": i, "why": str(exc)})
            remaining.remove(i)
            continue
        if _close(want, con["where"]):
            remaining.remove(i)
            continue
        partner = next(
            (j for j in remaining
             if j != i
             and _close(constraints[j]["where"], want)
             and sorted(constraints[j]["dof"]) == sorted(con["dof"])), None)
        if partner is None:
            unmatched.append({
                "index": i,
                "why": f"no restraint mirrors it: expected one at {want} "
                       f"holding dof {sorted(con['dof'])}"})
            remaining.remove(i)
        else:
            remaining.remove(i)
            remaining.remove(partner)

    return {"symmetric": not unmatched, "unmatched": unmatched,
            "n_constraints": len(constraints)}


def volume_balance(solid, plane: Plane, rel_tol: float = 1e-4) -> dict:
    """Do the two halves have the same volume?

    Necessary, not sufficient - two different shapes can share a volume - but
    it is a real measurement on the actual solid rather than a claim about it,
    and it catches the mistakes that occur in practice: an off-centre feature,
    a hole on one side, a plane in the wrong place.

    The CAD kernel is imported inside the function, as everywhere else in this
    project, so the checks above keep working when OCP will not load.
    """
    import cadquery as cq                            # noqa: PLC0415

    shape = solid.val() if hasattr(solid, "val") else solid
    bb = shape.BoundingBox()
    lo = (bb.xmin, bb.ymin, bb.zmin)[plane.index]
    hi = (bb.xmax, bb.ymax, bb.zmax)[plane.index]
    if not (lo < plane.at < hi):
        raise SymmetryError(
            f"the symmetry plane {plane.axis}={plane.at:g} does not pass "
            f"through the part, whose {plane.axis} extent is "
            f"[{lo:.4g}, {hi:.4g}]. Cutting there would keep everything or "
            f"nothing")

    whole = shape.Volume()
    pos = _half(shape, plane, "+", bb).Volume()
    neg = _half(shape, plane, "-", bb).Volume()
    diff = abs(pos - neg)
    ok = diff <= rel_tol * whole
    return {"balanced": bool(ok), "volume_mm3": whole,
            "positive_mm3": pos, "negative_mm3": neg,
            "difference_mm3": diff,
            "difference_pct": (diff / whole * 100.0) if whole else float("nan"),
            "rel_tol": rel_tol}


def _half(shape, plane: Plane, side: str, bb=None):
    """Intersect `shape` with the half-space on `side` of the plane."""
    import cadquery as cq                            # noqa: PLC0415

    bb = bb or shape.BoundingBox()
    span = max(bb.xlen, bb.ylen, bb.zlen) * 4.0 + 10.0
    centre = [(bb.xmin + bb.xmax) / 2.0,
              (bb.ymin + bb.ymax) / 2.0,
              (bb.zmin + bb.zmax) / 2.0]
    # A box of `span` on a side, pushed so one of its faces lies ON the plane.
    centre[plane.index] = plane.at + (span / 2.0 if side == "+" else -span / 2.0)
    box = cq.Workplane("XY").box(span, span, span).translate(tuple(centre))
    return shape.intersect(box.val())


def verify(solid, case: dict, plane: Plane) -> dict:
    """Demand evidence on all three counts, or refuse.

    Returns the evidence when it passes, so the log records WHY the cut was
    allowed rather than merely that it was.
    """
    loads = check_loads(case.get("loads") or [], plane)
    cons = check_constraints(case.get("constraints") or [], plane)
    vol = volume_balance(solid, plane)

    problems = []
    if not vol["balanced"]:
        problems.append(
            f"the halves differ in volume by {vol['difference_mm3']:.4g} mm3 "
            f"({vol['difference_pct']:.4g}%), so the geometry is not symmetric "
            f"about {plane.axis}={plane.at:g}")
    if not loads["symmetric"]:
        problems.append("loads are not symmetric: "
                        + "; ".join(u["why"] for u in loads["unmatched"]))
    if not cons["symmetric"]:
        problems.append("restraints are not symmetric: "
                        + "; ".join(u["why"] for u in cons["unmatched"]))

    if problems:
        raise SymmetryError(
            "symmetry_not_demonstrated: " + " | ".join(problems)
            + ". A half model imposes the mirror image on the discarded side "
              "and returns a converged, plausible, WRONG answer when that is "
              "untrue, with no residual to inspect - so it is refused rather "
              "than assumed. Solve the whole part, or correct the plane")

    return {"plane": plane.to_dict(), "geometry": vol, "loads": loads,
            "constraints": cons}


# ---------------------------------------------------------------------- apply
def cut_half(solid, plane: Plane) -> tuple[Any, dict]:
    """Keep one side of the plane. Returns (solid, report)."""
    shape = solid.val() if hasattr(solid, "val") else solid
    whole = shape.Volume()
    kept = _half(shape, plane, plane.keep)
    v = kept.Volume()
    if v <= 0.0:
        raise SymmetryError(
            f"cutting at {plane.axis}={plane.at:g} keeping '{plane.keep}' left "
            f"nothing")
    return kept, {
        "kept_mm3": v, "whole_mm3": whole,
        "fraction_kept": v / whole if whole else float("nan"),
        # Anything extensive read off this model - mass, volume, applied load,
        # reaction - is HALF of the real value. Stated as a number so a caller
        # multiplies rather than remembers.
        "extensive_factor": 2.0,
    }


def halve_shared_loads(loads: Sequence[dict], plane: Plane,
                       shared: Sequence[int]) -> list[dict]:
    """Halve any load SHARED across the plane; leave the rest alone.

    `force_total_N` is a total over the selected nodes. A selector that is its
    own mirror image picks nodes on both sides of the plane - most often a face
    that straddles it, like the whole top face of a beam - so the kept half
    must carry half of that total.

    Measured 2026-09-03, and it is not a rounding matter: a 60x40x400
    cantilever loaded on `{"axis": "z", "at": "max"}` and solved as a half
    model WITHOUT this returned 3.86490 mm tip against the whole model's
    1.93236 mm, and 122.2193 MPa against 61.2089 MPa. Exactly 2.000x on both.

    Loads away from the plane need no adjustment: the mirrored partner simply
    is not in the kept half, and its absence is the correct representation of
    a load the kept half never carried.
    """
    out = []
    on = set(shared)
    for i, load in enumerate(loads):
        if i in on:
            load = dict(load)
            load["force_total_N"] = [f / 2.0 for f in load["force_total_N"]]
            load["_halved_by_symmetry"] = True
        out.append(load)
    return out


def plane_nodes(mesh: dict, plane: Plane, tol: float = 1e-6) -> list[int]:
    """Node tags lying on the cut face, which is where the restraint goes."""
    idx = plane.index
    return [int(t) for t, xyz in zip(mesh["node_tags"], mesh["coords"])
            if abs(float(xyz[idx]) - plane.at) <= tol]


def drop_discarded(items: Sequence[dict], mesh: dict, plane: Plane,
                   select, kind: str = "load") -> tuple[list[dict], list[int]]:
    """Remove items that live entirely in the half being thrown away.

    A load at x = -330 on a model cut to keep x >= 0 selects nothing, and
    that is correct rather than an error: its half is gone, and so is it.
    Node selection refuses an empty selector - rightly, since an empty
    selector is usually a typo - so the discarded ones have to be removed
    before they reach it.

    The safeguard is that an item is only dropped when its MIRROR matches
    nodes. `verify()` has already established that every item is mirror-paired,
    so an item selecting nothing whose mirror selects something is the
    discarded partner of a kept one. An item selecting nothing whose mirror
    ALSO selects nothing is a broken selector, and is raised rather than
    quietly deleted - which would otherwise turn a typo into a silently
    unloaded model.
    """
    kept, dropped = [], []
    for i, item in enumerate(items):
        try:
            if len(select(mesh, item["where"])) > 0:
                kept.append(item)
                continue
        except Exception:
            pass                                  # empty selection, handled below

        try:
            mirrored = mirror_selector(item["where"], plane)
            n_mirror = len(select(mesh, mirrored))
        except Exception:
            n_mirror = 0

        if n_mirror > 0:
            dropped.append(i)                     # the discarded half's partner
        else:
            raise SymmetryError(
                f"{kind} {i} selects no node in the kept half AND no node in "
                f"its mirror image, so it is not a symmetry consequence - it "
                f"is a selector that matches nothing anywhere: "
                f"{item['where']}. Dropping it would leave the model quietly "
                f"missing a {kind}")
    return kept, dropped
