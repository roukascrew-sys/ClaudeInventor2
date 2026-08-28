"""Resolve the dependency graph between validators.

Phase 2 of the Declared Couplings proposal. Phase 1 split the validator
god-class; this makes the edges BETWEEN validators explicit, so that
"the modal result is stale because the mass model changed" becomes computable
rather than remembered.

WHAT IT DECIDES, AND WHAT IT DOES NOT
It determines what runs, in what order, and whether a result is stale. It
never decides whether a candidate passes. That stays in `design_engine/`,
because the architecture's load-bearing sentence is *the optimiser proposes,
the engine decides* — and a scheduler that started issuing verdicts would
quietly cross it.

THREE BEHAVIOURS, EACH AN EXISTING RULE ONE LEVEL UP

  UNMET DEPENDENCY IS UNKNOWN, NEVER A DEFAULT.
      `UNKNOWN is not a pass`, applied to coupling instead of solver failure.
      A stage whose `consumes` cannot be satisfied does not run with an
      assumed value; it reports UNKNOWN naming the fact it lacked.

  STALENESS IS COMPUTED.
      The evaluation cache already holds "anything that could change the
      answer changes the key". A fact digest is the same idea between stages:
      change the mass model and every modal and fatigue result downstream is
      stale by construction rather than by anyone noticing.

  A CYCLE IS REFUSED, NOT ORDERED.
      Real coupling can be cyclic — thermal to modulus to deflection to
      contact to thermal is a genuine loop. A DAG cannot express it, and
      quietly picking an evaluation order would be inventing an answer.
      Fixed-point iteration is a later, explicit stage type with a stated
      convergence criterion; until then this refuses.
"""

from __future__ import annotations

import hashlib
import json

from . import facts as _facts


class CouplingError(ValueError):
    """The declared graph cannot be resolved."""


class _Node:
    __slots__ = ("stage", "name", "consumes", "produces", "invalidates")

    def __init__(self, stage):
        self.stage = stage
        self.name = getattr(stage, "name", stage.__class__.__name__)
        ctx = f"stage {self.name!r}"
        self.consumes = _facts.validate(getattr(stage, "consumes", None),
                                        f"{ctx}.consumes")
        self.produces = _facts.validate(getattr(stage, "produces", None),
                                        f"{ctx}.produces")
        self.invalidates = _facts.validate(getattr(stage, "invalidates", None),
                                           f"{ctx}.invalidates")


