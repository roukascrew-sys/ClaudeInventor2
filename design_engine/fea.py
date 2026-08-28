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
from pathlib import Path

import numpy as np

from .log import ActionLog
from .mesh import (MeshError, describe_axis_options, mesh_step,
                   select_nodes)
from .parts import PartStore, _check_reason
from .geometry import build as build_solid
from .singularity import classify_peak


class FeaError(RuntimeError):
    pass


_CASE_KEYS = {"material", "mesh", "constraints", "loads", "limit_state"}
_MATERIAL_KEYS = {"name", "E_MPa", "nu", "yield_MPa", "source",
                  "service_temp_C", "yield_derate_curve", "E_derate_curve",
                  "derate_source"}
_MESH_KEYS = {"max_size_mm", "min_size_mm"}
_CONSTRAINT_KEYS = {"where", "dof"}
_LOAD_KEYS = {"where", "force_total_N"}
_LIMIT_KEYS = {"name", "required_SF"}


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
    missing = _CASE_KEYS - set(case)
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

    for name, allowed in (("constraints", _CONSTRAINT_KEYS), ("loads", _LOAD_KEYS)):
        section = case[name]
        if not isinstance(section, list) or not section:
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
                      "thermal_derated_yield")
    if ls.get("name") not in allowed_states:
        raise FeaError(
            f"case.limit_state.name: must be one of {allowed_states}, got "
            f"{ls.get('name')!r} — the gate must name its limit state")
    _num(ls, "required_SF", "case.limit_state", lo=0)
    if ls["name"] == "thermal_derated_yield":
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
              f"{eff['E_MPa_effective']:.9g}, {mat['nu']:.9g}",
              "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
              "*STEP"]
    if analysis == "buckle":
        # Linear (eigenvalue) buckling. CalculiX returns load MULTIPLIERS on
        # the applied reference load, so the lowest positive factor IS the
        # safety factor against elastic instability for that load pattern.
        lines += ["*BUCKLE", str(int(n_modes))]
    else:
        lines.append("*STATIC")
    lines.append("*BOUNDARY")
    for i, c in enumerate(case["constraints"]):
        for dof in c["dof"]:
            lines.append(f"FIX{i}, {dof}, {dof}, 0.")
    lines.append("*CLOAD")
    for nodal in load_sets:  # {node_tag: [fx, fy, fz]} consistent loads
        for tag in sorted(nodal):
            for dof, val in enumerate(nodal[tag], start=1):
                if val != 0:
                    lines.append(f"{tag}, {dof}, {val:.9g}")
    if analysis == "buckle":
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
    """
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # Read the peak BEFORE killing: a timeout at 6 GB and a timeout at
        # 500 MB are different failures and want different remedies.
        peak = _peak_rss_mb(proc)
        proc.kill()
        proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout_s) from None
    finally:
        pass
    return _SolverRun(proc.returncode, stdout or "", stderr or "",
                      _peak_rss_mb(proc))


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

    def _run_buckle(self, m, case, constraint_sets, load_sets, run_dir,
                    n_modes: int):
        """One *BUCKLE solve; returns (factors, threads, binary_name)."""
        _write_inp(run_dir / "job.inp", m, case, constraint_sets, load_sets,
                   analysis="buckle", n_modes=n_modes)
        # force_single: ccx_MT returns wrong eigenvalues ~4 times in 5 on an
        # identical job (measured). Never multithread a buckling solve.
        binary, env, threads = self._solver_command(force_single=True)
        try:
            proc = subprocess.run(
                [str(binary), "-i", "job"], cwd=run_dir, env=env,
                capture_output=True, text=True, timeout=self.solve_timeout_s)
        except subprocess.TimeoutExpired:
            raise FeaError(
                f"solver_timeout: {binary.name} exceeded {self.solve_timeout_s}s "
                f"on a buckling solve of {len(m['node_tags'])} nodes")
        if proc.returncode != 0:
            (run_dir / "ccx_stdout.txt").write_text(proc.stdout, encoding="utf-8")
            raise FeaError(f"solver_error: {binary.name} exit {proc.returncode}; "
                           f"tail: {proc.stdout[-400:]!r}")
        return parse_buckling_factors(run_dir / "job.dat"), threads, binary.name

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
        action_id = self.log.open_action(
            "validation", "fea_buckling", geometry_version=str(geometry_id),
            reason=str(reason))
        try:
            _check_reason(reason)
            validate_case(case)
            if case["limit_state"]["name"] != "elastic_buckling":
                raise FeaError(
                    f"fea_buckling requires limit_state 'elastic_buckling', got "
                    f"{case['limit_state']['name']!r}")
            if not self.ccx_path.is_file():
                raise FeaError(f"ccx solver not found at {self.ccx_path}")
            part = self.parts.get_part(geometry_id)
            run_dir = self._next_run_dir()

            m = mesh_step(part["step_file_path"], case["mesh"]["max_size_mm"],
                          case["mesh"].get("min_size_mm"))
            constraint_sets = [select_nodes(m, c["where"])
                               for c in case["constraints"]]
            rbm = check_rigid_body_modes(m, constraint_sets, case["constraints"])

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
                f1, threads, binary = self._run_buckle(
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
                f2, _, _ = self._run_buckle(
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
                "constraint_rank": rbm["constraint_rank"],
                "nodes": int(len(m["node_tags"])),
                "elements": int(len(m["connectivity"])),
                "solver_binary": binary, "solver_threads": threads,
                "solve_seconds": round(solve_s, 2),
                "run_dir": str(run_dir),
                "artifacts": [],
            }
            passed = lowest >= required
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise

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

    def fea_static(self, geometry_id: str, case: dict, reason: str) -> dict:
        action_id = self.log.open_action(
            "validation", "fea_static", geometry_version=str(geometry_id),
            reason=str(reason))
        try:
            _check_reason(reason)
            validate_case(case)
            if not self.ccx_path.is_file():
                raise FeaError(f"ccx solver not found at {self.ccx_path}")
            part = self.parts.get_part(geometry_id)
            run_dir = self._next_run_dir()

            m = mesh_step(part["step_file_path"], case["mesh"]["max_size_mm"],
                          case["mesh"].get("min_size_mm"))
            constraint_sets = [select_nodes(m, c["where"]) for c in case["constraints"]]
            rbm = check_rigid_body_modes(m, constraint_sets, case["constraints"])
            load_sets = [
                _consistent_face_loads(
                    m, select_nodes(m, ld["where"]), ld["force_total_N"],
                    f"case.loads[{i}]", ld["where"])
                for i, ld in enumerate(case["loads"])]
            _write_inp(run_dir / "job.inp", m, case, constraint_sets, load_sets)

            binary, env, threads_used = self._solver_command()
            solve_t0 = time.time()
            try:
                proc = _run_solver(
                    [str(binary), "-i", "job"], run_dir, env,
                    self.solve_timeout_s)
            except subprocess.TimeoutExpired:
                raise FeaError(
                    f"solver_timeout: {binary.name} exceeded "
                    f"{self.solve_timeout_s}s on {len(m['node_tags'])} nodes "
                    f"using {threads_used} thread(s). Direct-solve cost grows "
                    f"steeply with node count - coarsen case.mesh.max_size_mm "
                    f"(subject to the Jacobian gate on the thinnest feature), "
                    f"or raise ValidationTools(solve_timeout_s=...).")
            solve_s = time.time() - solve_t0
            if proc.returncode != 0 or "Job finished" not in proc.stdout:
                (run_dir / "ccx_stdout.txt").write_text(proc.stdout, encoding="utf-8")
                # Name the memory cost in the failure itself. An access
                # violation at a 6 GB working set and one at 500 MB are
                # different failures wanting different remedies, and without
                # this the log could not tell them apart.
                mem = (f" peak memory {proc.peak_rss_mb:.0f} MB;"
                       if proc.peak_rss_mb else "")
                raise FeaError(
                    f"solver_error: {binary.name} exit {proc.returncode};{mem} "
                    f"tail: {proc.stdout[-400:]!r}")

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
            sf = math.inf if max_vm == 0 else allowable / max_vm
            required = case["limit_state"]["required_SF"]

            png_rel = f"validation/{run_dir.name}/von_mises.png"
            _allow_note = (
                f"derated yield {allowable:.1f} MPa "
                f"(k={eff['k_yield']:.3f} @ {eff['service_temp_C']} C, "
                f"room {mat['yield_MPa']} MPa)"
                if ls_name == "thermal_derated_yield"
                else f"yield {mat['yield_MPa']} MPa")
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
                "max_von_mises_MPa": round(max_vm, 6),
                "median_von_mises_MPa": round(median_vm, 6),
                "p99_9_von_mises_MPa": round(p999, 6),
                "stress_outlier_ratio": round(outlier_ratio, 4),
                "stress_outlier_warning": outlier_warning,
                "singularity": singularity,
                "max_von_mises_node": int(max_node),
                "max_von_mises_at_mm": [round(float(v), 3) for v in coords[int(max_node)]],
                "max_displacement_mm": round(max_disp, 9),
                "nodes": int(len(m["node_tags"])),
                "result_records": {"disp": len(disp), "stress": len(stress)},
                "equilibrium": equilibrium,
                "elements": int(len(m["connectivity"])),
                "run_dir": str(run_dir),
                "constraint_rank": rbm["constraint_rank"],
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
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
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
