# Instructions for AI coding agents

## Project scope and capability evidence

Read [SKILLS.md](SKILLS.md) for this project's implemented capabilities and
limitations. Distinguish implemented, partially implemented, planned and blocked
capabilities with code and test evidence. Documentation and capability descriptions
do not enable a tool or authorize an external action.

Inspect before modifying; preserve unrelated work and historical evidence.
Continue independently through authorized safe local increments, validate
meaningful invariants, repair failures without weakening guards, and document
decisions. Respect the current task's scope: documentation-only authorization
does not permit application changes. Later explicit owner instructions govern
the assigned scope; sandbox/tool approvals remain mandatory.

Never infer permission from credentials, tests, a roadmap or an editor skill.
Deployment, pushing, publishing, external messages and live API diagnostics retain
the explicit authorization requirements below. Preserve offline operation and
fail-closed sandboxing.

These are the rules for any coding agent (Claude Code, Codex, Copilot, …)
working in this repository. They come from how the project is actually
built and verified, and every one of them has a reason. When a rule and an
instruction from the repository owner conflict, **stop and ask the owner**.

## What this project is

Agent Factory is a graph-based orchestrator for autonomous
software-development runs:

- **Agents:** planner, developer, tester, reviewer.
- **Safety:** exact-action human approvals, durable records of every
  approved action's execution (approval is not execution), and a Docker
  sandbox for untrusted code.
- **Product records:** projects, runs, milestones, and approvals, stored
  durably in PostgreSQL.
- **API:** a local FastAPI backend implementing `openapi.yaml`, backed by
  PostgreSQL. Every product request is authenticated (an OpenID Connect access
  token) and authorized (one permission per operation). **It stays on
  loopback:** no identity provider is configured yet and no deployment exists.
- **Workers:** processes that claim queued runs from PostgreSQL and advance
  them. Each run has one owner at a time, under a lease renewed by
  heartbeats, and every write carries a fencing token that the database
  checks. Workers apply people's decisions (approvals, rejections, expiries,
  reconciliation answers) only after re-checking them. One host, local
  development only.
- **Models:** agents ask a model only through the Model Gateway, which owns
  context limits, a secret guard, budgets, a usage ledger, bounded retries,
  schema validation, and audit. The deterministic FakeProvider is the
  default everywhere. One real adapter (Anthropic, standard-library HTTPS)
  exists, but **no real call is made without explicit configuration and the
  owner's authorization**. The model is untrusted input.
- **Notifications:** when a run needs a person, a durable notification
  intent is written in the same transaction as the attention event (an
  outbox). A separate dispatcher process sends it at least once, through a
  provider adapter. Only a development fake provider exists. **A
  notification alerts; it never authorizes.**
- **Dashboard:** a mobile-first web UI (`frontend/`: React, TypeScript,
  Vite) to observe, approve, and reconcile. A disposable view over the API:
  the browser holds no workflow state. Local development only, signed in
  with the development token issuer.

Python 3.12. The core uses only the standard library. The reproducible reviewer
environment uses Node.js 24 and the packages pinned in `frontend/package-lock.json`.

