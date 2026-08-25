"""A small library of L0 analytic models.

These exist because the audit found the real gap: there was nothing between
"do nothing" and "build geometry in the kernel". L0 screening needs closed
form, and closed form is cheap enough (~microseconds) to filter a population
of 100,000 before anything expensive runs.

They are a LIBRARY, not a framework assumption. `AnalyticStage` takes any
callable, so a pressure vessel, a linkage or a thermal problem supplies its
own model without touching this file. Nothing in `inventor/` assumes a design
is a beam.

Every model here states its assumptions in its docstring and returns metrics
under the same namespaced names the higher-fidelity stages use, so a
constraint written once applies at every fidelity — and the fidelity tag on
the result is what tells you which one answered.
"""

from __future__ import annotations

import math

# --- section properties ----------------------------------------------

def rect_section(width_mm: float, height_mm: float) -> dict:
    """Solid rectangle bending about the axis perpendicular to `height`."""
    area = width_mm * height_mm
    I = width_mm * height_mm ** 3 / 12.0
    return {"area_mm2": area, "I_mm4": I, "c_mm": height_mm / 2.0,
            "Z_mm3": I / (height_mm / 2.0) if height_mm else 0.0}


def hollow_rect_section(width_mm: float, height_mm: float, wall_mm: float) -> dict:
    """Rectangular tube. Returns zeros if the wall consumes the section."""
    wi, hi = width_mm - 2 * wall_mm, height_mm - 2 * wall_mm
    if wi <= 0 or hi <= 0:
        return {"area_mm2": 0.0, "I_mm4": 0.0, "c_mm": height_mm / 2.0, "Z_mm3": 0.0}
    area = width_mm * height_mm - wi * hi
    I = (width_mm * height_mm ** 3 - wi * hi ** 3) / 12.0
    return {"area_mm2": area, "I_mm4": I, "c_mm": height_mm / 2.0,
            "Z_mm3": I / (height_mm / 2.0)}


def channel_section(w_mm: float, h_mm: float, t_mm: float) -> dict:
    """Open U-channel. The neutral axis is NOT at mid-height.

    Kept because this project already learned the hard way that a
    hollow-rectangle approximation under-predicts an open channel by ~72%,
    and an L0 model that is wrong in the unsafe direction is worse than none.
    """
    a_f, y_f = w_mm * t_mm, -h_mm / 2.0 + t_mm / 2.0
    a_w, y_w = 2.0 * t_mm * (h_mm - t_mm), t_mm / 2.0
    area = a_f + a_w
    if area <= 0:
        return {"area_mm2": 0.0, "I_mm4": 0.0, "c_mm": 0.0, "Z_mm3": 0.0}
    ybar = (a_f * y_f + a_w * y_w) / area
    i_f = w_mm * t_mm ** 3 / 12.0 + a_f * (y_f - ybar) ** 2
    i_w = 2.0 * (t_mm * (h_mm - t_mm) ** 3 / 12.0) + a_w * (y_w - ybar) ** 2
    I = i_f + i_w
    c = h_mm / 2.0 - ybar
    return {"area_mm2": area, "I_mm4": I, "c_mm": c,
            "Z_mm3": I / c if c else 0.0, "ybar_mm": ybar}


# --- load cases -------------------------------------------------------

def cantilever_point_load(P_N: float, L_mm: float, section: dict,
                          E_MPa: float) -> dict:
    """Tip-loaded cantilever. M = P*L at the root."""
    M = P_N * L_mm
    Z = section["Z_mm3"]
    I = section["I_mm4"]
    return {"moment_Nmm": M,
            "bending_stress_MPa": (M / Z) if Z else float("inf"),
            "tip_deflection_mm": (P_N * L_mm ** 3 / (3.0 * E_MPa * I)) if I else float("inf")}


def simply_supported_centre_load(P_N: float, L_mm: float, section: dict,
                                 E_MPa: float) -> dict:
    """Centre-loaded simple span. M = P*L/4."""
    M = P_N * L_mm / 4.0
    Z, I = section["Z_mm3"], section["I_mm4"]
    return {"moment_Nmm": M,
            "bending_stress_MPa": (M / Z) if Z else float("inf"),
            "midspan_deflection_mm": (P_N * L_mm ** 3 / (48.0 * E_MPa * I)) if I else float("inf")}


