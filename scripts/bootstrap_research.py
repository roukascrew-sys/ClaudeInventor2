"""Distil the 2026-08-28 research ingestion into durable vault notes.

Seven papers were read (multi-fidelity optimisation, multi-fidelity Bayesian
optimisation, MFO survey, pool-based active learning, reliability-based robust
design, goal-oriented adaptive FEM, ML topology optimisation). Most of what
they contain is method detail that belongs in the papers, not here.

What belongs here is the small set of statements that survive being separated
from their paper: claims that would change how ClaudeInventor decides what to
run, and that a future session would be worse off for having to re-derive.

Two rules were applied when deciding what became a note:

  IT MUST BE ACTIONABLE HERE   A principle that cannot touch a decision this
                               engine makes is a summary, not knowledge.
  IT MUST CARRY ITS STATUS     Every note states whether it is established by
                               its sources, inferred across them, or merely a
                               recommended direction — in the frontmatter as
                               `epistemic`, not buried in prose. Three of the
                               twelve are syntheses that appear in no single
                               paper; those are the most useful and the least
                               certain, and the vault has to say so.

The strongest notes are the ones where the literature and this project's own
recorded failures arrived at the same conclusion independently.

Re-runnable, like `bootstrap_vault.py`: `Vault.write` updates canonical notes
in place rather than forking duplicates.

    .venv\\Scripts\\python.exe scripts\\bootstrap_research.py [--root PATH]

RUN THIS AFTER `bootstrap_vault.py`, never before or instead.

It rewrites two notes that `bootstrap_vault.py` also owns —
`Multi Fidelity Evaluation` and `Screening models need automatic calibration` —
as supersets carrying the research findings. Running the vault script
afterwards reverts them, because `Vault.write` replaces a note wholesale.
"""

import argparse
import importlib.util
from pathlib import Path

