# Validation report

Test campaign run 2026-09-28 on the MVP. Every defect found is listed with its root cause,
fix and the test that now guards it.

## Test layers

| Layer | Tool | What it covers | Result |
|---|---|---|---|
| Unit / physics | `pytest tests/test_physics.py` | NIST cp, reaction heats, LHV release, flame T, Bogue, elemental balances | 10/10 |
| Plant | `pytest tests/test_plant.py` | mass & energy closure, draught design point, nominal realism, response directions, interlocks, snapshot determinism | 10/10 |
| Regression | `pytest tests/test_regressions.py` | every defect below | 20/20 |
| Robustness sweep | `tests/validation/robustness.py` | all 19 disturbances at min/max for 60 min: NaN/inf, bounds, draught convergence, conservation | 32/32 cases clean |
| Operating sequences | `tests/validation/operations.py startup|shutdown` | hot standby → full production by operator commands; controlled shutdown to cold stop | see §3 |
| API hardening | `tests/validation/api_probe.py` | 20 malformed/malicious commands over HTTP and WebSocket | 0 accepted, socket survives |
| Browser E2E | `tests/validation/e2e_browser.py` | Chromium clicks through every screen, faceplate, group, trainer, fileset, scoring | 37/37 |
| FreeCAD buttons | `tests/validation/e2e_freecad.py` + `mock_freecad.py` | build/live/off against a mock addon: healthy, stalled GUI, FreeCAD restarted, FreeCAD not running | 16/16 |
| Mimic layout | `tests/validation/e2e_layout.py` | pipe connections, topology, pipes vs vessels and value boxes | 20/20 |
| Live 3-D plant | `tests/validation/e2e_3d.py` | /3d in WebGL: model load (all FreeCAD parts), render loop, gzip, shell colours = scanner(T_shell) sRGB-exact, rotation = live rpm, flame length = model, no light leak, picking through see-through floors, labels, HMI button, production mode without FreeCAD UI | 14/14 |
| HiDPI charts | `tests/validation/e2e_hidpi.py` | chart canvases at display scaling 100/125/150/200 %: size stable over ~20 redraws, content drawn | 20/20 |
| Soak | ad hoc | 3 h at ±20 % fuel & feed fluctuation | +4.3 MB memory, bounded buffers |

Run everything:

```bash
.venv/bin/python -m pytest -q
.venv/bin/python tests/validation/robustness.py            # ~10 min
.venv/bin/python tests/validation/operations.py startup    # ~15 min
.venv/bin/python -m cemsim.server --port 8000 &            # then:
.venv/bin/python tests/validation/api_probe.py localhost:8000
.venv/bin/python tests/validation/e2e_browser.py localhost:8000   # needs playwright + chromium
```

## 1. Defects found and fixed

