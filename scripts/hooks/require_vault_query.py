"""PreToolUse hook: block edits to the design layer without a fresh vault read.

Reads the Claude Code hook JSON from stdin, looks at the file the tool is
about to write, and if that file is part of the design/reasoning surface,
requires a `vault_query` action logged within the freshness window. This is
the enforcement half of `scripts/vault_query.py` — that module produces the
receipt; this refuses to proceed without one.

Exempted deliberately, not by omission:
  - tests/**            a test touching design_engine is verifying the tool,
                        not making a design decision
  - scripts/**           the tooling that produces the receipt, and the
                        bootstrap scripts, are infrastructure, not design work
  - design_engine/{memory,vault,log}.py
                        the second-brain plumbing itself. Gating edits to the
                        memory system behind a memory-system query is circular.
  - docs/**, *.md        documentation is not a design decision

Freshness window: 30 minutes. Long enough that answering a few of Claude's own
clarifying questions doesn't re-trigger it, short enough that a query from an
earlier, unrelated task doesn't count as having checked for this one.

Exit codes follow the Claude Code PreToolUse contract: 0 allows the tool call,
2 blocks it and surfaces stderr as the reason.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).parent.parent.parent   # scripts/hooks/ -> design-engine/

FRESHNESS_SECONDS = 30 * 60

_GATED = (re.compile(r"(^|[\\/])design_engine[\\/].*\.py$"),
          re.compile(r"(^|[\\/])designs[\\/].*\.py$"))

_EXEMPT = (re.compile(r"(^|[\\/])tests[\\/]"),
          re.compile(r"(^|[\\/])scripts[\\/]"),
          re.compile(r"(^|[\\/])docs[\\/]"),
          re.compile(r"design_engine[\\/]memory\.py$"),
          re.compile(r"design_engine[\\/]vault\.py$"),
          re.compile(r"design_engine[\\/]log\.py$"))


def is_gated(file_path: str) -> bool:
    p = file_path.replace("/", "\\")
    if any(rx.search(p) for rx in _EXEMPT):
        return False
    return any(rx.search(p) for rx in _GATED)


def check(file_path: str) -> tuple[bool, str]:
    """Returns (allowed, message)."""
    if not is_gated(file_path):
        return True, ""

    sys.path.insert(0, str(_HERE / "scripts"))
    import vault_query as vq          # local import: keep the hook's own
                                       # startup cheap for the common,
                                       # non-gated-file case

    age = vq.most_recent_seconds_ago(vq.DEFAULT_LOG)
    if age is None:
        return False, (
            f"No vault_query has ever been logged. Before editing "
            f"{file_path}, run:\n"
            f"    .venv\\Scripts\\python.exe scripts\\vault_query.py <topic>\n"
            f"and check Current State, the relevant Architecture Decisions, "
            f"and prior Failures for this area first.")
    if age > FRESHNESS_SECONDS:
        return False, (
            f"Last vault_query was {age/60:.0f} min ago (limit "
            f"{FRESHNESS_SECONDS//60} min). Before editing {file_path}, run:\n"
            f"    .venv\\Scripts\\python.exe scripts\\vault_query.py <topic>\n"
            f"with a topic naming what you're about to change.")
    return True, ""


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Malformed hook input is an environment problem, not a design-review
        # failure. Fail open rather than blocking every edit on a parser bug.
        return 0

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("path") or ""
    if not file_path:
        return 0

    allowed, message = check(file_path)
    if allowed:
        return 0
    print(message, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