# Load vault.py BY PATH, for the same reason bootstrap_vault.py does: the
# package __init__ drags in the CAD kernel, and the reasoning layer must keep
# working when the kernel does not.
_VAULT = Path(__file__).parent.parent / "design_engine" / "vault.py"
_spec = importlib.util.spec_from_file_location("ci_vault", _VAULT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Vault = _mod.Vault

DEFAULT_ROOT = Path.home() / "Downloads" / "ClaudeInventor"

# The seven sources. Two forms deliberately: the long one is the citation of
# record and goes in `sources:` frontmatter, where it is read as data; the
# short one goes inline, where a full citation mid-sentence is unreadable.
# Phase 8 gives each paper its own canonical note.
SRC1 = "Grbcic, Muller & de Jong (2024), Efficient Inverse Design Optimization through Multi-fidelity Simulations, Machine Learning, and Boundary Refinement Strategies, arXiv:2312.03654"
SRC2 = "Do & Zhang (2025), Multi-Fidelity Bayesian Optimization - A Review, arXiv:2311.13050"
SRC3 = "Li & Li (2024), Multi-Fidelity Methods for Optimization - A Survey, arXiv:2402.09638"
SRC4 = "Elsas, Casaprima & Menezes (2020), Accelerating engineering design by automatic selection of simulation cases through Pool-Based Active Learning, arXiv:2009.01420"
SRC5 = "Verma, Kumar, Obayashi & Alam (2022), Reliability-Based Robust Design Optimization Method for Engineering Systems with Uncertainty Quantification, arXiv:2210.07521"
SRC6 = "Becker, Gantner, Innerberger & Praetorius (2022), Goal-Oriented Adaptive Finite Element Methods with Optimal Computational Complexity, arXiv:2101.11407"
SRC7 = "Shin, Shin & Kang, Topology Optimization via Machine Learning and Deep Learning - A Review"

ARX1 = "Grbcic et al."
ARX2 = "Do & Zhang"
ARX3 = "Li & Li"
ARX4 = "Elsas et al."
ARX5 = "Verma et al."
ARX6 = "Becker et al."
ARX7 = "Shin et al."

TAGS = ["claudeinventor", "research", "principle"]

# Canonical note titles for the seven papers, so principle notes and paper
# notes can link to each other without either side hard-coding a filename.
P1 = "Multi-Fidelity Inverse Design with Boundary Refinement"
P2 = "Multi-Fidelity Bayesian Optimization Review"
P3 = "Multi-Fidelity Methods for Optimization Survey"
P4 = "Pool-Based Active Learning for Simulation Selection"
P5 = "Reliability-Based Robust Design Optimization"
P6 = "Goal-Oriented Adaptive FEM with Optimal Complexity"
P7 = "Topology Optimization via Machine Learning Review"

PAPER_TAGS = ["claudeinventor", "research", "paper"]


def build_principles(v: Vault) -> None:
    v.ensure_structure()

    # =================================================== surrogates and cost
    v.write(
        "07_Research/Methods",
        "A surrogate that screens may be sloppy, one that ranks may not",
        type="method", status="active", confidence="medium", tags=TAGS,
        extra={"epistemic": "Supported inference - appears in no single source",
               "sources": [SRC1, SRC2, SRC3]},
        body=f"""**This distinction is drawn in no single source.** Each paper
below is Established by source; the claim that the *use* is what decides
whether a weak surrogate is safe is this project's reading across three of
them, and it is the reason they appear to disagree about accuracy.

The same cheap model can be sound or dangerous depending on what the search
asks it to do, and the papers disagree mainly because they are asking
different questions of it.

**Rejecting is a one-sided decision.** {ARX1} trains on roughly 500 samples
and states plainly that the ML model does not have to be highly accurate,
because a candidate is only discarded when it misses by more than a threshold
scaled to the model's own RMSE, and the surviving region is then expanded by a
margin. Errors inside the margin cost search time; they do not cost answers.

**Ranking is two-sided.** {ARX2} states it directly: additional low-fidelity
observations *could slow the convergence if there is weak correlation between
the HF and LF models near the solution, which misdirects the search*. {ARX3}
reaches it structurally instead — space-mapping methods without a correlation
gate carry no convergence guarantee even to a local high-fidelity optimum.
Ranking has no margin to hide inside: if the cheap model orders two candidates
wrongly, the wrong one is promoted and the right one is never seen again.

Note **where** the correlation has to hold. {ARX2} says *near the solution* —
a model with good correlation on average, and bad correlation exactly where the
optimum sits, is the dangerous case, and an aggregate correlation score will
not reveal it.

**Where this bites here.** The L0 analytic model was measured at 76-96% error
and was used for *both* jobs — to screen, and to order the Pareto frontier that
came out of the run. Only the first use was defensible. See
[[Screening models are optimistic in the unsafe direction]].

The practical form: a surrogate may set the *boundary* of what gets solved. It
may not set the *order* of what gets reported.""",
        links=["Screening models are optimistic in the unsafe direction",
               "Inaccuracy is acceptable only when a margin absorbs it",
               "A weakly correlated cheap model is worse than none",
               "Multi Fidelity Evaluation", "Screened is not validated"] + [P1, P2, P3])

    v.write(
        "07_Research/Methods",
        "Inaccuracy is acceptable only when a margin absorbs it",
        type="method", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Established by source", "sources": [SRC1]},
        body=f"""{ARX1} gets useful work out of a deliberately small surrogate by
never letting its error reach a decision unabsorbed. Two devices do the
absorbing: a skip threshold scaled by the model's measured error, and a
refined-bounds expansion factor (alpha = 1.3) applied before the region is
handed to the expensive optimiser.

This is the mechanism behind
[[A surrogate that screens may be sloppy, one that ranks may not]] — the reason
a weak model is tolerable there and not elsewhere.

The inverse is the warning. A cheap model whose error is *not* absorbed by an
explicit stated margin is being trusted, whatever the surrounding prose says.
This engine has one of those: `KT_ROOT_JUNCTION = 1.85` is a hand-fitted
correction with no margin attached to it and no error bound behind it.

The test to apply to any new screening step: name the quantity that absorbs its
error. If nothing does, the step is a load-bearing assumption.""",
        links=["A surrogate that screens may be sloppy, one that ranks may not",
               "The skip threshold must be derived from measured error",
               "Calibrate only against converged results", "Refuse rather than invent"] + [P1])

    v.write(
        "07_Research/Methods",
        "The skip threshold must be derived from measured error",
        type="method", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Established by source - three independent papers",
               "sources": [SRC1, SRC3, SRC4]},
        body=f"""Three of the seven papers reach the same construction from
different directions, which is the strongest signal in the whole set.

- {ARX1} skips the expensive evaluation when the surrogate's predicted miss
  exceeds `delta = c * eps_M`, where `eps_M` is the model's measured RMSE. The
  gate moves as the model improves.
- {ARX3} reports gating on both the low-fidelity model's *variance* and its
  *correlation* with the high-fidelity one, and notes that space-mapping
  approaches without such a gate carry no convergence guarantee.
- {ARX4} bounds its error by an implied uncertainty calibrated against a
  reference set that was solved in full.

None of them lets a human choose the number. The threshold is a measurement.

**This engine already owns the measurement and does not use it.**
`KnowledgeBase.correction()` (`design_engine/inventor/knowledge.py:279`)
harvests calibration pairs from promoted candidates, computes a geometric-mean
factor, returns `None` below three observations rather than a neutral 1.0, and
flags itself untrustworthy when the ratios disagree by more than 1.5x. Nothing
consults it when deciding what to solve.

That gap is the subject of
[[Screening models need automatic calibration]], and the general form of it is
[[Check what the engine already measures before adding]].""",
        links=["Screening models need automatic calibration",
               "Check what the engine already measures before adding",
               "Engineering Knowledge Base",
               "Inaccuracy is acceptable only when a margin absorbs it"] + [P1, P3, P4])

    v.write(
        "07_Research/Methods",
        "A weakly correlated cheap model is worse than none",
        type="method", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Established by source, and observed here",
               "sources": [SRC2, SRC3]},
        body=f"""Not *less useful* — **actively harmful**, and the papers say so
without hedging.

{ARX2}, verbatim: additional low-fidelity observations *could slow the
convergence if there is weak correlation between the HF and LF models near the
solution, which misdirects the search*. {ARX3} makes the same point
structurally: space-mapping methods that lack a correlation gate have no
convergence guarantee even to a local high-fidelity optimum.

**The correlation that matters is local.** A rung that tracks the truth well
across the bulk of the design space and badly near the optimum is worse than
one that is uniformly mediocre, because the aggregate score looks fine. {ARX3}
records Kendall's rank correlation coefficient as the measure used for this,
which is the right choice here for an independent reason: promotion is a
**ranking** decision, and Kendall's tau scores order agreement rather than
magnitude agreement.

The intuition that a rough model is a free head start is wrong. A rough model
spends the budget it saves on solving the wrong candidates, and it spends it
invisibly, because the candidates it discarded are never evaluated and so never
contradict it.

**Observed here, independently of the papers.** The jetpack search preferred
designs that a later solve showed to be weaker than screened — a 23% mass win
was reported from screening numbers and had to be retracted. See
[[Screened is not validated]] and
[[Screening models are optimistic in the unsafe direction]].

The consequence for design: before adding a fidelity rung, measure its rank
correlation against the rung above it on cases already solved. A rung that
cannot demonstrate correlation should not be allowed to steer.""",
        links=["Screened is not validated",
               "Screening models are optimistic in the unsafe direction",
               "A surrogate that screens may be sloppy, one that ranks may not",
               "Numerical artifacts must not steer search"] + [P2, P3])

    v.write(
        "04_Optimization/Experiments",
        "Simulation cost depends on the design",
        type="method", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Established by source as a stated limitation; the "
                            "local cost model is Observed",
               "sources": [SRC2, SRC3]},
        body=f"""Most of the multi-fidelity literature assumes each fidelity level
has a fixed cost `c(t)` independent of the design point `x`. {ARX2} flags its
own version of this assumption as not realistic, and {ARX3} inherits the same
simplification across most of the methods it surveys.

For a structural validator the assumption is plainly false. Solve time is
driven by node count, which is driven by geometry: a thin web meshed to the
same target size costs far more than a stubby one, and a fillet added for
stress reasons multiplies the local element count.

**ClaudeInventor is ahead of the literature here, by accident.**
`KnowledgeBase.predict_solve()`
(`design_engine/inventor/knowledge.py:434`) fits solve time against node count
from real logged runs — measured on this repository as roughly
`t ~ 40.1 s * (nodes / 100k) ** 1.661` over 39 runs. Cost is already a function
of the candidate.

What is missing is the consumer. Promotion order is not currently ranked by
information gained per second, which is the decision this measurement exists to
inform. Same shape of gap as
[[The skip threshold must be derived from measured error]].""",
        links=["Multi Fidelity Evaluation", "Engineering Knowledge Base",
               "The skip threshold must be derived from measured error",
               "Check what the engine already measures before adding",
               "Solver memory bounds mesh refinement"] + [P2, P3])

    # ============================================ where computation is spent
    v.write(
        "07_Research/Methods",
        "Refine where the question is, not everywhere",
        type="method", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Established by source; the local instance is Observed",
               "sources": [SRC6]},
        body=f"""{ARX6} is built on the observation that when the answer wanted is
a single functional of the solution — a peak stress at one junction, a
compliance, a flux through one face — it is not necessary, and may actively
waste computation, to approximate the solution accurately over the whole
domain. Refinement is steered by a dual problem that weights error by its
influence on the goal.

**This is exactly the shape of the blocker on mesh convergence here.** A1 is
stalled because refining a 1280 mm frame to resolve a peak at one 19 mm
junction reached a 6.1 GB working set and crashed CalculiX with `0xC0000005`.
Almost none of those elements were near the quantity being asked about. See
[[Mesh convergence is unverified]] and
[[Solver memory bounds mesh refinement]].

Full goal-oriented adaptivity needs a dual solve and an error estimator this
engine does not have. **Submodelling is the cheap version of the same idea** —
solve the frame coarsely, then re-solve a small region around the junction with
displacements from the coarse run as boundary conditions — and it needs no new
theory, only a driver.

Before either is worth building, read
[[Adaptivity cannot rescue a singular goal]]: this project's peak sits on
geometry where the goal functional is unbounded, and neither method terminates
there.""",
        links=["Mesh convergence is unverified", "Solver memory bounds mesh refinement",
               "Adaptivity cannot rescue a singular goal", "Meshing is non-monotonic"] + [P6])

    v.write(
        "03_Engineering/FEA",
        "Adaptivity cannot rescue a singular goal",
        type="method", status="proposed", confidence="medium", tags=TAGS,
        extra={"epistemic": "Supported inference - not stated in either source, "
                            "and NOT yet validated in ClaudeInventor",
               "sources": [SRC6]},
        body=f"""Goal-oriented adaptive refinement drives the estimated error in a
goal functional below a tolerance. If the goal functional does not exist, that
loop does not terminate — it refines forever, reporting steady progress.

At a sharp re-entrant corner, linear elastic stress goes as `r ** (lambda - 1)`
with `lambda ~ 0.5445` at 270 degrees (Williams 1952), so peak von Mises is
unbounded. Every refinement genuinely reduces the discretisation error and
genuinely raises the peak. Nothing in the adaptive machinery notices, because
from inside the loop an unbounded goal and a slowly-converging one look alike.

Both halves are solid; **the join is inference and belongs to this project, not
to {ARX6}**, which assumes a bounded linear goal functional throughout.

**Why it matters here.** This engine already found a reported SF of 3.844 whose
peak sat 1.28 mm from exactly such a corner, and
`design_engine/singularity.py` now detects the condition on the CAD solid. The
sequencing that follows is the useful part:

1. Classify the peak against the geometry (`classify_peak`).
2. If SINGULAR, **fix the geometry** — the answer is a fillet, not a finer mesh.
3. Only then is adaptive or submodelled refinement meaningful.

Running step 3 before step 2 burns the budget and returns a number that rises
with every euro spent on it.

**Not yet validated:** no adaptive loop exists here to observe this failing in.
The claim rests on the singularity result plus the structure of the method.""",
        links=["Peak stress at a sharp re-entrant corner cannot converge",
               "Refine where the question is, not everywhere",
               "The outlier ratio does not detect geometric singularities",
               "Mesh convergence is unverified", "UNKNOWN is not a pass"] + [P6])

    v.write(
        "04_Optimization/Surrogates",
        "Calibrate only against converged results",
        type="method", status="active", confidence="medium", tags=TAGS,
        extra={"epistemic": "Supported inference from the surrogate literature "
                            "plus this project's own recorded failures",
               "sources": [SRC1, SRC3]},
        body="""**No source states this rule.** The surrogate literature assumes
its high-fidelity reference is correct and does not ask what happens when it is
not; the consequence below is inferred from that assumption meeting this
project's own recorded failures.

A correction factor fitted against an unconverged high-fidelity result does not
remove error. It **launders discretisation error into the screening layer**,
where it becomes invisible: from then on the cheap model agrees with the
expensive one, and the agreement is evidence of nothing.

This is the failure mode that makes surrogate calibration different from
ordinary curve fitting. A badly fitted surrogate is noisy and looks noisy. A
surrogate calibrated to a wrong reference is quiet and looks correct.

**Live instance.** `KT_ROOT_JUNCTION = 1.85` in the jetpack design script was
fitted to two solved peaks. A2 later showed both sat on a geometric
singularity, so neither had a converged value to fit against. The constant now
carries a caution comment; it is not yet withdrawn, because withdrawing it
without a replacement would leave the screen with no junction concentration at
all.

The rule this yields, which the engine does not yet enforce:

> A calibration pair may only enter the knowledge base if its high-fidelity
> member carries a discretisation error bound.

That is currently unenforceable here for a blunt reason — **no result in this
project carries one**, because mesh convergence is still blocked. See
[[Mesh convergence is unverified]]. The rule is recorded now so that it is
built in when convergence becomes available, rather than retrofitted after the
knowledge base has already been populated with unbounded pairs.""",
        links=["Mesh convergence is unverified",
               "Peak stress at a sharp re-entrant corner cannot converge",
               "Screening models need automatic calibration",
               "Engineering Knowledge Base",
               "Inaccuracy is acceptable only when a margin absorbs it"] + [P1, P3])

    # ==================================================== uncertainty layer
    v.write(
        "03_Engineering/FEA",
        "Deterministic feasibility is not feasibility under uncertainty",
        type="method", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Established by source; the local instance is Observed",
               "sources": [SRC5]},
        body=f"""{ARX5} makes the point with a worked case, and the detail that
matters is which peak loses.

The test function is a weighted sum of two Gaussians: a **sharp** peak of
height **11** at (2, 2), and a **broader** peak of height **10** at (-2, -2).
Deterministically the answer is unambiguous — 11 beats 10, and (2, 2) is the
global optimum. Impose a normal input scatter of sigma = 0.1, propagate it, and
constrain the response standard deviation below 0.1, and the taller peak is
**classified infeasible**: it is too sharp to survive its own tolerances. The
robust optimum is the shorter, flatter one.

So this is not a case of uncertainty demoting a mediocre candidate. **It
rejects the deterministic winner**, and it does so because of the curvature
around the point rather than the value at it.

A gate evaluated at nominal inputs answers a question nobody asked. The
physical part is built from a material whose properties scatter, welded by a
process whose heat input varies, and loaded by a pilot whose mass is a range.

**This is not hypothetical here.** The frame's pass or fail depends on where in
`rho_o,haz` in [0.375, 0.50] the true HAZ softening lies — under every sourced
factor in that range the frame fails its own 3.0 gate (SF 2.317 down to 1.738;
it needs rho >= 0.647). One number was chosen; the answer belongs to the range.
See [[Every safety factor used a strength the joints do not have]].

**What the engine has and has not got.** `inventor/analysis.py:182` defines a general
`Perturbation`, but `tolerance_perturbation` (`inventor/analysis.py:194`) reaches design
variables only — geometry — and cannot perturb a material or process parameter.
Extending it to material inputs is a small change with an immediately useful
first question: how much of the frame's margin is real, and how much is the
choice of a softening factor.

**Distinguish this from a safety factor.** A safety factor is a scalar applied
to a nominal answer. Propagating uncertainty tells you the *shape* of the
answer, including whether the nominal point is near a cliff. They are not
substitutes.""",
        links=["Every safety factor used a strength the joints do not have",
               "6061-T6 Thermal Derating", "UNKNOWN is not a pass",
               "Constraints are gates, not preferences", "Refuse rather than invent"] + [P5])

    v.write(
        "04_Optimization/Experiments",
        "Select simulations for information value, not predicted performance",
        type="method", status="proposed", confidence="medium", tags=TAGS,
        extra={"epistemic": "Established by source; NOT yet validated in ClaudeInventor",
               "sources": [SRC4, SRC2]},
        body=f"""{ARX4} selects the next case to simulate by **maximum predictive
variance** — the point the surrogate is least sure about — rather than the
point it predicts will perform best. {ARX2} describes the same trade-off in
acquisition-function terms, where augmented expected improvement explicitly
prices uncertainty alongside expected gain.

The distinction is easy to lose. An optimiser that always evaluates its current
best candidate learns almost nothing per evaluation: it confirms a region it
already understood, and it never discovers that its model is wrong somewhere
else. Exploitation is cheap to justify one step at a time and expensive over a
campaign.

For a **validator**, the framing is sharper still. The purpose of an expensive
solve is not to find a better design; it is to **reduce uncertainty about
whether a design is acceptable**. The most valuable solve is therefore the one
whose outcome is least predictable, not the one most likely to pass.

**Status here: none of this is implemented.** Promotion is by screened rank.
The prerequisite is a surrogate that carries an uncertainty estimate, and
`correction()` currently carries a trustworthiness *flag* rather than a
variance. Gated behind that, and behind B4.

**Caveat worth keeping.** Pure uncertainty sampling can spend a budget mapping
regions that no acceptable design would ever occupy. The literature pairs it
with a feasibility screen for exactly this reason.""",
        links=["Engineering Knowledge Base", "Multi Fidelity Evaluation",
               "Screening models need automatic calibration", "Roadmap",
               "Simulation cost depends on the design"] + [P4, P2])

    # ======================================================== what belongs here
    v.write(
        "11_Lessons",
        "A method with no refusal path does not belong in this engine",
        type="lesson", status="active", confidence="medium", tags=TAGS,
        extra={"epistemic": "Recommended design direction - a project judgement, "
                            "not a claim any source makes",
               "sources": [SRC1, SRC3, SRC4, SRC7]},
        body=f"""Reading seven papers side by side surfaced a property that none of
them names but that sorts them cleanly: **can the method decline?**

- {ARX1} declines by threshold — a candidate outside `c * eps_M` is not judged.
- {ARX3} declines by correlation gate — an uncorrelated fidelity is not used.
- {ARX4} declines by uncertainty bound — a prediction outside the bound is not
  trusted.
- {ARX7}'s ML topology optimisation review describes no method that declines. A
  trained network returns a topology for any input. The review reports that
  MLTO accuracy *is still inferior to that of the conventional iterative TO*,
  that GAN-generated designs *do not guarantee stiffness*, and that structural
  disconnection of the generated result is a recurring failure mode addressed
  by adding physics to the loss rather than by refusing to answer.

  That last part is the distinction. The field's response to unreliable output
  is to make it **more often right**, not to make it **say when it is wrong** —
  and those are different properties. That no reviewed method separates the two
  cases is my reading of the review, not a claim it makes.

For a search layer, an over-confident method costs wasted compute. For a
**validator**, it is disqualifying: a component that cannot say UNKNOWN
converts every out-of-distribution input into a confident answer, and this
project's entire history is a record of confident answers that were wrong for
reasons the arithmetic could not see.

This is the acceptance criterion, and it applies to anything imported from the
literature, not only to machine learning:

> Before adopting a method, name the input for which it returns UNKNOWN. If
> there is no such input, it is a generator, and its output must pass through
> a validator that has one.

That does not bar ML topology optimisation from the project. It bars it from
the gate. Its proper place is upstream, proposing candidates the existing
limit-state machinery then judges — which is the same boundary already drawn by
[[The engine decides, the optimiser proposes]].""",
        links=["UNKNOWN is not a pass", "Refuse rather than invent",
               "The engine decides, the optimiser proposes",
               "Validation Philosophy", "Constraints are gates, not preferences"] + [P1, P3, P4, P7])

    v.write(
        "11_Lessons",
        "Check what the engine already measures before adding",
        type="lesson", status="active", confidence="high", tags=TAGS,
        extra={"epistemic": "Observed - counted in this repository on 2026-08-28",
               "sources": ["2026-08-28 research ingestion, phases 4-5"]},
        body="""Mapping seven papers onto this codebase produced a result that was
not the expected one. The highest-value items were **not new capabilities**.
They were measurements the engine already takes and then ignores when deciding
what to run:

| Already measured | Where | Reaches a decision? |
|---|---|---|
| Surrogate error, with a trustworthiness flag | `knowledge.py:279` `correction()` | no — called only from tests |
| Solve cost vs node count, from 39 real runs | `knowledge.py:434` `predict_solve()` | no — feeds `affordable()`, which is itself called only from tests |
| Coarse-vs-fine fidelity switch | `FeaStage.__init__(fidelity=, mesh_mm=)` | defined, never selected |

Verified 2026-08-28 by grepping call sites, not by reading intent: each of these
has a complete, tested implementation and a consumer chain that terminates in
the test suite.

**One of them is worse than "unconsumed".** Checking `data/knowledge.sqlite`
the same day: `calibrations` holds **0 rows**, so `correction()` would return
`None` even if something did call it. Its producer — `record()`, which harvests
pairs from a promoted candidate carrying one metric at two fidelities — has
never populated a row either.

Meanwhile the project **has** measured its screen error, 13 times, and stored
it as prose: `observations.reason` carries strings like
`Predicted sigma=164.0 MPa` next to the measured `max_von_mises_MPa`. The
measurements exist and are unreadable by code.

That sharpens the lesson rather than replacing it. The gap is not always
measure-then-ignore; sometimes it is **measure into the wrong column**. Both
look like a missing feature from outside, and neither is.

The L2 rung is the sharpest instance: `FeaStage` accepts `fidelity` and its own
docstring says L2/L3, and the rung is dead code used by exactly one test
fixture. Activating it is a **config change, not an implementation** — and the
absence of that rung is how a 76-96% screening error survived to shape a whole
Pareto frontier.

The general lesson, which cost most of a research pass to learn:

> When a paper recommends keying a decision on some quantity, first check
> whether this engine already computes that quantity. It frequently does. The
> gap is usually between measuring and *consuming*, and a gap of that shape
> looks exactly like a missing feature from the outside.

Related in spirit to [[Read the vault before deciding, not after]] — the same
failure of not looking first, one layer down in the code instead of the
notes.""",
        links=["Read the vault before deciding, not after",
               "Multi Fidelity Evaluation", "Engineering Knowledge Base",
               "The skip threshold must be derived from measured error",
               "Simulation cost depends on the design", "Roadmap"])

    # ============================================= updates to existing notes
    v.write(
        "04_Optimization/Surrogates", "Screening models need automatic calibration",
        type="open-question", status="active", confidence="high",
        tags=["claudeinventor", "research"],
        extra={"epistemic": "Observed gap; the sourcing added 2026-08-28 is "
                            "Established by source"},
        body=f"""The correction that took the jetpack screening model from 76-96%
error down to 1.8% was applied **by a human reading two FEA results and editing
a constant** in a design script.

That loop is exactly what a surrogate should own: maintain a correction from
observed (screened, measured) pairs, carry its own uncertainty, and refuse to
correct where it has no data.

**Half of it already exists.** `KnowledgeBase.correction()` harvests calibration
pairs automatically from any promoted candidate — any metric that appears at two
different fidelities — computes a geometric-mean factor, and returns `None`
rather than a neutral 1.0 below three observations. It also flags itself
untrustworthy when the ratios disagree by more than 1.5x.

**What is missing:** nothing consumes it yet. The evaluator does not ask the
knowledge base for a correction before screening, and the design scripts still
carry hand-edited constants.

**The hard part is not the fitting, it is the scoping.** A correction learned on
a T-junction beam must not silently be applied to a pressure vessel. The
`problem` field exists for this and is currently set by the caller, which is a
weak guarantee.

On the [[Roadmap]]: back-filling the pairs is NOW, wiring the gate onto them
is LATER, because a gate calibrated against unconverged references is worse
than no gate.

## What the 2026-08-28 research pass added

The missing consumer now has a **specified form** rather than an intention.
{ARX1}, {ARX3} and {ARX4} independently key the decision to solve on a
threshold derived from the model's own measured error — see
[[The skip threshold must be derived from measured error]]. That is the
interface `correction()` should present, and it is why the flag it already
returns is nearly, but not quite, enough: a threshold needs a magnitude, and the
flag is a boolean.

Two constraints on the eventual implementation, both from this pass:

- **Only converged references may enter the fit** —
  [[Calibrate only against converged results]]. Currently unsatisfiable here,
  because no result carries a discretisation error bound.
- **The scoping problem is the correlation problem.** {ARX3}'s correlation gate
  is the same guarantee the weak `problem` field is trying to give: a
  correction may only apply where the two fidelities have been shown to track
  each other. See [[A weakly correlated cheap model is worse than none]].

## What the 2026-08-29 back-fill found

The pairs were moved out of prose and into `calibrations`, and the answer is
not the one the plan assumed.

**15 pairs were recoverable, 12 after collapsing duplicates, and NO problem
family reaches three distinct pairs.** `correction()` still returns `None` for
every one of them. That is not a plumbing failure - it is the honest state of
the evidence. This project has never solved enough **distinct** designs within
one problem family to calibrate its screen, and the conclusion is robust to
how the families are cut: the largest is n=2 under any defensible grouping.

So the missing consumer was never the only blocker. Even wired, there is
nothing for it to consume yet, and **more solving does not fix it unless the
solves are of DIFFERENT designs in the SAME family**. Three near-identical
candidates from one search generation are one sample, not three - P0024, P0025
and P0026 all carried predicted 42.1 against measured 62.36, the same numbers
to four figures.

Two things the data says in passing, both worth knowing before anyone builds a
single "screening correction":

- The two buckling pairs disagree by **3.97x** (ratios 0.102 and 0.405).
- The screen errs in **opposite directions** for the two metrics -
  under-predicting stress, which is unsafe, while under-predicting buckling
  safety factor, which is conservative. One correction factor cannot serve
  both.

Populating the table also made a latent hazard live, now closed: an
**unscoped** `correction()` call that would average across different problems
is refused rather than answered. Pooling a ladder channel with a jetpack frame
gives a factor with more observations behind it and less meaning.""",
        links=["Roadmap", "Engineering Knowledge Base",
               "Screening models are optimistic in the unsafe direction",
               "The skip threshold must be derived from measured error",
               "Calibrate only against converged results",
               "A weakly correlated cheap model is worse than none",
               "Check what the engine already measures before adding"])

    v.write(
        "04_Optimization/Experiments", "Multi Fidelity Evaluation",
        type="method", status="active", confidence="high",
        tags=["claudeinventor", "research"],
        extra={"epistemic": "Observed costs; the gap analysis is Supported inference"},
        body=f"""Measured on this repository, not assumed:

| Level | Work | Cost | Throughput |
|---|---|---|---|
| L0 | rules + closed form, spec only | ~4 us | ~250,000/s |
| L1 | real solid + exact mass properties | ~24 ms | ~40/s |
| L2 | coarse mesh + solve | ~8 s | ~0.1/s |
| L3 | converged mesh + solve | 8-800 s | ~0.01/s |

Six orders of magnitude between L0 and L3. Staging is not an optimisation
nicety here; it is the only reason search is possible at all.

**The L2 rung is defined but unused, and that is a real gap.** Screening jumps
L1 to L3, which is how a 76% model error survived long enough to shape an
entire Pareto frontier. A coarse 8-second rung would have exposed it after a
handful of solves. In the [[Roadmap]] NEXT section.

## What the 2026-08-28 research pass added

**An intermediate fidelity is not a luxury; skipping it is how large screening
errors survive.** Every method surveyed in {ARX3} and {ARX2} assumes a ladder
whose rungs are close enough that the correction between them is small and
estimable. A single jump across six orders of magnitude gives the correction
nothing to interpolate: there is no rung at which the model is nearly right, so
its error is never small enough to be measured cheaply.

That reframes the L2 gap. It is not that L2 would save time — at ~8 s a solve it
mostly does not. It is that **L2 is where surrogate error becomes observable at
a price worth paying**, which is what makes
[[The skip threshold must be derived from measured error]] implementable at
all.

Three further consequences, each with its own note:

- Cost is a function of the design, not of the rung — [[Simulation cost depends on the design]]
- A rung must demonstrate correlation before it may steer — [[A weakly correlated cheap model is worse than none]]
- Screening may set the boundary, not the ranking — [[A surrogate that screens may be sloppy, one that ranks may not]]

Activating L2 is a configuration change: `FeaStage.__init__` already takes
`fidelity` and `mesh_mm`. See
[[Check what the engine already measures before adding]].""",
        links=["Design Engine", "Optimization Engine", "Roadmap",
               "Screening models are optimistic in the unsafe direction",
               "The skip threshold must be derived from measured error",
               "Simulation cost depends on the design",
               "A weakly correlated cheap model is worse than none",
               "A surrogate that screens may be sloppy, one that ranks may not",
               "Check what the engine already measures before adding"])


