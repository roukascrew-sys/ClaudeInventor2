"""Parameters nobody gets to choose, and what a verdict means across them.

WHY THIS EXISTS
Every safety factor this engine has ever reported was computed at ONE value of
every input. That answers a question nobody asked, because the built article is
not at nominal: the material scatters, the welding process varies, and the
sources disagree with each other.

    Verma, Kumar, Obayashi & Alam (arXiv:2210.07521) make the point with a
    worked case in which the DETERMINISTIC WINNER is rejected: a sharp peak of
    height 11 loses to a broad peak of height 10, because it cannot hold its
    response spread. Feasibility is a property of the neighbourhood, not of
    the point.

The live instance here is the jetpack frame's HAZ softening factor. Four
different sources give four different values, the frame's verdict depends on
which one is used, and until now one of them was silently chosen.

A DESIGN VARIABLE IS NOT AN UNCERTAIN PARAMETER
The distinction this module exists to enforce. A design variable is something
the designer CHOOSES - a thickness, a radius - and an optimiser is entitled to
move it. An uncertain parameter is something the designer DISCOVERS - the HAZ
softening of a welded joint, a friction coefficient, a derating factor. Letting
an optimiser "choose" one is how a design gets optimised into a value the
physical world never agreed to.

So these are deliberately NOT design variables, and nothing here returns a
best value. It returns what the verdict does across all of them.

DISCRETE SOURCED VALUES, NOT AN INTERVAL
The sources give points, not a distribution: 0.500 from Eurocode 9, 0.475 for
5356 filler, 0.450 for 4043, 0.375 as-welded. Sweeping "0.375 to 0.500" would
evaluate at values no source supports and imply a uniform density nobody
measured. Each value carries its own citation and is evaluated on its own.

THE RULE
If the verdict is the same at every sourced value, that is the verdict. If it
changes anywhere in the set, the answer is UNKNOWN - not the nominal one, and
not the optimistic one. A result that depends on an undisclosed choice between
defensible sources is not a result.
"""

from __future__ import annotations


class UncertaintyError(ValueError):
    """A sourced range that cannot support a verdict."""


class SourcedValue:
    """One defensible value for an uncertain parameter, with its citation.

    `source` is mandatory and unchecked prose, the same rule already applied to
    `E`, `yield`, the derating curves, the S-N categories and the HAZ factor
    itself. The engine does not invent material data, and it does not invent
    the uncertainty in material data either.
    """

    def __init__(self, value: float, source: str, label: str = ""):
        if not isinstance(source, str) or not source.strip():
            raise UncertaintyError(
                f"SourcedValue({value}): source is required. A value with no "
                f"citation cannot be defended, and an undefended value in a "
                f"sensitivity set makes the sensitivity look broader or "
                f"narrower than the evidence supports")
        self.value = float(value)
        self.source = source.strip()
        self.label = label.strip() or f"{self.value:g}"

    def __repr__(self) -> str:
        return f"SourcedValue({self.value:g}, {self.label!r})"

    def to_dict(self) -> dict:
        return {"value": self.value, "label": self.label, "source": self.source}


class SourcedRange:
    """Every value the evidence supports for one uncertain parameter.

    `nominal` must be one of the sourced values. Gating on a number that no
    source supports - a midpoint, a rounded figure, an average of
    disagreeing references - is the failure this class exists to prevent, and
    an average is the most tempting of the three because it looks balanced.
    """

    def __init__(self, name: str, values, nominal: float, units: str = ""):
        vals = list(values)
        if not vals:
            raise UncertaintyError(f"SourcedRange({name!r}): no values given")
        for v in vals:
            if not isinstance(v, SourcedValue):
                raise UncertaintyError(
                    f"SourcedRange({name!r}): every entry must be a "
                    f"SourcedValue carrying its own citation, got {type(v).__name__}")
        seen = {}
        for v in vals:
            if v.value in seen:
                raise UncertaintyError(
                    f"SourcedRange({name!r}): {v.value:g} appears twice "
                    f"({seen[v.value]!r} and {v.label!r}). Two sources "
                    f"agreeing is worth recording in the source text, but a "
                    f"duplicated value silently weights that point twice")
            seen[v.value] = v.label

        self.name = name
        self.units = units
        self.values = sorted(vals, key=lambda v: v.value)
        match = [v for v in self.values if v.value == float(nominal)]
        if not match:
            raise UncertaintyError(
                f"SourcedRange({name!r}): nominal {nominal:g} is not one of "
                f"the sourced values {[v.value for v in self.values]}. Gating "
                f"on a value no source supports - a midpoint, a rounded "
                f"figure, an average of disagreeing references - is exactly "
                f"what this class exists to prevent")
        self.nominal = match[0]

    @property
    def most_severe(self) -> SourcedValue:
        return self.values[0]

    @property
    def least_severe(self) -> SourcedValue:
        return self.values[-1]

    @property
    def is_single_source(self) -> bool:
        return len(self.values) == 1

    @property
    def span(self) -> float:
        return self.least_severe.value - self.most_severe.value

    def to_dict(self) -> dict:
        return {"name": self.name, "units": self.units,
                "nominal": self.nominal.to_dict(),
                "values": [v.to_dict() for v in self.values],
                "span": round(self.span, 6)}


