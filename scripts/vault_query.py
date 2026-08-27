"""Query the second brain, and leave a receipt that it was queried.

The point this module exists to fix: on 2026-08-27 an instruction in
CLAUDE.md to read the vault before substantial work was skipped, and the
skipped read cost hours re-deriving a finding ("Numerical artifacts must not
steer search") that the vault already had. An instruction that only lives in
prose is unfalsifiable — nobody, including the one skipping it, can tell from
the transcript alone whether it happened.

So a vault read is logged as an action in the SAME FRACAS log that
`fea_static` and `create_part` write to — the project's own rule, applied to
itself: an unlogged claim didn't happen. `require_vault_query.py` (a
PreToolUse hook) enforces that a fresh row exists before Edit/Write can touch
`design_engine/**` or `designs/**`; this module is what produces that row.

Stdlib-only, like `vault.py`, `memory.py` and `inventor/knowledge.py` — it has
to work in the same breath as the hook that gates the CAD kernel's own
callers, so it cannot itself depend on the CAD kernel.

    .venv\\Scripts\\python.exe scripts\\vault_query.py <topic words...>
    .venv\\Scripts\\python.exe scripts\\vault_query.py --recent 180   # freshness check
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from pathlib import Path

_HERE = Path(__file__).parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        name, _HERE / "design_engine" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_log = _load("vq_log", "log.py")
ActionLog = _log.ActionLog

DEFAULT_VAULT = Path.home() / "Downloads" / "ClaudeInventor"
DEFAULT_LOG = _HERE / "data" / "design_engine.db"
ACTION = "vault_query"

_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)
_STOP = {"the", "a", "an", "is", "are", "of", "to", "for", "on", "in", "and",
         "or", "does", "do", "did", "with", "what", "how", "why", "at", "it"}


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOP}


def search(vault_root: Path, query: str, limit: int = 8) -> list[dict]:
    """Rank vault notes by keyword overlap with the query.

    Deliberately simple — word-set overlap, no embeddings, no ranking model —
    because the vault is a few dozen notes and a method that needs no external
    dependency is what keeps this usable when the CAD kernel is not.
    """
    q_words = _tokenize(query)
    if not q_words:
        return []
    hits = []
    for path in sorted(vault_root.rglob("*.md")):
        if path.parent.name == "_Templates":
            continue
        text = path.read_text(encoding="utf-8")
        m = _FM.match(text)
        body = text[m.end():] if m else text
        title = path.stem
        title_words = _tokenize(title)
        body_words = _tokenize(body)
        # a title match counts far more than a body match: a note titled
        # after the topic is almost always the one worth reading first
        score = 3 * len(q_words & title_words) + len(q_words & body_words)
        if score > 0:
            snippet_line = next(
                (l.strip() for l in body.splitlines()
                 if l.strip() and not l.startswith(("#", "**Type", "-"))), "")
            hits.append({"title": title, "path": str(path.relative_to(vault_root)),
                        "score": score, "snippet": snippet_line[:160]})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def log_query(log_path: Path, topic: str, hits: list[dict]) -> int:
    log = ActionLog(log_path)
    action_id = log.open_action("continuous-memory", ACTION, reason=topic)
    log.close_action(action_id, "pass", details={
        "topic": topic, "note_count": len(hits),
        "notes": [h["title"] for h in hits]})
    return action_id


def most_recent_seconds_ago(log_path: Path) -> float | None:
    """Age in seconds of the most recent logged vault_query, or None if there
    has never been one. Used by the hook to decide whether a read is fresh."""
    import datetime
    if not log_path.exists():
        return None
    log = ActionLog(log_path)
    rows = log.rows(action=ACTION, result="pass")
    if not rows:
        return None
    last = max(rows, key=lambda r: r["timestamp"])
    ts = datetime.datetime.fromisoformat(last["timestamp"])
    now = datetime.datetime.now(datetime.timezone.utc)
    return (now - ts).total_seconds()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("topic", nargs="*", help="query words, e.g. mesh convergence outlier")
    ap.add_argument("--root", type=Path, default=DEFAULT_VAULT)
    ap.add_argument("--log", type=Path, default=DEFAULT_LOG)
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--recent", type=int, metavar="SECONDS",
                    help="print the age of the last logged query and exit "
                         "0 if within SECONDS, 1 otherwise; used by the hook")
    args = ap.parse_args()

    if args.recent is not None:
        age = most_recent_seconds_ago(args.log)
        if age is None:
            print("no vault_query has ever been logged")
            return 1
        print(f"last vault_query was {age:.0f}s ago (limit {args.recent}s)")
        return 0 if age <= args.recent else 1

    if not args.topic:
        ap.error("a topic is required unless --recent is given")
    topic = " ".join(args.topic)

    if not args.root.exists():
        print(f"vault not found at {args.root}", file=sys.stderr)
        return 2

    hits = search(args.root, topic, args.limit)
    action_id = log_query(args.log, topic, hits)

    print(f"vault query: {topic!r}  (logged as action {action_id})")
    if not hits:
        print("  no matching notes")
    for h in hits:
        print(f"  [{h['score']:2d}] {h['title']}")
        print(f"        {h['path']}")
        if h["snippet"]:
            print(f"        {h['snippet']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