def build_papers(v: Vault) -> None:
    """One canonical note per paper.

    Distilled, not copied. `relevance` is this project's judgement of how much
    the paper can change a decision here, NOT a judgement of the paper — a
    rigorous result about a problem this engine does not have still rates low.
    """

    # ------------------------------------------------------------------ arx1
    v.write(
        "07_Research/Papers", P1,
        type="research", status="active", confidence="high", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx1",
               "epistemic": "Sections through Limitations are Established by source. ClaudeInventor Implications and Implementation Opportunities are Recommended design direction, Not yet validated in ClaudeInventor", "relevance": "high"},
        body=f"""## Core Idea

Train a cheap ML surrogate on **low-fidelity data only**, outside the
optimisation loop, and use it twice: once before the search to shrink the
design-space boundaries, and once per iteration to decide whether a
high-fidelity simulation is needed at all. The surrogate never replaces the
expensive solver — it decides when to call it.

## Method

Two mechanisms, deliberately separable:

1. **Conditional high-fidelity evaluation.** The model predicts a scalar
   `Minfo` for a design vector `x`. If it lands within a threshold of the
   target `Tinfo`, the expensive simulation runs; otherwise the candidate is
   discarded unevaluated.
2. **Boundary refinement.** Before optimising, run N cheap optimisations
   against the surrogate alone to find where good designs live, then contract
   the bounds to that region and expand by a margin so the contraction cannot
   exclude the true optimum.

Applied on top of Differential Evolution and Particle Swarm Optimization — the
method wraps an optimiser rather than being one.

## Important Technical Details

- The surrogate is trained **once, offline, on LF data**, and is reusable
  across instances of the same problem class. It is not retrained per run.
- Model choice compared across a DNN, LightGBM and XGBoost.
- Training sets of **500 and 1000 samples** — small on purpose.
- `c` was swept over {{1, 2, 4, 6, 8}} for airfoil inverse design and
  {{0.25, 0.5, 1, 2, 4}} for scalar-field reconstruction. The ranges differ
  because the RMSE magnitudes differ; they overlap so a general recommendation
  can be drawn.
- RMSE was chosen over other error metrics explicitly for interpretability.

## Important Equations

```
Minfo = M(x)                  surrogate prediction for design vector x
eps   = |Minfo - Tinfo|       miss against the target scalar
delta = c * eps_M             skip threshold; eps_M is K-fold CV RMSE
                              of the model, c a user scaling factor
if eps > delta: skip the high-fidelity simulation
refined bounds [lb_R, ub_R], expanded by alpha = 1.3
```

## Experimental Findings

Two problems — airfoil inverse design and scalar field reconstruction.
Improvements over the unenhanced optimiser on **both** DE and PSO, averaged
over 30 runs per configuration. The paper's own headline claim is that a model
that is *not* highly accurate still produces the gain, because the error is
absorbed by `delta` and `alpha` rather than trusted.

## Limitations / Failure Modes

- **Requires a pre-trained model**, and therefore a pre-existing LF dataset for
  the problem class. Cold start is not addressed.
- Stated as the central difficulty: you must first identify what reduced-order
  scalar `Minfo` is actually pertinent to the problem. Choosing the wrong
  quantity of interest silently invalidates the gate.
- `c` is a user-set hyperparameter. The paper sweeps it rather than deriving
  it, so the threshold is *scaled by* a measurement but not *fixed by* one.
- Inverse design, not constrained validation. Nothing here refuses to answer.

## ClaudeInventor Implications

This is the closest thing in the seven papers to a drop-in mechanism, because
it keeps the expensive solver authoritative and only automates *when to call
it* — which is exactly the boundary this project already draws in
[[The engine decides, the optimiser proposes]].

The `delta = c * eps_M` construction is the specific form that
[[The skip threshold must be derived from measured error]] should take here,
and the `alpha = 1.3` expansion is the mechanism named in
[[Inaccuracy is acceptable only when a margin absorbs it]].

The `Minfo` selection problem maps onto a live weakness: the analytic screen's
scalar is a safety factor computed from a section model, and
[[Analytic section models must match the real section]] records what happens
when that scalar is subtly the wrong one.

## Implementation Opportunities

- `KnowledgeBase.correction()` already computes a per-metric error; exposing it
  as a **magnitude** rather than a boolean trustworthiness flag is what a
  `delta` needs.
- Boundary refinement has no analogue here and is the cheaper half to try:
  it runs entirely against the L0 screen and touches no solver code.
- **Caveat before adopting either:** both assume the LF model is *correlated*
  with the HF one. This project has not measured that. See
  [[A weakly correlated cheap model is worse than none]].

## Related Papers

[[{P3}]] surveys the family this belongs to; [[{P2}]] covers the Bayesian
alternative to the same decision.

## Related ClaudeInventor Systems

`design_engine/inventor/knowledge.py`, `design_engine/inventor/evaluate.py`,
the L0/L1 screening rungs in [[Multi Fidelity Evaluation]].

## Source

{SRC1}""",
        links=[P2, P3, "The skip threshold must be derived from measured error",
               "Inaccuracy is acceptable only when a margin absorbs it",
               "A weakly correlated cheap model is worse than none",
               "The engine decides, the optimiser proposes",
               "Analytic section models must match the real section",
               "Multi Fidelity Evaluation", "Engineering Knowledge Base"])

    # ------------------------------------------------------------------ arX2
    v.write(
        "07_Research/Papers", P2,
        type="research", status="active", confidence="high", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx2",
               "epistemic": "Sections through Limitations are Established by source, including the fixed-cost assumption the review flags against itself. The judgement that the feasibility assumption disqualifies this for a gate is Supported inference", "relevance": "medium"},
        body=f"""## Core Idea

A review of the intersection of multi-fidelity optimisation and Bayesian
optimisation. Its value here is not a method to copy — it is a **map of which
assumptions each method needs**, which is how you tell in advance whether a
technique can survive contact with this project.

Rated medium relevance deliberately: most of what it covers is gated behind a
surrogate with calibrated uncertainty, which this engine does not have.

## Method

Survey, organised by component rather than by paper: multi-fidelity surrogates
(linear model of coregionalization, KOH auto-regressive, hierarchical Kriging,
recursive, deep GP, nonlinear auto-regressive, input-augmentation), then
acquisition functions (improvement-based, optimistic, information-based,
multi-step look-ahead), then the optimisation strategies built from them.

## Important Technical Details

- **AR1 / co-Kriging** is the workhorse multi-fidelity surrogate: model the
  high-fidelity response as a scaled low-fidelity response plus a discrepancy
  term. It needs *nested* sample sets — high-fidelity points must be a subset
  of low-fidelity points.
- **Augmented expected improvement** prices the cost and the correlation of
  each fidelity into the acquisition, rather than choosing fidelity separately.
- One-step look-ahead acquisitions are noted to **prefer exploitation over
  exploration**; multi-step look-ahead exists to mitigate that.

## Important Equations

```
AR1 / co-Kriging:   f_high(x) = rho * f_low(x) + delta(x)
                    rho       scaling between fidelities
                    delta(x)  discrepancy GP

Acquisition selects (x, fidelity) jointly, weighting expected gain
against the cost of the fidelity that would produce it.
```

## Experimental Findings

A review, so the findings are about the field rather than a single experiment.
The two that bear directly on this project:

- *"Additional LF observations could slow the convergence if there is weak
  correlation between the HF and LF models near the solution, which misdirects
  the search."*
- Multi-fidelity BO *"relies on a strong assumption that the MF surrogate for
  the objective function possesses sufficient accuracy to ensure both the
  feasibility and quality of candidate solutions."*

## Limitations / Failure Modes

- **The fixed-cost assumption.** The review flags it in its own terms: a
  constant cost per fidelity *"may be not realistic, especially for engineering
  design optimization problems where computational costs due to numerical
  solvers, which require initial guesses of solutions, often vary across the
  design variable space."*
- Curse of dimensionality — several method families are noted as workable only
  on small-dimensional problems.
- Weighted-sum multi-objective handling fails to capture non-convex parts of
  the Pareto frontier.

## ClaudeInventor Implications

The feasibility assumption is the disqualifying one. A method that assumes the
surrogate is accurate enough to establish **feasibility** is assuming away the
exact thing this engine exists to decide. Used for search that is fine; used
for a gate it is not — [[UNKNOWN is not a pass]].

The fixed-cost limitation is the inverse: a place where this project is
**ahead** of the literature, because `predict_solve()` already models cost as a
function of the candidate. See [[Simulation cost depends on the design]].

AR1/co-Kriging is attractive and currently unusable: it needs nested designs
and enough converged high-fidelity points to fit a discrepancy term, and mesh
convergence is blocked. Gated behind A1.

## Implementation Opportunities

- **Not recommended wholesale.** Adopting MF Bayesian optimisation would mean
  replacing the NSGA-II search with a machinery whose prerequisites this
  project cannot currently satisfy.
- **Worth taking piecemeal:** the idea that fidelity choice and point choice
  are *one* decision, priced by cost — which is what
  [[Simulation cost depends on the design]] makes possible here and nothing
  currently consumes.

## Related Papers

[[{P3}]] covers the same territory without the Bayesian commitment; [[{P1}]]
is the non-Bayesian version of the same skip decision; [[{P4}]] shares the
uncertainty-driven selection idea at a smaller scale.

## Related ClaudeInventor Systems

`design_engine/inventor/search.py`, `design_engine/inventor/knowledge.py`,
[[Optimization Engine]].

## Source

{SRC2}""",
        links=[P1, P3, P4, "UNKNOWN is not a pass",
               "Simulation cost depends on the design",
               "A weakly correlated cheap model is worse than none",
               "Mesh convergence is unverified", "Optimization Engine"])

    # ------------------------------------------------------------------ arx3
    v.write(
        "07_Research/Papers", P3,
        type="research", status="active", confidence="high", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx3",
               "epistemic": "Sections through Limitations are Established by source. That Kendall tau is the right statistic HERE because promotion is a ranking decision is Supported inference; that it is not computable on current data is Observed", "relevance": "high"},
        body=f"""## Core Idea

A systematic survey of multi-fidelity optimisation built on a text-mining
meta-analysis of **over 1,200 articles**, then a structured account of the
surrogate families and the optimisers built on them. The most useful single
idea for this project is that **fidelity correlation is a measurable quantity
that should gate the method**, not an assumption.

## Method

Two halves. First a text-mining framework over the collected literature, kept
as a closed loop with human feedback into data collection, cleaning and topic
modelling. Second a taxonomy of multi-fidelity surrogate modelling:

- Single-model methods
- Space-mapping methods
- Correction-based methods
- Auto-regressive (AR1) methods
- Multi-task Gaussian process methods
- Multi-fidelity physics-informed neural networks

then optimisers: multi-fidelity Bayesian optimisation, surrogate-assisted
evolutionary algorithms, and bandit-based algorithms.

## Important Technical Details

- **Kendall's rank correlation coefficient** is recorded as a measure used to
  gauge low-to-high fidelity correlation. Rank, not magnitude.
- Benchmark suites exist where fidelity correlation is a **controllable
  parameter** (Toal's four problems), which is how method robustness to weak
  correlation gets tested at all.
- A stated caveat on those suites: several have *considerable* correlation
  between fidelities, so their practicality as a stress test is limited.
- Fixed vs adaptive fidelity management is a real choice: fixed suits
  consistently high correlation, adaptive suits correlation that **varies
  across the search space**.

## Important Equations

```
Correction-based:  f_high(x) ~ rho(x) * f_low(x) + delta(x)
                   additive, multiplicative, or hybrid corrections

Rank agreement:    Kendall tau between LF and HF orderings
                   on a shared evaluated set
```

## Experimental Findings

Survey-level rather than experimental. The load-bearing negative result for
this project: space-mapping methods have *"no convergence guarantee, even to a
local HF optimum."*

## Limitations / Failure Modes

- A survey, so its claims are about the literature's coverage, not a validated
  comparison on a common benchmark.
- The text-mining meta-analysis measures **what has been published**, which is
  not the same as what works — publication bias is not corrected for.
- Its own note that benchmark suites are too well-correlated means the field's
  robustness evidence under weak correlation is thin.

## ClaudeInventor Implications

This paper supplies the missing measurement for
[[A weakly correlated cheap model is worse than none]]. The project's rule —
that a rung must demonstrate correlation before it may steer — is currently
unquantified; Kendall's tau makes it a number.

Kendall's tau is also the *right* statistic here for a reason beyond
precedent: promotion is a ranking decision, so order agreement is the property
that matters and magnitude agreement is not.

The fixed-versus-adaptive distinction bears on
[[Screening models need automatic calibration]]: a single global correction
factor is the "fixed" choice, and it is only defensible if correlation is
consistent across the space. The `problem` scoping field is a crude adaptive
scheme.

## Implementation Opportunities

- **Report tau alongside the geometric-mean correction in `correction()`**, so
  a correction with good magnitude agreement and bad rank agreement becomes
  visible rather than averaging into one reassuring factor.
- **Not yet computable here, and the reason matters.** Checked against
  `data/knowledge.sqlite` on 2026-08-28: the `calibrations` table holds **0
  rows**, and the 13 (predicted, measured) pairs that do exist are recoverable
  only by parsing free text out of `observations.reason`. Rank agreement also
  needs several candidates from **one** design family; the jetpack family has
  effectively two distinct points with three exact ties.
- So the order is: back-fill the pairs, run a real search to populate the
  table, then measure tau. Tau is a by-product of having a working calibration
  loop, not a precondition for building one.

## Related Papers

[[{P2}]] is the Bayesian-specific counterpart; [[{P1}]] is a concrete instance
of the correction-based family.

## Related ClaudeInventor Systems

`design_engine/inventor/knowledge.py`, `design_engine/log.py` (the pair
source), [[Multi Fidelity Evaluation]].

## Source

{SRC3}""",
        links=[P1, P2, "A weakly correlated cheap model is worse than none",
               "Screening models need automatic calibration",
               "Multi Fidelity Evaluation",
               "A surrogate that screens may be sloppy, one that ranks may not",
               "Engineering Knowledge Base"])

    # ------------------------------------------------------------------ arx4
    v.write(
        "07_Research/Papers", P4,
        type="research", status="active", confidence="high", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx4",
               "epistemic": "Sections through Limitations are Established by source, the 80% figure included and belonging to the offshore riser case only. That it does not transfer to the gate is the paper's own stated limit, not this project's caution", "relevance": "medium"},
        body=f"""## Core Idea

An engineering design campaign usually means simulating one candidate across
hundreds or thousands of **loading conditions**, most of which differ only
slightly from each other. That redundancy is exploitable: fit a regressor to
the cases already run, and use it to skip the cases whose outcomes it can
already predict.

The selection criterion is the important part — cases are chosen by **how
uncertain the model is about them**, not by how interesting their predicted
result is.

## Method

Pool-based active learning. The full set of loading conditions is a finite
pool, known in advance. Iteratively:

1. Fit a **Gaussian process** regression to the cases simulated so far.
2. Score every unsimulated case in the pool by its predictive variance.
3. Simulate the case with the highest variance (**uncertainty sampling**).
4. Repeat until the inferred response surface is good enough.

## Important Technical Details

- The pool is **enumerable ahead of time** — this is a design-of-experiments
  problem over known conditions, not a search over an open design space.
- Gaussian process regression is doing two jobs: interpolating results, and
  supplying the variance that drives selection. The second job is why a plain
  regressor will not substitute.
- Applied to offshore riser design over combinations of current and wave
  loading.

## Important Equations

```
Uncertainty sampling:   x_next = argmax  sigma^2(x)
                                x in pool
                        selection ignores the predicted MEAN entirely
```

## Experimental Findings

An acceptable approximation of the whole simulation portfolio from a subset
**80% smaller**, i.e. a five-fold reduction in execution time, on the offshore
riser case.

## Limitations / Failure Modes

The paper states the governing condition itself, and for a validator it is the
most important sentence in the whole set:

> *"If no error can be tolerated, such as in a critical validation phase, all
> simulations must be run."*

Beyond that:

- The speedup is *"a function of the uncertainty that can be tolerated"*, so it
  is not a fixed 80% — it is whatever the tolerance buys.
- Requires the pool to be known up front.
- Gaussian processes scale badly in the number of observations and in input
  dimension.

## ClaudeInventor Implications

The headline number does **not** transfer to the gate, and the paper says so
before this project has to. A validator is precisely the "critical validation
phase" where all simulations must be run.

Where it does transfer is the **load-case layer**, which does not exist yet.
Once a design is checked across a matrix of load cases — thrust vectors, pilot
mass, manoeuvre g, temperature — that matrix is exactly the enumerable pool
this method needs, and most of it will be redundant.

This is the source for
[[Select simulations for information value, not predicted performance]].

## Implementation Opportunities

- **Gated on B4** (the load-case layer). There is no pool to sample until load
  cases are enumerated, so this is not next.
- When it arrives, the honest split is: uncertainty sampling to build the
  *picture* of the response across load cases, and an exhaustive run of the
  cases nearest any limit-state boundary. Screening may be sparse; the gate
  may not.
- Prerequisite in common with everything else in this pass: a surrogate that
  reports a **variance**, not a flag.

## Related Papers

[[{P2}]] generalises uncertainty-driven selection into acquisition functions;
[[{P1}]] shares the skip-the-expensive-run structure with a fixed threshold
instead of a variance.

## Related ClaudeInventor Systems

Nothing yet — this is prospective. Nearest existing pieces:
`design_engine/inventor/knowledge.py` and the load-case handling in
`design_engine/fea.py`.

## Source

{SRC4}""",
        links=[P1, P2,
               "Select simulations for information value, not predicted performance",
               "UNKNOWN is not a pass", "Screened is not validated",
               "Engineering Knowledge Base"])

    # ------------------------------------------------------------------ arx5
    v.write(
        "07_Research/Papers", P5,
        type="research", status="active", confidence="high", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx5",
               "epistemic": "Sections through Limitations are Established by source. The HAZ range instance is Observed; the recommendation to adopt the question and not the machinery is Recommended design direction", "relevance": "high"},
        body=f"""## Core Idea

Optimising at nominal input values answers a question nobody asked, because the
built article is never at nominal. Reliability-based robust design optimisation
propagates input uncertainty through the model and optimises the **mean and the
spread** of the response together, so that a design too sharp to survive its
own tolerances is rejected even when it is the deterministic winner.

## Method

- Uncertainty quantification by **polynomial chaos** rather than direct Monte
  Carlo, chosen because MC needs too many samples to be affordable inside an
  optimisation loop.
- Optimisation by **MOSA** (multi-objective simulated annealing); gradient-free
  so the optimiser scans the whole space and does not depend on the initial
  guess.
- Multi-objective by construction: maximise the mean response, minimise its
  standard deviation, plus reliability constraints on the probability of
  failure.
- RDO (reduce response variance) and RBDO (bound the failure probability) are
  distinguished, and combined as RRDO.

## Important Technical Details

- Test function: a weighted sum of two Gaussians. Peak A at **(2, 2), height
  11, sharp**. Peak B at **(-2, -2), height 10, broad**.
- Input scatter: normal, standard deviation **0.1**.
- **Latin hypercube sampling, 50 points** generated around each candidate to
  characterise its neighbourhood.
- Objectives: maximise the mean, and hold the response standard deviation
  **below 0.1** as a constraint.
- **600 evaluations** total.

## Important Equations

```
Robust problem:   maximise   mu(x)          mean response
                  minimise   sigma(x)       response spread
                  subject to reliability constraints

Solutions form a Pareto front in (mu, sigma); the designer picks
from it. There is no universal robust formulation -- the paper is
explicit that the robustness criterion is a design decision.
```

## Experimental Findings

The taller, sharper global peak at (2, 2) **does not appear** in the robust
solution. It is classified infeasible because the response falls off too fast
around it to hold sigma below 0.1. The robust optimum is the shorter, flatter
peak at (-2, -2).

Height 11 loses to height 10 on curvature.

## Limitations / Failure Modes

- Stated plainly by the authors: *"A very long computation time and
  computational resources are the main challenges in robust optimization.
  Hence, it's only possible to conduct a robust optimization process on small
  components of engineering designs."*
- Demonstrated on a two-dimensional analytic test function, not on a solved
  physical model.
- There is **no universal robust formulation** — the objectives and constraints
  are chosen by the designer, so "robust" is not a property the method
  certifies, only one it optimises toward under a stated definition.
- Polynomial chaos needs the input distributions to be specified. Wrong
  distributions give confidently wrong spreads.

## ClaudeInventor Implications

The most immediately live paper of the seven, because this project has a real
instance of its central result. The jetpack frame's pass or fail is decided by
where in `rho_o,haz` in [0.375, 0.50] the true HAZ softening lies — under every
sourced factor in that range the frame fails its own 3.0 gate. A single chosen
value produces a single answer that the evidence does not support. See
[[Every safety factor used a strength the joints do not have]] and
[[Deterministic feasibility is not feasibility under uncertainty]].

The scale limitation cuts the other way and must be respected: full RRDO around
a CalculiX solve is unaffordable here, and mesh convergence is blocked anyway.

## Implementation Opportunities

- **Small and worth doing now:** extend `tolerance_perturbation`
  (`inventor/analysis.py:194`) beyond design variables so it can perturb a **material or
  process parameter**. `Perturbation` (`inventor/analysis.py:182`) is already general;
  the reach is the restriction.
- The first question to ask it is the sourced HAZ range, because the answer
  changes a live verdict rather than demonstrating a capability.
- **Do not** adopt polynomial chaos or MOSA. A handful of solves at the ends
  and middle of a sourced parameter range answers the question this project
  actually has, at a cost it can pay.
- Keep the distinction visible in reporting: a safety factor is a scalar on a
  nominal answer; a propagated range is the shape of the answer. They are not
  substitutes.

## Related Papers

[[{P2}]] treats uncertainty as an acquisition input rather than a constraint;
[[{P4}]] uses predictive variance for a different purpose (what to simulate,
not what to build).

## Related ClaudeInventor Systems

`design_engine/analysis.py`, `design_engine/weld.py`,
`design_engine/materials.py`, [[6061-T6 Thermal Derating]].

## Source

{SRC5}""",
        links=[P2, P4,
               "Deterministic feasibility is not feasibility under uncertainty",
               "Every safety factor used a strength the joints do not have",
               "6061-T6 Thermal Derating", "Constraints are gates, not preferences"])

    # ------------------------------------------------------------------ arx6
    v.write(
        "07_Research/Papers", P6,
        type="research", status="active", confidence="high", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx6",
               "epistemic": "Sections through Limitations are Established by source. That a singular goal functional prevents termination is Supported inference - the paper assumes a bounded goal and does not discuss the failure. Not yet validated in ClaudeInventor", "relevance": "high"},
        body=f"""## Core Idea

When the answer wanted is a single functional of the solution rather than the
whole field, resolving the whole domain is wasted work. Goal-oriented adaptive
FEM steers refinement by a **dual (adjoint) problem** that weights local error
by its influence on the goal — and this paper proves the resulting algorithm
converges at optimal rates measured in **total computational cost**, not just
in degrees of freedom.

## Method

Solve a primal problem and a dual problem, both inexactly, with a contractive
iterative solver (preconditioned CG with an optimal multilevel additive
Schwarz preconditioner, or geometric multigrid). Mesh refinement and solver
termination are both steered by computable a posteriori estimators. Each step
the algorithm chooses either a solver iteration or a local refinement.

## Important Technical Details

- The setting is a **linear symmetric elliptic PDE with a linear goal
  functional**. Linear elastostatics fits; contact, plasticity and large
  deformation do not.
- The key theoretical advance is **full linear convergence** of the estimator
  product independently of which action the algorithm picks at each step, and
  for arbitrary adaptivity parameters — which is what makes the cost-optimality
  result follow.
- Unlike earlier work it needs **no inner loop for data approximation** and no
  separate nested meshes for the primal and dual problems.
- The computed goal carries a residual correction term for the inexact solve:
  `G(u*) ~ G = G(u) + R`.

## Important Equations

```
PDE:    -div(A grad u) + c u = f + div f   in Omega,  u = 0 on boundary
Goal:   G(u) = integral( g u )  -  integral( g_vec . grad u )

Refinement is driven by the PRODUCT of the primal and dual
estimators, not by the primal estimator alone.
```

## Experimental Findings

Numerical experiments confirm the proven rates. The paper's framing sentence is
the one that matters here: to approximate the goal accurately *"it is not
necessary (and might even waste computational time) to accurately approximate
the solution"* over the whole domain.

## Limitations / Failure Modes

- **Linear, symmetric, elliptic.** Outside that class the theory does not
  apply.
- The goal functional must be **linear and bounded**. This is the load-bearing
  assumption for this project, and it is the one the jetpack frame violates.
- Requires a contractive iterative solver with a good preconditioner.
  **CalculiX here runs a single-threaded direct solve**, so the paper's cost
  model does not describe this engine's solver at all.
- Implementing primal-plus-dual estimators is substantial numerical work, not
  a configuration change.

## ClaudeInventor Implications

Directly relevant to the top blocker. A1 is stalled because refining a 1280 mm
frame to resolve a peak at one 19 mm junction reached a 6.1 GB working set and
crashed CalculiX with `0xC0000005`; almost none of those elements were near the
quantity being asked about. See [[Refine where the question is, not everywhere]]
and [[Solver memory bounds mesh refinement]].

**But the prerequisite is geometric, not numerical.** At a sharp re-entrant
corner peak von Mises is unbounded, so the goal functional does not exist and
the adaptive loop cannot terminate — it refines forever while reporting
progress. That is
[[Adaptivity cannot rescue a singular goal]], and it is inference from this
paper's assumptions rather than a claim it makes. Geometry first, adaptivity
second.

## Implementation Opportunities

- **Submodelling is the cheap version and should come first.** Solve the frame
  coarsely, then re-solve a small region around the junction with displacements
  from the coarse run as boundary conditions. It needs no dual problem, no
  estimator and no new theory — only a driver — and it attacks the same
  memory ceiling.
- Full GOAFEM is a later item: it needs an adjoint solve, an a posteriori
  estimator and a solver this project does not run.
- Either way, gate on `classify_peak()` first. Running refinement on a SINGULAR
  peak spends the budget to produce a number that rises with every euro.

## Related Papers

None of the other six overlap — this is the only numerical-analysis paper in
the set. Its nearest relative in spirit is [[{P4}]], which also spends effort
where uncertainty is highest rather than uniformly.

## Related ClaudeInventor Systems

`design_engine/mesh.py`, `design_engine/fea.py`,
`design_engine/singularity.py`, [[Mesh convergence is unverified]].

## Source

{SRC6}""",
        links=[P4, "Refine where the question is, not everywhere",
               "Adaptivity cannot rescue a singular goal",
               "Mesh convergence is unverified",
               "Solver memory bounds mesh refinement",
               "Peak stress at a sharp re-entrant corner cannot converge",
               "Solver runs cannot be parallelised"])

    # ------------------------------------------------------------------ arx7
    v.write(
        "07_Research/Papers", P7,
        type="research", status="active", confidence="medium", tags=PAPER_TAGS,
        extra={"source_type": "paper", "paper_id": "arx7",
               "epistemic": "Sections through Limitations are Established by source. The low relevance rating and the exclusion from the gate are Recommended design direction - a judgement about fit, not a claim the review makes", "relevance": "low"},
        body=f"""## Core Idea

A review of machine-learning approaches to topology optimisation, organised
along two axes: the TO perspective (**why** apply ML to TO) and the ML
perspective (**how** the TO problem is recast as a learning problem).

Rated **low relevance** — a judgement about fit, not quality. Topology
optimisation changes architectural structure rather than dimensional
parameters, which is genuinely something this project cannot currently do; but
no method reviewed here can decline to answer, and that bars it from the gate.

## Method

Survey. Reviewed approaches include: ML to accelerate the tail of iterative TO,
one-shot generation of an optimal topology without iteration, ML meta-models
replacing the FEA step inside the loop, latent-space search, neural
reparameterisation of the density field with physics in the loss, and
generative (GAN-based) design.

## Important Technical Details

- Conventional TO cost is dominated by the FEA sensitivity computation each
  iteration. Cited scaling: increasing mesh size by **125x** increased required
  time by **4,137x** (Liu & Tovar, 2014). High-resolution TO takes hours to
  days.
- Training-data burden is severe: one cited study generated **80,000** samples,
  another **100,000** optimised structures; a 3D case needed 13,500 samples
  over a 31,093-node, 154,677-element domain.
- Neural reparameterisation with physics in the loss needs far fewer data and
  can enforce structural connectivity — the most promising branch reviewed.
- Even in ML-accelerated schemes, FEA can remain ~50% of the cost (60x30 mesh),
  rising with mesh size.

## Important Equations

```
SIMP density penalisation:  E(rho) = E_min + rho^p (E_0 - E_min),  p ~ 3
                            minimise compliance s.t. volume fraction

MLTO recasts the map (boundary conditions, loads, volume fraction)
-> density field as a learned function.
```

## Experimental Findings

Inference is near-instant once trained. That is the whole value proposition,
and the review is candid that it is bought with a large front-loaded data cost
that is often not accounted for.

## Limitations / Failure Modes

The review's own Section 4, and this is why the rating is low:

- MLTO accuracy *"is still inferior to that of the conventional iterative TO."*
- GAN-generated data *"does not guarantee stiffness."*
- **Structural disconnection** of the generated result is a recurring failure
  mode, addressed by adding physics to the loss rather than by detecting it.
- **Most reviewed studies cannot handle 3D problems at all** — a long list of
  citations is given — and are therefore *"not yet applicable in the actual
  product development stage."* This project is entirely 3D.
- Scalability across problem types is largely absent; methods tend to work for
  one design problem or condition.
- Manufacturability must be imposed as an explicit constraint, because AM
  processing is often impossible for many TO results.

## ClaudeInventor Implications

The 3D limitation alone disqualifies current MLTO for this project's actual
work. The deeper objection is structural and is recorded as
[[A method with no refusal path does not belong in this engine]]: the field's
answer to unreliable output is to make it more often right, not to make it say
when it is wrong, and a validator needs the second property.

That does **not** bar topology optimisation from the project. It bars ML
topology optimisation from the **gate**. Classical SIMP, which iterates against
a real FEA and carries a convergence history, is a different proposition and is
not what this paper is about.

## Implementation Opportunities

- **None recommended.** This is the one paper of the seven with no proposed
  action.
- If topology optimisation is ever wanted here, the entry point is classical
  SIMP against CalculiX, with the existing limit-state machinery judging the
  result — the boundary in
  [[The engine decides, the optimiser proposes]].
- Retained because knowing **why** a technique was rejected is worth as much as
  knowing why one was adopted, and because the 4,137x FEA scaling figure
  (Liu & Tovar, 2014, quoted by this review) is a useful data point about cost
  growth regardless. It is their measurement on their problem, not a
  prediction for this engine.

## Related Papers

No methodological overlap with the other six. [[{P1}]] is the contrast case: a
machine-learning component used in a way that cannot silently decide.

## Related ClaudeInventor Systems

None. Nearest conceptual neighbour is
`design_engine/inventor/` as the proposal layer.

## Source

{SRC7}""",
        links=[P1, "A method with no refusal path does not belong in this engine",
               "The engine decides, the optimiser proposes",
               "UNKNOWN is not a pass", "Refuse rather than invent"])


