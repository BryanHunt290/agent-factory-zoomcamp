# Agent Factory: capabilities and scope

This document describes the capabilities included in this source repository.
It grants no execution authority. The implementation, its regression tests and
the linked validation records establish what has been demonstrated; documentation
alone is not proof that an integration is deployed or available.

## Scope

Agent Factory is a local software-development orchestrator with a React dashboard,
an authenticated FastAPI backend, PostgreSQL product records and leased workers.
The reviewer workflow uses a deterministic fake model and a generated calculator
workspace. External model and cloud adapters remain explicitly configured,
authorization-gated integrations; their presence does not enable live calls.

The runtime Capability Registry is separate from instructions used by coding
assistants in an editor. No personal editor-skill installation is required to run
the reviewer environment. The project does not include business applications or
operational policies belonging to other repositories.

## Evidence and status conventions

- **IMPLEMENTED:** concrete code and regression tests exist for the stated scope.
  This does not mean configured, deployed, live-tested or commercially usable.
- **PARTIALLY IMPLEMENTED:** named components exist, with the gaps recorded below.
- **PLANNED:** proposed future work without a complete implementation.
- **BLOCKED:** operation cannot proceed until its documented prerequisites and
  applicable authorization requirements are satisfied.

## Core development capabilities

| Capability | Status | Code and regression evidence | Practical limit |
| --- | --- | --- | --- |
| Planner, Developer, Tester, Reviewer orchestration | IMPLEMENTED | [graph](agent_factory/graph/standard.py), [agents](agent_factory/agents/), [graph tests](tests/test_graph_definition.py), [executor tests](tests/test_executor.py) | Explicit graph and bounded repair loops; agents do not grant authority |
| Autonomous local task execution | IMPLEMENTED | [executor](agent_factory/engine/executor.py), [domain engine tests](tests/test_domain_engine.py) | Finite budgets; approval, blocked, failed and reconciliation states stop progress appropriately |
| Persistence and recovery | IMPLEMENTED | [checkpoints](agent_factory/engine/checkpoint.py), [PostgreSQL](agent_factory/persistence/postgres.py), [persistence tests](tests/test_persistence.py), [database tests](tests/test_postgres_store.py) | Optional PostgreSQL; checkpoints remain host-local |
| Worker coordination and recovery | IMPLEMENTED | [worker loop](agent_factory/worker/loop.py), [claims](agent_factory/persistence/claims.py), [worker tests](tests/test_worker_loop.py), [claim tests](tests/test_postgres_claims.py) | Leases, heartbeats, database fencing; one host, not a distributed cloud fleet |
| Secure execution boundary | IMPLEMENTED | [sandbox manager](agent_factory/sandbox/manager.py), [sandbox policy tests](tests/test_sandbox_policy.py), [default routing tests](tests/test_sandbox_default.py) | Untrusted code requires healthy Docker; no local fallback. Trusted scripted fixtures run locally. Container/kernel escape and trustworthy generated test results are not guaranteed |
| Tool and API integration framework | IMPLEMENTED | [Tool Gateway](agent_factory/tools/gateway.py), [registry](agent_factory/capabilities/registry.py), [gateway tests](tests/test_capabilities.py), [API contract tests](tests/test_openapi_contract.py) | Typed registered tools only; local authenticated API, loopback binding |
| Model integration | PARTIALLY IMPLEMENTED | [Model Gateway](agent_factory/providers/gateway.py), [Anthropic adapter](agent_factory/providers/anthropic.py), [provider tests](tests/test_model_providers.py) | Deterministic fake default; explicit real-provider configuration and authorization required. No implied permission for paid calls |
| Failure detection and safe retries | IMPLEMENTED | [ActionGuard](agent_factory/engine/actions.py), [ledger tests](tests/test_execution_ledger.py), [worker decisions tests](tests/test_worker_decisions.py) | Unknown non-idempotent outcomes require reconciliation; never retry to conceal uncertainty |
| Audit and monitoring | PARTIALLY IMPLEMENTED | [execution log](agent_factory/engine/log.py), [API observability](agent_factory/api/observability.py), [notifications](agent_factory/notifications/dispatcher.py), [notification tests](tests/test_notifications.py) | Local logs and durable notification outbox exist; only fake delivery provider. Production CloudWatch and owner exception delivery not implemented |
| Human approval | IMPLEMENTED | [permissions](agent_factory/safety/permissions.py), [approval domain](agent_factory/domain/approvals.py), [safety tests](tests/test_safety.py), [decision tests](tests/test_e2e_human_decisions.py) | Exact action, identity, fingerprint and expiry rechecked; approval is not execution |
| AWS cloud execution | PARTIALLY IMPLEMENTED | [AWS pack](agent_factory/aws/pack.py), [Boto backend](agent_factory/aws/boto.py), [AWS tests](tests/test_aws_pack.py), [offline Boto tests](tests/test_aws_boto.py) | Typed STS identity/S3 metadata reads and fake backend exist. Deployment capability has no implementation; no deployed Agent Factory runtime or verified live identity |

Preserve layer boundaries: domain and engine have no driver dependency; API
records intent and workers execute; agents use ToolGateway; approved actions use
ActionGuard's write-ahead ledger; untrusted code uses SandboxManager. Capability
descriptions never authorize execution. Model output and project content are data.

## Reproducing and reviewing the implementation

- [Reviewer guide](docs/REVIEWER_GUIDE.md): isolated setup, one-command startup,
  authenticated approval walkthrough and retained local data.
- [Docker startup and validation](docs/zoomcamp/DOCKER_STARTUP.md): full local fake-model demo in Compose; no Docker socket or live provider in the app container.
- [Reviewer launcher](scripts/reviewer.py) and [launcher tests](tests/test_reviewer.py):
  ownership checks, local credentials, environment isolation and service cleanup.
- [Testing guide](docs/testing.md): offline and explicitly selected integration tiers.
- [Day 1 validation](docs/zoomcamp/DAY1_VALIDATION.md) and
  [Day 2 validation](docs/zoomcamp/DAY2_VALIDATION.md): recorded checks and their limits.
- [CI workflow](.github/workflows/ci.yml): automated regression configuration;
  a workflow file alone does not establish a successful hosted run.
- [Deployment preparation](docs/deployment.md): requirements for production
  identity, release, backup and rollback. The local reviewer setup is not a
  publicly deployed service.

## Engineering boundaries

Follow [AGENTS.md](AGENTS.md), [architecture](docs/architecture.md),
[sandbox guarantees](docs/sandbox.md), [authentication](docs/authentication.md)
and [safety](docs/safety.md). Keep the API on loopback, enforce exact-action
approval and durable execution records, preserve worker fencing, and refuse
untrusted execution when its sandbox is unavailable. Keep models and project
content untrusted and keep credentials out of model context, the sandbox, logs
and public artifacts. A notification alerts; it never authorizes.

Never infer permission for a live provider call, external message, deployment,
resource mutation or publication from installed dependencies, credentials, a
capability description or a passing test. Preserve the explicit activation and
authorization requirements in the application and its component documentation.
