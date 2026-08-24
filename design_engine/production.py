"""Production-phase tools behind the human sign-off lock (Phase 5).

The lock is CODE, not process: every production function calls
verify_sign_off() before doing anything, and that check can only be satisfied
by a sign-off row in the log. No row, no execution — there is no bypass
parameter and no force flag.

What a sign-off binds to, and what invalidates it:

- The sign-off stores the **spec digest** of the exact geometry version it
  approves. verify_sign_off() recomputes the digest from spec.json on disk at
  execution time — editing or tampering with the spec after signing makes the
  sign-off invalid.
- A sign-off is only accepted when the version's **latest validation verdict
  is a pass** (at least one fea_static run, most recent one passed). A failed
  validation run recorded after the sign-off invalidates it the same way at
  verify time.
- A new version of the part is a new geometry_id and needs its own sign-off.

Honest scope note: this is a single-user local tool — the sign-off row proves
*a name and statement were recorded in the audit log*, not cryptographic
identity. The enforced guarantees are: production cannot run without a
recorded sign-off, and cannot run on geometry that differs from, or failed
validation after, what was signed.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from .geometry import spec_digest
from .log import ActionLog
from .parts import PartStore, _check_reason


class SignOffError(ValueError):
    """A sign-off attempt was refused (nothing valid to sign)."""


class SignOffRequired(RuntimeError):
    """A production function was called without a valid sign-off. Locked."""


class ProductionTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore):
        self.root = Path(root)
        self.out_root = self.root / "production"
        self.log = log
        self.parts = parts

    # ---------- validation state ----------

    def _validation_state(self, geometry_id: str):
        """(ok, message, latest_pass_row) for the version's fea history."""
        rows = [r for r in self.log.rows(action="fea_static",
                                         geometry_version=geometry_id)
                if r["result"] != "pending"]
        if not rows:
            return False, f"no validation run recorded for {geometry_id}", None
        last = rows[-1]
        if last["result"] != "pass":
            return (False,
                    f"latest validation of {geometry_id} (log #{last['id']}) "
                    f"failed: {last['failure_mode']}", None)
        return True, "", last

    # ---------- the human step ----------

    def sign_off(self, geometry_id: str, signed_off_by: str,
                 statement: str) -> dict:
        """Record a human sign-off for one validated geometry version."""
        action_id = self.log.open_action(
            "production", "sign_off", geometry_version=str(geometry_id),
            reason=str(statement))
        try:
            if not isinstance(signed_off_by, str) or not signed_off_by.strip():
                raise SignOffError("signed_off_by: a person's name is required")
            if not isinstance(statement, str) or not statement.strip():
                raise SignOffError(
                    "statement: a non-empty sign-off statement is required "
                    "(what is approved, for what use)")
            part = self.parts.get_part(geometry_id)
            digest = spec_digest(part["spec"])
            ok, msg, vrow = self._validation_state(geometry_id)
            if not ok:
                raise SignOffError(f"sign-off refused: {msg}")
            vdetails = json.loads(vrow["details_json"])
            stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds")
            token = hashlib.sha256(
                f"{geometry_id}:{digest}:{signed_off_by}:{stamp}".encode()
            ).hexdigest()[:16]
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        self.log.close_action(
            action_id, "pass", signed_off_by=signed_off_by.strip(),
            details={
                "spec_digest": digest,
                "statement": statement.strip(),
                "token": token,
                "validation_action_id": vrow["id"],
                "validated_safety_factor": vdetails.get("safety_factor"),
                "limit_state": vdetails.get("limit_state"),
            })
        return {"sign_off_id": action_id, "token": token,
                "geometry_id": geometry_id, "spec_digest": digest}

    # ---------- the lock ----------

    def verify_sign_off(self, geometry_id: str) -> sqlite3.Row:
        """Return the valid sign-off row for this version or raise
        SignOffRequired with the precise reason production is locked."""
        part = self.parts.get_part(geometry_id)
        current_digest = spec_digest(part["spec"])
        rows = [r for r in self.log.rows(action="sign_off", result="pass",
                                         geometry_version=str(geometry_id))
                if r["signed_off_by"]]
        if not rows:
            raise SignOffRequired(
                f"sign_off_missing: no sign-off recorded for {geometry_id} — "
                f"production is locked")
        row = rows[-1]
        stored = json.loads(row["details_json"])["spec_digest"]
        if stored != current_digest:
            raise SignOffRequired(
                f"sign_off_invalid: spec digest of {geometry_id} is "
                f"{current_digest} but the sign-off (log #{row['id']}) signed "
                f"{stored} — geometry changed after sign-off")
        ok, msg, _ = self._validation_state(geometry_id)
        if not ok:
            raise SignOffRequired(f"sign_off_invalidated: {msg}")
        return row

    # ---------- first gated production function ----------

    def export_production_package(self, geometry_id: str, reason: str) -> dict:
        """Write the release package for a signed-off version: STEP + spec +
        validation summary + sign-off certificate (certificate from log rows)."""
        action_id = self.log.open_action(
            "production", "export_production_package",
            geometry_version=str(geometry_id), reason=str(reason))
        try:
            _check_reason(reason)
            sign_row = self.verify_sign_off(geometry_id)  # THE LOCK
            part = self.parts.get_part(geometry_id)
            sdet = json.loads(sign_row["details_json"])
            ok, _, vrow = self._validation_state(geometry_id)
            assert ok  # verify_sign_off already guaranteed this
            vdet = json.loads(vrow["details_json"])

            pkg = self.out_root / geometry_id.replace("@", "_")
            pkg.mkdir(parents=True, exist_ok=True)
            shutil.copy2(part["step_file_path"], pkg / "part.step")
            (pkg / "spec.json").write_text(
                json.dumps(part["spec"], indent=2), encoding="utf-8")
            (pkg / "validation_summary.json").write_text(json.dumps({
                "validation_action_id": vrow["id"],
                "timestamp": vrow["timestamp"],
                "limit_state": vdet.get("limit_state"),
                "required_SF": vdet.get("required_SF"),
                "safety_factor": vdet.get("safety_factor"),
                "max_von_mises_MPa": vdet.get("max_von_mises_MPa"),
                "material": vdet.get("material"),
                "mesh_nodes": vdet.get("nodes"),
                "artifacts": vdet.get("artifacts"),
            }, indent=2), encoding="utf-8")
            (pkg / "sign_off_certificate.json").write_text(json.dumps({
                "geometry_id": geometry_id,
                "spec_digest": sdet["spec_digest"],
                "signed_off_by": sign_row["signed_off_by"],
                "statement": sdet["statement"],
                "token": sdet["token"],
                "sign_off_log_id": sign_row["id"],
                "signed_at": sign_row["timestamp"],
            }, indent=2), encoding="utf-8")
            files = sorted(p.name for p in pkg.iterdir())
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        self.log.close_action(
            action_id, "pass",
            details={"package_dir": str(pkg), "files": files,
                     "sign_off_id": sign_row["id"]})
        return {"package_dir": str(pkg), "files": files,
                "sign_off_id": sign_row["id"], "action_id": action_id}
