"""Concrete evaluation stages that drive the existing engineering engine.

This is the only module in `inventor/` that knows CadQuery, CalculiX, the
price book or the PartStore exist. Everything above it deals in Candidates
and metrics.

Design rule followed throughout: **the engine decides, the optimiser
proposes**. No stage here ever computes its own safety factor when the engine
can compute one, and no stage converts an engine refusal into a pass.

Metric naming is namespaced and stable, because constraints and objectives
refer to metrics by string:

    mass_kg, volume_mm3, area_mm2
    bbox_x_mm, bbox_y_mm, bbox_z_mm
    com_x_mm, com_y_mm, com_z_mm
    cost_usd
    sf.<limit_state>          e.g. sf.thermal_derated_yield
    max_von_mises_MPa, max_displacement_mm, stress_outlier_ratio
    nodes, elements
"""

from __future__ import annotations

import math
import time
from typing import Callable

from .. import geometry as _geom
from ..fea import FeaError
from ..geometry import GeometryError, SpecError
from ..mesh import MeshError
from .candidate import (Candidate, FailureClass, FailureRecord, Fidelity,
                        StageResult)
from .evaluate import EvalContext
from .requirements import Status, digest_of

# Limit-state name -> the failure class it represents. Extend rather than
# hard-code branching at every call site.
LIMIT_STATE_CLASS = {
    "yield_von_mises": FailureClass.YIELD,
    "thermal_derated_yield": FailureClass.THERMAL,
    "elastic_buckling": FailureClass.BUCKLING,
}

# The engine's own calibration: models with an artificial constraint
# singularity sat at 1.95-2.12 on 24 real runs, physically sound models at
# 1.00-1.20. Above this, a "failure" is evidence about the MODEL, not the
# design, and must not steer the search.
ARTIFACT_OUTLIER_RATIO = 1.9


class RuleStage:
    """L0 - the design space's own cheap feasibility rules.

    Microseconds. This is the filter that stops obviously-invalid geometry
    (web thicker than the flange, boss outside the plate) from ever reaching
    the geometry kernel, let alone a solver.
    """
    fidelity = Fidelity.L0_ANALYTIC
    thread_safe = True

    def __init__(self, name: str = "rules"):
        self.name = name

    def config_digest(self) -> str:
        return "rules-v1"

    def run(self, cand: Candidate, ctx: EvalContext) -> StageResult:
        violated = ctx.space.violations(cand.values)
        if not violated:
            return StageResult(self.name, self.fidelity, Status.VALID)
        return StageResult(
            self.name, self.fidelity, Status.INVALID,
            failures=[FailureRecord(
                failure_class=FailureClass.GEOMETRIC,
                message=f"design-space rule(s) violated: {', '.join(violated)}",
                contributing_variables=sorted(cand.values.keys()),
                fidelity=self.fidelity)])


class AnalyticStage:
    """L0 - a caller-supplied closed-form model.

    Deliberately generic. The audit's biggest gap was that nothing existed
    between "do nothing" and "build geometry", but the right analytic model is
    problem-specific: a beam, a pressure vessel and a linkage share no
    formula. Rather than hard-code beam theory into the framework (which would
    violate "do not overfit to one type of gear"), the model is injected and
    `inventor.models` supplies a small library of standard ones.

    The function returns a metrics dict, or raises to signal UNKNOWN.
    """
    fidelity = Fidelity.L0_ANALYTIC
    thread_safe = True

    def __init__(self, fn: Callable[[dict, EvalContext], dict],
                 name: str = "analytic", version: str = "v1"):
        self.fn = fn
        self.name = name
        self.version = version

    def config_digest(self) -> str:
        return digest_of([self.name, self.version, getattr(self.fn, "__name__", "fn")])

    def run(self, cand: Candidate, ctx: EvalContext) -> StageResult:
        metrics = self.fn(cand.values, ctx)
        return StageResult(self.name, self.fidelity, Status.VALID,
                           metrics=dict(metrics),
                           provenance={"model": self.name, "version": self.version})


