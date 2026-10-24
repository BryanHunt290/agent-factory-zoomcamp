# Safety model

The default posture is **deny unless explicitly allowed**. Dangerous actions
always need a human.

## Action categories

Every tool declares one `ActionCategory`:

| Category | Default | Notes |
| --- | --- | --- |
| `read_workspace` | allow | confined to `project/` |
| `write_workspace` | allow | confined to `project/`; protected paths refused |
| `execute_local` | allow | reserved; no tool uses it in v0.2 |
| `execute_sandboxed` | allow | `run_tests` only; isolation is chosen and enforced by `SandboxManager` ([sandbox.md](sandbox.md)) |
| `destructive_delete` | **require approval** | |
| `spend_money` | **require approval** | |
| `production_deploy` | **require approval** | |
| `credential_change` | **require approval** | |
| `external_communication` | **require approval** | |
| `network_access` | **deny** | Agent Factory makes no network calls; sandboxed project code runs with networking disabled |
| *(any unlisted category)* | deny | |

**Hard floor:** `PermissionPolicy` raises `PolicyError` if any dangerous
category (the last six rows) is set to `allow`. A template can tighten a rule
to `deny` but can never loosen one past `require_approval`.

## Per-agent tool allowlists

Before a category rule is consulted, the agent must have the tool on its
allowlist. Anything else is denied.

| Agent | Tools |
| --- | --- |
| planner | `list_files`, `read_file` |
| developer | `list_files`, `read_file`, `write_file`, `delete_file`, `git_commit` |
| tester | `list_files`, `read_file`, `run_tests` |
| reviewer | `list_files`, `read_file` |

## The gateway

`ToolGateway.call(tool, **params)` is the only route from an agent to a side
effect:

1. Unknown tool, or a tool not on the agent's allowlist: log `denied`, raise
   `PermissionDenied`.
