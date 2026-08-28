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

No solver is needed for any of this. A `sleep` reproduces it exactly.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from design_engine import _DEFAULT_CCX
from design_engine.fea import _run_solver

# Small, because what is measured is the OVERSHOOT, not the wait.
DEADLINE_S = 3
# Generous: kill_tree allows a 10 s grace and taskkill is not synchronous
# under load. Still ~45x tighter than the 1341 s overshoot being regressed
# against, and ~20x tighter than the grandchild's own 600 s lifetime, which
# is what the unbounded reap would have waited for.
MAX_OVERSHOOT_S = 30.0


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
