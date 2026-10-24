# Workers: claims, leases, and fencing

| | |
| --- | --- |
| Status | **Implemented on `feature/auth-worker` (stages E4 and E5, unreleased).** Local development only. |
| Code | `agent_factory/domain/claims.py`, `agent_factory/persistence/claims.py`, migrations `0005_run_claims.sql` and `0006_human_decisions.sql`, `agent_factory/engine/ownership.py`, `agent_factory/worker/` |
| Tests | `tests/test_worker_claims.py`, `tests/test_worker_engine.py`, `tests/test_worker_loop.py`, `tests/test_worker_cli.py`, `tests/test_worker_decisions.py` (no database); `tests/test_postgres_claims.py`, `tests/test_worker_processes.py`, `tests/test_postgres_decisions.py`, `tests/test_cli_decisions.py` (PostgreSQL); `tests/test_worker_restart_live.py` (live, restarts the local database) |
| E5 | Workers consume human decisions: approvals, rejections, expiries, and reconciliation answers ([below](#human-decisions-wake-ups-and-consumption)). Operator commands claim runs or only record intent ([below](#operator-commands)). |
| Not included | Workers on several hosts, deployment, worker authentication, LISTEN/NOTIFY (evaluated, not adopted) |

Several worker processes can share one PostgreSQL database and advance
runs. **Only one worker owns a run at a time, and a worker that has lost a
run cannot change it any more, even if it does not know yet.** This page
explains how, what exactly is guaranteed, and what is not.

1. [The problem: zombie workers](#the-problem-zombie-workers)
2. [The guarantee](#the-guarantee)
3. [Run claims](#run-claims)
4. [Worker identity](#worker-identity)
5. [Leases and heartbeats](#leases-and-heartbeats)
6. [Clocks](#clocks)
7. [Fencing tokens](#fencing-tokens)
8. [The claim query](#the-claim-query)
9. [Protected mutations](#protected-mutations)
10. [Approved actions (ActionExecution)](#approved-actions-actionexecution)
11. [Human decisions: wake-ups and consumption](#human-decisions-wake-ups-and-consumption)
12. [Operator commands](#operator-commands)
13. [Checkpoints and the filesystem](#checkpoints-and-the-filesystem)
14. [The worker lifecycle](#the-worker-lifecycle)
15. [Graceful shutdown](#graceful-shutdown)
16. [Failures: fail closed](#failures-fail-closed)
17. [Observability](#observability)
18. [Running workers locally](#running-workers-locally)
19. [Workers are not users](#workers-are-not-users)
20. [A worker registry: evaluated, not added](#a-worker-registry-evaluated-not-added)
21. [Evidence](#evidence)
22. [Limitations](#limitations)

## The problem: zombie workers

```
Worker A claims run X
Worker A stalls (a long pause, a frozen VM, a lost network)
A's lease expires; worker B claims run X and continues it
Worker A wakes up, still sure that it owns run X, and writes
```

A's late write would overwrite B's work, or repeat a side effect B already
performed. A lease alone cannot prevent it: A may have checked its lease a
moment before it stalled. Whatever A believes, **the write itself** must be
refused. That is what fencing does.

## The guarantee

Stated precisely, with its limits:

- **One owner per run.** A run's claim names at most one worker with a
  valid lease. Of any number of workers claiming at once, exactly one wins
  each run.
- **A stale worker cannot change the run's database state.** Every database
  write a worker makes for a run (run status, milestones, approval records,
  execution-ledger records) runs in a transaction that first proves, under a
  row lock, that the worker's fencing token is still the run's current one
  and its lease still valid. Otherwise the transaction rolls back and the
  worker stops. This holds however sure the worker is about its lease.
- **A stale worker dispatches no approved action.** Ownership is proved in
  the transaction that writes the dispatch ahead, and again immediately
  before the tool runs.
- **A stale worker records no outcome and commits no checkpoint**: it proves
  ownership before each, and stops if it cannot.
- **A claim that cannot be confirmed counts as lost.** A database error while
  proving ownership is treated as lost ownership (fail closed).

**Not guaranteed:**

- **Exactly-once execution.** A side effect already in flight when ownership
  changes cannot be recalled. The execution ledger's rules
  ([action-execution.md](design/action-execution.md)) decide what happens
  next. A class C action whose outcome nobody recorded waits for a person.
- **Fencing of files across hosts.** See
  [Checkpoints and the filesystem](#checkpoints-and-the-filesystem): E4 is
  safe for workers on **one host**.
- **That every stale write is caught at the earliest moment.** A stale worker
  can finish the node it was running (agent work, workspace files under the
  run lock, the node's log entries) before its next protected step stops it.
  Its result is never committed. It is the same situation as a crash.

## Run claims

Migration `0005_run_claims.sql` adds one table. A run has at most one claim row.

| Column | Meaning |
| --- | --- |
| `project_id`, `run_id` | **Primary key.** **Foreign key** to `runs`: a claim belongs to a real run. |
| `worker_id` | The owner: `wkr-` + 20 hex. NULL when nobody owns the run. |
| `fencing_token` | The ownership generation: 0 before the first claim, +1 on every claim, never decreasing. |
| `claimed_at` | When the current (or last) ownership began. |
| `lease_expires_at` | The end of the owner's lease, in PostgreSQL time. NULL when nobody owns the run. |
| `last_heartbeat_at` | The owner's last confirmed renewal. |
| `available_at` | NULL: no worker should pick the run up. Otherwise the run wants a worker from that time on. |
| `created_at`, `updated_at` | Bookkeeping. |

**Constraints** make illegal states unstorable, whatever a buggy writer
attempts:

| Constraint | Rule |
| --- | --- |
| `run_claims_worker_id_format` | A worker ID matches `^wkr-[0-9a-f]{20}$`: never a hostname |
| `run_claims_fencing_token_range` | The token is never negative |
| `run_claims_claimed_iff_generation` | Generation 0 exactly when the run was never claimed |
| `run_claims_owner_has_lease` | An owner always has a lease; no owner, no lease |
| `run_claims_owner_has_heartbeat` | An owner always has a last heartbeat |
| `run_claims_lease_window` | A lease ends after its claim and its renewal, and lasts at most **5 minutes** |
| `run_claims_heartbeat_after_claim` | A heartbeat is never earlier than its claim |
| `run_claims_time_order` | `updated_at >= created_at` |

**Uniqueness**: the primary key is the only unique key. One row per run means
there can never be two parallel ownership histories for one run.

**Index**: `run_claims_claimable_idx` on `(available_at, project_id,
run_id) WHERE available_at IS NOT NULL` serves the claim query's range and
order. Owned runs stay in the index, because their `available_at` stays set,
so that a dead owner's run becomes claimable when its lease expires.
Therefore a claim scan also reads the rows of runs being worked on: at most
one per worker. The fence check, renewal, and release use the primary key.
Both are checked by
`tests/test_postgres_migrations.py::test_the_worker_claim_statements_are_served_by_their_indexes`.

**`available_at` lifecycle**: `worker enqueue` sets it. It stays set while a
worker owns the run. A release either sets it to now plus a delay (the run
still wants a worker) or clears it (the run waits for a person, is blocked,
or has finished).

## Worker identity

Each worker **process** gets a random ID, `wkr-` + 20 hex characters
(80 random bits), when it starts. It is never derived from the hostname, so
two workers on one host, and a worker restarted on the same host, are always
different owners. The host name and process ID are printed when the worker
starts, and recorded in its `worker_started` event, to help a person find
the process. Nothing decides anything by them.

The execution ledger records the owner of every dispatch as
`<worker ID>@<fencing token>`, for example `wkr-3f…@42`. The audit trail
therefore shows which worker and which ownership generation sent each
approved action.

## Leases and heartbeats

| Setting | Default | Bounds | Why |
| --- | --- | --- | --- |
| Lease | **60 s** | 2 s to 5 min | A crashed worker's run is taken over about a minute later. Long leases would only delay recovery; the database refuses leases over five minutes. |
| Heartbeat interval | **20 s** | at most a third of the lease | Two consecutive heartbeats can fail and the third still lands in time. |
| Local give-up | **15 s** before the lease would end (a quarter of the lease) | derived | The worker stops on its own when it cannot confirm a renewal ([Clocks](#clocks)). |

- **Heartbeats run on their own thread and their own database session.** A
  long node (an agent, the sandbox's tests) never delays a renewal, and a
  renewal never queues behind the run's writes. The lease therefore does not
  need to cover the longest tool call.
- **Renewal rules.** A renewal extends the lease to now + lease, only for the
  current fencing token **and only while the lease is still valid**. An
  expired lease is lost, even if nobody has taken the run over yet. Only a
  new claim, with a new token, could win the run back. A renewal never
  changes the token.
- **Takeover conditions.** Another worker may claim the run once
  `lease_expires_at <= now()` (database time) and the run still wants a
  worker (`available_at <= now()`) and has not finished. The new claim adds 1
  to the token. The old owner's next protected step fails.
- **Graceful release.** At the end of its bounded work the owner releases the
  claim. This clears the owner and lease, keeps the token, and sets
  `available_at` by outcome ([lifecycle](#the-worker-lifecycle)). A release
  by anyone but the current generation changes nothing.
- **A dropped heartbeat connection** is replaced: the keeper opens a new
  session and renews again. This is safe because the database alone decides
  whether a renewal is allowed, whichever connection sends it.

## Clocks

- **Lease validity is decided by PostgreSQL's clock only**: `now()`, the
  transaction's start time, in every claim, renewal, and fence check.
  Worker clocks are never compared with each other or with the database.
- **The boundary instant belongs to expiry**: a lease is valid while
  `now() < lease_expires_at` and expired once `lease_expires_at <= now()`.
  Valid and expired never overlap, so at no instant can both the old owner
  and a taker be accepted. The test sets `lease_expires_at = now()` inside a
  single transaction, so every statement sees the same `now()`. The old
  owner is then refused, its renewal is refused, a new claim succeeds, and
  one microsecond earlier the lease is still valid
  (`test_the_lease_expiry_instant_belongs_to_the_next_owner`).
- **Locally, workers use the monotonic clock.** The keeper keeps a local
  deadline: the moment the last confirmed renewal (or the claim) was *sent*,
  plus `lease - margin` (45 s by default). The database measured the lease
  from a later instant, so the local deadline can only be early. Past it, the
  worker stops working on the run even before the database says so. The local
  deadline only ever makes a worker more cautious: it never lets a worker
  write, because every write is checked by the database anyway.
- Wall-clock timestamps in events are for display only.

## Fencing tokens

The token numbers the ownership generations of a run. Each claim gets the
previous token plus one, in the same statement that assigns the worker:

```
generation 41: worker A claims run X
               A stops heartbeating; the lease expires (database time)
generation 42: worker B claims run X
               A wakes with token 41: every protected write checks
               "token = current token and lease valid", and 41 is not 42
```

**Why a lease alone is insufficient.** "My lease had not expired when I last
checked" is always a statement about the past. Between the check and the
write, the process can stall for any length of time. Only a check made **by
the storage, inside the same transaction as the write**, closes that gap.

**Why the token and not only the worker ID.** A worker can lose a run and then
claim it again itself. The old in-flight work (generation 41) and the new
claim (generation 42) have the same worker ID, and only the token tells them
apart (`test_the_same_worker_cannot_write_with_its_previous_generation`).

**How the check works**: `FencedPostgresDatabase`
(`persistence/claims.py`). The store given to a claimed run's executor is
bound to its fence, and every transaction it opens starts with:

```sql
SELECT 1 FROM run_claims
WHERE project_id = $1 AND run_id = $2
  AND worker_id = $3 AND fencing_token = $4
  AND lease_expires_at > now()
FOR SHARE
```

No row means the transaction is rolled back and `OwnershipLost` is raised.
A statement issued outside a transaction runs inside such a transaction of
its own, so no write, and no read, bypasses the proof. `FOR SHARE` conflicts
with the claim query's `FOR UPDATE` and with renewals and releases (row
updates). A takeover therefore waits for, or skips, a fenced write in
progress: **every fenced write is ordered entirely before or entirely after
any change of owner**
(`test_a_fenced_write_in_progress_holds_off_a_takeover_until_it_ends`). A
fenced transaction that began under a valid lease completes even if the lease
runs out meanwhile, and the takeover waits for it, bounded by the session's
60-second `statement_timeout`.

Knowing `project_id`, `run_id`, and an old token gives a worker nothing:
ownership is a database row, and the check compares against the row, not
against what the worker presents.

**`OwnershipLost` is a `BaseException`**, like `asyncio.CancelledError`.
`except Exception` handlers (an agent's, or the executor's own failure path)
therefore cannot swallow it and turn a lost claim into a "failed" run. Losing
a run is not a failure of the run.

## The claim query

One statement, in one short transaction. The graph runs afterwards, outside
any transaction:

```sql
WITH candidate AS (                      -- discover
    SELECT c.project_id, c.run_id, c.worker_id AS previous_worker_id,
           c.fencing_token AS previous_fencing_token, c.lease_expires_at AS previous_lease_expires_at
    FROM run_claims c
    WHERE c.available_at <= now()                                   -- verify eligibility: wanted now,
      AND (c.worker_id IS NULL OR c.lease_expires_at <= now())      -- no owner with a valid lease,
      AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.project_id = c.project_id
                      AND r.run_id = c.run_id AND r.status = ANY($finished))   -- not finished
    ORDER BY c.available_at, c.project_id, c.run_id                 -- oldest first
    LIMIT 1
    FOR UPDATE OF c SKIP LOCKED                                     -- lock; skip rows another claimer holds
)
UPDATE run_claims AS c
SET worker_id = $worker, fencing_token = c.fencing_token + 1,       -- assign; next generation
    claimed_at = now(), lease_expires_at = now() + $lease, last_heartbeat_at = now(), updated_at = now()
FROM candidate
WHERE c.project_id = candidate.project_id AND c.run_id = candidate.run_id
  AND (c.worker_id IS NULL OR c.lease_expires_at <= now())          -- the ownership rule again
RETURNING ...;                                                      -- COMMIT
```

- **`FOR UPDATE … SKIP LOCKED`** lets many workers poll the same table. A row
  another claimer is locking is skipped, never waited for: ten workers
  claiming ten runs at once each get a different run, and none waits
  (`test_ten_workers_claim_ten_different_runs_each_exactly_once`).
- **The claim is the committed row, not the lock.** The row lock ends at
  commit; the owner, lease, and token persist.
- **The UPDATE repeats the ownership condition**, so the statement stays safe
  even if the lock were missing. The mutation check that removes the lock is
  still caught, by the test showing that a locked row is skipped rather than
  waited for.
- **Polling, not notifications.** An idle worker polls every 2 seconds (the
  default). LISTEN/NOTIFY was evaluated in E5 and not adopted
  ([below](#human-decisions-wake-ups-and-consumption)); polling stays the
  source of truth, because notifications are lost while a worker is down.

## Protected mutations

| Mutation | Protection | Evidence (tests/) |
| --- | --- | --- |
| Advancing graph state, writing the checkpoint | Ownership proved immediately before each checkpoint write; the run lock (one writer per host) | `test_worker_engine.py::test_ownership_is_proved_before_every_checkpoint_write`, `test_postgres_claims.py::test_a_stale_worker_cannot_write_its_checkpoint_or_run_state` |
| Recording a run outcome (blocked, failed, exhausted, paused, completed) | Ownership proved before the outcome is logged, and again before its checkpoint | `test_worker_engine.py::test_a_stale_worker_records_no_outcome_not_even_blocked` |
| Changing run status, syncing product records (milestones, approvals) | Fenced transaction | `test_postgres_claims.py::test_every_write_of_the_stale_generation_is_refused_and_leaves_nothing` (8 operations) |
| Writing an approved action's dispatch ahead (ActionExecution dispatch) | Fenced transaction (the write-ahead row and the fence check commit together) | `test_postgres_claims.py::test_a_stale_worker_cannot_record_a_dispatch_so_nothing_is_sent` |
| Running the approved tool | Ownership proved again after the write-ahead record, immediately before the tool | `test_worker_engine.py::test_no_dispatch_when_ownership_is_lost_after_the_write_ahead_record`, `test_postgres_claims.py::test_ownership_lost_after_the_write_ahead_record_means_the_tool_never_runs` |
| Completing an ActionExecution (recording the outcome) | Fenced transaction | `test_postgres_claims.py::test_an_action_in_flight_when_ownership_changes_is_recovered_from_its_evidence_not_resent` |
| Applying a stored decision (approval, rejection, expiry) | Ownership proved before the decision is applied (`_still_owner`) and again before its checkpoint; the product records it changes are fenced | `test_postgres_decisions.py::test_c9_an_old_worker_cannot_consume_a_decision_after_a_takeover` |
| Applying a reconciliation answer (the ledger's record, a retry authorization) | Fenced transaction (the ledger write starts with the fence check) | `test_postgres_decisions.py::test_c10_a_stale_fencing_token_cannot_apply_a_reconciliation` |
| Heartbeat, release | The current generation only (`worker_id` and `fencing_token` in the `WHERE` clause) | `test_a_stale_heartbeat_is_refused_and_leaves_the_new_owners_lease_alone`, `test_a_stale_release_is_refused_and_the_new_owner_keeps_the_run` |

Fencing is not bolted onto the claims table alone. The checks are at the side-effect boundaries: the
transaction that writes, and the last step before a tool runs.

## Approved actions (ActionExecution)

Before any approved side effect, in order (E1–E2 is preserved, with ownership
added):

1. **Ownership and token.** The write-ahead transaction begins with the fence
   check. If the token is not current, nothing is written and nothing is
   sent.
2. **ActionExecution rules.** In the same transaction, the compare-and-set on
   the execution's version admits exactly one dispatch.
3. **Durable write-ahead.** The attempt row commits with `synchronous_commit
   = on`, and `action_dispatching` is appended, fsync'd, to the run's
   hash-chained log. The owner is recorded as `worker@token`.
4. **Ownership once more.** Ownership can change at any moment after the
   commit, so it is proved again: the last step before the tool. If it was
   lost, **nothing is sent**. This process may no longer write the ledger, so
   `action_not_dispatched` goes to the log. The next owner's recovery reads
   that evidence and returns the authorization. The dispatch slot is given
   back, and the next owner sends the action once.
5. **Only then does the tool run.**

**Ownership lost while the side effect is in progress** (after step 5) cannot
be undone, and Agent Factory does not pretend otherwise:

- the tool finishes, and its outcome goes to the log first;
- recording it in the ledger is a fenced write, and fails;
- the next owner's recovery finds the outcome in the log and records it, so a
  sent notice is never sent again
  (`test_an_action_in_flight_when_ownership_changes_is_recovered_from_its_evidence_not_resent`);
- if no outcome reached the log (the process died inside the tool), the
  action's class decides. A class C action becomes `uncertain`, and the run
  waits for a person (`awaiting_reconciliation`). That is unchanged from
  E1–E2.

## Human decisions: wake-ups and consumption

Since E5, workers consume what people decide, through the API or the CLI:
an approval, a rejection, a due expiry, and a reconciliation answer ("did
the uncertain action happen?"). **A stored decision authorizes nothing by
itself.** The worker re-checks it against the engine's exact pending action,
under its claim, and every side effect still goes through the ActionExecution
ledger.

```
person --HTTPS--> API: decision row + run_claims wake-up    (one transaction: the outbox)
worker --poll---> claim the run (FOR UPDATE SKIP LOCKED) --> Executor.advance:
                  load checkpoint -> re-check decision -> apply -> checkpoint -> ActionExecution -> continue
```

### The wake-up

- **A transactional outbox, in PostgreSQL.** The decision (or answer) and
  the run's wake-up (`RunQueue.wake`: `available_at = now()`) commit in the
  same transaction. There is never a decision without a wake-up, nor a
  wake-up for a decision that was rolled back. The claims table is the
  queue; there is no broker.
- **Nothing polls a waiting run.** A run released while it waits for an
  approval is wanted again only at that approval's expiry (`available_at =
  expires_at`), so that a worker applies the expiry on time. A run waiting
  for a reconciliation is not wanted at all. Only a decision wakes either
  earlier. Idle workers poll the claim query, not the runs.
- **No lost wake-up.** A worker may hold the run when the decision commits,
  having already looked and found nothing. Its release would then apply its
  own plan ("not before the expiry", or "not wanted") and undo the wake-up.
  `run_claims.wake_seq` prevents that:
  - every wake-up adds 1 to `wake_seq`; a claim copies it into
    `claimed_wake_seq`;
  - both are written under the claim row's lock, so they are ordered by the
    row's writes, whatever order the transactions commit in;
  - a release that finds `wake_seq > claimed_wake_seq` leaves the run
    available at once, whatever its owner planned.

  Timestamps cannot do this: a transaction's `now()` is its start, not its
  commit. Evidence: `test_postgres_decisions.py::test_c2_…`, `::test_c8_…`,
  and `test_postgres_claims.py::test_a_wake_up_during_a_claim_survives_the_release_whatever_the_owner_planned`.
- **LISTEN/NOTIFY: evaluated, not adopted.** A `NOTIFY` in the deciding
  transaction would let an idle worker react in milliseconds instead of
  within its 2-second poll. It was not added:
  - people decide on a human timescale, so 2 seconds is not a problem worth
    a second mechanism;
  - notifications are lost while no worker listens, so polling would still
    be needed for correctness;
  - every listener holds a dedicated connection.

  Correctness never depends on a notification. One could be added later, as
  a latency hint only.

### What a worker re-checks before applying a decision

`Executor.advance` loads the checkpoint, takes the run lock, and proves the
claim. For a run waiting for an approval, `ProgressRecorder.consumable_decision`
then checks, in order:

| # | Check | If it fails |
| --- | --- | --- |
| 1 | The stored request is the engine's exact pending action: the same project (the recorder's), run, node, agent, and action fingerprint | `DecisionRefused` |
| 2 | The stored decision carries that fingerprint | `DecisionRefused` |
| 3 | The request's status agrees with the decision (approved or rejected) | `DecisionRefused` |
| 4 | Expiry, by server time: the earlier of the engine's and the store's expiry; the instant itself is expired; an approval decided, or applied, at or after it does not count | applied as `refused="expired"`, never as approved |
| 5 | The decider may still decide: an enrolled, active principal still holding `approvals:decide`, or the local operator; never a system actor | applied as refused (`principal_unknown`, `principal_disabled`, `permission_revoked`, `system_actor`). A rejection is always applied. |
| 6 | The worker still owns the run (`_still_owner`, immediately before applying) | `OwnershipLost`: it stops and writes nothing |

`DecisionRefused` applies nothing: the run log records `decision_refused`
and why, and the run is released as not wanted until a person looks. After
the decision is applied and checkpointed, the approved action is used only
through the ActionExecution ledger, which checks the window again: a
dispatch must *begin* before the approval expires.

- **Consumed once.** The checkpoint is the proof that a decision was
  applied: once applied, the run no longer waits for that request, so no
  worker applies it again. A worker that dies after applying but before its
  checkpoint leaves the run waiting; the next owner applies the same
  decision again (the log then shows both applications, each with its
  owner), and the ledger still runs the action once
  (`test_postgres_decisions.py::test_k3_…`).
- **A rejection is a real outcome.** The engine records it, the agent's tool
  call is refused as rejected, and the graph continues by its own policy.
  The same request is never asked again; asking again is a new request, from
  a new visit of the step.
- **Expiry is applied on time** by the worker that the expiry wakes, with no
  decision needed. It never renews.

### Reconciliation answers

For a run waiting for a reconciliation, `Executor.advance` reads each
uncertain execution's recorded answer. `ProgressRecorder.reconciliation_decision`
checks that it is about exactly this execution, its uncertain attempt, and
its fingerprint, by someone still holding `actions:reconcile`
(otherwise `DecisionRefused`, and the execution stays uncertain). The answer
is applied through `ActionGuard.reconcile`, which is the execution ledger:

- `it_happened`: succeeded (confirmed by the person); nothing is sent again.
- `did_not_happen`: failed; never retried.
- `did_not_happen_retry`: a **new** authorization (authorization 2, 3, …)
  for one more attempt, valid for 15 minutes **from the person's decision**,
  never from whenever a worker applied it. The original approval is never
  touched. If no attempt begins in time, it lapses and nothing is sent.

When nothing is uncertain any more, the run continues. That includes the
case where a previous owner applied the answer and died before its
checkpoint (`test_k8_…`).

### Worker identity and human identity

| Who | Identity | Recorded in |
| --- | --- | --- |
| The person who decided or answered | their enrolled principal (`prn-…`, verified from the token), or `local:<name>` for the CLI | the decision or answer row; the approval timeline (`actor`); the run log (`actor_id`) |
| The worker that applied it, or sent an attempt | `wkr-…` and its fencing generation (`wkr-…@N`) | the run log (`owner`); every attempt (`owner`); the timeline (`worker`) |

Workers never hold a person's token, and no token reaches the engine.

## Operator commands

E4 left a gap: `approve`, `resume`, and `reconcile-action` with `--database`
held the run lock but not the claim, so their database writes were not
fenced. E5 closes it. Every database-backed command now takes one of the two
paths everything else takes:

- **Record intent only.** `approve --database` and `reconcile-action
  --database` record the decision or answer and wake the run, in one
  transaction, exactly like the API. They never touch the checkpoint. A
  worker, or `resume --database`, applies it.
- **Claim first.** `resume --database`, `demo --database`, and `db reconcile`
  claim the run (`ClaimRepository.claim_run`: a one-off claim for that run,
  with its own identity, lease, and heartbeat), advance it under that claim
  exactly as a worker would (fenced writes, the same re-checks), and release
  it. While a worker's lease is valid they refuse (`RunOwnedElsewhere`, exit
  code 2) and change nothing. A worker never takes a run an operator holds.
- **Without `--database`** the commands act on the workspace directly, as
  before. There is no database and there are no workers to race.

Evidence: `tests/test_cli_decisions.py` (each command a separate process,
against PostgreSQL), and the CLI tests in `tests/test_cli.py` (no database).

## Checkpoints and the filesystem

**A checkpoint is a file, and a file write cannot be fenced.** No transaction
covers both a PostgreSQL row and a file, so a file write cannot be made
conditional on the current token. E4 therefore does **not** claim that
checkpoint files are fenced the way database writes are. Here is what
protects them, and the limits.

What protects a checkpoint, on one host:

1. **The run lock** (`<run>/run.lock`, an OS lock: the E2 mechanism). A
   worker holds it for the whole time it advances a run. A zombie that is
   still alive still holds it. A worker that has taken over the claim
   therefore cannot even open the run on this host: it gets `RunLocked`,
   releases the claim with a short delay, and tries again later. Its claim
   has already fenced the zombie's database writes, so the zombie stops at
   its next protected step and releases the lock.
2. **Ownership is proved immediately before every checkpoint write.** A
   worker that lost its claim stops at its next checkpoint without writing
   it.
3. **Everything derived from the checkpoint in the database is fenced.**

So, on one host, **a newer owner's checkpoint is never overwritten by a stale
worker**:
- only the holder of the run lock writes the checkpoint;
- a newer owner can write only after the stale worker released the lock;
- a process releases the lock only when it stops advancing the run.

**The workspace lock** (`<workspace>/.agent_factory/workspace.lock`, E5).
A project's runs share its workspace's project files, so a process that may
run agents or tools for a run (start, resume, advance) also holds the
workspace's lock: two runs of one project never run at the same time on a
host. A worker that finds the workspace busy releases the claim at once,
untouched, and the run is offered again after the poll interval
(`run_busy`). Creating a run, a direct decision (`approve` without
`--database`), a direct reconciliation (`reconcile-action` without
`--database`), and `db reconcile` touch only the run's own files and take
only the run lock. Before E5, two workers could advance two runs of one
project at once and corrupt each other's files; an E4 test failed
intermittently because of it (`test_worker_cli.py`), and E5's timing made
it fail every time.

**Not guaranteed:**

- **The proof and the file write are two steps.** If the claim is taken over
  between them, that one write lands after the takeover. It cannot overwrite
  a newer owner's checkpoint, for the reason above, and it is the last write
  that process makes. It is a valid continuation of the last state that
  process committed while it still owned the run.
- **Workers on different hosts** must not share a workspace on a network
  filesystem: OS locks are unreliable there. This is unsupported, and nothing
  can detect it.
- **Copies of a workspace** on two hosts would be two histories. A worker
  works only on runs whose workspace is on its own disk, and never creates a
  workspace for a run that already exists.

**The production boundary** (design only; not implemented): make the
database the only place a run's state becomes real.

- **Checkpoints as rows**, written in a fenced transaction: the same
  transaction proves the token and writes the checkpoint. That is a true
  fenced write, atomic with the product records derived from it.
- **The execution log as append-only rows**, written in fenced transactions.
- **Workspace files as content-addressed snapshots** in object storage,
  uploaded before the fenced commit that references them.

The commit is then the single point of decision. A stale worker's upload is
an unreferenced object, never a state, and any worker on any host can load
the latest committed state.

## The worker lifecycle

```
start
  -> register identity      (a new wkr- ID; worker_started)
  -> find an eligible run   (the claim query; poll while there is none)
  -> claim it               (run_claimed; run_taken_over if a lease had expired)
  -> start the heartbeat    (a thread with its own session)
  -> advance the run        (Executor.advance: bounded; ownership proved at every protected step)
  -> release it             (run_released, wanting another worker or not)
  -> repeat                 (until stopped, or --max-runs / --idle-exit)
```

`Executor.advance` continues a run only if its checkpoint says `running`,
and decides that under the run lock and the claim. A run still waiting for a
person (`awaiting_approval`, `awaiting_reconciliation`) is **re-synced**:
its product records (the approval request, its attention, and that
attention's notification intents; [notifications.md](notifications.md)) are
derived again from the checkpoint, in one fenced transaction. Syncing is
idempotent, so this normally changes nothing. It repairs a process that died
after committing a waiting checkpoint but before its sync committed, which
before `feature/notifications-v1` left the approval unrecorded until
`db reconcile` (tested: `test_notifications.py::test_a_rollback_keeps_neither_the_attention_event_nor_its_notification`).
What the result means for the claim:

| Run status afterwards | Released as | Reason |
| --- | --- | --- |
| `running` (yielded at a node boundary) | wanted again, immediately | `yielded` (step budget), `shutdown` |
| `awaiting_approval` | wanted again at the approval's expiry (a decision wakes it earlier) | `waiting_for_person` |
| `awaiting_reconciliation` | not wanted (an answer wakes it) | `waiting_for_person` |
| `blocked` | not wanted | `blocked` (a person resumes it) |
| `completed`, `failed`, `exhausted` | not wanted | `finished` |
| (held by another process on this host: `RunLocked`) | wanted again after the poll interval | `run_busy` |
| (its workspace is not on this host) | wanted again after 30 s | `workspace_unavailable` |
| (this code cannot run it: unknown template or provider) | not wanted | `unsupported_run` |
| (a stored decision contradicts the engine's exact pending action: `DecisionRefused`) | not wanted, until a person looks | `decision_refused` |
| (ownership lost) | **not released**: the worker no longer owns it | `stale_worker_rejected` |

- **Runs waiting for a person stay waiting until a person decides.** Without
  a decision (and before its expiry) the run is released untouched, and
  nothing is written to it
  (`test_worker_loop.py::test_a_run_waiting_for_a_decision_sleeps_until_its_approval_expires_and_is_not_continued`).
  With one, the worker applies it ([above](#human-decisions-wake-ups-and-consumption)).
  A wake-up that arrives while a worker holds the run is never lost.
- **Bounded work.** `--step-budget N` hands the run back after N nodes, so
  one long run cannot keep a worker forever. Without it, a worker advances a
  run until it pauses or finishes, within the graph's own step limit.
- **One run at a time per process.** Nothing in the worker starts another
  process. Two workers means starting the command twice.

## Graceful shutdown

The **first** Ctrl+C, or SIGTERM, or Ctrl+Break on Windows:

- the worker claims nothing new;
- the run in progress continues to the end of its current node, and its
  checkpoint is committed. An action already being dispatched is never
  abandoned halfway: the executor yields only between nodes;
- the run is released as wanted again (`shutdown`), so another worker can
  continue it at once, and the worker exits (`worker_stopped`,
  `stop_requested`).

A **second** signal stops the worker at once (forced termination), as does
killing the process. Its lease then expires, about a minute later by default,
and another worker takes the run over. If an action was in flight, recovery
applies the execution ledger's rules.

Evidence: `tests/test_worker_processes.py::test_a_worker_process_stopped_by_a_signal_finishes_its_node_and_releases_the_run`
sends a real Ctrl+Break to a worker process.

## Failures: fail closed

- **The worker cannot prove ownership** (the claim was taken over, released,
  or expired; the database is unreachable; no renewal was confirmed by the
  local deadline). It stops working on the run at once and writes nothing
  more: no checkpoint, no outcome, no dispatch, no release. The next owner
  recovers the run.
- **The database is unreachable.** The worker does not continue in the hope
  that the database comes back. Its next protected step fails, and the
  attempt to prove ownership counts as a lost claim. It then waits, with a
  delay that doubles up to 30 s, before claiming again
  (`database_unavailable`). A later claim is a new generation: work never
  resumes under a claim that was in doubt.
- **A real database restart**:
  `tests/test_worker_restart_live.py::test_a_database_restart_stops_the_owner_and_another_worker_recovers_the_run`
  restarts the PostgreSQL container while a worker owns a run.
  - During the outage, the owner sends nothing, commits nothing, and records
    no outcome.
  - Afterwards, another worker takes over with generation 2 and sends the
    approved notice exactly once.

## Observability

Workers emit one JSON object per event, through the logger
`agent_factory.worker`, and `worker run` prints them. A record holds
identifiers, generations, database times, counts, and reasons from a closed
vocabulary: **never** a database URL, a credential, a file path, or exception
text. An error appears only as its type name.

| Event | When |
| --- | --- |
| `worker_started` | A worker registered its identity (host, process ID, lease, heartbeat, poll interval, step budget) |
| `run_claimed` | A new ownership generation began (run, token, lease end) |
| `lease_renewed` | A heartbeat was confirmed. **Logged at DEBUG only** |
| `run_released` | The owner gave the run back: reason, whether it wants another worker, the delay |
| `lease_expired` | A lease ran out: found by the owner's heartbeat (`owner`) or by a worker taking over (`takeover`) |
| `run_taken_over` | A claim replaced an owner whose lease had expired (previous worker and token) |
| `stale_worker_rejected` | An operation was refused because ownership is lost or cannot be proved: `operation` (`heartbeat`, `advance`, `release`) and `reason` (`superseded`, `expired`, `released`, `missing`, `unconfirmed`, `wrong_run`) |
| `database_unavailable` | The worker could not reach the database; it waits before trying again |
| `worker_stopped` | The loop ended: why (`stop_requested`, `max_runs`, `idle`, `error`), and counts |

**No milestones** come from these events. Milestones are progress a person
cares about, and heartbeats and claims are infrastructure noise. In the run's
own hash-chained log:
- `run_resumed` names the owner;
- `run_yielded` marks a hand-back;
- `action_dispatching` names the dispatcher as `worker@token`.

## Running workers locally

```powershell
# The database, as for the API (docs/database.md); migrated explicitly, never on start-up:
$env:AGENT_FACTORY_DATABASE_URL_FILE = ".local\database.url"
.venv\Scripts\python -m agent_factory db migrate

# A run for an existing project (e.g. created through the API), queued for a worker:
.venv\Scripts\python -m agent_factory worker enqueue --project-id prj-... --scenario repair

# One worker, in the foreground. Start it again in a second terminal for a second worker:
.venv\Scripts\python -m agent_factory worker run
```

`worker run` prints its worker ID first, then one JSON line per event.

| Flag | Default | Meaning |
| --- | --- | --- |
| `--workspace-base DIR` | `workspaces` | Where this host keeps project workspaces |
| `--lease S`, `--heartbeat S` | 60, 20 | The lease and heartbeat, validated as above |
| `--poll S` | 2 | Seconds between claim attempts while there is no work |
| `--step-budget N` | none | Hand a run back after N nodes |
| `--max-runs N`, `--idle-exit S` | none | Stop after N claims, or after S idle seconds |

`worker enqueue --project-id P [--run-id R]` creates a run for an existing
project, writing its first checkpoint without running anything, or takes an
existing run R, and queues it. The worker runs only the deterministic fake
provider's scenarios, because a real model provider is not part of this
stage.

There is no Windows service, no daemon, and no supervisor: nothing starts
workers on its own.

## Workers are not users

- **No HTTP endpoints** claim, renew, or release runs, and none starts, stops,
  or lists workers. Workers are internal infrastructure, reached only through
  the database. The OpenAPI contract is unchanged.
- **Run status stays truthful without a contract change.** A run queued for a
  worker, or interrupted, reports `running` ("in progress": not paused, not
  finished). Who owns it is internal, so no additive field is needed.
- **Workers do not authenticate with user tokens**, and the E3 architecture is
  unchanged: principals and permissions are for people using the API.
- **A worker's authority is its database role plus its claim.** Today the
  worker uses the same local database credentials as the CLI.
- **Future infrastructure identity** (design note only, not implemented):
  - in a deployment, each worker would have its own service identity (in AWS,
    an IAM role, with IAM database authentication or a secrets-manager
    credential);
  - a database role allowed to read runs and write claims, ledger rows, and
    product records, but not principals or permissions;
  - the claim stays the authority for which run a worker may change.
- **Credentials never reach untrusted code.** The worker's database
  configuration comes from its own environment. It is never put into a
  sandbox request, a generated project environment, or a prompt; the
  existing secret-boundary tests still hold.

## A worker registry: evaluated, not added

A registry table (worker ID, started, last seen, version, capabilities,
shutdown state) was considered and **not added**. Correctness depends only
on claims: a worker's liveness *is* its lease. A registry would be a second
source of liveness that can disagree with the leases, and nothing would
consult it for a decision. Operators find workers through their
`worker_started` and `worker_stopped` events and through the claims that name
them. A registry may come later for operations (a fleet view, version
rollout) without changing any guarantee.

## Evidence

The thirteen race and failure scenarios, and where each is proved. Every one
runs against a real PostgreSQL database; nothing is mocked.

| # | Scenario | Test | How |
| --- | --- | --- | --- |
| 1 | 2 workers race for one run | `test_postgres_claims.py::test_workers_racing_for_one_run_produce_exactly_one_owner[2]` | Threads released by a barrier, 15 rounds |
| 2 | 10 workers race for one run | `…[10]` | Threads released by a barrier, 8 rounds |
| 3 | Workers claim different runs | `test_ten_workers_claim_ten_different_runs_each_exactly_once` | 10 threads × 10 runs, 4 rounds, no waiting |
| 4 | Renewal races takeover | `test_a_renewal_racing_a_takeover_never_leaves_two_owners` | 40 rounds with the lease ending around the race; both orders occur |
| 5 | Stale heartbeat after takeover | `test_a_stale_heartbeat_is_refused_and_leaves_the_new_owners_lease_alone` | Forced interleaving |
| 6 | Stale release after takeover | `test_a_stale_release_is_refused_and_the_new_owner_keeps_the_run` | Forced interleaving |
| 7 | Stale checkpoint write | `test_a_stale_worker_cannot_write_its_checkpoint_or_run_state` | The takeover happens inside the worker's node |
| 8 | Stale run-state write | `test_every_write_of_the_stale_generation_is_refused_and_leaves_nothing` (run status, product sync, and 6 more) | Generations 41 and 42 |
| 9 | Stale ActionExecution dispatch | `test_a_stale_worker_cannot_record_a_dispatch_so_nothing_is_sent` and `test_ownership_lost_after_the_write_ahead_record_means_the_tool_never_runs` | The takeover just before, and just after, the write-ahead |
| 10 | Death inside the claim transaction | `test_worker_processes.py::test_a_worker_dying_inside_its_claim_transaction_claims_nothing` | Real process, `os._exit(137)` mid-transaction |
| 11 | Death right after the claim commit | `test_worker_processes.py::test_a_worker_dying_right_after_its_claim_commit_keeps_the_run_until_its_lease_expires` | Real process |
| 12 | Connection drop during heartbeat | `test_a_dropped_heartbeat_connection_is_replaced_while_the_database_is_healthy`, `test_a_worker_that_cannot_reach_the_database_stops_advancing_and_is_taken_over` | `pg_terminate_backend` on the heartbeat's connection |
| 13 | Database restart | `test_worker_restart_live.py::test_a_database_restart_stops_the_owner_and_another_worker_recovers_the_run` | A real container restart (live suite) |

Scenarios 1 to 4 are timing races, repeated. Scenarios 5 to 9 force the
dangerous interleaving deterministically, at the exact boundary, because a
timing race would hit it only by chance.

The primary acceptance test,
`test_generation_41_is_refused_once_42_exists_although_worker_a_still_trusts_its_lease`,
gives worker A a local clock on which its lease is still valid. The database
refuses A anyway.

**Real processes** (`tests/test_worker_processes.py`):
- four worker processes race for one run, and exactly one ever claims it;
- a worker process killed mid-run is taken over once its lease expires;
- a zombie worker process, which never renews and always believes its lease
  is valid, wakes after a takeover: recording its in-flight action's outcome
  is refused, its checkpoint never advances, and the next owner recovers the
  action from its log evidence without resending it;
- a real Ctrl+Break stops a worker gracefully.

Manual mutation checks: 15 deliberate defects, each caught by a named test
([testing.md](testing.md#worker-coordination-tests-unreleased)).

### Human decisions (E5)

All against real PostgreSQL. **Races** run separate sessions released by a
barrier, or hold a worker at an exact point while the test acts
(`tests/test_postgres_decisions.py`):

| # | Race | Test | What is proved |
| --- | --- | --- | --- |
| C1 | Two workers race for a newly approved run | `test_c1_…` | One claims it; the action runs once |
| C2 | A decision arrives while a worker holds the run, after it looked | `test_c2_…` | The wake-up survives the worker's release; the next worker applies it |
| C3 | Two identical approvals | `test_c3_…` | One decision recorded, the other a safe repeat; one effect |
| C4 | Conflicting approvals | `test_c4_…` | Exactly one wins; only it reaches the engine |
| C5 | The approval expires while the worker is claiming | `test_c5_…` | Applied as expired; nothing sent |
| C6 | The decision races the expiry (4 offsets around the instant) | `test_c6_…[…]` | An action never begins after the expiry; the records stay consistent |
| C7 | Two different reconciliation answers | `test_c7_…` | Exactly one recorded and applied |
| C8 | An answer arrives while a worker holds the run, after it looked | `test_c8_…` | The wake-up survives a release as "not wanted" |
| C9 | An old worker consumes a decision after a takeover | `test_c9_…` | It applies nothing (`superseded`); the next owner applies it once |
| C10 | A stale fencing token applies a reconciliation | `test_c10_…` | Refused; the next owner applies it once |
| C11 | Four sessions apply the same retry authorization | `test_c11_…` | One authorization (seq 2), one retry |

**Crashes**: a worker in a child process (`tests/decision_child.py`) dies
with `os._exit(137)` at one boundary; its lease is expired and a new worker
recovers the run. The oracle counts real effects:

| # | Death | Test | After recovery |
| --- | --- | --- | --- |
| K1 | Decision stored; the worker dies inside its claim transaction | `test_k1_…` | Claim rolled back; the next worker runs the action once |
| K2 | Claimed, before applying the decision | `test_k2_…` | Taken over; applied once; one effect |
| K3 | Decision applied and logged, before the checkpoint | `test_k3_…` | Applied again (both on the log); one effect |
| K4 | Checkpoint written, before the dispatch record | `test_k4_…` | Dispatched once |
| K5 | Side effect done, before its outcome | `test_k5_…` | Never resent; a person settles it; still one effect |
| K6 | Outcome recorded, before the node's checkpoint | `test_k6_…` | Replayed from the ledger; not resent |
| K7 | Answer stored; the worker dies inside its claim transaction | `test_k7_…` | Applied once; one effect |
| K8 | Answer applied to the ledger, before the checkpoint | `test_k8_…` | The run continues (it no longer waits for nothing) |
| K9 | Retry authorized, before its dispatch | `test_k9_…` | One retry within the 15 minutes; two effects in all, as authorized |
| K10 | The retry's effect done, before its outcome | `test_k10_…` | Never sent a third time; a person settles it again |

No fixture action ran more often than a person authorized. K8 and C2/C8
found two real defects during E5, both fixed before commit: a run left
waiting for an answer that was already applied, and a wake-up lost to a
concurrent release.

## Limitations

- **One host.** Workers on several hosts are not supported: workspaces and
  checkpoints are local files ([above](#checkpoints-and-the-filesystem)).
- **No routing of runs to capable workers.** Every worker claims from one
  queue. A worker that claims a run it cannot open (its workspace is not
  under that worker's `--workspace-base`) releases it with
  `workspace_unavailable` and a 30-second retry delay, and can win the claim
  again next time. With several kinds of worker on one database, a run can
  therefore wait for minutes. This was found in phase 1 of notifications
  (a test that ran two kinds of worker at once failed once in four runs);
  the test now stops a worker that has nothing left to do. Fixing it needs a
  routing rule (for example a workspace or capability column in
  `run_claims`), which is a schema change for the owner to decide.
- **A refused decision needs a person.** A decision that contradicts the
  engine's exact pending action, or a reconciliation answer by someone who
  may no longer answer, is never applied, and the run is not offered to
  workers again by itself. Nobody can override it through the API: a person
  investigates (the run log says why). An approval by someone disabled since
  is applied as refused instead, so the run continues without the action.
- **Latency is the poll interval.** A decision is applied within about 2
  seconds of being recorded (more if every worker is busy). There is no
  push notification.
- **Blocked runs are not retried automatically.** A person resumes them.
- **Fake provider only**: the worker rebuilds runs that use the deterministic
  scenarios; a real model provider is out of scope.
- **Polling**: an idle worker notices new work within the poll interval.
- **Local development only**: no deployment, no service manager, no worker
  authentication beyond the local database role.