def euler_buckling(E_MPa: float, I_mm4: float, L_mm: float, K: float = 1.0) -> float:
    """Critical load, N. K: 1.0 pinned-pinned, 0.6992 fixed-pinned, 0.5 fixed-fixed."""
    if I_mm4 <= 0 or L_mm <= 0:
        return 0.0
    return math.pi ** 2 * E_MPa * I_mm4 / (K * L_mm) ** 2


# --- composite screening model ---------------------------------------

def beam_screen(section_fn, load_fn, *, length_key: str, force_N: float,
                material_fn, K: float = 1.0, density_key: str | None = None):
    """Build an L0 model callable for a prismatic beam.

    Returns `fn(values, ctx) -> metrics` suitable for `AnalyticStage`.

    Emits `sf.yield_von_mises` as a NAMED limit state so the same constraint
    string works at L0 and at L3. The number is a beam-theory estimate and its
    fidelity tag says so — when FEA later answers the same metric, the higher
    fidelity overwrites it. That is the intended mechanism, not a collision.
    """
    def fn(values: dict, ctx) -> dict:
        mat = material_fn(values)
        E = float(mat["E_MPa"])
        fy = float(mat["yield_MPa"])
        sec = section_fn(values)
        L = float(values[length_key])
        res = load_fn(force_N, L, sec, E)
        stress = res["bending_stress_MPa"]
        out = {
            "section_area_mm2": sec["area_mm2"],
            "section_I_mm4": sec["I_mm4"],
            "section_Z_mm3": sec["Z_mm3"],
            "bending_stress_MPa": stress,
            "sf.yield_von_mises": (fy / stress) if stress > 0 else float("inf"),
            "P_cr_N": euler_buckling(E, sec["I_mm4"], L, K),
        }
        out["sf.elastic_buckling"] = (out["P_cr_N"] / force_N) if force_N else float("inf")
        for k in ("tip_deflection_mm", "midspan_deflection_mm"):
            if k in res:
                out["deflection_mm"] = res[k]
        if density_key:
            rho = float(values.get(density_key, mat.get("density_kg_m3", 0.0)))
            out["mass_kg"] = sec["area_mm2"] * L * 1e-9 * rho
        elif mat.get("density_kg_m3"):
            out["mass_kg"] = sec["area_mm2"] * L * 1e-9 * float(mat["density_kg_m3"])
        return out

    fn.__name__ = f"beam_screen[{section_fn.__name__},{load_fn.__name__}]"
    return fn


def machining_cost_model(rate_usd_per_hr: float, setup_min: float,
                         min_per_cm3_removed: float, stock_usd_per_kg: float,
                         stock_density_kg_m3: float, basis: str):
    """A transparent, stated-assumption cost model for `CostStage`.

    Every rate is an explicit argument and `basis` is carried into the metrics
    provenance, because the price book genuinely does not contain aluminium
    stock and inventing a number would violate the sourcing layer's own rule
    against unsourced prices. This is an ESTIMATE and labels itself as one.
    """
    def fn(values: dict, metrics: dict, ctx) -> dict:
        vol_mm3 = metrics.get("volume_mm3")
        bbox = [metrics.get("bbox_x_mm"), metrics.get("bbox_y_mm"),
                metrics.get("bbox_z_mm")]
        if vol_mm3 is None or any(b is None for b in bbox):
            return {}                      # missing input -> no fabricated cost
        stock_mm3 = bbox[0] * bbox[1] * bbox[2]
        removed_cm3 = max(0.0, (stock_mm3 - vol_mm3) / 1000.0)
        stock_kg = stock_mm3 * 1e-9 * stock_density_kg_m3
        material_usd = stock_kg * stock_usd_per_kg
        minutes = setup_min + removed_cm3 * min_per_cm3_removed
        machining_usd = minutes / 60.0 * rate_usd_per_hr
        return {"cost_usd": round(material_usd + machining_usd, 4),
                "cost_material_usd": round(material_usd, 4),
                "cost_machining_usd": round(machining_usd, 4),
                "machining_minutes": round(minutes, 2),
                "stock_mass_kg": round(stock_kg, 5)}

    fn.__name__ = "machining_cost_model"
    fn.basis = basis
    return fn
