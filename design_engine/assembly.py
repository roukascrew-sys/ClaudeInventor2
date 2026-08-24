"""Assemblies and 1D tolerance stackup analysis.

check_tolerance_stackup implements the two standard 1D methods (see e.g.
Fischer, *Mechanical Tolerance Stackup and Analysis*, and the ASME Y14.5
tolerancing framework):

- **Worst-case (arithmetic)**: every term simultaneously at its most adverse
  limit. This is the gate value — it makes no statistical assumptions.
- **RSS (root-sum-square)**: reported alongside for information. RSS assumes
  the terms are independent and centered within their tolerance bands; it is
  NOT the gate, because those assumptions are unverified for our parts.

Assembly spec (units: mm):

    {
      "name": "pin-in-bore",
      "units": "mm",
      "components": [{"geometry_id": "P0001@v1", "at": [0, 0, 0]}, ...],
      "chains": [
        {
          "name": "axial-clearance",
          "requirement_mm": {"min": 0.05},          # min and/or max bound
          "terms": [
            {"desc": "bore depth", "nominal": 10.0,
             "tol_plus": 0.10, "tol_minus": 0.10, "sense": 1},
            {"desc": "pin length", "nominal": 9.8,
             "tol_plus": 0.05, "tol_minus": 0.05, "sense": -1}
          ]
        }
      ]
    }

Sense +1/-1 is the term's direction in the dimension chain. Tolerances are
magnitudes (tol_plus/tol_minus >= 0), interpreted as nominal +tol_plus/-tol_minus.

`worst_case_mm` in the result = the smallest margin (mm) between the
worst-case stack and its requirement bound, across all chains and bounds.
Negative means at least one chain violates its requirement.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .geometry import SpecError
from .log import ActionLog
from .parts import PartStore, _check_reason

_AID_RE = re.compile(r"^A\d{4}$")


class AssemblyNotFound(KeyError):
    pass


def _num(chain_idx: int, term_idx: int, term: dict, key: str, *,
         nonneg: bool = False) -> float:
    val = term.get(key)
    if not isinstance(val, (int, float)) or isinstance(val, bool):
        raise SpecError(
            f"chains[{chain_idx}].terms[{term_idx}].{key}: expected number, got {val!r}")
    if nonneg and val < 0:
        raise SpecError(
            f"chains[{chain_idx}].terms[{term_idx}].{key}: must be >= 0, got {val!r}")
    return float(val)


def validate_assembly_spec(spec: dict, parts: PartStore) -> None:
    if not isinstance(spec, dict):
        raise SpecError("assembly spec must be a dict")
    if spec.get("units") != "mm":
        raise SpecError(f"assembly units must be 'mm', got {spec.get('units')!r}")
    if not isinstance(spec.get("name"), str) or not spec["name"]:
        raise SpecError("assembly needs a non-empty 'name'")
    comps = spec.get("components")
    if not isinstance(comps, list) or not comps:
        raise SpecError("assembly needs a non-empty 'components' list")
    for i, comp in enumerate(comps):
        gid = comp.get("geometry_id") if isinstance(comp, dict) else None
        if not gid:
            raise SpecError(f"components[{i}]: 'geometry_id' required")
        parts.get_part(gid)  # raises PartNotFound if the version doesn't exist
    chains = spec.get("chains")
    if not isinstance(chains, list) or not chains:
        raise SpecError("assembly needs a non-empty 'chains' list")
    for ci, chain in enumerate(chains):
        if not isinstance(chain.get("name"), str) or not chain["name"]:
            raise SpecError(f"chains[{ci}]: 'name' required")
        req = chain.get("requirement_mm")
        if not isinstance(req, dict) or not ({"min", "max"} & req.keys()):
            raise SpecError(
                f"chains[{ci}].requirement_mm: needs 'min' and/or 'max' bound")
        terms = chain.get("terms")
        if not isinstance(terms, list) or not terms:
            raise SpecError(f"chains[{ci}]: non-empty 'terms' list required")
        for ti, term in enumerate(terms):
            _num(ci, ti, term, "nominal")
            _num(ci, ti, term, "tol_plus", nonneg=True)
            _num(ci, ti, term, "tol_minus", nonneg=True)
            if term.get("sense") not in (1, -1):
                raise SpecError(f"chains[{ci}].terms[{ti}].sense: must be 1 or -1")


def analyze_chain(chain: dict) -> dict:
    """Worst-case + RSS stack for one chain. Pure math, fully deterministic."""
    nominal = wc_plus = wc_minus = mean = var = 0.0
    for term in chain["terms"]:
        s, n = term["sense"], term["nominal"]
        tp, tm = term["tol_plus"], term["tol_minus"]
        nominal += s * n
        # most adverse deviations: +tol_plus helps (+) terms up, -tol_minus down;
        # for (-) terms the roles swap
        wc_plus += tp if s > 0 else tm
        wc_minus += tm if s > 0 else tp
        # RSS on the equivalent bilateral band (assumes independence + centering)
        mid = n + (tp - tm) / 2.0
        half = (tp + tm) / 2.0
        mean += s * mid
        var += half * half
    rss_half = math.sqrt(var)
    wc_min, wc_max = nominal - wc_minus, nominal + wc_plus
    req = chain["requirement_mm"]
    margins = {}
    if "min" in req:
        margins["min_bound"] = round(wc_min - req["min"], 9)
    if "max" in req:
        margins["max_bound"] = round(req["max"] - wc_max, 9)
    worst_margin = min(margins.values())
    return {
        "name": chain["name"],
        "nominal_mm": round(nominal, 9),
        "worst_case_mm": {"min": round(wc_min, 9), "max": round(wc_max, 9)},
        "rss_mm": {"min": round(mean - rss_half, 9),
                   "max": round(mean + rss_half, 9),
                   "assumptions": "independent, centered terms"},
        "requirement_mm": req,
        "margins_mm": margins,
        "worst_margin_mm": round(worst_margin, 9),
        "result": "pass" if worst_margin >= 0 else "fail",
    }


class AssemblyStore:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore):
        self.root = Path(root) / "assemblies"
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log
        self.parts = parts

    def _next_assembly_id(self) -> str:
        nums = [int(p.name[1:]) for p in self.root.glob("A[0-9]*") if p.is_dir()]
        return f"A{(max(nums) + 1 if nums else 1):04d}"

    def get_assembly(self, assembly_id: str) -> dict:
        if not _AID_RE.match(assembly_id or ""):
            raise AssemblyNotFound(f"malformed assembly_id {assembly_id!r}")
        path = self.root / assembly_id / "assembly.json"
        if not path.is_file():
            raise AssemblyNotFound(f"assembly {assembly_id!r} does not exist")
        return json.loads(path.read_text(encoding="utf-8"))

    def create_assembly(self, spec: dict, reason: str) -> dict:
        action_id = self.log.open_action("design", "create_assembly",
                                         reason=str(reason))
        try:
            _check_reason(reason)
            validate_assembly_spec(spec, self.parts)
            aid = self._next_assembly_id()
            adir = self.root / aid
            adir.mkdir(parents=True)
            (adir / "assembly.json").write_text(
                json.dumps(spec, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        self.log.close_action(
            action_id, "pass", geometry_version=aid,
            details={"components": [c["geometry_id"] for c in spec["components"]],
                     "chains": [c["name"] for c in spec["chains"]]})
        return {"assembly_id": aid}

    def check_tolerance_stackup(self, assembly_id: str) -> dict:
        """Analysis is an action too — its pass/fail lands in the log."""
        action_id = self.log.open_action(
            "design", "check_tolerance_stackup", geometry_version=str(assembly_id))
        try:
            spec = self.get_assembly(assembly_id)
            chains = [analyze_chain(c) for c in spec["chains"]]
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        worst_case_mm = min(c["worst_margin_mm"] for c in chains)
        failing = [c["name"] for c in chains if c["result"] == "fail"]
        report = {
            "assembly_id": assembly_id,
            "method": "worst-case (gate) + RSS (informational)",
            "chains": chains,
            "worst_case_mm": worst_case_mm,
        }
        self.log.close_action(
            action_id,
            "pass" if not failing else "fail",
            failure_mode=None if not failing
            else "tolerance_stackup_violation: " + ", ".join(failing),
            details=report,
        )
        return {"report": report, "worst_case_mm": worst_case_mm}
