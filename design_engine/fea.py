"""Validation tools: CalculiX static FEA + safety-margin gate (Phase 4).

Gate contract (locked in the build plan): the gate is a **safety margin
against a named limit state**, never a pass percentage. v0 limit state:
von Mises stress vs. the material's yield strength —
SF = yield_MPa / max(von Mises), pass iff SF >= required_SF.

Non-linear revert contract: on a gate failure the failure record (mode +
magnitude + location) is written to the log BEFORE control returns to Design,
and the returned failure_id is what the next edit_part must reference via
addresses_failure_id — so the next attempt never repeats the failure blind.

Material values are caller-supplied and must carry a 'source' string (e.g.
"EN 10025-2 nominal, t<=16mm"). This module invents no material data.

Load application: 'force_total_N' is applied as the **consistent nodal load
vector** for uniform traction over the selected face's boundary triangles —
for straight-sided 6-node triangles that is corner nodes 0, each midside node
1/3 of its triangle's share (standard quadratic-element result; equal
splitting was tried first and produced a 3.5x spurious stress spike at the
load face in the analytic bar test). The selected face must be planar and
fully covered by boundary triangles whose nodes all lie in the selection.
"""

from __future__ import annotations

import ctypes
import json
import math
import re
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from .log import ActionLog
from .proc import REAP_GRACE_S, kill_tree, new_session_kwargs
from .mesh import (MeshError, describe_axis_options, mesh_step,
                   select_nodes)
from .parts import PartStore, _check_reason
from .geometry import build as build_solid
from .singularity import blend_resolution, classify_peak
from .submodel import (SubmodelError, coplanar_risk, converged,
                       cut_region, driven_nodes, plan,
                       refinement_ladder, solid_bounds,
                       )
from .interpolate import (InterpolationError, boundary_cards,
                          interpolate, read_frd)
from .fatigue import SNCurve, stress_range_from_ratio
from . import weld as _weld


class FeaError(RuntimeError):
    """A validation failure, optionally carrying the numbers that caused it.

    `details` exists because those numbers used to be formatted into the
    message and then thrown away. Action #346 recorded, as prose, "exceeded
    2400s on a submodel 0.2mm solve of 478512 nodes ... Peak memory at the
    kill: 4437 MB" — with `details_json` empty. So the single run that could
    have shown why a submodel solve costs more than the fitted cost model
    predicts was unfittable, and the knowledge base could not see it at all.

    A number the writer already held should never have to be parsed back out
    of a sentence. That is the mistake the calibration back-fill had to make
    once already, and once was enough. The message stays human-readable; the
    fields go to the log beside it.
    """

    def __init__(self, *args, details: dict | None = None):
        super().__init__(*args)
        self.details = dict(details) if details else None


_CASE_KEYS = {"material", "mesh", "constraints", "loads", "limit_state",
              "weld"}
# `weld` is OPTIONAL: a machined or bonded part has no heat-affected zone, and
# requiring an empty declaration from every case would be noise. _CASE_KEYS is
# the ALLOWED set; this is the REQUIRED one, and they were the same set until
# HAZ arrived.
_REQUIRED_CASE_KEYS = _CASE_KEYS - {"weld"}
_MATERIAL_KEYS = {"name", "E_MPa", "nu", "yield_MPa", "source",
                  "fatigue",
                  "service_temp_C", "yield_derate_curve", "E_derate_curve",
                  "derate_source",
                  # Only a modal solve needs it (the mass matrix). A static
                  # stress solve under force boundary conditions does not, and
                  # it was previously REJECTED here as "a geometry property,
                  # not an FEA one". That was right until *FREQUENCY existed.
                  "density_kg_m3"}
_MESH_KEYS = {"max_size_mm", "min_size_mm"}
_CONSTRAINT_KEYS = {"where", "dof"}
_LOAD_KEYS = {"where", "force_total_N"}
_LIMIT_KEYS = {"name", "required_SF", "excitation_hz", "harmonics",
               "required_cycles", "stress_ratio_R"}


def _reject_extra(d: dict, allowed: set, ctx: str) -> None:
    extra = set(d) - allowed
    if extra:
        raise FeaError(f"{ctx}: unexpected keys {sorted(extra)} — allowed: {sorted(allowed)}")


def _num(d: dict, key: str, ctx: str, *, lo=None, hi=None) -> float:
    val = d.get(key)
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise FeaError(f"{ctx}.{key}: expected number, got {val!r}")
    if lo is not None and val <= lo:
        raise FeaError(f"{ctx}.{key}: must be > {lo}, got {val}")
    if hi is not None and val >= hi:
        raise FeaError(f"{ctx}.{key}: must be < {hi}, got {val}")
    return float(val)


def _validate_derate_curve(curve, ctx: str) -> None:
    if not isinstance(curve, list) or len(curve) < 2:
        raise FeaError(f"{ctx}: must be a list of at least 2 [temp_C, factor] pairs")
    last_t = None
    for i, pt in enumerate(curve):
        if (not isinstance(pt, (list, tuple)) or len(pt) != 2
                or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                       for v in pt)):
            raise FeaError(f"{ctx}[{i}]: must be [temp_C, factor] numbers")
        t, k = float(pt[0]), float(pt[1])
        if not 0.0 <= k <= 1.0:
            raise FeaError(
                f"{ctx}[{i}]: factor {k} outside [0, 1] - a derating factor is "
                f"a fraction of the room-temperature value")
        if last_t is not None and t <= last_t:
            raise FeaError(
                f"{ctx}[{i}]: temperatures must strictly increase (got {t} "
                f"after {last_t})")
        last_t = t


def derate_factor(curve: list, temp_C: float, ctx: str) -> float:
    """Linearly interpolate a derating curve at temp_C.

    REFUSES to extrapolate. A material derating curve is measured data over a
    stated range; silently extending it past either end invents material
    behaviour, which is exactly the class of quiet-wrong answer this engine
    exists to prevent. Out of range is a spec error, not a clamp.
    """
    lo_t, hi_t = float(curve[0][0]), float(curve[-1][0])
    if not lo_t <= temp_C <= hi_t:
        raise FeaError(
            f"{ctx}: service temperature {temp_C} C is outside the derating "
            f"curve's range [{lo_t}, {hi_t}] C. This engine will not "
            f"extrapolate material data - supply a curve that covers the "
            f"service temperature, or state a temperature the data covers.")
    for (t0, k0), (t1, k1) in zip(curve, curve[1:]):
        if float(t0) <= temp_C <= float(t1):
            span = float(t1) - float(t0)
            if span == 0:
                return float(k0)
            frac = (temp_C - float(t0)) / span
            return float(k0) + frac * (float(k1) - float(k0))
    return float(curve[-1][1])


def effective_material(mat: dict) -> dict:
    """Room-temperature or temperature-derated E and yield, plus provenance.

    When the material carries a service_temp_C, the values actually used by
    the solver and the gate are the DERATED ones. A 6061-T6 frame rated at
    276 MPa at 20 C retains only 55% of that at 250 C (EN 1999-1-2 Table 1a),
    so gating a hot structure on its room-temperature yield overstates its
    strength by nearly 2x.
    """
    E = float(mat["E_MPa"])
    fy = float(mat["yield_MPa"])
    temp = mat.get("service_temp_C")
    out = {"service_temp_C": temp,
           "E_MPa_room": E, "yield_MPa_room": fy,
           "E_MPa_effective": E, "yield_MPa_effective": fy,
           "k_yield": 1.0, "k_E": 1.0,
           "derate_source": mat.get("derate_source")}
    if temp is None:
        return out
    if mat.get("yield_derate_curve"):
        k = derate_factor(mat["yield_derate_curve"], float(temp),
                          "case.material.yield_derate_curve")
        out["k_yield"] = k
        out["yield_MPa_effective"] = fy * k
    if mat.get("E_derate_curve"):
        k = derate_factor(mat["E_derate_curve"], float(temp),
                          "case.material.E_derate_curve")
        out["k_E"] = k
        out["E_MPa_effective"] = E * k
    return out


def validate_case(case: dict) -> None:
    if not isinstance(case, dict):
        raise FeaError("case must be a dict")
    _reject_extra(case, _CASE_KEYS, "case")
    missing = _REQUIRED_CASE_KEYS - set(case)
    if missing:
        raise FeaError(f"case: missing required sections {sorted(missing)}")

    mat = case["material"]
    _reject_extra(mat, _MATERIAL_KEYS, "case.material")
    if not isinstance(mat.get("name"), str) or not mat["name"]:
        raise FeaError("case.material.name: non-empty string required")
    if not isinstance(mat.get("source"), str) or not mat["source"].strip():
        raise FeaError(
            "case.material.source: required — cite where E/nu/yield come from; "
            "this engine does not accept unsourced material data")
    _num(mat, "E_MPa", "case.material", lo=0)
    _num(mat, "nu", "case.material", lo=0, hi=0.5)
    _num(mat, "yield_MPa", "case.material", lo=0)

    # --- temperature derating (optional, but all-or-nothing and sourced) ---
    has_curve = bool(mat.get("yield_derate_curve") or mat.get("E_derate_curve"))
    if mat.get("service_temp_C") is not None:
        _num(mat, "service_temp_C", "case.material", lo=-273.15)
        if not has_curve:
            raise FeaError(
                "case.material.service_temp_C given with no derating curve - "
                "a service temperature with no curve would silently be "
                "ignored and the part gated at its room-temperature strength")
    if has_curve:
        if mat.get("service_temp_C") is None:
            raise FeaError(
                "case.material: a derating curve was supplied with no "
                "service_temp_C - nothing would be derated")
        if not isinstance(mat.get("derate_source"), str) or not mat["derate_source"].strip():
            raise FeaError(
                "case.material.derate_source: required whenever a derating "
                "curve is given - cite the standard or test data the curve "
                "comes from; this engine does not accept unsourced derating")
        if mat.get("yield_derate_curve"):
            _validate_derate_curve(mat["yield_derate_curve"],
                                   "case.material.yield_derate_curve")
        if mat.get("E_derate_curve"):
            _validate_derate_curve(mat["E_derate_curve"],
                                   "case.material.E_derate_curve")

    _reject_extra(case["mesh"], _MESH_KEYS, "case.mesh")
    _num(case["mesh"], "max_size_mm", "case.mesh", lo=0)

    # Free vibration is the one analysis with nothing pushing on it: natural
    # frequencies come from stiffness, mass and restraint alone. Constraints
    # are still required — an unrestrained body has rigid-body modes at 0 Hz
    # and no meaningful separation margin.
    loads_optional = case.get("limit_state", {}).get("name") == "resonance_separation"
    for name, allowed in (("constraints", _CONSTRAINT_KEYS), ("loads", _LOAD_KEYS)):
        section = case[name]
        if not isinstance(section, list):
            raise FeaError(f"case.{name}: list required")
        if not section and not (name == "loads" and loads_optional):
            raise FeaError(f"case.{name}: non-empty list required")
        for i, item in enumerate(section):
            _reject_extra(item, allowed, f"case.{name}[{i}]")
            if not isinstance(item.get("where"), dict):
                raise FeaError(f"case.{name}[{i}].where: selector dict required")
    for i, c in enumerate(case["constraints"]):
        dof = c.get("dof")
        if (not isinstance(dof, list) or not dof
                or any(d not in (1, 2, 3) for d in dof)):
            raise FeaError(f"case.constraints[{i}].dof: list drawn from [1,2,3] required")
    for i, ld in enumerate(case["loads"]):
        f = ld.get("force_total_N")
        if (not isinstance(f, list) or len(f) != 3
                or any(not isinstance(x, (int, float)) or isinstance(x, bool) for x in f)):
            raise FeaError(f"case.loads[{i}].force_total_N: [Fx, Fy, Fz] required")
        if all(x == 0 for x in f):
            raise FeaError(f"case.loads[{i}].force_total_N: zero load is not a load case")

    ls = case["limit_state"]
    _reject_extra(ls, _LIMIT_KEYS, "case.limit_state")
    allowed_states = ("yield_von_mises", "elastic_buckling",
                      "thermal_derated_yield", "resonance_separation",
                      "fatigue_life")
    if ls.get("name") not in allowed_states:
        raise FeaError(
            f"case.limit_state.name: must be one of {allowed_states}, got "
            f"{ls.get('name')!r} — the gate must name its limit state")
    _num(ls, "required_SF", "case.limit_state", lo=0)
    if ls["name"] == "resonance_separation":
        # A separation is a fraction, not a stress ratio. required_SF = 0.2
        # means "every mode at least 20% clear of the excitation"; a value
        # above 1.0 would demand the nearest mode be more than double the
        # excitation away, which is almost certainly a units mix-up.
        if ls["required_SF"] > 1.0:
            raise FeaError(
                f"limit_state 'resonance_separation': required_SF is a "
                f"FRACTIONAL separation (0.2 = 20% clear of the excitation), "
                f"got {ls['required_SF']} - values above 1.0 are almost "
                f"always a stress-ratio habit applied to the wrong gate")
        if case["loads"]:
            raise FeaError(
                "limit_state 'resonance_separation': free vibration takes no "
                "loads. Natural frequencies depend on stiffness, mass and "
                "restraint only, so supplying case.loads here would imply a "
                "dependence the solve does not have")
    elif ls["name"] == "thermal_derated_yield":
        if mat.get("service_temp_C") is None or not mat.get("yield_derate_curve"):
            raise FeaError(
                "limit_state 'thermal_derated_yield' requires "
                "case.material.service_temp_C and "
                "case.material.yield_derate_curve - otherwise it is just "
                "yield_von_mises wearing a different name")


