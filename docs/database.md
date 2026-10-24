# Database: durable product records in PostgreSQL

| | |
| --- | --- |
| Status | PostgreSQL persistence plus local API pooling and idempotency on `feature/backend-api`; durable action executions, principals and permissions, and worker run claims on `feature/auth-worker`. **Unreleased** |
| Server | PostgreSQL 17.11, official image `postgres:17@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f` (linux/amd64, built 2026-09-19) |
| Driver | `psycopg` 3.3.6 with `psycopg-binary` 3.3.6 (bundled libpq 18.0.4); optional, see [Configuration](#configuration) |
| Code | [`agent_factory/persistence/`](../agent_factory/persistence/) |
| Schema | Immutable [`0001_initial.sql`](../agent_factory/persistence/migrations/0001_initial.sql), plus [`0002_idempotency_keys.sql`](../agent_factory/persistence/migrations/0002_idempotency_keys.sql), [`0003_action_executions.sql`](../agent_factory/persistence/migrations/0003_action_executions.sql), [`0004_principals.sql`](../agent_factory/persistence/migrations/0004_principals.sql), [`0005_run_claims.sql`](../agent_factory/persistence/migrations/0005_run_claims.sql), [`0006_human_decisions.sql`](../agent_factory/persistence/migrations/0006_human_decisions.sql), and [`0007_notifications.sql`](../agent_factory/persistence/migrations/0007_notifications.sql) |
| Tests | `tests/test_postgres_*.py`, the `[postgres]` variants of `tests/test_domain_repositories.py`, `tests/test_execution_ledger.py`, `tests/test_action_execution.py`, `tests/test_auth_principals.py`, and `tests/test_auth_http.py`, `tests/test_db_sandbox_boundary*.py`, `tests/test_worker_processes.py`, `tests/test_worker_cli.py`, `tests/test_worker_restart_live.py` |
| HTTP backend | [backend.md](backend.md): local FastAPI service with a bounded psycopg connection pool |
| Not included | UI, notifications, deployment, vector search. The database stores principals and permissions; authentication itself happens in the API ([authentication.md](authentication.md)) |

Agent Factory keeps two kinds of records, in two stores:

| Record | Where | Purpose |
| --- | --- | --- |
| **Engine state**: checkpoint and execution log | Files in the workspace (`.agent_factory/runs/<run>/`) | Run the workflow; audit every step |
| **Product records**: projects, runs, milestones, approval requests, decisions, action executions ([product-domain.md](product-domain.md)) | PostgreSQL for the API and CLI commands using `--database`; otherwise the CLI does not persist them | Answer users' questions across runs and projects, and survive restarts |

This document covers the second store.

## Contents

1. [Why PostgreSQL](#why-postgresql)
2. [Schema and relationships](#schema-and-relationships)
3. [Keys](#keys)
4. [Constraints](#constraints)
5. [Indexes](#indexes)
6. [JSONB](#jsonb)
7. [Timestamps, collation, and text](#timestamps-collation-and-text)
8. [Transactions](#transactions)
9. [Engine checkpoint vs. product database](#engine-checkpoint-vs-product-database)
10. [Migrations](#migrations)
11. [Repository pattern](#repository-pattern)
12. [Configuration](#configuration)
13. [Local development setup](#local-development-setup)
14. [Integration tests](#integration-tests)
15. [Security boundary](#security-boundary)
16. [Future: semantic memory with unrelated database (not implemented)](#future-semantic-memory-with-unrelated database-not-implemented)
17. [Action executions](#action-executions)
18. [Human decisions (migration 0006)](#human-decisions-migration-0006)
19. [Notifications (migration 0007)](#notifications-migration-0007)
20. [Model usage (migration 0008)](#model-usage-migration-0008)
21. [The cloud_read category (migration 0009)](#the-cloud_read-category-migration-0009)
19. [Principals and permissions](#principals-and-permissions)
20. [Run claims (workers)](#run-claims-workers)
21. [Known limitations](#known-limitations)

## Why PostgreSQL

The product records are relational by nature:

```
Project
   |
   +--< Run
          |
          +--< Milestone
          |
          +--< ApprovalRequest
                    |
                    +-- 0..1 ApprovalDecision
```

Agent Factory needs structured, transactional queries over them:

| Question | Query |
| --- | --- |
| Pending approvals for a project | `approval_requests` by project, `status = 'pending'`, oldest first |
| Milestones for a run | `milestones` by project and run, in sequence order |
| Milestones during a time range | `milestones` by project, `since <= occurred_at < until` |
| Runs belonging to a project | `runs` by project, in run-number order |
| Current project and run state | the latest run of a project |
| The decision for an exact request | `approval_decisions` by project and request |

**What the choice rests on.**

- Each question is a join-free lookup or range scan on keys the database can
  enforce.
- Each write that must be atomic (a decision and its request's new status)
  maps to a transaction.
- Foreign keys, unique constraints, and checks let the database refuse
  invalid states on its own.
- PostgreSQL is mature, free, runs locally in a container, and is what a
  later deployment would use.

**What it does not rest on.** PostgreSQL can also store vectors (unrelated database).
That played no part in the choice; see [the last section](#future-semantic-memory-with-unrelated database-not-implemented).

## Schema and relationships

Five product-entity tables, the `idempotency_keys` request-replay table, and
the migration tracking table `schema_migrations`. Everything lives in one
schema, `agent_factory` by default (tests use a fresh, randomly named schema
each). The first migration is unchanged; the second adds idempotency storage
required by project creation.

```
projects (id PK)
   | 1
   |                                    runs_project_fkey
   +--< runs (project_id, run_id PK; UNIQUE project_id+number)
          | 1
          |                             milestones_run_fkey
          +--< milestones (id PK; UNIQUE project_id+run_id+sequence, +key)
          |       \__ resolves_id --> milestones (same project and run)   milestones_resolves_fkey
          |
          |                             approval_requests_run_fkey
          +--< approval_requests (project_id, id PK)
                    | 1
                    |                   approval_decisions_binding_fkey (includes the fingerprint)
                    +-- 0..1 approval_decisions (id PK; UNIQUE project_id+request_id)
```

- **One-to-many.**
  - A project has many runs; a run has many milestones and many approval
    requests.
  - The "many" side holds a foreign key to the "one" side.
- **One-to-zero-or-one.**
  - An approval request has no decision or exactly one.
  - `approval_decisions` has a foreign key to its request, plus a `UNIQUE
    (project_id, request_id)` constraint that allows at most one row per
    request.

### From the domain model to tables

The domain model was mapped deliberately, not redesigned for SQL convenience:

| Domain | Relational | Why |
| --- | --- | --- |
| `Project` | `projects` row. `milestone_policy` and `metadata` are JSONB. | The policy is a configuration document, read only as a whole |
| `Run` | `runs` row; key `(project_id, run_id)` | Engine run IDs are unique per project only |
| `Milestone` | `milestones` row. `source` becomes three typed columns (`source_node`, `source_agent`, `source_step`); `metadata` is JSONB | The source fields are stable and queryable |
| `ApprovalRequest` | `approval_requests` row. `ExactAction` becomes `action_tool`, `action_category` (typed), and `action_params` (JSONB) | Tool and category are a fixed vocabulary; parameters vary per tool |
| `ApprovalDecision` | `approval_decisions` row. `Actor` becomes four typed columns (`actor_kind`, `actor_id`, `actor_display_name`, `actor_verified`) | "Decisions by this user" will be a query |
| Record envelope (`record_type`, `schema_version`) | **Not stored** | Table structure is versioned by [migrations](#migrations). The envelope versions JSON documents. |

### Deviations from the brief's sketch

- **`approval_requests` keeps the engine's request ID as `id`.** The brief
  sketched `request_id`; the domain calls it `id`. Its key is
  `(project_id, id)`, because engine request IDs are unique per workspace
  only.
- **The decision's foreign key includes the action fingerprint:**
  `(project_id, request_id, action_fingerprint)`. This makes exact-action
  binding a database rule. It needs one extra unique constraint on the
  requests table, `approval_requests_action_key`; see [Indexes](#indexes).
- **`resolves_id` uses a composite foreign key**,
  `(project_id, run_id, resolves_id)`. A repair can therefore only point at
  a milestone *of the same run*. This needs one extra unique constraint on
  milestones, `milestones_run_member_key`.
- **The milestone policy is JSONB, not three tables** (definitions, rules,
  outcomes).
  - It is configuration, owned by one project, written once, and only ever
    read whole.
  - The trade-off: the database cannot check "this milestone's type is in
    its project's catalog". The domain checks it instead (the recorder looks
    each type up in the catalog). Normalize the policy if catalogs ever
    become editable or queried.

## Keys

| Table | Primary key | Why this key |
| --- | --- | --- |
| `projects` | `id` (`prj-` + 20 hex) | Random, opaque, never a path |
| `runs` | `(project_id, run_id)` | Composite: the engine's `run-1` exists in many projects |
| `milestones` | `id` (`ms-` + hash of project, run, key) | Deterministic: recording the same milestone twice is the same row |
| `approval_requests` | `(project_id, id)` | The engine's exact-action request ID, scoped to its project |
| `approval_decisions` | `id` (`apd-` + hash of project, request) | Stable decision ID; one per request through `UNIQUE (project_id, request_id)` |

Natural keys are unique too:
- one run number per project (`runs_number_key`);
- one sequence number and one key per milestone within a run
  (`milestones_sequence_key`, `milestones_key_key`);
- one decision per request (`approval_decisions_request_key`).

## Constraints

> **Principle.** The database guards **structure and relationships**: keys,
> references, uniqueness, allowed values, JSON shapes, and time ordering.
> The domain model guards **behaviour**: which status changes are legal,
> text limits, and how fingerprints are computed. They solve different
> problems, so both stay.
>
> A state-machine transition (old status → new status) can't be expressed
> as a CHECK constraint. Repositories enforce transitions with a row lock
> and the domain's `check_successor` ([Transactions](#transactions)).

| Invalid state | Prevented by |
| --- | --- |
| A run of a nonexistent project | `runs_project_fkey` |
| A milestone of a nonexistent run | `milestones_run_fkey` |
| An approval request for a nonexistent run | `approval_requests_run_fkey` |
| A decision for a nonexistent request | `approval_decisions_binding_fkey` |
| A decision for a different action than its request's | `approval_decisions_binding_fkey` (includes the fingerprint) |
| Changing a decided request's fingerprint | the same foreign key: a referenced value cannot change |
| Two decisions for one request | `approval_decisions_request_key` |
| Duplicate stable IDs | every primary key |
| Duplicate run numbers, milestone sequences or keys | `runs_number_key`, `milestones_sequence_key`, `milestones_key_key` |
| A repair pointing at a milestone of another run | `milestones_resolves_fkey` |
| An unknown status, category, outcome, or actor kind | `*_valid` CHECK constraints (tested to match the Python enums exactly) |
| Malformed IDs or fingerprints; a run ID like `../etc` | `*_format` CHECK constraints |
| A pending request with an `updated_at`, or a final one without | `approval_requests_updated_iff_final` |
| A verified unauthenticated actor | `approval_decisions_unauthenticated_unverified` |
| Metadata that is not a JSON object | `*_is_object` CHECK constraints |
| Updated before created; expiry before request | ordering CHECK constraints |

`tests/test_postgres_store.py::test_the_database_itself_enforces_integrity`
proves these with raw SQL that bypasses every Python check. The repository
layer translates the resulting errors into the domain's own errors (see
[Repository pattern](#repository-pattern)).

## Indexes

Primary keys and unique constraints already create indexes. Only two indexes
were added, each for a query Agent Factory makes:

| Index | Query it serves |
| --- | --- |
| `milestones_project_occurred_idx` on `(project_id, occurred_at, run_id, sequence)` | "What happened between 22:00 and 07:00?" `MilestoneQuery` with `since`/`until`, ordered by `occurred_at, run_id, sequence`. Within a project `(run_id, sequence)` is unique, so the index order is the full query order. |
| `approval_requests_pending_idx` on `(project_id, requested_at, id) WHERE status = 'pending'` | "What is waiting for my approval?" A **partial index**: decided requests, the large majority over time, are not in it at all. |

The other documented queries use constraint indexes:

| Query | Index |
| --- | --- |
| Runs of a project, in order; the latest run | `runs_number_key` `(project_id, number)` |
| Milestones of a run; the next sequence number | `milestones_sequence_key` `(project_id, run_id, sequence)` |
| An approval request by ID | `approval_requests_pkey` or `approval_requests_action_key` (same leading columns) |
| The decision for a request | `approval_decisions_request_key` |
| A milestone or project by ID | the primary keys |

`tests/test_postgres_migrations.py::test_each_documented_query_is_served_by_its_index`
checks every query in both tables: with sequential scans priced out, the
planner answers each one from the documented index.

**Deliberately not indexed.**

| Query | Why no index |
| --- | --- |
| Milestones by status ("what is blocked?") | Filtered within one project's rows, which are few |
| Approval requests by run | Same reason |
| `resolves_id` | Never searched by itself; nothing is ever deleted |

Add one when a measured query needs it, not before.

**Worker coordination** (migration 0005) adds one index,
`run_claims_claimable_idx` on `(available_at, project_id, run_id) WHERE
available_at IS NOT NULL`, for the claim query; the fence check, renewal, and
release use the claim's primary key
([Run claims](#run-claims-workers);
`test_the_worker_claim_statements_are_served_by_their_indexes` checks the
statements exactly as the code sends them).

**Queries the API contract adds** (designed, not built):

| API query | Served by |
| --- | --- |
| Milestone pages, in keyset order (`occurred_at, run_id, sequence`) | `milestones_project_occurred_idx`, the same order |
| Runs, newest first | `runs_number_key`, scanned backwards |
| Projects, newest first | No index. Projects are few; add `(created_at, id)` if measured. |
| Pending approvals of one project | `approval_requests_pending_idx` |
| Decidable approvals across all projects (`listPendingApprovals` without `project_id`) | `approval_requests_attention_idx` (migration 0006) |
| The attention inbox (`listAttention`) | `approval_requests_attention_idx` and `action_executions_uncertain_idx` (migration 0006), one per branch of a `UNION ALL` |
| Notifications about one approval (the approval view) | `notifications_approval_idx` (migration 0007) |
| The dispatcher's claim (due pending notifications) | `notifications_due_idx` (partial: `status = 'pending'`; migration 0007) |
| Notifications whose dispatcher's lease expired | `notifications_sending_idx` (partial: `status = 'sending'`) |

The contract needs no schema change.

**A known redundancy.** `approval_requests_action_key` repeats the primary
key's leading columns. It exists only so that the decision's foreign key can
include the fingerprint. That costs one small index on a small table; in
return the database itself enforces exact-action binding.

## JSONB

`jsonb` stores JSON in a parsed, binary form that PostgreSQL can validate
and query. Agent Factory uses it only where the structure genuinely varies:

| Column | Contents |
| --- | --- |
| `projects.milestone_policy` | The project's milestone catalog and rules: a configuration document |
| `projects.metadata`, `milestones.metadata` | Free-form details |
| `approval_requests.action_params` | Parameters, which differ per tool |
| `approval_requests.context` | Free-form context for the approval screen |

**What stays in typed columns.** Everything Agent Factory filters, sorts,
joins, or relates: IDs, statuses, types, timestamps, sequence numbers, tool
and category, fingerprints, and actor fields. JSONB is not a way to skip
schema design.

**Rules.**

- Every JSONB column must hold a JSON *object* (a CHECK constraint).
- The domain limits JSON to 16 KiB and 8 levels of nesting.
- The domain refuses NUL characters, which PostgreSQL cannot store, so both
  stores reject the same input.
- Values round-trip exactly, with one JSON-level caveat: JSON keeps numbers,
  not Python's int/float distinction. `1e20` comes back equal, but as an
  integer.

## Timestamps, collation, and text

**UTC policy.**

- Every timestamp column is `timestamptz`, which stores an absolute instant.
- Agent Factory writes timezone-aware values and reads them back converted
  to UTC. Every connection also sets the session time zone to UTC.
- Nothing depends on the host's local time zone, or on the server's.
- The domain stores one canonical text form,
  `YYYY-MM-DDTHH:MM:SS.ffffffZ`. This is microsecond precision, the same as
  PostgreSQL's, so a value read back is identical to the value written.

`test_timestamps_are_utc_instants_whatever_the_session_time_zone` writes an
instant on a US daylight-saving switch day and checks it two ways. Read in
Los Angeles and Kolkata sessions, it is the same instant. Through the
repositories, it comes back as the same UTC string.

**Collation.** Identifier columns use `COLLATE "C"`, which compares and sorts
by code point:
- Ordering is deterministic and identical to the in-memory store's (Python's
  string order).
- It does not depend on the server's locale (`en_US.utf8` in the official
  image).
- Without it, `Run-B` and `run-a` would sort differently in different
  environments.

## Transactions

A **transaction** groups statements so they take effect together, or (on
any error) not at all. That property is **atomicity**. PostgreSQL also
checks every constraint before a transaction commits.

| Operation | Transaction boundary |
| --- | --- |
| **Recorder sync after a checkpoint** | One transaction for everything derived from that checkpoint: run status, milestones, approval requests, and any engine decision being recorded. Readers never see half a checkpoint. A failure rolls the whole sync back. |
| **Recording an approval decision** | `save_decision` locks the request row (`SELECT ... FOR UPDATE`), checks binding and the transition, inserts the decision, and updates the request's status, all in one transaction. Two devices deciding at once are serialized, and exactly one decision is ever stored. |
| **Any update** (run, milestone, request) | Lock the row, check `check_successor` against the latest committed version, update. One transaction, so there are no lost updates. |
| **Registering a project** | One `INSERT`. A concurrent registration gets a `ConflictError`, and the recorder uses the existing row. |
| **Each migration** | Its DDL and its tracking row in one transaction (PostgreSQL DDL is transactional), under an advisory lock |
| **Any other single write** | One statement, which is atomic by itself (autocommit) |
| **Claiming a run** (workers) | One statement: find, lock (`FOR UPDATE SKIP LOCKED`), check eligibility, assign the worker, add 1 to the fencing token, set the lease; committed at once. The graph runs outside it ([workers.md](workers.md#the-claim-query)). |
| **Any write for a claimed run** | The worker's session is *fenced*: each transaction first proves, with `FOR SHARE` on the claim row, that the worker's token is current and its lease valid, then writes. A stale worker's transaction rolls back with nothing stored. |

**Isolation and nesting.**

- Isolation is PostgreSQL's default, READ COMMITTED. Correctness comes from
  row locks and unique constraints, not from SERIALIZABLE.
- Nested blocks become SAVEPOINTs: an inner failure undoes only the inner
  block. The in-memory store implements the same semantics, and the shared
  contract tests check both.
- Each connection sets `lock_timeout = 10s` and `statement_timeout = 60s`.
  A stuck lock becomes an error instead of a run that hangs forever.

## Engine checkpoint vs. product database

The checkpoint (a file) and the product database are **two separate durable
stores**. There is no distributed transaction, no two-phase commit, so
Agent Factory cannot update both atomically, and does not claim to.

**Source of truth.**

- **The checkpoint is the source of truth for execution.** Product records
  are a projection derived from committed checkpoints.
- **Approval decisions are one exception.** They are written to the
  database first, as a durable record of the human's decision, and only
  then applied by the engine. A conflicting or misbound decision is
  therefore refused while the run is still paused.
- **Action executions are the other exception.** For a run that uses the
  database, the execution ledger lives there, and every dispatch of an
  approved action is committed to it *before* the side effect
  ([Action executions](#action-executions)). The checkpoint never decides
  whether an approved action already ran; the ledger and the hash-chained
  execution log do.

What happens when one write succeeds and the other fails:

| Failure | Resulting state | Is it safe? | Repair |
| --- | --- | --- | --- |
| Checkpoint saved, database write fails | The database is **behind** the checkpoint | Yes. The error is raised to the caller (nothing is swallowed), and nothing executes based on the projection. | The next checkpoint's sync re-derives everything. For a finished run: `db reconcile` |
| Part of a sync fails | Nothing from that sync is stored (one transaction) | Yes | As above |
| Decision stored, process dies before the checkpoint | The database says APPROVED; the engine is still waiting | Yes. **The action has not run.** A *different* decision is now refused, because the database holds the first. | `resume --database` or `db reconcile` applies the stored decision, after re-checking its fingerprint against the engine's pending action |
| Decided with plain `approve` (no `--database`) | The engine has a decision the database lacks | Yes | The next sync records it, bound to the engine's recorded action |
| The database and the engine disagree (rejected vs. approved) | A contradiction | **Not resolved automatically** | Reconciliation raises `ConflictError`; a human investigates |

**The API path uses the same mechanism.** In the distributed approval
architecture the owner chose
([api-contract.md](api-contract.md#workers-and-the-api)):

- the API records a decision through `ApprovalService`. That is a
  database-only write, bound to the stored request's fingerprint;
- a worker that holds the run's claim applies it later (`Executor.advance`,
  E5). This is the "decision stored, engine still waiting" row above, by
  design rather than after a crash, and the same transaction wakes the run
  (`run_claims`), so the worker comes within its poll interval.

The worker's re-check against the engine's exact pending action is what
makes it safe: a stored "approved" never authorizes anything by itself.

**Consistency model, stated precisely.**

- **Inside PostgreSQL:** strong (ACID) consistency. Every commit is atomic
  and constraint-checked, and readers see only committed data.
- **Between the two stores:** *not* strong consistency. The projection can
  lag the checkpoint after a failure. Divergence is always detected (the
  failing call raises), never silent.
- **Convergence:** reconciliation re-derives records from the full committed
  checkpoint, so it is idempotent and always converges when it runs.
- **Why not "eventual consistency":** it runs automatically on the next
  checkpoint of an active run, but for a finished run it needs an explicit
  `db reconcile`. The term is therefore not used without that
  qualification.

Tests:
- `tests/test_postgres_engine.py::test_reconcile_after_the_database_failed_mid_run`
  kills the real connection during a run;
- `test_a_decision_stored_before_a_crash_is_applied_by_reconcile`;
- `test_a_sync_that_fails_halfway_leaves_no_partial_records`;
- the in-memory equivalents in `tests/test_domain_engine.py`.

## Migrations

A **migration** is a versioned, reviewed script that moves a database
schema from one version to the next. Applying them in order builds the
same schema everywhere.

**Mechanism** ([`migrator.py`](../agent_factory/persistence/migrator.py)):

- **Files.** Migrations are SQL files `NNNN_name.sql`, numbered 1, 2, 3, …
  with no gaps. Today there are nine, `0001_initial.sql` to
  `0009_cloud_read_category.sql`.
- **Tracking.** `<schema>.schema_migrations` records each applied version,
  its name, the SHA-256 of its file, and when it was applied.
- **Applying.** `migrate` applies pending files in order. Each file runs in
  its own transaction together with its tracking row, so a failure leaves
  nothing behind. A session advisory lock stops two migrators from
  interleaving.
- **Refusals.** It refuses to run if the database records a version this
  code doesn't have (the database is newer), or a file whose checksum
  changed. History is never rewritten: a schema change is a new file.
- **No start-up migration.** Agent Factory never migrates by itself.
  Opening the store only *checks* that the schema is current, and refuses
  otherwise with "run `db migrate`".

```bat
.venv\Scripts\python -m agent_factory db check     # connectivity, role, migration status; changes nothing
.venv\Scripts\python -m agent_factory db migrate   # apply pending migrations
```

**Upgrade procedure** for a future version:

1. Back up the database (`pg_dump`).
2. Stop Agent Factory processes.
3. Install the new code.
4. `db check` shows the pending migrations.
5. `db migrate` applies them.
6. `db check` reports "up to date".
7. Restart.

A failed migration rolls back completely; fix it and run `db migrate` again.

**Why not Alembic.** Alembic is the usual choice alongside SQLAlchemy. Agent
Factory has no ORM: its queries are explicit SQL behind repositories, and it
has one third-party runtime dependency, the driver. A small runner covers
what Alembic would provide here: ordered versions, tracking, transactional
application, and refusing a changed history. What it lacks:

- automatic migration generation, which is pointless without an ORM model;
- downgrades, which are deliberately not offered (restore a backup instead).

## Repository pattern

```
Executor / ProgressRecorder / ApprovalService
              |
              v
   Repository interfaces (agent_factory/domain/repositories.py)
              |
              +------ In-memory implementation (fast unit tests)
              |
              +------ PostgreSQL implementation (agent_factory/persistence/postgres.py)
```

- **No SQL above the interfaces.** The engine contains no SQL, and the
  domain imports no driver.
- **Swapping stores is configuration:**
  `build_executor(..., product_store=open_postgres_store(config))` or
  `product_store=in_memory_store()`.
- **One contract, two implementations.** Both enforce the same invariants:
  identity, natural keys, references, transitions, binding, order, and
  atomicity.
- **Shared contract tests.** `tests/test_domain_repositories.py` runs every
  contract test against both. Where the semantics should match, they do.

**Domain model vs. persistence model.**

- The **domain model** is frozen dataclasses with rules (`Milestone`,
  `ApprovalRequest`, …), with no knowledge of storage.
- The **persistence model** is the tables in `0001_initial.sql`.
- The `_row` and `_from` functions in `postgres.py` translate between them,
  explicitly.

Every row read becomes a domain object again, so the domain's validation
runs on data coming *out* of the database too. A row edited behind the
domain's back, such as an action changed without its fingerprint, fails to
load (`test_a_tampered_row_fails_validation_when_read`).

**Errors.** Driver exceptions never reach business code. They are translated
into the domain's vocabulary:

| Database error | Domain error |
| --- | --- |
| Unique violation | `ConflictError` |
| Foreign-key violation | `RecordNotFoundError`. The binding key gives `ApprovalBindingError` |
| Check or not-null violation | `DomainValidationError` |
| Lock timeout, deadlock, or serialization failure | `ConflictError` (retryable) |
| Lost connection | `StorageUnavailableError` |

**Durable vs. in memory.**

- The in-memory store loses everything when the process exits.
- The PostgreSQL store survives restarts of Agent Factory, and, because its
  data lives on a named volume, restarts of the database container too.

## Configuration

Connection details come only from explicit configuration. Nothing is
hardcoded, and nothing has a default host or password.

| Variable | Meaning |
| --- | --- |
| `AGENT_FACTORY_DATABASE_URL_FILE` | Path of a file containing the URL (preferred) |
| `AGENT_FACTORY_DATABASE_URL` | The URL itself. Set exactly one of these two. |
| `AGENT_FACTORY_DATABASE_SCHEMA` | Schema to use (default `agent_factory`) |
| `AGENT_FACTORY_TEST_DATABASE_URL_FILE` / `_URL` | The **test** database: separate variables, so tests can never touch the development database |

**Safe handling.**

- **Example.** [`config/database.example.env`](../config/database.example.env)
  shows a safe configuration with placeholders only.
- **Redaction.** The URL is never printed. `DatabaseConfig`'s `repr`, its
  `str`, every error message, and every CLI line show
  `postgresql://user:***@host:port/db`.
- **Driver errors are scrubbed** of the password before being re-raised.
- **Production (future).** A secret manager would provide the URL file,
  mounted read-only. Agent Factory already reads only
  `AGENT_FACTORY_DATABASE_URL_FILE`, so nothing else changes. This is not
  implemented: there is no deployment yet.

**Optional dependency.**

- The driver is installed into the project's virtual environment:
  `requirements-postgres.txt` pins exact versions, and `pyproject.toml`
  declares the `postgres` extra.
- Without it, Agent Factory and the default test suite work as before.
- `--database` explains what to install.

## Local development setup

An isolated PostgreSQL for Agent Factory only. It shares no network,
volume, port, or credentials with any other container on the machine.

| | |
| --- | --- |
| Container | `agent-factory-postgres-dev`, labelled `agent-factory.service=postgres-dev` |
| Image | `postgres:17@sha256:d74eeac9…46f` (pulled once, with the owner's authorization; never pulled automatically: `--pull never`) |
| Port | `127.0.0.1:55432 → 5432`. Loopback only, so it is unreachable from the network; 55432 avoids clashing with any other PostgreSQL on 5432. |
| Network | `agent-factory-dev`, its own bridge network |
| Volume | `agent-factory-postgres-dev-data` |
| Roles | `af_admin`: bootstrap superuser, used only to provision. `agent_factory_app`: what Agent Factory uses; **not** a superuser, cannot create databases or roles, and owns only the two databases. |
| Databases | `agent_factory_dev`, `agent_factory_test`. `CONNECT` is revoked from `PUBLIC`. |

The container is **not** labelled `agent-factory.exec`. The sandbox's
janitor and cleanup checks look only at that label, so they never touch the
database.

Setup, from the repository root, in Windows Command Prompt. In PowerShell, use
`$env:NAME = 'value'` instead of `set` and a backtick instead of `^`; in bash,
use `export` and `\`.

```bat
# 1. Driver, in a project virtual environment that can still see the global pytest
py -3.12 -m venv --system-site-packages .venv
.venv\Scripts\python -m pip install -r requirements-postgres.txt

# 2. Generate passwords into git-ignored .local/ files (never printed, never overwritten)
py -3.12 scripts/dev_postgres.py secrets

# 3. Start the server (human-run; the image must already be present)
docker network create --label agent-factory.service=postgres-dev agent-factory-dev
docker volume create --label agent-factory.service=postgres-dev agent-factory-postgres-dev-data
docker run -d --name agent-factory-postgres-dev --label agent-factory.service=postgres-dev ^
  --network agent-factory-dev --env-file .local/postgres-admin.env -p 127.0.0.1:55432:5432 ^
  -v agent-factory-postgres-dev-data:/var/lib/postgresql/data --pull never ^
  postgres:17@sha256:d74eeac9a635390a49bc21bd49fccd973de707e2a53a76ac49b552b8712ec46f

# 4. Least-privilege role and the two databases (idempotent)
.venv\Scripts\python scripts/dev_postgres.py provision

# 5. Schema, then a persistence round trip
set AGENT_FACTORY_DATABASE_URL_FILE=.local\database.url
.venv\Scripts\python -m agent_factory db migrate
.venv\Scripts\python -m agent_factory db verify
```

Use it:

```bat
.venv\Scripts\python -m agent_factory demo --scenario approval --database --fresh
.venv\Scripts\python -m agent_factory approve --workspace workspaces\demo-approval --run-id demo --request-id apr-... --database
.venv\Scripts\python -m agent_factory resume  --workspace workspaces\demo-approval --run-id demo --database
.venv\Scripts\python -m agent_factory db reconcile --workspace workspaces\demo-approval
```

Stop with `docker stop agent-factory-postgres-dev`. To remove everything,
remove the container, the volume, and the network, then delete `.local/`.

## Integration tests

These tests use a real PostgreSQL; nothing is mocked. They are opt-in,
like the live Docker suites. Once requested, a skip or an unavailable
database **fails** the run.

```bat
set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\test-database.url
.venv\Scripts\python -m pytest -m postgres --run-postgres
# the live sandbox/database boundary needs both:
.venv\Scripts\python -m pytest -m "docker_live and postgres" --run-docker-live --run-postgres
```

**Isolation and safety.**

- Each test gets a fresh, randomly named schema, migrated from scratch and
  dropped afterwards.
- The fixtures refuse a database whose name lacks "test", or that is not on
  this machine.

What they prove is listed in [testing.md](testing.md#postgresql-integration-tests).

## Security boundary

```
TRUSTED ORCHESTRATOR ----------> PostgreSQL
UNTRUSTED PROJECT SANDBOX ---X-> PostgreSQL
```

Agent Factory using a database must not give generated code a way into it.

| Layer | Guarantee | Evidence |
| --- | --- | --- |
| Network | Sandbox containers run with `--network none`. The database is bound to host loopback, on its own Docker network. | Live: with the orchestrator connected, sandboxed code fails to reach the database by loopback, by `host.docker.internal`, by container name, and through the bridge gateway (`test_db_sandbox_boundary_live.py`) |
| Environment | The sandbox never inherits host variables, even though the URL sits in the orchestrator's environment during the test | Live: the container sees only its fixed variables. Tier 0: `scrubbed_env`, the docker argv, and every request of a full run carry nothing about the database (`test_db_sandbox_boundary.py`) |
| Validation | Database settings are refused even if a template allowlists them: names like `DATABASE_URL`, `*_DSN`, `PGPASSFILE`, `PGSERVICEFILE`, or any value holding a URL with credentials | `test_database_settings_are_rejected_even_when_allowlisted` |
| Files | Credentials live in `.local/` at the repository root, which is git-ignored and never inside a workspace, so they are never staged into a sandbox | Workspaces are separate directories; staging copies only the project directory |
| Privilege | Agent Factory connects as a non-superuser role that owns only its two databases | `db check` reports and warns about superuser connections |

This changes nothing in the v0.2 sandbox's guarantees; it adds stricter
environment validation. A future project that needs a database inside its
tests would need its own, separately designed capability, never Agent
Factory's.

## Future: semantic memory with unrelated database (not implemented)

**Nothing here exists.**
- No `vector` extension is enabled.
- There are no embedding columns, no embedding model, and no retrieval
  (RAG).
- The unrelated unrelated database container on this machine is not used or touched.

PostgreSQL *could* later also hold **semantic memory** with the unrelated database
extension: embeddings and similarity search. Possible uses:

- finding earlier failures similar to the current one;
- retrieving solutions used on previous projects;
- finding requirements similar to a new project's;
- retrieving relevant engineering notes;
- memory shared across projects.

Keep the two concerns separate:

| | Relational data | Semantic memory |
| --- | --- | --- |
| Contents | Projects, runs, milestones, approvals, decisions | Embeddings of engineering experience |
| Question | "What is true?" | "What is similar?" |
| Correctness | Exact: constraints, transactions | Approximate, by nature |
| Status | Implemented | Not implemented |

If it is ever built:
- use a separate schema and role;
- embed only content approved for it;
- never let retrieval results act as authorization. An approval is an exact
  record, not a similarity match.

## HTTP connection pool and queries

`PostgresStorePool` in `persistence/pool.py` lends repository stores over
psycopg-pool connections. The web server opens the pool at ASGI startup and
closes it at shutdown. Defaults are 1 minimum and 10 maximum connections,
with a 3-second borrowing timeout. Each request's unit of work holds one
connection, so multiple repository calls share a transaction instead of
opening connections independently. Read views use a consistent snapshot.
Connection checks replace broken sessions before reuse. Pool exhaustion,
closure or database unavailability map to safe API errors; no URL or driver
exception is returned. Readiness verifies availability and migration state.

Repository interfaces now include keyset pages for projects, runs,
milestones and pending approvals across projects; batched dashboard counts
avoid a query for each project. They retain deterministic ordering and UTC
time-range semantics. The API does not issue SQL directly. The existing
product indexes remain unchanged; no new product query index is claimed
without measurement.

## Durable HTTP idempotency

Migration `0002_idempotency_keys.sql` stores an operation scope, key,
deterministic request hash, original public response/status, and creation /
expiry times. A primary key arbitrates concurrent use. Project creation and
the replay record commit together; a retry reads that result after process
restart or reconnect. A different body under the same retained key is
refused (409 Conflict, `idempotency_key_reused`). Default retention is 24 hours; expired
keys can be reclaimed. The record holds no raw request body or signing key.

This table belongs to HTTP request processing, not to engine authorization.
An idempotency record cannot resume a run or authorize a tool. The migration
is applied only by the existing explicit `db migrate` command, never by API
startup. See [backend.md](backend.md#durable-project-creation-and-workspace-identity).

## Action executions

Migration [`0003_action_executions.sql`](../agent_factory/persistence/migrations/0003_action_executions.sql)
stores what Agent Factory knows about attempting approved actions
([product-domain.md](product-domain.md#action-executions),
[design](design/action-execution.md)). It also adds `awaiting_reconciliation`
to the `runs.status` vocabulary.

| Table | One row per | Notes |
| --- | --- | --- |
| `action_executions` | Approved action that was used | State, version (compare-and-set), current authorization, attempt counters, open attempt and owner, bounded result or failure |
| `action_execution_attempts` | Dispatch | Inserted and **committed before** the side effect; closed with its result afterwards |
| `action_reconciliations` | Person's determination of an uncertain attempt | For "try once more", also the new 15-minute authorization |

**What the constraints enforce.**

- An execution belongs to a real run (`action_executions_run_fkey`) and to
  that run's approval request (`action_executions_request_fkey`).
- Its fingerprint is the **approved** decision's: the composite foreign key
  `action_executions_approved_binding_fkey` references
  `(project_id, request_id, action_fingerprint, outcome)` with the outcome
  fixed to `approved`. A rejected decision, or one for a different action,
  has no matching row.
- One approved decision has at most one execution
  (`action_executions_one_per_approval`).
- State, effect class, attempt-result, and reconciliation-choice vocabularies
  equal the Python enums (`test_schema_vocabularies_match_the_domain_model`).
- A non-idempotent action has exactly one automatic attempt per
  authorization; `dispatching` exactly when an attempt is open and owned;
  final states exactly when `finished_at` is set; a result only for a success,
  a failure only for a failure.
- At most one attempt per execution is open (unique partial index
  `action_execution_attempts_one_open`).
- A reconciliation names a recorded attempt
  (`action_reconciliations_attempt_fkey`) and the execution's exact
  fingerprint (`action_reconciliations_binding_fkey`); it is by a person,
  never `system`, and a retry window exists exactly for a retry.

**Index.** `action_executions_run_idx (project_id, run_id)` serves "which
executions does this run have?": recovery when a run resumes, lapsing a node
visit's unused authorizations, and deriving milestones on every checkpoint.
Lookups by execution, attempt, or reconciliation use primary keys. "All
uncertain executions" (the attention inbox) uses
`action_executions_uncertain_idx`, added by migration 0006 ([below](#human-decisions-migration-0006)).

**Write-ahead and compare-and-set.** `PostgresExecutionRepository` runs every
change in its own transaction with `synchronous_commit = on` and commits
before returning, so a reported dispatch is durable before the tool runs. The
compare-and-set locks the row (`SELECT … FOR UPDATE`), checks the version, and
writes the execution with its attempt or reconciliation; a second writer
waits, then sees the new version and changes nothing. No transaction or row
lock is ever held while a side effect runs. Tests:
`test_concurrent_dispatches_of_one_execution_admit_exactly_one`,
`test_the_database_refuses_invalid_executions_on_raw_sql`, and the
connection-loss and real-process-death tests in
`tests/test_action_execution.py`.

**Binding a run to its ledger.** A run whose executions live in the database
gets a marker file next to its checkpoint, so it can never be resumed
without the database (where an empty local ledger could dispatch the action
again). The reverse is refused too.

## Human decisions (migration 0006)

Migration [`0006_human_decisions.sql`](../agent_factory/persistence/migrations/0006_human_decisions.sql)
adds only what E5 needs ([workers.md](workers.md#human-decisions-wake-ups-and-consumption)):

| Change | Why |
| --- | --- |
| Backfill: a pending request without `expires_at` gets `requested_at + 12 hours`; then CHECK `approval_requests_pending_expires` (`status <> 'pending' OR expires_at IS NOT NULL`) | No request stays decidable for ever. Requests that are no longer pending keep their stored value. |
| Table `reconciliation_decisions` | A person's answer about one uncertain attempt, recorded as intent; a worker applies it to `action_reconciliations` |
| `action_executions_run_member_key UNIQUE (project_id, run_id, id)` | Target of the answer's run foreign key (implied by the primary key on `id`) |
| `action_reconciliations.principal_id` with a foreign key and `action_reconciliations_user_is_principal` | An applied reconciliation by an authenticated user names its enrolled principal, as decisions do (0004) |
| `run_claims.wake_seq`, `run_claims.claimed_wake_seq`, CHECK `run_claims_wake_seq_valid` (`0 <= claimed_wake_seq <= wake_seq`) | No wake-up is lost to a concurrent release ([workers.md](workers.md#the-wake-up)) |
| Two partial indexes | The attention inbox (below) |

**`reconciliation_decisions` constraints.**

- One answer per attempt: primary key `(execution_id, attempt)`. A second
  insert is a unique violation, which the repository turns into a
  `ConflictError` inside its own savepoint, so the caller can read the
  winner and answer an identical repeat as a safe retry.
- About a real attempt (`reconciliation_decisions_attempt_fkey` to
  `action_execution_attempts`), of an execution of this very run
  (`reconciliation_decisions_run_fkey` to `(project_id, run_id, id)`), for
  that execution's exact action (`reconciliation_decisions_binding_fkey` to
  `(id, action_fingerprint)`).
- By a person: `actor_kind` is `unauthenticated` or `user`, never `system`;
  an unauthenticated actor is never verified; a user is a verified, enrolled
  principal with `principal_id = actor_id`
  (`reconciliation_decisions_user_is_principal`, foreign key to
  `principals`).
- A closed choice vocabulary, and a comment of at most 1000 characters.

**Indexes, and the queries they serve.**

| Index | Query |
| --- | --- |
| `approval_requests_attention_idx` on `(requested_at, project_id, id) WHERE status = 'pending'` | The inbox's decisions (pending, `expires_at > now`, in inbox order), and `listPendingApprovals` without a project filter |
| `action_executions_uncertain_idx` on `(updated_at, project_id, request_id) WHERE state = 'uncertain'` | The inbox's uncertain actions, in inbox order (`since` is when the execution became uncertain). Uncertain executions are rare, so the index is tiny. |
| `reconciliation_decisions_pkey` | The answer recorded for an attempt (the worker's re-check, the inbox's `NOT EXISTS`, the API's conflict check) |

`test_each_documented_query_is_served_by_its_index` checks each with
sequential scans priced out, and
`test_0006_gives_old_pending_requests_the_default_expiry_and_then_requires_one`
applies 0006 to a database built by 0001-0005 holding a pending request
without an expiry.

## Notifications (migration 0007)

The notification outbox ([notifications.md](notifications.md)):

- **Tables.**
  - `notification_destinations`: a principal's channels and addresses;
    personal data, never in an API response.
  - `notifications`: one durable intent per attention event and destination;
    `UNIQUE (dedup_key, destination_id)`.
  - `notification_attempts`: each hand-over to a provider, written before
    the call.
- **Written with the attention event.** `PostgresNotificationRepository.enqueue`
  runs in the recorder's sync transaction, which for a claimed run is fenced
  like every other write. It inserts with `ON CONFLICT DO NOTHING`, so
  repeated syncs, the dispatcher's sweep, and concurrent writers keep exactly
  one row.
- **Claimed by dispatchers.** One `UPDATE ... FROM (SELECT ... FOR UPDATE
  SKIP LOCKED)` statement per claim; the lease is in `now()`. Every later
  write checks `(claimed_by, claim_token)`, and no transaction is open
  during a provider call (`persistence/notifications.py`).
- **Constraints the database enforces:**
  - foreign keys to the real approval request and the real execution
    attempt;
  - a `link_path` that is a dashboard path only;
  - exactly one owner and lease while `sending`, with no lease over five
    minutes;
  - closed vocabularies for status, outcome, and failure category;
  - at most one open attempt per notification.
- **Indexes:** see [Indexes](#indexes). Tests:
  `tests/test_postgres_notifications.py`, `tests/test_postgres_migrations.py`.

## Model usage (migration 0008)

`run_model_usage` holds one row per run:

- calls, failed calls, and input, output, and cached tokens;
- `estimated_cost_usd`, `NULL` unless prices are configured;
- `by_model`: JSONB, read only as a whole.

The recorder writes it in the checkpoint's sync transaction, from the
summary of the run's usage ledger (`model_usage.jsonl`). That ledger is the
source of truth for budgets, because it is written before every call. The
row is replaced only when the counts change, so re-syncing never counts
twice (tested after a reconnect). Constraints: a foreign key to the run, no
negative counts, failed calls at most calls, and `by_model` an array of at
most 20 ([model-providers.md](model-providers.md)).

## The cloud_read category (migration 0009)

`0009_cloud_read_category.sql` widens the two CHECK constraints that list
the action categories (`approval_requests_category_valid` and
`action_executions_category_valid`) to include `cloud_read`.
- **Why:** the vocabulary must be the same in the contract, the API, the
  domain, the frontend, and the database. A production cloud read needs
  approval, so its request and execution rows must be storable.
- **What it changes:** only the constraints are replaced. Migrations 0001
  and 0003 are untouched, as applied migrations always are.
- **Tested:** `test_postgres_migrations.py` checks each constraint against
  the Python enum ([capabilities.md](capabilities.md)).

## Principals and permissions

Migration [`0004_principals.sql`](../agent_factory/persistence/migrations/0004_principals.sql)
stores who may use the API and what each may do
([authentication.md](authentication.md#principals-who-someone-is)). The owner
writes these tables with the local CLI (`auth principal ...`); no HTTP route
writes them.

| Table | One row per | Notes |
| --- | --- | --- |
| `principals` | Enrolled identity | `id` (`prn-` + 20 hex), `issuer`, `subject`, `display_name` and `email` (display only), `status` (`active` or `disabled`), `created_at`, `updated_at` |
| `principal_permissions` | Granted permission | `(principal_id, permission)`, `granted_at`; the permission from a closed vocabulary |

**What the constraints enforce.**

- One principal per external identity: `principals_external_identity_key`
  `UNIQUE (issuer, subject)`. The same subject from another issuer is a
  different principal; an email address is not a key at all.
- ID, issuer, and subject formats (`principals_id_format`,
  `principals_issuer_format`, `principals_subject_format`: printable ASCII,
  at most 512 and 255 characters); status from `active` and `disabled`.
- A grant belongs to an enrolled principal
  (`principal_permissions_principal_fkey`) and names one of the seven
  permissions (`principal_permissions_permission_valid`, equal to the Python
  enum: `test_schema_vocabularies_match_the_domain_model`).
- **A user decision names its principal.** `approval_decisions` gained a
  nullable `principal_id`, a foreign key to `principals`
  (`approval_decisions_principal_fkey`). `approval_decisions_user_is_principal`
  requires it for every `user` decision, requires it to equal `actor_id` and
  the actor to be verified, and forbids it for every other decision. Earlier
  decisions (the unauthenticated local CLI) are unchanged and valid.
- Principals are disabled, never deleted, so a decision's foreign key never
  dangles and nothing needs `ON DELETE`.

**Indexes.** Every authenticated request makes two lookups, both served by
constraint indexes (tested by `test_each_documented_query_is_served_by_its_index`):

| Query | Index |
| --- | --- |
| The principal for a token's issuer and subject | `principals_external_identity_key` `(issuer, subject)` |
| That principal's permissions | `principal_permissions_pkey` `(principal_id, permission)`, by its leading column |
| A principal by ID (CLI) | `principals_pkey` |

No index on `approval_decisions.principal_id`: no query filters decisions by
principal, and principals are never deleted, so the foreign key never has to
search that table. The approval audit query in
[authentication.md](authentication.md#approval-decisions-who-and-what) joins
from decisions to principals by primary key.

**Not stored.** Tokens, sessions, passwords, and signing keys: the identity
provider holds credentials, and tokens live only in the API process for one
request. There is no roles table (grants are direct; `--grant-all` makes an
owner) and no organization or team structure yet.

## Run claims (workers)

Migration [`0005_run_claims.sql`](../agent_factory/persistence/migrations/0005_run_claims.sql)
records which worker process owns each run
([workers.md](workers.md)). Workers write it; the API never reads or writes it.

| Column | Meaning |
| --- | --- |
| `project_id`, `run_id` | Primary key; foreign key to `runs` (runs are never deleted, so the key never has to search this table) |
| `worker_id` | The owner, `wkr-` + 20 hex: one random ID per worker process. NULL when free |
| `fencing_token` | The ownership generation: 0 before the first claim, +1 per claim, never decreasing |
| `claimed_at`, `last_heartbeat_at` | The current or last ownership's start and last confirmed renewal |
| `lease_expires_at` | The lease's end in PostgreSQL time; NULL when free |
| `available_at` | NULL: no worker should take the run. Otherwise it wants a worker from then on; it stays set while owned, so a dead owner's run becomes claimable when its lease ends |

**Constraints**: a worker ID is never a hostname; generation 0 exactly when
never claimed; an owner always has a lease and a heartbeat, and a free run
has no lease; a lease ends after its claim and renewal and lasts at most five
minutes; no negative tokens. One row per run (the primary key) means one
ownership history per run.

**Statements** (all in `persistence/claims.py`, each its own short
transaction, all using `now()`):

| Statement | Rule |
| --- | --- |
| Claim | The oldest available run with no owner holding a valid lease, and not finished; `FOR UPDATE SKIP LOCKED`; token + 1 |
| Renew | Only the current `worker_id` and `fencing_token`, and only while `lease_expires_at > now()` |
| Release | Only the current `worker_id` and `fencing_token`; the token is kept |
| Fence check | `worker_id`, `fencing_token`, and `lease_expires_at > now()`, `FOR SHARE`: the first statement of every transaction of a claimed run's store |

A lease is valid while `now() < lease_expires_at` and expired once
`lease_expires_at <= now()`: the boundary instant belongs to the next owner,
never to both.

## Known limitations

- **CLI opt-in.** The CLI records product data only with `--database`.
  Without it, records exist only in the engine's files.
- **CLI single connection.** A CLI store uses one connection. The HTTP
  backend uses the bounded pool described above.
- **No automatic retry.** A lost connection raises
  `StorageUnavailableError`. Recover by restarting (reconnecting), then
  `resume --database` or `db reconcile`.
- **Lag needs a trigger to repair.** Reconciliation needs a sync to run (see
  the consistency model).
- **No production configuration** (secret manager, TLS, backups): there is
  no deployment yet.
- **No milestone status history** (inherited from the domain model).
- **Workers on one host.** Run claims and fenced writes coordinate any
  number of worker processes, but checkpoints and workspaces are files on one
  host's disk, which cannot be fenced
  ([workers.md](workers.md#checkpoints-and-the-filesystem)).
- **Operator commands do not take claims.** `resume`, `approve`, and
  `reconcile-action` with `--database` are serialized with workers by the OS
  run lock, but their writes are not fenced.
- **An ambiguous commit is treated as unavailable.** If the connection drops
  during the write-ahead commit, nothing is dispatched and the run blocks; a
  later recovery may then declare a class C action uncertain although it was
  never sent (a safe false positive).
- **Losing both stores loses evidence.** The ledger and the workspace's
  execution log each record a dispatch; losing the database rows *and* the
  workspace could hide that an action ran.
- **No audit trail of grants.** A grant records when it was made, not by
  whom; enrollment and permission changes are made with the local CLI and are
  not logged to a table.
