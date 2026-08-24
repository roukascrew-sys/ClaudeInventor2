"""Phase 0 environment smoke test — design-engine.

Verifies the two load-bearing tools against known-correct values, not just imports:

1. CadQuery geometry kernel: 10x10x10 mm box must report volume 1000 mm^3,
   and must export a parseable ISO-10303 STEP file.
2. CalculiX solver: single C3D8 unit cube (steel, E=210000 MPa, nu=0.3) under
   100 MPa nominal uniaxial tension. Analytic tip displacement u_z = sigma*L/E
   = 100/210000 = 4.76190e-4 mm. Solver output must match to 0.01%.

Run with the project venv:
    .venv\\Scripts\\python.exe smoke_test.py
Exit code 0 = environment healthy. Non-zero = broken; do not build on it.
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
CCX = PROJECT_ROOT / "tools" / "CalculiX-2.23.0-win-x64" / "bin" / "ccx.exe"
SMOKE_DIR = PROJECT_ROOT / "phase0_smoke"

FAILURES = []


def check(name: str, ok: bool, detail: str) -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    if not ok:
        FAILURES.append(name)


def test_cadquery() -> None:
    import cadquery as cq

    box = cq.Workplane("XY").box(10, 10, 10)
    vol = box.val().Volume()
    check("cadquery volume", abs(vol - 1000.0) < 1e-6,
          f"10mm cube volume = {vol:.9f} mm^3 (expect 1000)")

    with tempfile.TemporaryDirectory() as td:
        step_path = Path(td) / "box.step"
        cq.exporters.export(box, str(step_path))
        head = step_path.read_text(errors="replace")[:100]
        check("cadquery STEP export", step_path.exists() and "ISO-10303" in head,
              f"exported {step_path.name}, header starts: {head.splitlines()[0]!r}")


def test_calculix() -> None:
    check("ccx binary present", CCX.exists(), str(CCX))
    if not CCX.exists():
        return

    inp = SMOKE_DIR / "cube.inp"
    check("cube.inp present", inp.exists(), str(inp))
    if not inp.exists():
        return

    result = subprocess.run(
        [str(CCX), "-i", "cube"], cwd=SMOKE_DIR,
        capture_output=True, text=True, timeout=120,
    )
    check("ccx run", result.returncode == 0 and "Job finished" in result.stdout,
          f"exit={result.returncode}")

    dat = (SMOKE_DIR / "cube.dat").read_text()
    # .dat rows: node_id  vx  vy  vz — take vz for the four top-face nodes
    rows = re.findall(
        r"^\s*([5678])\s+(\S+)\s+(\S+)\s+(\S+)\s*$", dat, re.MULTILINE)
    expected = 100.0 / 210000.0  # sigma*L/E, mm
    uz = [float(r[3]) for r in rows]
    ok = len(uz) == 4 and all(abs(u - expected) / expected < 1e-4 for u in uz)
    check("ccx displacement vs analytic", ok,
          f"u_z = {uz} mm, analytic = {expected:.6e} mm")


if __name__ == "__main__":
    test_cadquery()
    test_calculix()
    if FAILURES:
        print(f"\nSMOKE TEST FAILED: {', '.join(FAILURES)}")
        sys.exit(1)
    print("\nSMOKE TEST PASSED — environment healthy.")