| Path | What it is | Read first |
| --- | --- | --- |
| `agent_factory/engine/` | Executor, checkpoints, append-only log, run lock, the action guard and checkpoint ledger | `docs/graph-and-state.md`, `docs/design/action-execution.md` |
| `agent_factory/domain/` | Product records, state machines, repository interfaces | `docs/product-domain.md` |
| `agent_factory/persistence/` | PostgreSQL repositories and migrations (optional) | `docs/database.md` |
| `agent_factory/api/` | Local FastAPI routes, schemas, services and observability | `docs/backend.md`, `docs/api-contract.md` |
| `agent_factory/api/auth/`, `domain/principals.py` | Token verification, principals, permissions, authorization | `docs/authentication.md` |
| `agent_factory/worker/`, `domain/claims.py`, `persistence/claims.py`, `engine/ownership.py` | Workers: claims, leases, heartbeats, fencing, the worker loop | `docs/workers.md` |
| `agent_factory/sandbox/` | Execution sandbox (Docker for untrusted code) | `docs/sandbox.md` (the authoritative security statement) |
| `agent_factory/safety/`, `tools/gateway.py` | Permissions and approval gates | `docs/safety.md` |
| `openapi.yaml` | The v1 API contract | `docs/api-contract.md` |
| `agent_factory/providers/`, `agents/prompts.py`, `agents/schema.py` | Model providers: the Model Gateway, budgets and the usage ledger, context assembly, the request guard, capability manifests, versioned prompts, strict schemas, explicit activation, the Anthropic adapter, the fake provider | `docs/model-providers.md` |
| `agent_factory/capabilities/`, `tools/packs.py` | The Capability Registry: typed capability definitions, evidence, availability probes, tool packs, agent views, evidence-based assessment | `docs/capabilities.md` |
| `agent_factory/aws/` | The AWS tool pack: typed capabilities, credential slots, result filtering, the backend boundary (FAKE backend only) | `docs/aws-capabilities.md`, `docs/design/capability-registry-aws.md` |
| `agent_factory/notifications/`, `domain/notifications.py`, `domain/notification_repositories.py`, `persistence/notifications.py` | Attention notifications: the outbox, the dispatcher, the provider boundary, the fake provider | `docs/notifications.md` |
| `frontend/` | Dashboard v1: one API client (`src/api/client.ts`), types generated from `openapi.yaml`, screens, Vitest tests | `docs/frontend.md`, `docs/dashboard-reliability.md` |
| `tests/` | Everything that proves the above | `docs/testing.md` |

## Architecture boundaries

Keep each layer's job its own:

- **The engine contains no SQL and imports no database driver.** It sees
  product storage only through `ProgressRecorder` and the repository
  interfaces.
- **The domain imports no driver and no web framework.** Everything in
  `agent_factory/domain/` must work with the in-memory store.
- **Business code depends on repository interfaces**, never on
  `PostgresDatabase` or the `psycopg` driver. Driver errors are translated
  inside `persistence/postgres.py`.
- **`ToolGateway.call` is the only path from an agent to a side effect.**
  Never let an agent touch files, processes, or the network directly.
- **An approved action runs only through the `ActionGuard`**
  (`engine/actions.py`): write-ahead record first, then the tool, then the
  outcome. Never run an approved tool any other way, and never let an agent
  implement its own replay or retry. The engine reaches the ledger only
  through `ExecutionRepository`.
- **Untrusted code runs only through `SandboxManager`**, which routes it to
  Docker and blocks when Docker is not healthy. There is never a fallback
  to local execution.
- **Workspace git acts only on `<workspace>/project/.git`.** All workspace
  git goes through `Workspace._git`, which proves the repository and pins it.
  Never run git in a workspace any other way, and never let git search
  upward for a repository.
- **The API records intent and workers own execution.** API code must never
  write checkpoints, change run state, write the execution ledger, or execute
  anything (a static test keeps execution code out of `api/`). Its one write
  for workers is the wake-up that commits with a decision (`RunQueue.wake`).
- **A stored decision authorizes nothing by itself.** Only a worker (or a
  claimed operator command) applies one, through `Executor.advance`, after
  `ProgressRecorder.consumable_decision` / `reconciliation_decision` re-check
  it against the engine's exact pending action, the expiry, and the decider.
  Never apply a stored decision any other way, and never let a contradiction
  be "fixed up": it raises `DecisionRefused` and parks the run.
- **Every recorded human decision wakes its run in the same transaction**
  (the outbox), and no release may undo a wake-up that arrived during its
  claim (`run_claims.wake_seq`). Correctness never depends on a
  notification; polling suffices.
- **Operator commands never race workers.** With `--database`, `approve` and
  `reconcile-action` only record intent; `resume`, `demo`, and `db reconcile`
  claim the run first (`worker/operator.py`).