def build_synthesis(v: Vault) -> None:
    """The cross-paper hub: what the seven collectively say about this engine.

    Organised by engineering capability rather than by paper, because the
    useful question is not "what does arx3 say" but "what should decide when to
    run a solve" — and the answer to that is assembled from four papers and
    contradicted in part by a fifth.
    """
    v.write(
        "07_Research", "ClaudeInventor Research Synthesis",
        type="research", status="active", confidence="medium",
        tags=["claudeinventor", "research", "synthesis"],
        extra={"source_type": "synthesis",
               "epistemic": "Mixed - each claim below carries its own label; "
                            "the architecture and priorities are Recommended "
                            "design direction, not Established by source"},
        body=f"""Seven papers, read 2026-08-28. This note answers one question:
**what do they collectively teach us about building ClaudeInventor?**

The headline is not a technique. It is that the engine already measures two of
the three quantities this literature says a solve decision should key on, and
consults neither — see
[[Check what the engine already measures before adding]]. The papers were most
valuable for **naming the decisions those measurements were supposed to
serve**.

One framing runs through everything below. This project is a **validator**, not
a search engine. Almost all of this literature optimises; optimisation tolerates
a wrong answer as wasted compute, and validation does not. Where the two come
apart, [[UNKNOWN is not a pass]] wins.

---

## Efficient Design Search

The shared premise across [[{P1}]], [[{P2}]] and [[{P3}]] is that the expensive
evaluation is the budget, so the real algorithm is **the one that decides what
not to run**.

Three decision rules appear, in increasing sophistication:

| Rule | Source | Decides |
|---|---|---|
| Fixed threshold on surrogate miss | [[{P1}]] | run / skip |
| Correlation + variance gate | [[{P3}]] | trust the rung at all |
| Cost-weighted acquisition | [[{P2}]] | which point AND which fidelity |

The first is implementable here. The second is *measurable* here today. The
third needs a calibrated-uncertainty surrogate this engine does not have.

**The critical distinction is what the cheap model is allowed to decide.**
Rejecting is one-sided and an error costs search time; ranking is two-sided and
an error costs the answer. See
[[A surrogate that screens may be sloppy, one that ranks may not]]. The L0
model here, at 76-96% error, did both.

## Multi-Fidelity Simulation

Every method surveyed assumes a **ladder** whose rungs are close enough that
the correction between them is small and estimable. This engine jumps L1 to L3
across six orders of magnitude, which gives the correction nothing to
interpolate — see [[Multi Fidelity Evaluation]].

That reframes the dormant L2 rung. Its value is not saved time (at ~8 s a solve
it mostly is not); it is that **L2 is the rung at which surrogate error becomes
observable at a price worth paying**. Without it, every other item on this page
that depends on a measured error is unimplementable.

Two corrections the literature makes to how this project has thought about
fidelity:

- **Cost is a function of the design, not of the rung.** [[{P2}]] flags the
  fixed-cost assumption as *"not realistic"* for engineering problems. This
  engine is ahead here — `predict_solve()` already models it. See
  [[Simulation cost depends on the design]].
- **Correlation must hold where the optimum is.** [[{P2}]] warns about weak
  correlation *near the solution*. An aggregate score hides exactly that case.
  See [[A weakly correlated cheap model is worse than none]].

## Bayesian Optimization

Reviewed in [[{P2}]] and, more sceptically, in [[{P3}]]. **Not recommended
wholesale**, for a specific reason rather than a general conservatism.

Multi-fidelity BO *"relies on a strong assumption that the MF surrogate for the
objective function possesses sufficient accuracy to ensure both the feasibility
and quality of candidate solutions."* A method that assumes the surrogate is
accurate enough to establish **feasibility** assumes away the exact thing this
engine exists to decide. That is fine for search and disqualifying for a gate.

Worth taking piecemeal: the idea that *where to sample* and *at what fidelity*
are one decision, priced by cost. AR1 / co-Kriging is genuinely attractive and
currently unusable — it needs nested designs and converged high-fidelity points
to fit a discrepancy term, and [[Mesh convergence is unverified]].

## Active Learning

[[{P4}]] achieves an 80% reduction in simulations by selecting cases on
**predictive variance** rather than predicted performance —
[[Select simulations for information value, not predicted performance]].

The paper supplies its own limit, and it is the most important sentence in the
seven for this project:

> *"If no error can be tolerated, such as in a critical validation phase, all
> simulations must be run."*

So the 80% **does not transfer to the gate**. Where it transfers is the
load-case layer, which does not exist yet: once a design is checked across a
matrix of thrust vectors, pilot mass, manoeuvre g and temperature, that matrix
is exactly the enumerable pool this method needs, and most of it is redundant.

The honest split when it arrives: sparse uncertainty sampling to build the
*picture* across load cases, exhaustive runs for cases near a limit-state
boundary.

## Surrogate Models

The most consequential cluster, because it is where this project has already
been wrong ([[Screening models are optimistic in the unsafe direction]],
[[Screened is not validated]]).

Four rules, assembled from four papers and none of them stated whole in any one:

1. **The skip threshold is a measurement, not a choice** —
   [[The skip threshold must be derived from measured error]]. [[{P1}]]'s
   `delta = c * eps_M`, [[{P3}]]'s correlation gate and [[{P4}]]'s uncertainty
   bound are three routes to the same construction.
2. **Inaccuracy must be absorbed by a named margin** —
   [[Inaccuracy is acceptable only when a margin absorbs it]]. If nothing
   absorbs it, the step is a load-bearing assumption wearing a screening
   costume. `KT_ROOT_JUNCTION = 1.85` is currently one of those.
3. **A weak rung is worse than no rung** —
   [[A weakly correlated cheap model is worse than none]]. [[{P3}]] records
   **Kendall's rank correlation** as the measure, which is also the right
   statistic here because promotion is a ranking decision.
4. **Calibrate only against converged references** —
   [[Calibrate only against converged results]]. A correction fitted to an
   unconverged peak launders discretisation error into the screen permanently
   and invisibly. Currently unsatisfiable here: no result carries an error
   bound.

Rule 4 gates rule 1, and [[Mesh convergence is unverified]] gates rule 4. That
chain is the single biggest structural blocker this synthesis exposes.

## Adaptive Finite Elements

[[{P6}]] proves optimal convergence rates measured in **total computational
cost**, steering refinement by a dual problem weighted toward the goal
functional. The framing claim: to get the goal accurately it *"is not necessary
(and might even waste computational time)"* to resolve the whole domain —
[[Refine where the question is, not everywhere]].

This aims squarely at A1, where refining a 1280 mm frame to resolve a peak at
one 19 mm junction reached a 6.1 GB working set
([[Solver memory bounds mesh refinement]]).

**Two blockers, and the first is not numerical.**

- The goal functional must be **bounded**. At a sharp re-entrant corner peak
  von Mises is not, so the loop refines forever while reporting progress —
  [[Adaptivity cannot rescue a singular goal]]. Geometry first.
- The theory assumes a **contractive iterative solver**. CalculiX here runs a
  single-threaded direct solve, so the cost model does not describe this
  engine.

**Submodelling is the cheap version and should come first**: coarse global
solve, then re-solve a small region around the junction with displacements as
boundary conditions. No dual problem, no estimator, no new theory.

## Robust Optimization / Uncertainty

[[{P5}]] is the most immediately live paper of the seven, because this project
has a real instance of its result.

Its test case rejects the **deterministic winner**: a sharp peak of height 11
loses to a broad peak of height 10, because the sharp one cannot hold its
response spread under a 0.1 input scatter. Feasibility is a property of the
*neighbourhood*, not the point —
[[Deterministic feasibility is not feasibility under uncertainty]].

The live instance: the jetpack frame's pass or fail is decided by where in
`rho_o,haz` in [0.375, 0.50] the true HAZ softening lies, and under every
sourced factor in that range it fails its own 3.0 gate
([[Every safety factor used a strength the joints do not have]]). One number
was chosen; the answer belongs to the range.

**Adopt the question, not the machinery.** The paper's own limit is that robust
optimisation is affordable only on *"small components of engineering designs"*.
Polynomial chaos and MOSA around a CalculiX solve are not affordable here. A
handful of solves at the ends and middle of a sourced range answers the
question this project actually has.

## Topology Optimization

[[{P7}]] is reviewed and **rejected for this engine**, which is worth recording
as carefully as an adoption.

Topology optimisation changes architectural structure rather than dimensional
parameters, and that is a genuine capability gap here. But the review reports
that MLTO accuracy is *"still inferior to that of the conventional iterative
TO"*, that GAN-generated designs *"do not guarantee stiffness"*, that
structural disconnection recurs, and that **most reviewed studies cannot handle
3D at all**. This project is entirely 3D.

The structural objection outlives those:
[[A method with no refusal path does not belong in this engine]]. The field's
answer to unreliable output is to make it more often right, not to make it say
when it is wrong — and a validator needs the second property.

This bars **ML** topology optimisation from the **gate**, not topology
optimisation from the project. Classical SIMP against a real FEA, judged by the
existing limit-state machinery, is a different proposition — and the same
boundary as [[The engine decides, the optimiser proposes]].

---

## Recommended ClaudeInventor Architecture

*Recommended design direction. Not established by any source — this is the
project's synthesis of what the seven imply for a validator rather than an
optimiser.*

```
PROPOSE      search, ML, topology optimisation, human sketch
             may be unreliable; may not decide anything
   |
   v
SCREEN       ladder L0 -> L1 -> L2, each rung carrying:
             - a measured error magnitude  (eps_M)
             - a measured rank correlation (Kendall tau) vs the rung above
             - a cost model                (predict_solve)
             may set BOUNDARIES. may not set RANKINGS at the gate.
   |
   v
GATE         named limit states, sourced allowables, converged solves
             refuses: UNKNOWN is not a pass
   |
   v
PERTURB      propagate uncertainty on sourced parameter RANGES
             a verdict that flips inside the range is not a verdict
```

Four properties this arrangement is meant to guarantee structurally:

1. **Every rung states its own error and correlation.** A rung that cannot is
   not allowed to steer.
2. **The gate never consults a surrogate.** Screening decides what to solve;
   solving decides what passes.
3. **Cost enters the decision explicitly**, because it depends on the design.
4. **Every verdict has a refusal path.** Anything imported from the literature
   must name the input for which it returns UNKNOWN.

## Major Risks

*Each is a way this research could make the engine worse rather than better.*

- **Adding a rung without measuring its correlation.** The literature's own
  warning, and the failure is silent — the rung spends the budget it saves on
  the wrong candidates. Mitigation: measure Kendall tau before letting any rung
  rank.
- **Calibrating against unconverged references.** Launders discretisation error
  into the screen permanently. Mitigation: rule 4 above, enforced when
  convergence exists.
- **Running adaptivity on a singular goal.** Never terminates; produces a
  number that rises with every euro spent. Mitigation: gate on
  `classify_peak()` before any refinement loop.
- **Letting a surrogate establish feasibility.** The assumption [[{P2}]] states
  openly. Mitigation: property 2 above.
- **Applying active learning at the gate.** Explicitly ruled out by [[{P4}]]
  itself. Mitigation: sparse sampling for the picture, exhaustive near limit
  states.
- **Building capability instead of wiring measurements.** The finding of this
  whole pass. Mitigation: check the code before estimating the work.
- **Importing cost.** [[{P5}]]'s machinery, [[{P6}]]'s dual solver and
  [[{P2}]]'s co-Kriging are each substantial builds with prerequisites this
  project does not meet. Adopting the *question* is usually most of the value.
- **Over-trusting a single measurement.** [[One seed is an anecdote]] applies
  to correlation coefficients as much as to optimiser runs.

## Implementation Priorities

*Ranked by benefit divided by cost, with prerequisites made explicit. Nothing
here has been implemented — Phase 15 of the ingestion task forbade touching
production code.*

| # | Item | Cost | Prerequisite |
|---|---|---|---|
| 0 | **Back-fill `calibrations` from the 13 recoverable pairs** | free — no new solver runs | none |
| 1 | Activate the L2 rung; measure its tau before it may rank | config change | 0, plus enough candidates to rank |
| 2 | Extend `tolerance_perturbation` to material parameters; ask it the HAZ range | small | none |
| 3 | Submodelling driver for the junction | medium | `classify_peak` gate |
| 4 | Expose `correction()` as a magnitude; wire `delta = c * eps_M` | medium | 1 (a rung to measure), 4-of-Surrogates |
| 5 | Consume `predict_solve()` — order promotion by information per second | small | 1 |
| 6 | Load-case layer, then uncertainty sampling over it | large | B4 |
| 7 | AR1 / co-Kriging | large | A1 convergence |
| 8 | Full GOAFEM | very large | blend-clean geometry + a different solver |
| - | MF Bayesian optimisation wholesale | — | **not recommended** |
| - | ML topology optimisation | — | **not recommended** |

**Item 0 is the recommendation of this synthesis**, and checking it against the
database changed what it is. The first draft of this note proposed computing
Kendall's tau on pairs *"already in the FRACAS log"*. That was wrong, and the
correction is more useful than the original claim:

- **`calibrations` holds 0 rows.** `correction()` would return `None` even if
  something called it. The gap is not only the missing consumer recorded in
  [[Check what the engine already measures before adding]] — **the producer
  side has never run either.**
- **13 (predicted, measured) pairs are recoverable**, but only by parsing free
  text: `observations.reason` carries strings like `Predicted sigma=164.0 MPa`
  beside the measured `max_von_mises_MPa`. The project measured its own screen
  error 13 times and stored it where no code can read it.
- **Kendall's tau is not computable yet.** Rank agreement needs candidates from
  one design family. Within the jetpack family there are effectively two
  distinct points, and three of the rows are exact ties. Tau on n = 2 is
  meaningless — [[One seed is an anecdote]].

So item 0 is the **back-fill**, not the correlation. Parse the 13 pairs into
`calibrations` and `correction()` starts returning a number instead of `None`.
Measuring tau becomes a by-product of item 1 once a real search populates the
table, not a precondition for it.

What the 13 pairs already show, as magnitude rather than rank:

```
ratio = predicted / measured,  n = 13,  range 0.580 to 1.029
```

Every ratio below 1.0 is the analytic screen **under-predicting stress**, and
therefore **over-predicting safety factor**. The worst case predicted 192 MPa
where the solver measured 330.8 MPa — a 42% shortfall in the unsafe direction.
That is [[Screening models are optimistic in the unsafe direction]] with a
number attached, and it is a further argument for
[[A surrogate that screens may be sloppy, one that ranks may not]]: a screen
biased this hard is not entitled to rank anything.

## Where to read next

- [[Multi Fidelity Evaluation]] - the measured cost ladder these rungs sit on
- [[Screening models need automatic calibration]] - the missing consumer
- [[Roadmap]] - where these items sit against existing work
- [[Current State]] - what is true now, as opposed to what is proposed here""",
        links=[P1, P2, P3, P4, P5, P6, P7,
               "A surrogate that screens may be sloppy, one that ranks may not",
               "Inaccuracy is acceptable only when a margin absorbs it",
               "The skip threshold must be derived from measured error",
               "A weakly correlated cheap model is worse than none",
               "Simulation cost depends on the design",
               "Refine where the question is, not everywhere",
               "Adaptivity cannot rescue a singular goal",
               "Calibrate only against converged results",
               "Deterministic feasibility is not feasibility under uncertainty",
               "Select simulations for information value, not predicted performance",
               "A method with no refusal path does not belong in this engine",
               "Check what the engine already measures before adding",
               "Multi Fidelity Evaluation",
               "Research Knowledge Graph",
               "Screening models need automatic calibration",
               "UNKNOWN is not a pass", "The engine decides, the optimiser proposes",
               "Mesh convergence is unverified", "Roadmap", "Current State",
               "Open Questions", "One seed is an anecdote",
               "Screening models are optimistic in the unsafe direction",
               "Screened is not validated",
               "Every safety factor used a strength the joints do not have",
               "Solver memory bounds mesh refinement"])


