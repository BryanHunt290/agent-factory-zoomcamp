# Agent Factory

Public submission source: [BryanHunt290/agent-factory-zoomcamp](https://github.com/BryanHunt290/agent-factory-zoomcamp).
See [publication scope](docs/zoomcamp/PUBLICATION.md) for the source snapshot and validation history.

## Problem and expected behavior

AI coding runs can lose progress, repeat side effects, or need a human decision
without making that decision visible. Agent Factory gives a developer a durable
workflow: a planner specifies work, a developer implements it, a tester checks it,
and a reviewer either accepts it or requests a bounded repair. Risky actions pause
for an exact-action approval; uncertain outcomes require reconciliation.

The React dashboard lets a person observe runs and record approval or
reconciliation decisions. A FastAPI backend authorizes requests against an
OpenAPI contract; PostgreSQL stores product records; workers execute the workflow.
The dashboard currently observes and decides: operators create/queue demo runs.
The default model is a scripted fake, so the local demonstration costs nothing
and proves orchestration rather than real-model coding quality.

## Zoomcamp reviewer entry point

This checkout includes more than the historical v0.2 release described below.
Start with the [product specification](docs/zoomcamp/PRODUCT_SPEC.md),
[current submission checklist](docs/zoomcamp/SUBMISSION_CHECKLIST.md),
[rubric mapping](docs/zoomcamp/RUBRIC_MAPPING.md), and
[Day 1 validation record](docs/zoomcamp/DAY1_VALIDATION.md).
[Day 2 validation](docs/zoomcamp/DAY2_VALIDATION.md) records clean setup, Windows/Linux
regressions, browser workflows and launcher audit results.
Fresh Day 1 checks passed the offline baseline, frontend build, PostgreSQL integration
and real-browser workflows, including database-outage recovery. Exact counts and
limits are in the validation record above.
The [AI development workflow](docs/ai-workflow.md) describes human and AI roles;
[AGENTS.md](AGENTS.md) links the modular engineering instructions.

For the full web application, use the [reviewer guide](docs/REVIEWER_GUIDE.md).
Python 3.12, Node 24, Git and a running Docker Linux engine are prerequisites:

```powershell
py -3.12 scripts/reviewer.py setup          # once: pinned dependencies and PostgreSQL image
py -3.12 scripts/reviewer.py up             # start the local application
```

On Linux/macOS replace `py -3.12` with `python3.12`. Open the printed dashboard URL
and select the generated token file at sign-in. The launcher creates an isolated
review database and seeds a deterministic approval workflow. Ctrl+C stops its
API, worker and frontend; database and workspace history remain. It does not
use the existing development database. The API and dashboard stay on loopback
with development authentication.

`py -3.12 scripts/reviewer.py smoke` checks real startup and the approval pause.
`py -3.12 scripts/reviewer.py check --browser` runs the offline, PostgreSQL,
frontend and installed-Chrome checks. Omit `--browser` if Chrome is unavailable.
For the offline CLI demonstration, use the Quick start below.

[CI](.github/workflows/ci.yml) defines PR/push checks and retains results. Hosted
results are available on the [Actions page](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions).
[Deployment preparation](docs/deployment.md) describes production identity,
release, backup and rollback requirements. No public deployment or CD has been
activated. PostgreSQL and execution isolation use containers; the host-based
reviewer launcher is not full application containerization.


> Latest local release: **v0.2.0** (execution sandbox), tagged on branch
> `v0.2-sandbox`. Previous release: **v0.1.0**.

A reusable, graph-based orchestration system for autonomous software-development
projects. Agents (planner, developer, tester, reviewer) are nodes in an explicit
graph. They communicate through structured, checkpointed state, and every action
passes through a conservative permission layer with human approval gates.

```
START -> PLANNER -> DEVELOPER -> TESTER --pass--> REVIEWER --approved--> DONE
                       ^           |                  |
                       +---fail----+                  |
                       +-------changes_required-------+
```

Agent Factory runs entirely offline. The model provider is a deterministic
fake, but everything else is real: workspaces, file writes, test runs, git
snapshots, checkpoints, and the execution log.

## Security model (v0.2)

- **Untrusted project code**, meaning anything from a model provider other
  than the scripted fake, runs only in a **Docker container**. That gives
  container isolation, networking disabled, the project copied in with no
  host mounts, a read-only root, bounded CPU/memory/disk/PIDs/time/output,
  and verified cleanup.
- **If that sandbox is unavailable, unhealthy, or misconfigured, nothing
  runs.** The run moves to a resumable `blocked` state; there is no fallback
  to local execution.
- **Trusted, maintainer-scripted fixtures** run as ordinary local processes,
  which is not a sandbox.

v0.2 does **not** protect against Docker, kernel, or container escape, and
it does not stop generated code from faking its own test results. Only
Docker Desktop has been tested live. The full list is in
[docs/sandbox.md](docs/sandbox.md#what-v02-does-not-guarantee). The base
image's digest has been independently verified against Docker Hub.

## Quick start

Requires Python 3.12. The orchestration core uses only the standard library.
Tests need `pytest`. The optional PostgreSQL store, FastAPI backend and
contract tests use pinned packages in the project venv
(`requirements-postgres.txt`, `requirements-api.txt`, `requirements-dev.txt`).

```bash
# run the test suite
py -3.12 -m pytest            # or: python -m pytest
# with the project venv, which also runs the API contract tests (see AGENTS.md)
.venv\Scripts\python -m pytest

# run the deterministic demo (planner -> developer -> tester FAIL -> developer -> tester PASS -> reviewer -> done)
py -3.12 -m agent_factory demo --fresh
```

Other demo scenarios: `--scenario review | full | exhaust | approval`. See
[docs/testing.md](docs/testing.md).

**Offline supervised coding demonstration.** The explicitly selected
[supervised demonstration](docs/offline-supervised-demo.md) adds a fake coding
worker, separate change application, bound test/diff evidence and at most two
total repairs. Fresh validation confirms the standalone repair run, all 91
focused tests and an evidence-check mutation. See the
[validation record](docs/offline-supervised-validation.md) for regression results
and limits. The standard workflow remains unchanged. No real Codex adapter or
unattended real execution is enabled.

Optional (unreleased branch): add `--database` to `demo`, `approve`, and
`resume` to keep projects, runs, milestones, and approval decisions in
PostgreSQL across restarts. This needs a local database and the
`psycopg` driver; setup is in [docs/database.md](docs/database.md).
Everything above works without it.

**FastAPI backend (unreleased; loopback only).** The backend implements
[`openapi.yaml`](openapi.yaml) with real PostgreSQL storage, durable idempotent
project creation, cursor pagination, recorded approval decisions,
reconciliation answers for actions with an unknown outcome, an execution
timeline per approval, and an attention inbox. Every
product request needs a signed access token from the configured OpenID
Connect identity provider, and a permission for the operation
([docs/authentication.md](docs/authentication.md)). No production identity
provider is configured yet; a development issuer lets you try the API
locally. The backend does not start runs or execute approved actions: it
records what a person decided and wakes the run, and a worker applies it
(below).

After configuring the local database, try it from PowerShell with the
**development-only** token issuer:

```powershell
$env:AGENT_FACTORY_DATABASE_URL_FILE = '.local\database.url'
$env:AGENT_FACTORY_AUTH_MODE = 'development'
$env:AGENT_FACTORY_AUTH_DEV_ISSUER_DIR = '.local\dev-issuer'
.venv\Scripts\python -m agent_factory db migrate
.venv\Scripts\python -m agent_factory auth dev-issuer init
.venv\Scripts\python -m agent_factory auth principal add --issuer urn:agent-factory:development --subject me --grant-all
.venv\Scripts\python -m agent_factory auth dev-issuer token --subject me --out .local\dev-token.txt
.venv\Scripts\python -m agent_factory api serve
```

Then send the token from `.local\dev-token.txt` as `Authorization: Bearer <token>`.
Without an authentication configuration the server refuses to start.

The server defaults to `127.0.0.1:8000` and refuses non-loopback binding.
Do not deploy or tunnel it: no production identity provider is configured
and no deployment exists. See
[docs/backend.md](docs/backend.md) for setup, lifecycle, pooling, logging
and health checks, and [docs/api-contract.md](docs/api-contract.md) for the
13 product operations.

**Workers (unreleased; local, one host).** `python -m agent_factory worker run`
starts one worker process. It claims queued runs from PostgreSQL and advances
them. Several workers can share the database:

- each run has one owner at a time, under a lease that heartbeats renew;
- every write a worker makes to a run carries a fencing token, checked by
  the database in the same transaction, so a stalled ("zombie") worker cannot
  change a run once another worker has taken it over;
- `worker enqueue` queues a run;
- a person's decision (over HTTP, or `approve --database`) wakes the run,
  and a worker applies it after re-checking the exact action, the
  approval's expiry (12 hours), and the person; an action whose outcome is
  unknown waits for a person's answer (`reconcileAction`).

See [docs/workers.md](docs/workers.md) for the guarantees and their limits.

**Dashboard v1 (unreleased; local development only).** A mobile-first web
dashboard in [`frontend/`](frontend/) (React, TypeScript, Vite) to **observe,
approve, and reconcile**: projects and recent milestones, an attention inbox
that tells *approval required* from *reconciliation required*, each
approval with what, why, risk, and expiry, deliberate approve/reject, the
three reconciliation answers (a retry needs an explicit acknowledgement),
and what happened to the action. It uses the API unchanged, through a
same-origin proxy to the loopback API, and signs in with the
**development** token issuer only (the token lives in the tab's
`sessionStorage`). The browser holds no workflow state: the API and
PostgreSQL are the source of truth.

```powershell
cd frontend; npm ci; npm run build; npm run preview   # http://127.0.0.1:4173, with the API running as above
```

Its failure and recovery behaviour (frontend, API, and worker crashes, a
PostgreSQL outage, a closed browser, refused, timed-out, delayed, dropped,
and duplicated requests, stale approvals) is tested in a real browser against
the real stack: [docs/frontend.md](docs/frontend.md),
[docs/dashboard-reliability.md](docs/dashboard-reliability.md).

**Attention notifications v1 (unreleased; phase 1: no real provider yet).**
When a run pauses for an approval, or an approved action's outcome must be
verified, Agent Factory records a notification intent **in the same
transaction** as the attention event (a transactional outbox). A separate
dispatcher process (`notifications dispatch`) sends it "at least once":
- deduplication by attention event;
- bounded retries with backoff;
- leases and fencing, with no transaction open during a provider call;
- unknown outcomes recorded honestly.

The message only says that something needs attention, and links to the
dashboard. **It never authorizes anything.** Only the development fake
provider exists so far, and no real message has been sent. The approval
page shows whether people were told. See
[docs/notifications.md](docs/notifications.md), which includes the
SMS/email/push provider comparison and what a real provider needs.

**Real model providers, v1 (unreleased; foundation only, no real call made).**
Agents reach a model only through a Model Gateway, which owns:
- minimal context, with secret files withheld and host paths redacted;
- a guard that refuses any request carrying a credential;
- budgets checked before every call, and a restart-safe usage ledger;
- bounded retries, strict schema validation, and an audit record per call.

Prompts are versioned code. Capability manifests come only from the
permission policy. A model's answer is data: every action still passes the
Tool Gateway. The deterministic fake provider stays the default; an
Anthropic adapter (standard-library HTTPS) is ready but makes no call
without explicit configuration and the owner's authorization. See
[docs/model-providers.md](docs/model-providers.md) for the provider
comparison, privacy, and what the first live call needs.

**For AI coding agents:** read [AGENTS.md](AGENTS.md) first.

## Documentation

| Doc | Contents |
| --- | --- |
| [docs/architecture.md](docs/architecture.md) | Components, responsibilities, execution flow |
| [docs/graph-and-state.md](docs/graph-and-state.md) | Graph definitions, routing, retry budgets, the state model, checkpoints, the log |
| [docs/safety.md](docs/safety.md) | Permission policy, approval gates, workspace confinement, guarantees |
| [docs/testing.md](docs/testing.md) | Running tests and demos, what each test covers |
| [docs/sandbox.md](docs/sandbox.md) | Execution sandbox: what v0.2 guarantees and does not, routing, limits, image |
| [docs/design/v0.2-sandbox.md](docs/design/v0.2-sandbox.md) | Approved v0.2 sandbox design |
| [docs/project-templates.md](docs/project-templates.md) | How a future project plugs into Agent Factory, and how to add real model providers |
| [docs/product-domain.md](docs/product-domain.md) | Product records: projects, runs, milestones, approvals; state machines, repository interfaces, engine integration (unreleased) |
| [docs/database.md](docs/database.md) | PostgreSQL storage of product records: schema, keys, constraints, indexes, transactions, migrations, reconciliation, configuration, security boundary (unreleased) |
| [docs/api-contract.md](docs/api-contract.md) | The v1 API contract ([openapi.yaml](openapi.yaml)): resources, status codes, errors, pagination, idempotency, approval security, authentication and permissions |
| [docs/backend.md](docs/backend.md) | Local FastAPI backend: request lifecycle, layers, validation, pooling, idempotency, cursors, errors, observability and verification (unreleased; loopback only) |
| [docs/authentication.md](docs/authentication.md) | Authentication and authorization: OpenID Connect tokens, principals, permissions, 401 vs 403, approval identity, development and test authentication (unreleased) |
| [docs/workers.md](docs/workers.md) | Workers: run claims, leases, heartbeats, fencing tokens, the claim query, protected mutations, checkpoints and the filesystem, shutdown, failures (unreleased; one host) |
| [docs/frontend.md](docs/frontend.md) | Dashboard v1: running it, architecture, screens, the API client, development sign-in, polling and stale data, approval and reconciliation UX, security, accessibility, tests, the stack decision (unreleased; local development only) |
| [docs/dashboard-reliability.md](docs/dashboard-reliability.md) | Dashboard failure and recovery: each scenario's failure, what the person saw, what kept running, the final state, and repeated-run results; notification delivery under outages |
| [docs/model-providers.md](docs/model-providers.md) | Model providers: the provider comparison and recommendation, the Model Gateway, context assembly, versioned prompts, structured output, capability manifests, budgets and usage accounting, retries, prompt injection, privacy, explicit activation, and the authorization the first live call needs (unreleased) |
| [docs/capabilities.md](docs/capabilities.md) | The Capability Registry: typed capability definitions, evidence with provenance, availability, tool packs (Core, Git, AWS), agent views, evidence-based assessment, and how the Tool Gateway stays authoritative (unreleased) |
| [docs/aws-capabilities.md](docs/aws-capabilities.md) | The AWS tool pack: cloud_read, typed capabilities, the real backend without implicit credential chains (tested offline only), credential slots that refuse long-term keys, result filtering, availability, live-tested evidence, and the exact steps for the first real AWS call (unreleased; no AWS call has been made) |
| [docs/aws-e2-setup.md](docs/aws-e2-setup.md) | Owner guide for Stage E2: the IAM Identity Center console steps, the `agent-factory-dev` profile, temporary-sign-in checks that print no secret, and when the credential slot is bound (preparation only; nothing done) |
| [docs/notifications.md](docs/notifications.md) | Attention notifications: the channel and provider comparison, the transactional outbox, deduplication, the dispatcher, retries, crash and uncertainty semantics, privacy, content and links, and what a real provider needs (unreleased; fake provider only) |
| [AGENTS.md](AGENTS.md) | Instructions for AI coding agents: boundaries, commands, security invariants, frozen refs, prohibited shortcuts |
| [docs/ai-workflow.md](docs/ai-workflow.md) | How Agent Factory is built: an AI coding agent directed by the project owner, with review and verification at every stage |

## Layout

```
agent_factory/
  api/          local FastAPI backend: routes, schemas, services, errors, request context
  graph/        graph definition + validation; the standard dev graph
  engine/       executor, checkpoint store, append-only execution log
  domain/       product records (projects, runs, milestones, approvals) + repositories
  persistence/  PostgreSQL repositories, schema migrations, database configuration, run claims (optional)
  worker/       workers: identity, lease keeper (heartbeats), claim ownership, the claim/advance/release loop
  agents/       planner, developer, tester, reviewer (+ output-schema validation)
  tools/        workspace file tools, test runner, git snapshot, tool gateway, Core and Git tool packs
  capabilities/ Capability Registry: definitions, evidence, availability, tool-pack interface, assessment
  aws/          AWS tool pack: typed cloud_read capabilities, credential slots, result filtering; fake backend + offline-tested real backend
  safety/       action categories, permission policy, approval requests
  sandbox/      execution sandbox: manager, local (trusted only), docker, staging
  providers/    model-provider interface + deterministic fake provider
  templates/    ProjectTemplate + the calculator demo template
  state.py      ProjectState (structured, JSON-serializable)
  workspace.py  git-friendly project workspaces with path confinement
  factory.py    template + provider + workspace -> Executor
  demo.py       scripted demo scenarios
  cli.py        demo / status / log / approve / resume / sandbox / db / api serve / auth / worker
sandbox_image/  sandbox Dockerfile, af-entry, approved image lock
scripts/        developer helpers (local PostgreSQL secrets and provisioning)
config/         example configuration (placeholders only)
frontend/       Dashboard v1 (React + TypeScript + Vite): src/, generated contract types, Vitest tests
tests/          pytest suite (including the dashboard's browser, E2E, and reliability suites)
docs/           documentation
openapi.yaml    implemented v1 API contract (OpenAPI 3.1; bearer authentication, a permission per operation)
AGENTS.md       instructions for AI coding agents
```
