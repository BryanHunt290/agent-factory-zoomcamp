# Roadmap — product needs and Zoomcamp gaps, ordered by dependency

**Baseline:** `v0.2.0`. A sandboxed, tested orchestration engine with a
command-line interface.

**Inputs:**
- [PRODUCT_SPEC.md](PRODUCT_SPEC.md) gives the product needs, by FR and NFR
  ID.
- [RUBRIC_MAPPING.md](RUBRIC_MAPPING.md) gives the course gaps, by criterion
  C1–C14.

Stages are named, not versioned. A version number is chosen when a stage
ships a real capability. Semantic versioning says a new compatible
capability is a *minor* bump, so v0.3.0 should mark the first stage that
delivers one — most likely Stage 2 or 3. That decision is the owner's.

> **Why order by dependency?** Building a screen before the data it shows
> exists means building it twice. Each stage below produces something the
> next stage needs, and each "Why here" explains that link.

## The dependency picture

```
Stage 1  Repository foundations (docs + CI) ─────────────────────────────┐
Stage 2  Milestone + approval domain model  [branch, unreleased]          │
   │                                                                      │
Stage 3  API contract [branch; implemented: 5a] ◄── needs Stage 2's model │
   │                                                                      │
Stage 4  PostgreSQL storage [branch, unreleased] ◄── needs 2's records    │ CI grows
Stage 5  Backend service + authentication      ◄── needs 3 + 4            │ with every
         [5a backend; 5b ledger; 5c auth (E3); 5d workers (E4);       │
          5e human decisions (E5); IdP: not]                              │
Stage 6  Frontend: dashboard + mobile approval ◄── needs 3 (mockable) + 5 │ stage
Stage 7  Containerized full system (Compose)   ◄── needs 5 + 6            │
Stage 8  Deployment + CD                       ◄── needs 7 + auth (5)     │
Stage 9  Notifications + overnight summaries   ◄── needs 2, 6, 8          │
                                                                          │
Track B  Real model provider + cost budget     ◄── needs sandbox (done),  │
         (product-critical; not scored by the course)   budget, secrets ──┘
Optional Agent extension pack (MCP server for runs/approvals) ◄── needs 5
```

## Stage 1 — Repository foundations

- **Status.** Partly done, on unreleased branches. Done:
  - `docs/ai-workflow.md` (`feature/product-data-foundation`);
  - `AGENTS.md` (`feature/openapi-contract`).

  Open: the README problem statement, the `product-spec.md` pointer, CI,
  and a license.
- **Planned deliverables (original).**
  - `AGENTS.md`, the project instructions for coding agents.
  - `docs/ai-workflow.md`, the AI-assisted workflow with representative
    prompts, review checkpoints, and verification.
  - A README problem statement and user story.
  - A root `product-spec.md` pointer.
  - A CI workflow running the non-live suite.
  - License choice.
- **Closes.** C1 (→2), C2 (→2), C11 (→1), C12 (→1), and the structure items.
- **Why here.**
  - It is cheap, carries no product risk, and every later stage benefits.
  - CI verifies each change automatically.
  - `AGENTS.md` makes later AI-assisted work more consistent.
- **Needs owner decisions.** Create a GitHub remote and push. That is an
  outward-facing action, so it needs explicit approval.

## Stage 2 — Milestone and approval domain model (implemented on a branch, unreleased)

