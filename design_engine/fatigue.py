"""Fatigue life against a sourced S-N curve (roadmap B1).

WHY THIS IS THE URGENT ONE
The vault's Jetpack Frame note lists what the design does not cover and adds
that those are what actually kill jetpack pilots. Fatigue at 98,000 rpm is on
that list, and B2 made it worse than it looked: mode 18 of the frame sits 0.4%
from the 1633.3 Hz shaft frequency, so the structure is driven at resonance.

At 1633 Hz a frame accumulates **5.88 million cycles per hour**. Ten minutes of
running is a million cycles. Static strength says nothing about that.

THE FACT THAT MAKES ALUMINIUM DIFFERENT
Ferritic steels have a true endurance limit: below some stress range they
survive indefinitely. **Aluminium alloys do not.** The S-N curve keeps
descending, so there is no stress range at which an aluminium frame lasts
forever — only a range at which it lasts long enough. A fatigue check that
assumes an endurance limit will pass a part that is guaranteed to crack.

So `endurance_limit_MPa` is not optional-with-a-default here. It must be stated
explicitly, including stating it as `None`, and `None` means every load range
produces finite life. Defaulting it either way would silently decide the
question this module exists to ask.

WHAT THIS MODULE REFUSES
  - unsourced curves, exactly as `validate_case` refuses unsourced E and yield
  - extrapolation beyond the curve's stated cycle range, exactly as
    `derate_factor` refuses to extrapolate a temperature curve
  - inventing a stress range. The alternating amplitude at resonance depends on
    damping, which this project has never measured. The engine computes life
    FROM a range and will not guess one.

STANDARDS THIS IS SHAPED FOR, not shipped with
The Basquin form below is the shape used by EN 1999-1-3 (aluminium) and
EN 1993-1-9 (steel), where a detail category is the stress range at 2e6 cycles
and the curve is a power law in log-log. Detail-category VALUES are deliberately
not embedded: they depend on the joint geometry, and a wrong one is worse than
an absent one. The caller supplies them with their source.
"""

from __future__ import annotations

import math


class FatigueError(ValueError):
    """A fatigue input or query that cannot be answered honestly."""


class SNCurve:
    """A sourced stress-life curve in the Basquin / Eurocode power-law form.

        N = N_ref * (delta_sigma_ref / delta_sigma) ** m

    `endurance_limit_MPa=None` states that the material has NO endurance limit,
    which is the physically correct choice for aluminium. A number states one
    and is honoured. There is no default, because the two answers differ by
    "this part lasts forever" versus "this part will crack".
    """

    def __init__(self, name: str, detail_category_MPa: float, slope_m: float,
                 source: str, *, reference_cycles: float = 2e6,
                 endurance_limit_MPa: float | None = ...,
                 valid_cycles: tuple[float, float] = (1e4, 1e8)):
        if not isinstance(source, str) or not source.strip():
            raise FatigueError(
                "SNCurve.source: required — cite where the detail category and "
                "slope come from (e.g. 'EN 1999-1-3:2007 Table J.1, detail "
                "type 9.1'). This engine does not accept unsourced fatigue data")
        if endurance_limit_MPa is ...:
            raise FatigueError(
                f"SNCurve({name!r}).endurance_limit_MPa: must be stated "
                f"explicitly, including as None. Aluminium alloys have NO "
                f"endurance limit — their S-N curve keeps descending — while "
                f"ferritic steels do. Defaulting this would silently decide "
                f"whether the part can last forever")
        if detail_category_MPa <= 0 or slope_m <= 0 or reference_cycles <= 0:
            raise FatigueError(
                "SNCurve: detail_category_MPa, slope_m and reference_cycles "
                "must all be positive")
        lo, hi = valid_cycles
        if not 0 < lo < hi:
            raise FatigueError(f"SNCurve.valid_cycles: need 0 < lo < hi, got {valid_cycles}")

        self.name = name
        self.detail_category_MPa = float(detail_category_MPa)
        self.slope_m = float(slope_m)
        self.reference_cycles = float(reference_cycles)
        self.endurance_limit_MPa = (None if endurance_limit_MPa is None
                                    else float(endurance_limit_MPa))
        self.source = source
        self.valid_cycles = (float(lo), float(hi))

    # ------------------------------------------------------------------ query
    def allowable_cycles(self, stress_range_MPa: float) -> float:
        """Cycles to failure at this stress range. `inf` only below a STATED
        endurance limit — never as a default."""
        if stress_range_MPa <= 0:
            raise FatigueError("stress_range_MPa must be > 0; a zero range is "
                               "not a load cycle")
        if (self.endurance_limit_MPa is not None
                and stress_range_MPa <= self.endurance_limit_MPa):
            return math.inf
        n = self.reference_cycles * (
            self.detail_category_MPa / stress_range_MPa) ** self.slope_m
        lo, hi = self.valid_cycles
        if n > hi:
            raise FatigueError(
                f"{self.name}: a range of {stress_range_MPa:.3g} MPa implies "
                f"{n:.3g} cycles, beyond the curve's validated range "
                f"(<= {hi:.3g}). This engine will not extrapolate an S-N curve "
                f"— beyond the data, 'very long life' and 'infinite life' are "
                f"not the same claim and only one of them is safe. "
                f"{'This curve states NO endurance limit, so life is finite '
                   'however small the range.' if self.endurance_limit_MPa is None else ''}"
                f" Source: {self.source}")
        if n < lo:
            raise FatigueError(
                f"{self.name}: a range of {stress_range_MPa:.3g} MPa implies "
                f"only {n:.3g} cycles, below the curve's validated range "
                f"(>= {lo:.3g}). That is the low-cycle regime, where the "
                f"stress-life method does not apply — plastic strain governs "
                f"and a strain-life analysis is required instead")
        return n

    def allowable_range(self, cycles: float) -> float:
        """The inverse question, and usually the more useful one: at a required
        life, what stress range may the structure see?"""
        if cycles <= 0:
            raise FatigueError("cycles must be > 0")
        return self.detail_category_MPa * (
            self.reference_cycles / cycles) ** (1.0 / self.slope_m)

    def to_dict(self) -> dict:
        return {"name": self.name,
                "detail_category_MPa": self.detail_category_MPa,
                "slope_m": self.slope_m,
                "reference_cycles": self.reference_cycles,
                "endurance_limit_MPa": self.endurance_limit_MPa,
                "has_endurance_limit": self.endurance_limit_MPa is not None,
                "valid_cycles": list(self.valid_cycles),
                "source": self.source}


