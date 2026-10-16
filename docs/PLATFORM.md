# DuoSim as a platform (SaaS ecosystem)

The goal: a user learns on the cement simulator, can *change* it, and can build *other* simulators and their
3-D models. This repository provides the ecosystem pieces — contract, runtime, tenancy, design tooling,
deployment — not a finished product. What exists, what does not:

| Piece | Status | Where |
|---|---|---|
| Simulator plug-in contract + registry + conformance check | built, tested | `duosim/platform/registry.py` |
| Cement kiln as a plug-in; a second (water tank) simulator | built, tested | `duosim/platform/plugins/` |
| Scaffold generator (`new`), contract checker (`check`) | built, tested | `python -m duosim.platform` |
| Headless session API for any plug-in | built, tested | `duosim/api_platform.py` |
| Workspaces, hashed API keys, daily quotas, design history | built, tested | `duosim/platform/store.py` |
| Design-by-prompt (Gemini / Claude → verified FreeCAD model) | built, tested live (Gemini) | `duosim/design/`, `/design` |
| FreeCAD worker container, app container, compose file | built and run end to end (see *Deployment*) | `deploy/`, `docker-compose.yml` |
| Billing, sign-up/SSO, admin UI | **not built** | — |
| Worker pool / autoscaling, job queue | **not built** (one worker per `DUOSIM_FREECAD_URL`) | — |
| Sandboxing of tenant-written simulators | **not built** — plug-ins are operator-trusted code | — |
| Live 3-D binding for *arbitrary* simulators | **not built** (the cement view binds by part name) | — |

## Writing another simulator

```bash
python -m duosim.platform new boiler_drum --name "Boiler drum level" --domain power   # -> simulators/boiler_drum/
$EDITOR simulators/boiler_drum/__init__.py          # state, physics, tags, commands
python -m duosim.platform check boiler_drum --dir simulators
DUOSIM_SIMULATORS_DIR=simulators uv run duosim      # appears under /api/platform/simulators
```

A plug-in is a `Manifest` (id, tags with units/ranges, commands, `dt`, `design_prompt`) plus a model class.
`SimpleModel` makes snapshot/restore/determinism automatic if all state lives in `self.state` and all randomness
in `self.rng`. `conformance()` is the executable definition of a well-behaved simulator:

* manifest valid; `create()` returns independent instances
* `tags()` keys == the manifest's tags exactly, all finite floats inside their declared ranges
* `t` advances by exactly `steps × dt`
* `snapshot()` → `restore()` reproduces the future exactly
* an unknown/invalid command raises `CommandRejected` and leaves the simulator usable

The water-tank plug-in doubles as a worked example of *model validation*: RK4 volume integration with the
cumulative flows booked on the same weights, so its mass balance closes to rounding error in every regime
(including overflow and running dry) — and the test proves it, mirroring what `docs/MODEL.md` claims for the kiln.

## The main page and the public demo

`/` is the **Studio**: a *Design* tab (prompt → verified FreeCAD model) and a *Simulators* tab (gallery of
plug-ins with a generic live console). The cement kiln line is the flagship demo: its dedicated operator HMI is at
`/hmi` (with a "← Studio" button back), its 3-D plant at `/3d`. Any other plug-in appears in the gallery
automatically and gets the generic console, which is built from its manifest (tags, units, commands).

`DUOSIM_PUBLIC_DEMO=1` lets visitors use the Studio **without any key**, as a shared `public` workspace with small
caps so the model credits behind it are bounded:

| Cap | Default | Env |
|---|---|---|
| designs per visitor (client IP) per hour | 5 | `DUOSIM_PUBLIC_HOURLY_GENERATIONS` |
| designs, all visitors together, per UTC day | 20 | `DUOSIM_PUBLIC_DAILY_GENERATIONS` |
| builds per visitor per hour / all per day | 15 / 60 | `DUOSIM_PUBLIC_HOURLY_BUILDS` / `_DAILY_BUILDS` |
| simulator sessions per visitor per hour / all per day | 10 / 40 | `DUOSIM_PUBLIC_HOURLY_SESSIONS` / `_DAILY_SESSIONS` |
| idle time before an anonymous console is reaped | 90 s | `DUOSIM_PUBLIC_SESSION_IDLE_S` |