- **One run at a time runs agents or tools in a workspace** (the workspace
  lock in `Executor._locked`): a project's runs share its files.
- **A worker changes a run only under its claim** (`docs/workers.md`).
  - A claimed run's executor gets the claim as its `ownership` and the store
    fenced by the same claim (`open_fenced_store`). Never give it an unfenced
    store.
  - Never write a checkpoint, record a run outcome, or dispatch an approved
    action under an ownership without proving it first. The executor and
    `ActionGuard` already do this.
- **Never swallow `OwnershipLost`.** It is a `BaseException` on purpose. Only
  the worker loop catches it, and then it stops working on the run: no run
  failure, no outcome, no release.
- **Lease validity is decided by PostgreSQL's clock** (`now()`), never a
  worker's. A worker's monotonic deadline may only make it more cautious.
- **Workers are not API users.** No HTTP route claims, renews, releases, or
  controls workers, and workers never use user bearer tokens.
- **Every product route requires exactly one permission** through
  `require_permission` (`api/auth/policy.py`), matching its `x-permission` in
  `openapi.yaml`. Never add a route without one, never check permissions
  inside handlers instead, and never trust identity or permissions sent by a
  client: the actor comes only from the verified token's principal.
- **Tokens stay in the API process.** The engine, domain, sandbox, tools, and
  agents never import authentication code.
- **Thin HTTP handlers.** SQL belongs in persistence; business rules belong
  in services/domain. Validate real HTTP responses against checked-in
  `openapi.yaml`, not only FastAPI's generated schema.
- **The API stays on loopback.** No public binding, tunnel, wildcard CORS,
  or deployment, and no claim of a production identity provider before one is
  configured.
- **A model is untrusted input; it gets no authority** (`docs/model-providers.md`).
  - Agents reach a provider only through `ModelGateway` (`providers/gateway.py`).
    Never call `provider.complete` from new code, and never give a model a
    tool, a shell, a path, or a URL to act on: its schema-valid answer is
    data, and every action still goes through the Tool Gateway.
  - Context is minimal and assembled by `ContextBuilder`: never "send the
    repository". Secret-looking files are withheld, host paths redacted, and
    sizes capped. The `RequestGuard` refuses any request carrying a secret.
    Never weaken it.
  - Prompts live in `agents/prompts.py` as versioned templates. Changing a
    template's text or schema needs a new version (a test pins the
    digests).
  - Project content never enters system instructions or the capability
    manifest; manifests come only from the permission policy and the tool
    registry.
  - Budgets are checked before every call and recorded in the usage ledger
    before the call is sent. Never remove either. Retries are bounded at
    every layer.
  - A real provider is activated only by explicit configuration
    (`AGENT_FACTORY_MODEL_PROVIDER` plus `MODE=live` plus a name plus a key
    file), never by a credential in the environment.
- **A capability describes; the Tool Gateway decides** (`docs/capabilities.md`).
  - Capabilities are registered only by tool packs, in code. Never let a
    project file, model output, or an environment variable add, enable, or
    change one. A pack is enabled only by explicit configuration, never
    because a credential exists.
  - A capability must describe its tool exactly (category, effect,
    parameters). It may tighten a decision (`always`, `denied`), never loosen
    one, and never weaken a tool's execution semantics or bypass approval or
    ActionExecution.
  - Implemented is not available: an unavailable or unregistered capability
    is denied by the gateway. Probes are read-only, free, and secret-free.
  - Evidence comes only from code, existing tests, recorded live runs, and
    probes. Documentation is never evidence, and an assessment never asks a
    model whether something can be done.
  - A capability is never a model-provider tool. Claude's only callable tool
    stays `submit_result`.
