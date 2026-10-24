# Architecture

Agent Factory keeps its concerns in separate modules. Each depends only on
the ones listed after it:

| Concern | Module | Responsibility |
| --- | --- | --- |
| Dashboard (web UI) | `frontend/` | A mobile-first React app to observe, approve, and reconcile. One API client, types generated from `openapi.yaml`, same-origin proxy to the loopback API; holds no workflow state. Development sign-in only ([frontend.md](frontend.md)) |
| HTTP backend | `api/`, [`openapi.yaml`](../openapi.yaml) | FastAPI routes and schemas, application services, safe output views, request context. Implements the checked-in contract; loopback only ([backend.md](backend.md)) |
| Authentication and authorization | `api/auth/`, `domain/principals.py` | Verifies OpenID Connect access tokens (an OAuth 2.0 resource server), maps issuer and subject to an enrolled principal, and requires one permission per operation before any handler runs ([authentication.md](authentication.md)) |
| Graph execution | `engine/executor.py` | Walks the graph, runs agents, applies results, enforces budgets, checkpoints, logs. Holds the run lock (`engine/locks.py`) while it changes a run; under a worker's claim (`engine/ownership.py`), proves ownership before every checkpoint, outcome, and dispatch |
| Workers | `worker/`, `domain/claims.py`, `persistence/claims.py` | Claim queued runs from PostgreSQL (`FOR UPDATE SKIP LOCKED`), hold them under a lease renewed by heartbeats, fence every write with the run's fencing token, release them. One run per process; one host ([workers.md](workers.md)) |
| Attention notifications | `notifications/`, `domain/notifications.py`, `domain/notification_repositories.py`, `persistence/notifications.py` | A transactional outbox of notification intents, written with each attention event; a separate dispatcher process sends them through a provider adapter (only a fake exists), at least once, with deduplication, bounded retries, and fencing. Alerts only; authorizes nothing ([notifications.md](notifications.md)) |
| Durable execution | `engine/actions.py`, `engine/execution_store.py`, `domain/executions.py` | The only path by which an approved action runs: write-ahead dispatch, replay of recorded outcomes, recovery, reconciliation. Ledger in the product store, or in a per-run file without a database ([design](design/action-execution.md)) |
| Durable storage | `persistence/` | PostgreSQL implementation of the product repositories: schema migrations, explicit configuration, row-to-domain mapping. Optional; nothing in the engine or domain imports it ([database.md](database.md)) |
| Product domain | `domain/` | Projects, runs, milestones, and approval records for users rather than for execution: typed, versioned, behind repository interfaces ([product-domain.md](product-domain.md)) |
| Agents | `agents/` | Turn a state snapshot into an outcome and updates to the fields they own |
| Capability Registry | `capabilities/`, `tools/packs.py`, `aws/` | What this installation can actually do: typed, immutable capability definitions registered by tool packs (Core, Git, and AWS: a fake backend by default and a real `BotoAwsBackend` used only when explicitly authorized, tested offline), with evidence (implemented, tested, live-tested from a reviewed evidence file only, available now, approval) and availability probes; agent-scoped views; evidence-based assessment; the `cloud_read` category with per-environment policy. Describes; never authorizes ([capabilities.md](capabilities.md), [aws-capabilities.md](aws-capabilities.md)) |
| Tools | `tools/` | Single actions (read/write/delete file, run tests, git commit), each with an action category and declared execution semantics (read-only, idempotent, or non-idempotent), registered through tool packs |
| Safety/permissions | `safety/`, `tools/gateway.py` | Decide allow / require-approval / deny for every tool call |
| Execution sandbox | `sandbox/` | Run project code at the isolation level its trust requires: untrusted code only in the Docker sandbox, trusted fixtures locally; block otherwise ([sandbox.md](sandbox.md)) |
| Model providers | `providers/`, `agents/prompts.py` | Agents reach a model only through the `ModelGateway`: minimal context (`ContextBuilder`), a secret guard, budgets and a restart-safe usage ledger, bounded retries, strict schema validation, audit. `ModelProvider.complete(ModelRequest) -> ModelResponse`: the deterministic `FakeProvider` (default) or the explicitly activated `AnthropicProvider`. The model is untrusted input ([model-providers.md](model-providers.md)) |
| Project state | `state.py`, `engine/checkpoint.py`, `engine/log.py` | Structured run state, atomic checkpoints, append-only log |
| Graph definition | `graph/` | Immutable, validated nodes and outcome-labelled edges |

