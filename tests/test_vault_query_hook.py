"""The vault-read receipt, and the hook that enforces it.

Context, because it explains every design choice under test: on 2026-08-27 an
instruction in CLAUDE.md to read the vault before substantial work was skipped.
The skipped read cost hours re-deriving a finding the vault already held. The
conclusion was not "try harder" — it was that an instruction living only in
prose is unfalsifiable, so the read now produces a row in the same FRACAS log
as `fea_static`, and a PreToolUse hook refuses gated edits without one.

What is under test is the enforcement boundary, not the search quality:

  - the gate covers design decisions and exempts tooling, tests and the
    memory plumbing itself (a memory-query gate on the memory system is
    circular and would deadlock)
  - a query leaves a real, queryable receipt
  - freshness is actually checked, not assumed
  - malformed hook input fails OPEN, because a parser bug must never brick
    every edit in the repo

Loaded by path, like the other second-brain tests, so it keeps running when the
CAD kernel does not.
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_HOOK = _ROOT / "scripts" / "hooks" / "require_vault_query.py"
_QUERY = _ROOT / "scripts" / "vault_query.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load("hook_under_test", _HOOK)
vq = _load("vq_under_test", _QUERY)


# --------------------------------------------------------------- the gate
@pytest.mark.parametrize("path", [
    r"C:\Users\rouka\Downloads\design-engine\design_engine\geometry.py",
    r"C:\Users\rouka\Downloads\design-engine\design_engine\fea.py",
    r"C:\Users\rouka\Downloads\design-engine\designs\jetpack_optimization_run.py",
    "design_engine/geometry.py",
    "designs/jetpack_optimization_run.py",
])
def test_design_decisions_are_gated(path):
    """Editing the engine or a design IS a design decision."""
    assert hook.is_gated(path.replace("/", "\\")) is True


@pytest.mark.parametrize("path,why", [
    (r"...\design-engine\tests\test_phase4.py", "a test verifies the tool"),
    (r"...\design-engine\scripts\bootstrap_vault.py", "tooling, not design"),
    (r"...\design-engine\scripts\hooks\require_vault_query.py", "the gate itself"),
    (r"...\design-engine\docs\ARCHITECTURE_AUDIT.md", "documentation"),
    (r"...\design-engine\design_engine\memory.py", "memory plumbing"),
    (r"...\design-engine\design_engine\vault.py", "vault plumbing"),
    (r"...\design-engine\design_engine\log.py", "the log the receipt lives in"),
    (r"...\Downloads\CLAUDE.md", "not a python design file"),
    (r"...\design-engine\README.md", "not a python design file"),
])
def test_exemptions_are_deliberate(path, why):
    assert hook.is_gated(path.replace("/", "\\")) is False, why


def test_the_memory_layer_is_exempt_or_the_gate_deadlocks():
    """Gating edits to the memory system behind a memory-system query would
    make the system unfixable from inside itself when it breaks."""
    for mod in ("memory", "vault", "log"):
        assert not hook.is_gated(rf"design_engine\{mod}.py")
    # but a sibling engineering module in the same package IS gated
    assert hook.is_gated(r"design_engine\fea.py")


# ------------------------------------------------------------- the receipt
def test_a_query_leaves_a_queryable_receipt(tmp_path):
    """The whole point: 'did you check' becomes a SELECT, not a claim."""
    vault = tmp_path / "vault" / "05_Failures"
    vault.mkdir(parents=True)
    (vault / "Meshing is non-monotonic.md").write_text(
        "---\ntype: failure\n---\n\n# Meshing is non-monotonic\n\nRefining "
        "a mesh does not monotonically improve it.\n", encoding="utf-8")
    db = tmp_path / "log.db"

    hits = vq.search(tmp_path / "vault", "meshing monotonic")
    assert hits and hits[0]["title"] == "Meshing is non-monotonic"

    action_id = vq.log_query(db, "meshing monotonic", hits)
    assert action_id > 0

    rows = vq.ActionLog(db).rows(action=vq.ACTION, result="pass")
    assert len(rows) == 1
    assert rows[0]["reason"] == "meshing monotonic"
    details = json.loads(rows[0]["details_json"])
    assert details["notes"] == ["Meshing is non-monotonic"]


def test_title_matches_outrank_body_mentions(tmp_path):
    """A note named after the topic is the one worth reading first."""
    v = tmp_path / "vault"
    v.mkdir()
    (v / "Solver memory bounds mesh refinement.md").write_text(
        "---\n---\n\n# x\n\nunrelated words\n", encoding="utf-8")
    (v / "Some Other Note.md").write_text(
        "---\n---\n\n# y\n\nsolver memory bounds mesh refinement mentioned "
        "in passing\n", encoding="utf-8")
    hits = vq.search(v, "solver memory mesh")
    assert hits[0]["title"] == "Solver memory bounds mesh refinement"


def test_templates_are_never_returned(tmp_path):
    """Template stubs match everything and inform nothing."""
    v = tmp_path / "vault"
    (v / "_Templates").mkdir(parents=True)
    (v / "_Templates" / "Template - Failure.md").write_text(
        "---\n---\n\nSymptom:\n\nRoot cause:\n", encoding="utf-8")
    assert vq.search(v, "symptom root cause") == []


def test_freshness_is_measured_not_assumed(tmp_path):
    db = tmp_path / "log.db"
    assert vq.most_recent_seconds_ago(db) is None      # never queried
    vq.log_query(db, "topic", [])
    age = vq.most_recent_seconds_ago(db)
    assert age is not None and age < 60


# ------------------------------------------------------- the hook contract
def _run_hook(payload_text):
    return subprocess.run([sys.executable, str(_HOOK)], input=payload_text,
                          capture_output=True, text=True, timeout=120)


def test_exempt_file_is_allowed_without_any_query():
    p = json.dumps({"tool_name": "Edit",
                    "tool_input": {"file_path": r"C:\repo\tests\test_x.py"}})
    assert _run_hook(p).returncode == 0


def test_malformed_input_fails_open():
    """A parser bug must never brick every edit in the repository. An
    unparseable payload is an environment problem, not a review failure."""
    assert _run_hook("not json at all").returncode == 0
    assert _run_hook("").returncode == 0


def test_payload_without_a_file_path_is_allowed():
    assert _run_hook(json.dumps({"tool_name": "Bash",
                                 "tool_input": {"command": "ls"}})).returncode == 0


def test_block_message_names_the_file_and_the_remedy(monkeypatch):
    """A refusal that does not say what to do next is just an obstacle."""
    monkeypatch.setattr(hook, "FRESHNESS_SECONDS", -1)
    target = r"C:\repo\designs\jetpack_optimization_run.py"
    allowed, message = hook.check(target)
    assert allowed is False
    assert "jetpack_optimization_run.py" in message
    assert "vault_query.py" in message


def test_no_query_ever_logged_blocks_with_its_own_message(monkeypatch):
    """A fresh checkout has no receipt at all — a distinct case from a stale
    one, and it gets its own message pointing at what to read first."""
    # The hook imports `vault_query` lazily inside check(), so patch the module
    # it will actually resolve, not this file's separately-loaded copy.
    sys.path.insert(0, str(_ROOT / "scripts"))
    import vault_query as live_vq
    monkeypatch.setattr(live_vq, "most_recent_seconds_ago", lambda _p: None)

    allowed, message = hook.check(r"C:\repo\designs\thing.py")
    assert allowed is False
    assert "has ever been logged" in message
    assert "vault_query.py" in message          # the remedy
    assert "Current State" in message           # what to actually read