- **AWS goes only through typed capabilities** (`docs/aws-capabilities.md`).
  - One fixed operation per tool, behind `AwsBackend`. Never a generic call,
    an SDK method name from a caller, a script, a shell, or the AWS CLI as an
    agent tool, and never the Agent Toolkit's `aws___run_script`.
  - Credentials come from an abstract slot bound to one named profile, and
    only temporary credential kinds are accepted. Never fall back to
    `default`, another profile, or the environment; never use long-term keys.
    The profiles `default` and `internal-dev` are out of scope.
  - Results are projected onto known fields, redacted, and bounded before
    they enter state, logs, or a model request.
  - `FakeAwsBackend` is the default. `BotoAwsBackend` runs only with explicit
    settings *and* `real_backend_authorized=True`; nothing in the product
    sets that. It never uses `boto3`, a default session, the default
    credential chain, or the shared credentials file, and builds exactly one
    approved provider for one allowlisted profile. Never add a fallback.
  - No AWS API call, profile read, `aws login`, `aws sso login`, IAM change,
    or resource creation without the owner's authorization for that stage.
    Offline tests use botocore's Stubber and synthetic config only.
  - AWS reads are `cloud_read`: allowed in development, approval in
    production (a production policy that allows them is refused). A tool
    that may mutate can never be `cloud_read`.
  - IAM, credential, and organization changes are a denied class, never
    approvable.
  - LIVE_TESTED comes only from `evidence/live-capabilities.jsonl`, written
    by an authorized live verification and reviewed. Never write it from a
    unit test, and never hand-edit it.
- **Notifications alert; they never authorize** (`docs/notifications.md`).
  - Only the dispatcher (`agent_factory/notifications/`, started by
    `notifications dispatch`) calls a provider. Graph nodes, agents, tools,
    the engine, API handlers, and the dashboard never send anything, and
    never import the provider code (a static test).
  - A notification intent is written by the recorder in the attention
    event's own transaction (`NotificationRepository.enqueue`, idempotent
    per event and destination). Never write one anywhere else, and never
    send directly.
  - Never hold a database transaction open across a provider call: claim,
    commit the attempt (write-ahead), call, then record the result fenced by
    `claim_token`.
  - Never put approval authority in a notification: no reply-to-approve, no
    secret in a link, no approve button. A link is a plain dashboard path;
    the decision is made only in the authenticated dashboard.
  - Never put tool parameters, paths, credentials, tokens, or addresses in
    a notification, an event, or an API response. Recipients come only from
    principals' destinations and permissions, never from a client.
  - A notification failure never removes or hides an attention item.
- **The dashboard is a view, never an authority** (`docs/frontend.md`).
  - It reaches the API only through `frontend/src/api/client.ts`, the only
    module that calls `fetch` or sets `Authorization`, over the same-origin
    proxy to the loopback API. No CORS, no other origin, no CDN, no inline
    script.
  - It keeps no workflow state: every screen is rebuilt from the API, and
    nothing is shown as decided before the API confirms it.
  - It sends back the action fingerprint exactly as the API supplied it. It
    never resends a decision or answer by itself, and lets a person resend
    only the identical one, after the API shows nothing was recorded.
  - It renders API text as text, never as HTML.
  - It needs no API change. A contract change it needs is proposed
    OpenAPI-first and waits for the owner's approval.

## Commands

Start with the [reviewer guide](docs/REVIEWER_GUIDE.md). Run these commands from
the repository root. The reviewer launcher creates an isolated environment under
`.local/reviewer/.venv`, without global Python packages, and installs the pinned
requirements and npm lockfile. The launcher uses its own PostgreSQL resources.

The reviewer launcher examples use `py -3.12` on Windows; use `python3.12` on
Linux/macOS. In the lower-level examples, `python` means the isolated interpreter:
`.local/reviewer/.venv/Scripts/python.exe` on Windows or
`.local/reviewer/.venv/bin/python` on Linux/macOS. Environment-variable assignments
must use the syntax of your shell. The launcher commands configure their own
database, authentication and worker settings; individual CLI commands require the
configuration described in the linked component documentation.

