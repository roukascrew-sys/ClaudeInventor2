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

## 3. Vendored assets (in-repo, no download step)

Committed under `design_engine/data/` because the outputs must be
self-contained and reproducible offline:

- `three.min.js` — three.js **r147** UMD build, MIT licence, 594 KB, from
  `https://unpkg.com/three@0.147.0/build/three.min.js`. Inlined into every
  generated viewer. r147 is used deliberately: it is the last line with a UMD
  build, so the viewer needs no import map or module loader and works from a
  plain `file://` open.
- `viewer_template.html` — the viewer shell; `%%THREE_JS%%` and `%%PAYLOAD%%`
  are substituted at generation time.
- `price_book.json` — cached public supplier pricing (see Phase 6 notes in the
  build plan). Prices are **not live**; re-capture before purchasing.

`gmsh` (pip) provides STEP → tetrahedral meshing for the solver and is pinned
in `requirements.txt`.

## 4. Kinematics environment (Project Chrono) — separate by necessity

PyChrono is distributed **conda-only** and pins dependencies that conflict with
this pip venv, so it must NOT be installed alongside CadQuery. It lives in its
own Miniforge environment and is driven over a subprocess/JSON boundary
(`design_engine/chrono_worker.py`), which is the only supported arrangement.

```
winget install --id CondaForge.Miniforge3 -e
%USERPROFILE%\miniforge3\Scripts\conda.exe create -n chrono python=3.12 -y
%USERPROFILE%\miniforge3\Scripts\conda.exe install -n chrono projectchrono::pychrono -c conda-forge -y
```

Verified working 2026-08-24 with PyChrono on Python 3.12.14 and numpy 2.5.2 —
the old-numpy pin that originally motivated deferring Chrono no longer applies
to this build.

Note: run it through `conda run -n chrono`, not by calling the env's
`python.exe` directly. A direct call leaves `Libraryin` off PATH and the
PyChrono DLL load hangs rather than erroring. `kinematics.py` already does
this correctly.

If the env is absent, `run_kinematics` refuses with setup instructions — it
never silently falls back to an approximation. `design_engine.kinematics.
chrono_available()` reports status, and the kinematics tests skip cleanly.

## 5. Orchestration skill

The `design-engine-loop` skill (Phase 7) packages the operating process for
Claude Code. Canonical copy: `.claude/skills/design-engine-loop/SKILL.md` in
this repo. Install it for the Downloads project context with:

```
copy .claude\skills\design-engine-loop\SKILL.md ^
  %USERPROFILE%\Downloads\.claude\skills\design-engine-loop\SKILL.md
```

Keep the two copies identical (the skill's own editing notes say the same).

## 6. Verify

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