class GeometryStage:
    """L1 - build the real solid and take exact mass properties.

    ~24 ms measured. Uses `geometry.apply_changes` so the existing dot-path
    edit machinery is reused rather than reimplemented, and the resulting spec
    is stored on the candidate so a promoted design can be materialised later
    with byte-identical geometry.

    SpecError vs GeometryError is respected as the engine defines it:
    a malformed spec is genuinely INFEASIBLE, a kernel failure on a
    well-formed spec is UNKNOWN. Conflating them would let a kernel limitation
    delete a legitimate region of the design space.
    """
    fidelity = Fidelity.L1_GEOMETRY
    thread_safe = True

    def __init__(self, name: str = "geometry",
                 spec_builder: Callable[[dict, EvalContext], dict] | None = None):
        self.name = name
        self.spec_builder = spec_builder

    def config_digest(self) -> str:
        return digest_of(["geometry-v1",
                          getattr(self.spec_builder, "__name__", None)])

    def build_spec(self, values: dict, ctx: EvalContext) -> dict:
        if self.spec_builder is not None:
            return self.spec_builder(values, ctx)
        if ctx.base_spec is None:
            raise SpecError("GeometryStage needs a base_spec or a spec_builder")
        changes = ctx.space.to_spec_changes(values)
        spec, _ = _geom.apply_changes(ctx.base_spec, changes) if changes else (dict(ctx.base_spec), [])
        return spec

    def run(self, cand: Candidate, ctx: EvalContext) -> StageResult:
        try:
            spec = self.build_spec(cand.values, ctx)
            solid = _geom.build(spec)
            props = _geom.mass_properties(spec, solid)
        except SpecError as exc:
            return StageResult(
                self.name, self.fidelity, Status.INVALID,
                failures=[FailureRecord(
                    failure_class=FailureClass.GEOMETRIC,
                    message=f"spec is not buildable: {exc}",
                    contributing_variables=sorted(cand.values.keys()),
                    fidelity=self.fidelity)])
        except GeometryError as exc:
            return StageResult(
                self.name, self.fidelity, Status.UNKNOWN,
                failures=[FailureRecord(
                    failure_class=FailureClass.NUMERICAL,
                    message=f"geometry kernel could not build a well-formed "
                            f"spec: {exc}",
                    fidelity=self.fidelity, trustworthy=False)])

        cand.spec = spec
        cand.spec_digest = props["spec_digest"]
        size = props["bbox_mm"]["size"]
        com = props["center_of_mass_mm"]
        metrics = {
            "volume_mm3": props["volume_mm3"],
            "area_mm2": props["area_mm2"],
            "bbox_x_mm": size[0], "bbox_y_mm": size[1], "bbox_z_mm": size[2],
            "bbox_max_mm": max(size),
            "com_x_mm": com[0], "com_y_mm": com[1], "com_z_mm": com[2],
        }
        if "mass_kg_estimate" in props:
            metrics["mass_kg"] = props["mass_kg_estimate"]
        return StageResult(self.name, self.fidelity, Status.VALID,
                           metrics=metrics,
                           provenance={"spec_digest": props["spec_digest"]})


class CostStage:
    """L1 - cost from a caller-supplied model.

    The price book holds real captured prices for Bolt Depot fasteners and a
    few OnlineMetals steel bars only; it has no 6061 stock. Rather than invent
    material prices (which the sourcing layer explicitly refuses to do), the
    cost model is injected and must document its own basis. `generate_bom`
    stays sign-off gated; the pure pricing helpers in `sourcing` are not, so a
    model built on them is legitimate at L1.
    """
    fidelity = Fidelity.L1_GEOMETRY
    thread_safe = True

    def __init__(self, fn: Callable[[dict, dict, EvalContext], dict],
                 name: str = "cost", version: str = "v1"):
        self.fn = fn
        self.name = name
        self.version = version

    def config_digest(self) -> str:
        return digest_of([self.name, self.version, getattr(self.fn, "__name__", "fn")])

    def run(self, cand: Candidate, ctx: EvalContext) -> StageResult:
        metrics = self.fn(cand.values, cand.result.metrics, ctx)
        return StageResult(self.name, self.fidelity, Status.VALID,
                           metrics=dict(metrics),
                           provenance={"model": self.name, "version": self.version})