2. Category rule is `deny`: log `denied`, raise `PermissionDenied`.
3. Category rule is `require_approval`: compute
   `request_id = hash(run, node, visit, tool, params)`.
   - No decision recorded: raise `ApprovalRequired`. The executor pauses the
     run.
   - Rejected: log `denied`, raise `PermissionDenied`.
   - Approved: hand the call to the run's execution guard
     ([below](#durable-execution-approval-is-not-execution)). It runs the
     tool only after durably recording the dispatch, replays an outcome
     already recorded, or stops the run when an earlier attempt's outcome
     is uncertain. Without a guard, an approved action is refused.
4. Run the tool and log the result.

With a Capability Registry (every executor `build_executor` makes has one;
[capabilities.md](capabilities.md)), two more checks come before step 2:

- A tool that belongs to no registered capability, or whose capability is
  unavailable now, is denied without running.
- The capability's own rule can only make the decision stricter: `denied`
  becomes deny; `always`, or a mutation with unknown or recurring cost,
  becomes require-approval. A capability can never loosen the policy, and
  its definition is refused at registration if it claims a weaker category
  or execution class than its tool.

Approval is bound to the **exact** parameters (the content of long strings is
hashed). Approving `delete legacy_calc.py` does not approve deleting anything
else.

## Approval gate lifecycle

```
agent calls gated tool
  -> ApprovalRequired
  -> state.status = awaiting_approval, state.pending_approval = {...}
  -> log approval_requested + run_paused, checkpoint, executor returns

human: python -m agent_factory approve --workspace W --run-id R --request-id apr-... [--deny] [--note ...]
  -> Executor.decide_approval: records decision in state.approvals, logs approval_decision

human: python -m agent_factory resume --workspace W --run-id R
  -> node re-runs; the gateway finds the decision for the same request_id
```

`resume` refuses to continue while a decision is pending (`ApprovalPending`).
The developer agent requests deletions **before** writing anything, so the
*first* approval pause of a node visit happens before that node has changed
anything. A rejected deletion is skipped and recorded in
`implementation.deletions_rejected`.

**A node can pause more than once.** If it needs a second approval, the
first approved action has usually already completed when the node pauses
again, so a paused node *may* have made partial changes. Because a resumed
node re-runs from its start, that earlier action used to run a second time
(found in the design phase; [design](design/action-execution.md#12-evidence)).
Durable execution records now prevent this: the re-run node asks for the same
action, and the gateway **replays the recorded result instead of running it
again**.

**Product records (optional).** When the executor has a product store
([product-domain.md](product-domain.md#approvals)), each pending approval is
also stored as an `ApprovalRequest` carrying a fingerprint of the exact
action. `decide_approval` records an `ApprovalDecision` **before** the engine
changes state. The fingerprint is computed from the action the engine is
waiting on, so a decision can't transfer to a different action. Conflicting
decisions leave the run paused. The gateway check above is unchanged and
remains the enforcement point.

## Durable execution: approval is not execution

An approval proves that a person authorized one exact action. It says nothing
about whether the action ran. That is recorded separately, in an
**action execution** ([design](design/action-execution.md),
[product-domain.md](product-domain.md#action-executions)):

```
approved action requested
  -> ActionGuard: find or create the execution (ID from project, run, approval, fingerprint)
     succeeded?            -> replay the recorded result; never run it again
     failed / lapsed?      -> report it again (PermissionDenied or ToolError)
     uncertain?            -> ReconciliationRequired: the run awaits a person
     authorized?           -> write-ahead: commit "dispatching" (ledger), append action_dispatching
                              (fsync'd execution log); only then run the tool
  -> outcome to the execution log first, then to the ledger
```

**Execution semantics.** Every tool declares what may happen if an attempt
is repeated (`Tool.execution`):

| Class | Code | After an attempt whose outcome is unknown |
| --- | --- | --- |
| A | `READ_ONLY` | Retried automatically (bounded) |
| B | `IDEMPOTENT`, `NATURAL` or `KEYED` | Retried automatically. Keyed tools get the **same** idempotency key on every retry, and only while the first attempt is inside the provider's declared window; outside it, a person decides |
| C | `NON_IDEMPOTENT` | **Never resent automatically.** The execution becomes `uncertain` and the run `awaiting_reconciliation` |

An undeclared tool is treated as class C. A tool in a dangerous category
(money, deployment, credentials, deletion, external communication, network)
that declares nothing is **refused when it is registered**, and it can never
be declared read-only. The built-in tools:

| Tool | Class | Why |
| --- | --- | --- |
| `list_files`, `read_file` | A, read-only | Observe; change nothing. File content is not recorded for replay |
| `run_tests` | A, read-only | Observes the project. Nothing persists from the Docker sandbox (copy-in); the trusted local backend runs maintainer fixtures in the workspace, which may write caches |
| `write_file` | B, natural | Overwrites one file with given content: a repeat leaves the same file. Not harmless in general: replayed after a later write to the same path, it would revert it |
| `delete_file` | B, natural | Makes a path absent: a repeat reports "did not exist". Local and synchronous |
| `git_commit` | B, natural | A repeat on an unchanged tree commits nothing; after more changes it would commit again |

Only approval-gated actions go through the ledger in this version (today:
`delete_file`). The design keeps the path open for every side effect to flow
through it later.

**Reconciliation.** A person settles an uncertain action with one of three
answers: *it happened*, *it did not happen, do not retry*, or *it did not
happen, try once more*. The last is a **new** authorization valid for 15
minutes, bound to the same project, run, exact action, fingerprint, and the
attempt it follows; the original approval is never reactivated. The 15
minutes count from the person's decision, not from when a worker applies it.
A person answers over HTTP (`reconcileAction`, permission
`actions:reconcile`, which `approvals:decide` does not imply) or with the
local CLI (`reconcile-action`, recorded as an unverified local actor). Either
way the answer is recorded as intent; a worker holding the run's claim
re-checks it and applies it through the ledger
([workers.md](workers.md#reconciliation-answers)).

**Fail closed.** An approved action never runs without a durable record. If
the ledger cannot be read or written (a database outage, or a filesystem error
such as a full disk for the checkpoint ledger), the run is `blocked`
(resumable) and nothing new is dispatched. An unknown outcome of a class C action is never
guessed: the run waits. A dispatch must begin before its approval expires; an
expired, unused approval lapses and is never renewed. One process at a time
changes a run (an OS lock on `run.lock`).

## Workers: a stale worker cannot change a run

Several worker processes may share the database
([workers.md](workers.md)). Each run has at most one owner, under a lease
that heartbeats renew, and a fencing token that increases with every claim.
What that protects:

- **Every database write for a claimed run is fenced.** The write's own
  transaction first proves that the worker's token is current and its lease
  valid, by PostgreSQL's clock and under a row lock. A worker that stalled
  and was taken over (a "zombie") is refused, however sure it is that its
  lease is still valid.
- **No approved action is dispatched by a stale worker.** Ownership is proved
  in the write-ahead transaction, and again immediately before the tool runs.
  An action already in flight when ownership changes cannot be recalled: its
  outcome is recovered from the log if it got there, and otherwise the
  ledger's rules apply (a class C action waits for a person).
- **No checkpoint or outcome is recorded by a stale worker.** It proves
  ownership before each. Checkpoints are files, which cannot be fenced, so
  this protection holds for workers on one host, where the run lock also
  keeps one writer per run
  ([workers.md](workers.md#checkpoints-and-the-filesystem)).
- **Fail closed.** An ownership that cannot be confirmed (the database is
  unreachable, or no heartbeat was confirmed in time) counts as lost. The
  worker writes nothing more to the run and does not release it; the next
  owner recovers it.
- **Workers are not users.** They reach runs only through the database, have
  no HTTP endpoints, and never use user tokens. Their database configuration
  stays in the worker process: never in a sandbox request, a workspace, or a
  prompt.

## Workspace confinement

`Workspace.resolve` rejects the following:

- absolute paths, drive letters, and `..` components
- anything that resolves (symlinks included) outside `project/`
- anything inside `.git/`
- writes and deletes under template **protected paths**. The calculator
  template protects `tests/`, so the developer cannot "fix" failures by editing
  the acceptance tests.

Deleting directories is refused outright.

## Process and git isolation

- **Test execution** goes through the sandbox layer ([sandbox.md](sandbox.md)).
  - Untrusted generated code runs only in the Docker sandbox: container
    isolation, no network, copy-in, bounded resources, verified cleanup. If
    that isn't available, the run is `blocked`; it never falls back to a
    local process.
  - Trusted fixtures (maintainer-scripted) run with the local backend, which
    is **not a sandbox**. It runs a fixed argv (never a shell, and never a
    model-chosen command) with `cwd=project/` and a timeout. Its environment
    is built from an allowlist (`PATH`, `SYSTEMROOT`, temp dirs, ...), so API
    keys, tokens, and proxy settings in the parent environment are not
    inherited.
- **Git.** Workspace git acts only on the workspace's own repository,
  `project/.git`, and otherwise refuses (`Workspace._git`, fail closed):
  - Inherited `GIT_*` variables are dropped (a git hook sets `GIT_DIR`,
    `GIT_INDEX_FILE`, and others). `GIT_CONFIG_GLOBAL=/dev/null` and
    `GIT_CONFIG_NOSYSTEM=1` keep the user's credential helpers, hooks,
    signing keys, and remotes out of workspace commits.
  - Before every command, git's own repository discovery, capped at
    `project/` (`GIT_CEILING_DIRECTORIES`), must report exactly `project/` as
    the repository root and `project/.git` as both the git directory and the
    common directory, and `.git` must be a plain directory, not a `.git` file,
    link, or junction. Anything else raises `GitConfinementError` and runs
    nothing.
  - The command then runs pinned to that repository (`GIT_DIR`,
    `GIT_WORK_TREE`), so a repository that disappears after the check makes
    the command fail instead of finding another.
  - `init_git` is the only path that creates a repository, and it refuses to
    run over an existing `.git`. In a workspace created with git, `git_commit`
    refuses when the repository is missing or damaged, instead of skipping.

  Before this, git searched upward from `project/`. During E1-E2 testing, a
  test workspace whose `.git` had been deleted ran `git config` in Agent
  Factory's own repository, and very likely staged its files. There is no
  push tool.
- **Not guaranteed:** a concurrent process with write access to the workspace
  could swap `project/.git` for a redirecting `.git` file between the check
  and the command. Agents cannot: paths into `.git` are refused, and an
  agent's tools run one at a time. The workspace itself is trusted host
  state.

## Model output and agent contracts

- Model responses are validated against each agent's JSON schema. Invalid
  output fails the run (`AgentOutputError`) instead of being guessed at.
- An agent writing a state field it does not own fails the run.
- A graph outcome without a declared edge fails the run
  (`InvalidTransitionError`).

## Orchestrator guarantees (since v0.1)

- No network calls. No provider makes any, and `network_access` is denied.
- No credentials are read, stored, or passed to subprocesses.
  - **Exception (unreleased; only with `--database`):** the PostgreSQL URL
    is read from explicit configuration and used only inside the
    orchestrator process.
  - It is never written to a workspace, checkpoint, or log, and never
    passed to a subprocess or sandbox
    ([database.md](database.md#security-boundary)).
- No deployment tooling exists.
- File effects are confined to the workspace directory.

The execution sandbox's guarantees, and its limitations, are stated in
[sandbox.md](sandbox.md#what-v02-provides).

## HTTP API (unreleased, loopback only)

The local backend ([backend.md](backend.md)) adds a new way to *record* a
decision, not a new way to *act*. Every product request is authenticated and
authorized ([authentication.md](authentication.md)); the API still listens on
127.0.0.1 only.

- **Who is calling.** A signed OpenID Connect access token, verified on
  every request: an allowlisted asymmetric algorithm with the provider's
  published key, the exact issuer, the audience, expiry. Its issuer and
  subject must belong to an enrolled, active principal. Otherwise 401, the
  same for every reason. Production is the default mode and refuses to start
  unconfigured; the development issuer is refused in production.
- **What they may do.** Every operation requires exactly one permission,
  checked on the server before anything is looked up (403 otherwise). No
  permission implies another: reading approvals does not allow deciding, and
  deciding does not allow reconciling (`actions:reconcile`). Grants live in
  Agent Factory's store; token claims grant nothing.

- **Recording is not executing.** `POST .../approvals/{id}/decision`
  stores one decision bound to the request's action fingerprint, through
  the same `ApprovalService` the CLI uses, and wakes the run in the same
  transaction. It never resumes a run, applies a checkpoint, or calls a tool
  (a static test keeps execution code out of the API). A worker that holds
  the run's claim applies it, after re-checking the stored request against
  the engine's own exact pending action, the decision's fingerprint, the
  request's status, the expiry, and that the decider is still enrolled,
  active, and permitted; any contradiction applies nothing
  ([workers.md](workers.md#what-a-worker-re-checks-before-applying-a-decision)).
  An approved database row alone authorizes nothing.
- **Approvals expire.** 12 hours after the request, by server time, the
  instant itself included. An approval must be decided *and* begin its
  dispatch before then; expiry never renews, and asking again is a new
  request. Rejections and expiries are real outcomes the graph follows; a
  rejected request is never asked again.
- **Exact action, server-controlled facts.** Deciding needs
  `approvals:decide`. The client must send the fingerprint it was shown; a
  mismatch is 409 and records nothing. The decision time is the server's
  clock, and the actor is the authenticated principal (`verified: true`):
  authentication proves who decided, the fingerprint proves what. Requests
  carrying `decided_at`, `decided_by`, `actor`, `verified`, `user_id`,
  `status` or `id` are refused (422). Expired and cancelled requests cannot
  be decided (409). An identical retry by another principal is 409.
  Decisions and reconciliation answers share a limit of 30 a minute per
  principal (per API process).
- **No execution surface.** There is no route to run commands or tools,
  start or change runs, edit milestones, touch checkpoints or fingerprints,
  or set identity or permissions; every undocumented method is 405
  (`test_api_conformance.py`). Principals are enrolled only with the local
  CLI.
- **No new path to the sandbox.** The API process holds the database URL,
  a cursor-signing key, its authentication settings, and, for one request
  at a time, an access token. None of them is passed to sandboxed code: the
  sandbox environment is built from an allowlist that rejects secret-looking
  names (including every `AGENT_FACTORY_*` setting, `JWT`, `BEARER`, `OIDC`, `JWK`,
  and notification providers' `AWS_*`, `TWILIO*`, `*SID*`, `SES_*`, `SNS_*`, `SMTP*`),
  credential-bearing URLs, and any value shaped like a JWT under any name
  (regression tests in `test_api_conformance.py`, `test_auth_boundary.py`,
  and `test_db_sandbox_boundary*.py`). The engine, sandbox, tools, and agents
  never import authentication code, and the API never launches the sandbox.
- **Never logged:** access tokens and the `Authorization` header; a redactor
  removes anything shaped like a token from every log line.
- **Exposure.** Loopback bind only (public addresses refused), a Host
  allowlist against DNS rebinding, JSON-only writes, no CORS, no proxy
  headers, and a token on every product request. Public exposure still needs
  a deployment design and a configured identity provider.

## Models are untrusted input (unreleased)

A real model ([model-providers.md](model-providers.md)) supplies text, never
authority:

- **No direct tools.** A model answers with one schema-validated JSON object.
  The agent turns it into actions only through the Tool Gateway (policy,
  approval, ActionExecution, sandbox, audit). Undeclared fields, paths out of
  the project, and oversized answers are rejected by the schema. Writes to
  protected paths are refused by the workspace, and deletions wait for a
  person.
- **Capabilities come from policy.** The manifest a model is shown is built
  from the permission policy and tool registry only. Project text cannot add
  a capability.
- **Prompt injection.** Project data travels separately from instructions,
  labelled untrusted. Tested with a model that obeys injected text: it gains
  nothing.
- **No secrets leave.** Context withholds secret-looking files and redacts
  host paths. The request guard refuses any request carrying a credential
  the orchestrator holds, a credential URL, or a JWT. Provider keys come
  only from an explicitly configured file, travel only in the HTTPS header,
  and cannot reach the sandbox (every `AGENT_FACTORY_*` name is rejected
  there).
- **Spending fails closed.**
  - Budgets, including a configured dollar ceiling, are checked before each
    call against that call's worst case. The input estimate is an upper
    bound: one token per UTF-8 byte plus overhead.
  - The usage ledger is written before each call is sent.
  - An exhausted budget, or a provider-side spend limit, blocks the run with
    no retry.
  - A model refusal fails the node; it is never treated as a result.
- **Explicit activation.** A real provider needs
  `AGENT_FACTORY_MODEL_PROVIDER`, `MODE=live`, a model name, and a key file.
  A key in the environment activates nothing.
- **Generated code is untrusted.** A real model's code runs only in the
  Docker sandbox.

## Notifications never authorize (unreleased)
Attention notifications ([notifications.md](notifications.md)) tell a
person that something needs them. They are deliberately powerless:

- **No approval authority outside the dashboard.** No SMS reply, email link,
  push button, or URL approves, rejects, or answers anything. A
  notification's link is a plain dashboard path. Opened without a session,
  it shows sign-in; the decision is still made in the authenticated
  dashboard, recorded with the person's verified identity, and re-checked by
  a worker.
- **Minimal content.** The project's name (sanitized and clipped) and the
  reason: no tool, parameter, path, credential, token, or client data.
- **Recipients are decided by the server:** active principals who hold the
  permission to act on the attention (`approvals:decide` or
  `actions:reconcile`). No API route sends a message or names a recipient.
- **Only the dispatcher reaches a provider.** Agents, tools, the engine, the
  sandbox, and the API never import the provider code (a static test).
  Provider credentials, once a real provider exists, live only in the
  dispatcher's environment. The sandbox rejects their names.
- **A notification failure never hides the attention item.** The run stays
  paused and the dashboard shows the item whatever the delivery status.
- **Approval is not notification either:** a sent notification proves only
  that a provider accepted it.

## Cloud capabilities never hand over a cloud identity (unreleased; no AWS call yet)

The AWS tool pack ([aws-capabilities.md](aws-capabilities.md)) has a fake
backend (the default) and a real one, `BotoAwsBackend`, that has only ever run
offline against stubs. No AWS API call has been made. Its boundaries:

- **Typed capabilities only.** One fixed operation per tool (STS
  GetCallerIdentity, S3 HeadObject; the real backend implements only the
  first). There's no generic call, script, shell, or CLI path; an extra
  parameter is a different action needing a new approval, and the typed tool
  refuses it.
- **`cloud_read`, by environment.** Both reads are `cloud_read`: read-only,
  bounded, typed, through the trusted adapter. They're allowed in development,
  configurable in staging (approval by default), and need approval in
  production, where an allowing policy is refused. No agent has them on its
  allowlist by default. A tool that may mutate can't register as `cloud_read`.
- **The real backend is explicit.** It's used only when settings name it
  *and* mark it authorized; nothing in the product does. It never uses a
  default session or the default credential chain, never opens the shared
  credentials file, and resolves exactly one approved provider for one
  allowlisted profile.
- **Temporary credentials only, with no fallback.** A capability names an
  abstract slot bound to one named profile. Long-term keys, unknown credential
  kinds, and expired or expiry-less credentials are refused before any
  operation. `default` and `internal-dev` can't be bound, and nothing falls
  back to another profile, the environment, a credential process, the
  container endpoint, or the instance metadata service.
- **Fixed errors.** Every failure is one of a fixed set of `AWS_*`
  categories; SDK message text never reaches a result, the API, a model, or a
  log.
- **Results are filtered.** Only known fields survive. Credential-like keys
  and values are removed, account numbers masked, and the size bounded.
- **The model and the sandbox see none of it.** The model gets descriptions
  only and still has one callable tool (`submit_result`). The sandbox rejects
  `AWS_*` names and still has no network.
- **IAM, credential, and organization changes are a denied class,** never
  approvable.

## Known gaps

- **Database credentials** (unreleased branch) live in git-ignored files
  on the developer's machine. There is no secret manager yet.

- **Sandbox limitations.** Container escape, test-verdict forgery, possible
  registry metadata lookups during image builds, a single live-tested
  backend, non-persisted backend health, and a machine-specific image lock.
  See
  [sandbox.md](sandbox.md#what-v02-does-not-guarantee).
- **Exactly-once is not claimed.** A class C action whose process stops after
  sending and before recording the outcome is *uncertain*; a person decides.
  Class B relies on the external system honouring idempotency keys, which
  Agent Factory cannot verify. A person's reconciliation can be wrong.
- **The local CLI is not authenticated.** Anyone who can run the CLI
  against the workspace (or the database) can approve, and enroll
  principals; the CLI records an unverified local actor. The HTTP API
  authenticates every request, but no production identity provider is
  configured yet, browser sign-in does not exist, tokens cannot be revoked
  before they expire, and rate limits hold per process only
  ([authentication.md](authentication.md#what-is-not-guaranteed-and-what-remains)).
- **No per-run cost or token budget yet.** This is needed before
  `spend_money`-style providers are introduced.
- **Workers share one host.** Checkpoints and workspaces are local files;
  workers on several hosts, or a workspace on a network filesystem, are
  unsupported. Two runs of one workspace never run at the same time on a
  host (the workspace lock). Operator commands with `--database` claim the
  run like a worker or only record intent
  ([workers.md](workers.md#operator-commands)).
- **A person's answer can be wrong**, and "it did not happen, try once more"
  about an action that did happen sends it again. The interface asks for an
  answer only from someone with `actions:reconcile`, shows exactly which
  attempt, and records who answered; it cannot check the outside world.