def check_rigid_body_modes(mesh: dict, constraint_sets: list,
                          constraints: list) -> dict:
    """Refuse a model that can move without straining.

    A static analysis needs every rigid-body mode removed, or the stiffness
    matrix is singular. CalculiX may still return numbers: the STRAINS (and so
    the stresses and the safety factor) come out right, but the DISPLACEMENTS
    carry an arbitrary rigid-body component that changes with solver, ordering
    or thread count. A displacement reported from such a model is not a
    physical prediction, and silently comparing it to a hand calculation is
    exactly the kind of false agreement this engine exists to prevent.

    Rigid-body motion of a point p is u(p) = t + omega x p, six parameters.
    Each constrained (node, dof) contributes one linear equation u(p)_dof = 0.
    Stacking them gives A (n x 6); the model is fully restrained iff
    rank(A) == 6. Coordinates are centred and scaled first so the rotation
    columns are numerically comparable to the translation ones.
    """
    coords = {int(tag): c for tag, c in zip(mesh["node_tags"], mesh["coords"])}
    pts = []
    for tags, spec in zip(constraint_sets, constraints):
        for tag in tags:
            for dof in spec["dof"]:
                pts.append((coords[int(tag)], int(dof)))
    if not pts:
        raise FeaError("case.constraints: no nodes constrained")

    origin = np.mean([p for p, _ in pts], axis=0)
    scale = max(float(np.max(np.abs([p - origin for p, _ in pts]))), 1e-9)
    rows = []
    for p, dof in pts:
        q = (np.asarray(p) - origin) / scale
        row = np.zeros(6)
        row[dof - 1] = 1.0
        # d(omega x q)/d(omega) for this dof component
        if dof == 1:
            row[3:] = [0.0, q[2], -q[1]]
        elif dof == 2:
            row[3:] = [-q[2], 0.0, q[0]]
        else:
            row[3:] = [q[1], -q[0], 0.0]
        rows.append(row)
    A = np.asarray(rows)
    rank = int(np.linalg.matrix_rank(A, tol=1e-8))
    if rank < 6:
        names = ["translation x", "translation y", "translation z",
                 "rotation x", "rotation y", "rotation z"]
        # null space of A = the rigid-body motions still available
        _, s, vh = np.linalg.svd(A)
        s_full = np.zeros(6)
        s_full[:len(s)] = s
        free = [names[i] for i in range(6)
                if abs(vh[i] @ vh[i]) > 0 and s_full[i] <= 1e-8]
        dofs_used = sorted({d for _, d in pts})
        raise FeaError(
            f"underconstrained_model: the constraints leave {6 - rank} "
            f"rigid-body mode(s) free (constraint rank {rank}/6; dofs "
            f"constrained anywhere: {dofs_used}). The stiffness matrix is "
            f"singular, so reported DISPLACEMENTS carry an arbitrary "
            f"rigid-body component and vary with solver/thread count "
            f"(stresses are still valid). Likely free: "
            f"{', '.join(free) if free else 'see constraint rank'}. Fix by "
            f"fully restraining one location (e.g. a pin at one support with "
            f"dof [1,2,3] and a roller at the other with dof [1,2]).")
    return {"constraint_rank": rank, "constrained_node_dofs": len(pts)}


def _consistent_face_loads(mesh: dict, selected_tags, force_total_N: list,
                           ctx: str, where: dict | None = None) -> dict[int, np.ndarray]:
    """Nodal forces for uniform traction on the selected planar face.

    Per straight-sided T6 triangle of area a carrying force F*(a/A): corners
    get 0, each midside gets 1/3. Returns {node_tag: [fx, fy, fz]}.
    """
    selected = {int(t) for t in selected_tags}
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    tris = [row for row in mesh["tri6"]
            if all(int(n) in selected for n in row)]
    if not tris:
        hint = ""
        axes = [w.get("axis") for w in (where.get("all", [where]) if where else [])
                if isinstance(w, dict) and w.get("axis")]
        for ax in dict.fromkeys(axes):
            hint += "; " + describe_axis_options(mesh, ax)
        raise FeaError(
            f"{ctx}: no boundary triangles lie fully inside the selection - a "
            f"load must cover a real boundary face, and 'at': 'min'/'max' is a "
            f"coordinate extremum, not a face (a protruding round feature makes "
            f"the extremum a curved tangent that carries no complete "
            f"triangle){hint}")
    force_total = np.asarray(force_total_N, dtype=float)
    areas = []
    for row in tris:
        p0, p1, p2 = (np.asarray(coords[int(row[k])]) for k in range(3))
        areas.append(0.5 * np.linalg.norm(np.cross(p1 - p0, p2 - p0)))
    total_area = float(sum(areas))
    if total_area <= 0:
        raise FeaError(f"{ctx}: selected face has zero area")
    nodal: dict[int, np.ndarray] = {}
    for row, a in zip(tris, areas):
        share = force_total * (a / total_area) / 3.0
        for mid in row[3:]:
            nodal[int(mid)] = nodal.get(int(mid), np.zeros(3)) + share
    assembled = np.sum(list(nodal.values()), axis=0)
    if not np.allclose(assembled, force_total, rtol=1e-9, atol=1e-9):
        raise FeaError(f"{ctx}: internal error — assembled load {assembled} "
                       f"!= requested {force_total}")
    return nodal


def _write_inp(path: Path, mesh: dict, case: dict,
               constraint_sets: list, load_sets: list,
               analysis: str = "static", n_modes: int = 4) -> None:
    mat = case["material"]
    lines = ["*HEADING", f"design-engine fea_static, material {mat['name']}",
             "*NODE, NSET=NALL"]
    for tag, (x, y, z) in zip(mesh["node_tags"], mesh["coords"]):
        lines.append(f"{tag}, {x:.9g}, {y:.9g}, {z:.9g}")
    lines.append("*ELEMENT, TYPE=C3D10, ELSET=EALL")
    for eid, row in enumerate(mesh["connectivity"], start=1):
        lines.append(f"{eid}, " + ", ".join(str(t) for t in row))
    for i, tags in enumerate(constraint_sets):
        lines.append(f"*NSET, NSET=FIX{i}")
        lines += [", ".join(str(t) for t in tags[j:j + 8])
                  for j in range(0, len(tags), 8)]
    # Derated modulus when the material states a service temperature. This
    # matters most for BUCKLING, where P_cr is directly proportional to E:
    # solving a hot column with room-temperature stiffness overstates its
    # critical load. For a single-material static stress solve under force
    # boundary conditions the stress field is essentially E-independent, so
    # derating E there changes displacements, not the yield gate.
    eff = effective_material(mat)
    lines += [f"*MATERIAL, NAME=MAT",
              "*ELASTIC",
              f"{eff['E_MPa_effective']:.9g}, {mat['nu']:.9g}"]
    if analysis == "frequency":
        # THE UNIT TRAP. CalculiX is unit-agnostic: it multiplies whatever
        # numbers it is given. This deck is mm / N / MPa, and the mass unit
        # CONSISTENT with those is the tonne, not the kilogram - because
        # 1 N = 1 tonne * 1 mm/s^2. Feeding kg/m^3 straight in yields
        # frequencies wrong by a factor of sqrt(1e12) = 1e6.
        #   kg/m^3 -> t/mm^3  is  x 1e-12   (1 kg = 1e-3 t, 1 m^3 = 1e9 mm^3)
        #   steel 7850 kg/m^3 -> 7.85e-9 t/mm^3
        # With that, eigenfrequencies come out in Hz.
        lines += ["*DENSITY", f"{float(mat['density_kg_m3']) * 1e-12:.9g}"]
    lines += ["*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
              "*STEP"]
    if analysis == "buckle":
        # Linear (eigenvalue) buckling. CalculiX returns load MULTIPLIERS on
        # the applied reference load, so the lowest positive factor IS the
        # safety factor against elastic instability for that load pattern.
        lines += ["*BUCKLE", str(int(n_modes))]
    elif analysis == "frequency":
        # Free vibration: no applied load, so the answer depends only on
        # stiffness, mass and restraint. STORAGE=0 keeps the .frd small; the
        # eigenfrequencies land in the .dat.
        lines += ["*FREQUENCY, STORAGE=0", str(int(n_modes))]
    else:
        lines.append("*STATIC")
    lines.append("*BOUNDARY")
    for i, c in enumerate(case["constraints"]):
        for dof in c["dof"]:
            lines.append(f"FIX{i}, {dof}, {dof}, 0.")
    if analysis != "frequency":
        lines.append("*CLOAD")
    for nodal in (load_sets if analysis != "frequency" else []):
        for tag in sorted(nodal):
            for dof, val in enumerate(nodal[tag], start=1):
                if val != 0:
                    lines.append(f"{tag}, {dof}, {val:.9g}")
    if analysis in ("buckle", "frequency"):
        # no RF/stress for an eigenvalue step: mode shapes only
        lines += ["*NODE FILE", "U", "*END STEP", ""]
    else:
        lines += ["*NODE FILE", "U, RF", "*EL FILE", "S", "*END STEP", ""]
    path.write_text("\n".join(lines), encoding="ascii")


# frd value fields are NOT fixed-width: this ccx build prints 3-digit
# exponents, so a negative value is 13 chars and values concatenate with no
# separator (e.g. '-6.40522E-005-6.57696E-0052.24554E-004'). The {2,3}
# exponent quantifier also accepts classic 2-digit-exponent builds, where
# positives carry a leading space so digits never abut an exponent.
_FRD_FLOAT = re.compile(r"[-+]?\d\.\d+E[-+]\d{2,3}")


def _parse_frd(frd_path: Path) -> dict[str, dict[int, list[float]]]:
    """Parse nodal result blocks (DISP, STRESS) from a CalculiX .frd file.

    Data lines: ' -1' + node id (10 chars) + concatenated float values.
    Returns {block_name: {node_tag: [values...]}} — last step wins.
    """
    blocks: dict[str, dict[int, list[float]]] = {}
    current: dict[int, list[float]] | None = None
    for line in frd_path.read_text(encoding="ascii", errors="replace").splitlines():
        code = line[:5].strip()
        if code == "-4":
            name = line.split()[1]  # ' -4  DISP        4    1' -> 'DISP'
            current = blocks.setdefault(name, {})
            current.clear()  # multiple steps: keep only the latest block
        elif code == "-3":
            current = None
        elif current is not None and line[:3] == " -1":
            node = int(line[3:13])
            current[node] = [float(v) for v in _FRD_FLOAT.findall(line[13:])]
    return blocks