| What | Command |
| --- | --- |
| Reviewer environment, first setup | `py -3.12 scripts/reviewer.py setup` |
| One-command local startup | `py -3.12 scripts/reviewer.py up` |
| Real-stack smoke check, then stop its child services | `py -3.12 scripts/reviewer.py smoke` |
| Offline, PostgreSQL, frontend and installed-Chrome checks | `py -3.12 scripts/reviewer.py check --browser` |
| Default suite: no Docker, no network, no database (~50 s) | `python -m pytest` |
| The same, without the venv (contract tests are skipped) | `py -3.12 -m pytest` |
| API contract only | `python -m pytest tests/test_openapi_contract.py` |
| Local API | Set `AGENT_FACTORY_DATABASE_URL_FILE` to the local URL file, migrate explicitly with `db migrate`, then `python -m agent_factory api serve` |
| PostgreSQL integration (needs the local dev database) | `AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local/reviewer/test-database.url` then `python -m pytest -m postgres --run-postgres` |
| Live Docker smoke, including the database-boundary tests and the database-restart test (it restarts this project's PostgreSQL container) | `python -m pytest -m docker_live --run-docker-live --run-postgres -v` |
| Live adversarial sandbox suite | `python -m pytest -m docker_adversarial --run-docker-adversarial -v -s` |
| Stress suite (slow; hunts transient failures) | `python -m pytest -m stress --run-stress -v` |
| Sandbox self-check | `python -m agent_factory sandbox check` |
| Principals (database) | `python -m agent_factory auth principal add --issuer URL --subject SUB --grant-all`, `list`, `grant`, `revoke`, `disable`, `enable` |
| Development token issuer (DEVELOPMENT ONLY) | With `AGENT_FACTORY_AUTH_MODE=development`: `auth dev-issuer init`, `auth dev-issuer token --subject SUB --out FILE` |
| Database status | `AGENT_FACTORY_DATABASE_URL_FILE=.local/reviewer/database.url` then `python -m agent_factory db check` |
| One worker (foreground; Ctrl+C stops it after the current step) | With the database configured: `python -m agent_factory worker run`; queue a run with `worker enqueue --project-id PRJ` |
| Model configuration (no call is made) | `python -m agent_factory model check` |
| Live model suite (REAL, possibly paid calls; OWNER AUTHORIZATION REQUIRED) | With the `AGENT_FACTORY_MODEL_*` settings: `python -m pytest -m "model_live and docker_live" --run-model-live --run-docker-live -v -s` |
| Live AWS verification (REAL STS and SSO calls in `us-west-2`; OWNER AUTHORIZATION REQUIRED; Stage E2b, never run yet) | After `aws sso login --profile agent-factory-dev` and with `AGENT_FACTORY_AWS_LIVE_AUTHORIZED_BY` set: `python -m pytest -m aws_live --run-aws-live tests/test_aws_live.py -v -s` |
| Notification destinations (database) | `python -m agent_factory notifications destination add --principal-id PRN --channel email --address ADDR`, `list`, `disable`, `enable`; `notifications status --project-id PRJ --approval-id APR` |
| Notification dispatcher (DEVELOPMENT ONLY: fake provider) | With the database configured and `AGENT_FACTORY_NOTIFICATIONS_MODE=development`: `python -m agent_factory notifications dispatch --provider fake --fake-record FILE --dashboard-url http://127.0.0.1:4173` |
| Dashboard checks (from `frontend/`) | `npm run check` (contract types, `tsc`, Vitest); `npm run build` then `npm run preview` serves it on 127.0.0.1:4173 |
| Dashboard browser, E2E, and reliability suites (real Chrome, API, PostgreSQL, workers) | `AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local/reviewer/test-database.url` then `python -m pytest -m browser --run-browser --run-postgres`; add `--run-docker-live` for the PostgreSQL-outage scenario (it stops and starts this project's container) |

**Opt-in suites fail rather than skip** when requested and unavailable. A
skipped live test during a requested live run is a failure, not a pass.

**Every run gets its own temporary root, outside the repository**, so
concurrent runs are safe. A temporary root inside the repository is refused:
test workspaces are git repositories and must never nest inside this one.

## When to run what

| You changed | Run at least |
| --- | --- |
| Anything | The default suite |
| `domain/`, `persistence/`, the recorder, the executor | The default suite and the PostgreSQL suite |
| `sandbox/`, the executor, tools, the gateway, the action guard, the test runner | The default suite, the PostgreSQL suite, live smoke, adversarial, and `sandbox check` |
| `openapi.yaml` | The contract tests, and `docs/api-contract.md` updated to match |
| `api/` | The default suite, runtime contract-conformance tests, and PostgreSQL API integration; update `docs/backend.md` |
| `api/auth/`, permissions, principals | The default suite, the PostgreSQL suite, contract tests, and a manual mutation check; update `docs/authentication.md` |
| `worker/`, `domain/claims.py`, `persistence/claims.py`, the executor's ownership checks | The default suite, the PostgreSQL suite (with the process tests), live smoke (with the restart test), and a manual mutation check; update `docs/workers.md` |
| Decision consumption: the recorder's re-checks, `domain/reconciliation.py`, `domain/timeline.py`, `domain/attention.py`, the run queue | The default suite, the PostgreSQL suite (including `test_postgres_decisions.py`: races and real process deaths), the contract tests if a view changes, and a manual mutation check; update `docs/workers.md` and `docs/product-domain.md` |
| `frontend/` | `npm run check`, the default suite (its contract and policy tests), and the browser suites with `--run-docker-live`; for decision, reconciliation, polling, or sign-in logic also a manual mutation check; update `docs/frontend.md` (and `docs/dashboard-reliability.md` for failure behaviour) |
| The model layer: `providers/`, `agents/` (prompts, schemas, context) | The default suite (`test_model_providers.py`, never a real call), the PostgreSQL suite, live smoke, and a manual mutation check; update `docs/model-providers.md`. Never run the live model suite without the owner's authorization |
| Capabilities and tool packs: `capabilities/`, `tools/packs.py`, `aws/`, the gateway's capability checks, the `cloud_read` policy, the AWS live harness | The default suite (`test_capabilities.py`, `test_aws_pack.py`, `test_aws_boto.py`, `test_aws_live_harness.py`, never an AWS call), the PostgreSQL suite, live smoke, and a manual mutation check; update `docs/capabilities.md` and `docs/aws-capabilities.md` |
| Notifications: `notifications/`, `domain/notifications.py`, `domain/notification_repositories.py`, `persistence/notifications.py`, the recorder's enqueue | The default suite, the PostgreSQL suite (including `test_postgres_notifications.py`: real processes killed at each delivery boundary), the browser suite `test_dashboard_notifications.py` with `--run-docker-live`, and a manual mutation check; update `docs/notifications.md` |
| Security-critical logic | Also a **manual mutation check**: break the check on purpose, confirm a test fails, then restore it and delete the changed module's `__pycache__` bytecode (Python still runs a same-length mutation restored within the same second) |

Report every result honestly, including failures and skips.

## Security invariants (never weaken)

1. **Exact-action approval.**
   - An approval covers one exact action: tool, category, and every
     parameter, bound by fingerprint.
   - Never let a decision transfer to another action, run, or visit.
   - Never let the API or a database row authorize execution by itself.
     The worker re-checks the engine's pending action.
   - **Approvals expire** (12 hours, server time; the instant itself is
     expired), for deciding and for beginning the dispatch. Never renew one,
     never make one effectively permanent; asking again is a new request.
   - **A retry after reconciliation is a new authorization** of 15 minutes
     from the person's decision. Never reactivate or extend the original
     approval, and never let a reconciliation bypass the ActionExecution
     ledger.
   - `approvals:decide` never implies `actions:reconcile`, nor the reverse.
2. **Dangerous categories are never auto-allowed.** These are money,
   deployment, credentials, deletion, and external communication. Their
   most permissive setting is "require approval".
3. **Fail closed.** If isolation or permission is uncertain, do not act.
   Never add a fallback that runs untrusted code with weaker isolation.
   - **Approval is not execution.** Never infer that an action ran because
     it was approved. Never dispatch an approved action without a durable
     write-ahead record, and never resend a non-idempotent action whose
     outcome is unknown: the run waits for a person.
   - **Every tool declares its execution semantics.** A dangerous-category
     tool without a declaration is refused at registration; never weaken
     that, and never declare a tool more idempotent than its target
     guarantees.
   - **Never claim exactly-once.** State the real semantics: at-least-once,
     effectively-once only with external idempotency, or at-most-once with
     human reconciliation.
4. **No credentials reach untrusted code.**
   - The sandbox environment is built from an allowlist, and names that
     look like secrets (including database settings) are always rejected.
   - Never pass database URLs, API keys, or tokens into a sandbox request,
     staged files, or an agent prompt.
5. **Never print a secret.** Use `DatabaseConfig.redacted()`, and never
   echo a password or put one on a command line. Never log or print an access
   token or an `Authorization` header (the development issuer writes its
   tokens to a file).
6. **The API never exposes** credentials, file paths, sandbox or Docker
   internals, raw exceptions, or stack traces. It never offers routes that
   run commands, edit milestones, change run state, change fingerprints,
   or set identity or permissions.
7. **Authentication fails closed.**
   - Production is the default mode, and an unconfigured API refuses to start.
   - Development or test authentication is never active in production: never
     add a bypass header, an "insecure" flag, or a fallback to it.
   - Only asymmetric, allowlisted algorithms; never disable signature,
     issuer, audience, or expiry checks.
   - Identity is the token's issuer and subject, mapped to an enrolled
     principal; never an email address. A disabled principal never
     authenticates.
   - No permission implies another, and none comes from a token claim.

8. **A stale worker cannot change a run.**
   - Every database write for a claimed run is fenced in the same
     transaction as the write: the fencing token must be current and the
     lease valid.
   - Ownership is proved before every checkpoint write, run outcome, and
     dispatch.
   - An ownership that cannot be confirmed counts as lost.
   - Fencing tokens only ever increase, by one per claim. Never reuse or
     lower one, and never accept a renewal or a release from an older
     generation.
   - Leases stay bounded: the database refuses leases over five minutes.

9. **A notification never authorizes, and nothing real is sent without
   authorization.**
   - Notifications alert only; approval authority stays in the
     authenticated dashboard and the worker's re-checks.
   - Delivery is at least once with deduplication; never claim exactly once,
     and never record an attempt with no answer as delivered or as not sent:
     it is `unknown`.
   - No real provider, account, credential, phone number, or cloud resource
     is used, and no real message is sent, without the owner's explicit
     authorization. Provider credentials would live only in the dispatcher's
     environment; the sandbox rejects their names.

10. **A model has no authority, and no real model call happens without
    authorization.**
    - Model output is untrusted. It is validated against a strict schema,
      and acted on only through the Tool Gateway.
    - No credential, environment variable, host path, or other project ever
      enters a model request.
    - No real (possibly paid) model call is made without the owner's
      explicit authorization and the explicit `AGENT_FACTORY_MODEL_*`
      configuration. Budgets always apply. Never claim a dollar cost without
      configured prices.

11. **The browser is never trusted.**
   - The API authorizes every decision and answer from the verified token;
     hiding a control is never the protection.
   - The development token lives only in the tab's `sessionStorage` and the
     `Authorization` header: never `localStorage`, cookies, URLs, logs,
     error messages, or the bundle.
   - No secret, database URL, key, or `AGENT_FACTORY_*` setting is ever
     bundled into the frontend; its source never reads `import.meta.env`.
   - The production bundle is served with the strict Content Security Policy
     in `frontend/vite.config.js`. Never add `unsafe-inline`, `unsafe-eval`,
     or another origin.

12. **A cloud capability never hands the model a cloud identity.**
    - No AWS credential, profile name, account ID, or `AWS_*` value ever
      enters a model request, the sandbox, logs, the API, or the dashboard.
    - Long-term access keys are refused; only approved temporary credentials,
      through an explicitly bound slot, with no fallback.
    - No real cloud call is made without the owner's explicit authorization
      for that stage.

## Hard rules for the repository

- **Frozen refs: never modify, move, or delete them.**
  - Tags: `v0.1.0`, `v0.2.0`.
  - Branches: `master`, `v0.2-sandbox`, `zoomcamp/alignment`,
    `feature/product-data-foundation`, and `feature/postgres-persistence`.
  - Work on a new branch per milestone.
- **No pushing, no remotes, no publishing** unless the owner explicitly
  instructs it.
- **No network access, installs, image pulls, or image builds** without
  the owner's explicit authorization for that specific action.
  - Sandbox images are built only by a human (`sandbox build-image`).
  - New dependencies are pinned in `requirements-*.txt` (Python), or exactly
    in `frontend/package.json` with `frontend/package-lock.json` (npm), and
    recorded.
  - `npm install` and `npm ci` are network actions. Never run
    `playwright install`: the browser tests use the installed Chrome.
- **Hostile test fixtures** (`tests/hostile/*.hpy`) must never run on the
  host. The live suites' process guard enforces this; don't loosen it.
- **Database.**
  - Migrations are immutable once applied. Change the schema only with a
    new numbered file in `agent_factory/persistence/migrations/`, never by
    editing an old one (the migrator checksums them).
  - Agent Factory never migrates on start-up.
  - Tests use fresh, randomly named schemas, and refuse any database that
    is non-local or not named for testing.
- **Do not touch other projects' resources** on this machine. That
  includes other containers and other databases.
  - The live database-restart test restarts only a container named
    `agent-factory-postgres-*` that publishes the test database's port, and
    refuses anything else. Never point it at another container.

## Prohibited shortcuts

Do not:

- skip, delete, or weaken tests or assertions to make a change pass;
- mark failing tests as skipped or expected failures;
- disable the conftest guards against Docker and the network;
- catch broad exceptions to hide failures;
- edit an applied migration, or rewrite git history on a frozen ref;
- hardcode credentials, or commit anything from `.local/`, `.venv/`, or a
  workspace;
- use `--no-verify`, or bypass hooks or signing;
- add PUT, PATCH, or DELETE endpoints, or "admin" shortcuts, to the
  contract without a design discussion;
- claim a guarantee in docs that a test does not demonstrate;
- mock the API in the dashboard's browser, E2E, or reliability tests, or
  present component tests (which use a scripted API) as reliability
  evidence.

## Documentation expectations

- **Change docs with the behaviour.** If behaviour changes, the doc that
  describes it changes in the same commit. The authoritative docs are
  `docs/sandbox.md` (security), `docs/database.md`, `docs/api-contract.md`,
  and `docs/product-domain.md`.
- **Claim only what is demonstrated.** Say what is not guaranteed as
  clearly as what is.
- **Score conservatively.** `docs/zoomcamp/` maps the work to the course
  rubric; change a status only with concrete evidence in the repository.

## AI-assistance disclosure policy

- **Commits** use the repository owner's configured Git identity.
- **No AI attribution in commits:** no `Co-Authored-By` trailers, and no
  AI-assistance line in commit messages.
- **Disclosure lives in the documentation:** `docs/ai-workflow.md`
  describes how AI agents are used, and must stay accurate.

## Working style

- **One bounded milestone at a time.** Do what the owner's instructions
  say, stop at the stated stop point, and report:
  - what was built;
  - test results;
  - design problems found;
  - remaining gaps;
  - git status;
  - confirmation that frozen refs are unchanged.
- **Stop and ask when a decision belongs to the owner.** For example: a
  network action, a new dependency, a schema change that wasn't requested,
  or a conflict between instructions.
- **Prefer the smallest clean change**, in the style of the surrounding
  code.