class FeaStage:
    """L2/L3 - materialise the candidate and run the real solver.

    NOT thread-safe, and says so. `PartStore._next_part_number()` allocates by
    globbing the parts directory, and `ActionLog` holds one SQLite connection;
    running these concurrently would race on both. The Evaluator honours
    `thread_safe = False` by falling back to serial execution rather than
    corrupting the audit log.

    Materialisation is deliberate and rate-limited by the optimiser: only
    candidates promoted this far become real parts with real log rows. A
    100,000-candidate sweep would otherwise create 100,000 directories.
    """
    thread_safe = False

    def __init__(self, case_builder: Callable[[Candidate, EvalContext], dict],
                 analysis: str = "static", mesh_mm: float | None = None,
                 fidelity: Fidelity = Fidelity.L3_HIGH_FEA,
                 name: str | None = None, n_modes: int = 3):
        if analysis not in ("static", "buckling"):
            raise ValueError("analysis must be 'static' or 'buckling'")
        self.case_builder = case_builder
        self.analysis = analysis
        self.mesh_mm = mesh_mm
        self.fidelity = fidelity
        self.n_modes = n_modes
        self.name = name or f"fea_{analysis}_L{int(fidelity)}"
        self._materialized: dict[str, str] = {}   # spec_digest -> geometry_id

    def config_digest(self) -> str:
        return digest_of([self.analysis, self.mesh_mm, int(self.fidelity),
                          self.n_modes,
                          getattr(self.case_builder, "__name__", "case")])

    # -- materialisation ------------------------------------------------
    def materialize(self, cand: Candidate, ctx: EvalContext) -> str:
        if cand.geometry_id:
            return cand.geometry_id
        if cand.spec is None:
            raise FeaError("candidate has no spec; run GeometryStage first")
        digest = cand.spec_digest or _geom.spec_digest(cand.spec)
        if digest in self._materialized:
            cand.geometry_id = self._materialized[digest]
            return cand.geometry_id
        out = ctx.engine.create_part(
            cand.spec,
            reason=(f"optimisation candidate {cand.candidate_id} "
                    f"(gen {cand.generation}, via {cand.operator}) promoted to "
                    f"{self.fidelity.label} evaluation"))
        cand.geometry_id = out["geometry_id"]
        self._materialized[digest] = cand.geometry_id
        return cand.geometry_id

    # -- classification -------------------------------------------------
    @staticmethod
    def _classify(case: dict, out: dict) -> FailureRecord:
        ls = case["limit_state"]["name"]
        cls = LIMIT_STATE_CLASS.get(ls, FailureClass.UNKNOWN)
        ratio = out.get("stress_outlier_ratio")
        trustworthy = True
        msg = (f"{ls}: SF={out.get('safety_factor')} < required "
               f"{case['limit_state']['required_SF']}")
        if ratio is not None and ratio > ARTIFACT_OUTLIER_RATIO:
            # The peak is very likely a constraint/mesh singularity. Recording
            # it as a genuine structural failure would teach the optimiser
            # that a perfectly good region of the design space is unsafe.
            cls = FailureClass.NUMERICAL
            trustworthy = False
            msg += (f" - BUT stress outlier ratio {ratio:.2f} exceeds "
                    f"{ARTIFACT_OUTLIER_RATIO}, so the peak is probably a "
                    f"model artifact, not the design")
        return FailureRecord(
            failure_class=cls, metric=f"sf.{ls}", message=msg,
            location_mm=out.get("max_von_mises_at_mm"),
            actual=out.get("safety_factor"),
            required=case["limit_state"]["required_SF"],
            trustworthy=trustworthy)

    def run(self, cand: Candidate, ctx: EvalContext) -> StageResult:
        if ctx.engine is None:
            return StageResult(self.name, self.fidelity, Status.UNKNOWN,
                               warnings=["no engine bound; FEA not run"])
        case = self.case_builder(cand, ctx)
        if self.mesh_mm is not None:
            case = dict(case)
            case["mesh"] = dict(case.get("mesh", {}))
            case["mesh"]["max_size_mm"] = self.mesh_mm
        ls = case["limit_state"]["name"]

        t0 = time.perf_counter()
        try:
            gid = self.materialize(cand, ctx)
            if self.analysis == "static":
                out = ctx.engine.run_fea_static(
                    gid, case, reason=(
                        f"optimisation: {ls} on candidate {cand.candidate_id} "
                        f"at {self.fidelity.label}"))
            else:
                out = ctx.engine.run_fea_buckling(
                    gid, case, reason=(
                        f"optimisation: {ls} on candidate {cand.candidate_id} "
                        f"at {self.fidelity.label}"), n_modes=self.n_modes)
        except MeshError as exc:
            # Measured during the audit: a part that meshes at 3mm and 8mm can
            # fail the Jacobian gate at 5mm. Non-monotonic. This says nothing
            # about whether the DESIGN is good.
            return StageResult(
                self.name, self.fidelity, Status.UNKNOWN,
                seconds=time.perf_counter() - t0,
                failures=[FailureRecord(
                    failure_class=FailureClass.NUMERICAL,
                    message=f"mesh refused: {exc}",
                    fidelity=self.fidelity, trustworthy=False)],
                warnings=["mesh failure is not evidence of infeasibility"])
        except (FeaError, Exception) as exc:
            return StageResult(
                self.name, self.fidelity, Status.UNKNOWN,
                seconds=time.perf_counter() - t0,
                failures=[FailureRecord(
                    failure_class=FailureClass.NUMERICAL,
                    message=f"{type(exc).__name__}: {exc}",
                    fidelity=self.fidelity, trustworthy=False)])

        seconds = time.perf_counter() - t0
        sf = out.get("safety_factor")
        metrics = {f"sf.{ls}": (float(sf) if isinstance(sf, (int, float)) else None)}
        for k_out, k_metric in (("max_von_mises_MPa", "max_von_mises_MPa"),
                                ("max_displacement_mm", "max_displacement_mm"),
                                ("stress_outlier_ratio", "stress_outlier_ratio"),
                                ("allowable_MPa", "allowable_MPa")):
            if out.get(k_out) is not None:
                metrics[k_metric] = out[k_out]

        failures, warnings = [], []
        if out.get("stress_outlier_warning"):
            warnings.append(out["stress_outlier_warning"])
        status = Status.VALID
        if out.get("result") == "fail":
            rec = self._classify(case, out)
            failures.append(rec)
            rec.fidelity = self.fidelity
            # An untrustworthy peak leaves the candidate UNKNOWN rather than
            # INVALID: we did not establish that it fails, only that the model
            # misbehaved.
            status = Status.INVALID if rec.trustworthy else Status.UNKNOWN

        return StageResult(
            self.name, self.fidelity, status, metrics=metrics,
            failures=failures, warnings=warnings, seconds=seconds,
            provenance={"geometry_id": cand.geometry_id, "limit_state": ls,
                        "analysis": self.analysis,
                        "run_dir": out.get("run_dir"),
                        "action_id": out.get("action_id")})


class CallableStage:
    """Escape hatch for capabilities not yet given a first-class adapter
    (assembly mass properties, kinematics, sourcing BOMs, thermal models).

    Explicitly an ADAPTER BOUNDARY, not a pretence that the subsystem is
    integrated: the function receives the candidate and context and returns a
    StageResult, so an integration can be added incrementally without the
    framework claiming capability it does not have.
    """

    def __init__(self, fn: Callable[[Candidate, EvalContext], StageResult],
                 name: str, fidelity: Fidelity, version: str = "v1",
                 thread_safe: bool = True):
        self.fn = fn
        self.name = name
        self.fidelity = fidelity
        self.version = version
        self.thread_safe = thread_safe

    def config_digest(self) -> str:
        return digest_of([self.name, self.version,
                          getattr(self.fn, "__name__", "fn")])

    def run(self, cand: Candidate, ctx: EvalContext) -> StageResult:
        return self.fn(cand, ctx)
