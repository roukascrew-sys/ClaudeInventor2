"""Process lifetime helpers shared by the tools that shell out.

Stdlib only, deliberately: the CalculiX solver (`fea.py`) and the Chrono
worker (`kinematics.py`) run in different environments and share no other
dependency, but they need the identical guarantee — that a deadline binds on
the whole tree a subprocess started, not just on the handle Python happens to
hold.

Why it is a module and not a method on either caller: on 2026-08-28 the Chrono
bridge overshot a 900 s deadline by ~1341 s and took the test suite from ~2 min
to 38m49s, because `conda run` interposed a process and the kill never reached
the worker. `kill_tree` was written there (8f624c6) and lives here now,
unchanged in behaviour except for the POSIX guard below, because the mechanism
is not Chrono-specific — it is what `Popen.kill()` means on Windows.

The two halves are independent, and a caller wants both:

* Kill the TREE, so the kill actually reaches whatever is running.
* Bound what happens AFTER the kill. `communicate()` with no timeout blocks
  until every holder of the inherited pipe write handles exits, so if the tree
  kill ever fails — a protected process, a handle we cannot open — an
  unbounded reap turns that failure into an unbounded wait. Redirecting to
  files instead of pipes (`kinematics.run_worker`) removes the exposure
  entirely; a bounded reap (`fea._run_solver`, which needs the pipes) caps it.
"""

from __future__ import annotations

import os
import signal
import subprocess

# How long to wait for a killed tree to actually go away before giving up on
# it. Generous, because `taskkill /T` is not synchronous and a loaded machine
# is slow; still bounded, which is the whole point.
REAP_GRACE_S = 10.0


def kill_tree(proc: subprocess.Popen, grace: float = REAP_GRACE_S) -> None:
    """Kill `proc` *and everything it spawned*, then reap within `grace`.

    `Popen.kill()` on Windows is `TerminateProcess` on a single handle: a
    grandchild is untouched and keeps running — and keeps holding the stdout /
    stderr pipe write handles it inherited, which is what turns a missed kill
    into an unbounded wait in the parent.

    POSIX note: `killpg` is used only when the child is in a process group of
    its own (`start_new_session=True` at spawn). A child that shares the
    caller's group is killed singly instead, because signalling our own group
    would kill the caller — a test runner included.
    """
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=grace)
        else:
            pgid = os.getpgid(proc.pid)
            if pgid == os.getpgid(0):
                proc.kill()
            else:
                os.killpg(pgid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    for _ in range(2):
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass


def new_session_kwargs() -> dict:
    """Popen kwargs that make `kill_tree` able to do its job on POSIX.

    On Windows the tree is found from the parent PID by `taskkill /T`, so
    nothing is needed and nothing is added — in particular no console flags,
    which would change how an existing solver spawn behaves.
    """
    return {} if os.name == "nt" else {"start_new_session": True}
