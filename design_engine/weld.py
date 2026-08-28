"""Heat-affected zones: where a welded aluminium structure is actually weakest.

WHY THIS EXISTS
The jetpack frame is described throughout as a *welded* weldment, and every
safety factor ever computed for it used the **parent-metal** allowable —
276 MPa for 6061-T6511, straight off the supplier's product page.

That is not conservative. Welding a 6xxx aluminium alloy locally destroys the
T6 temper: the heat-affected zone reverts toward a substantially softer
condition, and design codes treat it as a different material with its own
reduced proof strength. A frame gated on parent-metal yield is gated on a
strength that does not exist at the joints — which is exactly where the load
path is most concentrated, and where this project has already found its peak
stresses sitting.

Aluminium differs sharply from steel here. A welded steel joint recovers most
of its strength; a welded 6xxx aluminium joint does not, and no amount of
post-weld handling short of full re-solution-treatment and ageing restores it.

    EN 1999-1-1 (Eurocode 9, Part 1-1), section 6.1.6, gives the HAZ softening
    factors rho_o,haz and rho_u,haz and the extent b_haz over which they apply.

THE VALUES ARE NOT EMBEDDED, AND THAT IS DELIBERATE
The softening factor depends on the alloy, the temper, the welding process,
the joint type, the thickness and whether the weld was made in one pass or
several. A wrong factor is worse than an absent one, so this module demands it
with a source — the same rule already applied to `E`, `yield`, the derating
curves and the S-N detail categories.

THE ENGINE CANNOT GUESS WHERE THE WELDS ARE
A spec that unions two boxes says nothing about whether the junction is
welded, bonded, bolted or machined from solid. Declaring a part "welded" is
not enough; the weld lines are explicit geometry, because the difference
between a HAZ that contains the peak stress and one that does not is the whole
answer.
"""

from __future__ import annotations

import math


class WeldError(ValueError):
    """A weld declaration that cannot be trusted."""


class HeatAffectedZone:
    """A softened region around a weld, with a sourced factor and extent.

    `factor` multiplies the parent proof strength: EN 1999-1-1's rho_o,haz.
    1.0 would mean welding costs nothing, which is not true of 6xxx aluminium
    and is refused as a likely placeholder.
    """

    def __init__(self, name: str, factor: float, extent_mm: float,
                 source: str, lines: list | None = None):
        if not isinstance(source, str) or not source.strip():
            raise WeldError(
                f"HeatAffectedZone({name!r}).source: required — cite the "
                f"softening factor and extent (e.g. 'EN 1999-1-1:2007 Table "
                f"6.4, 6082-T6 MIG, t<=15mm'). The factor depends on alloy, "
                f"temper, process, joint type and thickness; this engine will "
                f"not supply one")
        if not 0.0 < factor <= 1.0:
            raise WeldError(
                f"HeatAffectedZone({name!r}).factor: must be in (0, 1], got "
                f"{factor}. It multiplies the parent proof strength")
        if factor == 1.0:
            raise WeldError(
                f"HeatAffectedZone({name!r}).factor: 1.0 asserts that welding "
                f"costs no strength at all. That is not true of 6xxx aluminium "
                f"— if this joint genuinely has no HAZ (bonded, bolted, "
                f"machined from solid), do not declare a zone for it")
        if extent_mm <= 0:
            raise WeldError(
                f"HeatAffectedZone({name!r}).extent_mm: must be > 0. A zone "
                f"with no extent softens nothing and would silently pass")

        self.name = name
        self.factor = float(factor)
        self.extent_mm = float(extent_mm)
        self.source = source
        self.lines = [_check_line(l, f"{name}.lines[{i}]")
                      for i, l in enumerate(lines or [])]
        if not self.lines:
            raise WeldError(
                f"HeatAffectedZone({name!r}): no weld lines given. A spec that "
                f"unions two boxes says nothing about whether the junction is "
                f"welded, bonded or machined from solid — the engine cannot "
                f"guess, and a zone that matches nowhere would silently soften "
                f"nothing")

    # ------------------------------------------------------------- geometry
    def distance_to(self, point) -> float:
        """Shortest distance from a point to any weld line in this zone.

        Point-to-SEGMENT, not to an endpoint: a 240 mm weld run sampled at its
        midpoint would read 120 mm away from a peak sitting on one end of it.
        """
        return min(_point_to_segment(point, a, b) for a, b in self.lines)

    def contains(self, point) -> bool:
        return self.distance_to(point) <= self.extent_mm

    def to_dict(self) -> dict:
        return {"name": self.name, "factor": self.factor,
                "extent_mm": self.extent_mm, "source": self.source,
                "weld_lines": len(self.lines)}