| # | Found by | Defect | Root cause | Fix | Guard |
|---|---|---|---|---|---|
| V1 | API probe | PID SP `NaN` accepted and able to propagate into the physics; coal LHV −5 accepted; unknown tags gave HTTP 500 (WebSocket would disconnect the operator) | no input validation | `CommandError` + `_num()` finite/range checks for every command; server turns any failure into `{"ok": false, "msg": ...}`; WebSocket never drops | `test_bad_commands_rejected` (11 cases), `api_probe.py` |
| V2 | API probe | fileset name `../../x` → arbitrary pickle load | path not sanitised | name regex `[A-Za-z0-9_-]{1,60}`, existence check | `test_bad_commands_rejected` |
| V3 | shutdown sequence | crash (`IndexError`) with cooler + ID fan stopped | division by zero gas flow in preheater | zero-flow guards | `test_all_fans_stopped_…` |
| V4 | shutdown + robustness | draught solver residual up to 1100 kg/s near zero flow / after ID fan trip | Newton diverges where the √Δp law has unbounded slope | Newton fast path + guaranteed nested Brent bracketing fallback (both residuals monotone) | `test_all_fans_stopped_…`, `test_id_fan_trip_draught_converges` |
| V5 | shutdown sequence | "burning zone 2327 °C" in an emptying kiln | temperature of an empty cell is undefined; Newton drifted to the clamp. Also near-empty cells are numerically stiff | inversion only for cells with heat capacity; empty cells report the brick; second-law exchange limiter (bed can move at most half-way to its hottest/coldest source per step, same scaled flows in gas/wall/bed → energy still conserved); pyrometer KPI sees brick where there is no bed | `test_empty_kiln_no_overheating` |
| V6 | robustness | after cooler-fan-1 or kiln-drive trip, fuel kept burning: preheater exit 986 °C, calciner 1189 °C | missing protection trips | trips: preheater exit > 550 °C (ID fan / GCT protection) and calciner > 1050 °C (refractory); logged maintenance bypass for scenario generation | `test_preheater_exit_high_high_trips_fuel`, `test_trip_bypass_is_honoured` |
| V7 | start-up sequence | calciner burner could be started into a 660 °C calciner; coal did not ignite and the loop kept adding unburnt coal | permissive looked at kiln inlet, not at the calciner; burnout ignored O2 | permissive: calciner ≥ 750 °C; char burnout ∝ √(xO2 / 0.021) (half order, referenced to the calibrated nominal so the validated steady state is unchanged) | `test_calciner_burner_permissive`, `test_calciner_coal_ignites_easier_in_air` |
| V8 | start-up sequence | hot-standby fileset tripped the burner on load | scenario generated before the V6 trip existed | regenerated with the interlocks active; generator asserts no trip over 10 min with all interlocks live | `test_hot_standby_does_not_trip_on_load` |
| V9 | scenario mapping | coal set-points below 1.5 t/h ignored even after the code minimum was lowered | filesets restored drive *configuration* (min speed, ranges, rates) as well as state, silently overriding code fixes | restore only runtime state (drives: state/pv/sp/timer/fault; PID: mode/sp/out/pv/integral) | `test_restore_does_not_override_configuration` |
| V10 | browser E2E | drive SP out of range silently clamped (72.5 rpm → 4.5) | clamp instead of reject | out-of-range entries rejected with message, as a DCS does; ± buttons clamp client-side | `e2e_browser.py` |
| V11 | browser E2E | controller "C" buttons never showed AUTO | button built without its controller id | id wired | `e2e_browser.py` |
| V12 | browser E2E | rejected trainer commands gave no feedback without an open faceplate | ack only routed to faceplate | error toast | `e2e_browser.py` |
| V13 | soak | alarm journal unbounded; CO alarm chattering (54×/h at ±20 % coal) | list without bound; no off-delay | bounded deque; ISA-18.2 off-delay 15 s; CO deadband 500 ppm → 28×/h, all genuine | — |
| V14 | FreeCAD test | FreeCAD "build" froze the whole simulation; live pushes kept hammering FreeCAD during the build; no RPC timeout | slow external call inside the simulator lock | build outside the lock with pushes paused; 20 s / 180 s RPC timeouts; 15 s back-off after a failed push | manual (sim time advanced 445 s during a 90 s FreeCAD stall) |
| V16 | user report (Windows, display scaling > 100 %) | kiln-profile and trend charts grew taller on every redraw, then vanished (broken-canvas icon) | `setupCanvas` re-read the `height` attribute that it had just overwritten with height × devicePixelRatio, compounding 1.5× per redraw (330 px → 1 095 781 px in 20 redraws); headless tests ran at DPR 1.0 where it cannot occur | logical height read once from the markup; canvas reallocated only when its size changes | `tests/validation/e2e_hidpi.py` (DPR 1.0/1.25/1.5/2.0, 20 checks) |
| V17 | user report: "FreeCAD build / live on do nothing" | (a) external: FreeCAD's Qt layer had a *stuck left mouse button* (`QApplication.mouseButtons() == LeftButton`, release event lost), so the addon's dispatcher skipped every tick ("user is dragging") and 20 requests queued - even `print(1)` timed out while `ping`/status said healthy; (b) CemSim gave no feedback: no status shown, errors only after 90-180 s, raw addon message; (c) a build blocked that browser's command channel for up to 90 s; (d) "live on" before "build", or after a FreeCAD restart, failed forever (bridge assumed its module was still installed); (e) race: a build failing during a stall reset live mode that the operator had just switched on | (a) cleared by posting one `WM_LBUTTONUP` to the FreeCAD window; operator hint: click once inside FreeCAD; (b) bridge status (off/checking/building/live/error) with an actionable message streamed to a footer indicator + toast, fast `ping`/`get_rpc_status` check that does not need FreeCAD's GUI thread; (c) build runs as a background task, reply is immediate; (d) push detects missing module/document and rebuilds automatically; (e) operator intent (`want_live`) separated from `building`; failures back off 15 s and retry | `tests/validation/e2e_freecad.py` with `mock_freecad.py` (healthy / stalled GUI / restarted / not running) - 16/16 |
| V18 | user report: "misaligned connections" on the kiln-line mimic | preheater pipes were hand-placed coordinates: S1->ID fan duct started 60 px away from S1's outlet; S2->S1, S3->S2, S4->S3 ducts started 20 px off the lower cyclone's outlet; every stage duct ended in the upper cyclone's *outlet* (roof) instead of its tangential side inlet and ran through that cyclone's body; calciner riser ran through S3 and entered S5 through its roof; S4 meal line ran through S5 and stopped short of the calciner | port-based connection model: each cyclone registers `in` (side, tangential), `out` (vortex finder, roof) and `meal` (cone tip); every pipe is routed port-to-port (or to a junction on a riser); tower re-laid out with a central riser channel; layout geometry exported as `window.MIMIC_GEOM` | `tests/validation/e2e_layout.py` - 20 checks (endpoints, orthogonality, no pipe through vessels, gas chain topology, meal-to-next-riser, no pipe through value boxes); re-run on the old coordinates it flags all 7 defective pipes |
| V19 | user request: 3-D view must work without FreeCAD (production) | the only 3-D view was the FreeCAD mirror | browser 3-D page (three.js, vendored) fed by the live stream; geometry designed in FreeCAD at design time (script in repo) and exported once. Found and fixed while building it: meal dip pipes rising into the cone (junction above the tip), S5 inlet on the wrong side, gooseneck clashing with the calciner, firing floor/coal bin inside the cooler, burner pipe through the cooler roof, conveyor through the filter (automatic FreeCAD clash check: 0 clashes); colours treated as linear instead of sRGB (thermal colours wrong); rotation and camera tween tied to the clamped frame time (too slow at low fps); flame light leaking through the closed kiln; picks captured by see-through floors | `tests/validation/e2e_3d.py` 14/14; FreeCAD `clashes()` = [] |
| V20 | CI minimum-versions job | shipped filesets could not be loaded with numpy 1.26 (`No module named numpy._core`) | filesets were pickles of numpy-2 arrays: version-coupled, and unpickling shared files can execute code | portable JSON filesets (`cemsim/fileset.py`, arrays tagged with dtype/shape, atomic writes); shipped states migrated with a bit-exact round trip | full suite on numpy 1.26 / scipy 1.11 / fastapi 0.110 (CI `min-deps` job) |
| V21 | CI hardening | intermittent e2e checks: a fixed 700 ms wait for the label toggle and a "> 5 frames in 1.5 s" render check failed under load (software WebGL in CI) | assertions tied to wall-clock timing instead of conditions | condition-based waits; render check asserts the loop is alive (>= 3 frames in 3 s) - 12 consecutive local runs green | `scripts/ci_e2e.sh` |
| V15 | e2e | gradients missing on hidden screens; invisible overlay blocked clicks | paint servers in a `display:none` svg; global `svg{width:100%}` | global zero-size defs svg | `e2e_browser.py` |

