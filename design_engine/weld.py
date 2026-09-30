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


# ===========================================================================
# EN 1999-1-1 (Eurocode 9) 8.6.3.4 - design resistance in the heat-affected
# zone of a connection.
#
# THIS IS A DIFFERENT CHECK FROM THE ONE ABOVE, AND THE DIFFERENCE MATTERS.
#
# `HeatAffectedZone.factor` above is rho_o,haz from 6.1.6: it softens the 0.2%
# PROOF strength for a MEMBER check. That is correct for what it does, and this
# project already applies it with four sourced factors.
#
# 8.6.3.4 is a CONNECTION check, and it is written against f_u,haz - the
# ULTIMATE strength in the heat-affected zone - divided by gamma_Mw. Reading
# across from one clause to the other is exactly the mistake this project made
# once already in the other direction, so the two are kept apart in the code as
# the standard keeps them apart on the page.
#
#     (8.42) butt welds, (8.43) fillet welds:
#         sqrt(sigma_haz,Ed^2 + 3*tau_haz,Ed^2)  <=  f_u,haz / gamma_Mw
#     checked at the fusion boundary (HAZ F) and at the toe of the weld
#     (HAZ T), on the FULL CROSS SECTION.
#
#     8.6.2(3):  f_v,haz = f_u,haz / sqrt(3)
#     Table 8.1: gamma_Mw = 1,25 recommended; a National Annex may set another.
#
# Source: EN 1999-1-1:2007+A1:2009, 8.6.3.4, Table 8.1, Table 3.2.
#
# NOT IMPLEMENTED, DELIBERATELY: the weld-METAL checks of 8.6.3.2 (butt) and
# 8.6.3.3 (fillet), which go against f_w from Table 8.8 rather than f_u,haz.
# Equation (8.33) is typeset as an image in the copy of the standard this was
# read from and did not extract, so its exact form is not in hand. Writing it
# from memory would put a wrong number behind a code reference, which is worse
# than an absent check. `weld_static` therefore covers the HAZ and says so; it
# is not a complete connection design.
# ===========================================================================

_ROLES = ("HAZ F", "HAZ T")

# Table 3.2's HAZ columns are stated for MIG welding of material up to 15 mm
# thick. Outside that the standard requires a further reduction, and this
# engine does not hold its value. The condition is enforced rather than
# footnoted because the jetpack crossbeam is 15.875 mm: the one part this was
# built for is already outside the range, and a note in a docstring would not
# have stopped it being used anyway.
_HAZ_VALID_PROCESS = "MIG"
_HAZ_VALID_THICKNESS_MM = 15.0

_PRECIPITATION = "precipitation_hardening"
_STRAIN = "strain_hardening"
#: 6xxx and 7xxx are precipitation hardening; 3xxx, 5xxx and 8011A are strain
#: hardening. The engine does NOT infer this from an alloy designation - the
#: material name in a case is a free string, and parsing a strength-governing
#: decision out of one is the class of guess this module exists to refuse.
_ALLOY_FAMILIES = (_PRECIPITATION, _STRAIN)

_MIG_VALID_THICKNESS_MM = 15.0
_TIG_VALID_THICKNESS_MM = 6.0

_FOOTNOTE_4 = (
    "EN 1999-1-1:2007+A1:2009 Table 3.2b footnote 4 (identically Table 3.2a "
    "footnote 2): HAZ values are valid for MIG welding up to 15 mm; TIG on "
    "6xxx/7xxx up to 6 mm takes 0,8; above those thicknesses the HAZ values "
    "and rho-factors are reduced by a further 0,8 (6xxx/7xxx) or 0,9 "
    "(3xxx/5xxx/8011A); the reductions do not apply in temper O. 'Higher "
    "thickness' is fixed at >15 mm for MIG and >6 mm for TIG by Table 3.2c "
    "footnote 2, which points at footnote 4 for exactly those two cases")