class WeldMap:
    """All the heat-affected zones on one part."""

    def __init__(self, zones: list | None = None):
        self.zones = list(zones or [])

    def governing(self, point):
        """The zone that softens this point most, or None if it is parent metal.

        Most, not first: overlapping welds — a T-joint welded on both sides, a
        repair over an original run — leave the worst softening in force, not
        whichever zone happened to be declared earliest.
        """
        hits = [z for z in self.zones if z.contains(point)]
        return min(hits, key=lambda z: z.factor) if hits else None

    def allowable_at(self, point, parent_MPa: float) -> dict:
        """The proof strength actually available at this point.

        Returns the value AND why, so a safety factor can always say which
        material state it was computed against.
        """
        z = self.governing(point)
        if z is None:
            nearest = (min((zz.distance_to(point) for zz in self.zones),
                           default=None) if self.zones else None)
            return {"allowable_MPa": float(parent_MPa), "in_haz": False,
                    "zone": None, "factor": 1.0,
                    "nearest_haz_mm": (round(nearest, 4)
                                       if nearest is not None else None),
                    "basis": "parent metal"}
        return {"allowable_MPa": float(parent_MPa) * z.factor, "in_haz": True,
                "zone": z.name, "factor": z.factor,
                "distance_mm": round(z.distance_to(point), 4),
                "basis": f"HAZ softening x{z.factor:g} ({z.source})"}

    def to_dict(self) -> dict:
        return {"zones": [z.to_dict() for z in self.zones]}


def from_case(weld_spec) -> WeldMap:
    """Build a WeldMap from the `weld` block of an FEA case."""
    if not weld_spec:
        return WeldMap()
    if not isinstance(weld_spec, list):
        raise WeldError("case.weld: expected a list of heat-affected zones")
    zones = []
    for i, z in enumerate(weld_spec):
        if not isinstance(z, dict):
            raise WeldError(f"case.weld[{i}]: expected a dict")
        unknown = set(z) - {"name", "factor", "extent_mm", "source", "lines"}
        if unknown:
            raise WeldError(
                f"case.weld[{i}]: unexpected keys {sorted(unknown)} — allowed: "
                f"['extent_mm', 'factor', 'lines', 'name', 'source']")
        missing = {"factor", "extent_mm", "source", "lines"} - set(z)
        if missing:
            raise WeldError(f"case.weld[{i}]: missing {sorted(missing)}")
        zones.append(HeatAffectedZone(
            name=z.get("name", f"weld{i}"), factor=z["factor"],
            extent_mm=z["extent_mm"], source=z["source"], lines=z["lines"]))
    return WeldMap(zones)


# ------------------------------------------------------------------ helpers
def _check_line(line, ctx: str):
    try:
        a, b = line
        a = tuple(float(v) for v in a)
        b = tuple(float(v) for v in b)
    except (TypeError, ValueError):
        raise WeldError(
            f"{ctx}: expected [[x0,y0,z0], [x1,y1,z1]]") from None
    if len(a) != 3 or len(b) != 3:
        raise WeldError(f"{ctx}: each end needs three coordinates")
    if math.dist(a, b) == 0:
        raise WeldError(
            f"{ctx}: zero-length weld line. A weld run has extent; a point "
            f"weld should be given a short segment rather than a degenerate one")
    return (a, b)


def _point_to_segment(p, a, b) -> float:
    ax, ay, az = a
    dx, dy, dz = b[0] - ax, b[1] - ay, b[2] - az
    L2 = dx * dx + dy * dy + dz * dz
    t = ((p[0] - ax) * dx + (p[1] - ay) * dy + (p[2] - az) * dz) / L2
    t = max(0.0, min(1.0, t))
    return math.dist(p, (ax + t * dx, ay + t * dy, az + t * dz))
