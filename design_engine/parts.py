"""Part store: create_part / edit_part — the Design-phase mutating tools.

Contracts (locked in the build plan, Phase 1):

    create_part(spec, reason)          -> {geometry_id, step_file_path, properties}
    edit_part(geometry_id, changes, reason) -> {new_geometry_id, step_file_path,
                                                properties, diff}

Every mutating call requires a non-empty `reason` — that feeds the change log.
Log-first discipline: the pending row is written before geometry is built; the
row is finalized pass/fail afterwards. A refused call (empty reason, bad spec)
still leaves a fail row — refusals are data.

Storage layout (all reproducible from spec.json; the log is the truth):
    <root>/parts/P0001/v1/{spec.json, part.step, props.json}
Geometry ids are "P0001@v1". Edits always allocate the next version number of
the part, with the log row linking back to the parent version's row.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import cadquery as cq

from . import geometry
from .log import ActionLog

_GID_RE = re.compile(r"^(P\d{4})@v(\d+)$")


class PartNotFound(KeyError):
    pass


def _check_reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a non-empty 'reason' string is required — it feeds the change log")
    return reason.strip()


class PartStore:
    def __init__(self, root: str | Path, log: ActionLog):
        self.root = Path(root) / "parts"
        self.root.mkdir(parents=True, exist_ok=True)
        self.log = log

    # ---------- id helpers ----------

    def _next_part_number(self) -> str:
        nums = [int(p.name[1:]) for p in self.root.glob("P[0-9]*") if p.is_dir()]
        return f"P{(max(nums) + 1 if nums else 1):04d}"

    def _version_dir(self, geometry_id: str) -> Path:
        m = _GID_RE.match(geometry_id)
        if not m:
            raise PartNotFound(f"malformed geometry_id {geometry_id!r} (want P0000@vN)")
        d = self.root / m.group(1) / f"v{m.group(2)}"
        if not d.is_dir():
            raise PartNotFound(f"geometry_id {geometry_id!r} does not exist")
        return d

    def _next_version(self, part_number: str) -> int:
        versions = [int(v.name[1:]) for v in (self.root / part_number).glob("v[0-9]*")]
        return max(versions) + 1 if versions else 1

    # ---------- persistence ----------

    def _write_version(self, part_number: str, version: int, spec: dict,
                       solid: cq.Workplane) -> tuple[str, Path, dict]:
        gid = f"{part_number}@v{version}"
        vdir = self.root / part_number / f"v{version}"
        vdir.mkdir(parents=True, exist_ok=True)
        step_path = vdir / "part.step"
        cq.exporters.export(solid, str(step_path))
        props = geometry.mass_properties(spec, solid)
        (vdir / "spec.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
        (vdir / "props.json").write_text(json.dumps(props, indent=2), encoding="utf-8")
        return gid, step_path, props

    def get_part(self, geometry_id: str) -> dict:
        """Read-only fetch of a stored version (spec, properties, step path)."""
        vdir = self._version_dir(geometry_id)
        return {
            "geometry_id": geometry_id,
            "spec": json.loads((vdir / "spec.json").read_text(encoding="utf-8")),
            "properties": json.loads((vdir / "props.json").read_text(encoding="utf-8")),
            "step_file_path": str(vdir / "part.step"),
        }

    # ---------- mutating tools ----------

    def create_part(self, spec: dict, reason: str) -> dict:
        action_id = self.log.open_action("design", "create_part", reason=str(reason))
        try:
            reason = _check_reason(reason)
            solid = geometry.build(spec)
            gid, step_path, props = self._write_version(
                self._next_part_number(), 1, spec, solid)
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        self.log.close_action(action_id, "pass", geometry_version=gid,
                              details={"properties": props})
        return {"geometry_id": gid, "step_file_path": str(step_path),
                "properties": props}

    def edit_part(self, geometry_id: str, changes: dict, reason: str,
                  addresses_failure_id: int | None = None) -> dict:
        """addresses_failure_id: id of the fail row this edit responds to —
        the non-linear revert contract (never retry blind after a Validation
        failure; reference the failure record instead)."""
        parent_row = self.log.latest_pass_for(str(geometry_id))
        action_id = self.log.open_action(
            "design", "edit_part", geometry_version=str(geometry_id),
            reason=str(reason),
            linked_parent_id=parent_row["id"] if parent_row else None)
        try:
            reason = _check_reason(reason)
            if not isinstance(changes, dict) or not changes:
                raise ValueError("'changes' must be a non-empty dict of path -> value")
            if addresses_failure_id is not None:
                ref = [r for r in self.log.rows(result="fail")
                       if r["id"] == addresses_failure_id]
                if not ref:
                    raise ValueError(
                        f"addresses_failure_id={addresses_failure_id} does not "
                        f"reference an existing fail row in the log")
            parent = self.get_part(geometry_id)
            new_spec, diff = geometry.apply_changes(parent["spec"], changes)
            if new_spec == parent["spec"]:
                raise ValueError("changes produced an identical spec — nothing to edit")
            solid = geometry.build(new_spec)
            part_number = geometry_id.split("@")[0]
            gid, step_path, props = self._write_version(
                part_number, self._next_version(part_number), new_spec, solid)
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        prop_delta = {
            "volume_mm3": [parent["properties"]["volume_mm3"], props["volume_mm3"]],
        }
        details = {"diff": diff, "property_delta": prop_delta,
                   "parent": geometry_id, "properties": props}
        if addresses_failure_id is not None:
            details["addresses_failure_id"] = addresses_failure_id
        self.log.close_action(action_id, "pass", geometry_version=gid,
                              details=details)
        return {"new_geometry_id": gid, "step_file_path": str(step_path),
                "properties": props, "diff": diff}