def cycles_from_exposure(frequency_hz: float, hours: float) -> float:
    """Cycles accumulated by running at `frequency_hz` for `hours`.

    Written out because the number is counter-intuitive and it is the whole
    reason resonance is a fatigue problem rather than a comfort one: at the
    jetpack's 1633 Hz shaft frequency this is 5.88 million cycles per hour, so
    a ten-minute flight is a million cycles.
    """
    if frequency_hz <= 0 or hours < 0:
        raise FatigueError("frequency_hz must be > 0 and hours >= 0")
    return frequency_hz * 3600.0 * hours


def miner_damage(curve: SNCurve, spectrum: list) -> dict:
    """Palmgren-Miner cumulative damage over a load spectrum.

        D = sum(n_i / N_i),  failure predicted at D >= 1

    `spectrum` is [(stress_range_MPa, cycles), ...]. Miner's rule ignores
    sequence effects and is known to be non-conservative for some spectra; it
    is the standard first-pass method and is labelled as such rather than
    presented as a prediction.
    """
    if not spectrum:
        raise FatigueError("spectrum: at least one (range, cycles) block required")
    total, blocks = 0.0, []
    for i, item in enumerate(spectrum):
        try:
            rng, n = float(item[0]), float(item[1])
        except (TypeError, IndexError, ValueError):
            raise FatigueError(
                f"spectrum[{i}]: expected (stress_range_MPa, cycles)") from None
        if n < 0:
            raise FatigueError(f"spectrum[{i}]: cycles must be >= 0")
        N = curve.allowable_cycles(rng)
        d = 0.0 if N == math.inf else n / N
        total += d
        blocks.append({"stress_range_MPa": rng, "cycles": n,
                       "allowable_cycles": ("inf" if N == math.inf else N),
                       "damage": d})
    return {"damage": total,
            "survives": total < 1.0,
            "blocks": blocks,
            "method": "Palmgren-Miner linear cumulative damage; ignores "
                      "sequence effects and can be non-conservative for "
                      "variable-amplitude spectra",
            "curve": curve.to_dict()}


def stress_range_from_ratio(peak_MPa: float, R: float) -> float:
    """Stress range from a peak stress and a cycle ratio R = sigma_min/sigma_max.

    R = 0 is zero-to-peak, so the range IS the peak. R = -1 is fully reversed,
    so the range is TWICE the peak — the case people most often get wrong, and
    the one that halves predicted life at a given slope of 3.
    """
    if peak_MPa <= 0:
        raise FatigueError("peak_MPa must be > 0")
    if R >= 1.0:
        raise FatigueError(
            f"stress ratio R={R} implies no cycling at all (min == max); "
            f"fatigue needs a varying stress")
    return peak_MPa * (1.0 - R)