class CouplingGraph:
    """The declared dependencies among a list of stages.

    Stages that declare nothing behave exactly as before: empty sets satisfy
    every check, so this is additive and every existing stage keeps working
    untouched.
    """

    def __init__(self, stages):
        self.nodes = [_Node(s) for s in stages]
        self._producers: dict[str, list[str]] = {}
        for n in self.nodes:
            for f in n.produces:
                self._producers.setdefault(f, []).append(n.name)

    # ----------------------------------------------------------- inspection
    def declared(self) -> bool:
        """Has anyone actually declared anything? Used to keep the resolution
        out of the log for runs that are not using the feature."""
        return any(n.consumes or n.produces or n.invalidates for n in self.nodes)

    def cycles(self) -> list[list[str]]:
        """Cycles in the stage dependency graph, by stage name.

        Edge A -> B when B consumes something A produces. Depth-first with a
        colouring, so the reported cycle is the actual path rather than merely
        the set of nodes involved in one.
        """
        adj = {n.name: set() for n in self.nodes}
        for n in self.nodes:
            for f in n.consumes:
                for producer in self._producers.get(f, ()):
                    if producer != n.name:
                        adj[producer].add(n.name)

        WHITE, GREY, BLACK = 0, 1, 2
        colour = {name: WHITE for name in adj}
        found: list[list[str]] = []

        def walk(name, path):
            colour[name] = GREY
            path.append(name)
            for nxt in sorted(adj[name]):
                if colour[nxt] == GREY:
                    found.append(path[path.index(nxt):] + [nxt])
                elif colour[nxt] == WHITE:
                    walk(nxt, path)
            path.pop()
            colour[name] = BLACK

        for name in sorted(adj):
            if colour[name] == WHITE:
                walk(name, [])
        return found

    def order(self) -> list[str]:
        """Stage names in an order that satisfies the declared dependencies.

        Refuses on a cycle rather than returning a plausible order.
        """
        cyc = self.cycles()
        if cyc:
            pretty = "; ".join(" -> ".join(c) for c in cyc)
            raise CouplingError(
                f"cyclic coupling: {pretty}. A dependency graph cannot express "
                f"a feedback loop, and choosing an evaluation order for one "
                f"would be inventing an answer. Break the cycle, or model it "
                f"explicitly as a fixed-point iteration with a stated "
                f"convergence criterion.")

        adj = {n.name: set() for n in self.nodes}
        indeg = {n.name: 0 for n in self.nodes}
        for n in self.nodes:
            for f in n.consumes:
                for producer in self._producers.get(f, ()):
                    if producer != n.name and n.name not in adj[producer]:
                        adj[producer].add(n.name)
                        indeg[n.name] += 1
        # Ties broken by the order the caller listed the stages, so a graph
        # with no constraints reproduces the existing ladder exactly.
        listed = {n.name: i for i, n in enumerate(self.nodes)}
        ready = sorted([k for k, d in indeg.items() if d == 0], key=listed.get)
        out: list[str] = []
        while ready:
            name = ready.pop(0)
            out.append(name)
            for nxt in sorted(adj[name], key=listed.get):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
            ready.sort(key=listed.get)
        return out

    def unsatisfiable(self) -> dict[str, list[str]]:
        """Stage name -> facts no stage in this run produces.

        Reported before anything runs, because a modelling gap is worth
        knowing about in advance of spending solver time.
        """
        out: dict[str, list[str]] = {}
        for n in self.nodes:
            missing = sorted(f for f in n.consumes if f not in self._producers)
            if missing:
                out[n.name] = missing
        return out

    def report(self) -> dict:
        """What the graph resolved to, for the FRACAS log.

        Logged so that "was this result computed with the attached mass?" is a
        query rather than an assumption — the same move as the `vault_query`
        receipt.
        """
        gaps = self.unsatisfiable()
        return {
            "stages": [
                {"name": n.name,
                 "consumes": sorted(n.consumes),
                 "produces": sorted(n.produces),
                 "invalidates": sorted(n.invalidates)}
                for n in self.nodes],
            "order": self.order(),
            "unsatisfiable": gaps,
            "engine_gaps": {
                fact: _facts.why_unproduced(fact)
                for facts_ in gaps.values() for fact in facts_
                if fact in _facts.UNPRODUCED_TODAY},
        }


class FactStore:
    """Facts established during one candidate's evaluation, with digests.

    The digest is what makes staleness computable: a consumer's key includes
    the digest of everything it consumed, so a changed upstream fact changes
    the consumer's key and its cached result cannot be reused.
    """

    def __init__(self):
        self._values: dict[str, object] = {}
        self._digests: dict[str, str] = {}

    def establish(self, fact: str, value=None) -> None:
        _facts.validate([fact], "FactStore.establish")
        self._values[fact] = value
        self._digests[fact] = _digest(value)

    def retract(self, fact: str) -> None:
        """Remove a fact another stage has invalidated."""
        self._values.pop(fact, None)
        self._digests.pop(fact, None)

    def has(self, fact: str) -> bool:
        return fact in self._values

    def missing(self, consumes) -> list[str]:
        return sorted(f for f in consumes if f not in self._values)

    def digest_of(self, consumes) -> str:
        """Combined digest of the facts a stage consumed.

        Empty for a stage that consumes nothing, so its cache key is unchanged
        from before this feature existed.
        """
        parts = [f"{f}={self._digests.get(f, '')}" for f in sorted(consumes)]
        if not parts:
            return ""
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _digest(value) -> str:
    """Stable digest of a fact's value.

    Falls back to the type name for anything that will not serialise — a CAD
    solid, for instance. That is deliberately weak: it means "this fact
    exists" rather than "this fact has this value", and a stage relying on it
    for staleness gets a conservative answer rather than a wrong one.
    """
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = f"<{type(value).__name__}>"
    return hashlib.sha256(blob.encode()).hexdigest()[:16]
