# The design agent and multimodal input

`duosim/design/agent.py` turns a brief into a design through a fixed, budgeted workflow. The brief can be text,
reference images, an imported CAD model, or any combination. The language model is used where it is strong:
understanding intent, planning, and proposing geometry. Deterministic code is used where it is reliable: validation,
geometric checks, CAD-kernel measurement, and selection.

```
intake ─► plan ─► retrieve ─► generate ×N ─► validate/repair ─► critique ─► select ─► repair? ─► build+verify ─► visual judge ─► repair? ─► rebuild
```

| Step | What it does | Where |
|---|---|---|
| intake | sanitises the brief; images need a vision-capable model; a CAD import becomes the starting draft | `agent.py` |
| plan | the model writes an engineering plan first: components, counts, dimensions, primitives, placement, overall size, **assumptions** and **open questions** (shown to the user) | `PLAN_SCHEMA` |
| retrieve | BM25 retrieval of worked equipment patterns (cyclone, kiln, fan, tank, silo, filter, conveyor, stack, pump skid, structure, heat exchanger) shown as reference patterns | `templates.py` |
| generate | spec generation with plan, patterns and images; deep mode runs two diverse candidates in parallel | `agent.py` |
| validate/repair | the strict spec validator; precise errors are fed back for one repair | `spec.py` |
| critique | deterministic engineering checks: floating parts, below-ground parts, open pipe ends, duplicates, disconnected groups, scale against the plan, plan coverage. Each finding names the parts involved and the fix | `critic.py` |
| select | best candidate by critic score (best-of-N with a verifier) | `agent.py` |
| repair | critique-driven repair (standard: 1 round, deep: 2). **A repair is kept only if the score does not drop.** | `agent.py` |
| build + verify | FreeCAD builds every part and measures volume, centre of mass and solid validity against closed-form values | `builder.py` |
| visual judge | (deep) a vision model compares the FreeCAD render, and the user's reference images, with the brief. Low scores trigger one repair and a rebuild. A rebuild that does not verify is discarded | `agent.py` |

Every model call counts against a mode budget (fast 2, standard 5, deep 10) and a wall-clock deadline. Every
decision (accept, repair, stop, keep previous) is written to the trace, which the Studio shows under
*Agent reasoning*.

## Why this design: the methods it follows

The workflow combines several established techniques and recent CAD-generation results:

* **Plan before you act** (Plan-and-Solve prompting, Wang et al. 2023). An explicit equipment plan with dimensions removes most scale and missing-component errors, and it gives the critic a reference.
* **Retrieval-augmented generation**: retrieving worked examples is what makes a small primitive vocabulary produce recognisable equipment. SeekCAD combines retrieval with visual feedback for CAD code.
* **Verifier-in-the-loop refinement** (Self-Refine, Madaan et al. 2023; Reflexion, Shinn et al. 2023), with **deterministic** verifiers (schema validator, geometric critic, CAD kernel) rather than self-assessment.
* **Best-of-N with a verifier** (self-consistency, Wang et al. 2022): sample diverse candidates and select by an external score.
* **Hybrid feedback: exact kernel measurement plus a VLM judge**. CADSmith (2026) argues that visual feedback alone misses millimetre-scale errors while kernel measurements miss global shape problems. DuoSim does both: the FreeCAD verification is exact, and the deep agent's visual judge looks at the actual render (in the spirit of CADCodeVerify and CADDesigner's visual feedback).
* **Tool-using agents with engine feedback** (ReAct, Yao et al. 2022; TOOLCAD 2026). The CAD engine is the tool, and its measurements are the feedback.

Research directions not implemented yet: fine-tuning or RL on DuoSim's own verified designs (CADFusion trains with
visual preference; TOOLCAD uses curriculum RL), sketch-level parametric generation (CadVLM), and evaluation on public
benchmarks (for example BenchCAD, an image-to-CAD-code benchmark across 106 part families).

Sources: [CADSmith](https://arxiv.org/html/2603.26512v1), [TOOLCAD](https://www.alphaxiv.org/abs/2604.07960.md),
[CADDesigner](https://arxiv.org/html/2508.01031v4), [CADFusion: Text-to-CAD through visual feedback](https://huggingface-gw-kz.just-ai.com/papers/2501.19054),
[CadVLM](https://arxiv.org/abs/2409.17457v1), [BenchCAD](https://benchmarklist.com/benchmarks/benchcad/).

## Reference images

`POST /api/design/generate` accepts up to 4 images as data URLs (PNG, JPEG, WebP or GIF, 6 MB each). The bytes are sniffed, so the declared
type is ignored, and size and pixel count are bounded. Images go to the selected vision model (Gemini `inline_data`, Claude
`image` blocks, OpenAI `image_url`) for the plan, the generation and the repair steps. In the Studio you can drop, choose or paste them.

## CAD import

`POST /api/design/import?filename=…&units=auto|mm|m|…&up=auto|z|y&height=3&build=false&refine=…` takes the raw file as the body.

| Format | How it is read |
|---|---|
| STL (binary / ASCII), OBJ | weld vertices, split into connected bodies, closedness and signed volume per body |
| STEP | ISO-10303-21 text: one body per `MANIFOLD_SOLID_BREP`; envelope from B-rep vertices and points sampled on circles; analytic surfaces (cylinder / cone / sphere) as type hints; length units from the file |
| FreeCAD `.FCStd` | `Part::Box/Cylinder/Cone/Sphere/Tube` read **exactly**, with placement, rotation and colour. Other solids come from their stored BREP. Hidden objects and boolean operands are skipped. The document is never opened in FreeCAD, because documents can reference Python modules |
| DXF | circles become cylinders and closed polylines become boxes (rectangles keep their rotation), extruded by their thickness or `height`; layers become groups; `$INSUNITS` |
| IGES, BREP (and exact STEP) | measured by OpenCASCADE in the FreeCAD worker (volume, vertices, circle samples, surface types). The file reaches FreeCAD only as a base64 literal |

**Primitive fitting**: each body gets an oriented envelope (yaw from the principal axes of its footprint) and, when
closed, its volume. The fill ratio V / V_envelope separates box (≈1), cylinder (π/4), sphere (π/6) and tube, and the
radius profile along the axis separates cones from cylinders. The import report gives each part's method (exact,
fitted or envelope) and the mean volume error. Envelope boxes are drawn semi-transparent so an approximation is never
presented as exact. With `refine`, the imported spec is the starting draft for the agent (for example "name the parts and
add a stack").

## Tests

`tests/test_design_agent.py` covers each workflow path with a scripted provider (fast single shot, standard plan, retrieve
and repair, discarding a worse repair, deep candidates with judge and rebuild, budget limits, image rules), the critic, the
templates, all providers' image payloads and the OpenAI-compatible fallbacks. `tests/test_cad_import.py` builds every format
synthetically and checks the recovered primitives, units, rotation and refusal reasons.
