"""Kinematics / multibody validation via Project Chrono (Phase 8).

Chrono answers a question CalculiX cannot: a static FEA solves one geometry
under one load, but a mechanism has bodies, joints, motion, and loads that
depend on position. This layer computes joint reactions and motion, and its
main payoff is closing the loop — a peak joint reaction computed here becomes
a *computed* load case for run_fea_static instead of an assumed number.

**Separate environment by necessity.** PyChrono is conda-only and cannot live
in the project's 3.12 pip venv beside CadQuery. It runs in a Miniforge env
named `chrono`, driven over a JSON job/result boundary
(`chrono_worker.py`). If that env is absent the tool refuses with
instructions — it never silently degrades to an approximation.

**Launched directly, not through `conda run`.** The env's own interpreter is
invoked with the environment `conda activate` would have set (activation_env).
`conda run` made the worker a grandchild that `Popen.kill()` could not reach,
which on 2026-08-28 let a hung worker hold a full-suite run ~22 minutes past
its 900 s deadline; it also runs a `conda activate` shell round-trip that
failed outright under load. The deadline is enforced by run_worker, which
redirects to files rather than pipes and kills the whole process tree.

**Units.** The engine is mm-based; Chrono is run in SI. Conversion happens
here, at the boundary, so mm and kg can never meet inside the solve (which
would yield milli-newtons and look plausible). Reactions return in newtons,
directly usable as an FEA load.

**Gate.** Like every other validation tool, the gate is a margin against a
*named* limit state, never a pass percentage:
    joint_reaction_force  — peak |F| at any joint vs an allowable force
    joint_reaction_torque — peak |T| at any joint vs an allowable torque
The allowable must carry a `source`, exactly as material data must.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .log import ActionLog
from .proc import kill_tree
from .parts import PartStore, _check_reason

WORKER = Path(__file__).parent / "chrono_worker.py"
DEFAULT_ENV = "chrono"

_CASE_KEYS = {"gravity_mm_s2", "analysis", "duration_s", "dt_s", "fixed",
              "limit_state", "track_body", "external_forces"}
_LIMIT_KEYS = {"name", "allowable", "source"}
_LIMIT_STATES = {"joint_reaction_force": ("force_magnitude_N", "N"),
                 "joint_reaction_torque": ("torque_magnitude_Nm", "N.m")}


class KinematicsError(RuntimeError):
    """The motion case is malformed, or the solve could not be trusted."""


class ChronoUnavailable(RuntimeError):
    """The chrono environment is missing — refuse rather than approximate."""


def _tail(path: Path, n: int = 400) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return ""


def find_conda() -> Path | None:
    for cand in (Path.home() / "miniforge3" / "Scripts" / "conda.exe",
                 Path.home() / "miniconda3" / "Scripts" / "conda.exe",
                 Path.home() / "anaconda3" / "Scripts" / "conda.exe"):
        if cand.is_file():
            return cand
    found = shutil.which("conda")
    return Path(found) if found else None


def find_env_dir(env: str = DEFAULT_ENV) -> Path | None:
    conda = find_conda()
    return None if conda is None else conda.parent.parent / "envs" / env


def env_python(envdir: Path) -> Path:
    """The interpreter inside a conda env, addressed directly."""
    return (envdir / "python.exe" if os.name == "nt"
            else envdir / "bin" / "python")


# The directories `conda activate` prepends to PATH on Windows, in its order.
# Not cosmetic: see activation_env().
_WIN_ACTIVATION_DIRS = ("", "Library/mingw-w64/bin", "Library/usr/bin",
                        "Library/bin", "Scripts", "bin")


def activation_env(envdir: Path) -> dict:
    """The environment `conda activate <env>` would produce, for a direct spawn.

    The worker is launched as `<envdir>/python.exe` rather than through
    `conda run` (see _solve), which means nothing performs the activation the
    interpreter's native extensions rely on. On Windows PyChrono's
    `_irrlicht.pyd` pulls in Irrlicht.dll / SDL.dll / jpeg8.dll from
    `<envdir>/Library/bin`; without that directory on PATH the load fails
    inside a modal Windows error box, and the process then blocks forever with
    no CPU and no output. Measured on this machine, 2026-08-28:

        <envdir>/python.exe -c "import pychrono"      hung  (>9 min, killed)
        ... with the PATH below                       3.2 s ok
        conda run -n chrono python -c "import pychrono"  7.5 s ok

    PYTHONHOME/PYTHONPATH are stripped so the project's 3.12 pip venv cannot
    leak into an interpreter that must not see CadQuery.
    """
    e = dict(os.environ)
    if os.name == "nt":
        dirs = [str(envdir / d) if d else str(envdir)
                for d in _WIN_ACTIVATION_DIRS]
    else:
        dirs = [str(envdir / "bin")]
    e["PATH"] = os.pathsep.join(dirs + [e.get("PATH", "")])
    e["CONDA_PREFIX"] = str(envdir)
    e.pop("PYTHONHOME", None)
    e.pop("PYTHONPATH", None)
    return e


def chrono_available(env: str = DEFAULT_ENV) -> tuple[bool, str]:
    conda = find_conda()
    if conda is None:
        return False, "conda not found (expected Miniforge at ~/miniforge3)"
    envdir = find_env_dir(env)
    if not envdir.is_dir():
        return False, f"conda env {env!r} not found at {envdir}"
    py = env_python(envdir)
    if not py.is_file():
        return False, f"conda env {env!r} has no interpreter at {py}"
    return True, str(envdir)


def run_worker(cmd: list[str], env: dict, timeout_s: float,
               out_path: Path, err_path: Path) -> int:
    """Run `cmd` to completion or kill it at `timeout_s`. Returns the exit code.

    Two deliberate departures from `subprocess.run(..., timeout=...)`, both of
    which were needed to make the deadline actually bind:

    1. **stdout/stderr go to files, not pipes.** `subprocess.run`'s Windows
       timeout path calls `communicate()` a second time after `kill()` *with
       no timeout* (CPython Lib/subprocess.py, `run()`), and that call blocks
       until every process holding the inherited pipe write handles exits.
       A surviving worker therefore holds the parent past its deadline for as
       long as it likes. Files have no reader threads and nothing to block on.

    2. **The whole process tree is killed**, not just the direct child.

    Raises subprocess.TimeoutExpired once the tree is down, so the caller sees
    a deadline, not a hang.
    """
    with open(out_path, "wb") as so, open(err_path, "wb") as se:
        kw = ({"creationflags": subprocess.CREATE_NO_WINDOW}
              if os.name == "nt" else {"start_new_session": True})
        proc = subprocess.Popen(cmd, stdout=so, stderr=se, env=env, **kw)
        try:
            return proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            kill_tree(proc)
            raise
        except BaseException:
            kill_tree(proc)
            raise


def _num(d, key, ctx, *, lo=None):
    v = d.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        raise KinematicsError(f"{ctx}.{key}: expected number, got {v!r}")
    if lo is not None and v <= lo:
        raise KinematicsError(f"{ctx}.{key}: must be > {lo}, got {v}")
    return float(v)


def validate_motion_case(case: dict) -> None:
    if not isinstance(case, dict):
        raise KinematicsError("motion case must be a dict")
    extra = set(case) - _CASE_KEYS
    if extra:
        raise KinematicsError(
            f"motion case: unexpected keys {sorted(extra)} - allowed: "
            f"{sorted(_CASE_KEYS)}")
    g = case.get("gravity_mm_s2")
    if not (isinstance(g, list) and len(g) == 3
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    for x in g)):
        raise KinematicsError(
            "motion case: gravity_mm_s2 must be [gx, gy, gz] in mm/s^2 "
            "(earth gravity down z is [0, 0, -9810])")
    analysis = case.get("analysis", "static")
    if analysis not in ("static", "dynamic"):
        raise KinematicsError(
            f"motion case.analysis must be 'static' or 'dynamic', got {analysis!r}")
    if analysis == "dynamic":
        _num(case, "duration_s", "motion case", lo=0)
        _num(case, "dt_s", "motion case", lo=0)
    fixed = case.get("fixed")
    if not isinstance(fixed, list) or not fixed:
        raise KinematicsError(
            "motion case.fixed: list of component refs to hold fixed (ground) "
            "is required - a mechanism with nothing fixed is unconstrained")

    for i, f in enumerate(case.get("external_forces", [])):
        ctx = f"motion case.external_forces[{i}]"
        if not isinstance(f, dict):
            raise KinematicsError(f"{ctx}: must be a dict")
        extra = set(f) - {"body", "at_mm", "force_N"}
        if extra:
            raise KinematicsError(f"{ctx}: unexpected keys {sorted(extra)}")
        if not isinstance(f.get("body"), str) or not f["body"]:
            raise KinematicsError(f"{ctx}.body: component ref string required")
        for key, n in (("at_mm", 3), ("force_N", 3)):
            v = f.get(key)
            if not (isinstance(v, list) and len(v) == n
                    and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                            for x in v)):
                raise KinematicsError(f"{ctx}.{key}: [x, y, z] numbers required")
        if all(x == 0 for x in f["force_N"]):
            raise KinematicsError(f"{ctx}.force_N: zero force is not a load")

    ls = case.get("limit_state")
    if not isinstance(ls, dict):
        raise KinematicsError("motion case.limit_state: required")
    extra = set(ls) - _LIMIT_KEYS
    if extra:
        raise KinematicsError(f"limit_state: unexpected keys {sorted(extra)}")
    if ls.get("name") not in _LIMIT_STATES:
        raise KinematicsError(
            f"limit_state.name must be one of {sorted(_LIMIT_STATES)}, got "
            f"{ls.get('name')!r} - the gate must name its limit state")
    _num(ls, "allowable", "limit_state", lo=0)
    if not isinstance(ls.get("source"), str) or not ls["source"].strip():
        raise KinematicsError(
            "limit_state.source: required - cite where the allowable comes "
            "from (test, supplier rating, standard). This engine does not "
            "accept unsourced allowables.")


class KinematicsTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore,
                 assemblies, env: str = DEFAULT_ENV, timeout_s: int = 900):
        self.root = Path(root)
        self.log = log
        self.parts = parts
        self.assemblies = assemblies
        self.env = env
        self.timeout_s = timeout_s

    # ---------- job construction (mm -> SI happens here, once) ----------

    def _build_job(self, spec: dict, case: dict) -> dict:
        joints = spec.get("joints")
        if not joints:
            raise KinematicsError(
                f"assembly {spec['name']!r} declares no joints - a kinematics "
                f"run needs a 'joints' list; without one the components are a "
                f"parts list, not a mechanism")
        refs = {}
        for i, comp in enumerate(spec["components"]):
            refs[comp.get("ref", f"c{i}")] = comp

        fixed = set(case["fixed"])
        unknown = fixed - set(refs)
        if unknown:
            raise KinematicsError(
                f"motion case.fixed references unknown component(s) "
                f"{sorted(unknown)}; assembly refs are {sorted(refs)}")

        bodies = []
        for ref, comp in refs.items():
            part = self.parts.get_part(comp["geometry_id"])
            props = part["properties"]
            mass = props.get("mass_kg_estimate")
            inertia = props.get("inertia_kg_m2_about_com")
            if mass is None or inertia is None:
                raise KinematicsError(
                    f"component {ref!r} ({comp['geometry_id']}): no mass or "
                    f"inertia - the part spec needs 'density_kg_m3'. A "
                    f"multibody solve cannot proceed on unknown mass.")
            at = comp.get("at", [0, 0, 0])
            com = props["center_of_mass_mm"]
            bodies.append({
                "id": ref,
                "mass_kg": mass,
                "inertia_kg_m2": inertia,
                "com_m": [(at[k] + com[k]) / 1000.0 for k in (0, 1, 2)],
                "fixed": ref in fixed,
                "geometry_id": comp["geometry_id"],
            })

        jout = []
        for i, j in enumerate(joints):
            allowed = {"id", "type", "between", "at", "axis"}
            bad = set(j) - allowed
            if bad:
                raise KinematicsError(
                    f"joints[{i}]: unexpected keys {sorted(bad)} - allowed "
                    f"{sorted(allowed)}")
            between = j.get("between")
            if not (isinstance(between, list) and len(between) == 2):
                raise KinematicsError(f"joints[{i}].between: [refA, refB] required")
            miss = [b for b in between if b not in refs]
            if miss:
                raise KinematicsError(
                    f"joints[{i}].between references unknown component(s) "
                    f"{miss}; assembly refs are {sorted(refs)}")
            at = j.get("at")
            if not (isinstance(at, list) and len(at) == 3):
                raise KinematicsError(f"joints[{i}].at: [x, y, z] in mm required")
            jout.append({
                "id": j.get("id", f"j{i}"),
                "type": j.get("type"),
                "between": between,
                "at_m": [v / 1000.0 for v in at],
                "axis": j.get("axis", [0, 0, 1]),
            })

        ext_forces = []
        for i, f in enumerate(case.get("external_forces", [])):
            if f["body"] not in refs:
                raise KinematicsError(
                    f"motion case.external_forces[{i}].body: unknown ref "
                    f"{f['body']!r}; assembly refs are {sorted(refs)}")
            ext_forces.append({
                "body": f["body"],
                "at_m": [v / 1000.0 for v in f["at_mm"]],
                "force_N": f["force_N"],
            })

        job = {
            "gravity_m_s2": [v / 1000.0 for v in case["gravity_mm_s2"]],
            "bodies": bodies,
            "joints": jout,
            "analysis": case.get("analysis", "static"),
            "external_forces": ext_forces,
        }
        if job["analysis"] == "dynamic":
            job["duration_s"] = case["duration_s"]
            job["dt_s"] = case["dt_s"]
            if case.get("track_body"):
                job["track_body"] = case["track_body"]
        return job

    # ---------- the bridge ----------

    def _solve(self, job: dict) -> dict:
        ok, detail = chrono_available(self.env)
        if not ok:
            raise ChronoUnavailable(
                f"chrono_env_missing: {detail}. PyChrono is conda-only and "
                f"cannot be installed in this pip venv. Create it with: "
                f"conda create -n {self.env} python=3.12 -y && conda install "
                f"-n {self.env} projectchrono::pychrono -c conda-forge -y")
        envdir = find_env_dir(self.env)
        py = env_python(envdir)
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            jf, of = td / "job.json", td / "out.json"
            oo, oe = td / "worker.out", td / "worker.err"
            jf.write_text(json.dumps(job), encoding="utf-8")
            # The env's interpreter directly, not `conda run`. `conda run`
            # interposes a process, so the worker becomes a *grandchild* that
            # Popen.kill() cannot reach; on 2026-08-28 that let a hung worker
            # hold the suite ~22 minutes past a 900 s deadline. It also runs a
            # `conda activate` shell round-trip that failed outright under
            # load ("No output from 'conda activate'", exit 5), and costs
            # ~4.3 s per call that the direct spawn does not.
            try:
                rc = run_worker(
                    [str(py), str(WORKER), str(jf), str(of)],
                    activation_env(envdir), self.timeout_s, oo, oe)
            except subprocess.TimeoutExpired:
                raise KinematicsError(
                    f"chrono_timeout: the worker exceeded {self.timeout_s}s "
                    f"and was killed with its whole process tree. stderr "
                    f"tail: {_tail(oe)!r}") from None
            if not of.is_file():
                raise KinematicsError(
                    f"chrono_worker produced no result (exit {rc}); "
                    f"stderr tail: {_tail(oe)!r}")
            result = json.loads(of.read_text(encoding="utf-8"))
        if not result.get("ok"):
            raise KinematicsError(f"chrono_solve_failed: {result.get('error')}")
        return result

    # ---------- the gated tool ----------

    def run_kinematics(self, assembly_id: str, motion_case: dict,
                       reason: str) -> dict:
        action_id = self.log.open_action(
            "validation", "run_kinematics", geometry_version=str(assembly_id),
            reason=str(reason))
        try:
            _check_reason(reason)
            validate_motion_case(motion_case)
            spec = self.assemblies.get_assembly(assembly_id)
            job = self._build_job(spec, motion_case)
            result = self._solve(job)

            ls = motion_case["limit_state"]
            field, unit = _LIMIT_STATES[ls["name"]]
            allowable = float(ls["allowable"])
            worst = max(result["reactions"], key=lambda r: r[field])
            peak = worst[field]
            margin = (allowable / peak) if peak > 0 else float("inf")
            passed = peak <= allowable

            details = {
                "limit_state": ls["name"],
                "allowable": allowable,
                "allowable_unit": unit,
                "allowable_source": ls["source"].strip(),
                "peak_value": round(peak, 6),
                "peak_joint": worst["joint_id"],
                "margin_ratio": (round(margin, 6) if margin != float("inf")
                                 else "inf"),
                "analysis": result["analysis"],
                "reactions": result["reactions"],
                "bodies": result["bodies"],
                "joint_types": sorted({j["type"] for j in job["joints"]}),
                "units_note": ("solved in SI; reactions in N and N.m, directly "
                               "usable as an FEA load case"),
                "artifacts": [],
            }
            if result["analysis"] == "dynamic":
                details["duration_s"] = result["duration_s"]
                details["steps"] = result["steps"]
                details["trajectory"] = result.get("trajectory", [])
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
                    f"{ls['name']}: peak {peak:.3f} {unit} at joint "
                    f"{worst['joint_id']!r} exceeds allowable "
                    f"{allowable:.3f} {unit} (margin {margin:.3f})"))
        return {
            "result": "pass" if passed else "fail",
            "action_id": action_id,
            "failure_id": None if passed else action_id,
            "limit_state": ls["name"],
            "peak_value": peak,
            "peak_unit": unit,
            "peak_joint": worst["joint_id"],
            "margin_ratio": margin,
            "reactions": result["reactions"],
        }