def build_improvements(v: Vault) -> None:
    """Research finding -> implication -> subsystem -> build -> benefit -> test.

    The last field is the one that keeps this honest. An improvement with no
    stated way to tell whether it worked is a preference, and this project
    already has a rule about those:
    [[Constraints are gates, not preferences]].
    """
    v.write(
        "06_Architecture", "Research-Derived Improvements",
        type="architecture", status="proposed", confidence="medium",
        tags=["claudeinventor", "research", "roadmap"],
        extra={"source_type": "synthesis",
               "epistemic": "Recommended design direction throughout. Nothing "
                            "here is implemented; line numbers verified "
                            "2026-08-28, benefits are Hypothesized until "
                            "measured by the stated validation method"},
        body=f"""Every item from the 2026-08-28 research pass, mapped from the
principle that motivates it down to the test that would show it worked.

**Nothing here has been built.** Phase 15 of the ingestion task forbade
touching production code, and that restriction was honoured — the only files
changed were `scripts/`. Subsystem paths and line numbers were checked against
the working tree on 2026-08-28; the *expected benefits* are hypotheses, and
each carries the measurement that would confirm or kill it.

Ordering follows [[ClaudeInventor Research Synthesis]]. Prerequisites are
stated rather than implied — several of these are blocked on each other, and
one is blocked on a database table that turned out to be empty.

---

### 1. Back-fill the calibration table

**Research principle** — The skip threshold must be derived from the
surrogate's *measured* error, not chosen. [[{P1}]] scales it by RMSE, [[{P3}]]
gates on correlation, [[{P4}]] bounds by calibrated uncertainty.

**ClaudeInventor implication** — Before any of that is possible, the engine has
to be able to *read* its own screen error. It currently cannot:
`data/knowledge.sqlite` `calibrations` holds **0 rows**, while
`observations.reason` carries 13 measurements as prose
(`Predicted sigma=164.0 MPa` beside the measured `max_von_mises_MPa`).

**Target subsystem** — `design_engine/inventor/knowledge.py`, the `record()` /
`calibrations` path.

**Implementation** — A one-off parser over `observations.reason` that inserts
the 13 recoverable pairs into `calibrations`, plus a change at the source so
future runs write the predicted value to a **column** rather than into prose.

**Expected benefit** — `correction()` starts returning a `CorrectionEstimate`
instead of `None`, which is the precondition for items 4 and 5. Also converts
a measurement the project already paid for into one the code can use.

**Validation method** — `kb.correction("sf.yield", problem=...)` returns
non-`None` with `n >= 3`, and its geometric-mean ratio reproduces the
hand-computed value over the same 13 rows. Regression test asserting
`correction()` is `None` on an empty table and non-`None` after back-fill.

---

### 2. Activate the L2 coarse-FEA rung

**Research principle** — An intermediate fidelity is not a luxury; the whole
multi-fidelity literature assumes a ladder whose rungs are close enough for the
correction between them to be estimable.

**ClaudeInventor implication** — Screening jumps L1 to L3 across six orders of
magnitude, which is how a 76-96% model error survived to shape a Pareto
frontier. See [[Multi Fidelity Evaluation]] and
[[Screening models are optimistic in the unsafe direction]].

**Target subsystem** — `design_engine/inventor/adapters.py:216` `FeaStage`.

**Implementation** — A **configuration change, not new code**. `FeaStage.
__init__` already accepts `fidelity` and `mesh_mm`; it defaults to
`Fidelity.L3_HIGH_FEA`. Instantiating a second stage with
`fidelity=Fidelity.L2_COARSE_FEA` and a coarse `mesh_mm` adds the rung.
`Fidelity.L2_COARSE_FEA` (`candidate.py:32`) is presently referenced only by a
test fixture.

**Expected benefit** — Screen error becomes observable at ~8 s per candidate
instead of 8-800 s, which is what makes items 1 and 4 affordable to maintain.

**Validation method** — Run the jetpack design space through L0 to L2 to L3.
Compare the L3-promoted set against the set L3 would have promoted without L2,
and count high-fidelity calls in both. The rung earns its place only if it
*reduces* L3 calls without changing the final promoted set. **Watch for the
non-monotonic mesh trap** — [[Meshing is non-monotonic]] means a coarse rung
can be refused where a fine one succeeds, so count `UNKNOWN` returns too.

---

### 3. Measure whether the screen may rank, not just bound

**Research principle** — A weakly correlated cheap model is worse than none.
[[{P2}]], verbatim: weak correlation *near the solution* *"misdirects the
search"*. [[{P3}]] records Kendall's rank correlation as the measure.

**ClaudeInventor implication** — Rejecting is one-sided, ranking is two-sided
— [[A surrogate that screens may be sloppy, one that ranks may not]]. The L0
screen has done both without ever demonstrating it was entitled to.

**Target subsystem** — `design_engine/inventor/knowledge.py`, alongside
`correction()`.

**Implementation** — Report Kendall's tau between screened and measured values
per `(metric, problem)`, next to the geometric-mean correction, so a factor
with good magnitude agreement and bad rank agreement is visible rather than
averaged away.

**Expected benefit** — Turns "a rung must demonstrate correlation before it may
steer" from a principle into a gate with a number.

**Validation method** — **Not computable today, and that is the finding.** Rank
agreement needs several candidates from one design family; the jetpack family
has effectively two distinct points with three exact ties, and tau on n = 2 is
meaningless ([[One seed is an anecdote]]). This item is therefore validated as
a by-product of item 2 once a real search populates the table — with a stated
minimum sample size fixed *before* looking at the answer.

---

### 4. Wire the skip gate

**Research principle** — `delta = c * eps_M` from [[{P1}]]: skip the expensive
evaluation when the surrogate's predicted miss exceeds a threshold scaled by
the model's own measured error, and expand the surviving bounds by a margin
(alpha = 1.3) so the contraction cannot exclude the true optimum.

**ClaudeInventor implication** — This is the specific form
[[The skip threshold must be derived from measured error]] should take here,
and [[Inaccuracy is acceptable only when a margin absorbs it]] is why it is
safe with a weak model.

**Target subsystem** — `design_engine/inventor/knowledge.py` `correction()`
(expose a **magnitude**, not just the current trustworthiness flag), consumed
by the evaluator's promotion decision.

**Implementation** — `CorrectionEstimate` gains a dispersion figure; the
evaluator asks for it before promoting and skips candidates outside
`c * eps_M`.

**Expected benefit** — Fewer L3 calls at equal final design quality.

**Validation method** — The literature's own protocol: hold the design space
and seed fixed, run with and without the gate, and compare **final design
quality against the count of high-fidelity calls**. A gate that cuts calls but
degrades the promoted set has failed. Prerequisites: items 1 and 2.
**Do not calibrate against unconverged references** —
[[Calibrate only against converged results]] — which currently means this
cannot be finished honestly until A1 is unblocked.

---

### 5. Consume the cost model

**Research principle** — Simulation cost depends on the design, and most of the
literature wrongly assumes otherwise. [[{P2}]] flags its own fixed-cost
assumption as *"not realistic"* for engineering problems.

**ClaudeInventor implication** — This engine already models it and ignores it.
`predict_solve()` fits `t ~ 40.1 s * (nodes/100k) ** 1.661` from 39 real runs;
it feeds `affordable()`, which has **no caller outside tests**. See
[[Simulation cost depends on the design]] and
[[Check what the engine already measures before adding]].

**Target subsystem** — `design_engine/inventor/knowledge.py:434`
`predict_solve()` and `:533` `affordable()`; consumer in the evaluator's
promotion ordering.

**Implementation** — Order the promotion queue by expected information per
second rather than by screened rank, and refuse a candidate whose predicted
peak memory exceeds what is free — `affordable()` already returns the
three-way verdict for this.

**Expected benefit** — Fewer wall-clock hours lost to solves that were never
going to fit. Directly addresses
[[Solver memory bounds mesh refinement]].

**Validation method** — Replay a past optimisation run's candidate set through
the new ordering and compare total solve seconds to reach the same promoted
set. Historical runs are in the log, so this is measurable **without new solver
time**.

---

### 6. Submodel the junction

**Research principle** — Do not resolve the whole domain to answer a local
question. [[{P6}]]: it *"is not necessary (and might even waste computational
time)"* to approximate the solution accurately everywhere when the goal is one
functional.

**ClaudeInventor implication** — A1 is blocked because refining a 1280 mm frame
to resolve a peak at one 19 mm junction reached a 6.1 GB working set and
crashed CalculiX. Almost none of those elements were near the question. See
[[Refine where the question is, not everywhere]] and
[[Mesh convergence is unverified]].

**Target subsystem** — `design_engine/mesh.py`, `design_engine/fea.py`.

**Implementation** — Coarse global solve, then re-solve a small region around
the junction with displacements from the coarse run applied as boundary
conditions. No dual problem, no error estimator, no new theory — a driver.
Full GOAFEM is a much later item and needs a contractive iterative solver this
project does not run.

**Expected benefit** — A converged peak stress at the junction within the
memory the machine actually has, which unblocks A1 and therefore item 4.

**Validation method** — Two independent checks, because a submodel that is
merely *self-consistent* proves nothing: (a) the submodel peak converges under
its own refinement while the global mesh is held fixed; (b) on a geometry small
enough to solve monolithically, the submodel result matches the full solve
within a stated tolerance. **Gate on item 7 first.**

---

### 7. Refuse refinement on a singular peak

**Research principle** — Goal-oriented adaptivity assumes a **bounded** goal
functional. [[{P6}]] states it as a hypothesis; the consequence when it fails
is not stated there — that join is this project's inference,
[[Adaptivity cannot rescue a singular goal]].

**ClaudeInventor implication** — At a sharp re-entrant corner peak von Mises is
unbounded (Williams 1952, lambda ~ 0.5445 at 270 degrees), so refinement never
terminates and every step genuinely reduces discretisation error while
genuinely raising the peak. From inside the loop that is indistinguishable from
slow convergence. See
[[Peak stress at a sharp re-entrant corner cannot converge]].

**Target subsystem** — `design_engine/singularity.py:222` `classify_peak()`,
called by whatever drives item 6.

**Implementation** — A hard precondition in the refinement driver: if
`classify_peak()` returns SINGULAR, refuse and return UNKNOWN naming the edge.
**Implement it as code, not as a step to remember** — the same rule already
applied to the production sign-off token.

**Expected benefit** — Prevents the most expensive available failure: a
refinement campaign that burns the budget and returns a number that rises with
every hour spent on it.

**Validation method** — Regression test on the two recorded peaks. P0047
(sharp, peak 1.28 mm from the corner) must be refused; P0048 (filleted, 9.51 mm)
must be allowed. A box with no re-entrant edges must also be allowed, so the
gate is not simply refusing everything.

---

### 8. Perturb the sourced parameter range

**Research principle** — A deterministically feasible design can be infeasible
under uncertainty. [[{P5}]] rejects the **deterministic winner**: a sharp peak
of height 11 loses to a broad peak of height 10 because it cannot hold its
response spread.

**ClaudeInventor implication** — The jetpack frame's verdict is decided by
where in `rho_o,haz` in [0.375, 0.50] the true HAZ softening lies, and under
every sourced value in that range it fails its own 3.0 gate. One number was
chosen; the answer belongs to the range. See
[[Deterministic feasibility is not feasibility under uncertainty]] and
[[Every safety factor used a strength the joints do not have]].

**Target subsystem** — `design_engine/inventor/analysis.py` — `Perturbation`
(`:182`), `tolerance_perturbation` (`:194`), and the existing `robustness()`
harness. `design_engine/weld.py` supplies the factor.

**Implementation** — Smaller than it looks. `robustness()` already perturbs,
re-evaluates and reports `failure_rate` and `worst_case`, and `Perturbation.
apply` is general over the candidate's `values` dict. What is missing is that
the HAZ factor is a **module constant in the design script**, not a candidate
value, so no perturbation can reach it. Move it into the candidate values read
by the case builder, then add a `range_perturbation` — the sourced interval is
bounded, so a Gaussian is the wrong shape and endpoints-plus-middle is the
right one.

**Expected benefit** — The frame's verdict stops being a function of an
undisclosed choice. Reporting gains the distinction between *a safety factor*
(a scalar on a nominal answer) and *the shape of the answer across a sourced
range*.

**Validation method** — Report SF at 0.375, at 0.50, and at the value needed to
pass (rho >= 0.647). A verdict that flips inside a sourced range must be
reported as UNKNOWN rather than as the nominal answer — that is the actual
acceptance criterion, and it is a direct application of
[[UNKNOWN is not a pass]]. **Do not** adopt polynomial chaos or MOSA; [[{P5}]]
states its own machinery is affordable only on small components.

---

### 9. Select load cases by information value

**Research principle** — Choose simulations by predictive variance, not
predicted performance. [[{P4}]] reaches an 80% reduction that way.

**ClaudeInventor implication** — The same paper supplies the limit that matters
here: *"If no error can be tolerated, such as in a critical validation phase,
all simulations must be run."* So the reduction applies to the **screen**, never
to the gate. See
[[Select simulations for information value, not predicted performance]].

**Target subsystem** — Does not exist yet. Needs the load-case layer (roadmap
B4) before there is a pool to sample.

**Implementation** — Once load cases are enumerated (thrust vectors, pilot
mass, manoeuvre g, temperature), sample sparsely by predictive variance to
build the response picture, and run **exhaustively** for cases near any
limit-state boundary.

**Expected benefit** — A load-case matrix that is affordable to sweep at all.

**Validation method** — Hold out a fraction of the full matrix, predict it from
the actively-selected subset, and report the worst-case error against the
held-out cases — not the mean. Prerequisite: B4, and a surrogate reporting a
**variance** rather than the current boolean flag.

---

### 10. An acceptance criterion for imported methods

**Research principle** — Across the seven, methods sort cleanly by whether they
can decline: [[{P1}]] by threshold, [[{P3}]] by correlation gate, [[{P4}]] by
uncertainty bound. [[{P7}]] describes none that can.

**ClaudeInventor implication** — For a search layer over-confidence costs
compute; for a validator it is disqualifying. See
[[A method with no refusal path does not belong in this engine]].

**Target subsystem** — Process, not code. Belongs beside the existing
architecture decisions.

**Implementation** — A standing question asked before adopting anything from
the literature:

> Name the input for which this method returns UNKNOWN. If there is no such
> input, it is a generator, and its output must pass through a validator that
> has one.

**Expected benefit** — Keeps the boundary in
[[The engine decides, the optimiser proposes]] from eroding one convenient
exception at a time.

**Validation method** — Not measurable, and it should not pretend to be. It is
a review criterion, recorded so that a future session has to answer it
explicitly rather than skip it — the same reasoning that turned the vault read
into a logged receipt rather than an instruction.

---

## What is blocked on what

```
A1 mesh convergence +--> 4 (skip gate, needs converged references)
        ^           +--> co-Kriging (synthesis item 7)
        |
        6 submodelling --- gated by ---> 7 singular-peak refusal

1 back-fill ---> 4 wire the gate
2 L2 rung   +--> 3 rank correlation ---> 4
            +--> 5 cost-ordered promotion

B4 load cases ---> 9 active selection
```

Two items — **1** and **7** — have no prerequisites and no dependency on the
blocked convergence work. Both are small. If only two things are done from this
page, those are the two.""",
        links=["ClaudeInventor Research Synthesis", P1, P2, P3, P4, P5, P6, P7,
               "Multi Fidelity Evaluation",
               "Screening models are optimistic in the unsafe direction",
               "A surrogate that screens may be sloppy, one that ranks may not",
               "The skip threshold must be derived from measured error",
               "Inaccuracy is acceptable only when a margin absorbs it",
               "Calibrate only against converged results",
               "Simulation cost depends on the design",
               "Check what the engine already measures before adding",
               "Refine where the question is, not everywhere",
               "Adaptivity cannot rescue a singular goal",
               "Deterministic feasibility is not feasibility under uncertainty",
               "Select simulations for information value, not predicted performance",
               "A method with no refusal path does not belong in this engine",
               "Peak stress at a sharp re-entrant corner cannot converge",
               "Mesh convergence is unverified", "Meshing is non-monotonic",
               "Solver memory bounds mesh refinement", "One seed is an anecdote",
               "Every safety factor used a strength the joints do not have",
               "UNKNOWN is not a pass", "The engine decides, the optimiser proposes",
               "Constraints are gates, not preferences", "Roadmap",
               "Architecture Decisions"])