def en1999_haz_reduction(alloy_family: str, process: str, thickness_mm: float,
                         temper: str | None = None) -> dict:
    """The reduction Table 3.2b footnote 4 puts on the tabulated HAZ values.

    This is a SOURCED factor, not a house rule, which is why the engine may
    apply it without being handed a number. It was previously refused outright
    because the footnote had not been read: on 2026-09-30 `weld_static` shipped
    demanding a `reduction_factor` the standard turns out to supply itself.

    What the footnote does NOT do is say which family an alloy belongs to, so
    that stays a declaration. 6061 is 6xxx and therefore precipitation
    hardening, but the engine is not in the business of reading that off a
    string.

    One word is being interpreted. The footnote says the values are reduced by
    "a further 0,8" above the thickness limit, and "further" is natural for the
    TIG branch, which has already taken 0,8, but loose for the MIG branch,
    which has taken nothing. It is read here as: apply 0,8 to whatever the
    process branch already gives. Under the other reading MIG above 15 mm would
    be entirely uncovered - which Table 3.2c footnote 2 forbids, since it sends
    thicknesses over 15 mm MIG to this very footnote for an answer.
    """
    if alloy_family not in _ALLOY_FAMILIES:
        raise WeldError(
            f"alloy_family: must be one of {list(_ALLOY_FAMILIES)}, got "
            f"{alloy_family!r}. EN 1999-1-1 Table 3.2b footnote 4 reduces "
            f"6xxx/7xxx and 3xxx/5xxx/8011A by different factors, and this "
            f"engine will not read the family off an alloy designation")
    if not isinstance(thickness_mm, (int, float)) or thickness_mm <= 0:
        raise WeldError("thickness_mm: must be a positive number")

    precip = alloy_family == _PRECIPITATION
    steps = []

    if temper is not None and str(temper).strip().upper() == "O":
        return {"factor": 1.0,
                "basis": "temper O: footnote 4 says these reductions do not "
                         "apply in temper O",
                "source": _FOOTNOTE_4,
                "process": process, "thickness_mm": float(thickness_mm),
                "alloy_family": alloy_family, "temper": temper}

    proc = str(process).strip().upper()
    if proc == "MIG":
        limit = _MIG_VALID_THICKNESS_MM
        factor = 1.0
        steps.append("MIG: tabulated values apply as printed up to 15 mm")
    elif proc == "TIG":
        limit = _TIG_VALID_THICKNESS_MM
        factor = 0.8 if precip else 1.0
        steps.append(
            "TIG on 6xxx/7xxx up to 6 mm: x0,8" if precip
            else "TIG on 3xxx/5xxx/8011A up to 6 mm: tabulated values apply")
    else:
        raise WeldError(
            f"process {process!r}: EN 1999-1-1 Table 3.2b footnote 4 covers MIG "
            f"and TIG only. A different process needs its own source, and this "
            f"engine will not extrapolate one from the two it has")

    if float(thickness_mm) > limit:
        step = 0.8 if precip else 0.9
        factor *= step
        steps.append(
            f"thickness {float(thickness_mm):g} mm exceeds the {limit:g} mm "
            f"{proc} limit: a further x{step:g} for "
            f"{'6xxx/7xxx' if precip else '3xxx/5xxx/8011A'}")

    return {"factor": round(factor, 6), "basis": "; ".join(steps),
            "source": _FOOTNOTE_4, "process": process,
            "thickness_mm": float(thickness_mm),
            "alloy_family": alloy_family, "temper": temper}


_RESISTANCE_KEYS = {"f_u_haz_MPa", "source", "gamma_Mw", "gamma_Mw_source",
                    "process", "thickness_mm", "reduction_factor",
                    "reduction_factor_source", "alloy_family", "temper"}
_SECTION_KEYS = {"name", "role", "point_mm", "normal"}


