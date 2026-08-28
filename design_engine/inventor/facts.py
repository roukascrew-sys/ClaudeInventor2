"""The vocabulary a validator uses to declare what it needs and what it establishes.

WHY THIS EXISTS
The engine's physics couples, and on 2026-08-28 three of five couplings held
only because a person remembered them. The two that did not:

    model.attached_mass  -> modal      32.45 kg of engines, fuel and cradles
                                       exists as constants in a screening
                                       formula and never reaches the mass
                                       matrix. Nothing signals the omission.

    dynamics.amplification -> fatigue  the modal solve found the frame runs at
                                       resonance; the fatigue check takes its
                                       stress ratio from the caller and never
                                       consults it.

Both are a validator producing a confident number from a model another
validator has already contradicted. Declaring the edges makes the omission
BLOCKING rather than invisible.

WHY THE VOCABULARY IS CLOSED
Same reason `_LIMIT_KEYS` and `_MATERIAL_KEYS` are closed in `fea.py`: a typo
in an open vocabulary is not an error, it is a dependency that silently never
matches — which is precisely the failure mode this module exists to end. An
unknown fact name is refused at declaration time.

A FACT IS NOT A METRIC
Metrics are outputs a requirement is checked against. A fact is a piece of
MODEL STATE that another validator's answer depends on. `sf.yield` is a
metric; `field.stress_peak` is the fact a fatigue calculation consumes.
"""

from __future__ import annotations

# Canonical facts. Grouped by what part of the model they describe, and each
# one exists because some validator genuinely depends on it — this is not an
# inventory of everything the engine knows.
FACTS: frozenset[str] = frozenset({
    # --- geometry -------------------------------------------------------
    "geometry.solid",           # a built, manifold CAD solid
    "geometry.mass_kg",         # structural mass from volume x density
    "geometry.sharp_edges",     # re-entrant edges found by singularity.py

    # --- the model as analysed ------------------------------------------
    "model.attached_mass",      # non-structural mass, sourced and located
    "model.load_cases",         # the set of load conditions to be checked
    "model.restraint",          # constraint sets with a verified rank

    # --- material -------------------------------------------------------
    "material.effective",       # E and yield after temperature derating
    "material.sn_curve",        # a sourced stress-life curve

    # --- computed fields ------------------------------------------------
    "field.stress_peak",        # peak von Mises, and where it sits
    "field.stress_converged",   # that peak, with a discretisation error bound
    "modal.frequencies",        # natural frequencies of the analysed model
    "dynamics.amplification",   # resonant amplification factor; needs damping
})

# Facts nothing in the engine currently produces. Listed explicitly because
# "no validator produces this" and "somebody forgot to declare it" look
# identical from inside the graph, and only the first is a modelling gap
# worth reporting to a human as such.
UNPRODUCED_TODAY: frozenset[str] = frozenset({
    "model.attached_mass",
    "field.stress_converged",
    "dynamics.amplification",
})

_WHY_MISSING = {
    "model.attached_mass":
        "32.45 kg of engines, fuel and cradles exists only as constants in the "
        "analytic screen; no stage puts it into the solved model",
    "field.stress_converged":
        "mesh convergence has never completed on this machine — the 2.8 mm "
        "solve crashes at a 6.1 GB working set, so no peak carries an error bound",
    "dynamics.amplification":
        "the amplification at resonance depends on damping, which this project "
        "has never measured",
}


class FactError(ValueError):
    """A fact name that is not in the vocabulary, or a malformed declaration."""


def validate(names, ctx: str) -> frozenset[str]:
    """Check a declared fact set, or refuse it.

    Returns a frozenset so a declaration cannot be mutated after the graph has
    been resolved against it.
    """
    if names is None:
        return frozenset()
    if isinstance(names, str):
        raise FactError(
            f"{ctx}: expected a set of fact names, got the single string "
            f"{names!r} — a bare string would be read as a set of characters")
    try:
        given = frozenset(names)
    except TypeError:
        raise FactError(f"{ctx}: expected an iterable of fact names, "
                        f"got {type(names).__name__}") from None
    unknown = sorted(given - FACTS)
    if unknown:
        raise FactError(
            f"{ctx}: unknown fact(s) {unknown}. The vocabulary is closed on "
            f"purpose — a typo in an open one is not an error, it is a "
            f"dependency that silently never matches. Known facts: "
            f"{sorted(FACTS)}")
    return given


def why_unproduced(fact: str) -> str:
    """Why nothing produces this fact, when that is a known modelling gap.

    A missing fact that nobody produces is a gap in the ENGINE; a missing fact
    that some stage produces but this run did not schedule is a gap in the
    RUN. Callers report them differently, so the distinction is kept here
    rather than left for a reader to infer.
    """
    return _WHY_MISSING.get(fact, "")