def build_questions(v: Vault) -> None:
    """The ten questions the research pass was commissioned to answer.

    Kept as their own note rather than folded into the synthesis, because a
    future session is far more likely to arrive with a question than with a
    topic — and these are the questions.
    """
    v.write(
        "07_Research", "Research Questions Answered",
        type="research", status="active", confidence="medium",
        tags=["claudeinventor", "research", "questions"],
        extra={"source_type": "synthesis",
               "epistemic": "Each answer carries its own label. Several answers "
                            "are 'not possible yet, and here is what blocks it' "
                            "- that is a real answer, not a gap in this note"},
        body=f"""Ten questions posed to the 2026-08-28 research pass, answered
from the seven papers **and** from what the codebase actually does. Where the
honest answer is "not yet, because X", X is named.

---

### 1. How should ClaudeInventor decide when a cheap simulation is sufficient?

**By a threshold derived from the cheap model's own measured error** — [[{P1}]]'s
`delta = c * eps_M`, never a chosen constant. *Established by source*, and by
three of them independently ([[The skip threshold must be derived from measured error]]).

**And only for rejection.** A cheap model may decide what *not* to solve; it may
not decide the order of what survives, because rejection is one-sided and
ranking is two-sided — [[A surrogate that screens may be sloppy, one that ranks may not]].
*Supported inference.*

**Not possible today.** `calibrations` holds 0 rows, so `correction()` returns
`None` regardless of caller. *Observed.* Back-filling it is the first roadmap
item.

### 2. How should it decide when an expensive FEA run is worth the cost?

Worth = **expected information divided by predicted seconds**, with a memory
check first. Both halves already exist and neither is consulted:
`predict_solve()` fits solve time from 39 real runs, and `affordable()` returns
a three-way verdict (`yes` / `marginal` / `no`) checking **memory before time**,
because memory is the binding constraint here. *Observed* that they exist
unused; *Recommended design direction* for wiring them.

One case where the answer is never: **a peak sitting on a geometric
singularity**. That run has unbounded cost and zero information, because the
quantity it measures does not converge — [[Stress Singularity]].

### 3. How can it learn which candidate should be simulated next?

**By predictive variance, not predicted performance** — [[{P4}]] selects the
case the model is least sure about. *Established by source.*

**Not implementable today**, and the blocker is specific: the prerequisite is a
surrogate that reports a **variance**, and `correction()` reports a boolean
trustworthiness flag instead. *Not yet validated in ClaudeInventor.*
→ [[Select simulations for information value, not predicted performance]]

### 4. How can it use simulation history to reduce future computation?

Four mechanisms exist in the code. **One works; three have empty inputs.**
*Observed, 2026-08-28.*

| Mechanism | State |
|---|---|
| Content-addressed evaluation cache | working — [[Evaluation Cache]] |
| Cost model from 39 logged runs | fitted, **unconsumed** |
| `calibrations` (predicted vs measured) | **0 rows** |
| `failure_points` (known-bad regions) | **0 rows** |

The `calibrations` case is the instructive one: the project measured its screen
error **13 times** and wrote it into `observations.reason` as prose
(`Predicted sigma=164.0 MPa`), where no code can read it. History is not
missing; it is unqueryable.
→ [[Check what the engine already measures before adding]]

### 5. How should surrogate uncertainty affect candidate selection?

**It should set the boundary, not the order.** Magnitude of error plus an
explicit margin is enough to reject; changing rank additionally requires
demonstrated **rank** agreement. *Supported inference.*

The measure for that is Kendall's tau ([[{P3}]]), which is also the right
statistic here because promotion is a ranking decision. *Established by source.*

**Not computable on current data**: rank agreement needs several candidates
from one design family, and the jetpack family has effectively two distinct
points with three exact ties. *Observed.*
→ [[A weakly correlated cheap model is worse than none]]

### 6. How should mesh refinement be targeted around important quantities of interest?

**Goal-oriented, via a dual problem weighted toward the quantity of interest**
— [[{P6}]], which proves optimal rates in *total computational cost*.
*Established by source.*

**But only after the goal is bounded.** At a re-entrant corner peak von Mises
is unbounded, so the loop refines forever while reporting progress
([[Adaptivity cannot rescue a singular goal]] — *Supported inference*, stated
in no source).

For this engine the affordable subset is **submodelling**: coarse global solve,
then re-solve a small region with displacements as boundary conditions. Full
GOAFEM assumes a contractive iterative solver, and CalculiX here runs a
single-threaded direct solve. → [[Refine where the question is, not everywhere]]

### 7. How should optimization account for uncertain material, manufacturing, and operating conditions?

**By propagating sourced ranges, not by a scalar on a nominal answer.** A
safety factor and a propagated range are not substitutes: the first scales an
answer, the second tells you the answer's shape and whether the nominal point
sits on a cliff. *Established by source* — [[{P5}]] rejects the deterministic
winner on exactly that basis.

**The acceptance rule this yields:** a verdict that flips inside a sourced
range is UNKNOWN, not the nominal answer. *Recommended design direction*, and a
direct application of [[UNKNOWN is not a pass]].

Live here: the frame's verdict depends on where in `rho_o,haz` in
[0.375, 0.50] the truth lies. `robustness()` already exists; the blocker is
that the HAZ factor is a module constant, so no perturbation can reach it.
*Observed.* → [[Deterministic feasibility is not feasibility under uncertainty]]

### 8. How can topology optimization eventually extend ClaudeInventor beyond simple dimensional changes?

**Not through ML topology optimisation.** Most reviewed methods cannot handle
3D at all, and none can decline to answer — [[{P7}]]. *Established by source*
for the limitations; *Recommended design direction* for the exclusion.

The route, if it is ever wanted: **classical SIMP against a real FEA**, placed
in the PROPOSE layer and judged by the existing limit-state machinery — the
boundary in [[The engine decides, the optimiser proposes]]. That keeps the
generator unreliable and the gate authoritative, which is the only arrangement
consistent with [[A method with no refusal path does not belong in this engine]].

**Not evaluated in this pass.** Whether SIMP is affordable against CalculiX
here is *Unknown*.

### 9. Which of these ideas can be implemented without destabilizing the current deterministic engineering engine?

The invariant to preserve: **none of them may change what passes** — only what
gets *run*, *ordered*, or *reported*.

Safe by that test, all additive:

- **Singular-peak refusal** — adds a refusal. Can only make the engine decline
  more, never accept more.
- **`calibrations` back-fill** — writes a table nothing currently reads.
- **HAZ range perturbation** — a reporting change plus moving a constant into
  candidate values.
- **L2 rung activation** — configuration; `FeaStage.__init__` already accepts
  `fidelity` and `mesh_mm`.
- **Cost-ordered promotion** — changes evaluation *order*, not verdicts.

*Recommended design direction.* Determinism is preserved because none of these
introduces a stochastic decision into the gate; the perturbation work is a
sweep over a stated range, deliberately not a sampled distribution.

### 10. Which ideas should NOT be implemented yet, because the architecture is not ready?

*Recommended design direction throughout, with the specific blocker named.*

| Idea | Why not yet |
|---|---|
| MF Bayesian optimisation wholesale | assumes the surrogate can establish **feasibility** — the thing the engine exists to decide |
| ML topology optimisation | no refusal path; most methods are 2D-only |
| Full GOAFEM | needs a contractive iterative solver; CalculiX here is single-threaded direct |
| AR1 / co-Kriging | needs nested designs and **converged** high-fidelity points |
| Active learning at the gate | ruled out by [[{P4}]] itself: in a critical validation phase all simulations must be run |
| Wiring the skip gate now | it would calibrate against unconverged references and launder discretisation error into the screen permanently — [[Calibrate only against converged results]] |

The recurring blocker is the same one: **mesh convergence is unverified**, so
nothing that needs a trustworthy high-fidelity reference can be built honestly
yet. → [[Mesh convergence is unverified]]""",
        links=["ClaudeInventor Research Synthesis", "Research-Derived Improvements",
               P1, P3, P4, P5, P6, P7, "Stress Singularity",
               "The skip threshold must be derived from measured error",
               "A surrogate that screens may be sloppy, one that ranks may not",
               "Select simulations for information value, not predicted performance",
               "Check what the engine already measures before adding",
               "A weakly correlated cheap model is worse than none",
               "Adaptivity cannot rescue a singular goal",
               "Refine where the question is, not everywhere",
               "Deterministic feasibility is not feasibility under uncertainty",
               "Calibrate only against converged results",
               "The engine decides, the optimiser proposes",
               "A method with no refusal path does not belong in this engine",
               "Research Knowledge Graph",
               "Evaluation Cache", "UNKNOWN is not a pass",
               "Mesh convergence is unverified", "Roadmap"])