class WeldResistance:
    """f_u,haz / gamma_Mw, with both numbers sourced and the validity checked.

    Nothing here is embedded. f_u,haz depends on alloy, temper and product
    form - EN 1999-1-1 Table 3.2 lists it row by row, and the sheet/plate row
    and the extruded row of one alloy are not the same numbers. gamma_Mw is a
    National Annex parameter. The engine demands both with a citation, the same
    rule already applied to E, yield, the derating curves and the S-N detail
    categories.
    """

    def __init__(self, spec: dict):
        if not isinstance(spec, dict):
            raise WeldError(
                "case.limit_state.resistance: expected a dict carrying "
                "f_u_haz_MPa, gamma_Mw and their sources")
        unknown = set(spec) - _RESISTANCE_KEYS
        if unknown:
            raise WeldError(
                f"case.limit_state.resistance: unexpected keys "
                f"{sorted(unknown)} - allowed: {sorted(_RESISTANCE_KEYS)}")

        f_u = spec.get("f_u_haz_MPa")
        if not isinstance(f_u, (int, float)) or isinstance(f_u, bool) or f_u <= 0:
            raise WeldError(
                "case.limit_state.resistance.f_u_haz_MPa: required, > 0. This "
                "is the ULTIMATE strength in the heat-affected zone, which is "
                "what EN 1999-1-1 8.6.3.4 is written against - NOT the proof "
                "strength rho_o,haz*f_o that 6.1.6 uses for member checks. "
                "They are different clauses answering different questions, and "
                "substituting one for the other mis-states the joint in "
                "whichever direction the swap happens to fall")
        src = spec.get("source")
        if not isinstance(src, str) or not src.strip():
            raise WeldError(
                "case.limit_state.resistance.source: required - cite the row, "
                "e.g. 'EN 1999-1-1:2007+A1:2009 Table 3.2, 6061 T6/T651 sheet "
                "and plate, 12,5 < t <= 80 mm: f_u,haz = 175 N/mm2'. The "
                "sheet/plate and extruded rows of one alloy carry different "
                "numbers and the engine cannot tell which product form a spec "
                "describes")

        g = spec.get("gamma_Mw")
        if not isinstance(g, (int, float)) or isinstance(g, bool):
            raise WeldError(
                "case.limit_state.resistance.gamma_Mw: required. EN 1999-1-1 "
                "Table 8.1 recommends 1,25 for welded connections and a "
                "National Annex may set another value, so it is declared "
                "rather than assumed")
        if g < 1.0:
            raise WeldError(
                f"case.limit_state.resistance.gamma_Mw: {g} is below 1,0, which "
                f"would make the design resistance exceed the characteristic "
                f"one. Table 8.1 recommends 1,25")
        gsrc = spec.get("gamma_Mw_source")
        if not isinstance(gsrc, str) or not gsrc.strip():
            raise WeldError(
                "case.limit_state.resistance.gamma_Mw_source: required - a "
                "partial factor is a National Annex decision, so name the "
                "annex, or cite Table 8.1's recommended value explicitly")

        process = spec.get("process")
        if not isinstance(process, str) or not process.strip():
            raise WeldError(
                "case.limit_state.resistance.process: required (e.g. 'MIG', "
                "'TIG'). Table 3.2's HAZ columns are stated for MIG; TIG "
                "softens more and needs a further reduction")
        t = spec.get("thickness_mm")
        if not isinstance(t, (int, float)) or isinstance(t, bool) or t <= 0:
            raise WeldError(
                "case.limit_state.resistance.thickness_mm: required, > 0 - the "
                "thickness of the material being joined. Table 3.2's HAZ "
                "columns are stated up to 15 mm, and the engine cannot read "
                "the governing thickness off the geometry because which of two "
                "joined parts governs is a judgement about the joint")

        red = spec.get("reduction_factor")
        redsrc = spec.get("reduction_factor_source")
        family = spec.get("alloy_family")
        temper = spec.get("temper")
        outside = []
        if process.strip().upper() != _HAZ_VALID_PROCESS:
            outside.append(f"process {process!r} is not {_HAZ_VALID_PROCESS}")
        if float(t) > _HAZ_VALID_THICKNESS_MM:
            outside.append(f"thickness {float(t):g} mm exceeds "
                           f"{_HAZ_VALID_THICKNESS_MM:g} mm")

        # THE REDUCTION IS IN THE STANDARD, so the engine derives it rather
        # than demanding a number. It refused to on 2026-09-30 because the
        # footnote had not been read; that refusal was right at the time and is
        # wrong now, and the difference is a source, not a change of policy.
        #
        # What still is NOT derived is which family the alloy belongs to.
        # Footnote 4 reduces 6xxx/7xxx and 3xxx/5xxx/8011A differently, and
        # reading that off a free-text alloy name would be the guess this
        # module exists to refuse.
        derived = None
        if outside and red is None:
            if family is None:
                raise WeldError(
                    "case.limit_state.resistance: the tabulated HAZ strength "
                    "is outside its stated validity ("
                    + "; ".join(outside) + "), and EN 1999-1-1 Table 3.2b "
                    "footnote 4 gives the reduction that applies - 0,8 for "
                    "6xxx/7xxx, 0,9 for 3xxx/5xxx/8011A. The engine will apply "
                    "it, but not guess which family this alloy is in. Declare "
                    "alloy_family as 'precipitation_hardening' (6xxx, 7xxx) or "
                    "'strain_hardening' (3xxx, 5xxx, 8011A), and temper if it "
                    "is O. To override the code value instead, supply "
                    "reduction_factor with reduction_factor_source")
            derived = en1999_haz_reduction(family, process, float(t), temper)
            red = derived["factor"]
            redsrc = derived["source"] + " | " + derived["basis"]
        if red is not None:
            if (not isinstance(red, (int, float)) or isinstance(red, bool)
                    or not 0.0 < red <= 1.0):
                raise WeldError(
                    f"case.limit_state.resistance.reduction_factor: must be in "
                    f"(0, 1], got {red!r}. It multiplies f_u,haz downward for a "
                    f"joint outside the tabulated validity")
            if not isinstance(redsrc, str) or not redsrc.strip():
                raise WeldError(
                    "case.limit_state.resistance.reduction_factor_source: "
                    "required whenever a reduction factor is given. An "
                    "unsourced reduction is the same failure as an unsourced "
                    "strength, one step further from view")

        self.f_u_haz_MPa = float(f_u)
        self.source = src
        self.gamma_Mw = float(g)
        self.gamma_Mw_source = gsrc
        self.process = process
        self.thickness_mm = float(t)
        self.reduction_factor = float(red) if red is not None else None
        self.reduction_factor_source = redsrc
        self.outside_validity = outside
        self.alloy_family = family
        self.temper = temper
        self.derived = derived

    @property
    def f_u_haz_effective_MPa(self) -> float:
        r = self.reduction_factor if self.reduction_factor is not None else 1.0
        return self.f_u_haz_MPa * r

    @property
    def design_MPa(self) -> float:
        """f_u,haz / gamma_Mw - the right-hand side of (8.42) and (8.43)."""
        return self.f_u_haz_effective_MPa / self.gamma_Mw

    @property
    def shear_design_MPa(self) -> float:
        """f_v,haz / gamma_Mw, with f_v,haz = f_u,haz / sqrt(3) per 8.6.2(3)."""
        return self.design_MPa / math.sqrt(3.0)

    def to_dict(self) -> dict:
        return {
            "clause": "EN 1999-1-1:2007+A1:2009 8.6.3.4",
            "f_u_haz_MPa": self.f_u_haz_MPa,
            "f_u_haz_source": self.source,
            "reduction_factor": self.reduction_factor,
            "reduction_factor_source": self.reduction_factor_source,
            "f_u_haz_effective_MPa": round(self.f_u_haz_effective_MPa, 6),
            "gamma_Mw": self.gamma_Mw,
            "gamma_Mw_source": self.gamma_Mw_source,
            "design_resistance_MPa": round(self.design_MPa, 6),
            "shear_design_resistance_MPa": round(self.shear_design_MPa, 6),
            "process": self.process,
            "thickness_mm": self.thickness_mm,
            "alloy_family": self.alloy_family,
            "temper": self.temper,
            "outside_tabulated_validity": self.outside_validity,
            # Present when the reduction came from the standard rather than
            # from the case, so a reader can tell a code value from a declared
            # one without comparing source strings.
            "reduction_derived": self.derived,
        }


