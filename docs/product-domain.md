# Product domain: projects, runs, milestones, approvals

| | |
| --- | --- |
| Status | Implemented on branch `feature/product-data-foundation`. Durable PostgreSQL storage added on `feature/postgres-persistence` ([database.md](database.md)). **Both unreleased** |
| Code | [`agent_factory/domain/`](../agent_factory/domain/) |
| Tests | `tests/test_domain_model.py`, `test_domain_approvals.py`, `test_domain_repositories.py`, `test_domain_engine.py` |
| API | Exposed through the v1 contract ([api-contract.md](api-contract.md)) as client-shaped models, not as these classes. The contract is designed but not implemented |
| Not included | API server, UI, authentication, notifications, overnight-summary generation |

The engine's state (`ProjectState`) describes **one run, for executing it**.
The product needs answers to different questions:

- Which projects exist?
- What happened overnight, and what failed, was repaired, or is blocked?
- What is waiting for me?

The product domain provides those answers as typed, versioned records with
stable identities. The API, database, dashboard, mobile approval screen, and
notifications planned on the [roadmap](zoomcamp/ROADMAP.md) are meant to be
built on these records, which is why they came first.

## Contents

1. [Entities](#entities)
2. [Relationships](#relationships)
3. [Why there is no generic Event entity](#why-there-is-no-generic-event-entity)
4. [Milestones](#milestones)
5. [Approvals](#approvals)
6. [Reconciliation decisions](#reconciliation-decisions)
7. [Attention](#attention)
8. [The execution timeline](#the-execution-timeline)
9. [Action executions](#action-executions)
10. [Run status](#run-status)
11. [Run claims (workers)](#run-claims-workers)
12. [Records and schema evolution](#records-and-schema-evolution)
13. [Persistence boundary](#persistence-boundary)
14. [Engine integration](#engine-integration)
15. [Design issues found](#design-issues-found)
16. [Gaps before API work](#gaps-before-api-work)

## Entities

| Entity | Identity | Holds | Module |
| --- | --- | --- | --- |
| **Project** | `prj-` + 20 random hex characters, stored in the workspace marker (`.agent_factory/workspace.json`) | name, template, description, creation time, metadata, and its **milestone policy** | `project.py` |
| **Run** | `(project_id, run_id)`. `run_id` is the engine's run ID. | run number within the project (1, 2, …), graph name, status, status reason, created/updated times | `project.py` |
| **Milestone** | `ms-` + first 20 hex of SHA-256 over `(project_id, run_id, key)`. Deterministic. | per-run sequence, key, type, category, title, summary, status, `created_at`, `occurred_at`, source (node, agent, step), metadata, `resolves_id` | `milestones.py` |
| **ApprovalRequest** | `(project_id, id)`. `id` is the engine's exact-action request ID (`apr-` + 16 hex). | run, node, agent, exact action (tool, category, parameters), action fingerprint, reason, risk, `requested_at`, status, context, `expires_at`, `updated_at` | `approvals.py` |
| **ApprovalDecision** | `apd-` + first 20 hex of SHA-256 over `(project_id, request_id)`. At most one per request. | request, action fingerprint, outcome, `decided_at`, actor, comment | `approvals.py` |
| **Principal** | `prn-` + 20 random hex characters; its external identity is `(issuer, subject)`, unique | display name and email (display only), status (`active` or `disabled`), created/updated times, and granted **permissions** | `principals.py` |
| **ReconciliationDecision** | `(execution_id, attempt)`. At most one per uncertain attempt. | project, run, execution, attempt, action fingerprint, choice, actor, `decided_at`, comment: a person's answer, as **intent** (E5) | `reconciliation.py` |

Supporting value types:

| Type | Purpose | Module |
| --- | --- | --- |
| `MilestonePolicy` | A project's milestone vocabulary (`MilestoneCatalog` of `MilestoneDefinition`s) plus the `NodeMilestoneRule`s that reach each milestone | `milestones.py` |
| `ExactAction` | Tool, category, and parameters of a proposed action. `fingerprint` is SHA-256 over all three. | `approvals.py` |
| `Actor` | Who decided: kind (`unauthenticated`, `user`, `system`), ID, display name, `verified` flag | `identity.py` |
| `AttentionItem` | One thing that needs a person now: `decision_required` or `reconciliation_required` ([below](#attention)) | `attention.py` |
| `TimelineEntry` | One step in the life of an approved action, built from structured records ([below](#the-execution-timeline)) | `timeline.py` |

**Project identity is never a path.** The ID is random and stored inside
the workspace. Moving or renaming the workspace folder doesn't change which
project it is. Workspaces created by v0.2.0 or earlier have no ID; they get
one, written atomically, the first time they are used with a product store
(`Workspace.ensure_project_id`). An existing but invalid ID is never
silently replaced: something may already reference it.

## Relationships

```
Project 1 ───< Run 1 ───< Milestone
   │              │          └── resolves_id ──> an earlier FAILED milestone
   │              │                              (same run, same type)
   │              └───< ApprovalRequest 1 ───o ApprovalDecision (0 or 1)
   │                          ^
   │                          └── human_decision milestone (metadata.approval_id)
   └── MilestonePolicy: catalog of milestone types + node rules
```

- **A project has many runs.** Run numbers count up per project.
  `RunRepository.latest_for_project` gives the current one.
- **A run has many milestones.** `sequence` orders them within the run
  (1, 2, 3, …).
- **A repair is a relationship, not an edit.** A failed milestone stays
  failed forever. A later completed milestone of the same type in the same
  run points back at it with `resolves_id`. That answers "what was
  repaired?" without rewriting history.
- **A request has at most one decision.** The decision repeats the
  fingerprint of the action it decided.
- **Every child record carries `project_id`.** Run IDs and approval request
  IDs are only unique within a project; see
  [Design issues found](#design-issues-found).

## Why there is no generic Event entity

The brief asked whether a generic `Event` record is useful. It isn't, yet:

1. **An append-only event stream already exists.** The execution log is
   hash-chained, records every low-level step, and is the audit and debug
   record ([graph-and-state.md](graph-and-state.md#execution-log)). A second
   generic stream would duplicate it, or tempt future code to turn every log
   entry into a product record. The brief explicitly rules that out.
2. **Milestones are the curated, human-facing view.** A generic event type
   with a free-form payload would weaken the typed fields, state machine,
   and validation that make milestones trustworthy.
3. **No current consumer needs one.** Overnight questions are answered by
   milestones, runs, and approval requests; see
   [Answering the overnight questions](#answering-the-overnight-questions).

**When to revisit.** Notifications (roadmap Stage 9) will need a reliable
"something happened, tell someone" queue, usually called an *outbox*. That
would be a narrow, purpose-built record, not a general event model.

## Milestones

### Project-defined meaning

What counts as meaningful progress depends on the project, so the core code
holds no project-specific milestone list. A project's
**`MilestonePolicy`** has two parts:

- **Catalog**: the milestone types this project uses. Each type has a slug,
  a title, a category (`workflow`, `deliverable`, `approval`, `lifecycle`),
  a description, and an optional display order.
- **Rules**: a `NodeMilestoneRule` per workflow node. When the node finishes
  with a listed outcome, the milestone gets that status. Rules may map
  outcomes only to `in_progress`, `completed`, or `failed`. `record_blocked`
  says whether a blocked run at that node is shown on the milestone.

The policy lives on the `ProjectTemplate` (`milestone_policy`), and is copied
into the `Project` record when the project is first registered. After that
the **stored policy wins**, so editing a template never changes what an
existing project's earlier milestones mean.

Two **system types** are always present and reserved, because the engine
itself produces them:

- `human_decision`: a protected action waited for, then received, a
  decision.
- `run_finished`: the run reached a final state.

The **standard policy**, used by the standard
planner → developer → tester → reviewer graph:

| Node | Outcome | Milestone type | Status |
| --- | --- | --- | --- |
| `planner` | `planned` | `plan_ready` | completed |
| `developer` | `implemented` | `implementation_ready` | completed |
| `tester` | `pass` / `fail` | `tests_passing` | completed / failed |
| `reviewer` | `approved` / `changes_required` | `review_approved` | completed / failed |

**A different kind of project defines its own** without touching Agent
Factory. From
`tests/test_domain_engine.py::test_project_specific_milestones_need_no_core_changes`:

```python
policy = milestone_policy(
    [MilestoneDefinition("ingestion_working", "Ingestion working", MilestoneCategory.DELIVERABLE, order=1),
     MilestoneDefinition("transformation_complete", "Transformation complete", MilestoneCategory.DELIVERABLE, order=2),
     MilestoneDefinition("database_load_verified", "Database load verified", MilestoneCategory.DELIVERABLE, order=3)],
    [NodeMilestoneRule("ingest", "ingestion_working", {"ok": MilestoneStatus.COMPLETED}),
     NodeMilestoneRule("transform", "transformation_complete", {"ok": MilestoneStatus.COMPLETED}),
     NodeMilestoneRule("load", "database_load_verified", {"ok": MilestoneStatus.COMPLETED})],
)
template = ProjectTemplate(..., graph_factory=pipeline_graph, milestone_policy=policy)
```

`tests/test_domain_model.py::test_projects_define_their_own_milestone_types`
does the same for web-app and AI-application vocabularies. Invalid policies
are rejected: duplicate types, a rule naming a type that isn't in the
catalog, two rules for one node, or a project redefining a system type.

### Fields

| Field | Meaning |
| --- | --- |
| `id` | Stable, deterministic ID (see [Entities](#entities)) |
| `project_id`, `run_id` | Owning project and run |
| `sequence` | Position within the run: 1, 2, 3, … |
| `key` | What the milestone is *about* within the run, for example `step:4`, `approval:apr-…`, or `run:finished`. Same key, same milestone. |
| `type`, `category`, `title` | From the project's catalog |
| `summary` | Short human text, for example "5 tests, FAILED (1 failing)" |
| `status` | See the state machine below |
| `created_at` | When the milestone was first recorded |
| `occurred_at` | When its *current* status was reached. Time-range queries use this. |
| `source` | Node, agent, and step that produced it, when known |
| `metadata` | Small JSON object: outcome, visit, the preceding node and outcome, approval details |
| `resolves_id` | The earlier failed milestone this one repairs, if any |

### State machine

```
              +------------> completed (final)
              |
planned ---> in_progress ---> failed (final)
   |              |  ^
   |              v  |
   +----------> blocked
```

| From | Allowed next statuses |
| --- | --- |
| `planned` | `in_progress`, `completed`, `failed`, `blocked` |
| `in_progress` | `completed`, `failed`, `blocked` |
| `blocked` | `in_progress`, `completed`, `failed` |
| `completed`, `failed` | none. Final records never change, not even their summary. |

- **Blocked is temporary.** A blocked milestone can be unblocked in place.
- **Completed and failed are final.** A retry is a new milestone linked by
  `resolves_id`, so failures stay visible.
- **Open milestones can be revised.** The summary and metadata of an open
  milestone (not completed or failed) can be updated without a status
  change (`Milestone.revise`).
- **Enforced in two places.** `Milestone.transition` refuses illegal
  changes. Every repository `update` re-checks the stored record with
  `Milestone.check_successor`. That also rejects changes to identity fields
  (ID, project, run, sequence, key, type, category, title, creation time).

**`planned` and `in_progress` are not emitted by the engine today.** The
standard policy records a milestone when a node *finishes*. The catalog's
`order` already describes what is planned. A future view can show catalog
types not yet reached as "planned" without creating a record per run.
Projects can map an outcome to `in_progress` in their rules.

### What the engine emits

Only workflow boundaries become milestones. For the demo repair run that
means **7 milestones versus 36 execution-log entries**:

| Committed engine event | Milestone | Status |
| --- | --- | --- |
| A node with a rule finishes with a listed outcome | that rule's type | The rule's status. A `completed` one gets `resolves_id` if an unresolved failure of the same type exists in the run. |
| The run blocks at a node whose rule has `record_blocked` | that node's type, with the same key the real outcome will use | `blocked`, later moved to the real outcome in place |
| The run pauses for approval | `human_decision` | `blocked` |
| The decision is committed | the same `human_decision` | `completed`, with `metadata.decision` set to `approved` or `rejected` |
| The request expired undecided, or its approval could not be used (expired before use, or its decider was disabled or lost the permission) | the same `human_decision` | `failed`, with `metadata.decision` set to `expired` or `refused`, and the reason in the summary |
| A person's decision let the run continue | `run_resumed`, key `resumed:approval:<id>` or `resumed:action:<execution>:<attempt>` | `completed` ("Resumed after dana approved send_notice.") |
| An approved action is being dispatched | `action_execution`, key `action:<execution id>` | `in_progress` |
| It succeeded (or a person confirmed it) | the same `action_execution` | `completed` |
| It definitively did not take effect | the same `action_execution` | `failed` |
| Its outcome is uncertain: a person must check | the same `action_execution` | `blocked` ("Action outcome unknown"); the run is `awaiting_reconciliation` |
| A person authorized one more attempt | the same `action_execution` | `in_progress` |
| The run completes | `run_finished` | `completed` |
| The run fails or is exhausted | `run_finished` | `failed`, with the reason in the summary |

A rejected approval still *completes* its `human_decision` milestone: the
milestone records that the decision point was resolved, not that the action
went ahead. `metadata.decision` says which way it went.

**Never emitted.** Node entry, individual tool and model calls, transitions,
heartbeats, claims, crash recovery, a worker handing a run back, and other
internal steps stay in the execution log (or the worker's events) only. A run
resuming *because a person decided* is the exception: that is progress a
person cares about, so it is a `run_resumed` milestone, once per decision.

The demo repair run, as recorded:

| # | Key | Type | Status | Summary |
| --- | --- | --- | --- | --- |
| 1 | `step:0` | `plan_ready` | completed | Implement calculator.py with four arithmetic functions. |
| 2 | `step:1` | `implementation_ready` | completed | Initial implementation of calculator.py. |
| 3 | `step:2` | `tests_passing` | **failed** | 5 tests, FAILED (1 failing) |
| 4 | `step:3` | `implementation_ready` | completed | Fix subtract: use '-' instead of '+'. *(metadata: after tester/fail)* |
| 5 | `step:4` | `tests_passing` | completed, **resolves #3** | 5 tests, passed |
| 6 | `step:5` | `review_approved` | completed | Correct, tested, documented. |
| 7 | `run:finished` | `run_finished` | completed | Run completed after 6 steps |

### Answering the overnight questions

Summary *generation* is deliberately not built. The test
`tests/test_domain_repositories.py::test_the_model_answers_the_overnight_questions`
shows each question can be answered from the records:

| Question | Answer from the records |
| --- | --- |
| What happened between 22:00 and 07:00? | `milestones.query(MilestoneQuery(project_id, since=…, until=…))`, matching `since <= occurred_at < until` |
| What succeeded? | …those with status `completed` |
| What failed? | …those with status `failed` |
| What was repaired? | …completed milestones with `resolves_id`, which names the failure they fixed |
| What is blocked? | Milestones with status `blocked`; the latest run's status and `status_reason` |
| What needs my approval? | `approvals.page_attention(at=now)`: decisions to make (pending, unexpired) and uncertain actions to answer, across projects. Each item names its approval; the approval carries everything the screen needs. |
| Who approved, answered, and when; which worker sent what; was a retry authorized? | The decision and the reconciliation answers (actor, time); the execution's attempts (`owner` is `wkr-…@N`, result, times); applied reconciliations (`retry_authorized_until`). The approval's timeline puts them in order (E5: `tests/test_worker_decisions.py::test_the_overnight_questions_are_answered_by_structured_records_alone`). |
| What is the current status? | `runs.latest_for_project(project_id)`: status and reason |

**Ordering is deterministic.** Queries sort by `occurred_at`, then run ID,
then sequence, then ID. Equal timestamps cannot reorder results.

**Known limitation.** A milestone stores its *current* status, plus when
it was created and when that status was reached. Intermediate statuses
(for example, blocked from 23:00 to 02:00, then completed) are not kept on
the milestone; the execution log has them. If summaries need that history,
add a status-history record before summary work starts.

### Milestones vs. the execution log

| | Milestones | Execution log |
| --- | --- | --- |
| Question | "What did Agent Factory accomplish?" | "What exactly happened internally?" |
| Audience | The user | Engineers auditing or debugging |
| Granularity | Workflow boundaries (7 in the demo repair run) | Every step, action, result, and transition (36) |
| Meaning | Project-defined | Fixed engine vocabulary |
| Mutability | Open milestones move through the state machine; final ones never change | Append-only, hash-chained |
| Storage | Repository interfaces: in memory, or PostgreSQL ([database.md](database.md)) | `.agent_factory/runs/<run_id>/execution_log.jsonl` in the workspace |

The log is unchanged by this work.
`tests/test_domain_engine.py::test_milestones_and_execution_log_stay_distinct`
runs the same scenario with and without a product store and requires
identical log events.

## Approvals

### Exact-action binding

The engine's v0.2 rule still applies: **an approval covers one exact
action**. The product records add a second, independent binding:

| Identifier | Computed from | Identifies |
| --- | --- | --- |
| Request ID (engine) | SHA-256 of `(run, node, visit, tool, params)` | This *occurrence* of the action |
| Action fingerprint (domain) | SHA-256 of canonical JSON `{tool, category, params}` | The action's *content* |

A decision repeats the fingerprint of the action the decider was shown.
`ApprovalService.decide` refuses a decision whose fingerprint differs from
the request's (`ApprovalBindingError`). Approving "delete `a.py`" therefore
can't approve "delete `b.py`". Changing any parameter, the tool, or the
category changes the fingerprint.

When the engine records a decision, the fingerprint is computed from **the
action the engine is actually waiting on**, not read from the store. Even
if the store held a different action under the same request ID, the
decision is refused and the run stays paused
(`tests/test_domain_engine.py::test_decision_binds_to_the_action_the_engine_will_execute`).

**The engine stays the enforcement point.** Before a gated tool runs, the
gateway still checks the decision recorded in the run state.

**Update: the owner chose the distributed approval architecture** (see
[api-contract.md](api-contract.md#workers-and-the-api)).
- The API records a decision through `ApprovalService`: the domain service,
  never raw rows. It checks the client's fingerprint against the stored
  request, and wakes the run in the same transaction (`RunQueue.wake`).
- A worker that holds the run's claim applies it with `Executor.advance`,
  after `ProgressRecorder.consumable_decision` re-checks the stored request
  against the engine's exact pending action, the decision's fingerprint, the
  request's status, the expiry, and the decider
  ([workers.md](workers.md#human-decisions-wake-ups-and-consumption)).
- So a stored "approved" never authorizes anything by itself.

### State machine

```
           +--> approved   (final)
           +--> rejected   (final)
pending ---+--> expired    (final)
           +--> cancelled  (final)
```

Only a pending request can change, and only once.

| Status | Meaning |
| --- | --- |
| `expired` | The request outlived its validity window (`expires_at`). Stale approvals, such as a phone link opened days later, must not be actionable. |

### Expiry

Every pending request expires (E5):

- **Default validity: 12 hours** from `requested_at` (`APPROVAL_VALIDITY`,
  `approval_expiry()`). Long enough for an overnight run that pauses in the
  evening to be decided the next morning; short enough that a link from a
  previous day is dead. The engine sets it when it pauses, the recorder
  copies it, and a pending request without one is invalid (in the domain and
  by a database CHECK).
- **The instant itself is expired** (`is_expired(at)`: `at >= expires_at`),
  by server time.
- **One window covers deciding and starting.** An approval decided in time
  must also be *used* in time: a dispatch must begin before `expires_at`
  (the ledger's `authorized_until`), and a worker that applies a decision at
  or after the expiry applies it as `refused="expired"`.
- **The earlier deadline wins** when the engine's and the store's differ.
- **It never renews.** A new request comes only from a new visit of the
  workflow step (a new request ID): a new authorization lifecycle.
- An undecided request is marked `expired` by whoever notices first: the API
  when a late decision arrives, or the worker that the expiry wakes.
| `cancelled` | The request no longer applies, for example because its run was abandoned. |

### Decision rules

`ApprovalService.decide` checks, in this order:

1. The request must exist (`RecordNotFoundError`).
2. The fingerprint must match the request's (`ApprovalBindingError`).
3. If a decision already exists: an identical repeat (same outcome, same
   actor) returns the original, so a double-tap is harmless. Anything else
   is a `ConflictError`.
4. The request must still be pending (`StatusTransitionError`).
5. If the request has expired, it is marked `expired` and the decision is
   refused (`ApprovalExpiredError`).
6. Otherwise the decision and the request's new status are saved
   **atomically** (`ApprovalRepository.save_decision`).

Two decisions racing each other, for example from two devices, settle
exactly as if they had arrived one after the other. Exactly one decision is
ever stored. The loser either gets that decision back (an identical repeat)
or a `ConflictError`
(`test_a_decision_racing_another_is_settled_as_if_they_were_sequential`;
against PostgreSQL, `test_concurrent_decisions_from_two_devices`).

In the engine, the decision is recorded **before** the engine changes its
own state. A conflicting decision, for example one already made from
another device, therefore leaves the run paused and unchanged
(`test_conflicting_decision_is_refused_before_the_engine_changes`). If the
process stops between the two writes, `Executor.reconcile` applies the
stored decision later; see [Engine integration](#engine-integration).

### Ready for mobile approval (data only)

An approval request already holds everything a phone approval screen must
show (PRODUCT_SPEC FR-022):

| Screen needs | Field |
| --- | --- |
| Which project | `project_id`, joined to the project's name |
| Which agent and workflow step | `agent`, `node`, `context.step` |
| The exact requested action | `action.tool`, `action.category`, `action.params` |
| Why | `reason` |
| Risk | `risk`, a plain-language statement per action category, such as "Permanently deletes data; Agent Factory cannot undo it." |
| When | `requested_at`, and `expires_at` (always set since E5) |
| What happened afterwards (E5) | the execution and its timeline ([below](#the-execution-timeline)); the API's `getApproval` adds both |

### Actors

A decision's `actor` records who decided:

| Where the decision came from | Actor |
| --- | --- |
| The HTTP API | `{"kind": "user", "id": "prn-…", "display_name": "…", "verified": true}`: the authenticated principal, set by the server from the verified token; a client cannot choose it ([authentication.md](authentication.md)) |
| The local CLI | `{"kind": "unauthenticated", "id": "local:<name>", "display_name": "<name>", "verified": false}` |

The record shape did not change when authentication arrived. An
unauthenticated actor can never be marked verified, and a `user` actor is
always a verified principal with a `prn-` ID (`Actor.authenticated`). A
store refuses a `user` decision whose principal is not enrolled (in
PostgreSQL, the foreign key `approval_decisions_principal_fkey`).

### Principals and permissions

`principals.py` holds who may use the API and what each may do:

- **`Principal`**: an enrolled identity. Its identity is the OpenID Connect
  `(issuer, subject)` pair, mapped to a stable `prn-` ID; email and display
  name are display only. `check_successor` lets the profile and the status
  change, never the ID, the issuer and subject, or the creation time. A
  disabled principal cannot authenticate.
- **`Permission`**: the closed vocabulary (`projects:read`,
  `projects:create`, `runs:read`, `milestones:read`, `approvals:read`,
  `approvals:decide`, `actions:reconcile`). No permission implies another.
  `executions.RECONCILE_PERMISSION` is `Permission.ACTIONS_RECONCILE`.
- Principals are enrolled explicitly by the owner (the CLI), never created
  because a token arrived.

## Reconciliation decisions

When an approved action's outcome is unknown, its execution is `uncertain`
and the run waits (`awaiting_reconciliation`). A person answers for exactly
one attempt (`reconciliation.py`):

- `ReconciliationService.record` checks the execution exists, the
  fingerprint is the execution's (`ApprovalBindingError`), and then: an
  identical earlier answer by the same person is returned (a safe repeat),
  any other earlier answer is a `ConflictError`; the execution must be
  uncertain (`ReconciliationNotRequired`) and the attempt must be its
  current one (`StaleReconciliation`). It stores the answer and wakes the
  run, in the caller's transaction.
- **Recording is not applying.** The answer changes no execution. A worker
  applies it through `ExecutionLedger.reconcile` (an `ActionReconciliation`),
  re-checking it first; for "try once more" the ledger record is the new
  authorization, 15 minutes from the person's `decided_at`.
- A system actor can never answer: only a person can say what happened in
  the outside world.

## Attention

"What needs me now?" (`attention.py`, `ApprovalRepository.page_attention`)
has two kinds of item, both decided by the domain, never by a client:

| Kind | When | Who can act |
| --- | --- | --- |
| `decision_required` | A request is pending and not expired at the given time | `approvals:decide` |
| `reconciliation_required` | An execution is `uncertain` and no answer is recorded for its current attempt | `actions:reconcile` |

Items are ordered by `since` (when each began needing a person: the
request's time, or when the execution became uncertain), then project, then
approval. That triple is also the keyset position of a page. An answered
reconciliation leaves the list at once, though a worker has yet to apply it.

## Attention notifications

What needs a person can also be **announced**: an attention event (an
approval request waiting, or one uncertain attempt of an approved action)
gets one durable notification intent per eligible destination, recorded in
the same transaction as the event (the transactional outbox). Details in
[notifications.md](notifications.md).

- **Identity.**
  - The event: `dedup_key` (`decision_required:<project>:<approval>`, or
    `reconciliation_required:<project>:<execution>:<attempt>`).
  - A notification: `ntf-` derived from the event and the destination.
  - Recording the same event again creates nothing.
- **Eligibility.** A `Destination` (`nds-`) belongs to a principal. A
  notification goes to the active destinations of active principals that hold
  the permission to act on the event (`approvals:decide` or
  `actions:reconcile`).
- **States.** `pending -> sending -> sent | pending (retry) | failed`, or
  `skipped` when no longer needed. Each hand-over is a `NotificationAttempt`
  whose outcome may honestly be `unknown`.
- **Authority.** None. A notification is informational; the attention item
  and the decision rules above are unchanged by it, and its failure never
  removes the item.

## The execution timeline

`timeline.approval_timeline` builds the steps of one approval's life from
structured records only: the request, its decision, the execution, its
attempts, recorded answers, and applied reconciliations. Never from the
execution log. Kinds: `approval_requested`, `approved`, `rejected`,
`expired`, `action_dispatching`, `retry_dispatched`, `action_succeeded`,
`action_failed`, `action_not_dispatched`, `outcome_uncertain`,
`reconciliation_required`, `reconciliation_decided`, `retry_authorized`,
`resolved`.

- Each entry has a time, an attempt (where it applies), the person
  (decisions and answers), or the worker and its fencing generation
  (dispatches and outcomes, parsed from the attempt's `owner`).
- Entries are built in causal order (each attempt with its outcome and its
  answer) and sorted stably by time, so steps recorded in the same instant
  keep their order.
- It holds nothing beyond the approval's own action: no attempt details, no
  paths, no hosts.

## Action executions

**Approval is not execution.** An `ApprovalDecision` proves that a person
authorized one exact action. An `ActionExecution`
(`agent_factory/domain/executions.py`) records what Agent Factory knows about
*attempting* it. Execution success is never inferred from approval. The
design and its crash analysis are in
[design/action-execution.md](design/action-execution.md).

| Record | What it says |
| --- | --- |
| `ActionExecution` | One per approved action that was used. ID `axe-` + 20 hex, derived from project, run, approval request, and action fingerprint: the same approved action always maps to the same record, in any process, after any restart; no path is involved. Carries the action as approved, its execution class, state, version, current authorization (number, start, end), attempt counters, owner of an open attempt, and the bounded result or failure. |
| `ExecutionAttempt` | One dispatch, written **before** the side effect, closed with its result (`succeeded`, `failed`, `not_dispatched`, `indeterminate`) afterwards. Keyed tools' attempts carry the idempotency key, the same for every retry of one execution. |
| `ActionReconciliation` | A person's determination of one uncertain attempt: `it_happened`, `did_not_happen` (no retry), or `did_not_happen_retry`. Bound to the execution, attempt, and fingerprint; one per attempt; never edited. For a retry it is the new authorization, valid for 15 minutes. |

**States.** Only these transitions exist; an impossible change raises and is
never repaired:

| State | Meaning (what is *known*) | Next |
| --- | --- | --- |
| `authorized` | Approved for this exact action; no dispatch of the current attempt permitted | `dispatching`, `lapsed`, `uncertain`* |
| `dispatching` | A dispatch was permitted and durably recorded; the effect may or may not have happened | `succeeded`, `failed`, `authorized` (retry), `uncertain` |
| `uncertain` | The outcome cannot be determined and no automatic retry is allowed | `succeeded`, `failed`, `authorized` (person's retry), each only by reconciliation |
| `succeeded`, `failed`, `lapsed` | Final | none |

\* `authorized -> uncertain` only when the window for a retry closes after an
earlier attempt of the same authorization ended with an unknown outcome.
`lapsed` means *known never dispatched*: an authorization expired, its node
visit ended, or its run ended before it was used.

**Class policy** after an attempt whose outcome is unknown: read-only and
idempotent actions retry (bounded by the tool's `max_attempts`; keyed ones
with the same key and only inside the provider's window); non-idempotent
actions become `uncertain`, and the run waits for a person
([safety.md](safety.md#durable-execution-approval-is-not-execution)).

**Relationship to approvals.** One approved decision has at most one
execution. In the product store the database enforces that the execution's
fingerprint is the *approved* decision's (a rejected decision, or a decision
for a different action, cannot have one). The original approval never
changes: a person's retry is a new authorization recorded on the
execution's reconciliation.

**Stores.** The same protocol (`ExecutionLedger`) runs over an
`ExecutionRepository` with three implementations: in memory, PostgreSQL
([database.md](database.md#action-executions)), and a per-run
`executions.json` next to the checkpoint for runs without a database. Every
change is a compare-and-set on the record's version.

## Run status

Runs reuse the engine's `RunStatus` and allow exactly the changes the
executor can make:

| From | Allowed next statuses |
| --- | --- |
| `running` | `awaiting_approval`, `awaiting_reconciliation`, `blocked`, `completed`, `failed`, `exhausted` |
| `awaiting_approval` | `running` |
| `awaiting_reconciliation` | `running` |
| `blocked` | `running` |
| `completed`, `failed`, `exhausted` | none |

`awaiting_reconciliation` means an approved side effect **may already have
happened** and a person must say whether it did before the run continues.
It is deliberately not `blocked`, which means an infrastructure or policy
problem (the sandbox or the action ledger is unavailable).

`status_reason` holds a short explanation: the blocking reason, the error,
the approval being waited on, or the action awaiting reconciliation.

A run queued for a worker, or being advanced by one, is `running`: in
progress, not paused, and not finished. Which worker owns it is a
coordination record (below), not part of the run's product status.

## Run claims (workers)

Workers coordinate through claims ([workers.md](workers.md)). The records are
in `domain/claims.py`, and the repository interface, with an in-memory
implementation for tests, is in `domain/repositories.py`:

| Record | Meaning |
| --- | --- |
| `RunClaim` | The stored claim of one run: owner (`wkr-` + 20 hex), fencing token (the ownership generation), claim time, lease end, last heartbeat, availability. Validated with the same invariants as the database's constraints |
| `Fence` | What a worker presents with every protected write: project, run, worker, generation. `Fence.owner` (`worker@token`) is how the execution ledger records the dispatcher |
| `Claim` | A successful claim: the new fence and lease, and the expired owner it replaced, if any |
| `LeasePolicy` | Lease and heartbeat durations, validated: 2 s to 5 min, at least three heartbeats per lease, and a local give-up margin of a quarter of the lease |
| `OwnershipLost` | Raised when ownership cannot be proved. A `BaseException`, so `except Exception` never swallows it |

`ClaimRepository` (`enqueue`, `claim_next`, `renew`, `release`, `verify`,
`get`) is **not part of `ProductStore`**: claims coordinate worker
processes, and the API never touches them. Every implementation decides
lease validity by its own clock, never a worker's. The boundary instant
belongs to expiry: valid while `now < lease_expires_at`, expired once
`lease_expires_at <= now`.

The in-memory implementation follows the same rules on an injectable clock.
It proves nothing about coordination between processes, and the in-memory
store has no fenced writes: that guarantee is PostgreSQL's
(`persistence/claims.py`, [database.md](database.md#run-claims-workers)).

## Records and schema evolution

Every record serializes to one JSON object, wrapped in an envelope:

```json
{"record_type": "milestone", "schema_version": 1, "id": "ms-…", "...": "..."}
```

Record types: `project`, `run`, `milestone`, `approval_request`,
`approval_decision`. All are at version 1.

**Reading is strict and deliberate.**

| Input | Result |
| --- | --- |
| Wrong `record_type` | Rejected |
| Missing or non-integer `schema_version` | Rejected |
| Newer version than this code knows | Rejected. Never guessed. |
| Older version | Upgraded by an explicit upgrade function, or rejected if none exists |
| Unknown field | Rejected |
| Missing field | Rejected |

**Why unknown fields are rejected rather than ignored.** A field this code
doesn't know may carry meaning. If it were silently dropped and the record
saved again, that meaning would be lost. Adding a field therefore means
bumping `SCHEMA_VERSION` and registering an upgrade from the previous
version (`open_envelope(..., upgrades={1: upgrade_v1_to_v2})`).

**Writing is deterministic.** Keys are sorted, separators are compact, the
text is UTF-8, and NaN or Infinity are refused. Equal records always produce
byte-identical JSON, which makes hashing, diffing, and fixtures reliable.

**Values are validated on construction:**

- Every record is an immutable (frozen) dataclass.
- Timestamps are UTC and stored in one canonical form,
  `2026-10-02T01:00:00.000000Z`. Any accepted spelling of an instant (with
  or without a fraction) is normalized, so equal instants are equal values,
  and a record read back from any store is identical to the one written.
- IDs match their patterns.
- Text is length-limited and contains no control characters. Multi-line
  fields such as summaries and comments allow tabs and newlines.
- `metadata`, `context`, and action parameters must be JSON-safe objects:
  string keys, finite numbers, no NUL characters (PostgreSQL cannot store
  them), at most 8 levels deep, and at most 16 KiB when serialized. They are
  copied on construction, so callers can't mutate a stored record.

## Persistence boundary

Business logic depends only on abstract repositories. There are two
implementations:

- **In memory**: below; for tests and single-process use.
- **PostgreSQL**: durable; see [database.md](database.md).

| Interface | Operations |
| --- | --- |
| `ProjectRepository` | `add`, `get`, `list`, `page(limit, after)`, `get_many` |
| `RunRepository` | `add`, `get(project_id, run_id)`, `update`, `list_for_project`, `latest_for_project`, `page_for_project(limit, before_number)`, `latest_for_projects` |
| `MilestoneRepository` | `add`, `get`, `update`, `query(MilestoneQuery)`, `next_sequence(project_id, run_id)`, `page(query, limit, after)`, `count_since` |
| `ApprovalRepository` | `add_request`, `get_request(project_id, request_id)`, `update_request`, `list_requests(project_id, run_id=, status=)`, `save_decision(decision, request)`, `get_decision`, `page_pending(limit, project_id=, after=)`, `pending_counts` |
| `IdempotencyRepository` | `get(scope, key)`, `add`, `remove_expired`, `purge_expired` (for the API's `Idempotency-Key`) |
| `ExecutionRepository` | `get(project_id, run_id, id)`, `list_for_run`, `attempts`, `reconciliations`, `create` (idempotent), `replace(expected, new, attempt=, reconciliation=)` (compare-and-set) |
| `PrincipalRepository` | `add`, `get`, `find(issuer, subject)` (the authentication lookup), `list`, `update` (profile and status only), `permissions`, `grant(at=)`, `revoke` |

The `page*` methods are keyset queries for the API's cursor pagination:
"the next `limit` records after this sort key", served by an index, never
by offsets. `get_many`, `latest_for_projects`, `pending_counts` and
`count_since` answer a whole page in one query each.

`ProductStore` bundles the four for injection, plus a `UnitOfWork`:
`store.transaction()` groups calls so that they take effect together or not
at all. Nested blocks behave like savepoints. **Every implementation must
enforce:**

1. **Identity.** A record is added once. A second `add` with the same
   identity is a `ConflictError`.
2. **Natural keys.** One run number per project; one sequence number and
   one key per milestone within a run; one decision per request
   (`ConflictError`).
3. **References.** A run's project, a milestone's or request's run, the
   milestone a repair resolves (in the same run), and a decision's request
   must exist (`RecordNotFoundError`).
4. **Transitions.** An update must be a legal successor of the stored
   record: identity fields are immutable, status changes follow the state
   machines, and final milestones never change.
5. **Binding.** A decision must carry its request's action fingerprint
   (`ApprovalBindingError`).
6. **Order.** Queries return records in a deterministic order, comparing
   identifiers by code point.
7. **Atomicity.** `save_decision` stores the decision and the request's new
   status together; so does a `transaction()` block.

Every record belongs to exactly one project: keys and derived IDs include
the project ID, and listings and queries are per project.

`tests/test_domain_repositories.py` runs every contract test against both
implementations: in memory always, and PostgreSQL with
`-m postgres --run-postgres`.

- The in-memory store (`in_memory_store()`) serializes writes with a lock
  and restores a snapshot when a transaction fails. **Nothing survives the
  process.**
- The PostgreSQL store enforces the same rules with real constraints and
  transactions.

## Engine integration

The integration is opt-in and small:

```python
store = in_memory_store()
executor = build_executor(template, provider, workspace, product_store=store)
```

Without `product_store`, the executor behaves exactly as in v0.2
(`test_executor_without_a_product_store_is_unchanged`). The CLI passes the
PostgreSQL store when given `--database`.

There are two hook points in `agent_factory/engine/executor.py`:

| Hook | When | What happens |
| --- | --- | --- |
| `Executor._save` → `ProgressRecorder.sync(state)` | After **every committed checkpoint** | Run status, milestones, and approval requests are derived from the committed state, in **one transaction** |
| `Executor.decide_approval` → `ProgressRecorder.record_decision(...)` | **Before** the engine changes state | The decision is validated and recorded, bound to the engine's pending action |
| `Executor.reconcile(run_id)` | On demand: `db reconcile` (under the run's claim) | Applies a stored decision the engine missed (binding re-checked), then re-derives every record |
| `Executor.advance(run_id)` → `ProgressRecorder.consumable_decision` / `reconciliation_decision` | A worker (or `resume --database`) holding the run's claim | Re-checks and applies a stored decision, a due expiry, or a reconciliation answer, then continues the run (E5) |
| `ProgressRecorder.record_decision` | `approve --database` (and `decide_approval` with a store) | Records the decision and wakes the run, in one transaction |

**Records come from committed checkpoints, not from mid-step events.** That
choice gives three properties:

| Property | Why it holds | Test |
| --- | --- | --- |
| **Idempotent** | Milestone IDs are derived from `(project, run, key)`, and re-recording identical content is a no-op | `test_interrupted_and_resumed_run_records_each_milestone_once` |
| **No phantom milestones** | A node interrupted before its checkpoint re-runs on resume. Only the committed outcome becomes a milestone. | same |
| **No gaps** | Each sync derives records from the whole committed state, so anything missed is picked up by the next sync | `test_a_recording_failure_is_surfaced_and_the_next_sync_fills_the_gap` |

**Recording failures are surfaced, not swallowed.**

- If a repository write fails, the error propagates out of `start` or
  `resume` after the engine's checkpoint is safely committed.
- The failed sync stores nothing, because it is one transaction.
- Resuming the run fills the gap.
- A run that has already finished is reconciled from its checkpoint:

  ```python
  executor.reconcile(run_id)          # or: python -m agent_factory db reconcile --workspace W
  ```

**The other direction.** A decision stored before a crash, which the engine
never applied, is applied by the same call. The action had not run, and a
different decision is refused in the meantime.

The full failure model, including the consistency terms, is in
[database.md](database.md#engine-checkpoint-vs-product-database). Tests:
`test_a_finished_run_is_reconciled_from_its_checkpoint` and
`test_a_decision_stored_before_a_crash_is_applied_by_reconcile`.

## Design issues found

1. **Engine IDs are not globally unique.**
   - Run IDs, such as `run-1` in tests and demos, and approval request IDs
     are unique only within one workspace. Two projects can both have a
     `run-1`.
   - The domain therefore identifies runs as `(project_id, run_id)` and
     requests as `(project_id, request_id)`, and includes `project_id` in
     derived milestone and decision IDs. `Run.ref` (`<project_id>/<run_id>`)
     is the globally unique run reference.
   - The future API should use these composite keys, for example
     `/projects/{project_id}/runs/{run_id}`.
2. **Existing workspaces had no project identity.** Solved with
   `ensure_project_id` (see [Entities](#entities)).
3. **Template changes could rewrite history.** Solved by storing the
   policy with the project; the stored policy wins.
4. **A decision bound only to stored data could be misdirected.** Solved by
   computing the fingerprint from the engine's pending action. This was
   found by a manual mutation check, and a dedicated test was added.
5. **Equal instants had different spellings** (`…00Z` and `…00.000000Z`).
   This was found while persisting to PostgreSQL. Solved by storing one
   canonical form.
6. **The in-memory store accepted references to records that did not
   exist,** which PostgreSQL would refuse. Found by writing one contract for
   both stores; the in-memory store now enforces the same references and
   unique natural keys.

## Projects created through the API

The API, not a workspace, creates a project's identity:
`POST /api/v1/projects` stores the project with a new `prj-…` ID and creates
no files. When work starts, `factory.workspace_for_project(store,
project_id, base_dir)` creates (or reopens) the workspace **with that same
ID** (`Workspace.create(..., project_id=...)`), so the database and the
workspace can never disagree about which project it is. The directory is
`<base_dir>/<project_id>`: the ID's fixed format can hold no separator,
dot segment or drive letter, and the project's *name* is a label that never
becomes part of a path. An existing workspace of another project or
template, an occupied directory, and a redirected (symlink/junction) root
are refused (`tests/test_workspace_binding.py`, and
`test_project_names_are_labels_and_never_become_workspace_paths` over HTTP).

## Gaps before API work

*Updated after the local backend milestone (`feature/backend-api`).*

| Gap | Why it matters for the API |
| --- | --- |
| **Durable store: done on `feature/postgres-persistence`.** The API uses a bounded pool (`persistence/pool.py`). Remaining: the CLI records only with `--database`. | Done for the API. |
| **No authentication.** Actors are `unauthenticated`. | **Solved on `feature/auth-worker`** for the HTTP API: decisions record the authenticated principal ([authentication.md](authentication.md)). The local CLI still records an unverified local actor, and the API stays on loopback until a deployment and an identity provider exist. |
| **Done (E5): expiry.** The engine sets `expires_at` (12 hours) and the worker applies expiry on time. The engine still cannot cancel requests. | Cancellation needs a run-cancellation concept (below). |
| **No status history per milestone.** | Needed if summaries report "blocked for three hours, then recovered". |
| **Done (E5): how API decisions reach the engine.** The API records them and wakes the run; a worker that holds the run's claim re-checks and applies them (`Executor.advance`). | Built and tested end to end (`tests/test_e2e_human_decisions.py`). |
| **Done: "all pending approvals across projects".** `ApprovalRepository.page_pending`. | Serves `listPendingApprovals` in [`openapi.yaml`](../openapi.yaml). |
| **Done: paginated repository queries.** The keyset `page*` methods above. | Serve the contract's cursor pagination. |
| **Log access is not in the domain.** The execution log is per-run JSONL. | The API's log view needs a read model or pagination over the log. |
| **No run-cancellation concept in the engine.** | `cancelled` approvals and a "stop this run" button need it. |