def build_singularity_note(v: Vault) -> None:
    """The one canonical note the research pass found genuinely missing.

    Stress singularity knowledge was scattered across a failure note, a source
    module and a README paragraph, with no topic note to land on — and both
    the distillation and the graph phases wanted to point at one.
    """
    v.write(
        "03_Engineering/FEA", "Stress Singularity",
        type="method", status="active", confidence="high",
        tags=["claudeinventor", "fea"],
        extra={"epistemic": "Established by source for the Williams solution; "
                            "Observed for the classifications and the code "
                            "behaviour, verified 2026-08-28"},
        body="""Where linear elastic stress has **no finite value**, so a computed
peak is not a result — it is an artefact of how finely the mesh was cut.

## The mechanism

At a re-entrant corner the elastic solution goes as `sigma ~ r ** -p` as
`r -> 0`. Williams (1952) gives `p` by solving the symmetric characteristic
equation for the opening angle `a`:

```
sin(lambda * a) + lambda * sin(a) = 0        p = 1 - lambda
```

`design_engine/singularity.py` solves it by **bisection rather than a fitted
approximation**, because the equation is transcendental and a curve fit would
invent precision the reference does not give. Recomputed 2026-08-28:

| Interior angle | Exponent `p` | Meaning |
|---|---|---|
| 180 deg | 0.0000 | flat — no singularity |
| 225 deg | 0.3264 | mild re-entrant |
| **270 deg** | **0.4555** | the sharp corner that started this |
| 300 deg | 0.4878 | |
| 360 deg | 0.5000 | crack tip |

## Why it is detected on the CAD solid, not the mesh

A concave fillet's **tessellation is itself slightly re-entrant** (~198 deg),
so a mesh-based test flags every blended corner. `sharp_concave_edges()`
therefore works on the B-rep, and the convexity test took three attempts to get
right — projecting onto an unbounded surface misread 6 of a box's 12 convex
edges as 270 deg corners, and a face-centroid test then failed on a holed face
where the centroid lands in the hole. The working form orients the edge tangent
within one face and compares `n_b x t` against the other normal.

## What the engine does about it

**Static work: reported.** `classify_peak()` returns a verdict with the
distance from the peak to the nearest sharp edge, so a safety factor can say
whether it was measured somewhere meaningful.

**Fatigue: a hard refusal**, in code at `design_engine/fea.py:1173`. Life goes
as `range ** -m`, so an unbounded range drives predicted life to zero as the
mesh refines. That is **meaningless rather than conservative**, which is the
distinction the refusal exists to protect.

## What it cost to learn

A safety factor of **3.844** was reported, passed every check, and was
meaningless — its peak sat 1.28 mm from a sharp corner. The old outlier-ratio
heuristic returned **1.044** for that peak, the cleanest reading it can give.
→ [[The outlier ratio does not detect geometric singularities]]

Recorded classifications: **P0047 SINGULAR** at 1.28 mm; **P0048 CLEAN** at
9.51 mm after filleting; a plain box scores zero sharp concave edges.

## The consequences that are not obvious

- **A fillet is a 2D blend of a 3D corner.** Filleting the junction took the
  frame from 12 sharp edges to 8, not to zero.
- **Refinement cannot fix it, and looks like it is working.** Every refinement
  genuinely reduces discretisation error and genuinely raises the peak, so an
  adaptive loop never terminates — [[Adaptivity cannot rescue a singular goal]].
- **Geometry comes before mesh work.** Any convergence or submodelling effort
  must gate on `classify_peak()` first, or it spends the budget producing a
  number that rises with every hour.

## Blending them all, 2026-08-30

**Every frame the search had ever produced was singular** — all 8 edges, steel
and aluminium alike. Not one safety factor in any run was converged, and the
filleted P0048 was the exception rather than the rule.

**r = 1.0 mm, from IIW.** The effective-notch-stress method fictitiously rounds
a weld toe to 1 mm — Radaj's reference radius, adopted *"to avoid arbitrary or
infinite stress results"* at a geometrically sharp notch. Same problem,
established answer. Two caveats travel with it: IIW defines it for **fatigue**
assessment, so using it to make a static gate convergeable is a transfer of the
geometric device rather than a citation of the method; and it is a
**fictitious** radius embedding micro-structural support effects, not a claim
that the real toe measures 1 mm.

**The selector had to become declarative.** Hand-computing where the sharp
edges sit worked for one frame and silently selected nothing for **15 of 81**
designs in the same space — which faces meet which depends on the parameters. A
doubler thicker than its crossbeam puts the edge on the crossbeam, a thinner
one on the pad, and with no doubler the survivors are vertical instead of
horizontal. So `geometry.py` now accepts
`{"edges": {"sharp_concave": true}}` and asks this module which edges those
are. It states a **postcondition** — no sharp concave edge shall survive at
this radius — and raises if one does. Matching nothing is *success* there, not
a silent no-op, because the intent is already satisfied.

> A selector that has to be re-derived per design will be wrong for some
> design. The module that finds the problem should name the edges to fix.

## Fixing it disabled the gate that caught it

The awkward part, and the reason `blend_resolution()` now exists. A blended
corner has **finite** peak stress, so `classify_peak` correctly stops
objecting — while a 1 mm blend meshed at 3 mm has **a third of an element**
across it, and the peak still measures the mesh.

The problem moves from **unbounded** to **under-resolved**. Those need
different remedies — a fillet versus a finer mesh — and the check that caught
the first does not catch the second. `MIN_BLEND_ELEMENTS = 3` is a floor, not
a target, and is flagged as an estimate: notch practice asks for roughly r/6.

## And then the wall

The geometry is provably clean and the frame **cannot currently be evaluated
on this machine**. Two designs are refused by the Jacobian gate at every rung
down to 2.5 mm; the third meshes at 3.0 mm and then CalculiX exits
`3221225477` at **5,007 MB**.

Meshing is non-monotonic again, and the old ladder just missed: **3.2 mm fails
where 3.0 mm passes**, and 2.0 mm fails where 2.5 and 1.5 pass.

So the memory ceiling is now reached three independent ways — global
refinement at 6.1 GB, ccx `*SUBMODEL` at a cost per driven node that never
finishes, and now a blend-clean geometry that needs a finer mesh. **They were
always the same problem**, and it is the one thing standing between this
project and a converged number.
→ [[Solver memory bounds mesh refinement]]""",
        links=["Peak stress at a sharp re-entrant corner cannot converge",
               "The outlier ratio does not detect geometric singularities",
               "Adaptivity cannot rescue a singular goal",
               "Mesh convergence is unverified",
               "Refine where the question is, not everywhere",
               "Aluminium has no endurance limit", "Validation Philosophy",
               "UNKNOWN is not a pass"])


