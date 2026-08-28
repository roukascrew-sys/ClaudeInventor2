"""The Chrono subprocess bridge: the deadline must actually bind.

On 2026-08-28 two full-suite runs hung in tests/test_kinematics.py while the
same tests passed in isolation, and the suite went from ~2 min to 38m49s. The
worker was reported killed *1341 s past* its 900 s deadline. Two separate
defects made that possible, and both are pinned here:

1. `conda run` interposes a process, so the worker was a **grandchild**.
   `Popen.kill()` on Windows is TerminateProcess on one handle and never
   reached it. Worse, `subprocess.run`'s Windows timeout path calls
   `communicate()` a second time after `kill()` **with no timeout**, and that
   call blocks until every holder of the inherited pipe write handles exits —
   i.e. until the surviving worker feels like stopping.

2. Without conda's activation PATH the worker can block *forever*:
   `import pychrono` loads Irrlicht/SDL from `<env>/Library/bin`, and a failed
   load raises a modal Windows error box in a process with no interactive
   user. Measured that day: >9 min with no CPU and no output before it was
   killed by hand; 3.2 s with the PATH present.

These tests deliberately do not need PyChrono. The bridge's failure mode is
about process lifetime, and a `sleep` reproduces it exactly.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from design_engine.kinematics import (activation_env, chrono_available,
                                      env_python, find_env_dir, run_worker)

# The worker's deadline in these tests. Small, because what is being measured
# is the overshoot, not the wait.
DEADLINE_S = 3.0
# Generous: kill_tree allows a 10 s grace, and taskkill is not instant under
# load. Still ~450x tighter than the 1341 s overshoot being regressed against.
MAX_OVERSHOOT_S = 30.0


def _alive(pid: int) -> bool:
    """Is `pid` still running? (os.kill(pid, 0) is not safe on Windows —
    Python maps it to TerminateProcess, which would kill what it asks about.)"""
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
def hung_worker(tmp_path):
    """A worker that spawns a long-lived grandchild and then hangs itself.

    This is the shape `conda run` produced: the process the parent holds a
    handle to is not the process doing the work.
    """
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(600)\n", encoding="utf-8")
    script = tmp_path / "worker.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(600)\n", encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"
    return [sys.executable, str(script), str(grandchild), str(pidfile)], pidfile


def test_a_hung_worker_is_killed_at_its_deadline_not_long_after_it(
        tmp_path, hung_worker):
    cmd, _ = hung_worker
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        run_worker(cmd, dict(os.environ), DEADLINE_S,
                   tmp_path / "o", tmp_path / "e")
    elapsed = time.monotonic() - t0
    assert elapsed < DEADLINE_S + MAX_OVERSHOOT_S, (
        f"the worker was reaped {elapsed - DEADLINE_S:.1f}s past its "
        f"{DEADLINE_S}s deadline; the bug being regressed against overshot "
        f"by 1341s because the post-kill communicate() had no timeout")


def test_the_kill_reaches_the_grandchild_a_bare_kill_would_orphan(
        tmp_path, hung_worker):
    cmd, pidfile = hung_worker
    with pytest.raises(subprocess.TimeoutExpired):
        run_worker(cmd, dict(os.environ), DEADLINE_S,
                   tmp_path / "o", tmp_path / "e")
    assert pidfile.is_file(), "the grandchild never started; test is not testing"
    pid = int(pidfile.read_text())
    for _ in range(20):          # taskkill /T is not synchronous
        if not _alive(pid):
            break
        time.sleep(0.5)
    else:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)] if os.name == "nt"
                       else ["kill", "-9", str(pid)], capture_output=True)
        pytest.fail(f"grandchild {pid} outlived the kill — a worker that "
                    f"outlives its parent also outlives its deadline")


def test_a_worker_that_finishes_in_time_still_returns_its_output(tmp_path):
    script = tmp_path / "quick.py"
    script.write_text("import sys; sys.stdout.write('done'); "
                      "sys.stderr.write('noted'); sys.exit(3)\n",
                      encoding="utf-8")
    oo, oe = tmp_path / "o", tmp_path / "e"
    rc = run_worker([sys.executable, str(script)], dict(os.environ), 60, oo, oe)
    assert rc == 3
    assert oo.read_text() == "done"
    assert oe.read_text() == "noted"


@pytest.mark.skipif(not chrono_available()[0],
                    reason=f"chrono env unavailable: {chrono_available()[1]}")
def test_the_activation_path_carries_the_conda_library_directory():
    """Without `<env>/Library/bin` the worker's import blocks on a modal box.

    Asserted on the built environment rather than on a string literal, because
    what matters is that the directory Irrlicht.dll actually lives in is the
    one that ends up on PATH.
    """
    envdir = find_env_dir()
    path = activation_env(envdir)["PATH"].split(os.pathsep)
    for d in (envdir, envdir / "Library" / "bin", envdir / "Scripts"):
        assert str(d) in path, f"{d} missing from the worker PATH"
    assert path.index(str(envdir / "Library" / "bin")) < len(path) - 1


def test_the_venvs_own_python_paths_do_not_leak_into_the_chrono_env(
        monkeypatch, tmp_path):
    """The chrono env must never see this venv — that is the whole reason it
    is a separate env (CadQuery and PyChrono cannot share one)."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path))
    e = activation_env(tmp_path / "env")
    assert "PYTHONPATH" not in e
    assert "PYTHONHOME" not in e


@pytest.mark.skipif(not chrono_available()[0],
                    reason=f"chrono env unavailable: {chrono_available()[1]}")
def test_the_env_interpreter_imports_pychrono_without_conda_run():
    """The direct spawn must be a working substitute for `conda run`.

    This is the test that would have caught the PATH half of the bug: run the
    env's python.exe with a bare inherited environment and `import pychrono`
    never returns.
    """
    envdir = find_env_dir()
    oo, oe = Path(os.environ["TEMP"]) / "chrono_probe.out", \
        Path(os.environ["TEMP"]) / "chrono_probe.err"
    rc = run_worker([str(env_python(envdir)), "-c", "import pychrono"],
                    activation_env(envdir), 180, oo, oe)
    assert rc == 0, f"import pychrono failed: {oe.read_text()[-500:]!r}"
