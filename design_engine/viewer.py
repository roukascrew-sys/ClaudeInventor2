"""Interactive 3D viewer (Phase 6) — Production phase only, sign-off gated.

Per the build plan this is the LAST thing built and a human review layer, not
part of the loop the engine closes on itself. It reads only a **signed-off**
geometry version: generate_viewer() calls verify_sign_off() first, exactly
like every other production function, so a draft can never be rendered as if
it were released.

Self-contained: three.js r147 (MIT) is vendored in data/three.min.js and
inlined into the output, along with the tessellated mesh. The published file
makes no network requests.

Everything in the info panel comes from log rows — geometry from the signed
spec, safety factor and material from the validation row the sign-off cites,
cost from the BOM row if one exists. Nothing is authored here.

Orbit controls are hand-written (~50 lines) rather than a second vendored
dependency. Z-up is preserved (CAD convention), not silently converted to
three.js's default Y-up.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from . import geometry
from .log import ActionLog
from .parts import PartStore, _check_reason
from .production import ProductionTools

THREE_JS_PATH = Path(__file__).parent / "data" / "three.min.js"
TEMPLATE_PATH = Path(__file__).parent / "data" / "viewer_template.html"
ASSEMBLY_TEMPLATE_PATH = (Path(__file__).parent / "data"
                          / "assembly_viewer_template.html")

# Display tessellation only — never used for analysis. The solver meshes
# independently in fea.py; this is a picture, the numbers are the claim.
DEFAULT_LINEAR_TOL_MM = 0.08
DEFAULT_ANGULAR_TOL_RAD = 0.2


def tessellate(spec: dict, linear_tol_mm: float = DEFAULT_LINEAR_TOL_MM,
               angular_tol_rad: float = DEFAULT_ANGULAR_TOL_RAD) -> dict:
    """Rebuild the solid from its spec and triangulate it for display."""
    solid = geometry.build(spec)
    verts, tris = solid.val().tessellate(linear_tol_mm, angular_tol_rad)
    positions = []
    for v in verts:
        positions += [round(v.x, 4), round(v.y, 4), round(v.z, 4)]
    indices = [int(i) for tri in tris for i in tri]
    return {"positions": positions, "indices": indices,
            "triangles": len(tris), "vertices": len(verts),
            "linear_tol_mm": linear_tol_mm, "angular_tol_rad": angular_tol_rad}


class ViewerTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore,
                 production: ProductionTools, assemblies=None):
        self.root = Path(root)
        self.log = log
        self.parts = parts
        self.production = production
        self.assemblies = assemblies

    def _validation_summary(self, geometry_id: str, sign_row) -> dict:
        vid = json.loads(sign_row["details_json"])["validation_action_id"]
        row = [r for r in self.log.rows(action="fea_static") if r["id"] == vid]
        if not row:
            return {}
        det = json.loads(row[0]["details_json"])
        return {
            "validation_action_id": vid,
            "limit_state": det.get("limit_state"),
            "required_SF": det.get("required_SF"),
            "safety_factor": det.get("safety_factor"),
            "max_von_mises_MPa": det.get("max_von_mises_MPa"),
            "max_von_mises_at_mm": det.get("max_von_mises_at_mm"),
            "material": det.get("material"),
        }

    def _bom_summary(self, geometry_id: str) -> dict:
        rows = [r for r in self.log.rows(action="generate_bom", result="pass",
                                         geometry_version=geometry_id)]
        if not rows:
            return {}
        det = json.loads(rows[-1]["details_json"])
        return {
            "assemblies": det.get("assemblies"),
            "as_specified_total_usd": det.get("as_specified_total_usd"),
            "min_cost_total_usd": det.get("min_cost_total_usd"),
            "price_as_of": (det.get("pricing") or {}).get("price_as_of"),
            "budget_label": (det.get("budget") or {}).get("label"),
        }

    def generate_assembly_viewer(self, assembly_id: str, reason: str,
                                 out_path: str | Path | None = None,
                                 linear_tol_mm: float = DEFAULT_LINEAR_TOL_MM
                                 ) -> dict:
        """Render a whole assembly - every component must be signed off.

        This does not weaken the production gate, it applies it N times: a
        single unsigned or tampered component refuses the whole render, so an
        assembly view can never show a mix of released and draft geometry.

        Components are placed by their assembly 'at' translation. The v0
        assembly schema carries translation only, so a mechanism cannot be
        posed at an angle here; parts are shown in their declared positions.
        """
        action_id = self.log.open_action(
            "production", "generate_assembly_viewer",
            geometry_version=str(assembly_id), reason=str(reason))
        try:
            _check_reason(reason)
            spec = self.assemblies.get_assembly(assembly_id)
            components = []
            for i, comp in enumerate(spec["components"]):
                gid = comp["geometry_id"]
                sign_row = self.production.verify_sign_off(gid)   # THE LOCK, per part
                part = self.parts.get_part(gid)
                sdet = json.loads(sign_row["details_json"])
                components.append({
                    "index": i,
                    "geometry_id": gid,
                    "name": part["spec"]["name"],
                    "at": comp.get("at", [0, 0, 0]),
                    "properties": part["properties"],
                    "mesh": tessellate(part["spec"], linear_tol_mm),
                    "sign_off": {
                        "signed_off_by": sign_row["signed_off_by"],
                        "statement": sdet["statement"],
                        "token": sdet["token"],
                        "spec_digest": sdet["spec_digest"],
                        "signed_at": sign_row["timestamp"],
                        "log_id": sign_row["id"],
                    },
                })

            stack_rows = [r for r in self.log.rows(
                action="check_tolerance_stackup",
                geometry_version=str(assembly_id)) if r["result"] != "pending"]
            stackup = (json.loads(stack_rows[-1]["details_json"])
                       if stack_rows else {})

            payload = {
                "kind": "assembly",
                "assembly_id": assembly_id,
                "assembly_name": spec["name"],
                "components": components,
                "stackup": stackup,
            }
            three_js = THREE_JS_PATH.read_text(encoding="utf-8")
            html = (ASSEMBLY_TEMPLATE_PATH.read_text(encoding="utf-8")
                    .replace("%%THREE_JS%%", three_js)
                    .replace("%%PAYLOAD%%", base64.b64encode(
                        json.dumps(payload).encode()).decode()))
            out = Path(out_path) if out_path else (
                self.root / "production" / assembly_id / "assembly_viewer.html")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            size_kb = round(out.stat().st_size / 1024, 1)
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        details = {"viewer_path": str(out), "size_kb": size_kb,
                   "components": [c["geometry_id"] for c in components],
                   "triangles": sum(c["mesh"]["triangles"] for c in components),
                   "artifacts": []}
        self.log.close_action(action_id, "pass", details=details)
        return {**details, "action_id": action_id}

    def generate_viewer(self, geometry_id: str, reason: str,
                        out_path: str | Path | None = None,
                        linear_tol_mm: float = DEFAULT_LINEAR_TOL_MM) -> dict:
        action_id = self.log.open_action(
            "production", "generate_viewer", geometry_version=str(geometry_id),
            reason=str(reason))
        try:
            _check_reason(reason)
            sign_row = self.production.verify_sign_off(geometry_id)  # THE LOCK
            part = self.parts.get_part(geometry_id)
            sdet = json.loads(sign_row["details_json"])
            mesh = tessellate(part["spec"], linear_tol_mm)

            payload = {
                "geometry_id": geometry_id,
                "spec": part["spec"],
                "properties": part["properties"],
                "mesh": mesh,
                "sign_off": {
                    "signed_off_by": sign_row["signed_off_by"],
                    "statement": sdet["statement"],
                    "token": sdet["token"],
                    "spec_digest": sdet["spec_digest"],
                    "signed_at": sign_row["timestamp"],
                    "log_id": sign_row["id"],
                },
                "validation": self._validation_summary(geometry_id, sign_row),
                "sourcing": self._bom_summary(geometry_id),
            }

            three_js = THREE_JS_PATH.read_text(encoding="utf-8")
            html = (TEMPLATE_PATH.read_text(encoding="utf-8")
                    .replace("%%THREE_JS%%", three_js)
                    .replace("%%PAYLOAD%%",
                             base64.b64encode(
                                 json.dumps(payload).encode()).decode()))
            out = Path(out_path) if out_path else (
                self.root / "production" / geometry_id.replace("@", "_")
                / "viewer.html")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(html, encoding="utf-8")
            size_kb = round(out.stat().st_size / 1024, 1)
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        details = {"viewer_path": str(out), "size_kb": size_kb,
                   "triangles": mesh["triangles"],
                   "sign_off_id": sign_row["id"],
                   "artifacts": []}
        self.log.close_action(action_id, "pass", details=details)
        return {**details, "action_id": action_id}