Project-specific pieces (task, scaffold, test command, graph, agents, policy,
milestone policy) live in a `ProjectTemplate` (`templates/`).
`factory.build_executor` wires a template, a provider, and a workspace into
an `Executor`. It builds a `CapabilityRegistry` from the local Core and Git
packs (plus any packs the caller enables explicitly) and takes the tools from
it. With the registry, the Tool Gateway also refuses a tool with no registered,
available capability, and lets a capability's rules only tighten its decision
([capabilities.md](capabilities.md)).

Given a `product_store`, `build_executor` also attaches a
`ProgressRecorder`:

- After every checkpoint, the recorder derives product records from the
  committed state, in one transaction.
- It validates and stores approval decisions before the engine acts on
  them, and wakes the run in the same transaction.
- It re-checks a stored decision or reconciliation answer before a worker
  applies it (`consumable_decision`, `reconciliation_decision`).
- `Executor.reconcile(run_id)` brings the two back into agreement after a
  failure.

The store is either the in-memory one (tests) or PostgreSQL, which the CLI
uses when given `--database`. Without a store, the executor behaves as in
v0.2. The execution log is the same either way.

```
TRUSTED ORCHESTRATOR (engine, recorder, repositories) -----> PostgreSQL (product records)
        |
        +--> SANDBOX (untrusted project code) ---X---> PostgreSQL: no network, no credentials
```

**The product boundary** (the local API, workers, and Dashboard v1 exist;
production phone sign-in and deployment remain future work):

```
DASHBOARD (browser) ──same-origin HTTP──> frontend server (static files; proxies /api to loopback)
                                             │
                                             ▼
                         API: records intent (a decision, a reconciliation answer, a new project)
                              and wakes the run, in one transaction ──> PostgreSQL
                                                                          │ run_claims (the queue)
WORKER (engine) <── claims the run (FOR UPDATE SKIP LOCKED, fenced) ──────┘
   │ re-checks the decision against the exact pending action, its expiry, and its author
   │ applies it under the claim; approved actions only through the ActionExecution ledger
   └──> resumes the graph; untrusted code still runs only in the sandbox
```