def verdict_across(rng: SourcedRange, evaluate) -> dict:
    """Evaluate at every sourced value and decide what the set supports.

    `evaluate(value) -> (passed: bool, detail: dict)`.

    Three outcomes, and the third is the point:

        pass      every sourced value passes
        fail      every sourced value fails
        unknown   the verdict CHANGES inside the set

    An unknown here is not a missing measurement. It is a measured statement
    that the answer depends on which defensible source is chosen, which is a
    fact about the design and not a gap in the analysis. Reporting the nominal
    verdict instead would hide it.
    """
    rows = []
    for v in rng.values:
        passed, detail = evaluate(v.value)
        rows.append({"value": v.value, "label": v.label,
                     "passed": bool(passed), "source": v.source,
                     **(detail or {})})

    passes = {r["passed"] for r in rows}
    if len(passes) > 1:
        # Report the ADJACENT pair the verdict turns between, not the two
        # extremes. "it fails at 0.375 and passes at 0.500" is true of any
        # crossing; "it turns between 0.450 and 0.475" says where the answer
        # actually lives, and which source you would have to rule out.
        crossings = [(rows[i], rows[i + 1]) for i in range(len(rows) - 1)
                     if rows[i]["passed"] != rows[i + 1]["passed"]]
        lo, hi = crossings[0]
        extra = ("" if len(crossings) == 1 else
                 f" The verdict changes {len(crossings)} times across the "
                 f"set, so it is not monotonic in this parameter and the "
                 f"crossing below is only the first.")
        verdict, reason = "unknown", (
            f"the verdict changes inside the sourced range: {rng.name} turns "
            f"between {lo['value']:g} ({lo['label']}) and {hi['value']:g} "
            f"({hi['label']}). Both are defensible, so the result depends on "
            f"an undisclosed choice between sources and is not a result."
            + extra)
    elif passes == {True}:
        verdict, reason = "pass", (
            f"every sourced value of {rng.name} passes, including the most "
            f"severe ({rng.most_severe.value:g}, {rng.most_severe.label})")
    else:
        verdict, reason = "fail", (
            f"every sourced value of {rng.name} fails, including the least "
            f"severe ({rng.least_severe.value:g}, {rng.least_severe.label}). "
            f"No defensible choice of source rescues this")

    out = {"verdict": verdict, "reason": reason, "parameter": rng.to_dict(),
           "evaluated": rows, "checked": len(rows)}
    if rng.is_single_source:
        out["caveat"] = (
            "only one sourced value exists, so this is NOT a sensitivity "
            "check - it cannot detect a verdict that would change under a "
            "source this project has not found")
    return out


def required_value(sf_at_reference: float, reference_value: float,
                   required_sf: float) -> dict:
    """The parameter value at which the gate would just be met.

    ONLY valid where the safety factor is LINEAR in the parameter, which is
    true for a strength-reduction factor and a stress field that does not
    depend on it: SF = allowable / stress, allowable = parent * rho, and rho
    does not move the stress. It is NOT true of a parameter that changes the
    stiffness distribution, the load path or the geometry, and this function
    has no way to detect that - the caller has to know.

    Returned with `assumption` stated so the number cannot be quoted without
    the condition it rests on.
    """
    if reference_value <= 0:
        raise UncertaintyError("reference_value must be > 0")
    if sf_at_reference <= 0:
        raise UncertaintyError("sf_at_reference must be > 0")
    per_unit = sf_at_reference / reference_value
    return {"required": round(required_sf / per_unit, 6),
            "sf_per_unit": round(per_unit, 6),
            "assumption": ("safety factor is linear in this parameter; true "
                           "for a strength-reduction factor that does not "
                           "alter the stress field, false for anything that "
                           "changes stiffness, load path or geometry")}
