# design-engine — environment setup (Phase 0)

Reproduces the verified Phase 0 environment from scratch. Executed and verified 2026-08-23.

## 1. Python 3.12 venv

Python **3.12.x** is required — CadQuery/OCP wheel support is inconsistent above 3.12.
System default (3.14) is untouched; the project runs entirely from its own venv.

```
winget install --id Python.Python.3.12 -e        # installs 3.12.10 per-user
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` is a full `pip freeze` pin. Note: `vtk` is a hard runtime
requirement of `cadquery` 2.8.0 on Windows (`OCP.IVtkOCC` fails to import without
it) even though pip does not pull it in as a dependency — it is pinned in
requirements.txt for that reason. Do not remove it.

## 2. CalculiX 2.23 solver (ccx)

Native Win64 build from the official CalculiX GitHub org (binaries are committed
in-repo, not GitHub Releases):

- Source: https://raw.githubusercontent.com/calculix/CalculiX-Windows/master/releases/CalculiX-2.23.0-win-x64.zip (25.2 MB)
- Extract to: `tools\CalculiX-2.23.0-win-x64\`
- Solver binary: `tools\CalculiX-2.23.0-win-x64\bin\ccx.exe` (also `ccx_MT.exe`
  multithreaded, `cgx.exe` pre/post, and the ccx 2.23 manual PDF under `doc\`)

`tools/` is gitignored (25 MB of binaries); this file is the record of exactly
what goes there.

## 3. Verify

```
.venv\Scripts\python.exe smoke_test.py
```

Must print `SMOKE TEST PASSED`. The test checks real values, not imports:
CadQuery volume + STEP export, and a one-element ccx run whose displacement
must match the analytic solution (sigma*L/E) to 0.01%.

## Known constraint — Project Chrono (NOT installed)

PyChrono has **no official pip wheel**; it is distributed conda-only
(https://api.projectchrono.org/pychrono_installation.html) and pins an old
numpy, which conflicts with this venv (numpy 2.5.x). Installing it means either
a separate Miniforge/conda env or deferring until the kinematics layer is
actually needed. Decision pending — see build plan.
