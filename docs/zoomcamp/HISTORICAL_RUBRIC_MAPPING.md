# Rubric mapping — Agent Factory vs. AI Dev Tools Zoomcamp final project

| | |
| --- | --- |
| Requirements | [OFFICIAL_REQUIREMENTS.md](OFFICIAL_REQUIREMENTS.md) — `DataTalksClub/ai-dev-tools-zoomcamp@49b7a73`, retrieved 2026-10-02. **The rubric is officially a draft.** |
| Evidence baseline | Release `v0.2.0` (`bca41376`). Results recorded at release: 392 non-live tests passed; 16 live smoke and 44 live adversarial tests passed on identical application code. |
| Later evidence | Branch `feature/product-data-foundation` (unreleased, not merged): product domain model, repository interfaces, engine milestone emission, [`docs/ai-workflow.md`](../ai-workflow.md). Added as evidence under C2, C3, C7, and C9. No status or self-estimate changed. |
| Later evidence | Branch `feature/postgres-persistence` (unreleased, not merged): PostgreSQL storage with migrations, repositories, configuration, integration tests, and [`docs/database.md`](../database.md). Added as evidence under C3, C7, C9, C13, and C14. One self-estimate changed: C7, from 0–1 to 1–2. No status changed. |
| Later evidence | Branch `feature/openapi-contract` (unreleased, not merged): [`openapi.yaml`](../../openapi.yaml) (OpenAPI 3.1, contract tests), [`docs/api-contract.md`](../api-contract.md), [`AGENTS.md`](../../AGENTS.md). **Changes: C2 to SATISFIED (1 → 2), C5 to PARTIAL (0 → 1), C12 to PARTIAL (0 → 1).** Each is justified in its section below. |
| Later evidence | Branch `feature/backend-api` (unreleased, not merged): a local FastAPI backend implementing all 11 `openapi.yaml` operations on PostgreSQL ([`docs/backend.md`](../backend.md)), with HTTP integration tests against both stores and runtime conformance tests against the checked-in contract. **Changes: C5 to SATISFIED (1 → 2), C6 to PARTIAL (0 → 2–3).** C7 and C9 gain evidence; their estimates are unchanged. |
| Later evidence | Branch `feature/auth-worker` (unreleased, not merged): durable execution of approved actions, stages E1–E2 ([design](../design/action-execution.md), [`docs/safety.md`](../safety.md#durable-execution-approval-is-not-execution)). An execution ledger (PostgreSQL migration `0003`, or a per-run checkpoint file), write-ahead dispatch, replay of recorded results, reconciliation of uncertain outcomes, and crash tests that end real child processes. Added as evidence under C7, C9, and C13. **No status or self-estimate changed.** |
| Later evidence | Branch `feature/auth-worker`, stage E3 (unreleased, not merged): authentication and authorization ([`docs/authentication.md`](../authentication.md)). The API verifies OpenID Connect access tokens as an OAuth 2.0 resource server; principals and per-operation permissions are stored in PostgreSQL (migration `0004`); approval decisions record the authenticated principal; 20 manual mutants. Added as evidence under C6, C9, and C13. **No status or self-estimate changed:** no production identity provider is configured and the API is still local-only. |
| Later evidence | Branch `feature/auth-worker`, stage E4 (unreleased, not merged): worker coordination ([`docs/workers.md`](../workers.md)). Run claims in PostgreSQL (migration `0005`), bounded leases renewed by heartbeats, fencing tokens checked in the same transaction as every write, a claim query with `FOR UPDATE SKIP LOCKED`, and a worker loop with graceful shutdown (`worker run`). Evidence: 13 race and failure scenarios against a real PostgreSQL, real-process tests (death, takeover, a zombie fenced out, a real Ctrl+Break), a real database restart, and 15 manual mutants. Added as evidence under C6, C7, C9, and C13. **No status or self-estimate changed:** workers do not yet consume recorded decisions, they share one host, and nothing is deployed. |
| Later evidence | Branch `feature/auth-worker`, stage E5 (unreleased, not merged): approval and reconciliation consumption ([`docs/workers.md`](../workers.md#human-decisions-wake-ups-and-consumption)). Workers apply recorded decisions under their claim after re-checking the exact action, the 12-hour expiry, and the decider; a reconciliation API (`actions:reconcile`) with a 15-minute retry authorization; an execution timeline and an attention inbox in the contract (2 new operations); migration `0006`. Evidence: end-to-end tests from an authenticated HTTP decision to a worker's single effect, 11 races and 10 real process deaths against PostgreSQL, and 22 manual mutants. Added as evidence under C5, C6, C7, C9, and C13. **No status or self-estimate changed:** no production identity provider, no frontend, one host, and nothing is deployed. |
| Later evidence | Branch `feature/dashboard-v1` (unreleased, not merged): Dashboard v1 ([`docs/frontend.md`](../frontend.md), [`docs/dashboard-reliability.md`](../dashboard-reliability.md)). A React + TypeScript + Vite dashboard with one API client (`frontend/src/api/client.ts`) typed from `openapi.yaml`; local-development sign-in only. Evidence: 53 Vitest unit and component tests, 7 contract and source-policy tests, and 37 browser, end-to-end, and reliability tests against real Chrome, the API, PostgreSQL, and workers, with run instructions in [`docs/testing.md`](../testing.md#dashboard-tests-unreleased). Added as evidence under C4 and C9. **No status or self-estimate changed** pending owner review: development authentication only, and nothing is deployed. |
| Later evidence | Branch `feature/notifications-v1` (unreleased, not merged): attention notifications, phase 1 ([`docs/notifications.md`](../notifications.md)). The work:<br>• a transactional outbox (migration `0007`) written with each approval or reconciliation event;<br>• a separate dispatcher with `SKIP LOCKED` claims, leases, claim-token fencing, deduplication, bounded retries with jitter, and honest unknown outcomes;<br>• a provider abstraction with a fake provider only;<br>• a read-only, contract-first `notifications` status on `ApprovalRequest`, and the dashboard's notification line.<br>Evidence: in-memory, HTTP, and PostgreSQL tests, including seven crash boundaries with real dispatcher processes killed; 7 real-stack browser scenarios, including frontend, API, worker, dispatcher, and PostgreSQL outages; 19 manual mutants. Added as evidence under C5, C6, C7, and C9. **No status or self-estimate changed:** no real provider, nothing deployed. |
| Later evidence | Branch `feature/real-model-v1` (unreleased, not merged): real model providers, foundation ([`docs/model-providers.md`](../model-providers.md)). The work:<br>• the Model Gateway: minimal context, a secret guard, per-call/visit/run budgets and a restart-safe usage ledger, bounded retries, strict schemas, audit;<br>• versioned prompts and capability manifests derived from policy;<br>• an Anthropic adapter tested offline only;<br>• migration `0008` and a contract-first `Run.model_usage`, shown on the project page.<br>Evidence: offline tests, including injected provider failures, secret canaries, and a compromised model obeying prompt injection; PostgreSQL usage tests; 11 manual mutants. Added as evidence under C5, C6, C7, C9, and C13. **No status or self-estimate changed:** no real model call yet. |
| Stance | **Conservative.** A related concept is not credit. Point estimates are self-assessments; peer reviewers decide. |

Every requirement below has four clearly separated parts:

- **Course requirement**: what the official text says, quoted or closely
  paraphrased.
- **Our interpretation**: how we read it, labelled as ours.
- **Existing evidence**: what the repository demonstrates today, with files
  and tests.
- **Future work**: what is missing and what will be shown at submission.

Status values:

| Status | Meaning |
| --- | --- |
| **SATISFIED** | Fully met, with concrete evidence in the repository |
| **PARTIALLY SATISFIED** | Some real evidence, but the top scoring level is not met |
| **NOT YET SATISFIED** | No qualifying evidence |
| **NOT APPLICABLE** | The requirement does not apply to this project |

## Summary — the 14 scored criteria

| # | Criterion | Max | Status | Self-estimate |
| --- | --- | --- | --- | --- |
| 1 | Problem Description | 2 | PARTIALLY SATISFIED | 1 |
| 2 | AI-Assisted Development Workflow | 2 | **SATISFIED** | 2 |
| 3 | Technologies and System Architecture | 2 | PARTIALLY SATISFIED | 1 |
| 4 | Frontend Implementation | 3 | NOT YET SATISFIED | 0 |
| 5 | API Contract | 2 | **SATISFIED** | 2 |
| 6 | Backend Implementation | 3 | PARTIALLY SATISFIED | 2–3 |
| 7 | Database Integration | 2 | PARTIALLY SATISFIED | 1–2 |
| 8 | Containerization | 2 | NOT YET SATISFIED | 0 |
| 9 | Integration Testing | 2 | PARTIALLY SATISFIED | 1 |
| 10 | Deployment | 2 | NOT YET SATISFIED | 0 |
| 11 | CI/CD Pipeline | 2 | NOT YET SATISFIED | 0 |
| 12 | Agent Extension Pack | 2 | PARTIALLY SATISFIED | 1 |
| 13 | Security, Audit, and DevOps Hardening | 2 | PARTIALLY SATISFIED | 0–1 |
| 14 | Reproducibility | 2 | PARTIALLY SATISFIED | 1 |
| | **Total** | **30** | 2 satisfied · 8 partial · 4 not yet · 0 N/A | **12–15** |

> **Why the score is low, honestly.** The rubric scores a deployed
> full-stack web application: frontend, backend, API contract, database,
> containers, CI/CD, and cloud. Agent Factory v0.2.0 is a deliberately
> safety-first orchestration *engine* with a command-line interface. Its
> strengths are depth, not breadth: tested workflow, approvals, and a
> sandbox proven by adversarial tests. Most rubric points come from layers
> that do not exist yet. Those layers are also what the product needs next
> (dashboard, mobile approval), so closing the gap does not distort the
> product. See [ROADMAP.md](ROADMAP.md).

---

## Criterion 1 — Problem Description (max 2)

- **Course requirement.** 2 points: "The README clearly describes the
  problem, the system functionality, and expected behavior."
- **Our interpretation.** The README must say *why* the product exists (the
  problem), *what* it does, and *what a user should expect to happen*.
- **Status.** PARTIALLY SATISFIED.
- **Existing evidence.**
  - [`README.md`](../../README.md) describes functionality (graph workflow,
    agents, approvals, sandbox), expected behaviour (demo route), and the
    security model.
  - [`PRODUCT_SPEC.md`](PRODUCT_SPEC.md) §1–4 now states the problem, target
    user, and user story.
- **Gap.** The README does not state the problem from the user's point of
  view; the problem statement lives only in `docs/zoomcamp/`.
- **Future work.** Add a short problem statement and user story to the top
  of the README, linking to the specification. Consider a root
  `product-spec.md` pointer (see structural requirements below).
- **Submission evidence.** README "Problem" section plus a link to the
  product specification.

## Criterion 2 — AI-Assisted Development Workflow (max 2)

- **Course requirement.**
  - 1 point: "describes how AI tools were used to build the system".
  - 2 points: "clearly documents the AI workflow, including prompts or task
    delegation, context files, manual review, and verification".
- **Our interpretation.** Readers should be able to see *who did what*
  (human vs. AI), *what instructions* drove the work, *what context* the AI
  had, and *how its output was reviewed and verified*. This is explicitly
  not a claim that the owner hand-wrote AI-produced code.
- **Status.** **SATISFIED** (self-estimate 2; was PARTIALLY SATISFIED, 1).
  Every element of the level-2 text now has concrete evidence:

  | Level-2 element | Evidence |
  | --- | --- |
  | Prompts or task delegation | [`docs/ai-workflow.md`](../ai-workflow.md): the structure of each assignment with verbatim constraint quotes; the phases table |
  | Context files | [`AGENTS.md`](../../AGENTS.md) (branch `feature/openapi-contract`): boundaries, commands, security invariants, frozen refs, prohibited shortcuts |
  | Manual review | `docs/ai-workflow.md`: the owner's review of every report, and the recorded authorization points (image pulls, installs, network) |
  | Verification | `docs/ai-workflow.md`: test layers, live suites, manual mutation checks, with results per milestone |

  **Remaining risk.** The full original prompts are not published, only
  representative quotes. A reviewer who wants verbatim prompts might score
  1.
- **Existing evidence.**
  - Commit trailers:
    - Every commit up to `v0.2.0` (12 of 12), and the Zoomcamp alignment
      commit after it, carries a `Co-Authored-By: Claude …` trailer. The
      history itself records that an AI coding agent co-authored the work.
    - From the product-data-foundation commit onward, the owner instructed
      that commits carry no automatic trailer. Disclosure for those commits
      rests on the workflow document below.
  - **New, on the feature branch:** [`docs/ai-workflow.md`](../ai-workflow.md)
    documents:
    - who does what, and the six-step loop (human requirement →
      AI-assisted implementation → automated tests → report → human review
      and authorization → next assignment);
    - the structure of each assignment, quoting representative constraints
      verbatim;
    - verification layers, including manual mutation checks;
    - the human authorization points;
    - a table of every phase so far.
  - A shorter factual account is in
    [LEARNING_NOTES.md](LEARNING_NOTES.md#ai-assisted-development-how-agent-factory-was-built).
  - Verification practice is visible in the repository:
    - stage-by-stage commits;
    - the design document with its approval amendments
      ([`docs/design/v0.2-sandbox.md`](../design/v0.2-sandbox.md));
    - findings and fixes recorded in commit messages (af-entry exit codes,
      CPU-limit mapping).
- **Gap.**
  - The full original prompts are not in the repository, only representative
    quotes. Whether to publish them is the owner's decision.
  - All of this lives on unreleased branches.
- **Future work.** Keep `AGENTS.md` and `docs/ai-workflow.md` accurate as the
  workflow evolves, and consider publishing representative full prompts.
- **Submission evidence.** `AGENTS.md`, `docs/ai-workflow.md`, example
  stage instructions, and commit trailers.

## Criterion 3 — Technologies and System Architecture (max 2)

- **Course requirement.** 2 points: "clearly describes the frontend,
  backend, database, containerization, CI/CD, and how they fit together".
- **Our interpretation.** The architecture document must cover those five
  layers specifically. Describing other components well earns at most the
  middle level.
- **Status.** PARTIALLY SATISFIED.
- **Existing evidence.**
  - [`docs/architecture.md`](../architecture.md) explains each component's
    role (graph, engine, agents, tools, safety, sandbox, providers, state).
  - [`docs/sandbox.md`](../sandbox.md) explains the Docker isolation
    architecture.
  - [`docs/graph-and-state.md`](../graph-and-state.md) explains persistence.
  - **New, on the feature branch:** `docs/architecture.md` includes the
    product-domain layer, and [`docs/product-domain.md`](../product-domain.md)
    describes its entities, state machines, persistence boundary, and engine
    integration.
  - **New, on `feature/postgres-persistence`:** `docs/architecture.md` adds
    the durable-storage layer and the trust boundary between the
    orchestrator, the database, and the sandbox. [`docs/database.md`](../database.md)
    describes the database layer in full. One of the five layers the
    criterion names is now described; the others don't exist yet.
  - **New, on `feature/capability-registry-aws`:** `docs/architecture.md`
    adds the Capability Registry and tool packs. None of the five layers
    the criterion names, so no score change.
- **Gap.** There is no frontend, backend, database, or CI/CD to describe.
  Containerization exists only for the sandbox.
- **Future work.** Once those layers exist, add a system diagram and update
  `docs/architecture.md`.
- **Submission evidence.** Architecture document with a diagram covering
  all five layers.

## Criterion 4 — Frontend Implementation (max 3)

- **Course requirement.** 3 points: functional, well-structured frontend
  "with centralized backend communication" and tests for core logic.
- **Our interpretation.** A user-facing application (web UI), not a
  command-line interface.
- **Status.** NOT YET SATISFIED.
- **Existing evidence.** None. The only interface is a CLI
  ([`agent_factory/cli.py`](../../agent_factory/cli.py)), which does not
  count.
- **Gap.** No frontend.
- **Future work.** A mobile-responsive dashboard: runs, milestones, logs,
  and an approval screen (PRODUCT_SPEC FR-021–FR-023 and FR-029). It should
  have one API client module and its own tests.
- **Submission evidence.** `frontend/`, frontend tests, run instructions,
  and screenshots.

## Criterion 5 — API Contract (max 2)

- **Course requirement.** 2 points: OpenAPI specification that "reflects
  frontend requirements and is used as the contract for backend
  development".
- **Our interpretation.** `openapi.yaml` is written first, from the
  frontend's needs, and the backend is built and tested against it.
- **Status.** **SATISFIED** (self-estimate **2**; was PARTIALLY SATISFIED, 1,
  until a backend existed).
- **Existing evidence (branches `feature/openapi-contract` and
  `feature/backend-api`, unreleased).**
  - [`openapi.yaml`](../../openapi.yaml): OpenAPI 3.1.0 at the repository
    root, written contract-first, with 11 operations. They cover projects,
    runs, milestones, approvals and decisions, templates, errors,
    pagination, idempotency, and planned authentication.
  - **It reflects frontend requirements:** it was designed from the
    dashboard, overnight-progress, and mobile-approval screens. Its
    examples tell that exact story ([`docs/api-contract.md`](../api-contract.md)).
  - `tests/test_openapi_contract.py` (35 tests):
    - validation against the **official OpenAPI 3.1 schema**;
    - every example validated against its schema;
    - ID patterns and enums checked against the domain code;
    - absent capabilities and security rules enforced.
  - **New, stage E5 on `feature/auth-worker`:** two operations were added
    contract-first, before any code: `listAttention` (the phone's "what
    needs me now?" list) and `reconcileAction`. The approval view gained
    what happened to its action (`execution`, `timeline`), for the
    mobile-approval screen. 13 operations; the new vocabularies are checked
    against the domain, and the new models by runtime conformance. Already
    at the maximum: unchanged.
  - **Used as the contract for backend development** (`feature/backend-api`):
    - the backend implements the checked-in file. FastAPI's generated
      schema is switched off, so there is no second source of truth;
    - `tests/test_api_conformance.py` compares the running app with
      `openapi.yaml`: the exact operation set, and per operation the
      parameters, status codes, and response models. Every API model must
      have exactly its contract schema's fields, required set, and closed
      shape;
    - `tests/api_support.py` validates **every** real HTTP response in the
      HTTP tests (body, status, media type, documented headers) against the
      YAML.
- **Why 2 now.** Both parts of the level-2 text are demonstrated. The
  contract was designed from the dashboard and mobile-approval screens.
  The backend was built against it, and tests prove that real responses
  conform.
- **Caveats.**
  - Implementing the backend needed small, additive contract changes:
    422 on three GET operations for invalid query parameters, and
    `route_not_found` / `method_not_allowed` codes. They are listed in
    [`docs/api-contract.md`](../api-contract.md).
  - No frontend consumes the contract yet (C4).
- **Future work.** Generate or check the frontend's API client against the
  same file.
- **Submission evidence.** `openapi.yaml`, the contract tests, and the
  backend conformance tests.

## Criterion 6 — Backend Implementation (max 3)

- **Course requirement.** 3 points: well-structured backend that "follows
  the OpenAPI specification, and includes tests covering core
  functionality".
- **Our interpretation.** An HTTP service implementing the contract. The
  orchestration engine can sit behind it, but the engine on its own is not
  that service.
- **Status.** PARTIALLY SATISFIED (self-estimate **2–3**; was NOT YET, 0).
- **Existing evidence (branch `feature/backend-api`, unreleased).**
  - **Implements the required endpoints:** all 11 `openapi.yaml`
    operations, on real PostgreSQL ([`agent_factory/api/`](../../agent_factory/api/),
    [`docs/backend.md`](../backend.md)).
  - **Well structured:**
    - thin routes, then API models, then one service per use case, then
      the domain, repository interfaces, and PostgreSQL;
    - dependency injection and a bounded connection pool;
    - RFC 9457 errors with request IDs, structured logs, and
      liveness/readiness checks.
  - **Follows the OpenAPI specification:** runtime conformance tests, and
    every HTTP test response validated against the checked-in YAML (see
    C5).
  - **Tests covering core functionality:**
    - `tests/test_api_http.py` runs every scenario against the in-memory
      store and against real PostgreSQL through the production pool. It
      covers durable idempotent project creation (including concurrent
      requests and an API restart), cursor pagination and tampering,
      time-range filters, the approval inbox, decision semantics
      (201/200/409), server-controlled actor and time, and secret and
      path canaries;
    - `test_api_conformance.py` and `test_api_unit.py` cover the rest;
    - manual mutation checks were done on the security-critical logic.
- **Why not a firm 3.** The literal level-3 text is plausibly met, but we
  score conservatively:
  - authentication now exists (token verification, principals, a
    permission per operation), but **no production identity provider is
    configured**, so the API is still local-only by design;
  - the code lives in `agent_factory/api/`, not a `backend/` directory;
  - the branch is unreleased.

  Peer reviewers may reasonably read "core functionality" to include those.
- **New, stage E3 on `feature/auth-worker`:** every operation is
  authenticated and authorized on the server. HTTP tests cover 401, 403,
  and 429 and a permission matrix over all 11 operations, on both stores
  ([`docs/authentication.md`](../authentication.md)). This removes one
  reason for caution, not all of them, so the estimate stays at 2–3.
- **New, stage E4 on `feature/auth-worker`:** worker processes claim queued
  runs from PostgreSQL and advance them under leases and fencing tokens. A
  stale worker cannot change a run, which is proved with real processes and
  a real database restart ([`docs/workers.md`](../workers.md)). Workers still
  do not consume recorded decisions (stage E5), so a run cannot yet be
  advanced end to end from an API decision. The estimate stays at 2–3.
- **New, stage E5 on `feature/auth-worker`:** a run can now be advanced end
  to end from the backend: an authenticated decision or reconciliation
  answer over HTTP is recorded with the run's wake-up, and a worker applies
  it after re-checking it (`tests/test_e2e_human_decisions.py`, both stores).
  Two operations were added contract-first (`reconcileAction`,
  `listAttention`). That removes the "no end-to-end path" reason for caution;
  the others (no production identity provider, an unreleased branch, no
  `backend/` directory) remain, so the estimate stays at 2–3.
- **Future work.** A configured identity provider and a deployed
  configuration (C10), then reassess.
- **Submission evidence.** `agent_factory/api/`, the HTTP and conformance
  test suites, and `docs/backend.md`.

## Criterion 7 — Database Integration (max 2)

- **Course requirement.**
  - 0 points: "No database or persistent storage".
  - 2 points: "Database layer is properly integrated, supports different
    environments, and is documented".
  - The course README also says "Add database or persistent storage".
- **Our interpretation.**
  - Persistent storage avoids zero, but the scoring levels describe a
    *database*.
  - "Supports different environments" means the same code runs against
    differently configured databases without changes.
  - At v0.2.0 there was file-based storage only. The PostgreSQL branch adds
    a real database layer.
- **Status.** PARTIALLY SATISFIED. **Self-estimate raised from 0–1 to
  1–2**, on the concrete evidence below.
  - Level 1 is clearly met: a database is integrated, and its configuration
    and use are documented.
  - Level 2 is arguably met for the environments that exist. The
    integration is proper (migrations, repositories, transactions,
    reconciliation, integration tests), and it is documented. Development
    and test environments use separate databases and separate configuration
    variables.
  - It is not shown for a deployed environment: there is no deployment, no
    secret manager, and no production configuration. Hence 1–2, not 2, and
    the status stays PARTIAL.
- **Existing evidence.**
  - **New, on `feature/postgres-persistence`: a PostgreSQL database layer.**
    - **Schema:** five tables with primary and foreign keys, unique and
      check constraints, and two query-driven indexes. It is created by a
      versioned migration
      ([`0001_initial.sql`](../../agent_factory/persistence/migrations/0001_initial.sql))
      through a tracked, checksummed migration runner
      ([`migrator.py`](../../agent_factory/persistence/migrator.py)).
    - **Repositories:** PostgreSQL implementations of the existing
      interfaces, with transactions and row locks
      ([`postgres.py`](../../agent_factory/persistence/postgres.py)).
    - **Configuration per environment:**
      - URL from `AGENT_FACTORY_DATABASE_URL` or `..._URL_FILE`; schema
        selectable;
      - a separate `AGENT_FACTORY_TEST_*` configuration for the test
        database;
      - [`config/database.example.env`](../../config/database.example.env);
      - a least-privilege database role;
      - the password never printed.
    - **Integration:** `--database` on `demo`, `approve`, and `resume`;
      `db check`, `migrate`, `verify`, and `reconcile`.
    - **Documented:** [`docs/database.md`](../database.md).
    - **Tests (real PostgreSQL, 54):** `tests/test_postgres_migrations.py`,
      `test_postgres_store.py`, `test_postgres_engine.py`, and the
      `[postgres]` contract variants. They include persistence across
      three separate processes.
  - **New, on `feature/auth-worker`: an execution ledger in the
    database.**
    - Migration
      [`0003_action_executions.sql`](../../agent_factory/persistence/migrations/0003_action_executions.sql)
      adds three tables: executions, attempts, and reconciliations.
    - Composite foreign keys bind each execution to an *approved* decision
      for the same action fingerprint, one execution per approval. Check
      constraints mirror the execution state machine.
    - Each ledger write is its own transaction, committed with
      `synchronous_commit = on` before the side effect; no transaction is
      held open across one.

    This is more depth in the same environments, not a new environment, so
    the self-estimate is unchanged.
  - **New, stage E5 on `feature/auth-worker`: migration
    [`0006_human_decisions.sql`](../../agent_factory/persistence/migrations/0006_human_decisions.sql).**
    A backfill and a CHECK so that every pending approval expires; a
    `reconciliation_decisions` table whose foreign keys bind each answer to a
    real attempt of the run's own execution and its exact action; a
    wake-up counter on `run_claims`; two partial indexes for the attention
    inbox, each with a plan test. A migration test applies 0006 to a database
    built by 0001-0005. Depth in the same environments: unchanged.
  - **New, stage E4 on `feature/auth-worker`: run claims.**
    - Migration
      [`0005_run_claims.sql`](../../agent_factory/persistence/migrations/0005_run_claims.sql)
      adds one table and one partial index for the claim query (plan
      tested).
    - Constraints make illegal ownership states unstorable, for example an
      owner without a lease, or a lease longer than five minutes.
    - Every write for a claimed run is fenced by a row lock in its own
      transaction.

    Again more depth in the same environments: unchanged.
  - Earlier evidence, still valid:
    - Atomic per-run checkpoints
      ([`checkpoint.py`](../../agent_factory/engine/checkpoint.py)) and an
      append-only, hash-chained log
      ([`log.py`](../../agent_factory/engine/log.py)), documented in
      [`docs/graph-and-state.md`](../graph-and-state.md).
    - The persistence boundary (repository interfaces) from the
      product-data-foundation branch, which the PostgreSQL layer implements.
- **Gap.**
  - No deployed or production environment: no secret manager, no TLS, and
    no backups. (The backend now has a bounded connection pool,
    `persistence/pool.py`, and durable idempotency storage in migration
    `0002`; that is new evidence, but it doesn't change the estimate.)
  - The CLI records product data only with `--database`.
  - Both branches are unreleased.
- **Future work.**
  - Production configuration through a secret manager (the URL-file
    mechanism is ready for it), with the deployment (C10).
- **Submission evidence.** Migrations, `docs/database.md`, the example
  configuration, integration-test results, and (later) the production
  configuration.

## Criterion 8 — Containerization (max 2)

- **Course requirement.**
  - 1 point: "Dockerfiles exist, but running the full system requires manual
    steps".
  - 2 points: "The full system runs via Docker or Docker Compose".
- **Our interpretation.** Containerizing *the application*. The existing
  Dockerfile builds the *sandbox runtime for untrusted code*; it does not
  containerize Agent Factory itself.
- **Status.** NOT YET SATISFIED.
- **Existing evidence.**
  - [`sandbox_image/Dockerfile`](../../sandbox_image/Dockerfile) shows
    Docker competence, but it is not application containerization.
  - The orchestrator runs on the host.
- **Gap.** No application image and no `docker-compose.yml`.
- **Future work.** Compose file for backend, frontend, and database.
  Running the sandbox from a containerized orchestrator needs a deliberate
  trust decision; see "Conflicts" below.
- **Submission evidence.** `docker-compose.yml` and one-command start
  instructions.

## Criterion 9 — Integration Testing (max 2)

- **Course requirement.** 2 points: "Integration tests are clearly
  separated, cover key workflows, and are documented".
- **Our interpretation.** Separate integration tests from unit tests, and
  cover the end-to-end workflows a user relies on. For the final project,
  those include the future API, database, and UI flows.
- **Status.** PARTIALLY SATISFIED.
- **Existing evidence.**
  - Live Docker integration suites are separated by pytest markers and
    explicit opt-in flags (`docker_live` with `--run-docker-live`;
    `docker_adversarial` with `--run-docker-adversarial`;
    [`tests/conftest.py`](../../tests/conftest.py)). They are documented in
    [`docs/testing.md`](../testing.md).
  - They cover key workflows:
    `tests/test_sandbox_live.py::test_untrusted_run_end_to_end_in_docker`
    and `test_default_routing_executes_untrusted_code_in_docker`.
  - The adversarial suite, `tests/test_sandbox_adversarial.py`, has 44 live
    tests.
  - Engine-level integration tests (real workspace and real test
    subprocess) are in `tests/test_executor.py`.
  - **New, on the feature branch:** `tests/test_domain_engine.py` runs the
    real executor, real workspaces, and real test subprocesses with a
    product store attached. It covers:
    - milestone emission for repair, review, and exhausted runs;
    - approval and rejection;
    - crash and resume;
    - recording failures;
    - project-specific milestones.
  - **New, on `feature/postgres-persistence`: database integration tests,
    clearly separated.**
    - 54 tests against a real PostgreSQL, under their own marker
      (`postgres`) and opt-in flag (`--run-postgres`). They fail rather than
      skip when requested, and are documented in
      [`docs/testing.md`](../testing.md#postgresql-integration-tests).
    - They cover the key database workflows:
      - migrations;
      - persistence across reconnects and across three separate CLI
        processes;
      - approvals and concurrent decisions;
      - rollback;
      - reconciliation after a real connection loss;
      - constraints enforced on raw SQL.
    - Two further live tests (`docker_live` and `postgres`) prove the
      sandbox cannot reach the database.
  - **New, on `feature/auth-worker`: crash and concurrency tests for
    approved actions**, documented in
    [`docs/testing.md`](../testing.md#durable-action-execution-tests-unreleased).
    - A crash matrix ends a real child process (`os._exit(137)`, no
      cleanup) at eight boundaries for two classes of tool, restarts it, and
      counts the real effects in oracle files.
    - One ledger contract runs against the checkpoint file, the in-memory
      store, and real PostgreSQL. It includes six concurrent contenders for
      one dispatch, and losing the database connection mid-action.
    - The PostgreSQL suite now reports 151 passed (it was 121).

    They sit in the default suite, with `[postgres]` variants, so the
    separation gap below is unchanged.
  - **New, stage E3 on `feature/auth-worker`:** authentication and
    authorization tests over HTTP on real PostgreSQL (`test_auth_http.py`,
    `test_auth_principals.py` `[postgres]`), including the documented
    approval-audit query run as written. Same tiering as above; the
    estimate is unchanged.
  - **New, stage E4 on `feature/auth-worker`: worker coordination tests**
    ([`docs/testing.md`](../testing.md#worker-coordination-tests-unreleased)).
    - Races against a real PostgreSQL: 2 and 10 workers for one run, 10
      workers for 10 runs, a renewal against a takeover.
    - Forced interleavings of stale heartbeats, releases, checkpoints, run
      state, and dispatches.
    - Real worker processes that die inside and after a claim, stall as a
      zombie, or are stopped by Ctrl+Break.
    - A live test that restarts the database while a worker owns a run.
    - The PostgreSQL suite now reports 247 passed.

    These are separated by marker (`postgres`, `docker_live`), but still not
    run in CI, and there are no UI flows: unchanged.
  - **New, stage E5 on `feature/auth-worker`: human-decision tests**
    ([`docs/testing.md`](../testing.md#human-decisions-tests-unreleased-e5)).
    - End to end, in both stores: an authenticated person decides or answers
      over HTTP, a real worker applies it, and an oracle file counts the
      effect.
    - 11 races against PostgreSQL (two workers for an approved run, a
      decision arriving while a worker holds the run, conflicting decisions
      and answers, expiry against a claim and against a decision, stale
      workers after a takeover, a duplicate retry authorization).
    - 10 real process deaths at each boundary of decision consumption, each
      recovered by a new worker without an unauthorized repeat effect.

    Still not run in CI, and still no UI flows: unchanged.
- **Gap.**
  - Some engine integration tests are still mixed into the default suite
    with unit tests.
  - **API workflows are now covered** (`feature/backend-api`): HTTP tests
    run against real PostgreSQL under the `postgres` marker (121
    PostgreSQL tests in total), documented in
    [`docs/testing.md`](../testing.md#http-api-tests-unreleased). Their
    in-memory variants still run in the default suite.
  - There are no UI integration tests, because the UI doesn't exist.
  - Database workflows are covered.
- **Why the self-estimate stays at 1.** Our interpretation counts the
  future UI flows as key workflows of the final project, and some
  integration tests still share the default suite with unit tests. Raise
  it when the UI flows are tested and the tiers are run separately in CI.
- **Future work.** Add API and UI integration tests as those layers are
  built, and run every tier in CI.
- **Submission evidence.** A test layout table in `docs/testing.md` and CI
  runs of each tier.

## Criterion 10 — Deployment (max 2)

- **Course requirement.** 2 points: "deployed to the cloud with a working
  URL or clear proof of deployment".
- **Our interpretation.** A publicly reachable deployment of the
  application.
- **Status.** NOT YET SATISFIED.
- **Existing evidence.** None. Nothing is deployed; deployment is
  deliberately out of scope so far.
- **Gap.** No deployment, and no deployment documentation.
- **Future work.** Deploy after authentication exists, because a public
  approval interface must not be unauthenticated. Deployment needs owner
  decisions on cloud account, cost, and credentials.
- **Submission evidence.** URL, a deployment guide, and proof such as
  screenshots or logs.

## Criterion 11 — CI/CD Pipeline (max 2)

- **Course requirement.**
  - 1 point: "CI pipeline runs tests automatically".
  - 2 points: "runs tests and deploys the application when tests pass".
- **Our interpretation.** Automated test runs on push and pull request, plus
  automated deployment gated on passing tests.
- **Status.** NOT YET SATISFIED.
- **Existing evidence.** None. There is no `.github/workflows/`, and the
  repository has no remote yet.
- **Gap.** No CI and no CD.
- **Future work.**
  - CI runs the non-live suite on every push.
  - CD deploys after tests pass, ideally with a manual approval step, which
    is consistent with Agent Factory's own rule that production deployment
    needs approval.
  - Live Docker suites need a runner with Docker; this is a design decision.
- **Submission evidence.** Workflow files and CI run links.

## Criterion 12 — Agent Extension Pack (max 2)

- **Course requirement.**
  - 1 point: "basic project instructions or reusable prompts".
  - 2 points: an extension pack with "project instructions, a reusable
    workflow, a subagent/specialist, an MCP tool/server, a hook or
    guardrail, and permission notes".
- **Our interpretation.** Extensions for the *coding agents used to develop
  this project*, such as an `AGENTS.md`, a skill, or an MCP server. Agent
  Factory's own internal agents are product features, not an extension
  pack.
- **Status.** PARTIALLY SATISFIED (self-estimate **1**; was NOT YET, 0).
- **Existing evidence.**
  - [`AGENTS.md`](../../AGENTS.md) (branch `feature/openapi-contract`):
    real project instructions for coding agents. They are the level-1
    "basic project instructions":
    - architecture boundaries;
    - exact test commands per kind of change;
    - security invariants;
    - frozen refs;
    - database and sandbox rules;
    - prohibited shortcuts;
    - the AI-disclosure policy.
  - Related product concepts still don't count here: role-specific agents,
    the permission policy, and gateway guardrails.
- **Why not 2.** There is no reusable workflow or skill, no subagent, no MCP
  server, no hook, and no extension documentation.
- **Capability Registry, tool packs and AWS pack** (branch
  `feature/capability-registry-aws`): **no score change.** They are product
  features of Agent Factory's own agents, not an extension for the coding
  agents that build this project, so by our interpretation they don't count
  here. No MCP server was built or configured.
- **Future work.** A natural next step is an MCP server exposing Agent
  Factory runs, milestones, and approvals, built on the API once it
  exists.
  - Note: the official text makes the extension-pack contents conditional
    on Module 5, which is not in the 2026 curriculum yet.
- **Submission evidence.** `agent-capabilities/`, `mcp-server/`,
  `docs/agent-extension-pack.md`, and `docs/permissions.md`.

## Criterion 13 — Security, Audit, and DevOps Hardening (max 2)

- **Course requirement.**
  - 1 point: "at least one audit or security scan artifact".
  - 2 points: "PR audit output, deterministic security scan findings,
    agent/extension security notes, operational diagnosis output, and an AI
    tool/data policy".
- **Our interpretation.**
  - Artifacts produced by auditing or scanning the project.
  - The course's Module 4 suggests tool output, for example a deterministic
    scanner such as Semgrep, or PR audits.
  - Our existing security material is self-authored design, notes, and
    tests. A reviewer may or may not accept it as an "audit artifact",
    hence 0–1.
- **Status.** PARTIALLY SATISFIED.
- **Existing evidence.**
  - Agent security notes:
    - [`docs/sandbox.md`](../sandbox.md) (guarantees and limitations, each
      tied to tests);
    - [`docs/safety.md`](../safety.md) (permissions and approvals).
  - Threat model: [`docs/design/v0.2-sandbox.md`](../design/v0.2-sandbox.md).
  - Reproducible containment audit: `tests/test_sandbox_adversarial.py`, 44
    live tests covering host file access, network, secrets, resource
    exhaustion, malformed archives, concurrency, cleanup, fail-closed
    behaviour, and CPU.
  - Findings and fixes are recorded in the commit history.
  - **New, on `feature/postgres-persistence`:** database security notes
    ([`docs/database.md`](../database.md#security-boundary)).
    - Sandboxed code cannot reach the database or see its credentials:
      Tier-0 tests, plus a live test with a control that connects first.
    - Stricter sandbox environment validation.
    - A least-privilege database role.
    - Passwords never printed.

    These are still self-authored notes and tests, not scanner output, so
    the self-estimate is unchanged.
  - **New, on `feature/auth-worker`: replay protection for approved
    actions** ([`docs/safety.md`](../safety.md#durable-execution-approval-is-not-execution),
    [design](../design/action-execution.md)).
    - A node re-run after a second approval, or after a crash, replays a
      recorded success instead of running the approved action again.
    - A non-idempotent action whose outcome is unknown is never resent
      automatically; the run waits for a person
      (`awaiting_reconciliation`).
    - Every dispatch is written ahead to the hash-chained audit log.
    - Thirteen manual mutation checks, recorded in
      [`docs/ai-workflow.md`](../ai-workflow.md).

    Again these are self-authored design and tests, not scanner output, so
    the self-estimate is unchanged.
  - **New, stage E3 on `feature/auth-worker`: authentication and
    authorization** ([`docs/authentication.md`](../authentication.md)):
    strict token verification (algorithm allowlist, algorithm-confusion
    test), fail-closed configuration that refuses development authentication
    in production, tokens never logged or passed to the sandbox (canary
    tests), and 20 manual mutants. Still self-authored, not scanner output:
    unchanged.
  - **New, stage E5 on `feature/auth-worker`: decisions are re-checked and
    attributed.** A stored decision authorizes nothing until a worker
    re-checks it against the exact action, its expiry, and whether its
    author may still decide; approving and reconciling are separate
    permissions; every consumed decision names the person and the worker
    generation that applied it; 22 manual mutants. Still self-authored, not
    scanner output: unchanged.
  - **New, stage E4 on `feature/auth-worker`: a stale worker cannot change
    a run** ([`docs/workers.md`](../workers.md),
    [`docs/safety.md`](../safety.md#workers-a-stale-worker-cannot-change-a-run)):
    - fencing tokens checked by the database in the same transaction as each
      write;
    - fail-closed handling of an unconfirmed lease;
    - no dispatch after ownership is lost;
    - 15 manual mutants.

    Still self-authored, not scanner output: unchanged.
  - **New, on `feature/capability-registry-aws`: the cloud-access boundary**
    ([`docs/capabilities.md`](../capabilities.md),
    [`docs/aws-capabilities.md`](../aws-capabilities.md)):
    - typed AWS capabilities, with no generic call, script, or CLI path;
    - long-term AWS keys refused, with no fallback;
    - AWS results redacted (proven with canaries);
    - static tests that no module imports an AWS SDK or runs a process;
    - 19 manual mutants.

    Fake backend only, no AWS call. Still self-authored, not scanner output:
    unchanged.
  - **New, Stage E1 on the same branch:**
    - `cloud_read` added contract-first;
    - a real AWS backend that refuses implicit credential chains, long-term
      keys and fallbacks, tested offline with network and provider
      tripwires;
    - a hash-chained live-evidence file;
    - 25 more manual mutants.

    No AWS call. Still self-authored: unchanged.
- **Gap.**
  - No committed scan output.
  - No PR audit output.
  - No operational diagnosis output.
  - No explicit AI tool/data policy.
  - No `security/` or `ops/` directories.
- **Future work.**
  - Add a deterministic static-analysis scan with committed findings and
    triage.
  - Capture the adversarial suite report as an artifact.
  - Add an AI tool/data policy (what data may be sent to which AI tools).
  - Add PR audit output once pull requests exist.
  - Add operational diagnosis once something is deployed.
- **Submission evidence.** `security/` and `ops/` directories with these
  artifacts.

## Criterion 14 — Reproducibility (max 2)

- **Course requirement.** 2 points: "Clear instructions exist to set up,
  run, test, and deploy the system end to end."
- **Our interpretation.** Someone else can get from clone to running,
  tested, and deployed using only the documentation.
- **Status.** PARTIALLY SATISFIED.
- **Existing evidence.**
  - Set up, run, and test: [`README.md`](../../README.md) quick start and
    [`docs/testing.md`](../testing.md), with explicit commands for the
    non-live, live smoke, and adversarial suites.
  - Sandbox image preparation is documented and digest-pinned
    ([`docs/sandbox.md`](../sandbox.md)).
  - There are no third-party runtime dependencies (standard library only;
    pytest for tests).
  - **New, on `feature/postgres-persistence`:** step-by-step local database
    setup ([`docs/database.md`](../database.md#local-development-setup)):
    - the image is pinned by digest;
    - the driver is pinned exactly (`requirements-postgres.txt`), and is an
      optional extra, so the core still needs only the standard library;
    - secrets are generated, provisioned, migrated, and verified with
      scripted commands.
- **Gap.**
  - No deploy instructions.
  - The sandbox image lock is machine-specific, so other machines must
    rebuild it.
  - Only verified on Windows with Docker Desktop.
- **Future work.** Add end-to-end setup, run, test, and deploy
  instructions, verified on a clean machine or in CI.
- **Submission evidence.** README "Run it yourself" section and CI proof.

---

## Structural requirements (expected repository contents)

**Course requirement:** "Your project repo should include:" the following.
Statuses reflect `v0.2.0` plus this documentation branch.

| Path | Status | Evidence / gap |
| --- | --- | --- |
| `README.md` | **SATISFIED** | [`README.md`](../../README.md) exists |
| `product-spec.md` | PARTIALLY SATISFIED | The specification exists at `docs/zoomcamp/PRODUCT_SPEC.md`, not at the expected root path |
| `AGENTS.md` or equivalent | **SATISFIED** | [`AGENTS.md`](../../AGENTS.md) at the root (branch `feature/openapi-contract`) |
| `frontend/` | NOT YET SATISFIED | None |
| `backend/` | PARTIALLY SATISFIED | A local HTTP backend exists in [`agent_factory/api/`](../../agent_factory/api/) (branch `feature/backend-api`), not at the expected `backend/` path |
| `openapi.yaml` | **SATISFIED** | [`openapi.yaml`](../../openapi.yaml) at the root, OpenAPI 3.1, validated by the tests (branch `feature/openapi-contract`) |
| `docker-compose.yml` | NOT YET SATISFIED | None |
| `.github/workflows/` | NOT YET SATISFIED | None |
| `docs/` | **SATISFIED** | [`docs/`](../) exists, with architecture, safety, sandbox, testing, and design docs, plus product-domain, AI-workflow, and database docs on the feature branches |
| `security/` | NOT YET SATISFIED | Security docs exist under `docs/`; there is no `security/` artifact directory |
| `ops/` | NOT YET SATISFIED | None |

**Conditional on Module 5** ("If you complete Module 5…"). All are NOT YET
SATISFIED:

| Path | Notes |
| --- | --- |
| `agent-capabilities/` | — |
| `agent-hooks/` | — |
| `mcp-server/` | — |
| `plugins/` or `custom-agent/` | — |
| `docs/agent-extension-pack.md` | — |
| `docs/permissions.md` | `docs/safety.md` documents the product's permissions, not a coding-agent extension's |

**Process requirement.** Peer review: NOT YET SATISFIED. It is required "to
get points" and for the certificate; the rules are not yet published.

### Cross-reference: course "Final Project" bullets → criteria

| Course bullet | Criteria |
| --- | --- |
| Choose a problem and describe expected behavior | 1 |
| Build a frontend | 4 |
| Build a backend | 6 |
| OpenAPI contract | 5 |
| Database or persistent storage | 7 |
| Test the application | 4, 6, 9 |
| Containerize | 8 |
| Deploy so others can try it | 10 |
| Document AI tools, prompts, workflows, agent instructions, MCP, automation | 2, 12 |
| Agent extension pack | 12 |
| Audit and harden with open-source AI security/audit/DevOps tools | 13 |
| Review peer projects | Process requirement |

## Review of every SATISFIED claim

| Claim | Concrete evidence | Verified |
| --- | --- | --- |
| `README.md` exists | `git ls-tree v0.2.0 README.md` | Yes |
| `docs/` exists | `git ls-tree v0.2.0 docs/` | Yes |
| `AGENTS.md` exists | `git ls-tree feature/openapi-contract AGENTS.md` | Yes (on the branch) |
| `openapi.yaml` exists and is valid OpenAPI 3.1 | `tests/test_openapi_contract.py::test_the_contract_is_a_valid_openapi_3_1_document` (official validator) | Yes (on the branch) |
| **C2**: every level-2 element documented | `docs/ai-workflow.md` (delegation, manual review, verification) and `AGENTS.md` (context file); see the table under Criterion 2 | Yes (on the branch) |

One scored criterion is marked SATISFIED: **C2**. It rests on documentation in unreleased branches.

## Conflicts and material roadmap implications

1. **The rubric is a draft.**
   - Rules, scoring, and deadlines "may change".
   - No project deadline or peer-review rules are published, even though the
     cohort's listed end date (2026-09-28) has passed.
   - Plan against it, but re-check before building specifically to it.
2. **It assumes a deployed full-stack web app.** About 18 of the 30 points
   need layers Agent Factory doesn't have: criteria 3–8, 10, and 11. The
   product vision also needs most of these (dashboard, mobile approval), so
   this is an ordering change, not a distortion.
3. **Containerizing "the full system" conflicts with the sandbox's
   needs.**
   - To launch sandbox containers, a containerized orchestrator needs access
     to a Docker engine. Mounting the engine socket gives that container
     root-equivalent control of the host.
   - We forbid the socket for *untrusted* containers. Granting it to the
     trusted orchestrator is a separate trust decision that needs explicit
     approval.
   - Alternatives: keep the orchestrator on the host and containerize only
     API, frontend, and database, or use a separate runner.
4. **Public deployment raises the stakes of the approval interface.**
   - Authentication must exist before the approval UI is reachable from the
     internet.
   - Deployment itself requires spending money and credentials, which are
     owner decisions.
5. **"CD deploys when tests pass" vs. our approval principle.**
   - Agent Factory requires approval for production deployment by its
     *agents*.
   - For the project's *own* CD, a manual approval step keeps the story
     consistent.
6. **The criterion-13 tooling implies new tools.** Examples are a
   deterministic scanner, PR audit, and agent-security scanning.
   "Operational diagnosis" assumes a running system.
7. **The extension pack's status is ambiguous.** It is conditional on
   Module 5, which isn't in the 2026 curriculum, yet criterion 12 is scored.
   An MCP server for runs, milestones, and approvals fits the product
   naturally, so treat it as a later, optional stage.
8. **A real model provider is not required by the rubric.** The product
   needs one; the course does not score one. That frees ordering: web layers
   can come before a real provider.
9. **Commit trailers no longer carry the disclosure.**
   - From the product-data-foundation commit onward, the owner instructed
     that commits have no automatic AI co-author trailer.
   - Criterion 2 evidence for those commits therefore depends on
     [`docs/ai-workflow.md`](../ai-workflow.md). Keep that document accurate
     as the workflow evolves.
