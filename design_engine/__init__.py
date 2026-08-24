"""design_engine — deterministic tool layer (Phase 1).

The orchestrator (Claude, via Claude Code) calls these functions directly.
Every mutating call requires a `reason`; every action writes to the FRACAS
log before it runs and finalizes pass/fail after. The log is the source of
truth — reports and images are generated from it, never authored separately.

    from design_engine import DesignEngine
    eng = DesignEngine("data")
    part = eng.create_part(spec, reason="initial bracket per REQ-3")
    edited = eng.edit_part(part["geometry_id"], {"features.0.x": 25},
                           reason="clearance fix for failure #12")
    stack = eng.check_tolerance_stackup(assembly_id)
"""

from pathlib import Path

from .assembly import AssemblyStore
from .geometry import GeometryError, SpecError
from .log import ActionLog
from .parts import PartNotFound, PartStore

__all__ = ["DesignEngine", "ActionLog", "SpecError", "GeometryError", "PartNotFound"]


_DEFAULT_CCX = Path(__file__).parent.parent / "tools" / "CalculiX-2.23.0-win-x64" / "bin" / "ccx.exe"


class DesignEngine:
    """Facade wiring the log + stores + validation tools to one data root."""

    def __init__(self, root: str | Path = "data", ccx_path: str | Path | None = None):
        self.root = Path(root)
        self.log = ActionLog(self.root / "design_engine.db")
        self.parts = PartStore(self.root, self.log)
        self.assemblies = AssemblyStore(self.root, self.log, self.parts)
        from .fea import ValidationTools
        from .production import ProductionTools
        from .sourcing import SourcingTools
        self.validation = ValidationTools(
            self.root, self.log, self.parts, ccx_path or _DEFAULT_CCX)
        self.production = ProductionTools(self.root, self.log, self.parts)
        self.sourcing = SourcingTools(
            self.root, self.log, self.parts, self.production)

    # Phase 1 contracts
    def create_part(self, spec: dict, reason: str) -> dict:
        return self.parts.create_part(spec, reason)

    def edit_part(self, geometry_id: str, changes: dict, reason: str,
                  addresses_failure_id: int | None = None) -> dict:
        return self.parts.edit_part(geometry_id, changes, reason,
                                    addresses_failure_id=addresses_failure_id)

    def run_fea_static(self, geometry_id: str, case: dict, reason: str) -> dict:
        return self.validation.fea_static(geometry_id, case, reason)

    def sign_off(self, geometry_id: str, signed_off_by: str, statement: str) -> dict:
        return self.production.sign_off(geometry_id, signed_off_by, statement)

    def export_production_package(self, geometry_id: str, reason: str) -> dict:
        return self.production.export_production_package(geometry_id, reason)

    def generate_bom(self, geometry_id: str, bom_spec: dict, reason: str,
                     budget_usd: float | None = None) -> dict:
        return self.sourcing.generate_bom(geometry_id, bom_spec, reason,
                                          budget_usd=budget_usd)

    def get_part(self, geometry_id: str) -> dict:
        return self.parts.get_part(geometry_id)

    def create_assembly(self, spec: dict, reason: str) -> dict:
        return self.assemblies.create_assembly(spec, reason)

    def check_tolerance_stackup(self, assembly_id: str) -> dict:
        return self.assemblies.check_tolerance_stackup(assembly_id)

    def generate_report(self, out_path: str | Path | None = None) -> Path:
        from .report import generate_report
        return generate_report(
            self.log, out_path or self.root / "report.html", data_root=self.root)