def check_equilibrium(mesh: dict, forc: dict, constraint_sets: list,
                      load_sets: list, ctx: str, rel_tol: float = 1e-4) -> dict:
    """Verify the solution satisfies global force equilibrium.

    CalculiX's RF output gives the TOTAL nodal force at each node - applied
    load plus constraint reaction. For a converged static solution those must
    cancel over the whole model: sum over ALL nodes = 0.

    Summing over the constrained nodes only would be wrong: a node can be both
    loaded and constrained (the beam's midspan axial restraint shares nodes
    with the load patch), and its FORC already contains the applied term, so
    that sum double-counts. The global sum has no such overlap.

    This is a physics-level check on the SOLUTION, not on the input deck, so
    it catches a corrupted or partially-read result whatever the cause - which
    matters because three intermittent, non-reproducible bad results were seen
    in development (one turning a passing gate into a failing one) and neither
    the mesh nor the solver could be shown non-deterministic in isolation. A
    solve that does not balance is not a solve, and no safety factor is
    derived from it.
    """
    applied = np.zeros(3)
    for nodal in load_sets:
        for vec in nodal.values():
            applied += np.asarray(vec, dtype=float)

    net = np.zeros(3)
    for vals in forc.values():
        net += np.asarray(vals[:3], dtype=float)

    scale = max(float(np.linalg.norm(applied)), 1e-9)
    rel = float(np.linalg.norm(net)) / scale
    stats = {"applied_N": [round(v, 6) for v in applied.tolist()],
             "net_nodal_force_N": [round(v, 9) for v in net.tolist()],
             "residual_rel": round(rel, 12),
             "records": len(forc)}
    if not forc:
        raise FeaError(
            f"{ctx}: no reaction-force (RF) output in job.frd - equilibrium "
            f"could not be verified, so the result is not trusted.")
    if rel > rel_tol:
        raise FeaError(
            f"{ctx}: equilibrium_violation: nodal forces do not sum to zero. "
            f"applied={applied.tolist()} N, net={net.tolist()} N, relative "
            f"residual {rel:.3e} exceeds {rel_tol:.0e}. The solution is not "
            f"trustworthy and no safety factor is derived from it.")
    return stats


def _check_results_complete(mesh: dict, disp: dict, stress: dict,
                            run_dir: Path) -> None:
    """Refuse a result set that is not a complete, finite solution.

    The gate is computed from these numbers, so a partial or mis-parsed .frd
    must fail loudly rather than yield a plausible-looking safety factor. A
    correct run has exactly one entry per mesh node with 3 displacement and 6
    stress components, all finite; anything else means the file was truncated,
    the solver stopped early, or the fixed-format parse drifted.

    This guard exists because three intermittent, non-reproducible bad results
    were observed during development (a 5618 MPa peak, a displacement 2.5% off
    with the mesh unchanged, and a peak in the wrong location). The mesh and
    the solver were each proved deterministic in isolation, so the fault was
    never pinned down - this converts that failure mode from silent to loud.
    """
    n = len(mesh["node_tags"])
    if len(disp) != n or len(stress) != n:
        raise FeaError(
            f"incomplete_results: solver returned {len(disp)} displacement and "
            f"{len(stress)} stress records for a {n}-node mesh (expected {n} "
            f"of each). The .frd in {run_dir} is truncated or was read before "
            f"it was complete; the run is not trustworthy and no safety factor "
            f"is derived from it.")
    for name, table, width in (("DISP", disp, 3), ("STRESS", stress, 6)):
        for node, vals in table.items():
            if len(vals) < width:
                raise FeaError(
                    f"malformed_results: {name} record for node {node} has "
                    f"{len(vals)} components, expected {width} (parse drift in "
                    f"{run_dir}/job.frd)")
            if any(not math.isfinite(v) for v in vals[:width]):
                raise FeaError(
                    f"nonfinite_results: {name} record for node {node} contains "
                    f"NaN or Inf - the solve diverged ({run_dir}/job.frd)")


def von_mises(s: list[float]) -> float:
    # frd STRESS order: sxx, syy, szz, sxy, syz, szx
    sxx, syy, szz, sxy, syz, szx = s[:6]
    return math.sqrt(0.5 * ((sxx - syy) ** 2 + (syy - szz) ** 2 + (szz - sxx) ** 2)
                     + 3.0 * (sxy ** 2 + syz ** 2 + szx ** 2))


def _diagnostic_png(out_path: Path, mesh: dict, vm: dict[int, float],
                    title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    tag_list = [int(t) for t in mesh["node_tags"] if int(t) in vm]
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    xyz = np.array([coords[t] for t in tag_list])
    vals = np.array([vm[t] for t in tag_list])
    top = tag_list[int(np.argmax(vals))]

    fig = plt.figure(figsize=(7, 5), dpi=110)
    ax = fig.add_subplot(projection="3d")
    sc = ax.scatter(xyz[:, 0], xyz[:, 1], xyz[:, 2], c=vals, cmap="turbo", s=6)
    tx, ty, tz = coords[top]
    ax.scatter([tx], [ty], [tz], c="k", marker="x", s=80)
    ax.set_title(title + f"\nmax at node {top} ({tx:.1f}, {ty:.1f}, {tz:.1f}) mm",
                 fontsize=9)
    fig.colorbar(sc, label="von Mises (MPa), nodal", shrink=0.7)
    ax.set_xlabel("x"); ax.set_ylabel("y"); ax.set_zlabel("z")
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def parse_buckling_factors(dat_path: Path) -> list[float]:
    """Buckling factors (load multipliers) from a CalculiX .dat file.

    Each factor is the number the applied reference load must be multiplied
    by to reach elastic instability, so the lowest positive factor IS the
    safety factor against buckling for that load pattern.
    """
    if not dat_path.is_file():
        raise FeaError(
            f"solver produced no .dat file at {dat_path}; buckling factors "
            f"unavailable and no safety factor is derived")
    text = dat_path.read_text(encoding="ascii", errors="replace")
    if "BUCKLING" not in text.upper():
        raise FeaError(
            f"no buckling factor output in {dat_path} - the eigenvalue step "
            f"did not produce results")
    return [float(x) for x in
            re.findall(r"^\s+\d+\s+(-?[\d.]+E[+-]\d+)", text, re.M)]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_uint32),
                ("PageFaultCount", ctypes.c_uint32),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


def _peak_rss_mb(proc) -> float | None:
    """Peak working set of a finished subprocess, in MB, or None.

    PEAK, not final: a solver that transiently took 6 GB and then died shows
    almost nothing by the time it exits, and the peak is the number that
    explains the death. Windows tracks it for us, so no polling is needed.

    Returns None rather than guessing when unavailable (non-Windows, handle
    already closed, API failure). A missing measurement must read as missing —
    never as zero, which would poison any model fitted on it.

    KNOWN LIMIT: this is the DIRECT child's peak. If `cmd` is a launcher that
    runs the real work in a grandchild, the number describes the launcher and
    is quietly, plausibly wrong — measured 2026-08-28, this project's own
    `.venv/Scripts/python.exe` stub reports 4.1 MB for a script whose
    interpreter peaked at 266.1 MB. The solver is spawned as a direct
    executable (`tests/test_solver_timeout.py` pins that), so it is not
    affected; anything that grows a wrapper would be.
    """
    handle = getattr(proc, "_handle", None)
    if handle is None or not hasattr(ctypes, "windll"):
        return None
    try:
        counters = _PROCESS_MEMORY_COUNTERS()
        counters.cb = ctypes.sizeof(counters)
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.c_void_p(int(handle)), ctypes.byref(counters), counters.cb)
        if not ok:
            return None
        return round(int(counters.PeakWorkingSetSize) / (1024 * 1024), 1)
    except (OSError, AttributeError, ValueError):
        return None


class _SolverTimeout(subprocess.TimeoutExpired):
    """A solver deadline, carrying the peak memory it died holding.

    A subclass rather than a new exception type so that every existing
    `except subprocess.TimeoutExpired` keeps catching it, and a plain
    TimeoutExpired arriving from anywhere else is still handled — callers
    read the peak with `getattr`, never by assuming the subclass.
    """

    def __init__(self, cmd, timeout, peak_rss_mb: float | None = None):
        super().__init__(cmd, timeout)
        self.peak_rss_mb = peak_rss_mb