The dashboard is a view over the API: it holds no workflow state, shows
nothing as decided before the API confirms it, and settles a decision whose
answer was lost by asking the API, never by resending on its own
([frontend.md](frontend.md), [dashboard-reliability.md](dashboard-reliability.md)).
The API and the workers never call each other. The API owns user intent,
the workers own execution, and PostgreSQL is the boundary: a transactional
outbox (the decision and the wake-up commit together) and polling, no
broker. A stored decision is applied only after the worker's re-checks
([workers.md](workers.md#human-decisions-wake-ups-and-consumption)). Long
agent work never runs inside an API request: a request returns as soon as
the intent is durable, and a phone may lose its connection at any time.

**Attention notifications** ([notifications.md](notifications.md)):

```
WORKER (recorder.sync) ── attention event + notification intent, one transaction ──> PostgreSQL
NOTIFICATION DISPATCHER ── claim (SKIP LOCKED, lease, token) ── attempt (write-ahead) ── provider ── result (fenced)
person ── opens the link ── signs in to the DASHBOARD ── decides there (the notification authorizes nothing)
```

Nothing that creates attention sends anything: graph nodes, API handlers,
and the dashboard never call a provider. Only the dispatcher does, and it
needs neither the browser, the API, nor a worker to be running.

**Workers and ownership** ([workers.md](workers.md)):

```
WORKER A ─┐   claim: one short transaction        run_claims: one row per run
WORKER B ─┼── FOR UPDATE SKIP LOCKED ──────────>  owner, lease (PostgreSQL time),
WORKER C ─┘   (+1 fencing token per claim)         fencing token (the generation)
   │
   └── advance the claimed run: every write in a fenced transaction (token current, lease valid);
       ownership proved before each checkpoint, outcome, and dispatch; heartbeats on their own session
```

A stale (zombie) worker's writes fail in the database, whatever it believes
about its lease. Checkpoints are files on the host's disk, so workers share
one host: the run lock keeps one writer per run on that host
([workers.md](workers.md#checkpoints-and-the-filesystem)).

The HTTP implementation follows this dependency chain:

```text
Uvicorn -> FastAPI routes -> API schemas -> application services
  -> domain records/rules -> repository interfaces -> PostgreSQL repositories
```

Only the server composition root constructs the PostgreSQL pool. Services
borrow a store for each unit of work; the engine has no HTTP knowledge.
The pool opens and closes with ASGI lifespan. No API request opens a
workspace, launches Docker or calls the executor. Project creation records
an ID that a later workspace can preserve through `workspace_for_project`.

Every product request is authenticated and authorized
([authentication.md](authentication.md)). The supported command binds only
to loopback; there is no public deployment. Infrastructure liveness/readiness endpoints
and JSON HTTP logs are separate from the product contract and the engine's
execution log. See [backend.md](backend.md) for the request lifecycle.

## Execution flow

```
Executor.start(task)                       Executor.resume(run_id)
   | create ProjectState, log run_started     | load checkpoint, log run_resumed
   v                                          v
 +------------------------------ loop ------------------------------+
 | node = graph.node(state.current_node)                            |
 | terminal?            -> status=completed, log, checkpoint, return |
 | step >= max_steps?   -> status=exhausted                         |
 | log node_entered                                                 |
 | agent.run(ctx)  ctx = {state snapshot, ToolGateway, provider}    |
 |    each tool call   -> policy check -> (approval?) -> run -> log action
 |      approved call  -> ActionGuard: replay a recorded outcome, or  |
 |                        write-ahead (ledger + fsync'd log), run, record outcome
 |    each model call  -> provider.complete -> schema check -> log action
 |    ApprovalRequired -> status=awaiting_approval, checkpoint, return
 |    ReconciliationRequired / uncertain action                     |
 |                     -> status=awaiting_reconciliation, checkpoint, return
 |    ledger or sandbox unavailable -> status=blocked, checkpoint, return
 |    any exception    -> status=failed, checkpoint, return         |
 | unused authorizations of this node visit lapse                   |
 | log node_result                                                  |
 | edge = graph.transition(node, outcome)                           |
 |    undeclared outcome -> InvalidTransitionError -> failed        |
 | apply updates, history, counters                                 |
 | edge/target budget exceeded -> status=exhausted                  |
 | log transition, checkpoint                                       |
 | (under a worker's claim: ownership proved before each checkpoint,|
 |  outcome, and dispatch; OwnershipLost stops the loop at once,    |
 |  writing nothing; should_yield -> run_yielded between nodes)     |
 +------------------------------------------------------------------+

 every checkpoint -> recorder.sync(state)   (only with a product store)
```

## Design rules

- **Graph logic knows nothing about models.** The executor only sees outcome
  strings and state updates. Swapping the fake provider for a real one changes
  no graph or executor code.
- **Agents own fields, not the state.** Each agent declares `writes` (for
  example `{"test_report"}`). The executor rejects updates to any other field.
  Routing fields (`current_node`, counters, approvals) belong to the executor
  alone.
- **Verdicts that can be computed are computed.** The tester's pass/fail comes
  from the real test process, not from a model. The reviewer refuses to approve
  when tests have not passed.
- **One choke point for side effects.** Agents never touch the filesystem
  directly. Every effect goes through `ToolGateway.call` (or
  `call_capability`, which resolves a capability ID and then takes the same
  path), which checks the capability's registration and availability, the
  policy, enforces approvals, and logs.
- **A capability describes; it never authorizes, and it is never a model
  tool.** The registry is built in code from tool packs; no project file,
  model answer, or environment variable adds or enables a capability. The
  model's only callable tool stays `submit_result`.
- **One choke point for API access.** Every product operation goes through
  `require_permission`: authenticate the token, find the enrolled principal,
  require the operation's permission. Identity never comes from the request
  body, and tokens never leave the API process: the engine, sandbox, tools,
  and agents never see them.
- **Checkpoint after every completed node.** The state on disk always describes
  a consistent point between nodes. An interrupted node is re-run on resume, so
  agents must be idempotent per visit (developer writes are full-file
  overwrites, so they are).
- **Re-running a node never repeats an approved side effect.** Approved
  actions run only through the `ActionGuard`, which records every dispatch
  durably before it happens and replays a recorded outcome to a re-run node.
  An unknown outcome of a non-idempotent action stops the run for a person
  instead of being retried. Approval is not execution.
- **Determinism.** The fake provider selects responses by `(agent, visit)`, both
  of which are persisted, so a resumed run receives the same answers as an
  uninterrupted one. Approval request ids are hashes of
  `(run, node, visit, tool, params)` for the same reason.

## Run statuses

| Status | Meaning | Resumable |
| --- | --- | --- |
| `running` | In progress: being advanced, queued for a worker, or interrupted mid-node | yes |
| `awaiting_approval` | Paused on a gated action | after a decision (API, `approve`) or the approval's expiry, applied by a worker or `resume` |
| `awaiting_reconciliation` | An approved side effect may already have happened; a person must say whether it did | after an answer (API `reconcileAction`, CLI `reconcile-action`), applied by a worker or `resume` |
| `blocked` | Code could not be executed safely (no qualifying sandbox, unhealthy backend, unverified cleanup), or the record of approved actions is unavailable; no retry consumed | yes, once the sandbox or the ledger is back |
| `completed` | Reached a terminal node | no |
| `failed` | Agent/provider/tool error, policy denial, invalid transition | no |
| `exhausted` | A retry budget, node visit limit, or `max_steps` was hit | no |