def check_haz(sigma_MPa: float, tau_MPa: float, resistance) -> dict:
    """EN 1999-1-1 (8.42)/(8.43): sqrt(sigma^2 + 3*tau^2) <= f_u,haz/gamma_Mw.

    Both stresses are taken on the full cross section, which is what the clause
    asks for and is also why this check is usable where a von Mises peak is
    not: a section resultant is fixed by equilibrium, so it converges, while a
    peak at a re-entrant corner has no converged value to fix.
    """
    comb = math.sqrt(float(sigma_MPa) ** 2 + 3.0 * float(tau_MPa) ** 2)
    rd = resistance.design_MPa
    return {
        "sigma_haz_Ed_MPa": round(float(sigma_MPa), 6),
        "tau_haz_Ed_MPa": round(float(tau_MPa), 6),
        "combined_MPa": round(comb, 6),
        "design_resistance_MPa": round(rd, 6),
        "utilisation": round(comb / rd, 6) if rd > 0 else None,
        "safety_factor": (round(rd / comb, 6) if comb > 0 else math.inf),
        "passes": comb <= rd,
    }


def sections_from_case(spec) -> list:
    """Validate the `sections` list of a weld_static limit state."""
    if not isinstance(spec, list) or not spec:
        raise WeldError(
            "case.limit_state.sections: at least one section is required. "
            "EN 1999-1-1 8.6.3.4 checks the HAZ at the fusion boundary and at "
            "the toe of the weld on the full cross section, so the engine has "
            "to be told where to cut. It cannot infer the cut from a weld "
            "line: which plane carries the joint is a judgement about the load "
            "path, not a property of the geometry")
    out = []
    for i, s in enumerate(spec):
        if not isinstance(s, dict):
            raise WeldError(f"case.limit_state.sections[{i}]: expected a dict")
        unknown = set(s) - _SECTION_KEYS
        if unknown:
            raise WeldError(
                f"case.limit_state.sections[{i}]: unexpected keys "
                f"{sorted(unknown)} - allowed: {sorted(_SECTION_KEYS)}")
        missing = {"role", "point_mm", "normal"} - set(s)
        if missing:
            raise WeldError(
                f"case.limit_state.sections[{i}]: missing {sorted(missing)}")
        if s["role"] not in _ROLES:
            raise WeldError(
                f"case.limit_state.sections[{i}].role: must be one of "
                f"{list(_ROLES)} - 'HAZ F' at the fusion boundary, 'HAZ T' at "
                f"the toe of the weld. EN 1999-1-1 8.6.3.4 names both, they are "
                f"different planes, and recording which one a number came from "
                f"is the difference between a check and a number")
        for key in ("point_mm", "normal"):
            v = s[key]
            if (not isinstance(v, (list, tuple)) or len(v) != 3
                    or any(not isinstance(x, (int, float)) or isinstance(x, bool)
                           for x in v)):
                raise WeldError(
                    f"case.limit_state.sections[{i}].{key}: three numbers "
                    f"required")
        if all(float(x) == 0.0 for x in s["normal"]):
            raise WeldError(
                f"case.limit_state.sections[{i}].normal: the zero vector "
                f"defines no plane")
        out.append({"name": s.get("name", f"section{i}"), "role": s["role"],
                    "point_mm": [float(x) for x in s["point_mm"]],
                    "normal": [float(x) for x in s["normal"]]})
    names = [s["name"] for s in out]
    if len(set(names)) != len(names):
        raise WeldError(
            f"case.limit_state.sections: duplicate names {sorted(names)} - "
            f"every section is reported by name, and two sharing one makes the "
            f"log ambiguous about which plane a stress came from")
    return out