## 2. Robustness sweep (60 min each from nominal)

All 32 cases: no exception, no NaN, temperatures in [240, 3100] K, draught converged,
mass closure ≤ 2·10⁻⁹, energy closure ≤ 8·10⁻⁴ of fuel input. Selected physical responses:

| Disturbance | Response (after 60 min, no operator action) |
|---|---|
| main coal LHV 18 MJ/kg | BZ 1392 °C, free lime 7.0 % |
| calciner coal LHV 18 MJ/kg | calciner loop saturates, BZ 1334 °C, free lime 13 % |
| preheater false air ×5 | kiln starved of air: O2 1.5 %, CO 4000 ppm |
| kiln ring ×2 | kiln draught restricted, CO 2700 ppm |
| feed moisture 5 % | preheater exit 244 °C (evaporation) |
| flame length ×2.5 | BZ 1343 °C, free lime 12 % (lazy flame) |
| cyclone 4 blockage | calciner loses meal, kiln empties, BZ rises |

## 3. Operating sequences

**Shutdown** (feed → calciner → main burner → cooler + ID fan → kiln drive): completes
without error; kiln empties in about 60 min (1.5–2× solids residence), then cools about 3 K/min;
burner restart without ID fan is refused by the permissive.

**Start-up from hot standby**: the naive scripted procedures (feed ramp on a timer, fuel
without air) fail. Both are genuine operator errors the simulator punishes realistically:
cold tertiary air quenches the calciner below ignition; adding coal without air gives
CO and an incomplete burn. The competent-operator procedure result is in §4.

## 4. Start-up by a competent operator (hot standby → full production)

Scripted operator practice (`operations.py startup`):

1. Close the tertiary-air damper so hot kiln gas heats the calciner. Starting the calciner
   earlier is refused by the permissive.
2. Light the calciner at 780 °C.
3. Start feed at 50 %.
4. Every minute:
   - hold kiln-inlet O2 at 3 % with the TAD
   - trim main-burner coal on BZ temperature, never adding fuel while O2 < 2 %
   - raise feed 10 t/h after 10 min with BZ > 1420 °C and free lime < 3 %
   - acknowledge and restart any trip

| Time from hot standby | Feed | BZ | Free lime | O2 kiln | CO kiln | Calciner | q |
|---|---|---|---|---|---|---|---|
| 0 | 0 t/h | 1398 °C | – | 14.2 % | 0 | 603 °C | – |
| 8 min | 0 | 1360 | – | 15.7 | 0 | 780 (light-off) | – |
| 2.8 h | 110 | 1454 | 0.9 % | 3.1 | 19 ppm | 869 | – |
| 4.8 h | 200 | 1470 | 0.0 | 3.9 | 1 | 870 | – |
| 7.0 h | 200 | 1450 | 1.9 | 3.0 | 14 | 870 | 3080 kJ/kg |

No interlock trips. The end state, reached by an independent route, matches the calibrated
`nominal` fileset: 124.7 t/h, BZ 1450 °C, free lime 1.8–1.9 %, O2 3.0 %, 3080 vs 3082 kJ/kg.
This supports a unique, consistent steady state.

## 5. Open items

- **FreeCAD GUI blocked.** During the final FreeCAD test the GUI thread stopped
  dispatching (`execute_code` "waiting to start" for 90 s, even for `print(1)`), while the RPC
  status reported healthy. This is external to CemSim (typically a modal dialog in the
  FreeCAD window). Since V14 the simulator keeps running and retries every 15 s.
- **Speed:** ~19 ms/step on one core, so ≈ 50× real time.
- **Known model deviations:** see docs/MODEL.md §11.