- **Status.** Implemented on `feature/product-data-foundation`; not released
  or merged. It includes:
  - project, run, milestone, approval-request, and approval-decision
    records;
  - project-defined milestone policies;
  - repository interfaces with an in-memory implementation;
  - engine emission at workflow boundaries;
  - tests.

  See [`docs/product-domain.md`](../product-domain.md). Differences from the
  plan below:
  - **Derived from committed checkpoints, not from the execution log.**
    This avoids phantom milestones after a crash and leaves the log
    untouched.
  - **Period summary deferred.** The owner's instruction was not to build
    overnight-summary generation yet. The records and time-range queries it
    needs exist and are tested. Generation moves to Stage 9.
  - **No generic event model.** The reasons are in
    [`docs/product-domain.md`](../product-domain.md#why-there-is-no-generic-event-entity).
- **Planned deliverables (original).**
  - A milestone model (FR-025), derived from the existing execution log,
    covering plan completed, tests failed, repair completed, tests passing,
    review completed, approval required/received, blocked, and completed.
  - Richer approval records, ready for identity and risk/context (FR-018,
    FR-022).
  - A period summary function for overnight reports (FR-026).
  - All of it tested offline with the scripted provider.
- **Closes.** No rubric points directly. It is the prerequisite for C4–C7,
  and it closes the product's biggest gap: milestones vs. logs.
- **Why here.** Every user-facing feature reads these records:
  - dashboard;
  - mobile approval;
  - overnight summary;
  - notifications;
  - the API;
  - the database schema.

  It needs no credentials, network, money, or deployment, which matches the
  project's current safety posture.

## Stage 3 — API contract (designed on a branch, unreleased; implemented locally by Stage 5a)

- **Status.** `openapi.yaml` (OpenAPI 3.1) is on `feature/openapi-contract`,
  with contract tests and [`docs/api-contract.md`](../api-contract.md). How it
  differs from the plan below:
  - **Logs are not in v1.** No screen needs them yet; they need a read model.
  - **Authentication was declared first and implemented later** (Stage 5c,
    E3): the bearer scheme is enforced, and every operation names its
    permission (`x-permission`).
  - **Starting runs is left out.** It needs a queued-run concept the model
    lacks; that is the owner's decision.
  - **Approvals are distributed.** The API records decisions and workers
    apply them after re-checking the exact action.
- **Planned deliverables (original).** `openapi.yaml` for projects, runs, milestones, logs,
  approvals (view and decide), and authentication, written from what the
  dashboard and approval screens need.
- **Closes.** C5.
- **Why here.**
  - The course teaches the contract as "the source of truth" between
    frontend and backend.
  - It can only be written once Stage 2 defines what a milestone and an
    approval *are*.
  - It lets frontend and backend be built in parallel.

## Stage 4 — Storage: database behind an interface (implemented on a branch, unreleased)

- **Status.** Implemented on `feature/postgres-persistence`; not released or merged.
  This stage came before Stage 3, at the owner's direction; it depends only
  on Stage 2's records.
  - **PostgreSQL storage:**
    - schema migrations;
    - PostgreSQL implementations of Stage 2's repository interfaces;
    - transactions and reconciliation with the engine's checkpoints;
    - explicit configuration for development and test;
    - integration tests against a real database.
  - **Not yet:** production configuration (a secret manager), connection
    pooling, and users. Users arrive with authentication in Stage 5.
  - **Other storage:** file checkpoints remain the engine's own storage,
    and pgvector is not used.
  - Details are in [`docs/database.md`](../database.md).
- **Planned deliverables (original).**
  - Database-backed storage for runs, milestones, approvals, and users,
    behind a storage interface. File checkpoints can remain an
    implementation of it.
  - The interface now exists (Stage 2's repositories). This stage
    implements it on a database and runs the existing repository tests
    against that implementation.
  - Per-environment configuration and documentation.
- **Closes.** C7.
- **Why here.**
  - The API needs queries across many runs (FR-030), which per-run files
    don't serve well.
  - The schema follows from the Stage 2 records.
  - The engine keeps working unchanged behind the interface.

## Stage 5 — Backend service and authentication

- **Status.** Split into parts by the owner's milestone plan.
  - **5a — local backend: implemented on `feature/backend-api`,
    unreleased.** All 11 contract operations on PostgreSQL, with a bounded
    connection pool, durable idempotency, signed cursors, recorded (not
    executed) approval decisions, RFC 9457 errors, request IDs, and
    liveness/readiness checks. It has HTTP integration and runtime
    conformance tests. It binds to 127.0.0.1 and refuses public addresses
    ([backend.md](../backend.md)).
  - **5b — durable action execution (stages E1–E2, including the
    PostgreSQL ledger): implemented on `feature/auth-worker`, unreleased.**
    Approval is no longer treated as execution. Every approved action runs
    through an execution ledger (PostgreSQL, or a per-run checkpoint file)
    with write-ahead dispatch and replay of recorded results. A new run
    status, `awaiting_reconciliation`, covers actions whose outcome is
    unknown ([design](../design/action-execution.md),
    [safety.md](../safety.md#durable-execution-approval-is-not-execution)).
    The worker will build on it, so that applying a decision never re-runs
    an action already recorded as done.
  - **5c — authentication and authorization (stage E3): implemented on
    `feature/auth-worker`, unreleased.** The API verifies OpenID Connect
    access tokens as an OAuth 2.0 resource server, maps issuer and subject
    to enrolled principals, and requires one permission per operation;
    approval decisions record the authenticated principal
    ([authentication.md](../authentication.md)). Development and test
    authentication cannot be active in production.
  - **5d — worker coordination (stage E4): implemented on
    `feature/auth-worker`, unreleased.**
    - Worker processes claim queued runs from PostgreSQL (`FOR UPDATE SKIP
      LOCKED`) and advance them under leases renewed by heartbeats.
    - Every write carries a fencing token that the database checks, so a
      stalled worker cannot change a run after a takeover.
    - Graceful shutdown, crash takeover, and a real database restart are
      tested ([workers.md](../workers.md)).
    - Workers share one host.
  - **5e — approval and reconciliation consumption (stage E5): implemented
    on `feature/auth-worker`, unreleased.**
    - Workers apply recorded decisions under the run's claim, after
      re-checking the exact action, the expiry, and the decider; recording a
      decision queues the run in the same transaction.
    - Approvals expire after 12 hours; rejections and expiries are real
      outcomes the graph follows.
    - A reconciliation resource (`actions:reconcile`) with a new 15-minute
      authorization for a retry; an execution timeline in approval views; an
      attention inbox for the phone's first screen.
    - Operator commands never race workers ([workers.md](../workers.md#operator-commands)).
  - **Not yet:**
    - a chosen and configured production identity provider, and browser or
      phone sign-in (with the frontend, Stage 6);
    - starting runs through the API. A worker queue now exists, filled by
      the local `worker enqueue` command, but the contract has no run
      request.
- **Delivers.**
  - A service implementing the contract and wrapping the engine.
  - Authentication (FR-021, NFR-008).
  - Exact-action decisions exposed safely: bound to one request, decided
    once.
  - Unit and integration tests.
- **Closes.** C6, and part of C9.
- **Why here.**
  - It needs the contract (Stage 3) and storage (Stage 4).
  - Authentication must exist before any approval interface leaves the
    local machine.

## Stage 6 — Frontend: dashboard and mobile-first approval

- **Delivers.**
  - Milestones-first dashboard and log view (FR-029).
  - Mobile approval screen showing the exact action, project, agent,
    reason, and risk (FR-022, FR-023).
  - One API client module, plus tests.
- **Closes.** C4, and part of C9.
- **Why here.** It needs the contract; it can start against mocks in Stage 3
  but needs the backend to be real.

## Stage 7 — Containerized full system

- **Delivers.** Docker Compose for backend, frontend, and database, with
  one-command start instructions.
- **Closes.** C8, and C3 (→2) together with the architecture update.
- **Why here.**
  - There must be something to containerize first.
  - **Decision needed:** how the orchestrator reaches a Docker engine to
    start sandboxes. Mounting the engine socket gives root-equivalent host
    control. The alternatives are a host-side runner, or keeping the
    orchestrator on the host.

## Stage 8 — Deployment and CD

- **Delivers.**
  - A cloud deployment with a working URL.
  - CD on green, with a manual approval step.
  - A deployment guide.
  - Operational diagnosis output.
- **Closes.** C10, C11 (→2), C14 (→2), and part of C13.
- **Why here.**
  - A deployment must not expose an unauthenticated approval interface,
    which is why authentication comes in Stage 5.
  - **Decisions needed:** cloud provider (AWS is one candidate), budget, and
    credentials. Spending money and credential changes need explicit
    approval.

## Stage 9 — Notifications and overnight summaries

- **Delivers.**
  - Mobile-deliverable notifications (SMS or equivalent) containing a
    secure link, never a reply-to-approve (FR-019, FR-020).
  - Attention notifications for blocked, failed, or completed runs
    (FR-024).
  - A morning summary (FR-026), including the period-summary function
    deferred from Stage 2.
- **Closes.** No new rubric points. It completes the product vision.
- **Status.** Phase 1 is implemented on `feature/notifications-v1`
  (unreleased):
  - a transactional outbox for approval and reconciliation attention;
  - a dispatcher with deduplication, retries, leases, and fencing;
  - a fake provider, and delivery status in the dashboard.

  A real provider (SES email first, then SMS) waits for the owner's
  authorization. Notifications for blocked, failed, or completed runs
  (FR-024) and the morning summary (FR-026) are not built.
- **Why here.**
  - A link in a text message is useless until a deployed, authenticated
    approval page exists (Stages 5–8).
  - Notification services usually cost money, which needs approval.

## Track B — Real model provider (parallel, product-critical)

- **Delivers.**
  - One real provider implementing `ModelProvider` (FR-009).
  - A per-run cost budget (NFR-010).
  - Explicit, user-controlled secret handling (NFR-009).
- **Closes.** Not scored by the course. It is the product's core value.
- **Why parallel.**
  - It doesn't depend on the web stack, and the sandbox it needs already
    exists. Untrusted code is routed to Docker by default.
  - It needs owner decisions on provider, cost limits, and credentials.
- **Status.** The foundation is implemented on `feature/real-model-v1`
  (unreleased):
  - the provider comparison and a recommendation;
  - a Model Gateway with budgets, a usage ledger, context limits, a secret
    guard, versioned prompts, strict schemas, and capability manifests;
  - an Anthropic adapter tested offline;
  - an opt-in live suite.

  The first authorized live acceptance runs then passed with
  `claude-sonnet-5-5` (prompt version 2), within a $0.50 ceiling.

## Track C — Capabilities and cloud access (product, not scored directly)

- **Delivers.**
  - A provider-neutral Capability Registry: what the installation can
    actually do, with evidence and availability.
  - Tool packs.
  - An evidence-based capability assessment (the future marketplace
    boundary).
  - Typed cloud capabilities behind the Tool Gateway.
- **Status.**
  - Phase A (design) and Stages B to D are implemented on
    `feature/capability-registry-aws` (unreleased): the registry, the
    Core/Git packs, and an AWS pack with a **fake backend only**
    ([`docs/capabilities.md`](../capabilities.md),
    [`docs/aws-capabilities.md`](../aws-capabilities.md)).
  - No AWS call has been made.
  - Stage E1 (offline) added:
    - the `cloud_read` category, contract-first;
    - a real AWS backend (STS GetCallerIdentity only), explicitly activated,
      with no default credential chain;
    - the live-evidence mechanism.
- **Next (owner decisions).**
  - Stage E2, the first live identity check, is prepared but not done
    ([`docs/aws-e2-setup.md`](../aws-e2-setup.md)). It needs:
    - the owner to decide the Free-plan question (an AWS Organization is
      required for permission sets) and set up IAM Identity Center and an
      `agent-factory-dev` profile;
    - an opt-in live-verification runner, built and tested offline (E2a:
      done 2026-10-07, never run live);
    - the owner's authorization of the call through Agent Factory (E2b).
- **Rubric.** No score change: these are product features, not the course's
  web-stack layers or a coding-agent extension pack.

## Ongoing — security and DevOps hardening (C13)

These are added as their prerequisites appear:
- deterministic scan in CI (Stage 1+);
- a captured adversarial-suite report (any time);
- an AI tool/data policy (Stage 1);
- PR audit output (once pull requests exist);
- operational diagnosis (Stage 8).

## Optional — agent extension pack (C12, conditional on Module 5)

An MCP server that lets a coding agent read runs, milestones, and pending
approvals, plus skills, hooks, and permission notes. It comes after Stage 5,
because it should call the real API rather than reach into the engine.

## Recommended next milestone

Stages 2, 4, and 3 are on unreleased branches, in that order: the domain
model, PostgreSQL storage, and the API contract. Stage 5a (the local
backend), 5b (durable action execution, E1–E2), 5c (authentication and
authorization, E3), 5d (worker coordination, E4), and 5e (approval and
reconciliation consumption, E5) followed. Each awaits the owner's review.

The backend's human-in-the-loop workflow is now complete locally. What
remains before a frontend is useful beyond the local machine:
- choosing and configuring a production identity provider (an owner
  decision: it needs an account and credentials), before the API is
  reachable from anywhere but the local machine;
- the frontend itself (Stage 6), which the contract already serves: the
  attention inbox, the approval view with its timeline, and the two
  decision operations.

**Decision for the owner:** whether runs should be startable from the API.
Workers can now take queued runs; the API would need a run-request
resource, and a decision on whether to report queued runs with a distinct
status (today a queued run reports `running`, "in progress").

Stage 1's CI item still waits on the remote decision. The choice of the next
milestone, and of a version number, is the owner's.