def build_graph(v: Vault) -> None:
    """Phase 14: make the paper -> concept -> subsystem traversal explicit.

    The links already existed — measured 2026-08-28, every paper reaches a
    subsystem note in one or two hops. What was missing was a map, and a
    translation between the node names the research brief suggested and the
    titles this vault actually uses.
    """
    v.write(
        "07_Research", "Research Knowledge Graph",
        type="index", status="active", confidence="high",
        tags=["claudeinventor", "research", "index"],
        extra={"source_type": "synthesis",
               "epistemic": "Observed - connectivity measured over the vault "
                            "on 2026-08-28, not asserted"},
        body=f"""How to get from a paper to the code it bears on, and back.

Measured rather than claimed: every paper note reaches a subsystem note in
**one or two hops**, and the only notes with no inbound links are the three
templates and one session note.

## The chains

```
[[{P1}]]
   -> [[The skip threshold must be derived from measured error]]
   -> [[Screening models need automatic calibration]]
   -> [[Engineering Knowledge Base]]        knowledge.py correction()

[[{P3}]]
   -> [[A weakly correlated cheap model is worse than none]]
   -> [[Multi Fidelity Evaluation]]
   -> [[Optimization Engine]]               inventor/evaluate.py, adapters.py

[[{P2}]]
   -> [[Simulation cost depends on the design]]
   -> [[Engineering Knowledge Base]]        knowledge.py predict_solve()

[[{P4}]]
   -> [[Select simulations for information value, not predicted performance]]
   -> [[Optimization Engine]]               (needs the load-case layer, B4)

[[{P6}]]
   -> [[Refine where the question is, not everywhere]]
   -> [[Adaptivity cannot rescue a singular goal]]
   -> [[Stress Singularity]]                singularity.py, mesh.py, fea.py

[[{P5}]]
   -> [[Deterministic feasibility is not feasibility under uncertainty]]
   -> [[Every safety factor used a strength the joints do not have]]
   -> [[Design Engine]]                     inventor/analysis.py, weld.py

[[{P7}]]
   -> [[A method with no refusal path does not belong in this engine]]
   -> [[The engine decides, the optimiser proposes]]
   -> [[Validation Philosophy]]             (rejected for the gate)
```

## Name translation

The research brief suggested topic-note names that this vault does not use,
because the distillation was organised by **claim** rather than by **topic** —
a note called "Adaptive Mesh Refinement" would restate a principle rather than
assert one. Searching by the suggested name lands nowhere, so the mapping is here.

| Suggested name | This vault's note |
|---|---|
| Multi-Fidelity Optimization | [[Multi Fidelity Evaluation]] + [[{P3}]] |
| Bayesian Optimization | [[{P2}]] |
| Active Learning | [[Select simulations for information value, not predicted performance]] |
| Active Experiment Selection | same as above |
| Robust Design Optimization | [[Deterministic feasibility is not feasibility under uncertainty]] |
| Robust Optimization | same as above |
| Adaptive FEM | [[{P6}]] |
| Adaptive Mesh Refinement | [[Refine where the question is, not everywhere]] |
| Topology Optimization | [[{P7}]] |
| Surrogate Modeling | [[A surrogate that screens may be sloppy, one that ranks may not]] |
| Stress Singularity | [[Stress Singularity]] |
| Evaluator | [[Optimization Engine]] |
| FEA | [[Validation Philosophy]] + [[Stress Singularity]] |

## Entry points

- **Arrived with a question?** → [[Research Questions Answered]]
- **Want the whole argument?** → [[ClaudeInventor Research Synthesis]]
- **Want to build something?** → [[Research-Derived Improvements]], then
  [[Roadmap]]
- **Want to know what is true now?** → [[Current State]]

The repository remains executable truth; this graph is reasoning truth. Where
they disagree, check the code.""",
        links=[P1, P2, P3, P4, P5, P6, P7,
               "Research Questions Answered", "ClaudeInventor Research Synthesis",
               "Research-Derived Improvements", "Stress Singularity",
               "The skip threshold must be derived from measured error",
               "Screening models need automatic calibration",
               "A weakly correlated cheap model is worse than none",
               "Multi Fidelity Evaluation",
               "Simulation cost depends on the design",
               "Select simulations for information value, not predicted performance",
               "Refine where the question is, not everywhere",
               "Adaptivity cannot rescue a singular goal",
               "Deterministic feasibility is not feasibility under uncertainty",
               "Every safety factor used a strength the joints do not have",
               "A method with no refusal path does not belong in this engine",
               "A surrogate that screens may be sloppy, one that ranks may not",
               "The engine decides, the optimiser proposes",
               "Engineering Knowledge Base", "Optimization Engine",
               "Design Engine", "Validation Philosophy",
               "Current State", "Roadmap"])


def build(v: Vault) -> None:
    build_principles(v)
    build_papers(v)
    build_singularity_note(v)
    build_synthesis(v)
    build_improvements(v)
    build_questions(v)
    build_graph(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()
    v = Vault(args.root)
    print(f"Writing research notes into {v.root}")
    build(v)
    s = v.stats()
    print(f"\n  notes        : {s['notes']}")
    print(f"  by type      : {dict(sorted(s['by_type'].items()))}")
    broken = v.broken_links()
    if broken:
        print(f"\n  BROKEN LINKS ({len(broken)}) - a dangling link is a promise "
              f"the vault has not kept:")
        for src, target in broken:
            print(f"    {src} -> {target}")
        return 1
    print("  broken links : none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
