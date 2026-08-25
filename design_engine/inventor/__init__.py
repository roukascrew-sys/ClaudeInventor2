"""ClaudeInventor design-intelligence layer.

A search and optimisation layer that sits ABOVE the deterministic engineering
engine in `design_engine/`. It proposes designs; the engine decides whether
they are valid. Nothing in here computes a safety factor that the engine
could compute, and nothing in here can turn an engine refusal into a pass.

    RequirementSet   what "good" and "not allowed" mean, formally
    DesignSpace      what may vary, with dependencies and cheap rules
    Candidate        one proposal, with lineage and evidence
    Evaluator        staged, cached, fidelity-tagged evaluation
    Optimizer        proposes candidates; never judges them
    pareto           dominance, frontier, archetypes
    analysis         failure memory, sensitivity, robustness
    explain          why this design, and what it costs you

Typical use:

    from design_engine import DesignEngine
    from design_engine.inventor import (
        Constraint, DesignSpace, DesignVariable, Evaluator, EvalContext,
        Objective, OptimizationConfig, OptimizationRun, RequirementSet,
        EvolutionarySearch, Op, Sense, VarType,
    )

The existing engineering API is untouched: `DesignEngine.create_part`,
`run_fea_static`, `sign_off` and the rest behave exactly as before, and this
package is a pure addition.
"""

from .adapters import (AnalyticStage, CallableStage, CostStage, FeaStage,
                       GeometryStage, RuleStage, ARTIFACT_OUTLIER_RATIO)
from .analysis import (FailureMemory, Perturbation, RobustnessResult,
                       robustness, sensitivity, tolerance_perturbation)
from .candidate import (Candidate, EvaluationResult, FailureClass,
                        FailureRecord, Fidelity, StageResult)
from .evaluate import CODE_DIGEST, EvalContext, EvaluationCache, Evaluator
from .explain import compare, explain_candidate, render_run, render_text
from .models import (beam_screen, cantilever_point_load, channel_section,
                     euler_buckling, hollow_rect_section,
                     machining_cost_model, rect_section,
                     simply_supported_centre_load)
from .optimizers import (EvolutionarySearch, OptimizationConfig, Optimizer,
                         RandomSearch, make_optimizer, total_violation)
from .pareto import (archetypes, compare_fronts, crowding_distance,
                     dominates, hypervolume, non_dominated_sort, pareto_front)
from .requirements import (Constraint, ConstraintResult, Objective, Op,
                           Preference, ReqError, RequirementSet, Sense,
                           Status, digest_of)
from .run import GenerationRecord, OptimizationRun
from .space import (DesignSpace, DesignVariable, FeasibilityRule, VarType,
                    values_digest)

__all__ = [
    # requirements
    "Constraint", "ConstraintResult", "Objective", "Op", "Preference",
    "ReqError", "RequirementSet", "Sense", "Status", "digest_of",
    # space
    "DesignSpace", "DesignVariable", "FeasibilityRule", "VarType",
    "values_digest",
    # candidate
    "Candidate", "EvaluationResult", "FailureClass", "FailureRecord",
    "Fidelity", "StageResult",
    # evaluation
    "EvalContext", "EvaluationCache", "Evaluator", "CODE_DIGEST",
    # stages
    "AnalyticStage", "CallableStage", "CostStage", "FeaStage",
    "GeometryStage", "RuleStage", "ARTIFACT_OUTLIER_RATIO",
    # models
    "beam_screen", "cantilever_point_load", "channel_section",
    "euler_buckling", "hollow_rect_section", "machining_cost_model",
    "rect_section", "simply_supported_centre_load",
    # optimisation
    "EvolutionarySearch", "OptimizationConfig", "Optimizer", "RandomSearch",
    "make_optimizer", "total_violation",
    # pareto
    "archetypes", "compare_fronts", "crowding_distance", "dominates",
    "hypervolume", "non_dominated_sort", "pareto_front",
    # analysis
    "FailureMemory", "Perturbation", "RobustnessResult", "robustness",
    "sensitivity", "tolerance_perturbation",
    # run + explain
    "GenerationRecord", "OptimizationRun",
    "compare", "explain_candidate", "render_run", "render_text",
]
