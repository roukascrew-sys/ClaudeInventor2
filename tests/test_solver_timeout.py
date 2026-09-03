"""The solver deadline must bind on the process TREE, not on one handle.

Same mechanism as `tests/test_chrono_bridge.py`, one module over. When a
subprocess exceeds its timeout, killing it and then calling `communicate()`
with no timeout does NOT bound the wait: on Windows that second call blocks
until every process still holding the inherited stdout/stderr pipe write
handles has exited, and `Popen.kill()` is `TerminateProcess` on a single
handle, so any grandchild survives and holds the parent for as long as it
likes. On 2026-08-28 that shape overshot a 900 s deadline by ~1341 s in the
Chrono bridge and took the suite from ~2 min to 38m49s.

In `design_engine/fea.py` the same pattern was LATENT, not active: the solver
is spawned as a direct executable with no wrapper, and the shipped CalculiX
2.23.0 binaries cannot spawn a child at all (pinned below). These tests hold
both halves — the guard, and the assumption that made it only a guard — so
that pointing `ccx_path` at a wrapper script cannot quietly restore the hang.

No solver is needed for any of this. A `sleep` reproduces it exactly.\n"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from design_engine import DesignEngine, _DEFAULT_CCX
from design_engine import fea as fea_mod
from design_engine.fea import FeaError, _SolverTimeout, _run_solver

# Small, because what is measured is the OVERSHOOT, not the wait.
DEADLINE_S = 3
# Generous: kill_tree allows a 10 s grace and taskkill is not synchronous
# under load. Still ~45x tighter than the 1341 s overshoot being regressed
# against, and ~20x tighter than the grandchild's own 600 s lifetime, which
# is what the unbounded reap would have waited for.
MAX_OVERSHOOT_S = 30.0

#: The REAL interpreter, not this venv's launcher stub.
#: Measured 2026-08-28: `.venv/Scripts/python.exe` does not exec, it
#: CreateProcess-es the interpreter named in pyvenv.cfg and waits. Popen
#: therefore holds the stub, whose peak working set is 4.1 MB no matter
#: what the interpreter does (266.1 MB for the same script run directly).
#: Fatal to a memory assertion, which must measure the process doing the
#: work. An ASSET to the lifetime tests below, which keep sys.executable
#: deliberately: the stub makes the hung_solver fixture four levels deep,
#:     _run_solver -> stub -> solver.py -> stub -> grandchild.py
#: and the pid asserted dead is the one the DEEPEST process wrote for
#: itself. So `taskkill /T /F` is pinned across four levels, not the two
#: the fixture was written to produce.
BASE_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def _alive(pid: int) -> bool:
    """Is `pid` still running? `os.kill(pid, 0)` is not a probe on Windows —
    Python maps it to TerminateProcess, which would kill what it asks about."""
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@pytest.fixture
def hung_solver(tmp_path):
    """A "solver" that spawns a long-lived grandchild and then hangs itself.

    The grandchild inherits stdout/stderr — i.e. the parent's pipe write
    handles — because that is exactly what a wrapper script does, and it is
    the inheritance, not the sleeping, that makes an unbounded reap block.
    """
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n", encoding="utf-8")
    script = tmp_path / "solver.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(600)\n", encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"
    return [sys.executable, str(script), str(grandchild), str(pidfile)], pidfile


def test_a_hung_solver_is_reaped_at_its_deadline_not_at_its_grandchilds_leisure(
        tmp_path, hung_solver):
    cmd, _ = hung_solver
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run_solver(cmd, tmp_path, dict(os.environ), DEADLINE_S)
    elapsed = time.monotonic() - t0
    assert elapsed < DEADLINE_S + MAX_OVERSHOOT_S, (
        f"the solver was reaped {elapsed - DEADLINE_S:.1f}s past its "
        f"{DEADLINE_S}s deadline. An unbounded post-kill communicate() waits "
        f"for the surviving grandchild (600s here); a real solve_timeout_s of "
        f"600-900s makes that wait unbounded in practice")


def test_the_kill_reaches_the_grandchild_that_a_bare_kill_would_orphan(
        tmp_path, hung_solver):
    cmd, pidfile = hung_solver
    with pytest.raises(subprocess.TimeoutExpired):
        _run_solver(cmd, tmp_path, dict(os.environ), DEADLINE_S)
    assert pidfile.is_file(), "the grandchild never started; test is not testing"
    pid = int(pidfile.read_text())
    for _ in range(20):                      # taskkill /T is not synchronous
        if not _alive(pid):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)] if os.name == "nt"
                       else ["kill", "-9", str(pid)], capture_output=True)
        pytest.fail(f"grandchild {pid} outlived the kill — an orphan holding "
                    f"the solver's pipes is what makes the reap unbounded")


def test_a_solver_that_finishes_in_time_still_reports_output_and_peak_memory(
        tmp_path):
    """The timeout path must not have cost the normal path anything.

    Peak working set is why this uses Popen rather than subprocess.run at all
    (fea.py:_run_solver), so it is asserted, not assumed.
    """
    script = tmp_path / "quick.py"
    script.write_text("import sys\n"
                      "sys.stdout.write('Job finished')\n"
                      "sys.stderr.write('noted')\n"
                      "sys.exit(3)\n", encoding="utf-8")
    run = _run_solver([sys.executable, str(script)], tmp_path,
                      dict(os.environ), 60)
    assert run.returncode == 3
    assert run.stdout == "Job finished"
    assert run.stderr == "noted"
    if os.name == "nt":
        assert run.peak_rss_mb and run.peak_rss_mb > 0, (
            "peak memory went missing; a solve that dies at 6 GB and one that "
            "dies at 500 MB are different failures")


@pytest.mark.skipif(os.name != "nt", reason="peak working set is a Win32 API")
def test_a_timed_out_solve_reports_the_memory_it_died_holding(tmp_path):
    """The peak must survive the timeout, and must be read BEFORE the kill.

    Reading it after `TerminateProcess` yields nothing — a solve that
    transiently held 6 GB shows almost none of it by the time it is gone — and
    a measurement that is taken and then dropped is the same as no measurement.
    That is the whole reason this path uses Popen rather than subprocess.run.

    The fake solver claims a quarter of a gigabyte before it hangs, which no
    bare interpreter approaches, so a passing assertion cannot be the process's
    own baseline footprint. It runs under BASE_PYTHON: see that constant for
    why the venv stub would report 4.1 MB for the same work.
    """
    script = tmp_path / "greedy.py"
    script.write_text(
        "import time\n"
        "hog = bytearray(256 * 1024 * 1024)\n"
        "hog[::4096] = b'x' * len(hog[::4096])\n"   # touch it
        "time.sleep(600)\n", encoding="utf-8")
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        _run_solver([BASE_PYTHON, str(script)], tmp_path,
                    dict(os.environ), DEADLINE_S)
    peak = getattr(caught.value, "peak_rss_mb", None)
    assert peak is not None, (
        "the timeout carried no memory measurement; a timeout at 6 GB and one "
        "at 500 MB want different remedies and now look identical in the log")
    assert peak > 200, (
        f"peak {peak} MB is below the 256 MB the fake solver allocated, which "
        f"means it was read after the kill rather than before it")


def test_the_timeout_message_names_the_peak_so_the_log_records_it(
        tmp_path, monkeypatch):
    """`_solve` is where the number stops being a float and starts being
    evidence: its FeaError text is what reaches the FRACAS log."""
    eng = DesignEngine(tmp_path / "data")

    def fake_run_solver(cmd, cwd, env, timeout_s):
        raise _SolverTimeout(cmd, timeout_s, 6144.0)

    monkeypatch.setattr(fea_mod, "_run_solver", fake_run_solver)
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 504_000, what="static")
    assert "6144 MB" in str(caught.value)
    assert "solver_timeout" in str(caught.value)


def test_a_timeout_with_no_measurement_still_reports_the_deadline(
        tmp_path, monkeypatch):
    """None must read as missing, never as zero. A plain TimeoutExpired from
    anywhere else must not crash the handler on a missing attribute."""
    eng = DesignEngine(tmp_path / "data")

    def fake_run_solver(cmd, cwd, env, timeout_s):
        raise subprocess.TimeoutExpired(cmd, timeout_s)

    monkeypatch.setattr(fea_mod, "_run_solver", fake_run_solver)
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 504_000, what="static")
    assert "solver_timeout" in str(caught.value)
    assert "MB" not in str(caught.value), "invented a memory figure it never had"


@pytest.mark.skipif(not _DEFAULT_CCX.is_file(),
                    reason=f"CalculiX not installed at {_DEFAULT_CCX}")
@pytest.mark.parametrize("name", ["ccx.exe", "ccx_MT.exe"])
def test_the_shipped_calculix_binaries_cannot_spawn_a_child_process(name):
    """Why the unbounded reap above was latent here rather than active.

    The solver is invoked as `[str(binary), "-i", "job"]` — a direct PE
    executable, no wrapper script — and the shipped 2.23.0 win-x64 binaries
    contain no process-creation API anywhere in their bytes. Their import
    tables are pthread/libgomp/msvcrt/KERNEL32 only, so ccx parallelism is
    THREADS, not processes: `proc.kill()` really does close the pipes today.

    Byte-scan rather than import-table parse on purpose: `GetProcAddress` IS
    imported (ordinary CRT startup), so absence from the import table alone
    would not rule out dynamic resolution — absence of the name from the file
    does.

    If this test ever fails, the guard in `_run_solver` stopped being
    insurance and started being load-bearing. Do not weaken it.
    """
    binary = _DEFAULT_CCX.with_name(name)
    if not binary.is_file():
        pytest.skip(f"{name} not present")
    blob = binary.read_bytes()
    for api in (b"CreateProcess", b"ShellExecute", b"_popen", b"_spawn",
                b"_execv", b"WinExec"):
        assert api not in blob, (
            f"{name} references {api.decode()}: the solver can spawn a child, "
            f"so a killed solve can leave a grandchild holding its pipes")


@pytest.mark.skipif(not _DEFAULT_CCX.is_file(),
                    reason=f"CalculiX not installed at {_DEFAULT_CCX}")
def test_the_configured_solver_is_an_executable_not_a_wrapper_script():
    """A .bat/.cmd wrapper reinstates the grandchild the byte-scan rules out —
    cmd.exe would be the child and ccx the grandchild."""
    assert _DEFAULT_CCX.suffix.lower() == ".exe"
    assert _DEFAULT_CCX.read_bytes()[:2] == b"MZ", "not a PE executable"


# --------------------------------------------- failure numbers as FIELDS, not prose
# Added 2026-09-02. The tests above pin that the timeout MESSAGE names the peak.
# That was not enough: action #346 recorded "exceeded 2400s on a submodel 0.2mm
# solve of 478512 nodes ... Peak memory at the kill: 4437 MB" with an empty
# details_json, so the one run showing a submodel cost >3.4x its node-implied
# price could not be fitted, only read. These pin the same numbers as fields.

def test_a_timeout_records_its_numbers_as_fields_not_only_as_prose(
        tmp_path, monkeypatch):
    eng = DesignEngine(tmp_path / "data")

    def fake_run_solver(cmd, cwd, env, timeout_s):
        raise _SolverTimeout(cmd, timeout_s, 4437.0)

    monkeypatch.setattr(fea_mod, "_run_solver", fake_run_solver)
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 478_512, what="submodel 0.2mm")

    d = caught.value.details
    assert d is not None, "the numbers were formatted into prose and discarded"
    assert d["failure_kind"] == "solver_timeout"
    assert d["nodes"] == 478_512
    assert d["peak_rss_mb"] == 4437.0
    assert d["solve_kind"] == "submodel 0.2mm"
    assert d["threads"] >= 1
    # A killed solve is CENSORED. Anything fitting this must be able to tell
    # "cost at least this" from "cost exactly this", or the cost model learns
    # that huge meshes are cheap because they were all stopped early.
    assert d["solve_seconds_is_lower_bound"] is True
    assert d["peak_rss_is_lower_bound"] is True
    assert "solve_seconds" not in d, (
        "a censored time must not be recorded under the same key as a "
        "completed one")


def test_a_timeout_with_no_measurement_records_none_not_zero(
        tmp_path, monkeypatch):
    """Zero memory is a measurement. Missing memory is not."""
    eng = DesignEngine(tmp_path / "data")

    def fake_run_solver(cmd, cwd, env, timeout_s):
        raise subprocess.TimeoutExpired(cmd, timeout_s)

    monkeypatch.setattr(fea_mod, "_run_solver", fake_run_solver)
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 1000, what="static")
    assert caught.value.details["peak_rss_mb"] is None


def test_a_crashed_solve_is_censored_too_not_treated_as_completed(
        tmp_path, monkeypatch):
    """A crash is not a completed measurement.

    This test asserted the OPPOSITE when it was written, on the reasoning that
    the process "ran to completion, badly". A non-zero exit means ccx aborted,
    usually mid-factorisation, so the time is a lower bound on what finishing
    costs — exactly like a timeout. Three real global solves died this way at
    442k-642k nodes in 322-530 s; recorded as completed they would have been
    the largest meshes on file and among the fastest for their size.
    """
    eng = DesignEngine(tmp_path / "data")

    class _Bad:
        returncode = 3221225477                # 0xC0000005
        stdout = "*ERROR in u_calloc"
        peak_rss_mb = 8192.0

    monkeypatch.setattr(fea_mod, "_run_solver",
                        lambda cmd, cwd, env, timeout_s: _Bad())
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 250_000, what="static")

    d = caught.value.details
    assert d["failure_kind"] == "solver_error"
    assert d["returncode"] == 3221225477
    assert d["peak_rss_mb"] == 8192.0
    assert d["nodes"] == 250_000
    assert d["solve_seconds_is_lower_bound"] is True
    assert d["peak_rss_is_lower_bound"] is True
    assert "solve_seconds" not in d, (
        "a crashed solve must not occupy the key a completed one uses")


def test_a_clean_exit_without_finishing_is_censored_as_well(
        tmp_path, monkeypatch):
    """returncode 0 but no 'Job finished' means it stopped early. Same rule."""
    eng = DesignEngine(tmp_path / "data")

    class _Quiet:
        returncode = 0
        stdout = "*INFO reading input deck"     # never reports finishing
        peak_rss_mb = 512.0

    monkeypatch.setattr(fea_mod, "_run_solver",
                        lambda cmd, cwd, env, timeout_s: _Quiet())
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 60_000, what="static")
    assert caught.value.details["solve_seconds_is_lower_bound"] is True


def test_the_action_wrapper_writes_failure_details_to_the_log(tmp_path):
    """The mechanism, end to end.

    `_action` used to close the exception path with a failure_mode string and
    nothing else, so EVERY failed validation row in the project has an empty
    details_json. This is the regression against that.
    """
    import json
    eng = DesignEngine(tmp_path / "data")

    with pytest.raises(FeaError):
        with eng.validation._action("fea_static", "P0001@v1", "a test") as aid:
            raise FeaError("boom", details={"nodes": 123, "peak_rss_mb": 45.6})

    row = [r for r in eng.log.rows(action="fea_static") if r["id"] == aid][0]
    assert row["result"] == "fail"
    assert "boom" in row["failure_mode"]
    d = json.loads(row["details_json"])
    assert d["nodes"] == 123 and d["peak_rss_mb"] == 45.6


def test_an_exception_carrying_no_details_still_closes_the_action(tmp_path):
    """A MeshError, a KeyError, anything at all. Opting in must stay optional
    or the wrapper stops being a safety net and becomes a second failure."""
    eng = DesignEngine(tmp_path / "data")

    with pytest.raises(KeyError):
        with eng.validation._action("fea_static", "P0001@v1", "a test") as aid:
            raise KeyError("worst_outside_mm")

    row = [r for r in eng.log.rows(action="fea_static") if r["id"] == aid][0]
    assert row["result"] == "fail"
    assert "worst_outside_mm" in row["failure_mode"]


def test_any_exception_can_opt_in_by_setting_the_attribute(tmp_path):
    """`getattr`, not isinstance: the submodel ladder attaches its partial
    rungs to whatever came out of the loop, including a MeshError."""
    import json
    from design_engine.mesh import MeshError
    eng = DesignEngine(tmp_path / "data")

    with pytest.raises(MeshError):
        with eng.validation._action("fea_submodel", "P0001@v1", "a test") as aid:
            exc = MeshError("degenerate_mesh: 8 of 4547 elements")
            exc.details = {"partial": True, "completed_rungs": 2,
                           "rungs": [{"mesh_mm": 0.8, "nodes": 60_000,
                                      "solve_seconds": 41.2}]}
            raise exc

    row = [r for r in eng.log.rows(action="fea_submodel") if r["id"] == aid][0]
    d = json.loads(row["details_json"])
    assert d["completed_rungs"] == 2
    assert d["rungs"][0]["nodes"] == 60_000


# ------------------------------------------------------ the memory gate
# Added 2026-09-02. The engine had an accurate memory model since 2026-08-27
# and never consulted it; that evening three global solves ran 322, 332 and
# 530 s and then died at 0xC0000005 reaching for 7.1-9.1 GB with ~6 GB free.
# Twenty minutes to learn something two existing functions knew already.

class _FakeKB:
    """Stands in for KnowledgeBase so the gate is tested, not the fit."""

    def __init__(self, high_mb=None, avail=None, n=13):
        self._high, self._avail, self._n = high_mb, avail, n

    def predict_memory(self, nodes):
        if self._high is None:
            return None
        return {"nodes": nodes, "estimate_mb": self._high / 1.05,
                "low_mb": self._high / 1.1, "high_mb": self._high,
                "n": self._n, "band_multiplier": 1.05}

    def available_memory_mb(self):
        return self._avail


def _gated(tmp_path, monkeypatch, kb, nodes=442_725):
    """Run _solve with a solver that would explode if it were ever reached."""
    eng = DesignEngine(tmp_path / "data")
    monkeypatch.setattr(eng.validation, "_knowledge", lambda: kb)

    def _must_not_run(cmd, cwd, env, timeout_s):     # pragma: no cover
        raise AssertionError("the solver ran despite the memory gate")

    monkeypatch.setattr(fea_mod, "_run_solver", _must_not_run)
    return eng


def test_a_solve_the_machine_cannot_hold_is_refused_before_it_runs(
        tmp_path, monkeypatch):
    eng = _gated(tmp_path, monkeypatch, _FakeKB(high_mb=7908.6, avail=6048.1))
    with pytest.raises(FeaError) as caught:
        eng.validation._solve(tmp_path, 442_725, what="static")

    msg = str(caught.value)
    assert "insufficient_memory" in msg
    d = caught.value.details
    assert d["failure_kind"] == "insufficient_memory"
    assert d["nodes"] == 442_725
    assert d["available_mb"] == 6048.1
    assert d["shortfall_mb"] == pytest.approx(1860.5, abs=0.2)
    assert d["refused_before_solving"] is True
    # No solver ran, so there is no timing. Inventing one - even a censored
    # one - would put a fictional point in reach of the cost model.
    assert "solve_seconds" not in d and "solve_seconds_at_kill" not in d
    assert "peak_rss_mb" not in d


def test_a_solve_that_fits_is_not_blocked(tmp_path, monkeypatch):
    """The gate must not become a reason work does not happen."""
    eng = DesignEngine(tmp_path / "data")
    monkeypatch.setattr(eng.validation, "_knowledge",
                        lambda: _FakeKB(high_mb=1200.0, avail=6048.1))

    class _Ok:
        returncode = 0
        stdout = "Job finished"
        peak_rss_mb = 1100.0

    monkeypatch.setattr(fea_mod, "_run_solver",
                        lambda cmd, cwd, env, timeout_s: _Ok())
    proc, binary, threads, seconds = eng.validation._solve(
        tmp_path, 60_000, what="static")
    assert proc.returncode == 0
    assert not (tmp_path / "memory_warning.txt").exists()


def test_a_thin_history_does_not_gate(tmp_path, monkeypatch):
    """Refusing on absent knowledge would be worse than not gating at all."""
    eng = DesignEngine(tmp_path / "data")
    monkeypatch.setattr(eng.validation, "_knowledge",
                        lambda: _FakeKB(high_mb=None, avail=100.0))

    class _Ok:
        returncode = 0
        stdout = "Job finished"
        peak_rss_mb = 10.0

    monkeypatch.setattr(fea_mod, "_run_solver",
                        lambda cmd, cwd, env, timeout_s: _Ok())
    eng.validation._solve(tmp_path, 900_000, what="static")   # must not raise


def test_unreadable_available_memory_does_not_gate(tmp_path, monkeypatch):
    """available_memory_mb() uses a Windows API and returns None elsewhere.
    An unknown is not a refusal."""
    eng = DesignEngine(tmp_path / "data")
    monkeypatch.setattr(eng.validation, "_knowledge",
                        lambda: _FakeKB(high_mb=99_000.0, avail=None))

    class _Ok:
        returncode = 0
        stdout = "Job finished"
        peak_rss_mb = 10.0

    monkeypatch.setattr(fea_mod, "_run_solver",
                        lambda cmd, cwd, env, timeout_s: _Ok())
    eng.validation._solve(tmp_path, 900_000, what="static")   # must not raise


def test_a_solve_that_will_page_is_allowed_but_flagged(tmp_path, monkeypatch):
    """A slow answer is still an answer, so this band is recorded, not refused.

    5,091 MB predicted against 5,484 MB free is the real 2026-09-02 case: it
    completed, in 1282 s against a 259 s upper bound.
    """
    eng = DesignEngine(tmp_path / "data")
    monkeypatch.setattr(eng.validation, "_knowledge",
                        lambda: _FakeKB(high_mb=5091.0, avail=5484.0))

    class _Slow:
        returncode = 0
        stdout = "Job finished"
        peak_rss_mb = 5986.1

    monkeypatch.setattr(fea_mod, "_run_solver",
                        lambda cmd, cwd, env, timeout_s: _Slow())
    eng.validation._solve(tmp_path, 297_794, what="static")
    warn = (tmp_path / "memory_warning.txt").read_text(encoding="utf-8")
    assert "paging risk" in warn
    assert "5091" in warn and "5484" in warn


def test_the_gate_reaches_every_solve_kind(tmp_path, monkeypatch):
    """It sits in _solve, which static, buckling, modal and every submodel
    rung all pass through. Pinned so a future caller cannot route around it."""
    import inspect
    src = inspect.getsource(fea_mod.ValidationTools._solve)
    assert "_memory_gate" in src
    gate_line = next(i for i, ln in enumerate(src.splitlines())
                     if "_memory_gate" in ln)
    run_line = next(i for i, ln in enumerate(src.splitlines())
                    if "_run_solver(" in ln)
    assert gate_line < run_line, "the gate must precede the solver call"