class _SolverRun:
    """What `subprocess.run` returns, plus the peak memory it cost."""

    def __init__(self, returncode: int, stdout: str, stderr: str,
                 peak_rss_mb: float | None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.peak_rss_mb = peak_rss_mb


def _run_solver(cmd, cwd, env, timeout_s: int) -> _SolverRun:
    """Run the solver and measure what it actually cost in memory.

    Popen rather than `subprocess.run` because the peak working set has to be
    read from the process handle, and `run()` closes it before returning.

    Why this exists: on 2026-08-27 a 504k-node solve died with an access
    violation, and nothing in the log said whether it had been near a memory
    limit. The learned cost model predicts TIME, so `affordable()` cheerfully
    returned `yes` for a solve that could not physically run. Time was never
    the binding constraint on that machine; memory was, and it was invisible.

    The timeout path kills the TREE and bounds the reap. `Popen.kill()` on
    Windows is `TerminateProcess` on one handle, and a post-kill
    `communicate()` with NO timeout blocks until every process still holding
    the inherited stdout/stderr pipe write handles has exited - so a single
    surviving grandchild holds this function open indefinitely, deadline or
    no deadline. That is measured, not theorised: against this function as it
    stood, a 40 s grandchild under a 3 s deadline surfaced its TimeoutExpired
    at 40.8 s — the overshoot IS the survivor's lifetime. It is the same shape
    that cost the Chrono bridge a full-suite run on 2026-08-28, whose reported
    ~1341 s overshoot is quoted from the incident, not reproduced.

    Today the CalculiX binaries spawn nothing - verified on the shipped
    2.23.0 win-x64 build, whose import tables contain no process-creation
    API at all (`tests/test_solver_timeout.py` pins that) - so this is
    insurance, not a live bug fix. The insurance is worth its four lines
    because the invariant belongs to the *binary*, not to this code: point
    `ccx_path` at a .bat wrapper, or ship a solver that shells out to a
    licence server, and the unbounded wait is back with nothing to catch it.
    """
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **new_session_kwargs())
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # Read the peak BEFORE killing: a timeout at 6 GB and a timeout at
        # 500 MB are different failures and want different remedies. It
        # travels out on the exception, because a number measured here and
        # then dropped is the same as not measuring it.
        peak = _peak_rss_mb(proc)
        kill_tree(proc)
        try:
            proc.communicate(timeout=REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            # Something outlived the tree kill and still holds the pipes.
            # Abandoning the reap loses the solver's output, which was
            # already lost - the run timed out. Waiting for it loses the
            # deadline too, which is the failure this guards against.
            pass
        raise _SolverTimeout(cmd, timeout_s, peak) from None
    return _SolverRun(proc.returncode, stdout or "", stderr or "",
                      _peak_rss_mb(proc))


def parse_eigenfrequencies(dat_path: Path) -> list[float]:
    """Natural frequencies in Hz from a *FREQUENCY step's .dat file.

    CalculiX prints, per mode:

        MODE NO   EIGENVALUE   (RAD/TIME)   (CYCLES/TIME)   IMAGINARY PART

    The Hz value is the CYCLES/TIME column. A mode with a NEGATIVE eigenvalue
    reports zero real frequency and a non-zero imaginary part, which means the
    structure is unrestrained in that direction — a rigid-body mode, not a
    vibration. Reporting those as "0 Hz modes" would be a quiet lie about a
    model that is not fit to answer the question, so they are refused.
    """
    if not dat_path.is_file():
        raise FeaError(
            f"solver produced no .dat file at {dat_path}; eigenfrequencies "
            f"unavailable and no separation margin is derived")
    text = dat_path.read_text(encoding="ascii", errors="replace")
    if "EIGENVALUE" not in text.upper():
        raise FeaError(
            f"no eigenvalue output in {dat_path} - the frequency step did not "
            f"produce results")

    # SECTION-AWARE, and it has to be. CalculiX follows the eigenvalue table
    # with PARTICIPATION FACTORS and EFFECTIVE MODAL MASS, whose rows also
    # begin with a mode number and carry six floats. Parsing the whole file
    # for "a row starting with an integer" picked those up as extra modes —
    # 12 where 6 were requested — and their columns, read as frequencies,
    # looked exactly like imaginary rigid-body modes.
    freqs, imaginary = [], []
    in_eigen = False
    for line in text.splitlines():
        upper = line.upper()
        if "E I G E N V A L U E" in upper:
            in_eigen = True
            continue
        if in_eigen and ("P A R T I C I P A T I O N" in upper
                         or "E F F E C T I V E" in upper
                         or "S T E P" in upper):
            in_eigen = False
        if not in_eigen:
            continue
        nums = _FRD_FLOAT.findall(line)
        parts = line.split()
        if len(nums) < 4 or not parts or not parts[0].isdigit():
            continue
        _eigen, _rad, cycles, imag = (float(nums[0]), float(nums[1]),
                                      float(nums[2]), float(nums[3]))
        if abs(imag) > 0:
            imaginary.append(abs(imag))
        else:
            freqs.append(cycles)

    if imaginary:
        raise FeaError(
            f"rigid_body_mode: {len(imaginary)} mode(s) came back with an "
            f"imaginary frequency (largest {max(imaginary):.4g} rad/time), "
            f"which means the model is unrestrained in that direction. A "
            f"natural frequency is only meaningful for a structure that is "
            f"actually held: check case.constraints before reading any "
            f"separation margin from this.")
    if not freqs:
        raise FeaError(f"no usable modes parsed from {dat_path}")
    return freqs


class _SolveInputs:
    """What the shared front half of every solve produces.

    Named rather than returned as a tuple because four call sites unpacking
    five values in the same order is exactly how a refactor silently swaps two
    of them.
    """

    __slots__ = ("part", "run_dir", "mesh", "constraint_sets", "rbm")

    def __init__(self, part, run_dir, mesh, constraint_sets, rbm):
        self.part = part
        self.run_dir = run_dir
        self.mesh = mesh
        self.constraint_sets = constraint_sets
        self.rbm = rbm

    @property
    def nodes(self) -> int:
        return int(len(self.mesh["node_tags"]))

    @property
    def elements(self) -> int:
        return int(len(self.mesh["connectivity"]))

    def provenance(self) -> dict:
        """The fields every analysis logs identically.

        Centralised so a new limit state cannot quietly omit one — the
        knowledge base reads these columns, and a missing `nodes` on one
        analysis is a silent hole in every model fitted from history.
        """
        return {"nodes": self.nodes, "elements": self.elements,
                "constraint_rank": self.rbm["constraint_rank"],
                "run_dir": str(self.run_dir)}


class ValidationTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore,
                 ccx_path: str | Path, threads: int | None = 1,
                 solve_timeout_s: int = 600,
                 buckle_max_attempts: int = 6):
        """threads: CPUs for the solver. Default 1 (single-threaded).

        ccx_MT.exe IS NOT TRUSTED BY DEFAULT. Measured 2026-08-24 on an
        identical buckling job, same mesh, same deck: single-threaded gave the
        correct answer 5/5 times, bit-identical (factor 3.5282 vs Euler
        3.5338); ccx_MT gave a WRONG answer 4/5 times (2.86, 2.06, 0.98, 2.08
        where 3.53 is correct), silently and with no error. That is a
        threading race producing wrong numbers, not a performance trade-off,
        and it is the most likely cause of this project's long-running
        intermittent corruption (byte-identical .inp -> different results).

        MT remains available via threads>1 for exploratory work where a
        re-checked answer is acceptable, but it must never be the default for
        a safety gate. Buckling always forces single-threaded regardless of
        this setting, because that is where MT was proven broken.
        """
        self.root = Path(root)
        self.run_root = self.root / "validation"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.parts = parts
        self.ccx_path = Path(ccx_path)
        self.threads = threads if threads is not None else 1
        self.solve_timeout_s = solve_timeout_s
        self.buckle_max_attempts = buckle_max_attempts
        mt = self.ccx_path.with_name(
            self.ccx_path.stem + "_MT" + self.ccx_path.suffix)
        self.ccx_mt_path = mt if mt.is_file() else None

    def _solver_command(self, force_single: bool = False
                        ) -> tuple[Path, dict, int]:
        """(binary, env, threads_used). See __init__ on why MT is opt-in."""
        env = dict(os.environ)
        if not force_single and self.ccx_mt_path is not None and self.threads > 1:
            env["OMP_NUM_THREADS"] = str(self.threads)
            return self.ccx_mt_path, env, self.threads
        env["OMP_NUM_THREADS"] = "1"
        return self.ccx_path, env, 1

    def _next_run_dir(self) -> Path:
        nums = [int(p.name[1:]) for p in self.run_root.glob("R[0-9]*") if p.is_dir()]
        run = self.run_root / f"R{(max(nums) + 1 if nums else 1):04d}"
        run.mkdir()
        return run

    # ------------------------------------------------------ shared pipeline
    # Every analysis walks the same road: open an action, check the request,
    # mesh, check the restraint, write a deck, solve, parse, gate, close the
    # action. Only the middle three differ. What follows is the road; each
    # fea_* method supplies its own deck fragment, parser and gate.

    @contextmanager
    def _action(self, name: str, geometry_id: str, reason: str):
        """Open a FRACAS action, and guarantee it is closed on the way out.

        The non-linear gate contract requires the failure record to be written
        BEFORE control returns to the caller, so the next attempt can reference
        it. Only the EXCEPTION path is handled here; the pass/fail-with-details
        close stays with the analysis, because only the analysis knows what its
        details are and whether it passed.

        A failing run may still have MEASURED something — how many nodes it got
        to, how long it burned, how much memory it took — and until 2026-09-02
        all of that was discarded, leaving `details_json` empty on every failed
        row. Any exception carrying a `details` dict now has it written to the
        log beside the message. `getattr` rather than an isinstance check, so a
        `MeshError` or anything else can opt in by setting the attribute
        without this module having to know the exception's type.
        """
        action_id = self.log.open_action(
            "validation", name, geometry_version=str(geometry_id),
            reason=str(reason))
        try:
            yield action_id
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", details=getattr(exc, "details", None),
                failure_mode=f"{type(exc).__name__}: {exc}")
            raise

    def _prepare(self, geometry_id: str, case: dict) -> _SolveInputs:
        """Validate the request, then mesh and check the restraint.

        Order is load-bearing and is pinned by test_validation_pipeline:
        the reason and the case are checked before the solver is looked for,
        and the solver is looked for before anything expensive is meshed. A
        malformed case must read as malformed even on a machine with no solver
        installed.
        """
        validate_case(case)
        if not self.ccx_path.is_file():
            raise FeaError(f"ccx solver not found at {self.ccx_path}")
        part = self.parts.get_part(geometry_id)
        run_dir = self._next_run_dir()
        m = mesh_step(part["step_file_path"], case["mesh"]["max_size_mm"],
                      case["mesh"].get("min_size_mm"))
        constraint_sets = [select_nodes(m, c["where"])
                           for c in case["constraints"]]
        rbm = check_rigid_body_modes(m, constraint_sets, case["constraints"])
        return _SolveInputs(part, run_dir, m, constraint_sets, rbm)

    #: Below this fraction of free memory the solve is expected to fit outright.
    #: Above it the solve still starts, but the run is flagged: the machine can
    #: satisfy the request only by paging, and paging does not fail, it crawls.
    #: Measured 2026-09-02: a 297,794-node global predicted at 5,091 MB (high
    #: end) against 5,484 MB free took 1282 s where the cost model's upper bound
    #: was 259 s - 4.95x - while a 338,446-node solve with room to spare took
    #: 209.95 s. More nodes, a sixth of the time, same solver and machine.
    _PAGING_RISK_FRACTION = 0.80

    def _knowledge(self):
        """The knowledge base over this engine's own log, or None.

        Lazy because it is only needed at solve time, and optional because a
        missing or unreadable history must not stop a solve that would have
        worked. Gating on absent knowledge would be worse than not gating.
        """
        if getattr(self, "_kb", None) is None:
            try:
                from .inventor.knowledge import KnowledgeBase  # noqa: PLC0415
                self._kb = KnowledgeBase(self.log.db_path)
            except Exception:                  # pragma: no cover - env-dependent
                self._kb = False
        return self._kb or None

    def _memory_gate(self, n_nodes: int, run_dir: Path, what: str) -> None:
        """Refuse a solve the machine cannot hold, BEFORE spending it.

        The engine has had an accurate memory model since 2026-08-27 and never
        consulted it. On 2026-09-02 that cost three global solves - 442,725,
        528,439 and 642,603 nodes - which ran 322, 332 and 530 s and then died
        at `0xC0000005` reaching for 7.1-9.1 GB on a machine with about 6 GB
        free. Roughly twenty minutes to discover something two already-written
        functions could have answered in milliseconds. This is the non-linear
        gate the project's own rules ask for: refuse before spending, and write
        the refusal down; never retry blind.

        The prediction is checked against AVAILABLE memory read live, not
        installed memory, and not a figure cached from earlier in the session.
        On this machine free memory moved between 2.7 GB and 6.1 GB over one
        afternoon while an unrelated job ran, so a stale reading is a wrong
        reading.

        Refuses only when the model's own HIGH end exceeds what is free, which
        is the case where a crash is predicted rather than merely possible. It
        does not refuse the paging-risk band - a slow answer is still an answer,
        and a gate that blocks work the machine can actually do would be traded
        away by the first person it inconvenienced. That band is recorded
        instead, so the cost model's outliers can be explained later rather
        than puzzled over.

        Silent when the history is too thin to fit a model, or when available
        memory cannot be read (this uses a Windows API and returns None
        elsewhere). An unknown is not a refusal.
        """
        kb = self._knowledge()
        if kb is None or not n_nodes:
            return
        pred = kb.predict_memory(n_nodes)
        avail = kb.available_memory_mb()
        if not pred or avail is None:
            return

        if pred["high_mb"] >= avail:
            raise FeaError(
                f"insufficient_memory: a {n_nodes:,}-node "
                f"{what or 'static'} solve is predicted to need "
                f"{pred['estimate_mb']:.0f} MB (high end {pred['high_mb']:.0f} "
                f"MB, from {pred['n']} real runs) and only {avail:.0f} MB is "
                f"free. Exceeding available memory does not slow CalculiX "
                f"down, it kills it outright, so this is refused before the "
                f"solve rather than after it. Coarsen case.mesh.max_size_mm "
                f"(subject to the Jacobian gate on the thinnest feature), "
                f"free memory, or submodel the region of interest.",
                details={
                    "failure_kind": "insufficient_memory",
                    "nodes": n_nodes,
                    "predicted_mb": pred["estimate_mb"],
                    "predicted_high_mb": pred["high_mb"],
                    "available_mb": avail,
                    "shortfall_mb": round(pred["high_mb"] - avail, 1),
                    "model_n": pred["n"],
                    "solve_kind": what or "static",
                    "run_dir": str(run_dir),
                    # No solver ran, so there is no timing to censor and none
                    # is invented. This row must never enter a cost fit.
                    "refused_before_solving": True,
                })

        if pred["high_mb"] >= avail * self._PAGING_RISK_FRACTION:
            (run_dir / "memory_warning.txt").write_text(
                f"paging risk: predicted high {pred['high_mb']:.0f} MB against "
                f"{avail:.0f} MB free ({pred['high_mb'] / avail * 100:.0f}% of "
                f"what is available). The solve was allowed to start; expect it "
                f"to take considerably longer than the cost model predicts.\n",
                encoding="utf-8")

    def _face_loads(self, m: dict, case: dict) -> list:
        return [_consistent_face_loads(
                    m, select_nodes(m, ld["where"]), ld["force_total_N"],
                    f"case.loads[{i}]", ld["where"])
                for i, ld in enumerate(case["loads"])]

    def _solve(self, run_dir: Path, n_nodes: int, *, force_single: bool = False,
               what: str = "", require_finished: bool = True):
        """Run CalculiX on the deck in `run_dir`. Returns (run, binary, threads,
        seconds).

        Both failure paths name what the caller needs to act on, and both name
        the peak memory, because an access violation at 6 GB and one at 500 MB
        want different remedies — and so do two timeouts at those sizes. Near
        the memory ceiling a timeout is a machine that is paging, and coarsening
        the mesh is the fix. At a few hundred MB it is not, and the remedy may
        not be known at all. A *SUBMODEL deck spends its budget in
        interpolation, ~88-182 ms per driven node and superlinear
        (a71901f as corrected by 3bc7450 - it terminates; it is not the infinite
        loop that commit first recorded), and the real 314-driven-node case
        still exceeded 900 s and was running at ~2 h, which that rate does not
        explain. Coarsening does cut the driven set - `driven_nodes` counts
        submodel nodes on the cut planes - so it is worth trying there; but
        3bc7450 records the residual as Unknown, and a remedy that is worth
        trying is not the same as one that is known to work. Without the number
        the log cannot even separate that case from the paging one.
        """
        binary, env, threads = self._solver_command(force_single=force_single)
        self._memory_gate(n_nodes, run_dir, what)
        t0 = time.time()
        try:
            proc = _run_solver([str(binary), "-i", "job"], run_dir, env,
                               self.solve_timeout_s)
        except subprocess.TimeoutExpired as timeout:
            peak = getattr(timeout, "peak_rss_mb", None)
            mem = f" Peak memory at the kill: {peak:.0f} MB." if peak else ""
            # The same numbers as FIELDS, not only as prose. A timed-out solve
            # is still a measurement of what that node count costs - arguably
            # the most valuable one, since it is the only kind that establishes
            # a LOWER bound above the budget. The fitted cost model is blind to
            # exactly these runs, which is why it under-predicts submodels.
            raise FeaError(
                f"solver_timeout: {binary.name} exceeded {self.solve_timeout_s}s "
                f"on {'a ' + what + ' solve of ' if what else ''}{n_nodes} nodes "
                f"using {threads} thread(s).{mem} Direct-solve cost grows steeply "
                f"with node count - coarsen case.mesh.max_size_mm (subject to "
                f"the Jacobian gate on the thinnest feature), or raise "
                f"ValidationTools(solve_timeout_s=...).",
                details={
                    "failure_kind": "solver_timeout",
                    "nodes": n_nodes,
                    # Censored, not observed: the solve was killed, so this is
                    # a lower bound on the true cost. Named so nothing fits it
                    # as if the run had finished at this figure.
                    "solve_seconds_at_kill": round(time.time() - t0, 3),
                    "solve_seconds_is_lower_bound": True,
                    "timeout_s": self.solve_timeout_s,
                    "peak_rss_mb": peak,
                    "peak_rss_is_lower_bound": True,
                    "solver_binary": binary.name,
                    "threads": threads,
                    "solve_kind": what or "static",
                    "run_dir": str(run_dir),
                }) from None
        seconds = time.time() - t0
        bad = proc.returncode != 0 or (require_finished
                                       and "Job finished" not in proc.stdout)
        if bad:
            (run_dir / "ccx_stdout.txt").write_text(proc.stdout, encoding="utf-8")
            mem = (f" peak memory {proc.peak_rss_mb:.0f} MB;"
                   if proc.peak_rss_mb else "")
            raise FeaError(f"solver_error: {binary.name} exit "
                           f"{proc.returncode};{mem} "
                           f"tail: {proc.stdout[-400:]!r}",
                           details={
                               "failure_kind": "solver_error",
                               "nodes": n_nodes,
                               # CENSORED, like a timeout, and for the same
                               # reason. This was first written as a completed
                               # measurement on the grounds that the process
                               # "ran to completion, badly" — which is wrong: a
                               # non-zero exit means ccx ABORTED, typically
                               # mid-factorisation, and a clean exit without
                               # "Job finished" means it stopped early too.
                               # Either way the solve never finished, so the
                               # time is a lower bound on what finishing costs.
                               #
                               # Caught on 2026-09-02 by the first real run
                               # under this code: three global solves died at
                               # 0xC0000005 having reached 442k-642k nodes in
                               # 322-530 s. Recorded as completed, they would
                               # have been the three largest meshes on file AND
                               # among the fastest for their size — teaching the
                               # cost model that huge meshes are cheap, which is
                               # precisely the pathology the censoring flag was
                               # added to prevent.
                               "solve_seconds_at_kill": round(seconds, 3),
                               "solve_seconds_is_lower_bound": True,
                               # It died reaching for memory it could not get,
                               # so the true requirement is above this figure.
                               "peak_rss_mb": proc.peak_rss_mb,
                               "peak_rss_is_lower_bound": True,
                               "returncode": proc.returncode,
                               "solver_binary": binary.name,
                               "threads": threads,
                               "solve_kind": what or "static",
                               "run_dir": str(run_dir),
                           })
        return proc, binary, threads, seconds

    def _run_buckle(self, m, case, constraint_sets, load_sets, run_dir,
                    n_modes: int):
        """One *BUCKLE solve; returns (factors, threads, binary_name, peak_mb).

        force_single: ccx_MT returns wrong eigenvalues ~4 times in 5 on an
        identical job (measured). Never multithread a buckling solve.

        require_finished=False: a *BUCKLE step does not print the "Job
        finished" banner that a *STATIC step does.
        """
        _write_inp(run_dir / "job.inp", m, case, constraint_sets, load_sets,
                   analysis="buckle", n_modes=n_modes)
        proc, binary, threads, _seconds = self._solve(
            run_dir, len(m["node_tags"]), force_single=True, what="buckling",
            require_finished=False)
        return (parse_buckling_factors(run_dir / "job.dat"), threads,
                binary.name, proc.peak_rss_mb)

    def fea_buckling(self, geometry_id: str, case: dict, reason: str,
                     n_modes: int = 3) -> dict:
        """Linear (eigenvalue) buckling against the elastic_buckling limit state.

        Verified against the Euler closed form for a fixed-pinned prismatic
        column to 0.16% (tests/test_buckling.py).

        SELF-CHECK, and the reason this runs the solve TWICE: a buckling
        factor is a load multiplier, so halving the reference load must
        double every factor. When this project's intermittent CalculiX corruption
        hits the pre-buckling static solve, the geometric stiffness is lost
        and every factor collapses toward 1.0 - a plausible-looking number
        that does NOT scale. The scaling check catches exactly that, and
        because it tests the SOLUTION rather than the input it catches other
        corruption modes too.
        """
        with self._action("fea_buckling", geometry_id, reason) as action_id:
            _check_reason(reason)
            validate_case(case)
            if case["limit_state"]["name"] != "elastic_buckling":
                raise FeaError(
                    f"fea_buckling requires limit_state 'elastic_buckling', got "
                    f"{case['limit_state']['name']!r}")
            si = self._prepare(geometry_id, case)
            m, run_dir = si.mesh, si.run_dir
            constraint_sets, rbm = si.constraint_sets, si.rbm

            def loads_scaled(k):
                return [_consistent_face_loads(
                            m, select_nodes(m, ld["where"]),
                            [v * k for v in ld["force_total_N"]],
                            f"case.loads[{i}]", ld["where"])
                        for i, ld in enumerate(case["loads"])]

            # BOUNDED RETRY against a known upstream defect, not a fix for it.
            # This project has an unresolved intermittent CalculiX corruption
            # (proved solver-side: byte-identical .inp -> different results).
            # It hits buckling solves relatively often and has a distinctive
            # signature - the geometric stiffness is lost and every factor
            # collapses toward 1.0, which does NOT scale with the reference
            # load. The scaling check below is the acceptance criterion; a
            # rejected attempt is retried because the corruption is transient.
            # If every attempt is rejected the run REFUSES rather than
            # reporting a number, so a corrupted result can never reach a gate.
            t0 = time.time()
            attempts = []
            lowest = lowest_2x = ratio = None
            factors = []
            for attempt in range(1, self.buckle_max_attempts + 1):
                base_dir = run_dir / f"ref{attempt}"
                base_dir.mkdir()
                f1, threads, binary, peak_mb = self._run_buckle(
                    m, case, constraint_sets, loads_scaled(1.0), base_dir,
                    n_modes)
                check_dir = run_dir / f"scaled{attempt}"
                check_dir.mkdir()
                # scale DOWN, not up: halving keeps the check load further
                # BELOW the critical load. Doubling can push an already-near
                # -critical reference load deep into the supercritical range
                # where the linearised eigenvalue solve degrades - measured
                # directly (a case whose 1x factor was correct at 0.6656 gave
                # a meaningless 0.983 at 2x, every attempt).
                f2, _, _, _ = self._run_buckle(
                    m, case, constraint_sets, loads_scaled(0.5), check_dir,
                    n_modes)

                pos1 = [f for f in f1 if f > 0]
                pos2 = [f for f in f2 if f > 0]
                if not pos1 or not pos2:
                    attempts.append({"attempt": attempt, "rejected":
                                     "no positive factor"})
                    continue
                lo1, lo2 = min(pos1), min(pos2)
                r = lo2 / lo1
                attempts.append({"attempt": attempt,
                                 "factor_at_1x": round(lo1, 6),
                                 "factor_at_half": round(lo2, 6),
                                 "ratio": round(r, 6),
                                 "accepted": abs(r - 2.0) <= 0.04})
                if abs(r - 2.0) <= 0.04:
                    factors, lowest, lowest_2x, ratio = f1, lo1, lo2, r
                    break
            solve_s = time.time() - t0

            if lowest is None:
                raise FeaError(
                    f"buckling_scaling_violation: no attempt in "
                    f"{self.buckle_max_attempts} produced a load-multiplier "
                    f"that scales correctly (halving the reference load must "
                    f"double every factor, ratio 2.0). Attempts: {attempts}. "
                    f"This is the signature of the project's intermittent "
                    f"CalculiX corruption losing the geometric stiffness, "
                    f"which collapses every factor toward 1.0. No safety "
                    f"factor is derived from this run.")

            required = case["limit_state"]["required_SF"]
            details = {
                "limit_state": "elastic_buckling",
                "required_SF": required,
                "safety_factor": round(lowest, 6),
                # P_cr is directly proportional to E, so a derated modulus
                # lowers the critical load proportionally. Recorded here so a
                # hot buckling factor is never mistaken for a cold one.
                "thermal_derating": effective_material(case["material"]),
                "buckling_factors": [round(f, 6) for f in factors],
                "scaling_check": {"factor_at_1x": round(lowest, 6),
                                  "factor_at_half_load": round(lowest_2x, 6),
                                  "ratio": round(ratio, 6),
                                  "required_ratio": 2.0,
                                  "attempts": attempts},
                **si.provenance(),
                "solver_binary": binary, "solver_threads": threads,
                "solve_seconds": round(solve_s, 2),
                # Buckling previously ran through a raw subprocess.run and so
                # carried no memory measurement at all, unlike every other
                # analysis. Routing it through the shared solve closes that.
                "peak_rss_mb": peak_mb,
                "artifacts": [],
            }
            passed = lowest >= required

        if passed:
            self.log.close_action(action_id, "pass", details=details)
        else:
            self.log.close_action(
                action_id, "fail", details=details,
                failure_mode=(
                    f"elastic_buckling: factor {lowest:.4f} < required "
                    f"{required} (the applied load reaches "
                    f"{100.0 / lowest:.1f}% of the elastic critical load)"))
        return {"result": "pass" if passed else "fail",
                "action_id": action_id,
                "failure_id": None if passed else action_id,
                "safety_factor": lowest, "buckling_factors": factors,
                "scaling_ratio": ratio}

    def fea_fatigue(self, geometry_id: str, case: dict, reason: str) -> dict:
        """Fatigue life against the fatigue_life limit state.

        Runs a static stress solve, then evaluates life on a SOURCED S-N curve.
        The static run is logged in its own right and this action links to it,
        so the stress the life was computed from is always recoverable.

        WHY THIS MATTERS MORE THAN THE STATIC CHECK. Life goes as the stress
        range to the power of the curve slope — around 3.4 for welded
        aluminium — so a 20% error in peak stress is a factor of 1.8 in life.
        Static strength tolerates a sloppy peak; fatigue does not.

        Which is why a peak sitting on a geometric singularity is REFUSED here
        rather than warned about. At a re-entrant corner the peak is unbounded,
        so the computed life tends to zero as the mesh refines: the answer
        would not be conservative, it would be meaningless.
        """
        ls = case.get("limit_state", {})
        if ls.get("name") != "fatigue_life":
            raise FeaError(
                f"fea_fatigue requires limit_state 'fatigue_life', got "
                f"{ls.get('name')!r}")
        for key in ("required_cycles", "stress_ratio_R"):
            if key not in ls:
                raise FeaError(
                    f"case.limit_state.{key}: required. A fatigue life is "
                    f"meaningless without the number of cycles demanded and "
                    f"the shape of the cycle (R = sigma_min/sigma_max); this "
                    f"engine will not assume either")
        fat = case["material"].get("fatigue")
        if not isinstance(fat, dict):
            raise FeaError(
                "case.material.fatigue: required — an S-N curve with its "
                "detail category, slope, endurance limit and source. Static "
                "material data says nothing about life under cycling")

        # Build the stress solve. The fatigue gate is applied here, not there,
        # so the sub-run is asked only for a stress field.
        static_case = json.loads(json.dumps(
            {k: v for k, v in case.items() if k != "limit_state"}))
        static_case["material"].pop("fatigue", None)
        static_case["limit_state"] = {
            "name": "thermal_derated_yield" if (
                case["material"].get("service_temp_C") is not None
                and case["material"].get("yield_derate_curve"))
            else "yield_von_mises",
            "required_SF": 1.0}

        with self._action("fea_fatigue", geometry_id, reason) as action_id:
            _check_reason(reason)
            curve = SNCurve(
                name=fat.get("name", case["material"]["name"]),
                detail_category_MPa=fat["detail_category_MPa"],
                slope_m=fat["slope_m"],
                source=fat.get("source", ""),
                reference_cycles=fat.get("reference_cycles", 2e6),
                endurance_limit_MPa=fat["endurance_limit_MPa"]
                if "endurance_limit_MPa" in fat else ...,
                valid_cycles=tuple(fat.get("valid_cycles", (1e4, 1e8))))

            static = self.fea_static(
                geometry_id, static_case,
                reason=f"stress field for fatigue life: {reason}")
            self.log._conn.execute(
                "UPDATE actions SET linked_parent_id = ? WHERE id = ?",
                (static["action_id"], action_id))
            self.log._conn.commit()

            sing = static.get("singularity") or {}
            if sing.get("verdict") == "singular":
                raise FeaError(
                    f"singular_peak: the peak stress sits on a geometric "
                    f"singularity, so a fatigue life computed from it is "
                    f"meaningless rather than conservative — life goes as "
                    f"range**-{curve.slope_m:g}, and an unbounded range drives "
                    f"predicted life to zero as the mesh refines. "
                    f"{sing.get('reason', '')}")

            peak = float(static["max_von_mises_MPa"])
            R = float(ls["stress_ratio_R"])
            rng = stress_range_from_ratio(peak, R)
            required_cycles = float(ls["required_cycles"])
            allowable = curve.allowable_cycles(rng)
            sf = (math.inf if allowable == math.inf
                  else allowable / required_cycles)
            required_sf = float(ls.get("required_SF", 1.0))

            details = {
                "limit_state": "fatigue_life",
                "required_SF": required_sf,
                "safety_factor": ("inf" if sf == math.inf else round(sf, 6)),
                "peak_von_mises_MPa": round(peak, 6),
                "stress_ratio_R": R,
                "stress_range_MPa": round(rng, 6),
                "required_cycles": required_cycles,
                "allowable_cycles": ("inf" if allowable == math.inf
                                     else round(allowable, 1)),
                "allowable_range_at_required_life_MPa": round(
                    curve.allowable_range(required_cycles), 6),
                "curve": curve.to_dict(),
                "static_action_id": static["action_id"],
                "singularity": sing,
                "material": case["material"],
            }
            passed = sf >= required_sf

        if passed:
            self.log.close_action(action_id, "pass", details=details)
        else:
            self.log.close_action(
                action_id, "fail", details=details,
                failure_mode=(
                    f"fatigue_life: {details['allowable_cycles']} allowable "
                    f"cycles at a {rng:.1f} MPa range against "
                    f"{required_cycles:.4g} required (SF "
                    f"{details['safety_factor']} < {required_sf}). The "
                    f"structure survives "
                    f"{curve.allowable_range(required_cycles):.1f} MPa at that "
                    f"life"))
        return {"result": "pass" if passed else "fail",
                "action_id": action_id,
                "failure_id": None if passed else action_id,
                "safety_factor": sf,
                "stress_range_MPa": rng,
                "allowable_cycles": allowable,
                "required_cycles": required_cycles,
                "allowable_range_at_required_life_MPa":
                    curve.allowable_range(required_cycles),
                "static_action_id": static["action_id"]}

    def fea_modal(self, geometry_id: str, case: dict, reason: str,
                  n_modes: int = 10) -> dict:
        """Natural frequencies against the resonance_separation limit state.

        WHY THIS EXISTS. The jetpack frame carries four turbines at 98,000 rpm
        — about 1633 Hz — bolted to a structure whose natural frequencies had
        never been computed. If a mode sits near that, the frame is driven at
        resonance and the stress amplitude a static solve reports is wrong by
        whatever the damping-limited amplification happens to be. Every static
        result on that frame rests on the unexamined assumption that no mode
        is nearby.

        THE GATE. This limit state is a SEPARATION, not a stress ratio, so
        `required_SF` is read as the required fractional separation: 0.2 means
        every mode must sit at least 20% away from the excitation and from
        each harmonic checked. The reported `safety_factor` is the achieved
        separation, so the engine's "SF >= required_SF" contract still holds
        and reads the same way in the log.

        Harmonics matter and are not optional: a rotor excites at its running
        speed AND at multiples of it, so a mode at 2x the shaft frequency is
        just as dangerous as one at 1x. `case.limit_state.harmonics` says how
        many to check.
        """
        with self._action("fea_modal", geometry_id, reason) as action_id:
            _check_reason(reason)
            validate_case(case)
            ls = case["limit_state"]
            if ls["name"] != "resonance_separation":
                raise FeaError(
                    f"fea_modal requires limit_state 'resonance_separation', "
                    f"got {ls['name']!r}")
            if "excitation_hz" not in ls:
                raise FeaError(
                    "case.limit_state.excitation_hz: required — a separation "
                    "margin is meaningless without the frequency being "
                    "separated FROM. State the driving frequency (for a rotor, "
                    "rpm / 60) rather than leaving it implied.")
            exc_hz = _num(ls, "excitation_hz", "case.limit_state", lo=0)
            harmonics = int(ls.get("harmonics", 1))
            if harmonics < 1:
                raise FeaError("case.limit_state.harmonics: must be >= 1")
            if case["material"].get("density_kg_m3") is None:
                raise FeaError(
                    "case.material.density_kg_m3: required for a modal solve. "
                    "A natural frequency is sqrt(stiffness/mass) and there is "
                    "no mass matrix without density — this engine will not "
                    "assume one.")
            _num(case["material"], "density_kg_m3", "case.material", lo=0)

            si = self._prepare(geometry_id, case)
            m, run_dir = si.mesh, si.run_dir

            _write_inp(run_dir / "job.inp", m, case, si.constraint_sets, [],
                       analysis="frequency", n_modes=n_modes)
            # force_single: an eigenvalue solve is never multithreaded here,
            # for the same reason buckling is not. See ccx_MT.
            # require_finished=False: a *FREQUENCY step does not print the
            # "Job finished" banner a *STATIC step does.
            proc, binary, threads, solve_s = self._solve(
                run_dir, si.nodes, force_single=True, what="modal",
                require_finished=False)

            freqs = parse_eigenfrequencies(run_dir / "job.dat")
            checked = [exc_hz * k for k in range(1, harmonics + 1)]
            clashes, separation = [], float("inf")
            for h_i, f_exc in enumerate(checked, start=1):
                for mode_i, f in enumerate(freqs, start=1):
                    sep = abs(f - f_exc) / f_exc
                    if sep < separation:
                        separation = sep
                    if sep < float(ls["required_SF"]):
                        clashes.append({
                            "mode": mode_i, "mode_hz": round(f, 4),
                            "harmonic": h_i,
                            "excitation_hz": round(f_exc, 4),
                            "separation": round(sep, 6)})

            required = float(ls["required_SF"])
            details = {
                "limit_state": "resonance_separation",
                "required_SF": required,
                "safety_factor": round(separation, 6),
                "excitation_hz": exc_hz,
                "harmonics_checked": [round(f, 4) for f in checked],
                "mode_frequencies_hz": [round(f, 4) for f in freqs],
                "n_modes": len(freqs),
                "clashes": clashes,
                "material": case["material"],
                "thermal_derating": effective_material(case["material"]),
                **si.provenance(),
                "solver_binary": binary.name, "solver_threads": threads,
                "solve_seconds": round(solve_s, 2),
                "peak_rss_mb": proc.peak_rss_mb,
                "artifacts": [],
            }
            passed = not clashes

        if passed:
            self.log.close_action(action_id, "pass", details=details)
        else:
            worst = min(clashes, key=lambda c: c["separation"])
            self.log.close_action(
                action_id, "fail", details=details,
                failure_mode=(
                    f"resonance_separation: mode {worst['mode']} at "
                    f"{worst['mode_hz']:.1f} Hz is {worst['separation'] * 100:.1f}% "
                    f"from harmonic {worst['harmonic']} of the excitation "
                    f"({worst['excitation_hz']:.1f} Hz), inside the required "
                    f"{required * 100:.0f}% separation"))
        return {"result": "pass" if passed else "fail",
                "action_id": action_id,
                "failure_id": None if passed else action_id,
                "safety_factor": separation,
                "mode_frequencies_hz": freqs,
                "excitation_hz": exc_hz,
                "harmonics_checked": checked,
                "clashes": clashes}

    def fea_static(self, geometry_id: str, case: dict, reason: str) -> dict:
        with self._action("fea_static", geometry_id, reason) as action_id:
            _check_reason(reason)
            si = self._prepare(geometry_id, case)
            m, run_dir, part = si.mesh, si.run_dir, si.part
            constraint_sets, rbm = si.constraint_sets, si.rbm
            load_sets = self._face_loads(m, case)
            _write_inp(run_dir / "job.inp", m, case, constraint_sets, load_sets)
            proc, binary, threads_used, solve_s = self._solve(
                run_dir, si.nodes)

            blocks = _parse_frd(run_dir / "job.frd")
            stress = blocks.get("STRESS")
            disp = blocks.get("DISP")
            if not stress or not disp:
                raise FeaError("solver produced no STRESS/DISP results in job.frd")
            _check_results_complete(m, disp, stress, run_dir)
            equilibrium = check_equilibrium(
                m, blocks.get("FORC", {}), constraint_sets, load_sets,
                f"run {run_dir.name}")

            vm = {n: von_mises(s) for n, s in stress.items()}
            coords = {int(t): c for t, c in zip(m["node_tags"], m["coords"])}
            max_node = max(vm, key=vm.get)
            max_vm = vm[max_node]
            vm_vals = np.array(list(vm.values()))
            median_vm = float(np.median(vm_vals))
            # Outlier guard: the gate divides yield by max nodal stress, so a
            # single bad node sets the safety factor. Comparing the max with
            # the 99.9th percentile separates a physical peak from a numerical
            # one. Calibrated on 24 real runs: physically sound models sat at
            # 1.00-1.20, models with an artificial constraint singularity at
            # 1.95-2.12, and one non-reproducible garbage result at 5.47.
            # Advisory, not a refusal: a spurious HIGH stress lowers SF, so it
            # can only cause a false FAIL, never an unsafe pass.
            p999 = float(np.percentile(vm_vals, 99.9))
            outlier_ratio = (max_vm / p999) if p999 > 0 else float("inf")
            outlier_warning = None
            if outlier_ratio > 2.0:
                outlier_warning = (
                    f"stress_outlier: peak {max_vm:.1f} MPa is "
                    f"{outlier_ratio:.1f}x the 99.9th percentile "
                    f"({p999:.1f} MPa). Likely a numerical artifact or an "
                    f"unconverged singularity (sharp re-entrant corner, "
                    f"point-like restraint) rather than a physical peak. The "
                    f"safety factor derived from it is pessimistic, not "
                    f"unsafe - but do not treat it as a converged stress.")

            # Geometric singularity check. The outlier ratio above compares the
            # peak against the bulk field, which catches a peak DECOUPLED from
            # its surroundings - a constraint singularity. A peak sitting on a
            # sharp re-entrant corner is fed BY the surrounding field, so the
            # ratio stays low while the stress is still unbounded. P0047@v1
            # read 1.633 here, comfortably "clean", with its peak 1.28 mm off a
            # 270-degree corner. Two different questions; both must be asked.
            peak_xyz = [float(v) for v in coords[int(max_node)]]
            try:
                singularity = classify_peak(
                    build_solid(part["spec"]).val(), peak_xyz,
                    case["mesh"]["max_size_mm"])
            except Exception as exc:        # noqa: BLE001 - never break a solve
                singularity = {"verdict": "unknown", "singular_edges": 0,
                               "reason": f"not analysed: {type(exc).__name__}: {exc}"}
            # A clean geometry is not the same as a resolved one. Blending a
            # sharp corner makes the peak FINITE, so classify_peak stops
            # objecting - correctly. But a 1 mm blend meshed at 3 mm has a
            # third of an element across it, and the peak still measures the
            # mesh. Fixing the geometry moves the problem from "unbounded" to
            # "under-resolved", and the check that caught the first does not
            # catch the second.
            radii = [float(f["radius"]) for f in part["spec"].get("features", [])
                     if f.get("op") == "fillet" and f.get("radius")]
            blend = (blend_resolution(min(radii),
                                      float(case["mesh"]["max_size_mm"]))
                     if radii else None)

            max_disp = max(math.sqrt(sum(v ** 2 for v in u[:3]))
                           for u in disp.values())

            mat = case["material"]
            ls_name = case["limit_state"]["name"]
            eff = effective_material(mat)
            # The allowable is the DERATED yield whenever the material states
            # a service temperature. Gating a hot part on its room-temperature
            # yield is the silent-overstrength failure this limit state exists
            # to close.
            allowable = (eff["yield_MPa_effective"]
                         if ls_name == "thermal_derated_yield"
                         else mat["yield_MPa"])

            # HEAT-AFFECTED ZONE. Welding a 6xxx aluminium alloy destroys the
            # T6 temper locally, and the softened zone is a different material
            # with its own reduced proof strength. Applied AFTER thermal
            # derating because the two are independent: a hot weld is softened
            # by both, and taking only the worse of them would overstate the
            # strength of a structure that is simultaneously welded and hot.
            welds = _weld.from_case(case.get("weld"))
            haz = welds.allowable_at(peak_xyz, allowable)
            allowable = haz["allowable_MPa"]

            sf = math.inf if max_vm == 0 else allowable / max_vm
            required = case["limit_state"]["required_SF"]

            png_rel = f"validation/{run_dir.name}/von_mises.png"
            _allow_note = (
                f"derated yield {eff['yield_MPa_effective']:.1f} MPa "
                f"(k={eff['k_yield']:.3f} @ {eff['service_temp_C']} C, "
                f"room {mat['yield_MPa']} MPa)"
                if ls_name == "thermal_derated_yield"
                else f"yield {mat['yield_MPa']} MPa")
            if haz["in_haz"]:
                _allow_note += (f", HAZ x{haz['factor']:g} -> "
                                f"{allowable:.1f} MPa")
            _diagnostic_png(
                self.root / png_rel, m, vm,
                f"{geometry_id} — {ls_name}: SF={sf:.3f} "
                f"(required {required}) — max vM {max_vm:.1f} MPa vs "
                f"{_allow_note} [{mat['name']}]")

            details = {
                "material": mat,
                "limit_state": ls_name,
                "required_SF": required,
                "safety_factor": round(sf, 6) if sf != math.inf else "inf",
                "allowable_MPa": round(allowable, 6),
                "thermal_derating": eff,
                "weld_haz": haz,
                "welds": welds.to_dict(),
                "max_von_mises_MPa": round(max_vm, 6),
                "median_von_mises_MPa": round(median_vm, 6),
                "p99_9_von_mises_MPa": round(p999, 6),
                "stress_outlier_ratio": round(outlier_ratio, 4),
                "stress_outlier_warning": outlier_warning,
                "singularity": singularity,
                "blend_resolution": blend,
                "max_von_mises_node": int(max_node),
                "max_von_mises_at_mm": [round(float(v), 3) for v in coords[int(max_node)]],
                "max_displacement_mm": round(max_disp, 9),
                **si.provenance(),
                "result_records": {"disp": len(disp), "stress": len(stress)},
                "equilibrium": equilibrium,
                "solver_binary": binary.name,
                "solver_threads": threads_used,
                "solve_seconds": round(solve_s, 2),
                # None, never 0, when the platform cannot report it: a missing
                # measurement must stay distinguishable from a real zero, or a
                # model fitted on this column silently learns that big solves
                # are free.
                "peak_rss_mb": proc.peak_rss_mb,
                "artifacts": [png_rel],
            }
            passed = sf >= required
        if passed:
            self.log.close_action(action_id, "pass", details=details)
        else:
            # non-linear gate: mode + magnitude recorded BEFORE control returns
            self.log.close_action(
                action_id, "fail", details=details,
                failure_mode=(
                    f"{ls_name}: SF={sf:.3f} < required {required} "
                    f"(max vM {max_vm:.1f} MPa vs allowable {allowable:.1f} MPa "
                    f"[{_allow_note}] "
                    f"at node {int(max_node)} {details['max_von_mises_at_mm']} mm)"))
        return {
            "result": "pass" if passed else "fail",
            "action_id": action_id,
            "failure_id": None if passed else action_id,
            "safety_factor": sf,
            # The allowable and the outlier advisory used to live only in the
            # log details, so a caller printing the return value saw
            # "allowable=None" and never saw the warning at all. That is how a
            # constraint-singularity artifact (P0028@v1: peak 183.6 MPa sitting
            # exactly on the constraint patch corner, outlier ratio 2.48) can
            # be mistaken for a real structural failure. Surfaced here.
            "allowable_MPa": allowable,
            "max_von_mises_MPa": max_vm,
            "median_von_mises_MPa": median_vm,
            "p99_9_von_mises_MPa": p999,
            "stress_outlier_ratio": outlier_ratio,
            "stress_outlier_warning": outlier_warning,
            "singularity": singularity,
            "max_von_mises_at_mm": details["max_von_mises_at_mm"],
            "max_displacement_mm": max_disp,
            "thermal_derating": eff,
            "artifacts": [png_rel],
            "run_dir": str(run_dir),
        }

    # ------------------------------------------------------------ submodel
    def _write_submodel_inp(self, path: Path, mesh: dict, case: dict,
                            fragment: dict) -> None:
        """A submodel deck: same material and elements, different restraint.

        There is no `*BOUNDARY` fixity and no `*CLOAD` here, and that is not
        an omission. The driven cut surface carries the entire effect of the
        rest of the structure - the loads that produced those displacements,
        and the constraints that reacted them. Re-applying either would count
        it twice.
        """
        mat = case["material"]
        eff = effective_material(mat)
        lines = ["*HEADING",
                 f"design-engine fea_submodel, material {mat['name']}",
                 "*NODE, NSET=NALL"]
        for tag, (x, y, z) in zip(mesh["node_tags"], mesh["coords"]):
            lines.append(f"{tag}, {x:.9g}, {y:.9g}, {z:.9g}")
        lines.append("*ELEMENT, TYPE=C3D10, ELSET=EALL")
        for eid, row in enumerate(mesh["connectivity"], start=1):
            lines.append(f"{eid}, " + ", ".join(str(t) for t in row))
        lines += fragment["before_step"]
        lines += ["*MATERIAL, NAME=MAT", "*ELASTIC",
                  f"{eff['E_MPa_effective']:.9g}, {mat['nu']:.9g}",
                  "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
                  "*STEP", "*STATIC"]
        lines += fragment["inside_step"]
        lines += ["*NODE FILE", "U", "*EL FILE", "S", "*END STEP", ""]
        path.write_text("\n".join(lines), encoding="ascii")

    def _region_conflicts(self, gm: dict, case: dict, region) -> list:
        """Loads or constraints that fall inside the submodel region.

        Their effect is ALREADY carried by the driven cut surface, because the
        global solve that produced those displacements had them applied.
        Applying them again inside the submodel counts them twice, and the
        result would still be reported as a converged local stress.
        """
        conflicts = []
        for kind in ("loads", "constraints"):
            for i, entry in enumerate(case.get(kind, [])):
                tags = set(int(t) for t in select_nodes(gm, entry["where"]))
                inside = sum(1 for t, xyz in zip(gm["node_tags"], gm["coords"])
                             if int(t) in tags and region.contains(xyz))
                if inside:
                    conflicts.append({"kind": kind, "index": i,
                                      "nodes_inside": inside})
        return conflicts

    def fea_submodel(self, geometry_id: str, case: dict, global_result: dict,
                     reason: str, *, feature_mm: float,
                     standoff_elements: float, standoff_source: str = "",
                     centre=None, start_mesh_mm: float | None = None,
                     max_projection_mm: float = 0.0,
                     ladder_steps: int = 3,
                     ladder_factor: float = 2.0, tol_pct: float = 5.0) -> dict:
        """Re-solve a small region around the peak, driven by a global solve.

        Consumes an existing `fea_static` result rather than re-running it.
        The expensive global solve is what found the peak in the first place,
        and running it again to produce the same number is precisely the waste
        this feature exists to avoid.

        Answers "has the peak stopped moving", NOT "does the part pass". The
        limit-state gate stays with `fea_static`; giving one number two gates
        is how a result ends up with two different verdicts.
        """
        _check_reason(reason)
        with self._action("fea_submodel", geometry_id, reason) as action_id:
            for key in ("run_dir", "max_von_mises_at_mm"):
                if key not in global_result:
                    raise FeaError(
                        f"global_result.{key} is required - pass the dict "
                        f"returned by fea_static for this geometry")

            # The gate, before anything is cut or meshed. A peak on a
            # geometric singularity has no finite value to converge to, so
            # this whole procedure would spend the solver budget producing a
            # number that rises with every rung.
            # Defaults to the global peak, which is the usual question. An
            # explicit centre is allowed because the location worth resolving
            # is not always the global maximum - a fillet you care about can
            # sit well below a peak you have already explained.
            peak_xyz = [float(v) for v in
                        (centre if centre is not None
                         else global_result["max_von_mises_at_mm"])]
            part = self.parts.get_part(geometry_id)
            solid = build_solid(part["spec"])

            # Classify the peak against the geometry being REFINED, not
            # against whatever the global solve happened to report.
            #
            # These are frequently not the same part, and that is the normal
            # way to use submodelling rather than an edge case: the global
            # model carries simplified geometry and the submodel carries the
            # detail. Here it is forced - a 1 mm blend gives gmsh slivers at a
            # coarse global size and 690k elements at a fine one, so the
            # blend-clean frame cannot be globally meshed on this machine at
            # all, while the sharp one solves in seconds.
            #
            # Reading the global's verdict would refuse exactly that workflow:
            # the sharp global IS singular, and it does not matter, because
            # DISPLACEMENTS converge at a re-entrant corner even though
            # stresses do not. Driving a blended submodel from a sharp
            # global's displacement field is sound; refining a sharp
            # submodel's stress is not. The gate has to look at the second.
            local_class = classify_peak(
                solid.val() if hasattr(solid, "val") else solid,
                peak_xyz, case["mesh"]["max_size_mm"])
            region = plan(local_class, peak_xyz,
                          feature_mm, standoff_elements,
                          case["mesh"]["max_size_mm"], source=standoff_source)

            # Only now touch the global results. The gate above is cheap -
            # build the solid, classify one peak - and reading the field is
            # not: the jetpack global .frd is 394 MB. Cheap refusals first is
            # the same ordering `_prepare` uses, for the same reason.
            global_frd = Path(global_result["run_dir"]) / "job.frd"
            if not global_frd.is_file():
                raise FeaError(
                    f"global results not found at {global_frd}. The submodel "
                    f"is driven by them; without the .frd there is nothing to "
                    f"interpolate from")

            # Read ONCE. Every rung drives points from the same field, so
            # re-reading per rung would cost more than the solving.
            try:
                field = read_frd(global_frd)
            except InterpolationError as exc:
                raise FeaError(f"global results unreadable: {exc}") from exc

            cut, cut_report = cut_region(solid, region)
            risks = coplanar_risk(solid_bounds(solid), region)

            # Check the boundary conditions against the GLOBAL FIELD's own
            # nodes, not a fresh mesh of the part.
            #
            # This used to call mesh_step on the whole submodel part, which
            # defeated the entire feature: if the part could be globally
            # meshed there would be no reason to submodel it. On the jetpack
            # frame it failed exactly there - 20 of 64,373 elements degenerate
            # at 5 mm - after the gate, the cut and the region had all
            # succeeded.
            #
            # The global field is also the RIGHT mesh to ask. Those are the
            # nodes the boundary conditions were actually applied to, so a
            # selector evaluated against them answers the real question:
            # does an applied load or restraint fall inside the region whose
            # effect the driven surface already carries?
            gm = {"node_tags": np.asarray(sorted(field["coords"]),
                                          dtype=np.int64),
                  "coords": np.asarray([field["coords"][t] for t
                                        in sorted(field["coords"])],
                                       dtype=float)}
            conflicts = self._region_conflicts(gm, case, region)
            if conflicts:
                raise FeaError(
                    f"the region contains applied boundary conditions "
                    f"{conflicts}. Their effect is already carried by the "
                    f"driven cut surface, so applying them again would count "
                    f"them twice. Submodels containing a load or a restraint "
                    f"are not supported yet - move the region, or solve the "
                    f"part directly")

            # The ladder starts where the CALLER says, defaulting to the
            # global size. Tying it to the global was wrong: a submodel exists
            # to be finer than the global, and on the jetpack junction the
            # global 5 mm is far too coarse for a 1 mm blend - the first rung
            # failed the Jacobian gate with 8 of 4,547 elements degenerate
            # before any of the refinement happened. The region is small, so
            # it can afford elements the whole part never could.
            ladder = refinement_ladder(
                float(start_mesh_mm or case["mesh"]["max_size_mm"]), region,
                steps=ladder_steps, factor=ladder_factor)

            # MEASURED 2026-08-28. ccx 2.23 win-x64 *SUBMODEL interpolation is
            # PROHIBITIVELY SLOW, and superlinear in the number of driven
            # nodes. Holding one C3D10 submodel mesh fixed and varying only
            # the driven set:
            #
            #     272 driven   ->   23.8 s   ( 88 ms per driven node)
            #    1082 driven   ->  197.1 s   (182 ms per driven node)
            #
            # An earlier reading of this as an infinite loop in createtet was
            # WRONG: both of those terminate. What is true is that on the real
            # geometry (120 mm bar, 6 mm global, 314 driven) it did not finish
            # inside a 900 s solver timeout and was still running after ~2 h
            # wall, so there it is non-terminating for any practical budget.
            # Why that case is so much worse than 1082 driven nodes on the
            # probe is NOT established - driven count does not explain it.
            #
            # Two things ruled out: the global element type is irrelevant
            # (C3D4 and C3D10 globals behave identically), and driving only
            # corner nodes is ~8x faster but is NOT a fix - unconstrained
            # midside nodes on a cut boundary let it bulge.
            #
            # Everything above this line is validated and cheap: the gate, the
            # region, the cut, the driven-node classification, the boundary-
            # condition conflict check. Only the interpolation is unusable,
            # and the honest options are to write it here (locate the global
            # C3D10 containing each driven node and evaluate its shape
            # functions) or to find a ccx configuration that is affordable.
            #
            # Since 2026-08-30 the interpolation is ours, so none of that
            # applies any more: the deck states the displacements outright
            # instead of asking ccx to work them out. Measured on the real
            # global solve at 3.5 ms per driven node against ccx's 88-182 ms,
            # and exact on a linear field to 7.1e-15 mm.
            #
            # Read ONCE, outside the ladder. The jetpack global .frd is 394 MB
            # and every rung drives points from the same field; re-reading it
            # per rung would cost more than the solving.
            peaks, rungs = [], []
            binary = threads = None
            # Bound before the try so the handler below can never raise
            # NameError over the top of a real solver failure. An empty ladder
            # cannot reach the handler at all, but a diagnostic that destroys
            # the diagnosis is not a trade worth one saved line.
            mm = None
            # A rung that dies still measured what it reached, and the
            # rungs BEFORE it completed normally. Discarding both is how
            # action #346 came to record a 2400 s overrun with an empty
            # details_json, leaving the one run that shows a submodel
            # costing >3.4x its node-count-implied price unfittable.
            try:
                for mm in ladder:
                    run_dir = self._next_run_dir()
                    step_path = run_dir / "submodel.step"
                    import cadquery as cq              # noqa: PLC0415
                    cq.exporters.export(cut, str(step_path))
                    # GRADED, not uniform. The coarsest rung sets the size at
                    # the cut; each rung refines only a ball around the feature.
                    #
                    # Uniform refinement forces a choice the submodel should not
                    # have to make: stand the cut far enough off that the peak is
                    # not a boundary artefact, OR resolve the feature - node count
                    # goes as the cube of region size, so a 12 mm box at 0.2 mm
                    # reached 478,512 nodes and timed out at 4,437 MB. Grading
                    # buys both. Measured on a plain bar: 0.8 mm everywhere is
                    # 303,191 nodes, 0.8 mm inside an 8 mm ball is 40,928 - the
                    # same resolution where it matters at a seventh of the cost.
                    m = mesh_step(str(step_path), ladder[0], None,
                                  refine={"centre": peak_xyz,
                                          "radius": max(2.0 * feature_mm,
                                                        6.0 * mm),
                                          "size": mm})
                    driven = driven_nodes(m, region)
                    by_tag = {int(t): xyz for t, xyz
                              in zip(m["node_tags"], m["coords"])}
                    interp = interpolate(field=field,
                                         points={t: by_tag[t] for t in driven},
                                         max_projection_mm=max_projection_mm)
                    if interp["outside"]:
                        # A driven node outside every global element would have to
                        # be extrapolated, and nothing downstream could tell. The
                        # cut is supposed to lie INSIDE the global solve, so this
                        # means the region and the global model disagree.
                        sample = ", ".join(
                            str(o["point"]) for o in interp["outside"][:3])
                        raise FeaError(
                            f"{len(interp['outside'])} of {len(driven)} driven "
                            f"nodes at {mm:g} mm fell outside every element of "
                            f"the global mesh, e.g. {sample}. They cannot be "
                            f"driven without extrapolating, and nothing "
                            f"downstream could tell an invented displacement "
                            f"from a real one, so this is refused rather than "
                            f"guessed. Either the region is not fully inside the "
                            f"global model, or the submodel's surface lies "
                            f"outside the global tet mesh's faceted boundary")
                    frag = {"before_step": [],
                            "inside_step": boundary_cards(interp["values"])}
                    self._write_submodel_inp(run_dir / "job.inp", m, case, frag)
                    _, binary, threads, solve_s = self._solve(
                        run_dir, len(m["node_tags"]), what=f"submodel {mm:g}mm")
                    blocks = _parse_frd(run_dir / "job.frd")
                    stress = blocks.get("STRESS", {})
                    if not stress:
                        raise FeaError(
                            f"the submodel at {mm:g} mm produced no STRESS "
                            f"results ({run_dir}/job.frd)")
                    vm_by_node = {n: von_mises(sv) for n, sv in stress.items()}
                    peak_node = max(vm_by_node, key=vm_by_node.get)
                    peak_vm = vm_by_node[peak_node]

                    # WHERE the peak sits decides what it means.
                    #
                    # A peak ON a driven node is not automatically wrong - over a
                    # region with a monotonic field, like mid-span of a bent bar,
                    # the largest stress legitimately sits on a cut face because
                    # the region simply contains no local maximum. What it is
                    # NEVER able to be is evidence about the feature, because the
                    # value is set by the imposed displacements rather than by the
                    # geometry being studied.
                    #
                    # And when the interpolation feeding those displacements is
                    # slightly off, the error becomes a local concentration that
                    # SHARPENS with refinement - so it climbs exactly like a
                    # singularity while the geometry is provably blend-clean.
                    # Measured on the jetpack junction 2026-09-01: 91.09 MPa at
                    # 0.8 mm rising to 228.30 MPa at 0.4 mm, +150.6%, with every
                    # top peak on a driven node 0.000 mm from a cut face and 6 mm
                    # from the junction being studied.
                    #
                    # So it is recorded, not raised, and it BLOCKS a claim of
                    # convergence: a settled number read off the driven boundary
                    # is still not a number about the feature.
                    pk = by_tag.get(int(peak_node))
                    on_driven = int(peak_node) in set(int(t) for t in driven)
                    peaks.append(peak_vm)
                    rungs.append({"mesh_mm": mm, "coarse_mm": ladder[0], "nodes": len(m["node_tags"]),
                                  "projected_nodes": len(interp["projected"]),
                                  "worst_projection_mm": (
                                      max((x["gap_mm"] for x in interp["projected"]),
                                          default=0.0)),
                                  "elements": len(m["connectivity"]),
                                  "driven_nodes": len(driven),
                                  "max_von_mises_MPa": round(peak_vm, 6),
                                  "peak_at_mm": ([round(float(v), 4) for v in pk]
                                                 if pk is not None else None),
                                  "peak_on_driven_boundary": on_driven,
                                  "solve_seconds": round(solve_s, 3),
                                  "run_dir": str(run_dir)})
            except Exception as exc:
                # Merge, never overwrite: _solve has already attached the
                # failing rung's own nodes / seconds / peak memory, and those
                # are the fields the cost model actually needs.
                exc.details = {**(getattr(exc, 'details', None) or {}),
                               'partial': True,
                               'rungs': rungs,
                               'ladder': ladder,
                               'failed_rung_mm': mm,
                               'completed_rungs': len(rungs),
                               'region': region.to_dict()}
                raise

            verdict = converged(peaks, tol_pct)
            boundary_rungs = [r["mesh_mm"] for r in rungs
                              if r["peak_on_driven_boundary"]]
            if boundary_rungs:
                # Convergence is not claimable while the peak is being read
                # off the driven surface, however steady the number looks.
                verdict = dict(verdict, converged=False, reason=(
                    f"the peak sits on a DRIVEN node at "
                    f"{len(boundary_rungs)} of {len(rungs)} rungs "
                    f"({boundary_rungs}), so it is set by the imposed "
                    f"displacements rather than by the feature. "
                    f"{verdict.get('reason', '')} Enlarge the region so the "
                    f"cut stands further off from the stress being read - "
                    f"standoff_elements is currently "
                    f"{region.standoff_elements:g}"))
            coarse = global_result.get("max_von_mises_MPa")
            details = {
                "region": region.to_dict(),
                "singularity_of_refined_geometry": local_class,
                "cut": cut_report,
                "coplanar_risk": risks,
                "global_run_dir": str(global_result["run_dir"]),
                "global_max_von_mises_MPa": coarse,
                "rungs": rungs,
                "convergence": verdict,
                "solver_binary": binary.name if binary else None,
                "threads": threads,
            }
            if coarse:
                # How much the coarse global solve understated the peak. This
                # is the number the whole exercise exists to produce.
                details["submodel_vs_global_ratio"] = round(
                    peaks[-1] / float(coarse), 4)

            # The log's vocabulary is deliberately binary, and this closes
            # FAIL when the peak has not settled. That is the study failing to
            # establish convergence, NOT the part failing its gate - the gate
            # belongs to fea_static and giving one number two verdicts is how
            # a result ends up with two answers. The failure_mode says so in
            # terms, because "fail" on a row about a passing part is exactly
            # the sort of thing a later reader misreads.
            #
            # This line previously closed with "unknown", which close_action
            # refuses. It had never executed: the *SUBMODEL refusal returned
            # before reaching it, so removing that refusal is what exposed it.
            converged_ok = bool(verdict.get("converged"))
            self.log.close_action(
                action_id, "pass" if converged_ok else "fail",
                details=details,
                failure_mode=(None if converged_ok else
                              f"convergence_not_established: "
                              f"{verdict['reason']}. This is the refinement "
                              f"study failing to converge, not the part "
                              f"failing a limit state"))
            if global_result.get("action_id") is not None:
                self.log._conn.execute(
                    "UPDATE actions SET linked_parent_id = ? WHERE id = ?",
                    (global_result["action_id"], action_id))
                self.log._conn.commit()
            return {"action_id": action_id, "geometry_id": geometry_id,
                    **details}
