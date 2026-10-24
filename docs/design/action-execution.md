# Durable action execution: design

| | |
| --- | --- |
| Status | **Approved** by the owner with eight decisions (below). **Stages E1-E2 implemented** on `feature/auth-worker` (unreleased), including the PostgreSQL ledger. The owner's milestone plan then made **E3 authentication and authorization** (implemented: [authentication.md](../authentication.md)) and **E4 worker coordination** (implemented: [workers.md](../workers.md)) and **E5 approval and reconciliation consumption** (implemented: workers apply decisions, a reconciliation endpoint, the execution timeline, approval expiry; see [E5](#human-decisions-e5-done)). See [Implementation status and deviations](#implementation-status-and-deviations-e1-e2) and [Stage numbering](#17-proposed-implementation-stages). |
| Baseline | `feature/backend-api` at `cd8fb4a`; design commit `83b22d1` |
| Scope | Approval-gated tool actions: recording whether an approved action actually executed, so that no approved action ever executes twice automatically |
| Out of scope | Authentication, the worker loop, schema migrations, application code. Each is a later, separately approved stage ([section 17](#17-proposed-implementation-stages)). |

## Owner decisions (binding)

1. States: `authorized`, `dispatching`, `succeeded`, `failed`, `uncertain`,
   `lapsed`; only legal transitions; no silent recovery from impossible
   states.
2. A distinct run status `awaiting_reconciliation` (not `blocked`).
3. A future permission `actions:reconcile`, separate from `approvals:decide`.
4. A durable checkpoint ledger for runs without PostgreSQL.
5. A dispatch must begin before its approval expires; no silent renewal.
6. "It did not happen, try once more" is a new authorization valid for 15
   minutes, bound to the project, run, action, fingerprint, and prior
   attempt.
7. Every side-effecting tool declares its class (read-only, idempotent,
   non-idempotent); undeclared means the most conservative class; an
   undeclared dangerous tool is refused at registration.
8. Eventually all external side effects flow through the ledger; E1-E2 may
   start with approval-gated actions.

## Implementation status and deviations (E1-E2)

**Implemented:** the domain records and six-state machine
(`domain/executions.py`); the `ExecutionLedger` protocol over
`ExecutionRepository`, with three stores (in memory, PostgreSQL migration
`0003`, and a per-run checkpoint file); tool declarations and registration
checks (`safety/execution.py`); the `ActionGuard` (`engine/actions.py`),
which the gateway uses for every approved action; write-ahead dispatch to
the ledger and to the fsync'd execution log; replay; recovery from log
evidence and by class policy; lapse; the `awaiting_reconciliation` status
(engine, checkpoint v3, PostgreSQL, API contract); reconciliation records and
`Executor.reconcile_action` (CLI `reconcile-action`); the run lock;
`action_execution` milestones. Evidence is listed in
[testing.md](../testing.md#durable-action-execution-tests-unreleased).

**Deviations from the text below**, each deliberate:

| Design said | Implemented | Why |
| --- | --- | --- |
| Class names A `NONE` / B / C | `READ_ONLY` / `IDEMPOTENT` / `NON_IDEMPOTENT` | The owner's naming; same meaning |
| Transitions in section 5.4 | Adds `authorized -> uncertain` | When a retry window closes (expiry, node visit or run end) after an earlier attempt of the same authorization ended unknown, "lapsed" (known never dispatched) would be false |
| The executor creates `authorized` executions when it applies a decision | Created lazily, when the gateway first uses the approval; never-requested approvals are created and lapsed when their node visit ends | Lets a decision applied without the store (CLI `approve` without `--database`) be bound to the run's real ledger later, and removes a crash window |
| The checkpoint holds `executions` | A separate file, `<run>/executions.json`, with a schema version and SHA-256 digest; it also records the run's ledger binding | Keeps `checkpoint.json` a node-boundary state; ledger writes happen mid-node |
| Run claims (`run_claims`, `SKIP LOCKED`, leases, fencing) | **Deferred to the worker; implemented in E4** (migration `0005`), with the deviations listed under [Worker coordination (E4, done)](#implementation-status-and-deviations-e1-e2) | E2 relied on the version compare-and-set plus the OS run lock |
| Tool probes (`ProbeKind`), `fresh_key_after_reconciliation` | Not implemented | `delete_file` is class B natural, so recovery retries it; no keyed tool exists yet. Retries after reconciliation reuse key generation 1 |
| Ambiguous commit re-read (section 8.2) | Not implemented | An ambiguous write-ahead commit is treated as unavailable: no dispatch, the run blocks, and a later recovery may declare a class C action uncertain (a safe false positive) |
| A ledger write that fails after the tool (section 8.2): keep the run lock and retry with backoff | The run blocks at once; on resume, the outcome logged before the ledger write finishes the record without a person | No retry loop holds a run open; the logged outcome makes blocking safe (tested). A filesystem error in the checkpoint ledger counts as unavailable, like a database outage |
| Indexes for uncertain/dispatching executions | Only `action_executions_run_idx` | Only queries that exist get indexes |
| A separate "reconciliation required" milestone | The `action_execution` milestone's `blocked` status plus the run status | Avoids duplicate, noisy milestones |
| (not in design) | The gateway checks an approved call's parameters against the tool's signature **before** the write-ahead record; a `TypeError` during a dispatch is then an unknown outcome | Otherwise a crash inside a tool after it acted could be misreported as "definitely not sent" |
| (not in design) | Runs with no approved action never consult the ledger | Keeps runs without approvals exactly as before, with no new storage dependency |

**Authentication (E3, done).** An approval decision now records the
authenticated principal who made it, after `approvals:decide` is checked and
before the fingerprint binding; authentication proves who decided, the
fingerprint what. `actions:reconcile` exists as a distinct permission, held
by no route yet. Approval is still not execution: nothing in E3 dispatches
an action ([authentication.md](../authentication.md#approval-decisions-who-and-what)).

**Worker coordination (E4, done).** Run claims, leases, heartbeats, fencing
tokens, and a minimal worker loop are implemented
([workers.md](../workers.md)). Deviations from sections 8.6, 9, and 12.3,
each deliberate:

| Design said | Implemented | Why |
| --- | --- | --- |
| `run_claims` columns `holder`, `fence`, `lease_until`, `heartbeat_at`, `wanted_since` + `available_at` | `worker_id`, `fencing_token`, `lease_expires_at`, `last_heartbeat_at`, `claimed_at`; one `available_at` (NULL: not wanted) | The owner's E4 field names; one column says both whether and from when a run wants a worker |
| The dispatch gate checks the claim with `EXISTS (… FOR SHARE)` inside its `UPDATE` (9.3), and the outcome write checks owner and fence (9.4) | **Every** transaction of a claimed run's store begins with the fence check under `FOR SHARE`; statements outside a transaction are wrapped in one | The same guarantee, applied to every write (product records, ledger, approvals) rather than to two statements |
| `owner_fence` on executions and attempts | The attempt's `owner` text records `worker@token`; no new ledger columns | The fence is proved by the transaction that writes the attempt; the text keeps the audit trail without a schema change to the ledger |
| Lease: the larger of 60 s and three times the longest `dispatch_timeout`; heartbeat every lease/3 (9.6) | Lease 60 s, heartbeat 20 s, bounds 2 s to 5 min (also a database constraint); heartbeats on their own thread and session | A tool's duration no longer sizes the lease, because heartbeats continue during it; no tool declares a dispatch timeout yet |
| (not specified) | Renewal only while the lease is valid; an expired lease is lost even without a takeover | Fail closed; only a new claim, with a new token, can win a run back |
| The claim selects wanted, available, unowned-or-expired runs (9.2) | Also never a finished run; the `UPDATE` repeats the ownership condition | Defense in depth: a stale `available_at` can never hand out a finished run |
| Every process acting on a run, the CLI included, holds its claim (8.6) | Done in E5: database-backed operator commands claim the run (`claim_run`) or only record intent; without `--database` they keep the run lock alone | No database, no workers to race ([workers.md](../workers.md#operator-commands)) |
| Worker loop step 3: apply stored decisions and expiries (12.3) | Done in E5 (`Executor.advance`) | See below |
| (not in design) | Ownership proved again after the write-ahead record, immediately before the tool; if lost, `action_not_dispatched` goes to the log and recovery returns the authorization | Ownership can change between the write-ahead commit and the dispatch |
| (not in design) | Ownership proved before a run outcome is logged, and before every checkpoint write | A stale worker records no outcome and commits no checkpoint |
| Assumption 2: one host per run's workspace | Kept, and stated as E4's limit: workers share one host | Files cannot be fenced; the production boundary (checkpoints and logs as fenced rows, workspaces as content-addressed snapshots) is designed in [workers.md](../workers.md#checkpoints-and-the-filesystem), not built |

### Human decisions (E5, done)

Workers consume decisions under the run's claim, re-checking the exact
pending action, the expiry, and the decider first; recording a decision
queues the run (the transactional outbox of 9.2); `reconcileAction`
(`actions:reconcile`) records answers as intent; approval views carry the
execution and its timeline; approvals expire
([workers.md](../workers.md#human-decisions-wake-ups-and-consumption)).
Deviations:

| Design | Implementation | Why |
| --- | --- | --- |
| The API records a reconciliation directly in the ledger (`action_reconciliations`) | The API records **intent** (`reconciliation_decisions`, migration 0006); the worker that holds the claim applies it to the ledger | Every ledger write stays fenced by the claim; the 15 minutes still count from the person's decision (`ExecutionLedger.reconcile(decided_at=…)`) |
| The queue's wake-up sets `available_at` | Also counts it (`run_claims.wake_seq`), and a release that sees a wake-up newer than its claim leaves the run available | Otherwise a release by a worker that looked before the decision committed undoes the wake-up (found by a race test) |
| One approval validity, unspecified | 12 hours from the request, for deciding *and* for beginning the dispatch; the earlier of the engine's and the store's deadline | A phone link from yesterday must be dead; one window is simpler to explain than two |
| Run lock per run (assumption 2) | Also a workspace lock: one run at a time may run agents or tools in a workspace | Runs of one project share its files; two workers advancing two of its runs corrupted each other (found by an E4 test made deterministic by E5) |
| A refused decision raises for a person | `DecisionRefused` (a stored decision contradicting the engine) parks the run (not offered again); an approval whose decider may no longer decide is applied as refused | A contradiction needs investigation; a disabled approver just means "no" |

**Still to do:** extending the ledger to auto-allowed side effects; tool
probes; a shared (multi-process) rate limit; a way for a person to clear a
refused decision through the product rather than by investigation.

## Summary of the proposed decisions

1. **Authorization and execution become separate records.** `ApprovalDecision`
   keeps meaning "a person approved this exact action". A new
   **`ActionExecution`** record means "this is what happened when Agent Factory
   used that approval". Approval never implies execution.
2. **One approval authorizes at most one effect.** A repeated request for the
   same exact action is answered from the record (*replay*), not executed
   again. Another attempt is allowed only when the tool's declared semantics
   make a repeat harmless, or when a person explicitly authorizes it.
3. **Write-ahead dispatch.** An approved action is sent only after a durable
   record says that it may have been sent. A crash therefore always leaves
   evidence that errs toward "it may have run", never toward "it did not run".
4. **Six states**: `authorized`, `dispatching`, `succeeded`, `failed`,
   `uncertain`, `lapsed` ([section 5](#5-the-action-execution-state-machine)).
5. **Tools declare their execution semantics**: class A (no external effect),
   class B (idempotent, naturally or by an idempotency key), class C
   (non-idempotent). An undeclared tool is class C.
6. **Class C never retries automatically after an uncertain attempt.** The run
   pauses for human reconciliation.
7. **PostgreSQL coordinates workers.** Runs are claimed with
   `SELECT … FOR UPDATE SKIP LOCKED`, leases, and fencing tokens. Each dispatch
   is gated by a single-row compare-and-set that is committed *before* the side
   effect. Holding a row lock across the side effect is rejected.
8. **A run lock on the host.** Any process that changes a run holds an OS file
   lock on it, which the operating system releases when the process dies.
9. **No exactly-once claim.** Class A is at-least-once. Class B is
   effectively-once only if the external system honours idempotency. Class C is
   at-most-once automatically, and an uncertain attempt is resolved by a person.

---

## 1. Problem statement

### 1.1 Authorization is recorded; execution is not

What the code does today:

- `ApprovalRequest` and `ApprovalDecision` record **authorization** in the
  product domain (and in PostgreSQL when a product store is attached;
  [product-domain.md](../product-domain.md#approvals)).
- Inside the engine, the decision lives in the checkpoint as
  `state.approvals[request_id] = {"approved": true, …}`
  (`Executor._apply_decision`, `agent_factory/engine/executor.py`).
- `ToolGateway.call` (`agent_factory/tools/gateway.py`) runs an
  approval-gated tool **whenever** that record says approved. Nothing marks
  the approval as used, either before or after the tool runs.
- A paused node is not committed. After a decision, `Executor.resume` re-runs
  the **whole node** from the start ([safety.md](../safety.md#approval-gate-lifecycle)).
  The agent asks for the same action again (request IDs are deterministic),
  the gateway finds the approval, and runs the tool again.
- The execution log writes an `action` entry only **after** the tool returns.

So there are two paths to a second execution, and neither needs a bug
anywhere else:

- **(a) A node that needs two approvals.** The first approved action runs, and
  then the second gated call pauses the node. When the node re-runs after the
  second decision, the first action runs again.
- **(b) A crash after the side effect, before the node's checkpoint.** The node
  re-runs on resume and the action runs again.

A related documentation inaccuracy: [safety.md](../safety.md#approval-gate-lifecycle)
says "a paused run has made no partial changes in that node". That is true only
for the *first* gated call of a node visit. In case (a), the second pause
happens after the first deletion. It will be corrected when this design is
implemented.

### 1.2 Evidence

On 2026-10-03 a throwaway experiment was run against the baseline, using
temporary workspaces. It is not committed; [Appendix A](#appendix-a-reproducing-the-experiment)
describes how to reproduce it. A wrapper counted the real dispatches of
`delete_file`, the only approval-gated tool.

| Case | Steps | Real dispatches for one approval |
| --- | --- | --- |
| (a) Two approvals in one node | The developer asks to delete `legacy_calc.py` and `old_a.py`. Approve the first and resume; approve the second and resume. | **2** for `legacy_calc.py`. The second returned "did not exist". |
| (b) Crash after the side effect | Approve and resume; the process dies right after the deletion, before the node checkpoint. A new executor resumes. | **2**. The execution log holds **one** `delete_file` entry, for the harmless second attempt. **The real deletion is missing from the audit trail.** |

`delete_file` survives being run twice only because of the tool:
`Workspace.delete` reports "did not exist" the second time. That is not a
property of the engine. The tools the roadmap needs (`spend_money`,
`production_deploy`, `credential_change`, `external_communication`) would pay,
deploy, rotate, or send twice.

### 1.3 Requirements

- An approved action executes **at most once**, unless its tool *declares and
  justifies* that repeats are harmless, or a person explicitly authorizes
  another attempt.
- Exact-action binding is preserved: a record for action A can never authorize
  or describe action B.
- A crash at any point never causes an automatic repeat of a non-idempotent
  action.
- Uncertainty is shown to a person, never settled by guessing.
- Two workers, or a worker and a CLI, never dispatch the same attempt.
- The audit trail records a dispatch before it happens, and its outcome after.
- The processing semantics are stated precisely. There is no exactly-once
  claim.
- Everything above is observable, eventually on a phone.

## 2. Vocabulary: authorization is not execution

| Record | Answers | Written by | Exists today |
| --- | --- | --- | --- |
| `ApprovalRequest` | "Which exact action does the engine want to take?" | Engine (recorder sync) | Yes |
| `ApprovalDecision` | "Did a person approve or reject *that exact* action, and who, and when?" | API / CLI | Yes |
| **`ActionExecution`** | "Has Agent Factory used this approval? What happened?" One per approved decision. | Engine (gateway, through a ledger interface) | Proposed |
| **`ExecutionAttempt`** | "One durable dispatch of the action, by which owner, and its outcome" | Gateway | Proposed |
| **`ActionReconciliation`** | "A person's determination of an uncertain attempt" | API / CLI | Proposed |

The approval's status stays `approved` forever, because it is a historical
fact. Execution has its own state. The phone shows both.

## 3. Threat and failure model

### 3.1 Failures considered

| Failure | Examples | What it does |
| --- | --- | --- |
| Process death | `kill -9`, out of memory, power loss, Ctrl+C at a bad moment | Knowledge held only in memory is lost; durable records survive |
| Process hang | Blocked I/O, an external call that never returns | The owner is alive but silent, and its leases expire |
| Database restart or failover | Container restart, managed failover | In-flight transactions roll back, connections drop, session locks vanish |
| Lost commit acknowledgement | The connection drops during `COMMIT` | The client cannot tell whether the commit happened |
| Database data loss | Restore from backup, promotion of an asynchronous replica | Committed rows can disappear |
| Workspace loss | Disk failure, a deleted directory | The checkpoint and the execution log are gone |
| External uncertainty | A timeout after sending, a connection reset, an ambiguous 5xx, a partial batch | Agent Factory does not learn the outcome |
| Concurrency | Two workers, a worker and an operator's `resume`, a "zombie" whose lease expired | Concurrent attempts to act on one run |
| Clock skew | Host clocks disagree | Lease and expiry comparisons become inconsistent |
| Human error | A wrong reconciliation answer | A duplicate, or a missed action |

### 3.2 Adversaries

- **API clients**, even authenticated ones, can approve, reject, or reconcile.
  They can never execute anything, choose an attempt, an idempotency key, or a
  state, or reset an execution. All of those belong to the server.
- **A holder of `approvals:decide`** can authorize a retry after
  reconciliation. That is equivalent to approving the action again, so it
  carries the same trust, with verified identity, step-up authentication,
  audit, and notification ([section 11.4](#114-authority-and-binding)).
- **A compromised orchestrator host** holds the database credentials and the
  workspace. That is outside this design: it is the trusted side of the
  boundary ([sandbox.md](../sandbox.md)).
- **Untrusted generated code** runs in the sandbox. It never sees the ledger,
  database settings, or idempotency keys, and nothing here changes that.

### 3.3 Assumptions

Each assumption must hold, and each is either tested or documented.

1. **PostgreSQL acknowledges a commit only after it is durable.** That means
   `fsync = on`, `synchronous_commit = on` for ledger transactions (set per
   transaction), and failover that does not lose acknowledged commits.
   Asynchronous replicas never serve ledger reads.
2. **A run's workspace is on a local filesystem of exactly one host**, and
   every process acting on that run runs on that host. Network filesystems make
   OS file locks unreliable.
3. **The OS releases a process's file locks when it exits**, however it exits.
4. **Tools are trusted code** and classify their own failures honestly
   ([section 6.2](#62-error-taxonomy-the-tool-decides-the-gateway-records)).
5. **Lease and expiry arithmetic uses the database clock only.**
6. **`fsync` on the execution log is honoured by the disk**: no write cache
   that lies about durability.

### 3.4 What no design can do

No protocol guarantees exactly-once execution of an effect in a system that
cannot deduplicate it and cannot report on it. Suppose the process fails after
sending a request and before recording the response. Nothing Agent Factory
holds can then tell "sent and done" from "never sent". This design does not
pretend otherwise. It records the uncertainty durably, refuses to guess, and
asks a person.

### 3.5 Knowing versus inferring

This rule is used throughout:

- **Known**: facts that Agent Factory recorded durably before the failure, and
  definitive reports from a tool that were recorded durably.
- **Inferred**: anything about another process or an external system that is
  drawn from timing or from the absence of a record. For example: a lease
  expired, a lock was free, or a probe found nothing.
- **The rule**: an inference may only make Agent Factory *more cautious*, for
  example releasing a claim or declaring an attempt uncertain. It **never**
  authorizes a new dispatch of a class C action.
- **One inference is backed by an OS guarantee.** Acquiring a run's file lock
  proves that no live process on this host still holds it. It proves the owner
  *process* is gone, not what that process did.

## 4. Execution identity

### 4.1 The execution ID

```text
execution_id = "axe-" + sha256_hex(canonical_json({
    "v":           1,
    "project":     project_id,          # prj-…, from the workspace marker / product store
    "run":         run_id,              # the engine's run id
    "request":     request_id,          # apr-… = make_request_id(run, node, visit, tool, params)
    "fingerprint": action_fingerprint,  # sha256:… over tool, category, params (ExactAction)
}))[:20]
```

| Component | Why it is included |
| --- | --- |
| `project` | Engine run IDs and request IDs are unique only within one workspace ([product-domain.md, design issue 1](../product-domain.md#design-issues-found)). The project scopes them globally. |
| `run` | The run whose checkpoint governs the action |
| `request` | Binds node, node visit, tool, and parameters. The same action requested in a *later* visit is a different action, which needs a new approval. |
| `fingerprint` | Binds the category and the exact parameters as approved. An action that differs in any way can never map to the same record. |
| `v` | Lets the derivation change later without colliding with old IDs |

Properties:

- **Deterministic.** Any process can compute the ID from the call in hand,
  with no lookup: a worker, the CLI, or a node that re-runs.
- **Collisions fail loudly.** The ID is 80 bits, the same as `apd-` decision
  IDs. A collision would violate the unique and foreign-key constraints, and
  would never be silently merged.
- **Not secret.**

**Parameters and the phone.** Request IDs and fingerprints use
`Tool.summarize_params`, which replaces strings longer than 200 characters with
their SHA-256. The binding stays exact, because different content gives a
different hash. But someone approving on a phone sees a hash, not the content.
That is a concern for the approval screen ([section 13.4](#134-on-the-phone)),
not a weakness in the binding.

### 4.2 Attempts

An attempt is identified by `(execution_id, attempt)`, where `attempt` counts
1, 2, …. Each attempt also records its owner (worker ID) and the owner's
**fencing token**: the claim generation of the run ([section 9.6](#96-leases-heartbeats-fencing-and-clocks)).

### 4.3 External idempotency keys (class B, keyed tools only)

```text
idempotency_key = "af1-" + execution_id + "-k" + key_generation
```

- **Automatic retries reuse the key** (`key_generation` is unchanged), so the
  external system deduplicates them.
- `key_generation` increments **only** when a person authorizes a new attempt
  after reconciliation **and** the tool declares that a fresh key is needed.
  Some providers cache a failed response under its key, so a real retry needs a
  new one.
- Because the key derives from the fingerprint-bound execution ID, two
  different actions never share a key.

### 4.4 Why not reuse the approval request ID

Authorization and execution are different facts with different lifecycles. A
request has at most one decision, but an execution has attempts and
reconciliations. Separate IDs keep "approved" and "executed" from ever being
conflated, in the data or on a screen.

## 5. The action-execution state machine

### 5.1 States

| State | What Agent Factory **knows** | Final? |
| --- | --- | --- |
| `authorized` | An approved decision bound to this exact action exists. No dispatch of the current attempt has been permitted. | No |
| `dispatching` | A dispatch was permitted and recorded durably (attempt *n*, owner, fence). The side effect may not have started, may be in progress, or may have finished. | No |
| `succeeded` | The tool reported a definitive success, recorded durably. Or a person confirmed it, or a conclusive probe showed it. | Yes |
| `failed` | The action definitively did not take effect. Either the tool reported a definitive failure, or a person confirmed it did not happen and chose not to retry. | Yes |
| `uncertain` | Agent Factory cannot determine an attempt's outcome, and the tool's semantics forbid an automatic retry. It is waiting for a person. | No, until resolved |
| `lapsed` | The authorization ended with **no** dispatch. The approval window closed, the node visit ended without the agent requesting the action again, or the run ended. | Yes |

### 5.2 Why these names

The brief suggested PENDING, CLAIMED, STARTED, SUCCEEDED, FAILED, and UNKNOWN.

- **`authorized`, not PENDING.** "Pending" already means "awaiting a decision"
  for approval requests. Reusing it for "approved, not yet run" invites exactly
  the confusion this design removes.
- **No CLAIMED state for executions.** Claims are taken on **runs**
  ([section 9.2](#92-claiming-a-run-for-update-skip-locked)), because the only
  way to reach a dispatch is to resume the run's graph. Per-execution claims
  would let two workers claim different executions of one run and resume it
  concurrently. Ownership of a dispatch is recorded on the attempt, as owner
  and fence.
- **`dispatching`, not STARTED.** When the durable record is written, Agent
  Factory knows only that it permitted itself to send. It does not know that
  the effect began, and the name must not overstate that.
- **`uncertain`, not UNKNOWN.** The state is entered deliberately, after
  recovery or after an indeterminate report. It means "examined, and cannot be
  determined". "Unknown" reads like "not looked at yet".
- **`lapsed` is new.** It says "known never dispatched", which is different
  from `failed`: a failure can follow a dispatch.
- **No "partially succeeded" state.** A partial effect is `uncertain`, with
  details, and tools should make each action atomic ([section 6.1](#61-classes)).

### 5.3 Diagram

```text
 approved decision
        │ (engine applies it; idempotent)
        v
  ┌────────────┐  CAS + attempt row, COMMITTED   ┌─────────────┐  definitive success   ┌───────────┐
  │ authorized │ ──────── before dispatch ──────>│ dispatching │ ─────────────────────>│ succeeded │
  └────────────┘                                 └─────────────┘                       └───────────┘
     ^   │ ^  ^                                        │  │  │      definitive failure   ┌────────┐
     │   │ │  │ not_dispatched, or class A/B           │  │  └──────────────────────────>│ failed │
     │   │ │  └────────── retry within limits ─────────┘  │                              └────────┘
     │   │ │                                              │ indeterminate; class C, or      ^
     │   │ │                                              v B out of window / attempts      │
     │   │ │                                        ┌───────────┐                           │
     │   │ └──── person: "not done, retry once" ────│ uncertain │──── person: "not done" ───┘
     │   │       (new authorization)                └───────────┘
     │   │                                              │ person: "it happened", or a positive probe
     │   │                                              └──────────────────────> succeeded
     │   └── window closed / visit ended / run ended, no dispatch ──> lapsed
```

### 5.4 Transitions

| From | To | Who | Condition | Durable record |
| --- | --- | --- | --- | --- |
| (none) | `authorized` | Executor, when it applies an approved decision. Idempotent, and redone if missing. | An approved decision with the same fingerprint exists (database foreign key) | Execution row, or checkpoint entry |
| `authorized` | `dispatching` | Gateway, by compare-and-set | The caller holds the run's current claim and fence; attempts remain for this authorization; the authorization is still valid ([section 11.6](#116-interaction-with-approval-expiry)); no attempt is open | Execution state plus a new attempt row, **committed before dispatch** |
| `dispatching` | `succeeded` | Owner (fenced), or recovery from local outcome evidence | The tool reported success | Outcome and replayable result |
| `dispatching` | `failed` | Owner (fenced) | Definitive failure | Outcome |
| `dispatching` | `authorized` | Owner | `NotDispatched` (provably never sent) and attempts remain | Attempt closed as `not_dispatched` |
| `dispatching` | `authorized` | Recovery, by the holder of the run lock | Class A, or class B within its key retention, and attempts remain | Attempt closed as `indeterminate` |
| `dispatching` | `uncertain` | Recovery (holder of the run lock), or the owner on an indeterminate result | Class C, or class B out of window or out of attempts | Attempt closed as `indeterminate` |
| `uncertain` | `succeeded` | A person ("it happened"), or a positive probe | — | Reconciliation record, or probe evidence |
| `uncertain` | `failed` | A person ("it did not happen; don't retry") | — | Reconciliation record |
| `uncertain` | `authorized` | A person ("it did not happen; try once more") | Verified identity, permission, fingerprint and attempt echoed, new validity window | Reconciliation record. This is a **new authorization** (`authorization_seq + 1`). |
| `authorized` | `lapsed` | Executor or worker | The node visit ended without a dispatch; the window closed; or the run finished | Lapse reason |

### 5.5 Invariants

- **I1.** At most one attempt per execution is open at a time (a unique
  partial index).
- **I2.** No dispatch happens without a committed `dispatching` record for
  that attempt. This depends on the order of operations in the gateway, and is
  tested by fault injection.
- **I3.** Class C has at most one attempt per authorization
  (`max_attempts = 1`, enforced by a CHECK).
- **I4.** An execution exists only for an **approved** decision with the same
  fingerprint (a foreign key that includes the outcome).
- **I5.** `succeeded` and `failed` come only from a tool's definitive report,
  local outcome evidence, a conclusive probe, or a person. They never come
  from timing.
- **I6.** `uncertain` is declared only by the owner itself, or by a holder of
  the run lock, who therefore knows the owner process is gone.
- **I7.** `lapsed` only if the current authorization was never used for a
  dispatch.
- **I8.** Every write is fenced: it carries the writer's fence, and a stale
  fence changes nothing.

## 6. Tool classes, retry policy, and processing semantics

### 6.1 Classes

- **A — No external effect.** The action is pure or read-only, or its effects
  are confined to a throwaway environment. Repeating it is harmless.
- **B — Idempotent effect.** Repeating the dispatch leaves the same end state
  and has no further effect. There are two kinds:
  - **Natural**: idempotent by definition. Examples: "make sure this path is
    absent", "set X to V", or "deploy release R" when the target treats a
    redeploy as a no-op. `delete_file` is one: the second call returns "did not
    exist".
  - **Keyed**: the external system accepts a client-chosen idempotency key or
    operation ID, and promises to deduplicate it for a declared retention
    window. Payment APIs that accept an `Idempotency-Key` header are the usual
    example.
- **C — Non-idempotent effect.** The external system cannot deduplicate the
  action and cannot prove whether an uncertain attempt took effect. Examples:
  an email sent over SMTP, an SMS through a gateway without message IDs,
  rotating a credential (two rotations produce two different keys), or a
  payment API without idempotency support.

Each action should be **one atomic external operation**. A multi-step
operation should be split into separate actions, or treated as class C.

### 6.2 Error taxonomy: the tool decides, the gateway records

| The tool… | Meaning | Recorded as |
| --- | --- | --- |
| returns a `ToolResult` | The action took effect, or there was definitively nothing to do (e.g. "did not exist") | `succeeded` |
| raises `ToolError` (definitive) | The target refused, and **nothing happened** | `failed` |
| raises `NotDispatched` (new; a `ToolError` subclass) | The request provably never left the process, e.g. validation, or connection refused before sending | Attempt closed `not_dispatched`; a retry is allowed for every class, within the attempt limit |
| raises `OutcomeUnknown` (new) | The request was sent, but the outcome is unknown: a timeout after sending, a reset, an ambiguous 5xx, a partial batch | `indeterminate`, then the class policy |
| raises anything else after dispatch began | Treated as `OutcomeUnknown` | `indeterminate` |
| — (the process died after `dispatching`) | Nothing was reported | `indeterminate`, at recovery |

### 6.3 Retry policy

| | Class A | Class B, natural | Class B, keyed | Class C |
| --- | --- | --- | --- | --- |
| After an indeterminate attempt | Retry automatically | Retry automatically | Retry **with the same key**, only while the first attempt is within the declared key retention; otherwise `uncertain` | **Never retry automatically. `uncertain`, then a person.** |
| After `NotDispatched` | Retry | Retry | Retry, same key | Retry (proven not sent) |
| After a definitive failure | `failed`, unless the tool marks the error transient | `failed` | `failed` | `failed` |
| Automatic attempts per authorization | Declared by the tool, e.g. 3 | Declared | Declared | **Exactly 1** |
| Backoff | Bounded exponential, with jitter | Same | Same | — |
| New human authorization needed | Never | Never | Never | For any further attempt after uncertainty |

### 6.4 Processing semantics, stated precisely

| What | Semantics |
| --- | --- |
| Recording a decision (API or CLI) | At most one decision per request, enforced by PostgreSQL. Identical retries are idempotent. |
| Delivering a decision to the engine (worker) | At-least-once delivery, idempotent application, so the checkpoint changes once |
| Class A action | At-least-once. Repetition is harmless by declaration. |
| Class B, natural | At-least-once dispatch, effectively-once **end state** |
| Class B, keyed | At-least-once dispatch. **Effectively-once effect if, and only if,** the external system honours the key across the retry window. Agent Factory relies on that guarantee and cannot verify it. |
| Class C | **At-most-once automatic dispatch per authorization.** An uncertain outcome is resolved by a person, and another attempt happens only if a person authorizes it. Correctness then rests on that person's determination. |
| Replay | Once `succeeded` or `failed` is recorded, a re-run node gets the recorded result, with no dispatch |

Exactly-once is **not** claimed for any class.

### 6.5 Today's tools

| Tool | Category | Approval-gated? | Class | Notes |
| --- | --- | --- | --- | --- |
| `list_files`, `read_file` | `read_workspace` | No | A | |
| `run_tests` | `execute_sandboxed` | No | A | Copy-in sandbox with no network; nothing persists from the container |
| `write_file` | `write_workspace` | No | B, natural | A re-run writes identical content locally |
| `git_commit` | `write_workspace` | No | Local only | A re-run usually finds nothing to commit. Re-execution is today's node semantics ([section 15](#15-remaining-risks)). |
| `delete_file` | `destructive_delete` | **Yes** | B, natural, with a conclusive probe | The only gated tool today. A repeat returns "did not exist". |

So at first the ledger protects one tool, whose repeats happen to be harmless.
The design exists for the tools the roadmap needs, which are class B keyed or
class C.

## 7. Declaring execution semantics on tools (proposal)

```python
class EffectClass(StrEnum):
    NONE = "none"                      # class A
    IDEMPOTENT = "idempotent"          # class B
    NON_IDEMPOTENT = "non_idempotent"  # class C

class Idempotency(StrEnum):
    NATURAL = "natural"   # repeating yields the same end state
    KEYED = "keyed"       # the target deduplicates a key Agent Factory supplies

class ProbeKind(StrEnum):
    NONE = "none"
    POSITIVE_ONLY = "positive_only"   # can confirm an effect, never prove its absence
    CONCLUSIVE = "conclusive"         # can prove either way (local, synchronous effects)

@dataclass(frozen=True)
class ExecutionSemantics:
    effect: EffectClass
    idempotency: Idempotency | None = None        # required for IDEMPOTENT, forbidden otherwise
    key_retention: timedelta | None = None         # KEYED: how long the target deduplicates
    fresh_key_after_reconciliation: bool = False   # KEYED: the target caches failures per key
    max_attempts: int = 1                          # automatic attempts per authorization; class C: 1
    dispatch_timeout: timedelta = timedelta(seconds=60)  # bounds one dispatch; sizes the lease
    probe: ProbeKind = ProbeKind.NONE
    replayable_result: bool = True                 # the result is small JSON, replayed to a re-run node

class Tool(ABC):
    ...
    #: How retries and crashes are handled. The conservative default is class C.
    execution: ClassVar[ExecutionSemantics] = ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT)

    def probe(self, workspace: Workspace, attempt: AttemptRecord, **params: Any) -> ProbeResult:
        """Look for the effect of an uncertain attempt. Only for tools that declare a probe."""
        raise NotImplementedError
```

`ToolContext` gains `execution_id`, `attempt`, and `idempotency_key`. Only
keyed tools get the key.

Rules, enforced when a tool is registered and covered by tests:

- A tool in a **dangerous category** must declare `execution` explicitly; it
  cannot inherit the default. That forces a decision.
- `NON_IDEMPOTENT` implies `max_attempts == 1`.
- `KEYED` requires `key_retention`, at least as long as the worst-case retry
  window (attempts × backoff + lease).
- A `CONCLUSIVE` probe is allowed only for local, synchronous effects, where
  the owner process's death proves nothing is in flight, or with a documented
  external guarantee.
- `NONE` in a dangerous category needs a justification, listed in a test.
- Every declaration is documented per tool and covered by a contract test.

Examples:

```python
class DeleteFileTool(Tool):  # today's only gated tool
    execution = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.NATURAL,
        max_attempts=3, dispatch_timeout=timedelta(seconds=10), probe=ProbeKind.CONCLUSIVE,
    )

class ChargeCardTool(Tool):  # hypothetical: a payment API with idempotency keys
    category = ActionCategory.SPEND_MONEY
    execution = ExecutionSemantics(
        effect=EffectClass.IDEMPOTENT, idempotency=Idempotency.KEYED,
        key_retention=timedelta(hours=24), fresh_key_after_reconciliation=True,
        max_attempts=3, dispatch_timeout=timedelta(seconds=30), probe=ProbeKind.POSITIVE_ONLY,
    )

class SendEmailTool(Tool):  # hypothetical: SMTP, no deduplication
    category = ActionCategory.EXTERNAL_COMMUNICATION
    execution = ExecutionSemantics(effect=EffectClass.NON_IDEMPOTENT, dispatch_timeout=timedelta(seconds=30))
```

## 8. The dispatch protocol

### 8.1 Where it runs

In `ToolGateway.call`, for an approval-gated tool, after the policy says
`REQUIRE_APPROVAL` and the engine's approval record says approved. The gateway
stays the single path from an agent to a side effect ([AGENTS.md](../../AGENTS.md)).

### 8.2 Steps, in order of durable writes

1. **Compute the identity.** The request ID (as today), the fingerprint from
   the *actual call* (`ExactAction(tool, category, summarized params)`), and
   the execution ID.
2. **Read the ledger.**
   - `succeeded` or `failed`: replay it ([section 8.3](#83-replay)). No dispatch.
   - `uncertain`: raise `ReconciliationRequired`, and the run pauses
     ([section 11](#11-reconciliation)).
   - `lapsed`: refuse with `PermissionDenied`, because the authorization has
     lapsed.
   - `dispatching` with an attempt this process does not own: refuse. Recovery
     runs at the start of every resume, so under the run lock this does not
     occur.
   - `authorized`: continue.
3. **Begin: the write-ahead step, committed.** Compare-and-set
   `authorized → dispatching`, inserting a new attempt row with the owner,
   fence, and database time, committed with synchronous commit. If the
   compare-and-set changes nothing, **do not dispatch**. Possible reasons:
   another owner, a stale fence, an expired authorization, or no attempts left.
4. **Local write-ahead.** Append `action_dispatch` (execution ID, attempt,
   fingerprint) to the run's execution log, which is fsync'd and hash-chained.
   This is a second durable marker, independent of the database.
5. **Dispatch.** Call `tool.run(…)` with a `ToolContext` carrying the execution
   ID, the attempt, and the key, bounded by `dispatch_timeout`.
6. **Local outcome.** Append `action_outcome` (result class and replayable
   result) to the execution log, fsync'd.
7. **Ledger outcome.** A fenced compare-and-set from `dispatching` to
   `succeeded`, `failed`, `authorized` (for a retry), or `uncertain`, which
   also closes the attempt.
8. **Return** the result to the agent, or raise the definitive failure or
   `ReconciliationRequired`.

**The order is the safety argument.** The dispatch (step 5) happens only after
steps 3 and 4 are durable. Outcomes (steps 6 and 7) are recorded only after the
tool returned. A crash anywhere leaves evidence that errs toward "it may have
run".

- **The ledger is unreachable at step 3** (the database is down). Fail closed:
  no dispatch. The run is blocked with "execution ledger unavailable", and is
  resumable.
- **The ledger write fails at step 7.** Keep the run lock and keep retrying
  with backoff. The local outcome from step 6 lets any successor finish the
  record without a person.
- **The commit at step 3 is ambiguous** (the connection was lost during
  `COMMIT`). Reconnect and read the row. If it shows this process's attempt and
  fence, the process *knows* it has not dispatched yet, so it proceeds.
  Otherwise it does not dispatch.

### 8.3 Replay

A re-run node calls the same tool with the same parameters, so it gets the same
request ID. The gateway answers from the record:

- `succeeded`: the recorded `ToolResult` (ok, summary, and bounded JSON data);
- `failed`: the recorded definitive failure, raised again;
- confirmed by a person: a `ToolResult` whose summary names the confirmation
  and whose data is empty.

The execution log records `action_replayed`. Agents must not depend on data
that is not replayable, and tools declare `replayable_result`. Replay also
covers an agent repeating an identical call within one node visit: one approval
gives one effect.

### 8.4 Lapse

An authorization can be used only during the node visit that requested it,
because request IDs include the visit. When the executor commits that node
visit, the visit counter increments, and every still-`authorized` execution of
that visit becomes `lapsed`: it can never be requested again. An execution also
lapses if its authorization window closes before dispatch
([section 11.6](#116-interaction-with-approval-expiry)), or when the run
reaches a final status. A lapse is *known*: no attempt exists.

With a real, non-deterministic model, a re-run node may not ask for the
approved action again. The authorization then lapses instead of lingering. If
the agent asks again later, it needs a new approval.

### 8.5 The ledger interface: PostgreSQL or the checkpoint, one per run

The engine sees only an interface, so it contains no SQL
([AGENTS.md](../../AGENTS.md)):

```python
class ActionLedger(Protocol):
    def authorize(self, spec: ExecutionSpec) -> None: ...                    # idempotent
    def get(self, execution_id: str) -> ExecutionRecord | None: ...
    def begin(self, execution_id: str, *, owner: Owner) -> Attempt | None: ...  # the compare-and-set
    def finish(self, attempt: Attempt, outcome: Outcome) -> bool: ...        # fenced
    def recover(self, run: RunRef, *, owner: Owner) -> list[ExecutionRecord]: ...
    def lapse(self, run: RunRef, *, node: str, visit: int, reason: str) -> None: ...
    def resolve(self, reconciliation: Reconciliation) -> ExecutionRecord: ...
```

There are two adapters:

- **PostgreSQL**, when a product store is attached. It is the authoritative
  ledger: it works across processes and is visible to the API.
- **Checkpoint**, in v0.2 mode with no database. The run's checkpoint holds
  `executions`, and the execution log holds the write-ahead and outcome
  entries. Correctness relies on the run lock (one writer). This mode needs
  checkpoint writes to **fsync the directory after the rename** on POSIX.
  Today `write_json_atomic` fsyncs the file, but not the directory.

Exactly one adapter is authoritative per run. The choice is recorded in the
checkpoint when the run starts (`execution_ledger: "postgres" | "checkpoint"`),
and resuming with the other is refused (fail closed). Otherwise a run could
dispatch once under each ledger.

### 8.6 The run lock

Before any operation that changes a run (start, resume, decide, reconcile,
expire, recover), the process takes an exclusive, non-blocking OS lock on
`<workspace>/.agent_factory/runs/<run_id>/run.lock`. It uses `msvcrt.locking`
on Windows and `fcntl.flock` on POSIX, both in the standard library. The lock
is held until the operation ends, and the OS releases it if the process dies.

- It serializes the worker, a second worker, and an operator's CLI `resume` on
  the same run.
- In PostgreSQL mode, every process acting on a run, the CLI included, also
  holds the run's database claim ([section 9.2](#92-claiming-a-run-for-update-skip-locked)).
- It is a same-host mechanism by design ([assumption 2](#33-assumptions)).

## 9. PostgreSQL coordination

### 9.1 Tables (illustrative; not applied)

This becomes a numbered migration only after approval. It reuses the binding
pattern that `0001_initial.sql` already uses between decisions and requests.

```sql
-- Unique keys that the foreign keys below reference.
ALTER TABLE approval_requests  ADD CONSTRAINT approval_requests_run_member_key
    UNIQUE (project_id, run_id, id);
ALTER TABLE approval_decisions ADD CONSTRAINT approval_decisions_outcome_key
    UNIQUE (project_id, request_id, action_fingerprint, outcome);

CREATE TABLE action_executions (
    id                     text        NOT NULL,   -- axe-…, derived (section 4.1)
    project_id             text        NOT NULL,
    run_id                 text        NOT NULL,
    request_id             text        NOT NULL,
    action_fingerprint     text        NOT NULL,
    decision_outcome       text        NOT NULL DEFAULT 'approved',
    node                   text        NOT NULL,   -- for the lapse rule (section 8.4)
    visit                  integer     NOT NULL,
    tool                   text        NOT NULL,
    effect_class           text        NOT NULL,
    idempotency            text,
    max_attempts           integer     NOT NULL,   -- per authorization
    state                  text        NOT NULL DEFAULT 'authorized',
    authorization_seq      integer     NOT NULL DEFAULT 1,  -- 1 = the approval; +1 per person-authorized retry
    authorization_attempts integer     NOT NULL DEFAULT 0,  -- dispatches under the current authorization
    attempts               integer     NOT NULL DEFAULT 0,  -- dispatches in total
    key_generation         integer     NOT NULL DEFAULT 1,
    authorized_until       timestamptz,            -- the authorization window, checked in the dispatch CAS
    open_attempt           integer,                -- the attempt in flight
    owner                  text,                   -- worker id: host:pid:random, never a secret
    owner_fence            bigint,
    result                 jsonb,                  -- replayable outcome, size-bounded
    lapse_reason           text,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,
    finished_at            timestamptz,
    CONSTRAINT action_executions_pkey PRIMARY KEY (id),
    CONSTRAINT action_executions_one_per_approval UNIQUE (project_id, request_id),
    CONSTRAINT action_executions_fingerprint_key UNIQUE (id, action_fingerprint),
    CONSTRAINT action_executions_request_fkey FOREIGN KEY (project_id, run_id, request_id)
        REFERENCES approval_requests (project_id, run_id, id),
    -- I4: only an APPROVED decision for the SAME exact action can have an execution.
    CONSTRAINT action_executions_approved_binding_fkey
        FOREIGN KEY (project_id, request_id, action_fingerprint, decision_outcome)
        REFERENCES approval_decisions (project_id, request_id, action_fingerprint, outcome),
    CONSTRAINT action_executions_only_approved CHECK (decision_outcome = 'approved'),
    CONSTRAINT action_executions_id_format CHECK (id ~ '^axe-[0-9a-f]{20}$'),
    CONSTRAINT action_executions_state_valid CHECK (
        state IN ('authorized', 'dispatching', 'succeeded', 'failed', 'uncertain', 'lapsed')),
    CONSTRAINT action_executions_effect_valid CHECK (
        effect_class IN ('none', 'idempotent', 'non_idempotent')),
    CONSTRAINT action_executions_idempotency_valid CHECK (
        (effect_class = 'idempotent' AND idempotency IN ('natural', 'keyed'))
        OR (effect_class <> 'idempotent' AND idempotency IS NULL)),
    -- I3: class C gets one automatic attempt per authorization.
    CONSTRAINT action_executions_class_c_single CHECK (effect_class <> 'non_idempotent' OR max_attempts = 1),
    CONSTRAINT action_executions_attempts_bounded CHECK (
        authorization_attempts BETWEEN 0 AND max_attempts AND attempts >= authorization_attempts),
    CONSTRAINT action_executions_dispatch_owned CHECK (
        (state = 'dispatching') = (open_attempt IS NOT NULL AND owner IS NOT NULL AND owner_fence IS NOT NULL)),
    CONSTRAINT action_executions_finished_iff_final CHECK (
        (state IN ('succeeded', 'failed', 'lapsed')) = (finished_at IS NOT NULL)),
    CONSTRAINT action_executions_lapse_reason CHECK ((state = 'lapsed') = (lapse_reason IS NOT NULL))
);

CREATE TABLE action_execution_attempts (
    execution_id      text        NOT NULL,
    attempt           integer     NOT NULL,
    authorization_seq integer     NOT NULL,
    owner             text        NOT NULL,
    owner_fence       bigint      NOT NULL,
    idempotency_key   text,
    dispatched_at     timestamptz NOT NULL,   -- database time of the write-ahead commit
    ended_at          timestamptz,
    result            text,                   -- succeeded | failed | not_dispatched | indeterminate
    detail_code       text,                   -- curated; never raw exception text
    CONSTRAINT action_execution_attempts_pkey PRIMARY KEY (execution_id, attempt),
    CONSTRAINT action_execution_attempts_execution_fkey FOREIGN KEY (execution_id)
        REFERENCES action_executions (id),
    CONSTRAINT action_execution_attempts_positive CHECK (attempt >= 1),
    CONSTRAINT action_execution_attempts_result_valid CHECK (
        result IS NULL OR result IN ('succeeded', 'failed', 'not_dispatched', 'indeterminate')),
    CONSTRAINT action_execution_attempts_ended_iff_result CHECK ((ended_at IS NULL) = (result IS NULL))
);
-- I1: at most one attempt in flight per execution.
CREATE UNIQUE INDEX action_execution_attempts_one_open
    ON action_execution_attempts (execution_id) WHERE ended_at IS NULL;

CREATE TABLE action_reconciliations (
    execution_id       text        NOT NULL,
    attempt            integer     NOT NULL,   -- the uncertain attempt
    action_fingerprint text        NOT NULL,   -- echoed by the person; must be the execution's
    resolution         text        NOT NULL,
    actor_kind         text        NOT NULL,
    actor_id           text        NOT NULL,
    actor_display_name text        NOT NULL,
    actor_verified     boolean     NOT NULL,
    decided_at         timestamptz NOT NULL,   -- server time
    comment            text        NOT NULL DEFAULT '',
    evidence           jsonb       NOT NULL DEFAULT '{}',   -- e.g. a probe result the person saw
    CONSTRAINT action_reconciliations_pkey PRIMARY KEY (execution_id, attempt),  -- one resolution per attempt
    CONSTRAINT action_reconciliations_attempt_fkey FOREIGN KEY (execution_id, attempt)
        REFERENCES action_execution_attempts (execution_id, attempt),
    CONSTRAINT action_reconciliations_binding_fkey FOREIGN KEY (execution_id, action_fingerprint)
        REFERENCES action_executions (id, action_fingerprint),
    CONSTRAINT action_reconciliations_resolution_valid CHECK (
        resolution IN ('confirmed_succeeded', 'confirmed_not_executed', 'retry_authorized')),
    CONSTRAINT action_reconciliations_by_a_person CHECK (actor_kind IN ('user', 'unauthenticated')),
    CONSTRAINT action_reconciliations_unauthenticated_unverified CHECK (
        NOT (actor_kind = 'unauthenticated' AND actor_verified))
);

CREATE TABLE run_claims (
    project_id   text        NOT NULL,
    run_id       text        NOT NULL,
    wanted_since timestamptz,           -- set: the run needs a worker (decision, reconciliation, retry due)
    available_at timestamptz NOT NULL,  -- backoff: not before this time
    holder       text,                  -- worker id
    fence        bigint      NOT NULL DEFAULT 0,   -- increments on every claim
    lease_until  timestamptz,
    heartbeat_at timestamptz,
    CONSTRAINT run_claims_pkey PRIMARY KEY (project_id, run_id),
    CONSTRAINT run_claims_run_fkey FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, run_id),
    CONSTRAINT run_claims_holder_has_lease CHECK ((holder IS NULL) = (lease_until IS NULL))
);

-- Indexes, each named for the query it serves.
CREATE INDEX run_claims_ready_idx ON run_claims (available_at) WHERE wanted_since IS NOT NULL;   -- claim
CREATE INDEX run_claims_lease_idx ON run_claims (lease_until) WHERE holder IS NOT NULL;          -- expired claims
CREATE INDEX action_executions_run_idx ON action_executions (project_id, run_id, state);        -- recovery, lapse
CREATE INDEX action_executions_uncertain_idx
    ON action_executions (project_id, updated_at) WHERE state = 'uncertain';                    -- "needs your check"
CREATE INDEX action_executions_dispatching_idx
    ON action_executions (updated_at) WHERE state = 'dispatching';                              -- monitoring
```

Attempt rows are inserted once at dispatch and closed once. They are never
deleted, and the application role could be denied `DELETE` on them.

### 9.2 Claiming a run (`FOR UPDATE SKIP LOCKED`)

This is one short transaction, committed immediately:

```sql
WITH candidate AS (
    SELECT project_id, run_id
    FROM run_claims
    WHERE wanted_since IS NOT NULL
      AND available_at <= now()
      AND (holder IS NULL OR lease_until < now())
    ORDER BY available_at, project_id, run_id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE run_claims c
SET holder = $worker_id, fence = c.fence + 1, lease_until = now() + $lease, heartbeat_at = now()
FROM candidate
WHERE c.project_id = candidate.project_id AND c.run_id = candidate.run_id
RETURNING c.project_id, c.run_id, c.fence;
```

- `SKIP LOCKED` lets several workers poll the same table without blocking on
  rows another worker is claiming at that moment.
- **The claim is the committed holder, fence, and lease, not the row lock.**
  The row lock ends at commit, and the claim persists.
- After committing the claim, the worker takes the run's file lock. If it
  cannot (a live process holds it, such as an operator's CLI or a zombie), it
  releases the claim with a backoff and moves on.
- Wake-ups set `wanted_since` and `available_at` in the same transaction as the
  write that triggered them: a decision recorded, a reconciliation resolved, or
  a retry falling due. This is the transactional-outbox pattern.

### 9.3 The dispatch gate: compare-and-set

```sql
BEGIN;  -- READ COMMITTED
SET LOCAL synchronous_commit = on;
UPDATE action_executions e
SET state = 'dispatching',
    attempts = e.attempts + 1, authorization_attempts = e.authorization_attempts + 1,
    open_attempt = e.attempts + 1, owner = $worker_id, owner_fence = $fence, updated_at = now()
WHERE e.id = $execution_id
  AND e.state = 'authorized'
  AND e.authorization_attempts < e.max_attempts
  AND (e.authorized_until IS NULL OR now() < e.authorized_until)
  AND EXISTS (SELECT 1 FROM run_claims c
              WHERE c.project_id = e.project_id AND c.run_id = e.run_id
                AND c.holder = $worker_id AND c.fence = $fence AND c.lease_until > now()
              FOR SHARE)
RETURNING e.open_attempt, e.authorization_seq, e.key_generation;
-- No row returned: DO NOT dispatch.
INSERT INTO action_execution_attempts
    (execution_id, attempt, authorization_seq, owner, owner_fence, idempotency_key, dispatched_at)
VALUES ($execution_id, $open_attempt, $authorization_seq, $worker_id, $fence, $key, now());
COMMIT;  -- the side effect may be dispatched only after this commit is acknowledged
```

Why exactly one compare-and-set wins:

- The second of two concurrent updates on the row waits for the first to
  commit.
- Under READ COMMITTED, PostgreSQL then re-evaluates the `WHERE` clause against
  the newly committed version of the row. `state = 'authorized'` no longer
  holds, so the second update changes nothing.
- `FOR SHARE` on the claim row makes a concurrent takeover of the claim wait
  for this transaction, and vice versa, so a stale fence cannot win.

In checkpoint mode, the run lock plays the role of the row lock.

### 9.4 Recording an outcome

```sql
BEGIN;
SET LOCAL synchronous_commit = on;
UPDATE action_executions
SET state = $new_state,              -- succeeded | failed | authorized (retry) | uncertain
    result = $replayable_result, open_attempt = NULL, owner = NULL, owner_fence = NULL,
    finished_at = CASE WHEN $new_state IN ('succeeded', 'failed') THEN now() END,
    updated_at = now()
WHERE id = $execution_id AND state = 'dispatching'
  AND open_attempt = $attempt AND owner = $worker_id AND owner_fence = $fence;
UPDATE action_execution_attempts
SET ended_at = now(), result = $attempt_result, detail_code = $code
WHERE execution_id = $execution_id AND attempt = $attempt AND ended_at IS NULL;
COMMIT;
```

If the first update changes no row, the writer has lost its claim. It writes
nothing more and stops working on the run.

### 9.5 Recovery

Recovery runs first in every resume, by the holder of the run lock and claim,
before any decision is applied:

```sql
SELECT e.id, e.effect_class, e.idempotency, e.open_attempt, a.dispatched_at, a.idempotency_key
FROM action_executions e
JOIN action_execution_attempts a ON a.execution_id = e.id AND a.attempt = e.open_attempt
WHERE e.project_id = $project_id AND e.run_id = $run_id AND e.state = 'dispatching'
FOR UPDATE OF e;
```

Holding the run's file lock proves the previous owner's process has exited.
For each open attempt:

1. **Look for local outcome evidence.** If the execution log holds an
   `action_outcome` for this execution and attempt, record it. That outcome is
   known.
2. **Otherwise, a conclusive probe if declared.** `delete_file`: the path is
   absent, so `succeeded`; the path is present, so retry. A positive-only
   probe can confirm `succeeded` and never anything else.
3. **Otherwise, the class policy** ([section 6.3](#63-retry-policy)):
   - class A, or class B within limits: close the attempt as
     `indeterminate`, and return the execution to `authorized`;
   - class C, or class B out of window or out of attempts: close the attempt
     as `indeterminate`, and mark the execution `uncertain`.

The same recovery also re-creates, idempotently, an `authorized` execution
that is missing for an approved decision the checkpoint has already applied.
That happens when the process died between the two writes.

**No late reports are needed.** Recovery can declare `uncertain` only while
holding the run lock (I6). A process that could still report a late outcome
would still hold the lock, so under assumption 2 no process exists that could
contradict a recovery decision.

### 9.6 Leases, heartbeats, fencing, and clocks

- **Lease.** The default is the larger of 60 s and three times the longest
  `dispatch_timeout` among the run's tools. A heartbeat every lease/3 extends
  it, with a fenced update.
- **Lease expiry only frees the claim.** It never declares anything uncertain
  by itself (I6). The file lock decides whether a takeover can actually happen.
- **Fencing.** The fence increments on every claim. Every ledger write carries
  the holder and fence, and is conditional on them. A stale owner's write
  changes nothing (zero rows), and that owner must stop working on the run.
- **Clocks.** Every lease, expiry, and ordering comparison uses PostgreSQL
  `now()`: one clock. Host clocks are used only for display.
- **Timeouts.** Ledger transactions use `statement_timeout` and `lock_timeout`,
  so a wedged transaction cannot block forever.
  `idle_in_transaction_session_timeout` is a backstop. A transaction is never
  held open across a dispatch.

### 9.7 Alternatives evaluated

| Mechanism | How it would work | Verdict |
| --- | --- | --- |
| `SELECT … FOR UPDATE SKIP LOCKED` plus committed claim columns (lease, fence) | A short claim transaction. The claim persists after commit, and leases recover crashed workers. | **Adopted** for handing runs to workers |
| Holding a `FOR UPDATE` row lock across the side effect, in one long transaction | The lock is released automatically if the worker dies | **Rejected.** The write-ahead record cannot be committed while the transaction is open. A crash rolls it back, and a successor sees `authorized` and dispatches again: exactly the duplicate this design prevents. It also pins a connection, blocks vacuum, and hits idle-in-transaction timeouts. |
| Single-row compare-and-set `UPDATE … WHERE state = 'authorized'` | The row lock plus the READ COMMITTED re-check admits one winner | **Adopted** as the dispatch gate |
| Session advisory locks (`pg_try_advisory_lock`) | A lock per run, released automatically when the session ends | **Not relied on.** They are not transactional with ledger state. They are released when the connection drops (a database restart, a network blip) while the process may still be dispatching. They don't work with transaction-mode connection poolers, and their keys are 64-bit hashes. Possibly useful later as a liveness hint. |
| SERIALIZABLE isolation | Serialization failures instead of re-checks | **Not needed.** The invariants are single-row compare-and-sets plus unique indexes, and READ COMMITTED semantics are simpler to reason about and test. |
| LISTEN/NOTIFY | Wake workers immediately | **Optional**, for latency. Polling stays the source of truth, because notifications are lost while a worker is down. |
| An external queue (SQS, Redis, Kafka) | The queue delivers work items | **Not needed now.** It adds a second durable store with the same at-least-once semantics, and the ledger compare-and-set would still be required ([section 12.3](#123-the-future-worker)). |

### 9.8 Defense layers

| Layer | Prevents | Limit |
| --- | --- | --- |
| Run claim (`SKIP LOCKED`, lease, fence) | Two workers picking the same run at once | A lease can expire while its holder is alive, which leaves it to the next layers |
| Run file lock (OS) | Two engine instances on one run on this host, the CLI included | Same host only; unreliable on network filesystems |
| Dispatch compare-and-set | Two dispatches of the same attempt, **even if both layers above failed** | None for dispatch exclusivity, given durable commits |
| Fenced writes | A stale (zombie) owner's writes after a takeover | — |
| Local write-ahead entry (execution log) | A re-dispatch after database data loss | Losing the local disk as well |

## 10. Crash matrix

The ledger is PostgreSQL. In checkpoint mode, rows 9–14 collapse into the run
lock. The **Effects so far** column is the ground truth, which Agent Factory
may not know.

| # | Crash point | Durable evidence afterwards | Agent Factory **knows** | Can only **infer** | Recovery: A / B / C | Effects so far |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Before any claim | Decision; execution `authorized`, or not yet created | No dispatch happened: there are no attempt rows, and a dispatch requires one | — | Any worker claims later; the same for all classes | 0 |
| 2 | After the run claim, before the dispatch compare-and-set | Claim (holder, fence, lease); execution `authorized` | No dispatch happened | The holder is gone (lease expired). Used only to free the claim. | The next claim (fence + 1) proceeds normally | 0 |
| 3 | After the compare-and-set commits, before the local entry or before sending | Open attempt row | A dispatch was permitted | That the owner is gone, which the run lock *proves*. It cannot know nothing was sent. | A: retry. B: retry with the same key. C: `uncertain` (a false positive; the person will find nothing happened). | 0, which cannot be known |
| 4 | While the side effect is in progress | Open attempt; usually a local `action_dispatch` entry | The same as row 3 | Whether the request arrived, and whether it will still complete (it may be in flight) | A: retry. B natural: retry. B keyed: retry with the same key (the target deduplicates concurrent duplicates or reports "in progress", so back off). C: `uncertain`. Any probe must allow for completion in flight. | 0, 1, or partial |
| 5 | After the side effect, before any outcome is persisted | The same as row 4 | The same: indistinguishable from row 4 | The same | The same. A keyed retry returns the original result (effectively once). C: a person, or a positive probe finds it. | 1 |
| 6 | After the local outcome, before the ledger outcome | Open attempt in the database; `action_outcome` in the execution log | **The outcome**, from the fsync'd log | — | The successor copies the outcome into the ledger. No person needed, for any class. | 1, or 0 on failure |
| 7 | After the ledger outcome, before the node checkpoint | `succeeded`/`failed` with a replayable result; the node is not committed | The outcome | — | Resume re-runs the node, and the gateway **replays**. No dispatch. | 1 |
| 8 | After the node checkpoint, before the worker clears its work item | Everything recorded; the claim is still wanted | Done | — | The worker reprocesses the run: nothing to apply or dispatch, and it clears the item | 1 |
| 9 | Two workers racing | Claims serialized by `SKIP LOCKED` and fences | PostgreSQL admits one compare-and-set winner per attempt | — | The loser does nothing and backs off | ≤ 1 per attempt |
| 10 | A zombie worker (alive, lease expired) | Its old claim can be taken in the database | The file lock stays with the zombie on this host, so no second engine runs on that run | That the zombie is dead, which can be wrong and is harmless | If another process did get the claim, the zombie's fenced writes fail and it stops | ≤ 1 |
| 11 | Process restart, at any of rows 1–8 | As in that row | As in that row | As in that row | The new process is a new owner (new ID, new fence) and runs recovery first | As in that row |
| 12 | Database restart before the compare-and-set commit is acknowledged | The transaction rolled back, or committed with the acknowledgement lost | The *same live process* knows it has not dispatched | Whether the commit landed, which it resolves by re-reading | Reconnect and read the row. If it shows this attempt and fence, proceed (nothing was sent); otherwise don't dispatch. If the process dies first, row 3 applies. | 0 |
| 13 | Database restart during the dispatch or before the outcome write | Open attempt; local outcome once the tool returns | What the owner holds in memory, then on disk | — | The owner keeps the run lock, writes the local outcome, and retries the ledger write until the database returns. If it dies meanwhile, row 6 applies. Session state such as advisory locks would be lost, which is why it is not relied on. | As reported |
| 14 | Database data loss (restore, asynchronous failover) | Ledger rows may regress or vanish | The local `action_dispatch` entry for this execution | — | Before any dispatch, the gateway also checks the run's execution log for an `action_dispatch` entry for this execution. If there is one, it refuses and declares the attempt `indeterminate`; A and B may then retry per policy. | ≤ 1 for C, unless both stores are lost |
| 15 | Workspace loss (checkpoint and log gone) | Ledger only | The ledger state | — | The run cannot resume at all, because there is no engine state. Nothing re-dispatches, and a person decides. | Unchanged |
| 16 | An operator runs CLI `resume` during worker processing | — | — | — | The file lock refuses the second process | Unchanged |

## 11. Reconciliation

### 11.1 When

An execution enters `uncertain` in any of these cases:

- class C, after any `indeterminate` attempt;
- class B keyed, outside its key retention or out of attempts;
- any class, when a tool reports a partial effect.

### 11.2 What the run does

The engine puts the run in a new status, `awaiting_reconciliation`. That is an
additive change to the engine's status, the database CHECK, and the API's
`RunStatus`, and it is an owner decision ([section 16](#16-decisions-for-the-owner)).
`resume` refuses while any execution of the run is `uncertain`
(`ReconciliationPending`, like `ApprovalPending`).

Why not reuse an existing status:

- `blocked` means "an infrastructure problem; resumes when fixed";
- `awaiting_approval` means "a *new* action awaits a decision";
- neither describes "a past action's outcome is unknown, and only a person can
  determine it".

### 11.3 Resolutions

There is exactly one resolution per uncertain attempt:

| The person chooses | Recorded resolution | Execution becomes | The run then |
| --- | --- | --- | --- |
| "It happened" | `confirmed_succeeded` | `succeeded` (reconciled) | Resumes. The re-run node is told the action succeeded: a replayed confirmation, with no dispatch. |
| "It did not happen; don't do it" | `confirmed_not_executed` | `failed` (reconciled) | Resumes. The re-run node gets a definitive refusal, like a rejected approval. |
| "It did not happen; try once more" | `retry_authorized` | `authorized`, with `authorization_seq + 1`, and one more attempt allowed (class C: exactly one) | Resumes, and dispatches once more |
| Nothing | — | Stays `uncertain` | Stays paused, with reminders. A person may stop the run (future run cancellation), which never asserts what happened. |

There is no "I don't know; just retry" option: a retry requires asserting that
the action did not happen. The screen's wording must make that consequence
explicit.

### 11.4 Authority and binding

- **Reconciliation is at least as sensitive as approval.** Through the API it
  requires an authenticated person with `approvals:decide`, or a dedicated
  permission (an owner decision). That depends on the authentication milestone.
  Step-up authentication is recommended for `retry_authorized`, on the phone
  especially.
- **The request echoes the action fingerprint and the attempt number** shown on
  screen, as decisions do, and a mismatch is refused. The person resolves
  exactly the attempt they saw.
- **The server controls the actor and the time.** There is one resolution per
  attempt (the primary key). An identical retry is idempotent, and a
  conflicting second resolution gets 409.
- **`retry_authorized` is a new authorization** for one more attempt of the
  same exact action, bound to the same fingerprint and execution ID. It has its
  own short validity window (a policy, e.g. 1 hour), and lapses if unused.
- **The local CLI may reconcile in checkpoint mode**, recorded as the
  unauthenticated local operator, consistent with CLI approvals today. It is
  never recorded as `system`.

### 11.5 Evidence and probes

What the person is shown:

- the exact action: tool, category, and parameters. Long values appear as
  hashes, with "show full content" offered only on authenticated screens;
- the project, run, agent, and node;
- the original approval: who, and when;
- the attempt number and dispatch time, in database time;
- what Agent Factory knows and does not know, in plain words;
- the external reference to look up (the idempotency key or operation ID);
- any probe result.

Probes:

- **Positive-only.** For example, "a payment with our reference exists". A
  positive result resolves the execution to `succeeded` automatically, recorded
  as evidence and visible in the history. A *negative* result never authorizes
  a class C retry, because the request could still be in flight, or the probe
  could be stale. It is shown as evidence, and the execution stays `uncertain`.
- **Conclusive.** For local, synchronous effects whose owner process is proven
  gone: `delete_file` checks whether the path is absent. It can resolve either
  way, which is why `delete_file` never needs a person.

### 11.6 Interaction with approval expiry

- **A dispatch may begin only while the authorization is valid.**
  `authorized_until`, copied from the request's `expires_at` or the retry
  window, is checked in the compare-and-set using database time. An authorized
  execution whose window closes becomes `lapsed`, never dispatched. So **an
  expired approval is never executable.**
- **Once dispatching, expiry does not interrupt.** A dispatched effect cannot
  be recalled; its outcome is recorded whenever it is known.
- **Reconciliation has no deadline by default.** It is about the past, and the
  run simply stays paused. `retry_authorized` creates a fresh, short window.
- **The expiry policy itself belongs to the auth/worker milestone**: the
  default lifetime, and what happens to a rejection after expiry. This design
  only requires that the start of an execution is checked against it.

## 12. Relationships

### 12.1 `ApprovalRequest` and `ApprovalDecision`

- One approved decision has at most one `ActionExecution`
  (`UNIQUE (project_id, request_id)`).
- The foreign key includes the outcome, so a rejected decision cannot have an
  execution. It also includes the fingerprint, which keeps the execution bound
  to the exact action the person saw.
- Rejected, expired, and cancelled requests have no execution. For a rejection,
  the gateway raises `PermissionDenied`, as today.
- Decision records stay immutable. Execution state and reconciliations are
  separate records.
- The phone shows both facts: "Approved by Dana at 06:40 · ran at 06:41 ·
  succeeded".

### 12.2 `ToolGateway`

- It stays the single path from an agent to a side effect. The ledger is
  called only from the gateway, after the policy and approval checks and
  immediately before `tool.run`. Tools never see the ledger.
- The gateway recomputes the fingerprint from the actual call, and requires
  all three of these:
  - the engine's approval record for this request says approved;
  - the ledger execution, whose ID derives from that fingerprint, is
    `authorized`;
  - the compare-and-set succeeds.

  A database row alone never authorizes, because the engine's record must
  match. In PostgreSQL mode the engine's record alone never authorizes either,
  because the ledger must agree.
- The new pre-dispatch and outcome log events close today's audit gap
  ([section 1.2](#12-evidence)).
- **Auto-allowed tools are unchanged**: they get no ledger. Their re-execution
  on node re-run remains v0.1 semantics, a documented limitation
  ([section 15](#15-remaining-risks)).

### 12.3 The future worker

The worker loop, as a sketch:

1. Claim a wanted run ([section 9.2](#92-claiming-a-run-for-update-skip-locked)),
   then take the run's file lock. If the lock is unavailable, release the claim
   with a backoff.
2. Run recovery ([section 9.5](#95-recovery)): resolve open attempts from local
   evidence, probes, and the class policy.
3. Apply stored decisions and expiries to the checkpoint. This is the existing
   `reconcile` path, where the fingerprint is re-checked against the engine's
   pending action, and it creates `authorized` executions.
4. Resume the graph. The gateway dispatches under the claim's fence.
5. Heartbeat the claim while working. Stop at once if a fenced write fails:
   the claim was lost.
6. On a pause or finish, lapse unused executions, clear `wanted_since`, and
   release the claim and the lock.

Polling PostgreSQL is enough at this stage. In a future AWS deployment, SQS
could carry wake-ups (at-least-once; duplicates are harmless) while PostgreSQL
remains the ledger and the claim authority. The dispatch compare-and-set does
not change.

A run is processed only on the host that holds its workspace
([assumption 2](#33-assumptions)). A host-affinity column on `run_claims` can
come later.

### 12.4 Executor, checkpoint, and recorder

- **Checkpoint schema v3**: `executions` (checkpoint mode) or cached outcomes
  (replay in PostgreSQL mode), plus the `execution_ledger` choice. An older
  engine refuses v3, so it can never ignore a single-use marker.
- **The run lock** is taken in the executor's entry points.
- **The `awaiting_reconciliation` status**, with `ReconciliationPending`.
- **The lapse rule** runs at node commit.
- **The recorder** projects execution states into milestones
  ([section 13.2](#132-milestones)).
- **A database.md change, when implemented.** It currently says the checkpoint
  is the source of truth, with approval decisions as the one exception. Action
  executions become a second exception, written to the ledger before the
  engine acts ([database.md](../database.md#engine-checkpoint-vs-product-database)).
- **Upgrading existing checkpoints.** An approved approval without an execution
  record becomes `authorized`. That is safe today only because the single gated
  tool, `delete_file`, is class B natural. The argument must be made again if a
  class C tool ships before the upgrade.

## 13. Observability, milestones, and the phone

### 13.1 Events

| Event | Execution log (local, hash-chained) | Ledger | Milestone (phone timeline) | Notification (future) |
| --- | --- | --- | --- | --- |
| Action approved | `approval_decision` (exists) | Execution `authorized` | `human_decision` completed: "Approved by Dana" (exists) | — |
| Action claimed | `run_claimed` (worker ID, fence) | `run_claims` holder and fence | — (internal) | — |
| Action started | `action_dispatch`, **before** sending | `dispatching`, attempt row | `action_execution` in progress: "Running: delete legacy_calc.py" | — |
| Action succeeded | `action_outcome`, ok | `succeeded` | `action_execution` completed: "Done: deleted legacy_calc.py" | Optional digest |
| Action failed | `action_outcome`, failed | `failed` | `action_execution` failed, with a curated reason | Yes |
| Outcome uncertain | `action_outcome_uncertain` | `uncertain` | `action_execution` blocked: "Outcome uncertain" | Yes, urgent |
| Reconciliation required | `reconciliation_requested` | (the same) | Run status `awaiting_reconciliation` | Yes, linking to the reconciliation screen |
| Reconciled | `action_reconciled` (actor, resolution) | Reconciliation row; new state | `action_execution` moves to completed, failed, or in progress | — |
| Lapsed | `action_lapsed` | `lapsed` | `human_decision` metadata: "approved, not used" | — |

### 13.2 Milestones

- A new built-in milestone type, `action_execution` (category `approval`), with
  key `action:{execution_id}`: one per execution.
- It moves only through transitions that `MILESTONE_TRANSITIONS` already
  allows: in progress to completed, failed, or blocked; blocked to completed,
  failed, or in progress (a retry).
- Summaries are curated: the tool and display-safe parameters, never raw
  errors.

### 13.3 Logs and metrics

- **Worker logs** are JSON lines carrying the worker ID, run, execution ID,
  attempt, fence, and transition. Idempotency keys are not secret and may be
  logged for support.
- **Metrics**: executions by state; the age of the oldest `dispatching`
  attempt; the `uncertain` count (alert above zero); lapses; retries;
  compare-and-set losses (contention); claim takeovers.

### 13.4 On the phone

The approval card gains an execution timeline:

```text
┌ Approval · Client ETL Pipeline ───────────────┐
│ deployer wants to run deploy_release          │
│ environment: production · release 2026.10.02-1│
│ Risk: Changes what real users run in prod.    │
│                                               │
│ ● Requested            06:16                  │
│ ● Approved by Dana     06:40                  │
│ ● Running              06:41                  │
│ ● Done                 06:42                  │
└───────────────────────────────────────────────┘
```

The reconciliation screen, in the same inbox, a distinct kind:

```text
┌ Needs your check · Invoice Calculator ────────┐
│ Agent Factory started this approved action    │
│ and stopped before it could record the result.│
│                                               │
│ send_email to billing@example.test            │
│ approved by Dana 06:40 · started 06:41:07     │
│                                               │
│ Known:   the send was started once.           │
│ Unknown: whether the email went out.          │
│ Reference: af1-axe-…-k1                       │
│                                               │
│ [ It was sent ]                               │
│ [ It was not sent: don't send ]               │
│ [ It was not sent: send once more ] (re-auth) │
└───────────────────────────────────────────────┘
```

Rules for the phone:

- A notification carries no secrets and no parameters; for example, "Agent
  Factory needs your check on Invoice Calculator".
- Its link opens an authenticated screen, with step-up before a retry.
- Times are shown in local time.
- Nothing is resolved on the person's behalf, except by a conclusive or
  positive probe, and that is shown in the history.

## 14. Testing strategy

1. **Domain unit tests.** Every legal and illegal state transition; the
   invariants; ID derivation (stable, scoped, bound to the fingerprint); the
   key-generation rules.
2. **Ledger contract tests.** One suite run against both adapters, checkpoint
   and PostgreSQL, as `test_domain_repositories.py` does today.
3. **Gateway protocol tests** with a counting fake tool per class:
   - A;
   - B natural;
   - B keyed, against a fake service that deduplicates;
   - C.

   The oracle is the number of real effects.
4. **Crash injection with real process death.**
   - A child Python process runs the executor with a named fault point, which
     calls `os._exit(137)` at each boundary of [section 10](#10-crash-matrix)
     (rows 1–8 and 12–14). A fresh process then resumes.
   - Assertions: the oracle counts (class C ≤ 1 without a person; A and B as
     declared), the final states, and that `uncertain` appears exactly where
     section 10 says.
   - Fast in-process variants use a `BaseException`, as the experiment did.
   - These are trusted test processes, never hostile fixtures, and they don't
     use Docker.
5. **Races.**
   - N threads and M processes race the dispatch compare-and-set against
     PostgreSQL: exactly one winner.
   - Two processes resume one run: the file lock refuses the second.
   - A zombie with a stale fence: its writes are rejected.
6. **Database faults.**
   - `pg_terminate_backend` on the worker's connection at each boundary, the
     technique `test_postgres_engine.py` already uses.
   - The ambiguous-commit re-read logic, unit-tested with a fake connection.
   - Restore-from-backup, simulated by regressing ledger rows: the local
     write-ahead entry must block a re-dispatch.
7. **Model checking.** A small explicit-state model of the protocol in plain
   Python: two workers, a crash at every step, a database restart, lease
   expiry. It is explored exhaustively up to a bound, and checks I1–I8 plus
   "class C effects ≤ 1 without a person" in every reachable state. Standard
   library only.
8. **Regression.** The two experiment cases ([section 1.2](#12-evidence))
   become tests that require exactly one dispatch.
9. **Reconciliation.** Binding (the echoed fingerprint and attempt); one
   resolution per attempt; server-controlled actor and time; permissions; each
   resolution's effect on the run; the retry window lapsing.
10. **Lapse.** A re-run that does not repeat the action lapses at node commit;
    expiry before dispatch lapses; the end of a run lapses.
11. **Manual mutation checks**, each of which must fail a test:
    - dispatch before the compare-and-set commits;
    - drop `state = 'authorized'` from the compare-and-set;
    - skip the fence check;
    - treat a recovered class C `dispatching` as `authorized`;
    - skip the local-evidence check;
    - allow class C `max_attempts > 1`.
12. **The existing suites:** non-live, PostgreSQL, live smoke, adversarial, and
    `sandbox check`, because gateway changes touch the security core.

## 15. Remaining risks

1. **External idempotency is trusted, not verified.** Key scope, retention,
   cached failures, and handling of concurrent duplicates differ between
   providers. Mitigation: reviewed per-tool declarations, conservative
   retention, and fakes that model each documented behaviour.
2. **A person can reconcile wrongly**: "it didn't happen" when it did, so a
   retry duplicates it. Mitigation: evidence and probes, explicit wording,
   step-up, and audit. Keyed tools reuse the key across reconciliation unless
   the provider caches failures.
3. **Partial effects of non-atomic operations**, such as batch sends or
   multi-step deploys. Design tools so that one action is one atomic external
   operation; otherwise the action is class C, and partial results are
   `uncertain`.
4. **Non-deterministic agents.** A re-run may not repeat an action, which
   causes lapses and more approval prompts. Caching model responses per node
   visit is a separate design.
5. **Other re-executions remain.** Auto-allowed workspace side effects
   (`write_file`, `git_commit`) and model calls still re-execute on node
   re-run. That is v0.1 semantics: the effects are local and mostly
   idempotent, but model calls cost money with a real provider. Extending the
   journal to every tool would amount to durable execution, a later decision.
6. **The single-host assumption for workspaces.** Network filesystems break
   the file-lock guarantees.
7. **Durability configuration.** `synchronous_commit`, `fsync`, failover
   without loss of acknowledged commits, directory fsync for checkpoints on
   POSIX, and disk write caches that lie.
8. **Double failure.** Losing both the database rows and the workspace's
   execution log can lose the evidence of a dispatch.
9. **Long-running external operations can outlast leases.** They need an
   asynchronous pattern: submit, record the operation ID durably, then poll.
   That is class B keyed with a status query, and is not designed in detail
   here.
10. **Complexity in the security core.** Mitigated by staged implementation,
    model checking, and mutation checks.

## 16. Decisions for the owner

1. Adopt the six-state model and its names.
2. Add the run status `awaiting_reconciliation` (engine, database CHECK, and
   API `RunStatus`, as an additive contract change in a later milestone), or
   reuse `blocked`.
3. Use `approvals:decide` for reconciliation, or add `actions:reconcile`.
4. Support the checkpoint ledger in CLI mode without a database (recommended,
   because it keeps v0.2 mode safe), or require PostgreSQL for approval-gated
   tools.
5. Require a dispatch to start before the approval expires (recommended), and
   choose the default validity window for `retry_authorized`.
6. Make undeclared tools class C, and refuse to register an undeclared tool in
   a dangerous category (both recommended).
7. Whether to extend journaling to auto-allowed side effects later.
8. The order of work: implement the engine half (stages E1–E2) before the
   worker loop, or together with it.

## 17. Proposed implementation stages

| Stage | Content | Exit criteria |
| --- | --- | --- |
| E1 | Tool execution-semantics declarations and registry validation. No change in behaviour. | Contract tests |
| E2 | Engine: run lock, checkpoint ledger, gateway protocol, replay, lapse, `awaiting_reconciliation`, CLI reconciliation (local operator), new log events | Both experiment cases give one dispatch; crash-injection suite in checkpoint mode; mutation checks; all suites, live included |
| E3 | PostgreSQL ledger: migration, repository, compare-and-set, recovery, claims | Race tests, connection-kill tests, PostgreSQL suite |
| E4 | The worker loop (auth-worker milestone), on top | The worker crash-matrix tests |
| E5 | API and phone: the execution timeline in approval views, and a reconciliation resource (contract-first, after authentication) | Contract and HTTP tests |

**Stage numbering, as built.** E1 and E2 were implemented together, and
included the PostgreSQL ledger planned above as E3 (without worker claims).
The owner's milestone plan then numbered the remaining work: **E3**
authentication and authorization (done); **E4** worker coordination: claims,
leases, fencing, and the worker loop (done: [workers.md](../workers.md));
**E5** the reconciliation endpoint and the execution timeline.

---

## Appendix A: Reproducing the experiment

The experiment ran against `cd8fb4a`, with temporary workspaces created by
`CALCULATOR_TEMPLATE.create_workspace`:

1. **Count real dispatches.** Wrap `DeleteFileTool.run` to append each call to
   a list before deleting.
2. **Case (a).** A `FakeProvider` whose developer response deletes
   `legacy_calc.py` and `old_a.py` (create the second file first). Then:
   - `start`;
   - `decide_approval(approved=True)` and `resume`;
   - `decide_approval(approved=True)` and `resume`.

   Result: `['legacy_calc.py', 'legacy_calc.py', 'old_a.py']`.
3. **Case (b).** One deletion; approve; arm the wrapper to raise a
   `BaseException` right after the deletion; `resume`. The executor's
   `except Exception` does not catch it, as with a real kill. Then build a new
   executor on the same workspace and `resume` again.

   Results:
   - dispatches: `['legacy_calc.py', 'legacy_calc.py']`;
   - the execution log holds one `delete_file` entry: `('ok', 'legacy_calc.py did not exist')`.
