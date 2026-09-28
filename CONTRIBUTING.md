# Contributing to CemSim

Thanks for helping build an open cement-plant operator training simulator.

## Setup

```bash
uv sync --group dev --group e2e        # Python 3.11 - 3.14
uv run pre-commit install              # ruff + hygiene hooks on every commit
uv run python -m cemsim.server         # http://localhost:8000  (3-D plant at /3d)
```

## Before you open a pull request

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest                                      # physics, plant, regressions (~3 min)
uv run playwright install chromium && bash scripts/ci_e2e.sh   # browser end-to-end suite
```

CI runs the same steps:

- lint
- tests on Python 3.11–3.14, Linux, Windows and macOS
- minimum dependency versions
- browser end-to-end
- dependency audit
- CodeQL

Nightly runs execute the long robustness sweep and the start-up/shutdown sequences.

## Rules for model changes

The simulator is only useful if its physics can be trusted:

- **Energy.** Keep the absolute-enthalpy formulation: species carry formation plus sensible
  enthalpy, and never insert a reaction heat by hand. Mass and energy closure are tested
  (`tests/test_plant.py::test_mass_and_energy_conservation`).
- **Equations.** Document every new equation, with its source, in `docs/MODEL.md`.
- **Parameters.** If you change a parameter, re-settle the shipped filesets
  (`scripts/calibrate.py`, `scripts/make_scenarios.py`). Then check the steady state in
  `docs/MODEL.md` §11 against typical plant values.
- **Bug fixes.** Every bug fix gets a regression test and an entry in `docs/VALIDATION.md`.

## 3-D plant geometry

The 3-D model is designed in FreeCAD by `tools/freecad/build_plant3d.py`. After changing it,
run `clashes()` (it must return `[]`), then `export_json(...)`, and copy the result to
`cemsim/web/assets/plant3d.json`. Live bindings depend on object names (`KilnSegNN`,
`CycS1..5`, `CoolFanN`, ...).
