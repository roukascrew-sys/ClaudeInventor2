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

import json
import math
import re
import subprocess
from pathlib import Path

import numpy as np

from .log import ActionLog
from .mesh import MeshError, mesh_step, select_nodes
from .parts import PartStore, _check_reason


class FeaError(RuntimeError):
    pass


_CASE_KEYS = {"material", "mesh", "constraints", "loads", "limit_state"}
_MATERIAL_KEYS = {"name", "E_MPa", "nu", "yield_MPa", "source"}
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
    if ls.get("name") != "yield_von_mises":
        raise FeaError(
            f"case.limit_state.name: only 'yield_von_mises' is implemented in v0, "
            f"got {ls.get('name')!r} — the gate must name its limit state")
    _num(ls, "required_SF", "case.limit_state", lo=0)


def _consistent_face_loads(mesh: dict, selected_tags, force_total_N: list,
                           ctx: str) -> dict[int, np.ndarray]:
    """Nodal forces for uniform traction on the selected planar face.

    Per straight-sided T6 triangle of area a carrying force F*(a/A): corners
    get 0, each midside gets 1/3. Returns {node_tag: [fx, fy, fz]}.
    """
    selected = {int(t) for t in selected_tags}
    coords = {int(t): c for t, c in zip(mesh["node_tags"], mesh["coords"])}
    tris = [row for row in mesh["tri6"]
            if all(int(n) in selected for n in row)]
    if not tris:
        raise FeaError(
            f"{ctx}: no boundary triangles lie fully inside the selection — "
            f"the load selector must cover one planar boundary face")
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
               constraint_sets: list, load_sets: list) -> None:
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
    lines += [f"*MATERIAL, NAME=MAT",
              "*ELASTIC",
              f"{mat['E_MPa']:.9g}, {mat['nu']:.9g}",
              "*SOLID SECTION, ELSET=EALL, MATERIAL=MAT",
              "*STEP", "*STATIC"]
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
    lines += ["*NODE FILE", "U", "*EL FILE", "S", "*END STEP", ""]
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


class ValidationTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore,
                 ccx_path: str | Path):
        self.root = Path(root)
        self.run_root = self.root / "validation"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.parts = parts
        self.ccx_path = Path(ccx_path)

    def _next_run_dir(self) -> Path:
        nums = [int(p.name[1:]) for p in self.run_root.glob("R[0-9]*") if p.is_dir()]
        run = self.run_root / f"R{(max(nums) + 1 if nums else 1):04d}"
        run.mkdir()
        return run

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
            load_sets = [
                _consistent_face_loads(
                    m, select_nodes(m, ld["where"]), ld["force_total_N"],
                    f"case.loads[{i}]")
                for i, ld in enumerate(case["loads"])]
            _write_inp(run_dir / "job.inp", m, case, constraint_sets, load_sets)

            proc = subprocess.run(
                [str(self.ccx_path), "-i", "job"], cwd=run_dir,
                capture_output=True, text=True, timeout=600)
            if proc.returncode != 0 or "Job finished" not in proc.stdout:
                (run_dir / "ccx_stdout.txt").write_text(proc.stdout, encoding="utf-8")
                raise FeaError(
                    f"solver_error: ccx exit {proc.returncode}; "
                    f"tail: {proc.stdout[-400:]!r}")

            blocks = _parse_frd(run_dir / "job.frd")
            stress = blocks.get("STRESS")
            disp = blocks.get("DISP")
            if not stress or not disp:
                raise FeaError("solver produced no STRESS/DISP results in job.frd")

            vm = {n: von_mises(s) for n, s in stress.items()}
            coords = {int(t): c for t, c in zip(m["node_tags"], m["coords"])}
            max_node = max(vm, key=vm.get)
            max_vm = vm[max_node]
            median_vm = float(np.median(list(vm.values())))
            max_disp = max(math.sqrt(sum(v ** 2 for v in u[:3]))
                           for u in disp.values())

            mat = case["material"]
            sf = math.inf if max_vm == 0 else mat["yield_MPa"] / max_vm
            required = case["limit_state"]["required_SF"]
            ls_name = case["limit_state"]["name"]

            png_rel = f"validation/{run_dir.name}/von_mises.png"
            _diagnostic_png(
                self.root / png_rel, m, vm,
                f"{geometry_id} — {ls_name}: SF={sf:.3f} "
                f"(required {required}) — max vM {max_vm:.1f} MPa vs "
                f"yield {mat['yield_MPa']} MPa [{mat['name']}]")

            details = {
                "material": mat,
                "limit_state": ls_name,
                "required_SF": required,
                "safety_factor": round(sf, 6) if sf != math.inf else "inf",
                "max_von_mises_MPa": round(max_vm, 6),
                "median_von_mises_MPa": round(median_vm, 6),
                "max_von_mises_node": int(max_node),
                "max_von_mises_at_mm": [round(v, 3) for v in coords[int(max_node)]],
                "max_displacement_mm": round(max_disp, 9),
                "nodes": int(len(m["node_tags"])),
                "elements": int(len(m["connectivity"])),
                "run_dir": str(run_dir),
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
                    f"(max vM {max_vm:.1f} MPa vs yield {mat['yield_MPa']} MPa "
                    f"at node {int(max_node)} {details['max_von_mises_at_mm']} mm)"))
        return {
            "result": "pass" if passed else "fail",
            "action_id": action_id,
            "failure_id": None if passed else action_id,
            "safety_factor": sf,
            "max_von_mises_MPa": max_vm,
            "median_von_mises_MPa": median_vm,
            "max_displacement_mm": max_disp,
            "artifacts": [png_rel],
            "run_dir": str(run_dir),
        }
