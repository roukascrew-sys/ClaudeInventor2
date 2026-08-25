"""Mass properties and thrust-line validation (Phase 10).

Two named limit states, both pure rigid-body statics — no solver, no
approximation beyond the stated inputs:

  thrust_cg_alignment
      The resultant thrust vector must pass within `max_offset_mm` of the
      system centre of mass. A thruster whose line of action misses the CG
      applies a CONSTANT moment the pilot must fight for the whole flight;
      it is not a transient. This is the check that decides whether a
      propulsion layout is flyable at all, and no amount of structural
      margin substitutes for it.

  thrust_to_weight
      Resultant thrust magnitude over system weight must be >= `min_ratio`.
      Below 1.0 the vehicle cannot leave the ground; the margin above 1.0 is
      the entire control and acceleration authority available.

Why point masses are first-class here: on a wearable vehicle the dominant
mass is the PILOT, who is not geometry and never will be. Any CG that
ignores them is meaningless, so `point_masses` entries sit alongside the
assembly's real components and each one must carry its own `source` string —
this module invents no masses.

Offset is computed from the force/moment resultant, not by assuming the
thrusters are parallel:
    F   = sum(F_i)
    M_o = sum(r_i x F_i)                     (moment about the origin)
    M_c = M_o - r_cg x F                     (moment about the CG)
    d   = |M_c - (M_c . F_hat) F_hat| / |F|  (perpendicular miss distance)
The component of M_c along F is pure torque about the thrust axis (roll); it
cannot be removed by moving the line of action, so it is reported separately
rather than folded into the offset.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .assembly import AssemblyStore
from .log import ActionLog
from .parts import PartStore, _check_reason

G = 9.80665   # m/s^2, standard gravity (CGPM 1901 definition)


class MassPropsError(RuntimeError):
    pass


_CASE_KEYS = {"point_masses", "thrust", "limit_states", "pilot"}
_PM_KEYS = {"name", "mass_kg", "at_mm", "source"}
_THRUST_KEYS = {"name", "force_N", "at_mm"}
_ALLOWED_STATES = ("thrust_cg_alignment", "thrust_to_weight")


def _vec3(v, ctx: str) -> np.ndarray:
    if (not isinstance(v, (list, tuple)) or len(v) != 3
            or any(not isinstance(x, (int, float)) or isinstance(x, bool)
                   for x in v)):
        raise MassPropsError(f"{ctx}: expected [x, y, z] numbers, got {v!r}")
    return np.array([float(x) for x in v])


def _reject_extra(d: dict, allowed: set, ctx: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise MassPropsError(
            f"{ctx}: unexpected keys {sorted(extra)} — allowed: {sorted(allowed)}")


def validate_massprops_case(case: dict) -> None:
    if not isinstance(case, dict):
        raise MassPropsError("case must be a dict")
    _reject_extra(case, _CASE_KEYS, "case")

    pms = case.get("point_masses", [])
    if not isinstance(pms, list):
        raise MassPropsError("case.point_masses: list required")
    for i, pm in enumerate(pms):
        ctx = f"case.point_masses[{i}]"
        if not isinstance(pm, dict):
            raise MassPropsError(f"{ctx}: dict required")
        _reject_extra(pm, _PM_KEYS, ctx)
        if not isinstance(pm.get("name"), str) or not pm["name"].strip():
            raise MassPropsError(f"{ctx}.name: non-empty string required")
        m = pm.get("mass_kg")
        if not isinstance(m, (int, float)) or isinstance(m, bool) or m <= 0:
            raise MassPropsError(f"{ctx}.mass_kg: positive number required")
        _vec3(pm.get("at_mm"), f"{ctx}.at_mm")
        if not isinstance(pm.get("source"), str) or not pm["source"].strip():
            raise MassPropsError(
                f"{ctx}.source: required — cite where this mass comes from "
                f"(datasheet, weighing, stated assumption). This engine does "
                f"not accept unsourced masses; the CG is only as good as they are.")

    thrust = case.get("thrust")
    if not isinstance(thrust, list) or not thrust:
        raise MassPropsError("case.thrust: non-empty list required")
    for i, t in enumerate(thrust):
        ctx = f"case.thrust[{i}]"
        if not isinstance(t, dict):
            raise MassPropsError(f"{ctx}: dict required")
        _reject_extra(t, _THRUST_KEYS, ctx)
        if not isinstance(t.get("name"), str) or not t["name"].strip():
            raise MassPropsError(f"{ctx}.name: non-empty string required")
        f = _vec3(t.get("force_N"), f"{ctx}.force_N")
        if not np.any(f):
            raise MassPropsError(f"{ctx}.force_N: zero thrust is not a thruster")
        _vec3(t.get("at_mm"), f"{ctx}.at_mm")

    states = case.get("limit_states")
    if not isinstance(states, list) or not states:
        raise MassPropsError("case.limit_states: non-empty list required")
    for i, ls in enumerate(states):
        ctx = f"case.limit_states[{i}]"
        if not isinstance(ls, dict) or ls.get("name") not in _ALLOWED_STATES:
            raise MassPropsError(
                f"{ctx}.name: must be one of {_ALLOWED_STATES} — the gate "
                f"must name its limit state")
        if ls["name"] == "thrust_cg_alignment":
            _reject_extra(ls, {"name", "max_offset_mm"}, ctx)
            v = ls.get("max_offset_mm")
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise MassPropsError(f"{ctx}.max_offset_mm: positive number required")
        else:
            _reject_extra(ls, {"name", "min_ratio"}, ctx)
            v = ls.get("min_ratio")
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise MassPropsError(f"{ctx}.min_ratio: positive number required")

    pilot = case.get("pilot")
    if pilot is not None:
        _reject_extra(pilot, {"mass_kg", "max_cg_shift_mm"}, "case.pilot")
        for k in ("mass_kg", "max_cg_shift_mm"):
            v = pilot.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v <= 0:
                raise MassPropsError(f"case.pilot.{k}: positive number required")


class MassPropsTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore,
                 assemblies: AssemblyStore):
        self.root = Path(root)
        self.log = log
        self.parts = parts
        self.assemblies = assemblies

    def check_mass_properties(self, assembly_id: str, case: dict,
                              reason: str) -> dict:
        action_id = self.log.open_action(
            "validation", "check_mass_properties",
            geometry_version=str(assembly_id), reason=str(reason))
        try:
            _check_reason(reason)
            validate_massprops_case(case)
            spec = self.assemblies.get_assembly(assembly_id)

            items = []
            for i, comp in enumerate(spec["components"]):
                gid = comp["geometry_id"]
                part = self.parts.get_part(gid)
                props = part["properties"]
                mass = props.get("mass_kg_estimate")
                if mass is None:
                    raise MassPropsError(
                        f"components[{i}] {gid}: no mass_kg_estimate — its "
                        f"spec has no density_kg_m3, so it cannot contribute "
                        f"to a centre of mass. Add a density or declare it as "
                        f"a point mass with a source.")
                at = np.array([float(v) for v in comp.get("at", [0, 0, 0])])
                com = np.array([float(v) for v in props["center_of_mass_mm"]])
                items.append({
                    "kind": "component",
                    "name": comp.get("ref", f"c{i}"),
                    "geometry_id": gid,
                    "mass_kg": float(mass),
                    "at_mm": [round(v, 4) for v in (com + at)],
                    "source": f"geometry volume x spec density ({gid})",
                })
            for pm in case.get("point_masses", []):
                items.append({
                    "kind": "point_mass",
                    "name": pm["name"],
                    "geometry_id": None,
                    "mass_kg": float(pm["mass_kg"]),
                    "at_mm": [round(float(v), 4) for v in pm["at_mm"]],
                    "source": pm["source"],
                })

            total_mass = sum(it["mass_kg"] for it in items)
            if total_mass <= 0:
                raise MassPropsError("total system mass is zero")
            cg = sum(np.array(it["at_mm"]) * it["mass_kg"] for it in items) / total_mass

            F = np.zeros(3)
            M_o = np.zeros(3)
            for t in case["thrust"]:
                f = _vec3(t["force_N"], "thrust.force_N")
                r = _vec3(t["at_mm"], "thrust.at_mm") / 1000.0   # mm -> m
                F += f
                M_o += np.cross(r, f)
            F_mag = float(np.linalg.norm(F))
            if F_mag == 0:
                raise MassPropsError(
                    "thrust vectors cancel to zero resultant — there is no "
                    "thrust line to align")
            F_hat = F / F_mag

            M_c = M_o - np.cross(cg / 1000.0, F)          # moment about the CG
            along = float(np.dot(M_c, F_hat))              # roll about thrust axis
            M_perp = M_c - along * F_hat                   # the trimmable part
            M_perp_mag = float(np.linalg.norm(M_perp))
            offset_mm = M_perp_mag / F_mag * 1000.0

            weight_N = total_mass * G
            twr = F_mag / weight_N

            pilot = case.get("pilot")
            trim = None
            if pilot:
                # Checkable physical argument, not a rule of thumb: the pilot
                # trims a constant pitching moment by shifting the system CG.
                # Restoring moment = W * shift, so the shift needed is
                # M_perp / W. Compare against the shift they can actually
                # achieve by leaning.
                need_mm = M_perp_mag / weight_N * 1000.0
                trim = {
                    "pitch_moment_Nm": round(M_perp_mag, 4),
                    "required_cg_shift_mm": round(need_mm, 3),
                    "available_cg_shift_mm": float(pilot["max_cg_shift_mm"]),
                    "within_authority": bool(need_mm <= pilot["max_cg_shift_mm"]),
                }

            results = []
            for ls in case["limit_states"]:
                if ls["name"] == "thrust_cg_alignment":
                    lim = float(ls["max_offset_mm"])
                    ok = offset_mm <= lim
                    results.append({
                        "limit_state": "thrust_cg_alignment",
                        "measured_offset_mm": round(offset_mm, 4),
                        "max_offset_mm": lim,
                        "margin_mm": round(lim - offset_mm, 4),
                        "result": "pass" if ok else "fail",
                    })
                else:
                    lim = float(ls["min_ratio"])
                    ok = twr >= lim
                    results.append({
                        "limit_state": "thrust_to_weight",
                        "measured_ratio": round(twr, 5),
                        "min_ratio": lim,
                        "margin": round(twr - lim, 5),
                        "result": "pass" if ok else "fail",
                    })

            details = {
                "assembly_id": assembly_id,
                "total_mass_kg": round(total_mass, 6),
                "weight_N": round(weight_N, 4),
                "centre_of_mass_mm": [round(v, 4) for v in cg],
                "thrust_resultant_N": [round(v, 4) for v in F],
                "thrust_magnitude_N": round(F_mag, 4),
                "thrust_cg_offset_mm": round(offset_mm, 4),
                "roll_torque_about_thrust_axis_Nm": round(along, 4),
                "thrust_to_weight": round(twr, 5),
                "pilot_trim": trim,
                "mass_items": items,
                "limit_states": results,
            }
            failing = [r for r in results if r["result"] == "fail"]
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise

        if not failing:
            self.log.close_action(action_id, "pass", details=details)
        else:
            # non-linear gate: mode + magnitude before control returns
            modes = "; ".join(
                (f"thrust_cg_alignment: thrust line misses CG by "
                 f"{r['measured_offset_mm']:.1f} mm > {r['max_offset_mm']:.1f} mm allowed"
                 if r["limit_state"] == "thrust_cg_alignment" else
                 f"thrust_to_weight: {r['measured_ratio']:.3f} < "
                 f"{r['min_ratio']:.3f} required")
                for r in failing)
            self.log.close_action(action_id, "fail", details=details,
                                  failure_mode=modes)
        out = dict(details)
        out["result"] = "fail" if failing else "pass"
        out["failure_id"] = action_id if failing else None
        return out
