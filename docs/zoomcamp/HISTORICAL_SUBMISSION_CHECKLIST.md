# Submission checklist — AI Dev Tools Zoomcamp final project

This checklist is driven strictly by the official rubric in
[OFFICIAL_REQUIREMENTS.md](OFFICIAL_REQUIREMENTS.md) (draft, `@49b7a73`).
`C#` refers to scored criterion #. Statuses come from
[RUBRIC_MAPPING.md](RUBRIC_MAPPING.md).

Rules:
- Only items that are truly finished are ticked.
- Every item gets an **Evidence** link when it is done.
- Partial work is listed as what remains, not ticked.

## Already complete

| ✓ | Item | Rubric | Evidence |
| --- | --- | --- | --- |
| [x] | `README.md` present | Structure | [`README.md`](../../README.md) |
| [x] | `docs/` present | Structure | [`docs/`](../) |
| [x] | README describes system functionality and expected behaviour (the problem statement is still missing; see below) | C1 (part) | [`README.md`](../../README.md) |
| [x] | Main components and their roles documented | C3 (part) | [`docs/architecture.md`](../architecture.md) |
| [x] | Product specification written, at `docs/zoomcamp/`; the root-path item is below | Structure (part) | [PRODUCT_SPEC.md](PRODUCT_SPEC.md) |
| [x] | AI assistance disclosed: `Co-Authored-By` trailers on the first 13 commits; for later commits (no automatic trailers, at the owner's instruction) in the workflow document | C2 (part) | `git log`; [`docs/ai-workflow.md`](../ai-workflow.md) |
| [x] | AI workflow documented: roles, the review loop, task delegation, manual review, and verification. Prompts are quoted only representatively. On the feature branches. | C2 | [`docs/ai-workflow.md`](../ai-workflow.md) |
| [x] | `AGENTS.md`: real project instructions for coding agents (boundaries, commands, security invariants, frozen refs, prohibited shortcuts), on branch `feature/openapi-contract` | C2, C12 (level 1), Structure | [`AGENTS.md`](../../AGENTS.md) |
| [x] | Contract-first `openapi.yaml` (OpenAPI 3.1) designed from the dashboard and mobile-approval screens; validated against the official schema; 35 contract tests (branch `feature/openapi-contract`, unreleased) | C5 (part), Structure | [`openapi.yaml`](../../openapi.yaml), [`docs/api-contract.md`](../api-contract.md), `tests/test_openapi_contract.py` |
| [x] | Factual description of how AI tools were used | C2 (part) | [LEARNING_NOTES.md](LEARNING_NOTES.md#ai-assisted-development-how-agent-factory-was-built) |
| [x] | File-based persistent storage, documented and tested (not a database) | C7 (part) | [`docs/graph-and-state.md`](../graph-and-state.md), `tests/test_persistence.py` |
| [x] | PostgreSQL database layer: versioned migrations, keys and constraints, repositories, transactions, reconciliation, `--database` CLI integration (branch `feature/postgres-persistence`, unreleased) | C7 (part) | [`docs/database.md`](../database.md), [`agent_factory/persistence/`](../../agent_factory/persistence/) |
| [x] | Database integration tests against a real PostgreSQL, separated by marker and opt-in flag, and documented | C9 (part) | `tests/test_postgres_*.py`, [`docs/testing.md`](../testing.md#postgresql-integration-tests) |
| [x] | Development and test database configuration: separate databases and variables, an example file, a least-privilege role | C7 (part) | [`config/database.example.env`](../../config/database.example.env), [`docs/database.md`](../database.md#configuration) |
| [x] | Live integration suites separated by marker and opt-in flag, and documented | C9 (part) | [`docs/testing.md`](../testing.md), [`tests/conftest.py`](../../tests/conftest.py) |
| [x] | Security design, threat model, and sandbox guarantees and limitations documented. This counts toward C13 only if a reviewer accepts self-authored artifacts. | C13 (input) | [`docs/sandbox.md`](../sandbox.md), [`docs/design/v0.2-sandbox.md`](../design/v0.2-sandbox.md) |
| [x] | Setup, run, and test instructions for the current system | C14 (part) | [`README.md`](../../README.md), [`docs/testing.md`](../testing.md) |
| [x] | A backend that implements `openapi.yaml`, with conformance tests against real responses (C5 level 2). Branch `feature/backend-api`, unreleased; local only. | C5, C6 (part) | [`agent_factory/api/`](../../agent_factory/api/), [`docs/backend.md`](../backend.md), `tests/test_api_conformance.py` |
| [x] | Backend tests of core functionality over HTTP, against the in-memory store and real PostgreSQL | C6 (part), C9 (part) | `tests/test_api_http.py`, [`docs/testing.md`](../testing.md#http-api-tests-unreleased) |
| [x] | Connection pool for the backend (bounded, explicit start-up and shutdown, safe 503 when exhausted) | C6, C7 (part) | [`agent_factory/persistence/pool.py`](../../agent_factory/persistence/pool.py) |
| [x] | Backend run instructions (local development only) | C6, C14 (part) | [`docs/backend.md`](../backend.md#start-locally) |
| [x] | Authentication and authorization in the API: OpenID Connect token verification, principals and per-operation permissions in PostgreSQL, approval decisions recording the authenticated principal, development authentication refused in production (branch `feature/auth-worker`, unreleased; no production identity provider configured) | C6 (part), C9 (part), C13 (input) | [`docs/authentication.md`](../authentication.md), `tests/test_auth_*.py` |
| [x] | Durable execution of approved actions: an execution ledger (PostgreSQL migration `0003`, or a per-run checkpoint file), write-ahead dispatch, replay protection, and reconciliation of uncertain outcomes, with crash tests that end real child processes (branch `feature/auth-worker`, unreleased) | C7 (part), C9 (part), C13 (input) | [`docs/design/action-execution.md`](../design/action-execution.md), [`docs/safety.md`](../safety.md#durable-execution-approval-is-not-execution), [`docs/testing.md`](../testing.md#durable-action-execution-tests-unreleased) |
| [x] | Worker coordination: run claims (PostgreSQL migration `0005`), bounded leases with heartbeats, fencing tokens checked in the same transaction as every write, `FOR UPDATE SKIP LOCKED` claiming, graceful shutdown, crash takeover; race tests on real PostgreSQL, real-process zombie and death tests, and a real database restart (branch `feature/auth-worker`, unreleased; one host) | C6 (part), C7 (part), C9 (part), C13 (input) | [`docs/workers.md`](../workers.md), [`docs/testing.md`](../testing.md#worker-coordination-tests-unreleased), `tests/test_postgres_claims.py`, `tests/test_worker_processes.py` |
| [x] | Human-in-the-loop completion (E5): workers apply recorded decisions after re-checking the exact action, the 12-hour approval expiry, and the decider; recording a decision wakes the run (PostgreSQL outbox, polling); a reconciliation API (`actions:reconcile`) with a new 15-minute retry authorization; an execution timeline and an attention inbox, contract-first; migration `0006`; end-to-end HTTP-to-worker tests, 11 races and 10 real process deaths on PostgreSQL (branch `feature/auth-worker`, unreleased; no frontend) | C5 (part), C6 (part), C7 (part), C9 (part), C13 (input) | [`docs/workers.md`](../workers.md#human-decisions-wake-ups-and-consumption), [`docs/api-contract.md`](../api-contract.md#reconciliation-the-execution-timeline-and-the-attention-inbox), [`docs/testing.md`](../testing.md#human-decisions-tests-unreleased-e5), `tests/test_postgres_decisions.py`, `tests/test_e2e_human_decisions.py` |

| [x] | Capability Registry and tool packs: typed capability definitions with evidence and availability, agent views, evidence-based assessment; an AWS pack with a FAKE backend only (no AWS call), typed capabilities, credential slots that refuse long-term keys, and redacted results; 81 offline tests and 19 manual mutants (branch `feature/capability-registry-aws`, unreleased). Stage E1 added the `cloud_read` category contract-first (migration `0009`), a real AWS backend tested offline only (no implicit credential chain, no AWS call), and the live-evidence mechanism, with 25 more mutants. A product feature: no rubric score change | C13 (input only) | [`docs/capabilities.md`](../capabilities.md), [`docs/aws-capabilities.md`](../aws-capabilities.md), `tests/test_capabilities.py`, `tests/test_aws_pack.py`, `tests/test_aws_boto.py` |

## Needs development

| ☐ | Item | Rubric | Evidence (when done) |
| --- | --- | --- | --- |
| [ ] | Backend completion: starting runs through the API (needs a run-request resource, and an owner decision). The local backend, the workers, and the decision workflow are done (above). | C6 | |
| [ ] | Frontend: functional, well structured, one API client module, tests of core logic | C4 | |
| [ ] | Production database configuration (secret manager, TLS, backups). The development and test configurations and the backend's connection pool are done (above). | C7, C10 | |
| [ ] | A configured production identity provider, and browser or phone sign-in, required before the API leaves 127.0.0.1 or any remote approval interface exists. Token verification and authorization are done (above). | C6, C10 (prerequisite) | |
| [ ] | Full system runs via Docker Compose. Includes a decision on how the orchestrator reaches Docker for the sandbox. | C8 | |
| [ ] | Integration tests for UI workflows, and all tiers run separately in CI (database and API workflows are done, above) | C9 | |
| [ ] | CI: tests run automatically on every push or pull request | C11 (level 1) | |
| [ ] | CD: deploys when tests pass, ideally with a manual approval step | C11 (level 2) | |
| [ ] | Agent extension pack: reusable workflow or skill, subagent, MCP tool/server, hook or guardrail. Conditional on Module 5. | C12 | |
| [ ] | Deterministic security scan with findings and triage | C13 | |
| [ ] | PR audit output (needs a remote and pull requests) | C13 | |
| [ ] | A first real model run: the live suite (`--run-model-live`) under its hard budget. The model layer, budgets, and offline tests are done on `feature/real-model-v1`; an API key, a spend limit, network egress, and the spend need the owner's authorization ([model-providers.md](../model-providers.md#authorization-needed-for-the-first-live-call)). Product requirement, not a rubric item. | product (FR-009, AC-016) | |
| [ ] | A real notification provider (email through SES first, then SMS). The outbox, dispatcher, fake provider, and tests are done on `feature/notifications-v1`; an account, credentials, and a real send need the owner's authorization ([notifications.md](../notifications.md#real-providers)). Product requirement, not a rubric item. | product (FR-019) | |

## Needs documentation

| ☐ | Item | Rubric | Evidence (when done) |
| --- | --- | --- | --- |
| [ ] | README problem statement and user story | C1 | |
| [ ] | Full representative prompts or stage instructions in `docs/ai-workflow.md`, if the owner chooses to publish them (optional; C2 is met without them) | C2 | |
| [ ] | Architecture covering frontend, backend, database, containerization, and CI/CD, with a diagram | C3 | |
| [ ] | `product-spec.md` at the repository root, or a pointer to the specification | Structure | |
| [ ] | Frontend run and test instructions (backend: done, above) | C4 | |
| [ ] | Production database configuration documented, with the deployment (development and test are documented) | C7 | |
| [ ] | Test-tier table updated with the UI integration tests (the API tests are documented in `docs/testing.md`) | C9 | |
| [ ] | Deployment guide | C10, C14 | |
| [ ] | AI tool/data policy: which data may go to which AI tools | C13 | |
| [ ] | Agent and extension security notes for the extension pack | C13 | |
| [ ] | `docs/agent-extension-pack.md` and `docs/permissions.md`. Conditional on Module 5. | C12 | |
| [ ] | End-to-end setup → run → test → deploy instructions, verified on a clean machine or in CI | C14 | |

## Needs deployment

| ☐ | Item | Rubric | Evidence (when done) |
| --- | --- | --- | --- |
| [ ] | Owner decision: create a GitHub remote. Needed for CI, PR audits, and submission. | C11, C13 | |
| [ ] | Owner decision: cloud target, budget, and credentials. Spending money and credentials need explicit approval. | C10 | |
| [ ] | Authentication live before public exposure: a configured identity provider, then a deployment design (the API's verification and authorization exist) | C10 (prerequisite) | |
| [ ] | Application deployed with a working URL | C10 | |
| [ ] | CD deploying on green | C11 | |
| [ ] | Operational diagnosis output from the running deployment | C13 | |

## Needs submission evidence

| ☐ | Item | Rubric | Evidence (when done) |
| --- | --- | --- | --- |
| [ ] | Re-check the official requirements (currently draft) and update [OFFICIAL_REQUIREMENTS.md](OFFICIAL_REQUIREMENTS.md) | All | |
| [ ] | Frontend screenshots or video, including the mobile approval flow | C4 | |
| [ ] | Links to CI runs | C11 | |
| [ ] | Deployment URL or proof | C10 | |
| [ ] | `security/` artifacts committed: scan output, adversarial-suite report, PR audit | C13 | |
| [ ] | `ops/` artifacts committed: operational diagnosis | C13 | |
| [ ] | Final self-check of every criterion, with evidence links | All | |
| [ ] | Peer reviews completed (rules not yet published) | Process | |
| [ ] | Project submitted through the official channel (not yet published) | Process | |