Visitors cannot list each other's designs or sessions. Behind a reverse proxy set `DUOSIM_TRUST_FORWARDED=1` so the
per-visitor cap uses `X-Forwarded-For` (leave it unset when the app is reached directly: the header is spoofable).
A *presented* key must still be valid (a wrong key is a 401, never a silent downgrade), and the page only ever sends
platform keys (`cs_…`): a model key such as Gemini's is configured on the server and is refused client-side.

## Tenancy and cost control

Generation spends model credits and a build occupies the shared FreeCAD worker, so **every** design/platform
route needs an API key and is quota-limited. Keys are 256-bit random, stored only as SHA-256.

```bash
python -m duosim.platform keys create --workspace acme --label "alice"   # printed once
curl -H "X-API-Key: cs_..." -d '{"prompt":"a tank farm with three tanks"}' -H 'content-type: application/json' \
     localhost:8000/api/design/generate
```

| Env | Default | Meaning |
|---|---|---|
| `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` | — | which providers exist (from the environment or a git-ignored `.env`) |
| `DUOSIM_DEFAULT_PROVIDER` | first configured | `gemini` or `claude` |
| `DUOSIM_GEMINI_MODEL` / `DUOSIM_CLAUDE_MODEL` | `gemini-flash-latest` / `claude-opus-5-5` | model overrides |
| `DUOSIM_DAILY_GENERATIONS` / `_BUILDS` / `_SESSIONS` | 50 / 200 / 100 | per workspace per UTC day |
| `DUOSIM_MAX_SESSIONS` | 5 | concurrent simulator sessions per workspace |
| `DUOSIM_FREECAD_URL`, `FREECAD_MCP_TOKEN(_FILE)` | — | the FreeCAD worker (or use `--freecad`) |
| `DUOSIM_DATA_DIR` | `./.duosim` | SQLite + design artifacts |
| `DUOSIM_PUBLIC_DEMO` | unset | `1`: keyless Studio for anonymous visitors with the caps above |
| `DUOSIM_DEV_OPEN` | unset | `1` disables auth *and* caps (local development only) |

> The training simulator's own control API (`/api/cmd`, `/ws`) predates this layer and is **not**
> authenticated — it is meant for a training room. Do not expose it on the public internet without a
> reverse-proxy login (or use a separate private network for it).

## Design-by-prompt: why the model never writes code

`execute_code` in the FreeCAD addon runs arbitrary Python with the worker's privileges. If a language model
(steered by a user's prompt, or by text it was shown) produced that Python, a prompt could become remote code
execution. So:

1. the model emits a **JSON spec** in a six-primitive DSL (`duosim/design/spec.py`);
2. a strict validator rejects anything else with precise messages (the model gets one repair round);
3. only validated numbers and identifiers (`[A-Za-z][A-Za-z0-9_]{0,31}`) are formatted into the FreeCAD script;
4. the result is **measured** against closed-form volumes and centres of mass — `verified: true` means FreeCAD's
   geometry equals the spec, not merely "no error".

Outputs per design: STEP, FreeCAD `.FCStd`, a three.js mesh (shown in the Studio), a FreeCAD render, the spec.

Limits that protect the shared worker (a started FreeCAD GUI task cannot be cancelled): 300 parts, 24 points per
pipe, 120 pipe points in total, 2000-character prompts, 1 MB request bodies (also enforced for chunked uploads),
daily quotas per workspace. The largest valid design builds in ~30 s on the reference machine.

## Deployment

```bash
mkdir -p .duosim && python3 -c "import secrets; print(secrets.token_urlsafe(32))" > .duosim/freecad_token
cp .env.example .env            # GEMINI_API_KEY / ANTHROPIC_API_KEY
docker compose up --build
docker compose exec app python -m duosim.platform keys create --workspace acme
```

The FreeCAD worker sits on an internal network (no published port, no internet route) with a shared secret;
the app is the only thing that talks to it. Both containers start as root only to read the mounted secret and
then drop to an unprivileged uid (`setpriv`); the addon's IP allow-list defaults to private ranges plus loopback.

Verified by running the stack (not just building it): 401 without a key; app→worker healthy; a build through
`POST /api/design/build` returns `ok: true` with a real STEP file; the worker is unreachable from the host and has
no internet route; a token-less client is refused; PID 1 of both containers is non-root. Not verified: more than
one worker, Kubernetes, or any cloud provider.
