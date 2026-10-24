# Learning notes — the concepts behind Agent Factory

These notes explain the software-engineering ideas Agent Factory
demonstrates, in plain language. Each concept has four parts:

- **What it is.**
- **Why it matters.**
- **How Agent Factory demonstrates it**, with pointers into the repository.
- **How to explain it in an interview**: a short, honest answer you could
  give.

> **About authorship.** Agent Factory was built with an AI coding agent
> working under the project owner's direction; see the
> [last section](#ai-assisted-development-how-agent-factory-was-built). The
> interview answers are phrased accordingly. You can own the requirements,
> the decisions, and the verification you did; you should not claim to have
> hand-written code an AI produced.

## Contents

1. [Requirements and specification](#requirements-and-specification)
2. [Orchestration and state](#orchestration-and-state)
3. [Humans in the loop](#humans-in-the-loop)
4. [Design structure](#design-structure)
5. [Persistence and observability](#persistence-and-observability)
6. [Product data model](#product-data-model)
7. [Relational databases (PostgreSQL)](#relational-databases-postgresql)
8. [APIs and contract-first design](#apis-and-contract-first-design)
9. [The backend: serving the contract over HTTP](#the-backend-serving-the-contract-over-http)
10. [Durable execution: approval is not execution](#durable-execution-approval-is-not-execution)
11. [Authentication and authorization](#authentication-and-authorization)
12. [Distributed workers: claims, leases, and fencing](#distributed-workers-claims-leases-and-fencing)
13. [Human decisions in a distributed system](#human-decisions-in-a-distributed-system)
14. [Real model providers](#real-model-providers)
15. [Capabilities and cloud access](#capabilities-and-cloud-access)
16. [Attention notifications](#attention-notifications)
17. [Testing](#testing)
18. [Security](#security)
19. [Version control and releases](#version-control-and-releases)
20. [AI-assisted development: how Agent Factory was built](#ai-assisted-development-how-agent-factory-was-built)

---

## Requirements and specification

### Requirements engineering

- **What it is.** The discipline of working out *what* a system must do,
  writing it down precisely, and checking that the result does it, before
  and while building.
- **Why it matters.** Most failed projects build the wrong thing, not a
  broken thing. Written requirements make disagreements visible early and
  give every change a reason.
- **How Agent Factory demonstrates it.**
  - Every phase started from a written brief with scope, constraints, "do
    not" lists, and a definition of done.
  - The sandbox phase began with a design report that was *approved with
    amendments* before any code was written
    ([`docs/design/v0.2-sandbox.md`](../design/v0.2-sandbox.md)).
  - [PRODUCT_SPEC.md](PRODUCT_SPEC.md) turns the vision into numbered
    requirements.
- **In an interview.** "I wrote requirements and acceptance criteria before
  each phase, including explicit non-goals. For the sandbox, the design was
  reviewed and amended before implementation, and the final docs claim only
  what tests demonstrated."

### User stories

- **What it is.** A one-sentence requirement from the user's point of view:
  *As a [user], I want [capability], so that [benefit].*
- **Why it matters.** It keeps the team focused on who benefits and why,
  not on features for their own sake.
- **How Agent Factory demonstrates it.** [PRODUCT_SPEC.md §4](PRODUCT_SPEC.md#4-primary-user-story):
  hand off a task, walk away, and still be asked, securely and from a phone,
  before anything risky.
- **In an interview.** "The primary story is a developer who wants to
  delegate a task overnight but keep control of risky actions. Every feature
  traces back to that: bounded loops, approvals, and milestones."

### Acceptance criteria

- **What it is.** Concrete, checkable conditions that must be true for a
  requirement to count as done, often written as "given / when / then".
- **Why it matters.** Without them, "done" is an opinion. With them, it's a
  test.
- **How Agent Factory demonstrates it.**
  - The v0.1 definition of done was a demo of a specific route with a
    failure and a repair.
  - The v0.2 release required a live adversarial suite covering a named list
    of attacks.
  - [PRODUCT_SPEC.md §12](PRODUCT_SPEC.md#12-acceptance-criteria) has
    AC-001…AC-017, each marked verified or not.
- **In an interview.** "Every stage had acceptance criteria up front, and
  most are automated tests. For example, the sandbox wasn't 'done' until
  hostile code demonstrably couldn't read a host canary file or reach the
  network."

### Functional vs. non-functional requirements

- **What it is.**
  - *Functional* requirements say what the system does: "resume a paused
    run".
  - *Non-functional* requirements say how well, or under what constraints:
    "never lose more than the in-progress step"; "fail closed".
- **Why it matters.** Non-functional requirements (security, reliability,
  cost) are where systems usually fail. They need to be explicit and
  testable too.
- **How Agent Factory demonstrates it.** PRODUCT_SPEC separates FR-001…FR-033
  from NFR-001…NFR-017. For example, FR-031 (isolate untrusted code) is
  backed by NFR-001 (fail closed) and NFR-003 (bounded execution).
- **In an interview.** "The functional requirement is running generated
  tests. The non-functional ones make it safe: isolation, limits, and
  failing closed. Those are the ones the adversarial tests target."

---

## Orchestration and state

### Graph orchestration

- **What it is.** Modelling a workflow as a graph: steps are nodes, and
  labelled edges say which step comes next for each outcome.
- **Why it matters.** The flow becomes explicit data you can validate, test,
  and visualise, instead of logic scattered through code.
- **How Agent Factory demonstrates it.**
  - [`agent_factory/graph/definition.py`](../../agent_factory/graph/definition.py)
    defines nodes and outcome-labelled edges.
  - [`standard.py`](../../agent_factory/graph/standard.py) builds the
    planner → developer → tester → reviewer graph.
  - Graphs are validated before running: unknown nodes, unreachable nodes,
    and loops without a budget are all rejected.
- **In an interview.** "The workflow is a validated graph. Each agent
  returns an outcome like `pass` or `fail`, and the graph decides the next
  step. An outcome the graph doesn't declare is an error, not a guess."

### State machines

- **What it is.** A system that is always in one of a fixed set of states,
  with defined transitions between them.
- **Why it matters.** It makes "what can happen next?" answerable and makes
  illegal situations impossible or detectable.
- **How Agent Factory demonstrates it.**
  - A run has an explicit status: `running`, `awaiting_approval`, `blocked`,
    `completed`, `failed`, or `exhausted`
    ([`agent_factory/state.py`](../../agent_factory/state.py)).
  - Only some states can resume.
  - The workflow graph is itself a state machine over nodes.
  - Test: `tests/test_executor.py::test_invalid_transition_fails_the_run`.
  - The product domain adds state machines for milestones and approvals;
    see [State transitions](#state-transitions).
- **In an interview.** "Runs are a state machine. 'Blocked' and 'awaiting
  approval' are resumable; 'completed', 'failed', and 'exhausted' are
  terminal. That made resume logic simple and safe."

### Structured state

- **What it is.** Keeping a workflow's information in well-defined fields,
  rather than an ever-growing free-text transcript.
- **Why it matters.**
  - Transcripts grow without bound and are hard to check.
  - Structured fields can be validated, checkpointed, and given owners.
- **How Agent Factory demonstrates it.**
  - `ProjectState` has fields (`plan`, `implementation`, `test_report`,
    `review`), and each agent may write only its own.
  - Tests: `tests/test_executor.py::test_agent_cannot_write_fields_it_does_not_own`
    and `test_malformed_model_output_fails_the_run`.
- **In an interview.** "Agents communicate through typed state, not chat
  history. Each agent owns specific fields, so the tester can't overwrite
  the reviewer's verdict, and the state can be checkpointed to disk."

### Bounded autonomy

- **What it is.** Letting a system act on its own, but only within explicit
  limits on scope, retries, and risk.
- **Why it matters.** Unlimited autonomy eventually loops forever or does
  something unwanted. Bounds make autonomy safe to leave running.
- **How Agent Factory demonstrates it.**
  - Repair edges have budgets, nodes have visit limits, and runs have a
    step limit.
  - The graph validator rejects any cycle without a budget.
  - Tests: `test_retry_exhaustion_on_test_failures`,
    `test_retry_exhaustion_on_review_rejections`, and
    `test_max_steps_backstop` (all in `tests/test_executor.py`).
- **In an interview.** "Every loop has a budget, enforced in three layers,
  and a graph with an unbounded cycle won't even load. The agents can
  repair, but not forever."

---

## Humans in the loop

### Human-in-the-loop systems

- **What it is.** Automation that deliberately hands specific decisions to a
  person and waits for them.
- **Why it matters.** Some decisions need judgment or authority a machine
  shouldn't have: money, deployments, deletions.
- **How Agent Factory demonstrates it.**
  - A protected action pauses the run, saves a checkpoint, and records a
    pending request.
  - A human approves or rejects through the CLI, and the run resumes
    accordingly.
  - Tests: `tests/test_executor.py::test_approval_gate_pauses_then_resumes_when_approved`
    and `test_approval_gate_rejection_skips_the_action`.
- **In an interview.** "Routine steps run automatically. Protected ones pause
  the run safely and wait for a human. The pause survives restarts because
  it's checkpointed."

### Approval boundaries

- **What it is.** Precise rules about *which* actions need approval and
  *what* an approval covers.
- **Why it matters.** Vague approval scope is how "yes, delete that file"
  becomes "yes, delete anything".
- **How Agent Factory demonstrates it.**
  - The protected categories (spending money, deployment, credential changes,
    deletion, external communication) can never be auto-allowed. The policy
    refuses that configuration; the most permissive setting is "require
    approval".
  - Each approval is bound to a fingerprint of the exact action and its
    parameters.
  - Tests: `tests/test_safety.py::test_approval_gate_blocks_until_human_decides`,
    which also checks that changing a parameter needs a new approval, and
    `test_dangerous_categories_cannot_be_auto_allowed`.
- **In an interview.** "Approvals are exact-action: approving deletion of one
  file approves exactly that, once. A different file means a different
  fingerprint and a new request."

---

## Design structure

### Interfaces and abstractions

- **What it is.** Defining *what* a component must do (its interface)
  separately from *how* a particular version does it.
- **Why it matters.** You can swap implementations — a fake for testing, a
  real one later — without touching the code that uses them.
- **How Agent Factory demonstrates it.**
  - `ModelProvider`, implemented today only by the deterministic
    `FakeProvider`.
  - `SandboxBackend`, with Local, Docker, and Fake implementations.
  - `Tool` and `Agent`.
  - The graph engine never knows which provider or sandbox it is using.
- **In an interview.** "The workflow depends on interfaces, not vendors.
  Adding a real model means writing one provider class. The tests already
  run the whole system against a scripted fake."

### Dependency injection

- **What it is.** Giving a component its collaborators from the outside,
  rather than having it create them itself.
- **Why it matters.** Tests can inject fakes, and configuration lives in one
  place.
- **How Agent Factory demonstrates it.**
  - `Executor` receives its graph, agents, provider, workspace, tools,
    policy, and even its clock.
  - `DockerBackend` receives the function that runs Docker commands, so 100+
    tests exercise Docker logic with no Docker at all
    (`tests/docker_fakes.py`).
  - `SandboxManager.default(docker_runner=...)` works the same way.
- **In an interview.** "The Docker backend takes its command runner as a
  parameter. In unit tests that's an in-memory fake, so we tested the
  container logic thoroughly without starting a single container."

---

## Persistence and observability

### Checkpoints and persistence

- **What it is.** Saving progress durably so work can continue after a crash
  or a long pause.
- **Why it matters.** Long, unattended runs *will* be interrupted. Losing
  hours of work, or repeating side effects, is unacceptable.
- **How Agent Factory demonstrates it.**
  - A checkpoint is written after every completed step, using
    write-temp-then-rename so a crash never leaves a half-written file.
  - Resume continues from the last completed step.
  - The state format is versioned, and old checkpoints are upgraded on load.
  - Tests: `tests/test_executor.py::test_checkpoint_and_resume_after_interruption`
    and `test_resume_is_independent_of_executor_instance_for_every_checkpoint`.
- **In an interview.** "A test interrupts the run after every possible step,
  resumes it with a brand-new executor that has only the checkpoint on disk,
  and checks that the outcome is identical."

### Observability

- **What it is.** Being able to understand what a system is doing, and why,
  from what it records.
- **Why it matters.** You can't trust or debug an autonomous system you
  can't see into.
- **How Agent Factory demonstrates it.**
  - An append-only execution log records nodes entered, actions, results,
    transitions, and timestamps.
  - Each entry is hash-chained to the previous one, so edits are detectable
    (`tests/test_persistence.py::test_log_is_append_only_and_hash_chained`).
  - The CLI `status` and `log` commands, and evidence lines from the
    sandbox tests.
- **In an interview.** "Every action is in a tamper-evident log. If someone
  edits an entry, verification fails."

### Milestones vs. logs

- **What it is.**
  - *Milestones*: short, human-readable progress — "tests passing".
  - *Logs*: detailed engineering evidence — "this command, these
    parameters, this result".
- **Why it matters.** Users shouldn't have to read debug logs to answer "how
  is my project going?", but engineers still need the detail.
- **How Agent Factory demonstrates it.** Partly, on the unreleased
  `feature/product-data-foundation` branch.
  - The engine records milestones only at workflow boundaries: plan ready,
    tests passing or failed, review approved, human decision, run finished.
    The demo repair run produces **7 milestones and 36 log entries**.
  - Milestones are derived from committed checkpoints. The log itself is
    unchanged (`tests/test_domain_engine.py::test_milestones_and_execution_log_stay_distinct`).
  - **Not built yet:** a screen that shows milestones, durable storage for
    them, and overnight summaries (FR-026).

  See [`docs/product-domain.md`](../product-domain.md#milestones-vs-the-execution-log).
- **In an interview.** "We separate 'what did it accomplish?' from 'what
  exactly happened?'. The audit log records everything; milestones are a
  curated view at workflow boundaries. A demo run has 36 log entries but
  only 7 milestones, and the log doesn't change when milestones are
  recorded."

---

## Product data model

These concepts come from the product-data-foundation milestone (unreleased
branch `feature/product-data-foundation`). The full design is in
[`docs/product-domain.md`](../product-domain.md).

### Domain modeling

- **What it is.** Deciding which concepts a product is about, what each one
  holds, and which rules always apply to them. This is done independently
  of databases, APIs, or screens.
- **Why it matters.**
  - APIs, databases, and screens are all views of the domain. If the
    concepts are vague, every layer inherits the vagueness.
  - A rule written once in the domain, such as "a failed milestone never
    changes", can't be bypassed by a later layer.
- **How Agent Factory demonstrates it.**
  - [`agent_factory/domain/`](../../agent_factory/domain/) models projects,
    runs, milestones, approval requests, and approval decisions. Their rules
    are enforced on construction and on every update.
  - The engine's `ProjectState` stays an *execution* model. The domain is the
    *product* model: it answers "what happened overnight?" and "what is
    waiting for me?".
- **In an interview.** "The engine already had state for running a
  workflow. The product needed different answers, so I had the product
  concepts modelled separately, with their rules in one place, before any
  API work."

### Entities

- **What it is.** An object defined by its identity, not its attributes.
  It can change over time and remain the same thing; for example, a
  milestone moves from blocked to completed. A *value object*, by contrast,
  is defined only by its values.
- **Why it matters.** Knowing which things are entities tells you what needs
  an ID, what may change, and what must be tracked over time.
- **How Agent Factory demonstrates it.**
  - Entities: `Project`, `Run`, `Milestone`, `ApprovalRequest`,
    `ApprovalDecision`.
  - Value objects: `ExactAction`, `Actor`, `MilestoneSource`,
    `MilestonePolicy`.
  - All are immutable in code. A change produces a new version, and the
    repository checks that version against the stored one
    (`check_successor`).
- **In an interview.** "Entities have an identity and a life cycle; value
  objects are just data. Even the entities are immutable in code: a status
  change creates a new version, and storage checks it's a legal successor."

### Stable identity

- **What it is.** An identifier that never changes, and never depends on
  details that can change, such as a file path or a name.
- **Why it matters.** Other records, URLs, and notifications will refer to
  it. If an ID changes when a folder moves, every reference breaks.
- **How Agent Factory demonstrates it.**
  - Project IDs are random (`prj-…`), stored inside the workspace, and never
    a path. Tests:
    `tests/test_domain_model.py::test_project_ids_are_opaque_and_never_paths`
    and `tests/test_domain_engine.py::test_project_identity_survives_multiple_runs`.
  - Milestone and decision IDs are hashes of what they describe. Recording
    the same thing twice gives the same ID, so recording after a crash is
    idempotent.
  - Modelling identity exposed a real issue: engine run IDs such as `run-1`
    are only unique within a project. Runs are therefore identified by
    `(project_id, run_id)`.
- **In an interview.** "Project IDs are opaque, not paths. Milestone IDs are
  derived from what they describe, which makes recording safe to repeat
  after a crash. Modelling identity also showed that run IDs weren't
  globally unique, so the model uses composite keys."

### Relationships

- **What it is.** How entities refer to each other: one-to-many,
  at-most-one, and links between records.
- **Why it matters.** Relationships decide which questions you can answer,
  and they shape the database schema and API URLs.
- **How Agent Factory demonstrates it.**
  - A project has many runs. A run has many milestones and approval
    requests. A request has at most one decision.
  - A completed milestone can point to the failure it repaired
    (`resolves_id`). "What was repaired?" is then a query, not a guess
    (`tests/test_domain_engine.py::test_repair_run_emits_meaningful_milestones`).
- **In an interview.** "A repair is a link, not an edit. The failed test
  milestone stays failed, and the later passing one points back at it.
  History stays honest, and a morning summary can list what was fixed."

### State transitions

- **What it is.** The explicit set of statuses a record can have, and which
  changes between them are legal.
- **Why it matters.** An illegal change, such as deciding an already-decided
  approval, is a bug or an attack. Listing the legal transitions makes such
  changes impossible.
- **How Agent Factory demonstrates it.** There are three state machines:
  - **Milestones:** planned → in progress → completed or failed. Blocked is
    temporary.
  - **Approvals:** pending → approved, rejected, expired, or cancelled, and
    only once.
  - **Runs:** these mirror the engine's statuses.

  Each is enforced when a record changes and enforced again by the
  repository. Tests: `test_impossible_milestone_transitions_are_rejected`,
  `test_invalid_approval_transitions_are_rejected`, and
  `test_run_updates_must_follow_the_state_machine`.
- **In an interview.** "A decided approval can never be decided again, and a
  failed milestone can never quietly become a success. Storage re-checks
  every update, so even a buggy caller can't break those rules."

### Repository pattern

- **What it is.** An interface that looks like a collection of domain
  objects (add, get, query) and hides how they are stored.
- **Why it matters.** Business logic doesn't change when storage changes,
  and tests can use an in-memory version.
- **How Agent Factory demonstrates it.**
  - `ProjectRepository`, `RunRepository`, `MilestoneRepository`, and
    `ApprovalRepository` are abstract interfaces, bundled as `ProductStore`.
  - Two implementations exist:
    - in memory, for fast tests;
    - PostgreSQL, for durable storage (unreleased branch; see
      [Relational databases](#relational-databases-postgresql)).
  - One contract-test file runs against both
    (`tests/test_domain_repositories.py`).
- **In an interview.** "The recorder and the approval service only see
  repository interfaces. When PostgreSQL arrived, neither changed: it's a
  second implementation, held to the same contract tests."

### Separation of concerns

- **What it is.** Each part of a system has one job and doesn't take on
  others.
- **Why it matters.** Changes stay local, and each part can be tested on its
  own.
- **How Agent Factory demonstrates it.**
  - The engine executes, and the execution log keeps low-level evidence.
  - The domain holds product records. A small recorder derives them from the
    engine's checkpoints, and repositories store them.
  - Project-specific milestone meaning lives in project templates, not in
    core code
    (`tests/test_domain_engine.py::test_project_specific_milestones_need_no_core_changes`).
- **In an interview.** "The engine doesn't know what milestones mean; a
  recorder derives them from checkpoints using the project's own policy. A
  data-pipeline project defines its milestones in its template, with no
  change to core code."

### Persistence boundary

- **What it is.** The line between business logic and storage technology.
  It is crossed only through a defined interface with defined guarantees.
- **Why it matters.**
  - The database choice can wait until the requirements are known.
  - Guarantees such as atomicity, ordering, and uniqueness become explicit,
    so every future storage technology must meet them.
- **How Agent Factory demonstrates it.**
  - The repository module lists invariants every implementation must
    enforce:
    - each record is added once;
    - updates must be legal successors;
    - ordering is deterministic;
    - a decision and its request's new status are saved atomically;
    - everything is scoped per project.
  - Records are versioned JSON with strict reading, so whatever stores them
    later stores a stable format.
  - **Update.** On the unreleased `feature/postgres-persistence` branch,
    PostgreSQL now sits behind this boundary, and the same contract tests
    pass against it.
    - Writing that second implementation tightened the contract.
    - The in-memory store now also enforces references and unique natural
      keys, as a database would.
- **In an interview.** "We defined what storage must guarantee before
  choosing a database. Saving a decision and updating its request must be
  atomic, and a request gets one decision. When we added PostgreSQL, the
  same contract tests ran against it, and the exercise made the in-memory
  store stricter too."

### Why the data model comes before OpenAPI, the API, and the UI

- **What it is.** Ordering the work: first the domain records, then the API
  contract that exposes them, then the backend and frontend.
- **Why it matters.**
  - An API contract mostly describes domain records and the operations on
    them. Writing it first means guessing.
  - Wrong guesses mean rewriting the contract, the backend, and the UI.
  - The course's contract-first method assumes the domain is already
    understood.
- **How Agent Factory demonstrates it.**
  - [ROADMAP.md](ROADMAP.md) puts the domain model (Stage 2) before the
    OpenAPI contract (Stage 3).
  - The domain now answers the contract's basic questions:
    - which keys identify a run;
    - what a milestone is;
    - how an approval is decided exactly once;
    - what an approval screen shows.
  - [`docs/product-domain.md`](../product-domain.md#gaps-before-api-work)
    lists what is still open.
- **In an interview.** "An OpenAPI file is a promise about data. We built and
  tested the data model first, so the contract will describe something
  real. For example, we only discovered that run IDs need a project prefix
  by modelling the domain."

---

## Relational databases (PostgreSQL)

These concepts come from the PostgreSQL durable-persistence milestone
(unreleased branch `feature/postgres-persistence`). The full design is in
[`docs/database.md`](../database.md).

### Relational databases

- **What it is.** A database that stores data in tables and connects them
  through shared key values. You query it with SQL, and it enforces rules
  and transactions.
- **Why it matters.**
  - Connected records need to stay consistent with each other.
  - Exact questions ("which approvals are pending in this project?") need
    exact answers.
  - A relational database gives both, even when the application has bugs.
- **How Agent Factory demonstrates it.**
  - PostgreSQL 17 stores projects, runs, milestones, approval requests, and
    decisions.
  - It was chosen because the data is a tree of related records with exact
    questions over it, and because of its constraints and transactions.
  - Its vector extension played no part in the choice.
- **In an interview.** "Our records are related data with exact questions
  over them: a textbook relational fit. We chose PostgreSQL for its
  constraints and transactions, not because it can also store vectors."

### Tables and rows

- **What it is.** A table holds one kind of thing. Each row is one instance,
  and each column is one typed attribute.
- **Why it matters.** Typed columns let the database validate, compare,
  sort, and index values.
- **How Agent Factory demonstrates it.**
  - One table per domain entity, and one row per record.
  - Nested value objects became typed columns. An actor became `actor_kind`,
    `actor_id`, `actor_display_name`, and `actor_verified`.
  - Only genuinely free-form data, such as metadata and action parameters,
    is stored as JSONB.
- **In an interview.** "Every entity became a table and every record a row.
  We kept anything we filter or relate on in typed columns, and used JSON
  only for parts that genuinely vary."

### Primary keys

- **What it is.** The column or columns whose value identifies each row
  uniquely and never changes.
- **Why it matters.** Everything else refers to rows by their key.
  Duplicates or changing keys break those references.
- **How Agent Factory demonstrates it.**
  - Project IDs are random (`prj-…`).
  - Milestone IDs are hashes of what they describe, so recording a milestone
    twice targets the same row.
  - Runs have a **composite** key, `(project_id, run_id)`, because engine
    run IDs such as `run-1` repeat across projects.
- **In an interview.** "Engine run IDs turned out to be unique only within a
  project, so runs got a composite primary key. Milestone IDs are derived
  from their content, which makes crash recovery idempotent."

### Foreign keys

- **What it is.** A column whose value must match a key in another table.
  The database refuses a row that points at nothing.
- **Why it matters.** It prevents orphans, such as a milestone of a run that
  doesn't exist, whatever the application code does.
- **How Agent Factory demonstrates it.**
  - Runs point to projects; milestones and approval requests point to runs.
  - A decision points to its request **including the action fingerprint**.
    The database itself refuses a decision for a different action, and once
    a decision exists, the request's fingerprint can't change.
  - The test proves this with raw SQL that bypasses Python entirely:
    `tests/test_postgres_store.py::test_the_database_itself_enforces_integrity`.
- **In an interview.** "Our favourite foreign key carries the approval
  fingerprint. Even with hand-written SQL, you can't store an approval of
  action A against a request for action B."

### One-to-many relationships

- **What it is.** One row is related to many rows elsewhere. The "many" side
  stores the key of the "one" side.
- **Why it matters.** Most real data looks like this. Modelling it
  explicitly turns "all the X of this Y" into a simple, indexed query.
- **How Agent Factory demonstrates it.**
  - A project has many runs.
  - A run has many milestones and many approval requests.
  - The child tables hold the parent's key, and indexes on those keys serve
    the listings.
- **In an interview.** "Project to runs to milestones is one-to-many twice.
  Listing a run's milestones is an index lookup on the run's key."

### One-to-zero-or-one relationships

- **What it is.** A row is related to at most one row elsewhere, and
  possibly none.
- **Why it matters.** It expresses "optional, but never more than one".
- **How Agent Factory demonstrates it.**
  - An approval request has no decision or exactly one.
  - This is a foreign key plus a `UNIQUE (project_id, request_id)`
    constraint.
  - A test has two separate connections decide the same request at the same
    moment; exactly one decision is stored
    (`test_concurrent_decisions_from_two_devices`).
- **In an interview.** "An approval has zero or one decision. A unique
  constraint makes 'one' a database rule, and we proved it with two
  connections deciding at once."

### Constraints

- **What it is.** Rules the database checks on every write: `NOT NULL`,
  `UNIQUE`, `CHECK`, and foreign keys.
- **Why it matters.** They hold even if application code has a bug, or
  someone edits data by hand.
- **How Agent Factory demonstrates it.**
  - The database guards **structure**: keys, references, allowed statuses,
    ID formats, JSON shapes, and time ordering.
  - The domain model guards **behaviour**, such as which status changes are
    legal. A CHECK constraint can't see a row's old value, so transitions
    stay in code, protected by row locks.
  - A test checks that every allowed-value list in the schema equals its
    Python enum.
- **In an interview.** "We split the job deliberately. The database refuses
  structurally invalid data; the domain refuses invalid behaviour, like
  reopening a finished milestone."

### Indexes

- **What it is.** An extra data structure, like a book's index, that finds
  rows by some columns without reading the whole table.
- **Why it matters.** Queries stay fast as data grows. Every index also
  costs storage and slows writes, so each should serve a real query.
- **How Agent Factory demonstrates it.**
  - Only two indexes were added, each tied to a question:
    - "What happened overnight?": milestones by project and time.
    - "What is waiting for me?": a *partial* index containing only pending
      approvals.
  - Constraint indexes serve every other query.
  - A test checks that each documented query is answered from its index.
  - One known redundancy is documented, with the reason it is kept.
- **In an interview.** "Every index has the query it serves written next to
  it. The pending-approvals index is partial: decided requests, most of
  them over time, aren't in it at all."

### Transactions

- **What it is.** A group of database operations that succeed or fail as
  one unit.
- **Why it matters.** Changes that take several steps must never be left
  half done.
- **How Agent Factory demonstrates it.** Each of these is one transaction:
  - recording a decision: the decision row plus the request's new status;
  - each recorder sync: everything derived from one checkpoint;
  - each update: lock the row, check the transition, write;
  - each migration.

  Nested blocks behave like savepoints in both stores.
- **In an interview.** "Recording an approval writes two rows, in one
  transaction, after locking the request. Two simultaneous approvals can't
  both win."

### Atomicity

- **What it is.** The "all or nothing" property of a transaction: the A in
  ACID.
- **Why it matters.** An error or crash halfway through must leave no
  partial state.
- **How Agent Factory demonstrates it.**
  - Tests force failures in the middle of operations:
    - A decision write that fails after inserting its row leaves nothing.
    - A sync that fails on its last write also undoes the run-status change
      it had already made.
    - A failed migration leaves no tables behind.
  - **There is no atomicity across** the engine's checkpoint file and the
    database. That is documented, and designed for: decisions are written
    to the database first, and reconciliation repairs either direction.
- **In an interview.** "Inside PostgreSQL, everything is atomic. Between the
  checkpoint file and the database there's no shared transaction, so we
  said so and designed for it, rather than pretend."

### Database migrations

- **What it is.** Versioned scripts that change the schema step by step,
  with the database recording which ones have run.
- **Why it matters.** Every environment ends up with the same schema,
  reproducibly, and schema changes are reviewed like code.
- **How Agent Factory demonstrates it.** A small runner:
  - applies numbered SQL files, each in a transaction;
  - records each one's version and checksum;
  - refuses edited history or a newer database;
  - uses an advisory lock so concurrent runs can't interleave.

  Agent Factory checks the schema version at start-up but **never migrates
  itself**. Tests prove fresh builds, repeatability, the refusals,
  rollback, and concurrency.
- **In an interview.** "Applied migrations are immutable: the runner
  checksums them and refuses edited history. Migrating is an explicit step,
  never a side effect of starting the app."

### Repository pattern, with a real database

- **What it is.** The [repository pattern](#repository-pattern) above, now
  with a durable implementation.
- **Why it matters.** It shows the pattern paying off: storage changed and
  the business logic didn't.
- **How Agent Factory demonstrates it.**
  - The PostgreSQL repositories implement the existing interfaces.
  - The shared contract tests pass identically for both stores.
  - The recorder, the approval service, and the engine needed no new SQL.
- **In an interview.** "Adding a real database didn't touch the business
  logic. It was a second implementation behind the same interfaces, held to
  the same tests."

### Domain model vs. persistence model

- **What it is.**
  - The *domain model* is how the code thinks about the business: objects
    with rules.
  - The *persistence model* is how the data is laid out in storage: tables
    and columns.
- **Why it matters.** Keeping them apart lets each be right for its job, and
  stops storage details leaking into business rules.
- **How Agent Factory demonstrates it.**
  - Frozen dataclasses on one side, tables on the other, with explicit
    mapping functions in between.
  - The JSON record envelope isn't stored at all: migrations version the
    tables instead.
  - Every row read is turned back into a domain object, so the domain's
    validation also checks data coming *out* of the database. A row edited
    behind the code's back fails to load
    (`test_a_tampered_row_fails_validation_when_read`).
- **In an interview.** "We map explicitly between objects and rows, and
  validate on the way out as well as in. Even tampered data in the database
  can't become a valid object."

### Durable storage vs. in-memory state

- **What it is.** In-memory state disappears when the process stops; durable
  storage survives restarts and crashes.
- **Why it matters.** An overnight run, a phone approval, and a morning
  summary happen in different processes, hours apart.
- **How Agent Factory demonstrates it.**
  - The in-memory store remains, for fast tests.
  - PostgreSQL keeps product records across processes. One test runs start,
    approve, and resume as **three separate Agent Factory processes**, and
    the test process then finds the complete record.
  - In a manual check, records also survived a restart of the database
    container.
- **In an interview.** "We proved durability end to end: three separate
  processes, then a fourth that only reads, and the record is complete."

### Connection configuration

- **What it is.** How the application learns where the database is and how
  to log in.
- **Why it matters.**
  - Hard-coded credentials leak.
  - Configuration lets the same code run in development, test, and
    production.
- **How Agent Factory demonstrates it.**
  - Exactly one of `AGENT_FACTORY_DATABASE_URL` or
    `AGENT_FACTORY_DATABASE_URL_FILE` must be set; the file variant keeps
    the password out of the environment.
  - Tests use separate variables, so they can never touch the development
    database.
  - The password is never printed: the configuration object is redacted,
    and driver errors are scrubbed.
  - Agent Factory connects as a least-privilege role.
  - An example file contains placeholders only.
- **In an interview.** "The password sits in a git-ignored file that a
  variable points to. Nothing ever prints it, and the app's database role
  can't create databases or roles."

### Database integration testing

- **What it is.** Tests that run against a real database instead of mocks.
- **Why it matters.** Mocks can't prove that constraints, transactions, time
  zones, or concurrency behave the way you assume.
- **How Agent Factory demonstrates it.**
  - 54 PostgreSQL tests: four files, plus the `[postgres]` versions of the
    contract tests. They are opt-in by marker and fail rather than skip
    when requested.
  - Each test gets a fresh, migrated schema that is dropped afterwards.
  - The fixtures refuse any database that isn't local and named for testing.
  - The tests covered:
    - persistence across restarts;
    - rollback;
    - constraints enforced on raw SQL;
    - concurrent decisions;
    - time zones;
    - index use.
- **In an interview.** "Nothing about PostgreSQL is mocked. Each test builds
  its own schema from the migrations and drops it afterwards, and the tests
  refuse to run against anything but a local test database."

---

## APIs and contract-first design

These concepts come from the OpenAPI contract milestone (unreleased branch
`feature/openapi-contract`). The contract is [`openapi.yaml`](../../openapi.yaml)
and the full explanation is [`docs/api-contract.md`](../api-contract.md).
When the contract was written, no server implemented it. The local backend
that now does is covered in
[the next section](#the-backend-serving-the-contract-over-http).

### APIs

- **What it is.** An agreed way for one program to ask another for data or
  actions. A web API does this over HTTP.
- **Why it matters.** Agent Factory's dashboard, phone approval screen, and
  workers will run in different places, so they need one stable way to
  talk.
- **How Agent Factory demonstrates it.** A versioned HTTP API is defined
  for projects, runs, milestones, and approvals, with eleven operations.
- **In an interview.** "The API is the seam between the product's pieces.
  We defined it before writing any server, so the dashboard and backend can
  be built against the same promise."

### REST and resources

- **What it is.**
  - A *resource* is a thing with a stable identity and URL, like
    `/api/v1/projects/{project_id}`.
  - REST is a style that acts on resources through standard HTTP methods,
    rather than through custom commands.
- **Why it matters.** Predictable URLs and methods mean less to learn, and
  standard tools such as caches, clients, and documentation work.
- **How Agent Factory demonstrates it.**
  - The resources are the domain entities: projects, runs, milestones,
    approvals, and decisions.
  - Runs and approvals are **nested under projects**, because their IDs are
    unique only within a project.
- **In an interview.** "The URLs mirror the domain's identity rules. An
  approval ID repeats across projects, so its URL includes the project, and
  the API can never look up the wrong one."

### HTTP methods

- **What it is.** The verb of a request:
  - GET reads, and is safe;
  - POST creates or submits;
  - PUT replaces, PATCH modifies, and DELETE removes.
- **Why it matters.** Methods promise behaviour. Clients and proxies may
  repeat a GET freely, but not a POST.
- **How Agent Factory demonstrates it.**
  - GET for every read; POST only to create a project or record a decision.
  - **No PUT, PATCH, or DELETE anywhere.** There is no milestone editing
    and no run control, and a test enforces this.
- **In an interview.** "The interesting part is what's missing: there's no
  PUT on milestones, because they record what really happened. Generic CRUD
  would have undone the safety model."

### Endpoints

- **What it is.** One method on one path, such as
  `POST /api/v1/projects/{project_id}/approvals/{approval_id}/decision`.
  OpenAPI calls it an *operation* and gives it an `operationId`.
- **Why it matters.** Each endpoint is a promise you must keep. Fewer,
  well-chosen endpoints are easier to secure and maintain.
- **How Agent Factory demonstrates it.**
  - Eleven operations, each with a unique `operationId`.
  - The test suite pins the exact set, so adding one is a deliberate
    decision.
  - Starting runs was **deliberately left out**: the product model can't
    represent a queued run yet.
- **In an interview.** "We chose the smallest contract that serves the
  screens. The overnight summary is a milestone query with a time range,
  not a separate endpoint."

### Request and response models

- **What it is.** The exact shape of the JSON sent and received, defined as
  schemas.
- **Why it matters.** Both sides validate against the same definition.
  Closed request models also stop clients from sending fields they mustn't
  control.
- **How Agent Factory demonstrates it.**
  - A decision request has exactly `outcome`, `action_fingerprint`, and
    `comment`. A body that also has `decided_at`, `actor`, or `verified` is
    refused.
  - Response models carry only what screens need, for example
    `project_name` on an approval for the phone.
- **In an interview.** "The client can't send the decision time or claim a
  verified identity: those fields don't exist in the request schema, which
  is closed."

### Status codes

- **What it is.** The three-digit result of a request: 2xx success, 4xx the
  client's problem, 5xx the server's.
- **Why it matters.** Programs branch on them. Returning 200 for every
  outcome hides failures.
- **How Agent Factory demonstrates it.**
  - 201 for "recorded now", and 200 for "already recorded" (a safe retry).
  - 404 for a missing project or approval.
  - 409 for a conflict with current state: a fingerprint mismatch, or a
    request already decided differently.
  - 422 for invalid input, and 503 when the database is down.
  - 401 when a request is not authenticated, 403 when the identity lacks the
    operation's permission ([Authentication and authorization](#authentication-and-authorization)).
- **In an interview.** "A retried approval returns 200 instead of 201, so
  the phone knows nothing new happened. A different decision gets 409, and
  the response shows what was actually recorded."

### API contracts

- **What it is.** A precise, written agreement about an API:
  - its paths and inputs;
  - its outputs and errors;
  - its rules, such as ordering and idempotency.
- **Why it matters.** Teams can build both sides in parallel, and changes
  become explicit decisions instead of surprises.
- **How Agent Factory demonstrates it.**
  - `openapi.yaml` is the contract. It also states what the API
    **cannot** do: run commands, change run state, edit milestones, or
    change fingerprints.
  - The contract tests prove those capabilities are absent.
- **In an interview.** "Our contract documents negative capabilities as
  carefully as features: there is no route to run a command, edit a
  milestone, or change an approval's fingerprint, and tests check that."

### OpenAPI

- **What it is.** The standard, machine-readable format for HTTP API
  contracts. Version 3.1 uses standard JSON Schema for its data models.
- **Why it matters.** One file drives documentation, client generation,
  request validation, and tests.
- **How Agent Factory demonstrates it.** `openapi.yaml` is OpenAPI 3.1.0,
  and it is validated against the **official OpenAPI 3.1 schema** in the
  test suite, using `openapi-spec-validator`.
- **In an interview.** "The contract is checked by the official OpenAPI
  validator on every test run. Its examples are checked against their own
  schemas too."

### Schema validation

- **What it is.** Checking data against a schema: types, required fields,
  formats, patterns, and allowed values.
- **Why it matters.** Bad input is caught at the edge, with a precise error,
  before it reaches business logic.
- **How Agent Factory demonstrates it.**
  - ID patterns, timestamp formats, and enums in the contract are tested to
    be **identical to the domain's**.
  - Every one of the contract's examples validates against its schema.
  - Example fingerprints are recomputed with the real domain code.
- **In an interview.** "The API's schemas can't drift from the domain: a
  test compares every ID pattern and enum value with the Python code."

### Pagination

- **What it is.** Returning a large collection in pages.
- **Why it matters.** Responses stay small and fast however much history
  builds up.
- **How Agent Factory demonstrates it.**
  - **Cursor (keyset) pagination:** `limit`, an opaque `cursor`, and pages
    of `items` with `next_cursor`.
  - Offset paging was rejected: it skips or repeats items when new
    milestones arrive, and it gets slower deeper in.
  - The cursor follows the same order the database index sorts by.
  - Clients are warned that a milestone whose status changes can appear
    again later in the same listing.
- **In an interview.** "We use cursors, not page numbers. New milestones
  keep arriving overnight, and a cursor continues exactly where you were,
  using the same index that sorts the query."

### Filtering

- **What it is.** Letting a client narrow a collection with query
  parameters.
- **Why it matters.** Clients fetch only what they need. A fixed filter set
  stays secure and fast; an open query language doesn't.
- **How Agent Factory demonstrates it.**
  - Milestones filter by time range (`since` inclusive, `until`
    exclusive), status, and category.
  - The approval inbox can be scoped to one project.
  - There is no general query language.
- **In an interview.** "Filters are a short, fixed list that matches the
  screens. The overnight view is just `since` and `until`."

### Idempotency

- **What it is.** Doing something twice has the same effect as doing it
  once.
- **Why it matters.** Phones retry requests on bad networks, and one tap
  must not become two approvals or two projects.
- **How Agent Factory demonstrates it.** Two strategies, chosen per
  operation:
  - **Approval decisions are naturally idempotent.** The approval holds one
    decision, so an identical retry returns it, and a different one is
    refused. No header is needed, and a key can never be used to sneak in a
    conflicting decision.
  - **Creating a project requires an `Idempotency-Key` header,** so a
    retried request replays the first response instead of creating a
    second project.
- **In an interview.** "For approvals, the approval itself is the
  idempotency key. For project creation, there's no natural key, so the
  client sends one."

### Optimistic concurrency

- **What it is.** Letting writers proceed without locking, then refusing a
  write if the data changed since the writer last looked. Often done with
  version numbers or ETags.
- **Why it matters.** Without it, two editors can silently overwrite each
  other.
- **How Agent Factory demonstrates it.**
  - The decision request carries the fingerprint the user *saw*. The
    server records it only if that still matches, otherwise 409: a
    compare-and-set on the action.
  - Two simultaneous decisions are settled by the database, and exactly one
    wins.
  - There are no in-place updates in v1, so no ETags yet. That decision is
    documented for when there are.
- **In an interview.** "The fingerprint works like an ETag for the action.
  If the screen showed anything other than what's stored, the decision is
  refused."

### API versioning

- **What it is.** Letting an API evolve without breaking existing clients.
- **Why it matters.** Mobile apps can't all update at once.
- **How Agent Factory demonstrates it.**
  - The version is in the path: `/api/v1`.
  - Within v1, only additive changes are allowed.
  - Breaking changes go to `/api/v2`, with deprecation headers.
  - The contract is `1.0.0-draft` until a backend ships.
- **In an interview.** "One version, with clear rules for when the next one
  is needed. We didn't build multiple versions ahead of need."

### API model vs. domain model vs. database row

- **What it is.** The same information in three shapes:
  - the **row** is shaped for integrity and queries;
  - the **domain object** for rules;
  - the **API model** for what a client needs.
- **Why it matters.** Exposing the database or the Python classes directly
  would leak internals, and would tie clients to storage decisions.
- **How Agent Factory demonstrates it.**
  - `ProjectSummary` matches no table or class: it combines a project, its
    latest run, and counts.
  - A milestone's internal `key` and raw metadata are hidden, and `follows`
    and `approval_id` are extracted from the metadata for clients.
  - A test forbids secret or internal field names in any schema.
- **In an interview.** "The API shows curated views. The dashboard's
  project card is three queries' worth of data, shaped for one screen, and
  none of it is a database row."

### Contract-first development

- **What it is.** Writing the API contract before implementing it, then
  building both sides against it.
- **Why it matters.** Disagreements surface while they're cheap, and the
  contract becomes the test both sides must pass.
- **How Agent Factory demonstrates it.**
  - The order was domain model, then database, then the contract, and then
    the backend, which implements the contract unchanged in substance
    ([below](#the-backend-serving-the-contract-over-http)).
  - The contract is based on the real domain, so it describes things that
    exist.
  - Design problems were found before any code: approval IDs aren't
    globally unique, and run creation needs a model change.
- **In an interview.** "We designed the API after the data model, but
  before the server. Writing the contract showed us that starting runs
  remotely needs a queued state the engine doesn't have, before anyone
  built the wrong endpoint."

---

## The backend: serving the contract over HTTP

The contract in the previous section describes the API. These notes cover
the local server that implements it ([backend.md](../backend.md)).
**It listens on localhost only. Every request is authenticated and authorized
([Authentication and authorization](#authentication-and-authorization)).**

### HTTP request/response lifecycle

- **What it is.** Everything that happens between a client sending a
  request and receiving the response.
- **Why it matters.** Each stage is a place to validate, log, or fail
  safely. A bug is much easier to find when you know which stage owns
  which job.
- **How Agent Factory demonstrates it.** One fixed pipeline:
  request ID and Host check → route match → parameter and body validation
  → service → domain rules → repository → PostgreSQL → API view →
  response validation → JSON response and one access-log line. Each step
  lives in its own module (`api/middleware.py`, `api/routes/`,
  `api/services/`, …).
- **In an interview.** "I can trace a request from the socket to the
  database and back, and say which module owns each step: for example,
  validation happens before the service ever runs."

### Web servers (and ASGI)

- **What it is.** The program that accepts network connections and speaks
  HTTP. In Python, the server (Uvicorn) and the application (FastAPI) are
  separate pieces joined by a standard interface, ASGI.
- **Why it matters.** The application doesn't need to know about sockets,
  and the server can be swapped or configured independently: bind
  address, proxy headers, workers.
- **How Agent Factory demonstrates it.** `api/server.py` runs Uvicorn with
  deliberate settings: loopback only, no proxy headers trusted, no server
  banner, its own access log disabled in favour of ours. Tests use the same
  ASGI application in-process, without a socket.
- **In an interview.** "Uvicorn handles HTTP; FastAPI is the app behind the
  ASGI interface. The server config is where 'only listen on localhost' is
  enforced."

### FastAPI

- **What it is.** A Python web framework where routes are ordinary
  functions whose type hints describe their inputs and outputs.
- **Why it matters.** Validation, parameter parsing, and dependency
  injection come from the types, so handlers stay short.
- **How Agent Factory demonstrates it.**
  - Handlers are a few lines: declare parameters, receive a service, call
    it once.
  - FastAPI's generated `/docs` and `/openapi.json` are **switched off**.
    The checked-in `openapi.yaml` is the contract, and a second, generated
    one would drift from it.
- **In an interview.** "I used FastAPI for routing, validation, and
  injection, but not as the source of truth for the contract: that's the
  hand-written OpenAPI file, and tests compare the app against it."

### Routing

- **What it is.** Mapping a method and path, such as
  `GET /api/v1/projects/{project_id}`, to the code that handles it.
- **Why it matters.** The route table *is* the attack surface: every route
  is something a client can call.
- **How Agent Factory demonstrates it.** Exactly the contract's 11
  operations exist under `/api/v1`, plus two infrastructure endpoints. A
  test compares the running route table with `openapi.yaml`, and checks
  that every undocumented method on every path answers 405 without side
  effects.
- **In an interview.** "A test proves there are exactly 11 product
  operations, so nobody can quietly add a 'run this command' endpoint."

### Request validation

- **What it is.** Rejecting malformed or disallowed input before any
  business code runs.
- **Why it matters.** Code behind the boundary can trust its inputs, and
  the client gets a precise error instead of a crash.
- **How Agent Factory demonstrates it.**
  - Bodies must be JSON, at most 64 KiB, with no duplicate keys and no
    `NaN`.
  - Models are closed: an unknown field such as `decided_at` is refused,
    which is how a client is stopped from choosing the decision time.
  - Errors list the field and the rule, never the rejected value, which
    could contain a secret.
- **In an interview.** "Unknown fields are errors, not ignored. That one
  setting is what stops a client from sending its own identity."

### Response validation

- **What it is.** Checking what the server *sends*, not just what it
  receives.
- **Why it matters.** It stops internal fields from leaking and catches
  responses that break the contract.
- **How Agent Factory demonstrates it.** Domain records are converted into
  explicit API models (`services/views.py`), so a new domain field is never
  published by accident. In tests, every response is validated against the
  checked-in contract's schema.
- **In an interview.** "Responses go through explicit view models, and the
  tests validate every response against the contract, not just the
  requests."

### Dependency injection in a web app

- **What it is.** A handler declares what it needs (a service, a validated
  body), and the framework supplies it.
  ([The general idea](#dependency-injection).)
- **Why it matters.** Handlers don't construct database connections, so
  tests can swap the store, the clock, or the signing key.
- **How Agent Factory demonstrates it.** `api/dependencies.py` builds a
  service per request from one application context (store provider,
  clock, cursor codec). The same HTTP tests run against an in-memory
  store and against real PostgreSQL by swapping one provider.
- **In an interview.** "Because the store is injected, the same 50-odd
  HTTP scenarios run against memory and against PostgreSQL."

### Service layer

- **What it is.** One method per use case ("create a project", "record a
  decision"), sitting between HTTP and the domain.
- **Why it matters.** HTTP details stay out of business logic, and
  business logic stays out of HTTP handlers.
- **How Agent Factory demonstrates it.** `api/services/` owns transactions,
  pagination, idempotency, and error codes, and knows nothing about
  requests or status codes. Handlers contain no SQL, filesystem, Docker,
  or state-transition code.
- **In an interview.** "Handlers translate HTTP; services hold the use
  case. You could call a service from a CLI without changing it."

### Repository layer

- **What it is.** Interfaces for storage, so business code asks "give me
  the next page of milestones" without writing SQL.
  ([The general idea](#repository-pattern).)
- **Why it matters.** Storage can change, and the rules are tested once for
  every store.
- **How Agent Factory demonstrates it.** The API needed new queries:
  keyset pages, a cross-project approval inbox, batch counts. Each was
  added to the interface and implemented in both stores, with one shared
  contract test.
- **In an interview.** "New API queries went into the repository interface
  first, then into both stores, with tests that run against each."

### API, domain, and database separation

- **What it is.** Keeping the web layer, the business rules, and the
  storage independent, each depending only on the layer below.
- **Why it matters.** You can change one without breaking the others, and
  security boundaries stay clear.
- **How Agent Factory demonstrates it.** The engine imports no web
  framework and no database driver. The domain works with the in-memory
  store. **The API records intent; workers own execution:** recording an
  approval never resumes a run.
- **In an interview.** "The API can write exactly two things, a project and
  a decision, and neither one runs anything."

### Connection pooling

- **What it is.** Keeping a few database connections open and lending them
  to requests, instead of connecting per query.
- **Why it matters.** Connecting is slow, and an unbounded number of
  connections can overwhelm the database.
- **How Agent Factory demonstrates it.** `persistence/pool.py`:
  - at most 10 connections by default;
  - opened at start-up and closed at shutdown, explicitly;
  - connections health-checked before they are lent;
  - a request that waits more than 3 seconds gets a safe 503 rather than
    queueing forever.

  Tests show an exhausted pool answering 503 and then recovering.
- **In an interview.** "The pool is bounded and fails fast. Under overload
  a client gets a retryable 503, and the database is protected."

### HTTP status codes

- **What it is.** The three-digit number that says what kind of result a
  response is: 2xx success, 4xx the client's problem, 5xx the server's.
- **Why it matters.** Clients decide whether to retry, fix the request, or
  give up from the code alone.
- **How Agent Factory demonstrates it.** 201 for a new project or a first
  decision, 200 for an identical retry, 409 for a conflict (wrong
  fingerprint, already decided), 422 for invalid input, 400 for a bad
  cursor, 503 with `Retry-After` when the database is unreachable.
- **In an interview.** "A retried approval returns 200 rather than 201, so
  the phone can tell 'recorded now' from 'already recorded'."

### Exception and error mapping

- **What it is.** Turning internal exceptions into a fixed set of safe,
  documented client errors.
- **Why it matters.** Raw exception text can contain file paths, SQL, or
  credentials.
- **How Agent Factory demonstrates it.**
  - One table in `api/handlers.py` maps every error type to an RFC 9457
    problem with a stable `code` and a `request_id`.
  - Exception messages are **never** copied into responses or logs.
  - Run failures are explained by curated sentences chosen by the
    exception's *type name*.
  - Tests plant secret, connection-string, and path "canaries" in stored
    data and assert that no response contains them.
- **In an interview.** "Errors are chosen from a fixed list. We plant fake
  secrets in the data and check they never come back."

### Pagination, implemented

- **What it is.** The cursor design from the contract, made real.
  ([The design](#pagination).)
- **Why it matters.** A cursor a client can edit is a cursor a client can
  abuse.
- **How Agent Factory demonstrates it.**
  - A cursor holds the last item's sort key, a digest of the filters, and
    an expiry, signed with HMAC.
  - Edited cursors, cursors reused with different filters, and expired
    cursors all get 400 `invalid_cursor`.
  - Queries fetch `limit + 1` rows to know whether there is a next page,
    so no total count is computed.
- **In an interview.** "Cursors are signed and bound to their filters, so
  you can't tamper with one or reuse it on a different query."

### Idempotency, implemented

- **What it is.** The `Idempotency-Key` design from the contract, made
  durable. ([The design](#idempotency).)
- **Why it matters.** "Retry" must be safe even if the server restarted in
  between.
- **How Agent Factory demonstrates it.**
  - A database table (migration `0002`) stores each key with a hash of
    the request and the original response.
  - The project and its key record are written in **one transaction**, and
    the key's primary key settles races: in a test, 8 simultaneous
    identical requests create exactly one project.
  - A different body with the same key is refused.
  - A replay works after the API restarts with a new connection pool.
- **In an interview.** "The key and the project commit together, so either
  both exist or neither does. The unique key decides who wins a race."

### Integration testing (HTTP)

- **What it is.** Testing through the real interface (HTTP), with the
  real database, rather than calling functions directly.
- **Why it matters.** Most bugs live where pieces meet: routing,
  validation, serialization, transactions.
- **How Agent Factory demonstrates it.** `tests/test_api_http.py` sends real
  HTTP requests through the ASGI app. Every scenario runs twice: against
  memory in the default suite, and against a fresh PostgreSQL schema
  through the production pool with `--run-postgres`.
- **In an interview.** "No route function is called directly in the HTTP
  tests; everything goes through the stack, and against a real database."

### Contract testing: the backend implements the contract

- **What it is.** Two separate checks: the contract is valid, and the
  server matches it.
- **Why it matters.** A valid contract that the server ignores is worse
  than none, because clients believe it.
- **How Agent Factory demonstrates it.**
  - `test_openapi_contract.py` checks the YAML.
  - `test_api_conformance.py` compares the running app with it: routes,
    parameters, status codes, model fields, and closed schemas.
  - The test client validates every response body and header against the
    YAML.
- **In an interview.** "We test both directions: the spec is valid, and the
  implementation conforms to the checked-in spec, not to whatever the
  framework generates."

### Request IDs and structured logging

- **What it is.** A unique ID per request, returned to the client and
  written on every log line, with logs as structured JSON.
- **Why it matters.** A user can quote one ID, and an operator finds every
  related log line.
- **How Agent Factory demonstrates it.**
  - The server generates `req_…` itself; a client-supplied ID is ignored.
  - The ID is returned in `X-Request-Id` and in every error.
  - Access logs record time, method, route template, status, and duration.
  - Logs never include bodies, headers, query strings, or secrets.
  - HTTP logs are separate from the engine's hash-chained execution log.
- **In an interview.** "Every error a user sees has a request ID I can grep
  for, and the logs can't leak a request body because they never contain
  one."

### Liveness vs. readiness

- **What it is.** Two health questions. *Liveness*: is the process alive?
  *Readiness*: can it serve requests right now?
- **Why it matters.** Mixing them up causes damage: restarting a healthy
  API won't fix a database outage.
- **How Agent Factory demonstrates it.** `/healthz` touches nothing and
  always answers 200 while the process runs. `/readyz` borrows a connection
  and checks the schema is fully migrated. It answers 503 with a one-word
  reason (`database_unavailable`, `schema_not_current`) and never connection
  details.
- **In an interview.** "During a database outage the API stays live but not
  ready, so a load balancer stops routing to it without restarting it."

### Local-only service exposure

- **What it is.** Making sure a service is reachable only from the machine
  it runs on.
- **Why it matters.** Authentication now protects every request, but no
  production identity provider is configured, there is no TLS, and no
  deployment has been designed or reviewed. Until then, exposure is a risk
  nobody has assessed.
- **How Agent Factory demonstrates it.**
  - It binds to `127.0.0.1`, and public addresses are refused.
  - A Host-header allowlist blocks DNS-rebinding attacks from web pages.
  - There is no CORS, and writes require JSON.
  - None of this replaces authentication, which every request now also
    needs.
- **In an interview.** "Even with authentication, the server refuses to
  listen anywhere but localhost until a deployment is designed. That's
  enforced in code and tested, not just written in the README."

---

## Durable execution: approval is not execution

These notes cover how Agent Factory makes sure an approved action never runs
twice by accident ([design](../design/action-execution.md),
[safety.md](../safety.md#durable-execution-approval-is-not-execution)).

### Approval vs. execution

- **What it is.** Two different facts. *Approval*: a person authorized one
  exact action. *Execution*: what actually happened when the system tried
  to carry it out.
- **Why it matters.** Treating "approved" as "done" (or as "safe to do
  again") is how a payment gets made twice or an email sent twice.
- **How Agent Factory demonstrates it.** An `ApprovalDecision` records the
  authorization; a separate `ActionExecution` records each attempt and its
  outcome. The design phase found the bug this prevents: a node that needed
  two approvals re-ran its first approved action, and so did a crash. Both
  are now regression tests.
- **In an interview.** "Approval answers *may I*; the execution record
  answers *did I*. We never infer the second from the first."

### Side effects

- **What it is.** Anything an action changes outside the computation
  itself: a deleted file, a sent message, a charged card.
- **Why it matters.** Side effects are what make retries dangerous. Reading
  twice is harmless; paying twice is not.
- **How Agent Factory demonstrates it.** Every tool declares its execution
  semantics: read-only, idempotent, or non-idempotent. A tool in a
  dangerous category that declares nothing is refused when it is
  registered.
- **In an interview.** "Each tool says what happens if it runs twice, and
  the system won't even load a dangerous tool that doesn't say."

### Idempotency

- **What it is.** Doing something twice has the same effect as doing it
  once: naturally ("make sure the file is gone") or with an idempotency key
  the receiver uses to deduplicate.
- **Why it matters.** It turns "retry and hope" into a safe retry.
- **How Agent Factory demonstrates it.** `delete_file` is naturally
  idempotent. A keyed tool receives the same key on every retry of one
  execution (`af1-axe-…-k1`), and only while the provider still remembers
  it: outside its declared window a retry could duplicate, so a person
  decides.
- **In an interview.** "The key belongs to the execution, not the attempt,
  so a retry is recognisably the same request. We never mint a new key per
  retry."

### The exactly-once myth

- **What it is.** The belief that a system can guarantee an external effect
  happens exactly once. With a receiver that cannot deduplicate or report,
  no protocol can: if the process stops after sending and before recording
  the answer, "sent and done" and "never sent" look the same.
- **Why it matters.** Systems that claim it either hide duplicates or hide
  losses.
- **How Agent Factory demonstrates it.** The documentation never claims it.
  It states each class's real semantics instead (below), and makes the
  uncertain case explicit.
- **In an interview.** "We can't know what happened on the other side of a
  crash. So we don't pretend; we record that we don't know, and ask."

### At-most-once, at-least-once, effectively-once

- **What they are.** *At-most-once*: never twice, possibly not at all.
  *At-least-once*: retried until known, possibly twice. *Effectively-once*:
  at-least-once delivery plus a receiver that deduplicates, so the effect
  happens once **if** the receiver keeps its promise.
- **Why it matters.** The right one depends on the action; the wrong one is
  either a lost action or a duplicate.
- **How Agent Factory demonstrates it.** Read-only tools: at-least-once.
  Keyed idempotent tools: effectively-once, relying on the provider.
  Non-idempotent tools: at-most-once automatically; an uncertain attempt is
  resolved by a person.
- **In an interview.** "Payments with idempotency keys are effectively-once;
  an email is at-most-once with a human check. We choose per tool."

### Write-ahead recording

- **What it is.** Durably writing "I am about to do X" *before* doing X.
- **Why it matters.** After a crash, the record proves the action may have
  happened, so recovery is cautious instead of naive.
- **How Agent Factory demonstrates it.** Before an approved tool runs, the
  attempt is committed to the ledger (PostgreSQL commits with
  `synchronous_commit = on`, or an atomic file write) and
  `action_dispatching` is appended, fsync'd, to the hash-chained log. A test
  kills a real child process inside the tool: the record is there afterwards.
- **In an interview.** "We never hold a transaction open across the side
  effect: the intent is committed first, so a crash can't erase it."

### Replay protection

- **What it is.** Answering a repeated request from the record instead of
  performing it again.
- **Why it matters.** Agent Factory re-runs a whole node after a pause or a
  crash; without replay, every approved action in it would run again.
- **How Agent Factory demonstrates it.** A recorded success is replayed to
  the re-run node as a normal tool result; the agent continues as if the
  tool had returned. One approval gives one effect, even if the agent asks
  twice in one visit.
- **In an interview.** "Agents don't implement replay; the gateway does,
  for every approved action, so no agent can get it wrong."

### Crash consistency

- **What it is.** After a crash at *any* point, the stored state still makes
  sense and recovery does the right thing.
- **Why it matters.** Crashes happen at the worst moments: mid-write,
  mid-request.
- **How Agent Factory demonstrates it.** A crash matrix ends a real child
  process (`os._exit`, like `kill -9`) at eight boundaries for two classes of
  tool, then restarts and counts real effects. Writes are atomic (temporary
  file, fsync, rename, directory fsync) or single database transactions.
- **In an interview.** "We tested crashes by actually killing the process at
  each boundary, not just by raising exceptions."

### Uncertain outcomes

- **What it is.** A state that admits "we cannot tell whether this
  happened".
- **Why it matters.** Without it, a system must guess, and both guesses are
  wrong some of the time.
- **How Agent Factory demonstrates it.** An execution is `uncertain` when a
  non-idempotent attempt's outcome is unknown. The run moves to
  `awaiting_reconciliation`, distinct from `blocked` (an infrastructure
  problem), and shows what is known: the action, the attempt, when it
  started, and why the outcome is unknown.
- **In an interview.** "'Unknown' is a first-class state, with its own run
  status and milestone, rather than an error or a guess."

### Reconciliation

- **What it is.** A person comparing the system's record with the outside
  world and saying what actually happened.
- **Why it matters.** Only someone who can look at the email outbox or the
  payment dashboard can settle an uncertain outcome.
- **How Agent Factory demonstrates it.** Three answers: *it happened*; *it
  did not happen, do not retry*; *it did not happen, try once more*. The
  last is a **new** 15-minute authorization bound to the same exact action
  and attempt; the original approval is never reactivated. Each answer is
  recorded once, bound to the fingerprint and attempt the person saw.
- **In an interview.** "A retry after an uncertain outcome is a fresh,
  short-lived authorization, never the old approval coming back to life."

### Fencing and claims (conceptually)

- **What it is.** A *claim* marks who is working on something; a *fencing
  token* is a number that grows with each claim, so writes from an old,
  stale claimant can be recognised and refused.
- **Why it matters.** A worker can be slow rather than dead; without
  fencing, two workers could act on the same thing.
- **How Agent Factory demonstrates it.** One process at a time holds a run
  through an OS file lock, and every ledger change is a compare-and-set on the
  record's version: of six concurrent contenders, exactly one gets to dispatch
  (tested). Since the worker stage, runs also have database claims with
  leases and fencing tokens (`SELECT … FOR UPDATE SKIP LOCKED`); see
  [Distributed workers](#distributed-workers-claims-leases-and-fencing).
- **In an interview.** "The version check is the fence: a writer that read
  an old version changes nothing."

### Why fail-closed applies to uncertainty

- **What it is.** When a safety-relevant fact is unknown, choose the action
  that cannot cause harm.
- **Why it matters.** For a non-idempotent action, "retry" risks a duplicate
  and "assume it ran" risks a silent loss; only waiting is safe.
- **How Agent Factory demonstrates it.** No approved action runs without a
  durable record; an unavailable ledger blocks the run; an uncertain class C
  action stops the run for a person; an expired approval cannot start a
  dispatch. Inference may make the system *more* cautious, never less.
- **In an interview.** "The same rule as the sandbox: if we can't be sure
  it's safe, we don't do it. Here 'sure' includes knowing what already
  happened."

---

## Authentication and authorization

These notes cover how Agent Factory knows who is calling its API and what
they may do ([authentication.md](../authentication.md)).

### Authentication vs. authorization

- **What it is.** Two separate questions. *Authentication*: who is making
  this request? *Authorization*: is that identity allowed to do this?
- **Why it matters.** Mixing them up produces systems where anyone who can
  sign in can do everything, or where a refusal does not tell the user what
  to fix.
- **How Agent Factory uses it.** Every API request first proves who it comes
  from (a verified token naming an enrolled principal), then must hold the
  one permission its operation requires. The failures differ: 401 for the
  first, 403 for the second.
- **How to explain it in an interview.** "Authentication answers *who are
  you*, authorization answers *what may you do*. We check both on every
  request, in that order, and report them differently."

### Principals

- **What it is.** The system's own record of an identity that may act: a
  person today, perhaps a service later.
- **Why it matters.** Permissions, audit trails, and decisions need a stable
  name for "who", independent of how that person signs in.
- **How Agent Factory uses it.** A principal has a random `prn-` ID, an
  active or disabled status, and granted permissions. The owner enrolls
  principals with the CLI; a valid token for anyone not enrolled is refused.
  Approval decisions record the principal, and the database requires it.
- **How to explain it in an interview.** "We map the identity provider's
  account to our own principal ID once, and everything else (permissions,
  decisions, logs) refers to that ID."

### Permissions and least privilege

- **What it is.** A permission is one named capability, such as
  `approvals:decide`. *Least privilege* means granting only what a job
  needs.
- **Why it matters.** If reading implies deciding, every viewer can approve
  a production deployment.
- **How Agent Factory uses it.** Seven permissions, exactly one required per
  operation, declared in the contract (`x-permission`) and enforced by one
  server-side check that a test compares with the contract. No permission
  implies another: reading approvals does not allow deciding, deciding does
  not allow reconciling, creating projects does not allow reading them.
- **How to explain it in an interview.** "Each endpoint names one permission
  in the OpenAPI file, and a test fails if the code ever enforces a
  different one."

### OAuth 2.0 and OpenID Connect

- **What it is.** OAuth 2.0 is a standard for issuing *access tokens* that
  let a client call an API. OpenID Connect adds identity on top: a standard
  way to learn *who* signed in (issuer, subject, profile claims).
- **Why it matters.** Using them means not writing your own login system:
  the identity provider handles passwords, passkeys, multi-factor sign-in,
  and recovery, which are hard to get right.
- **How Agent Factory uses it.** The API is an OAuth 2.0 *resource server*:
  it never sees a password and never issues tokens; it verifies the access
  tokens an OpenID Connect provider issues. The provider is chosen per
  deployment (none is configured yet).
- **How to explain it in an interview.** "OAuth is about access, OpenID
  Connect is about identity. Our API trusts a provider's signed tokens and
  keeps authorization for itself."

### JWT access tokens

- **What it is.** A JSON Web Token: base64url header, claims, and
  signature. The signature lets anyone with the issuer's *public* key check
  that the claims were not changed.
- **Why it matters.** A JWT can be verified without calling the provider on
  every request, but only if the verification is strict: famous bugs came
  from accepting `alg: none` or letting the token choose the algorithm.
- **How Agent Factory uses it.** It accepts only allowlisted asymmetric
  algorithms, uses the key the provider published *for that algorithm*,
  requires `exp`, `iat`, `iss`, `aud`, and `sub`, and refuses tokens issued
  for more than a day. A test signs an HMAC token with the RSA public key
  (the algorithm-confusion attack) and expects a refusal.
- **How to explain it in an interview.** "The key decides how the signature
  is checked, never the token. That closes the algorithm-confusion hole."

### Issuer and subject: identity is not an email address

- **What it is.** In OpenID Connect, `iss` names the provider and `sub` the
  account at that provider. The pair is stable and never reassigned.
- **Why it matters.** Email addresses change and can be reassigned to
  someone else; display names are not unique. Keying identity on them lets
  a new owner of an old address inherit someone's access.
- **How Agent Factory uses it.** Principals are unique on `(issuer,
  subject)`; email and name are display only and follow the token's claims.
  A token with the same email but another subject is refused (tested).
- **How to explain it in an interview.** "We identify people by issuer plus
  subject. Email is a label we display, never a key."

### Verifying tokens: keys, rotation, and failing closed

- **What it is.** The provider publishes its public keys (a JWKS
  document); the API fetches and caches them, and providers rotate keys
  over time.
- **Why it matters.** A verifier that cannot get keys must not guess, and
  one that refetches on every unknown key ID can be made to hammer the
  provider.
- **How Agent Factory uses it.** Keys are cached for 10 minutes; an unknown
  key ID triggers one refresh, at most once a minute; if keys cannot be
  fetched, the last good set serves for an hour, then every request gets 503
  until they return. Only public signature keys are accepted.
- **How to explain it in an interview.** "If we can't verify, we refuse with
  503. We never fall back to trusting a token."

### 401 vs. 403

- **What it is.** 401 Unauthorized means "not authenticated: who are you?";
  403 Forbidden means "authenticated, but not allowed".
- **Why it matters.** The client's fix differs: sign in again, or ask for a
  permission. And neither should leak account details.
- **How Agent Factory uses it.** No credential: 401
  `authentication_required`. Any refused credential (bad signature,
  expired, wrong issuer, unknown or disabled account): the *same* 401
  `invalid_token`, so the response reveals nothing about accounts. Missing
  permission: 403, naming only the permission required. Authorization comes
  before any lookup, so a 403 never reveals whether something exists.
- **How to explain it in an interview.** "401 says who are you, 403 says not
  you. Every refused token gets the same answer, so attackers learn
  nothing."

### Hiding a button is not authorization

- **What it is.** The rule that the server decides what a user may do,
  whatever the user interface shows.
- **Why it matters.** Anyone can send an HTTP request by hand, so a hidden
  button protects nothing.
- **How Agent Factory uses it.** Every request meets the same permission
  check. Permissions claimed in the token, in headers, or in the request body
  are ignored; only grants in Agent Factory's store count (tested).
- **How to explain it in an interview.** "The frontend may hide what you
  can't do, for convenience. The server enforces it, for security."

### Test authentication vs. production authentication

- **What it is.** Tests and local development need tokens without a real
  identity provider; production must never accept those tokens.
- **Why it matters.** "Test mode" back doors (a magic header, a bypass
  flag) are a classic way for production systems to be breached.
- **How Agent Factory uses it.** There is no bypass: test and development
  tokens are really signed and checked by the production code; only the
  trusted issuer differs. Production is the default mode, refuses to start
  unconfigured, refuses the development issuer in every form, and refuses
  mixed settings. Even a production configuration pointed at the
  development keys refuses development tokens (tested).
- **How to explain it in an interview.** "Our test tokens go through the real
  verifier. Production can't trust the test issuer because it refuses that
  issuer's name, its keys, and the mode."

---

## Distributed workers: claims, leases, and fencing

These notes cover how several worker processes share one database without
two of them owning the same run, and why a stalled worker cannot do damage
when it wakes up ([workers.md](../workers.md)).

### Distributed workers

- **What it is.** Several independent processes take work from a shared
  store and do it, instead of one process doing everything.
- **Why it matters.** It gives throughput, and survival when one process
  dies. It also creates the hard part: processes can stall, die, or lose the
  network at any moment, and the others cannot tell which.
- **How Agent Factory uses it.** `python -m agent_factory worker run` starts
  one worker process, and starting it again starts another. They share
  PostgreSQL, claim queued runs, advance them, and give them back. Nothing
  starts workers automatically.
- **How to explain it in an interview.** "Workers coordinate only through the
  database. That keeps the protocol simple to reason about: every decision
  about who owns what is a row and a transaction."

### Work claiming

- **What it is.** Before working on an item, a worker records durably that
  it owns it, so that others leave it alone.
- **Why it matters.** Without a claim, two workers pick the same item. With
  a claim that is only held in memory, a restart forgets it.
- **How Agent Factory uses it.** `run_claims` has one row per run: the owner,
  its lease, and its fencing token. A claim is one short transaction; the run
  executes afterwards, outside any transaction.
- **How to explain it in an interview.** "The claim is the committed row, not
  a lock held for the whole job. Holding a transaction open while running
  would pin a connection, and a crash would roll back the record of what was
  already done."

### `SELECT … FOR UPDATE` (database locking)

- **What it is.** Reading rows while locking them, so that no other
  transaction can change them until this one ends.
- **Why it matters.** "Read, decide, write" races when two transactions read
  the same row before either writes. The lock turns the race into a queue.
- **How Agent Factory uses it.** The claim query locks the row it hands out.
  Fenced writes take `FOR SHARE` on the claim row, which conflicts with the
  claim's `FOR UPDATE` and with renewals. A takeover therefore cannot happen
  in the middle of a fenced write: each write is ordered entirely before or
  entirely after a change of owner (tested).
- **How to explain it in an interview.** "Row locks give us ordering. `FOR
  SHARE` on the claim during a write means a takeover waits for, or skips,
  that write, and never interleaves with it."

### `SKIP LOCKED`

- **What it is.** An option to `FOR UPDATE` that skips rows another
  transaction has locked, instead of waiting for them.
- **Why it matters.** Many workers polling one table would otherwise queue up
  behind each other on the same row. With `SKIP LOCKED`, each takes a
  different one.
- **How Agent Factory uses it.** Ten workers claiming ten runs at once each
  get a different run, and none waits. A run another claimer is locking is
  skipped, not waited on. Both are tested, and removing `SKIP LOCKED` made a
  test fail.
- **How to explain it in an interview.** "It turns a table into a work queue
  without a separate queue product: competing consumers, no blocking, one
  winner per row."

### Leases

- **What it is.** Ownership with an expiry time. The owner must keep
  renewing it, or it lapses and someone else may take over.
- **Why it matters.** A dead worker cannot release what it holds, so without
  an expiry its runs would be stuck forever.
- **How Agent Factory uses it.** The default lease is 60 seconds (2 seconds
  to 5 minutes allowed; the database refuses longer). A crashed worker's run
  is taken over about a minute later. An expired lease is lost for good, even
  if nobody took over yet: only a new claim, with a new token, can win the
  run back.
- **How to explain it in an interview.** "A lease bounds how long a dead
  owner can block progress. It's a liveness tool. It doesn't give safety by
  itself; fencing does."

### Heartbeats

- **What it is.** Periodic "I'm still alive" renewals that keep a lease from
  expiring.
- **Why it matters.** The renewal rate has to tolerate a few lost heartbeats,
  while still letting a dead owner's lease run out promptly.
- **How Agent Factory uses it.** A heartbeat every 20 seconds, three per
  lease, from a background thread on its own database connection, so a long
  tool call never delays it. A dropped connection is replaced. Heartbeats are
  logged at DEBUG and never become milestones.
- **How to explain it in an interview.** "Three heartbeats per lease means
  two can fail without losing the run. A separate connection means a renewal
  never waits behind the work."

### Zombie workers

- **What it is.** A worker that stalled (a long pause, a frozen VM, a
  network partition), lost its lease, and wakes up still believing it owns
  the work.
- **Why it matters.** It is the classic failure of lease-based systems. The
  zombie's late writes overwrite the new owner's, or repeat its side effects.
- **How Agent Factory uses it.** A real zombie process is tested. It never
  renews, and it always believes its lease is valid. It wakes after another
  worker took the run over, and recording its in-flight action's outcome is
  refused. Its checkpoint never advances, and the next owner recovers the
  action from the zombie's own log evidence without sending it again.
- **How to explain it in an interview.** "You can't prevent zombies, because
  you can't tell slow from dead. What you can do is make their writes fail."

### Why a lease alone is insufficient

- **What it is.** The gap between checking a lease and acting on it.
- **Why it matters.** "My lease was valid when I checked" is always about
  the past. A process can stall for any time between the check and the write.
- **How Agent Factory uses it.** Nothing trusts the worker's own view. The
  primary acceptance test gives worker A a clock on which its lease is still
  valid, and the database refuses A's writes anyway, because worker B holds a
  newer generation.
- **How to explain it in an interview.** "The check has to happen where the
  write happens, in the same transaction. Otherwise there is always a window."

### Fencing tokens (monotonic)

- **What it is.** A number that increases with every new owner. Every write
  carries it, and the storage refuses writes whose number is not the current
  one.
- **Why it matters.** It closes the window that leases leave open, and it
  tells apart two generations of the same worker: a worker that lost a run
  and then claimed it again.
- **How Agent Factory uses it.** Each claim adds exactly 1 to the run's
  token, and renewing or releasing never changes it. Every write for a
  claimed run starts its transaction with "token = current and lease valid".
  Generation 41 is refused once 42 exists. Removing the token comparison, or
  not incrementing it, made tests fail.
- **How to explain it in an interview.** "The token is checked by the
  storage, not by the worker. A stale worker can believe anything; its write
  still fails."

### Compare-and-set

- **What it is.** "Change this value only if it is still what I read."
- **Why it matters.** It makes concurrent updates safe without long locks: of
  two writers that read the same version, exactly one succeeds.
- **How Agent Factory uses it.** The execution ledger changes only by
  compare-and-set on a version, and claims change only for the current
  `worker_id` and `fencing_token`. A stale heartbeat or release changes
  nothing.
- **How to explain it in an interview.** "Fencing is compare-and-set on
  ownership: the update's `WHERE` clause names the generation I think I own."

### Race conditions

- **What it is.** A result that depends on the timing of concurrent events.
- **Why it matters.** Races hide in testing and appear in production. They
  need tests that make the dangerous timing happen.
- **How Agent Factory uses it.** Two kinds of tests, both against a real
  PostgreSQL:
  - timing races, run in threads released by a barrier and repeated: 2 and
    10 workers for one run, 10 workers for 10 runs, a renewal against a
    takeover;
  - forced interleavings, where the takeover happens at the exact dangerous
    point: just before a checkpoint, just before and just after a dispatch's
    write-ahead record.
- **How to explain it in an interview.** "Random timing rarely hits the worst
  case, so the most important races are forced deterministically, at the
  exact boundary."

### Leader/owner concepts

- **What it is.** At most one participant has authority over something at a
  time: a leader of a cluster, or an owner of a task.
- **Why it matters.** Two leaders (split brain) means conflicting decisions.
- **How Agent Factory uses it.** Ownership is per run, not per cluster: each
  run has at most one owner, and different runs are owned by different
  workers at once. There is no global leader to elect.
- **How to explain it in an interview.** "Per-item ownership avoids leader
  election entirely. The database row is the authority, and fencing makes a
  stale owner harmless."

### Clock considerations

- **What it is.** Different machines' clocks disagree and can jump. Time
  comparisons across processes therefore need one clock.
- **Why it matters.** If each worker judged leases by its own clock, two
  could each believe they own the same run.
- **How Agent Factory uses it.** Lease validity is decided only by
  PostgreSQL's `now()`. The boundary instant belongs to expiry: valid while
  `now() < lease_expires_at`, expired once `lease_expires_at <= now()`, never
  both (tested inside one transaction). Workers use a monotonic clock for
  their own deadline, which may only make them stop earlier.
- **How to explain it in an interview.** "One authoritative clock for
  decisions, monotonic clocks for local timeouts, and the boundary assigned
  to exactly one side."

### Crash recovery

- **What it is.** Continuing correctly after a process dies at any point.
- **Why it matters.** Crashes happen between any two steps. Recovery has to
  be right for every one of them.
- **How Agent Factory uses it.**
  - A worker that dies inside its claim transaction claims nothing.
  - One that dies right after committing keeps the run until its lease
    expires, and then another worker takes over with the next token.
  - Approved actions that were in flight are settled from log evidence, or by
    the action's class.
  - All of this is tested with real processes and a real database restart.
- **How to explain it in an interview.** "Recovery is the normal path for a
  takeover: the next owner always starts by settling whatever the previous
  one left open."

### At-least-once scheduling

- **What it is.** Every item is eventually processed, possibly more than
  once, because "did the worker finish?" cannot always be known.
- **Why it matters.** Work must be safe to redo. Exactly-once processing is
  not something a scheduler can promise.
- **How Agent Factory uses it.** A run may be advanced by several workers
  over time, and a node interrupted by a takeover runs again. That is safe
  because checkpoints commit only completed nodes, and approved side effects
  replay recorded outcomes instead of repeating. An uncertain non-idempotent
  action waits for a person.
- **How to explain it in an interview.** "Scheduling is at-least-once;
  effects are protected separately: idempotent where possible, replayed where
  recorded, and handed to a person where unknown."

### Graceful shutdown

- **What it is.** Stopping without abandoning work halfway.
- **Why it matters.** Killing a worker mid-action turns a known outcome into
  an uncertain one.
- **How Agent Factory uses it.** On the first Ctrl+C or SIGTERM, the worker
  stops claiming, finishes the current node (never interrupting a dispatch),
  commits it, and releases the run for another worker. A second signal stops
  it at once, and the lease then expires. Tested with a real Ctrl+Break sent
  to a worker process.
- **How to explain it in an interview.** "Yield at safe points only. A forced
  stop falls back on the same crash recovery as any other death."

### Worker observability

- **What it is.** Seeing what workers do: claims, renewals, releases,
  takeovers, rejections.
- **Why it matters.** Distributed failures are diagnosed from events, not
  stack traces.
- **How Agent Factory uses it.**
  - JSON events: `worker_started`, `run_claimed`, `lease_renewed` (DEBUG),
    `run_released`, `lease_expired`, `run_taken_over`,
    `stale_worker_rejected`, `database_unavailable`, `worker_stopped`.
  - Each holds identifiers, generations, and reasons from a closed
    vocabulary; never URLs, paths, or exception text.
  - The run's own log records the owner and generation of every dispatch.
- **How to explain it in an interview.** "Every ownership change and every
  refusal is an event with the generation number, so you can reconstruct who
  owned what, when."

## Human decisions in a distributed system

These notes cover how a person's decision, made on a phone at any time,
reaches a worker process safely: recorded, woken, re-checked, applied once
([workers.md](../workers.md#human-decisions-wake-ups-and-consumption)).

### Asynchronous human-in-the-loop workflows

- **What it is.** A workflow that stops at a point only a person can decide,
  waits as long as it takes (minutes, a night), and continues when the
  person answers, from anywhere, while nothing is running.
- **Why it matters.** People are slow and unpredictable compared with
  programs. A process that sits waiting for them holds resources, and dies
  with the person's context the moment it crashes or is redeployed.
- **How Agent Factory uses it.** A run that needs approval checkpoints and
  stops (`awaiting_approval`); no process waits for it. The person decides
  through the API; a worker picks the run up afterwards, re-checks the
  decision, and continues. Expiry (12 hours) bounds how long a decision can
  arrive.
- **How to explain it in an interview.** "The wait is data, not a blocked
  thread: the run's state says what it waits for, and any worker can
  continue it once the answer is in the database."

### Command vs. execution

- **What it is.** Separating "a person asked for X" (a command, or intent)
  from "X was done" (execution).
- **Why it matters.** The two happen at different times, in different
  processes, and either can fail. Treating a stored command as if it were
  the result is how actions get skipped, or done twice.
- **How Agent Factory uses it.** `decideApproval` and `reconcileAction`
  store intent and return; they never touch the checkpoint or the execution
  ledger (a static test keeps execution code out of the API). The worker
  executes, and the execution ledger records what actually happened. The
  approval view shows both: the decision, and the execution timeline.
- **How to explain it in an interview.** "An approval row is permission, not
  proof. What happened is a separate record, written by the only component
  allowed to act."

### Durable intent

- **What it is.** Writing a request to durable storage before anything acts
  on it, so it survives crashes of everyone involved.
- **Why it matters.** If intent lives only in memory or in a message on its
  way, a crash loses what the person decided, and they may not know it.
- **How Agent Factory uses it.** A decision or answer is a committed row
  (`approval_decisions`, `reconciliation_decisions`) before the HTTP
  response is sent. Crash tests kill workers at every step after that; the
  decision is still applied, by whoever claims the run next.
- **How to explain it in an interview.** "Once we say 201, the decision is
  committed. Everything after that is recovery-safe because it starts from
  that row."

### Work eligibility (the transactional outbox)

- **What it is.** Deciding *when* a piece of work should be picked up, and
  recording that in the same transaction as the change that caused it (an
  "outbox" in the same database, rather than a separate message broker).
- **Why it matters.** Writing the decision and then notifying a queue are two
  operations: a crash between them leaves a decision nobody acts on, or a
  notification for a decision that was rolled back.
- **How Agent Factory uses it.** The decision and `UPDATE run_claims SET
  available_at = now()` commit together. A run waiting for a person is not
  wanted by workers at all, except at its approval's expiry, so nothing
  polls it. A counter (`wake_seq`) makes sure a worker releasing the run
  can't overwrite a wake-up that arrived while it held the run (a real race
  the tests found).
- **How to explain it in an interview.** "The queue is a column next to the
  claim. One transaction records the decision and makes the run claimable,
  so they can't disagree."

### Polling (and why not notifications)

- **What it is.** Workers asking the database for work at an interval,
  instead of being pushed a message when work appears.
- **Why it matters.** Pushes are fast but can be lost (no listener at that
  moment, a dropped connection). Polling is slower but cannot miss anything
  that is in the table.
- **How Agent Factory uses it.** Idle workers run the claim query every 2
  seconds. PostgreSQL's LISTEN/NOTIFY was evaluated and not adopted: a
  person deciding is on a human timescale, notifications are lost while no
  worker listens, and correctness would still need polling.
- **How to explain it in an interview.** "Correctness comes from polling;
  a notification could only ever be an optimization, and 2 seconds of latency
  on a human decision wasn't worth a second mechanism."

### Reconciliation (by a person)

- **What it is.** Settling a disagreement between what a system knows and
  what happened in the outside world, here by asking a person who can check.
- **Why it matters.** When a non-idempotent action (an email, a payment)
  times out after sending, the system cannot know whether it happened.
  Guessing either way is wrong: retrying may duplicate it, skipping may lose
  it.
- **How Agent Factory uses it.** The run waits (`awaiting_reconciliation`)
  and the inbox lists it. Someone with `actions:reconcile` answers for
  exactly one attempt: it happened, it did not, or it did not and may be
  tried once more. "Once more" is a new 15-minute authorization from the
  person's decision; the original approval is never reused.
- **How to explain it in an interview.** "There's no 'I don't know, retry
  anyway' button: a retry asserts that it didn't happen, the person who says
  so is recorded, and the window is short."

### Idempotent consumption

- **What it is.** Processing a message or decision so that doing it twice
  (after a crash, by two workers) has the same effect as doing it once.
- **Why it matters.** Distributed systems redeliver. Anything that is applied
  "at least once" must be safe to apply again.
- **How Agent Factory uses it.** A decision is applied to the checkpoint;
  once applied, the run no longer waits for it, so it is never applied again.
  If a worker dies between applying and checkpointing, the next one applies
  it again, and the execution ledger guarantees the action itself still runs
  once (crash test K3). Answers are applied through the ledger's
  compare-and-set, which records one reconciliation however many workers try
  (race C11).
- **How to explain it in an interview.** "We consume at least once and make
  the effect happen once: the checkpoint and the ledger are the proofs of
  what was already applied."

### Worker identity vs. human identity

- **What it is.** Keeping the two kinds of actor apart: people, who are
  authenticated and authorized, and infrastructure processes, whose
  authority is their role and their current claim.
- **Why it matters.** If a worker acted with a person's token, the audit
  trail would lie about who did what, and a stolen token would reach the
  engine.
- **How Agent Factory uses it.** Every decision row names the person
  (their principal). Every application and every attempt names the worker
  and its fencing generation (`wkr-…@N`). Workers never hold tokens; the
  worker re-checks that the person may *still* decide (enrolled, active,
  permitted) when it applies their decision.
- **How to explain it in an interview.** "The timeline answers both
  questions separately: which person decided, and which worker generation
  did it."

### Distributed race handling

- **What it is.** Designing for events that happen in any order across
  processes: two people deciding at once, a decision arriving while a worker
  looks, an expiry racing a claim.
- **Why it matters.** Races are rare in testing and certain in production.
  Each one needs a rule that decides the outcome, not luck.
- **How Agent Factory uses it.** One decision per request and one answer per
  attempt (unique keys); claims with `FOR UPDATE SKIP LOCKED`; fencing on
  every write; expiry by server time; a wake-up counter. Eleven race tests
  run real sessions against PostgreSQL, some holding a worker at an exact
  point while the test acts.
- **How to explain it in an interview.** "For each race we can name the rule
  that settles it and the test that forces it, not just hope the timing
  works out."

### The attention inbox

- **What it is.** One list of everything that needs a person now, decided by
  the server.
- **Why it matters.** A phone's first screen should answer "what needs me?"
  in one request, without the client understanding engine states or merging
  several lists.
- **How Agent Factory uses it.** `listAttention` returns decisions to make
  (pending and not expired) and uncertain actions to answer, in one order,
  with one cursor. Expiry and "already answered" are applied on the server.
- **How to explain it in an interview.** "One endpoint instead of two
  queries, because the merge and the expiry rules belong to the server, not
  to every client."

### The execution timeline

- **What it is.** The steps an approved action went through, in order, shown
  to the person who approved it.
- **Why it matters.** "Approved" doesn't tell anyone what happened. A person
  who approves from a phone needs to see later whether the action ran,
  failed, or needs them again.
- **How Agent Factory uses it.** `getApproval` returns a timeline built only
  from structured records (the request, the decision, the ledger's attempts,
  answers, and reconciliations), never from raw logs, so it holds no paths or
  internals. Steps recorded in the same instant keep their causal order.
- **How to explain it in an interview.** "The timeline is a projection of the
  ledger, safe to show on a phone because it's built from fields we chose,
  not from log text."

### Why API requests should not execute long-running agent work

- **What it is.** Keeping HTTP requests short: they record what was asked
  and return, while separate workers do the long work.
- **Why it matters.** A phone's connection can drop at any moment, proxies
  time out, and an API process may be restarted. Work tied to a request is
  lost or duplicated when that happens; a client retrying a slow request
  can start the work twice.
- **How Agent Factory uses it.** Deciding returns as soon as the decision
  and the wake-up are committed (201, or 200 for a repeat). The run, which may
  take minutes and call tools, runs in a worker under a claim, where crashes,
  takeovers, and retries are handled.
- **How to explain it in an interview.** "The API's job is to make intent
  durable and say so. Execution needs leases, fencing, and recovery, so it
  lives where those exist."

---

## Real model providers

How Agent Factory uses a large language model (LLM) without giving it
authority ([model-providers.md](../model-providers.md)).

### Model provider abstraction

- **What it is.** One small interface, `ModelProvider.complete(request) ->
  response`, in front of every model service: a fake, Anthropic, Bedrock,
  OpenAI, or a local model.
- **Why it matters.** The workflow, budgets, and safety rules must not depend
  on one vendor's SDK. Switching providers, or testing without one, should
  change an adapter only.
- **How Agent Factory uses it.**
  - Agents never see a provider: they ask the `ModelGateway`, which calls
    the configured `ModelProvider`.
  - The deterministic `FakeProvider` drives every test and demo.
  - `AnthropicProvider` is the first real adapter (standard-library HTTPS).
- **How to explain it in an interview.** "Vendors are adapters. Everything
  that matters (budgets, validation, permissions) is ours and provider-neutral."

### Inference

- **What it is.** Running a trained model on an input to produce an output:
  one request, one billed answer, measured in tokens.
- **Why it matters.** Each call costs money and time, can fail or time out,
  and returns something new every time.
- **How Agent Factory uses it.** Inference happens only inside a provider
  call made by the gateway. Each call has a deadline, a token cap, a budget
  check, and a ledger entry.
- **How to explain it in an interview.** "A model call is an expensive,
  unreliable remote call, and we treat it exactly like one."

### Prompts as code

- **What it is.** Treating the instructions sent to a model as versioned
  source code, not strings scattered through classes.
- **Why it matters.** A changed prompt changes behaviour. Without versions
  you cannot tell which prompt produced which output.
- **How Agent Factory uses it.**
  - `agents/prompts.py` holds one template per role: a shared security
    preamble, the instructions, and the output schema. Version 2
    (`planner/2`, `developer/2`, `reviewer/2`) is in use; version 1 stays
    defined, unchanged, so old audit records still mean something.
  - Every call records its template version.
  - A test pins each template's digest, old versions included, so an edit
    without a new version fails.
- **How to explain it in an interview.** "Prompts are versioned and
  reviewed like code, and every call says which version it used. When a
  live run showed a prompt problem, we released a new version instead of
  editing the old one."

### Model-provider tools versus agent capabilities

- **What it is.** Two different things that are both called "tools":
  - a **model-provider tool** is a function the model may *call* in its
    reply (the `tools` list of an API request);
  - an **agent capability** is an action the *system* may perform: read or
    write a file, run tests, commit.
- **Why it matters.** If a model is shown capability names, it may try to
  call them as tools, even when they are not offered. That breaks the
  answer, or, in a careless design, lets the model act directly.
- **How Agent Factory uses it.**
  - Each request offers exactly one provider tool, `submit_result`, which
    carries the role's structured answer.
  - Capabilities appear only as a description of what Agent Factory may do
    *after* validating that answer, through the Tool Gateway.
  - A project file cannot add or rename a tool.
  - Any other tool call is ignored, never executed. The audit records only
    counts: unexpected calls, how many used a manifest name, how many used
    an unknown name; never the name or its arguments.
- **What the live run showed (2026-10-06).** With version-1 prompts, Claude
  answered 4 of 9 times by calling capability-like tools instead of
  `submit_result`, which cost re-asks and one failed review. Version 2 says
  plainly "The capability names below are NOT callable tools in this model
  request … The ONLY callable response tool is submit_result". In the next
  run all 6 answers were valid at the first attempt, with 0 unexpected
  calls. One run is evidence, not a guarantee; the counts stay in the audit
  to catch a relapse.
- **How to explain it in an interview.** "The model fills in one form,
  `submit_result`. Our capabilities aren't its tools: it describes the work,
  and our gateway decides what actually runs."

### Structured outputs

- **What it is.** Making the model answer with data that matches a schema
  (JSON), instead of free prose.
- **Why it matters.** Prose cannot safely become state or actions. A schema
  makes the answer checkable.
- **How Agent Factory uses it.**
  - The Anthropic adapter offers one tool, `submit_result`, and asks the
    model to call it once (`claude-sonnet-5-5` refuses forced tool use, so
    the request uses `tool_choice: auto`).
  - Agent Factory then validates it against a strict schema: no unexpected
    keys, bounded strings and lists, relative paths only.
  - An invalid answer gets one corrective re-ask, then the node fails.
- **How to explain it in an interview.** "The model fills in a form we
  designed, and we check the form. Schema-valid still doesn't mean
  authorized."

### Context windows

- **What it is.** The limited amount of text a model can read in one
  request.
- **Why it matters.** Sending too much costs money, slows answers, fails on
  the limit, and leaks data the model never needed.
- **How Agent Factory uses it.** `ContextBuilder` sends each role only what
  it needs:
  - the planner gets file names;
  - the developer gets the planned files and the feedback;
  - the reviewer gets the changed files and the test evidence.

  Secret-looking files are withheld, host paths redacted, sizes capped, and
  every file sent is listed in the audit record.
- **How to explain it in an interview.** "Minimum necessary context: chosen
  per role, bounded, and logged. Never 'send the repo'."

### Token budgets

- **What it is.** Hard limits on how many calls and tokens a call, a step,
  or a whole run may use.
- **Why it matters.** An agent loop can spend without bound; a budget makes
  spending a policy instead of an accident.
- **How Agent Factory uses it.**
  - Per call, per visit, and per run limits, plus an optional dollar ceiling
    when prices are configured.
  - They are checked before every call, and the call is reserved in a
    restart-safe ledger before it is sent.
  - When a budget is exhausted the run blocks; nothing keeps spending.
- **How to explain it in an interview.** "We check the budget before the
  call, write the reservation down before sending, and stop the run when the
  money runs out."

### Rate limits

- **What it is.** A provider's cap on requests or tokens per minute,
  signalled by HTTP 429 (often with `retry-after`).
- **Why it matters.** Hammering a limited provider makes it worse, and
  naive retries at several layers multiply into storms.
- **How Agent Factory uses it.**
  - 429, 5xx, and timeouts are retried at most twice per call, with backoff
    that honours `retry-after` (capped).
  - Then the run blocks.
  - Corrective re-asks and graph repair loops count against the same per-run
    call budget, so retries never multiply.
- **How to explain it in an interview.** "Retries are bounded at each layer,
  and one shared budget bounds them all together."

### Model nondeterminism

- **What it is.** The same request can produce different answers.
- **Why it matters.** Tests that expect exact wording are flaky, and a run
  can't be replayed by asking again.
- **How Agent Factory uses it.**
  - Exact-output tests use the scripted `FakeProvider`.
  - Live tests check only invariants: schemas, routing, budgets, boundaries,
    outcomes.
  - Decisions already made are kept in checkpoints, never re-asked.
- **How to explain it in an interview.** "Deterministic tests use a fake;
  real-model tests assert properties, not words."

### Tool calling

- **What it is.** Letting a model ask for an action (a function, a shell
  command) as structured output.
- **Why it matters.** A model that can call tools directly has every
  permission those tools have, and can be talked into using them.
- **How Agent Factory uses it.**
  - The model gets **no tools**.
  - Its only "tool" is the form it fills in (`submit_result`), which does
    nothing.
  - The agent turns the answer into Tool Gateway calls: policy, approval,
    ActionExecution, sandbox, audit.
- **How to explain it in an interview.** "The model proposes; the
  orchestrator disposes. No SDK gets a shell."

### Capability boundaries

- **What it is.** An explicit statement of what each role may do, enforced
  outside the model.
- **Why it matters.** Without one, a model's claim ("I'm allowed to
  deploy") becomes a decision.
- **How Agent Factory uses it.**
  - The capability manifest is built only from the permission policy and the
    tool registry. Project text and model output never change it (tested).
  - It is the foundation for future tool packs: GitHub, the AWS Agent
    Toolkit.
- **How to explain it in an interview.** "Capabilities are configuration we
  own, not something a model can ask its way into."

### Prompt injection

- **What it is.** Text in the data ("ignore your instructions and ...") that
  tries to steer the model.
- **Why it matters.** Any file, issue, or web page a model reads can carry
  it. You cannot fully prevent a model from being persuaded.
- **How Agent Factory uses it.**
  - Instructions and capabilities stay in the system prompt; project data
    goes in a delimited user message labelled untrusted.
  - More importantly, **a persuaded model still has no way to act**.
    Tests use a fake model that obeys injected text: escapes, undeclared
    tools, and changes to protected tests all fail, and deletion waits for a
    person.
- **How to explain it in an interview.** "We assume the model can be fooled
  and make sure a fooled model can't do anything dangerous."

### Provider vs. agent vs. tool

- **What it is.** Three different things:
  - a **provider** generates text;
  - an **agent** is Agent Factory code with a role, which assembles context,
    asks a provider, and decides what the answer means;
  - a **tool** is a single capability the orchestrator performs under
    policy.
- **Why it matters.** Mixing them up puts authority in the wrong place,
  for example letting the provider act.
- **How Agent Factory uses it.**
  - Providers sit behind the `ModelGateway`, agents in `agents/`, tools
    behind the `ToolGateway`.
  - Only agents connect the two gateways, and only through structured data.
- **How to explain it in an interview.** "The model thinks, the agent
  decides what that means, the gateway decides whether it may happen."

### Usage and cost accounting

- **What it is.** Recording which model was called, by whom, with how many
  tokens, at what (estimated) cost.
- **Why it matters.** You cannot control or explain spending you did not
  record, and you must not claim a cost you cannot compute.
- **How Agent Factory uses it.**
  - The usage ledger records each call before and after it is sent; a crash
    counts the call at its reservation, and nothing is counted twice.
  - The checkpoint carries a summary, stored in PostgreSQL and shown by the
    API and dashboard.
  - A dollar figure appears only when prices are configured, labelled an
    estimate.
- **How to explain it in an interview.** "Write-ahead accounting: if the
  process dies mid-call, we assume the call was billed."

## Capabilities and cloud access

- **Design:** [design/capability-registry-aws.md](../design/capability-registry-aws.md).
- **Implemented, offline:**
  - the registry, tool packs, evidence, availability, and assessment
    ([capabilities.md](../capabilities.md));
  - the `cloud_read` category;
  - an AWS pack with a fake backend and a real backend that has only run
    against stubs ([aws-capabilities.md](../aws-capabilities.md)).
- **Not done yet:** any real AWS call (Stage E2 waits for authorization), and
  cloud workload identity (design only).

### Capability registry

- **What it is.** A catalogue of what a system can actually do: each
  capability's identity, version, effects, cost and risk, and its
  *evidence* (implemented, tested, live-tested, available now).
- **Why it matters.** An agent system must answer "can we do this job?"
  from facts, not from what a language model believes.
- **How Agent Factory uses it.** Packs register capabilities as data. The
  manifest a model sees is a filtered view of the registry. The registry
  describes; it never authorizes.
- **How to explain it in an interview.** "The registry says what exists and
  what's proven; the gateway decides what runs."

### Tool registry

- **What it is.** The table of implementations: the code that performs an
  action, keyed by tool name.
- **Why it matters.** A capability needs exactly one accountable
  implementation, with declared execution semantics.
- **How Agent Factory uses it.** `ToolRegistry` already refuses a dangerous
  tool without declared semantics. Each capability points to one tool.
- **How to explain it in an interview.** "Capabilities are the menu; tools
  are the kitchen."

### Tool pack

- **What it is.** A bundle that registers a family of capabilities, their
  tools, their permission needs, and an availability probe: core, git,
  AWS, later GitHub.
- **Why it matters.** New integrations arrive without touching the core,
  and each can be enabled, disabled, and tested on its own.
- **How Agent Factory uses it.** A pack is enabled by explicit
  configuration, never because a credential is lying around.
- **How to explain it in an interview.** "Plugins with a security
  contract."

### Least privilege and IAM

- **What it is.** IAM is AWS's identity and access system. Every AWS call
  is signed by an identity and checked against its policies. Least
  privilege means that identity gets only the actions and resources it
  needs.
- **Why it matters.** Whatever the software does wrong, IAM limits the
  damage it can do.
- **How Agent Factory uses it.** One role per capability family and
  environment (for example a read role limited to named log groups). Never
  `AdministratorAccess`. IAM changes are never an agent capability. IAM
  allowing an action never removes Agent Factory's own approval.
- **How to explain it in an interview.** "Approval decides whether to act;
  IAM caps what acting can ever do. They're independent locks."

### AWS SDK vs. AWS CLI

- **What it is.** The SDK (boto3 in Python) makes typed API calls from
  code. The CLI is a command-line program for people and scripts.
- **Why it matters.** A typed SDK call can be validated, fingerprinted for
  approval, and recorded. "Run this CLI command" can do anything the
  identity allows.
- **How Agent Factory uses it.** Each AWS capability is a typed boto3
  adapter with a fixed operation. The model never gets the CLI, and never
  gets AWS's "run any script" tool either.
- **How to explain it in an interview.** "We give the agent buttons, not a
  terminal."

### Model tool vs. application capability

- **What it is.** A model tool is a function the model may call in its
  reply. An application capability is an action the application may
  perform.
- **Why it matters.** Confusing them hands action selection to the model.
  Agent Factory's first live run showed Claude trying to call capability
  names as tools.
- **How Agent Factory uses it.** One provider tool (`submit_result`).
  Capabilities are described, and requested as typed intent inside the
  answer. AWS capabilities follow the same rule.
- **How to explain it in an interview.** "The model fills in a form; the
  application decides which of its powers to use."

### Availability probe

- **What it is.** A safe check that a capability is usable *now*: the
  dependency is installed, an identity exists, permission is granted, the
  region is supported.
- **Why it matters.** Implemented isn't the same as available. Failing
  early with a clear reason beats failing halfway through a run.
- **How Agent Factory uses it.** Probes are read-only and free (for example
  AWS `GetCallerIdentity`). They print no secrets, and distinguish
  `not_configured`, `no_identity` and `permission_denied`. A failed probe
  makes the capability `UNAVAILABLE`, which is fail closed.
- **How to explain it in an interview.** "Health checks for powers, not
  servers."

### Credential chain

- **What it is.** The ordered places an AWS SDK looks for credentials:
  environment variables, the shared credentials and config files (static
  keys, SSO, `aws login`, assumed roles, credential processes), and the
  workload role.
- **Why it matters.** Whatever is found first wins, so a stray environment
  variable or an old key file can silently become the identity used.
- **How Agent Factory uses it.** Each capability family names a profile
  slot explicitly. A profile resolving to long-term keys is refused. The
  local options are `aws login` (temporary, refreshed every 15 minutes, 12
  hours at most) or IAM Identity Center.
- **How to explain it in an interview.** "Be explicit about which identity
  you use, and refuse the long-lived kind."

### Cloud workload identity

- **What it is.** In the cloud, the running service itself has an IAM role
  (an ECS task role, an instance profile), and AWS hands it rotating
  temporary credentials.
- **Why it matters.** No access key exists to leak, rotate, or commit.
- **How Agent Factory uses it (design).** The worker's role may only assume
  per-family roles, with session tags tying every AWS call to a run. The
  API process gets no AWS role.
- **How to explain it in an interview.** "In production the workload is the
  identity; there are no keys."

### Capability evidence

- **What it is.** Proof attached to a capability: a passing offline test, a
  recorded live acceptance, a fresh probe.
- **Why it matters.** "Can we do this Upwork job?" must be answered from
  evidence, and say what's missing or needs a person, not from a model's
  confidence.
- **How Agent Factory uses it.** Evidence is computed, each item with its
  source:
  - `IMPLEMENTED`: a tool exists;
  - `TESTED`: a named test exists, found by parsing the test file;
  - `LIVE_TESTED`: only a recorded run of that exact version;
  - `AVAILABLE_NOW`: a probe passed;
  - `REQUIRES_APPROVAL`, and `UNAVAILABLE` with a reason.

  Documentation is never evidence. Nothing is live-tested yet, because no
  live run has been recorded.
- **How to explain it in an interview.** "We don't say yes because the LLM
  said yes; we say yes because the test suite and a live check did."

### Availability vs. implementation

- **What it is.** Two different facts: the code exists (implemented), and it
  can work right now (available).
- **Why it matters.** The AWS pack's code exists, but without an approved
  identity it can't do anything. Treating "installed" as "able" is how
  agents promise what they can't deliver.
- **How Agent Factory uses it.**
  - Every capability has an availability status with a fixed reason
    (`not_configured`, `no_identity`, `permission_denied`, …).
  - The Tool Gateway denies an unavailable capability before running
    anything.
  - The registry never infers availability from an importable library.
- **How to explain it in an interview.** "Having the code is necessary,
  not sufficient; we check the identity and permissions before claiming the
  power."

### Typed cloud adapters

- **What it is.** One small tool per cloud action, wrapping exactly one API
  operation with typed parameters, for example S3 `HeadObject` on a
  designated bucket.
- **Why it matters.** A typed action can be validated, approved by its exact
  parameters, recorded, and filtered.
- **How Agent Factory uses it.**
  - Each AWS tool calls one fixed operation through a narrow backend
    interface (only a fake exists today).
  - Its answer is projected onto known fields, redacted, and size-bounded
    before anything sees it.
- **How to explain it in an interview.** "Each cloud action is a function
  we wrote, not a string the model wrote."

### Why arbitrary cloud commands are dangerous

- **What it is.** Giving an agent "run any CLI command", "call any API", or
  "run this script with my credentials".
- **Why it matters.**
  - One approval would cover an unbounded program.
  - Arguments can't be validated, and idempotency can't be declared.
  - Results can't be filtered.
  - The identity's full permissions become the model's.
- **How Agent Factory uses it.**
  - The AWS pack has no generic call, no SDK method names from callers, no
    script runner, and no CLI. A static test checks that no module imports
    an SDK or runs a process.
  - AWS's own Agent Toolkit offers a `run_script` tool, which is deliberately
    not used.
- **How to explain it in an interview.** "We give the agent buttons, not a
  terminal: a terminal plus credentials is the whole account."

### Credential slots

- **What it is.** An abstract name for *which* identity a capability uses
  (`aws.dev.readonly`), bound by configuration to one named profile.
- **Why it matters.** It keeps capabilities free of credentials, keeps dev,
  staging, and prod separate, and makes "no fallback" enforceable.
- **How Agent Factory uses it.**
  - An unbound slot is refused, never defaulted.
  - The profiles `default` and `internal-dev` can't be bound.
  - Before any call, the credential's *kind* must be temporary (SSO,
    `aws login`, a role). Long-term keys and unknown kinds are refused.
- **How to explain it in an interview.** "Capabilities name a seat, not a
  key, and only short-lived keys may sit in it."

### IAM metadata

- **What it is.** The exact cloud permissions a capability would need,
  written down with it: `s3:GetObject` and `s3:ListBucket` for S3 metadata,
  `sts:GetCallerIdentity` for identity.
- **Why it matters.** Least-privilege policies are written from it, and
  reviews can see that nothing asks for `*`.
- **How Agent Factory uses it.**
  - Wildcards are refused at registration.
  - Identity, credential, and organization actions (`iam:`,
    `sts:AssumeRole`, …) force a capability into the denied class.
  - IAM metadata documents; it never authorizes.
- **How to explain it in an interview.** "Every capability carries its own
  least-privilege shopping list, and the list can't contain a wildcard."

### Evidence-based capability assessment

- **What it is.** Comparing requested work against the registry: supported,
  unavailable, missing, requires approval, requires a person, each with
  evidence.
- **Why it matters.** It's the honest answer a marketplace integration
  (such as Upwork) would need, and it can't be talked into a yes.
- **How Agent Factory uses it.**
  - `assess()` is deterministic and calls no model or network.
  - A model may later help list the requirements; the verdict is the
    registry's.
  - It submits or promises nothing.
- **How to explain it in an interview.** "The model can draft the checklist;
  only the registry may tick the boxes."

### `cloud_read`: an external read is not a local read

- **What it is.** An action category for read-only access to an external
  cloud system. A local read (`read_workspace`) touches only the project's
  own files; a cloud read uses the network and a cloud identity, and can
  disclose information about a whole account.
- **Why it matters.** "Read-only" doesn't mean "harmless": reading an
  account's identity, logs or object metadata can leak a lot. It deserves
  its own policy, not the local-file rule.
- **How Agent Factory uses it.**
  - Added contract-first: `openapi.yaml`, the API, the domain, the
    frontend types, and a database migration.
  - Allowed in development, configurable in staging, and approval-required
    in production (a production policy that allows it can't be built).
  - A `cloud_read` capability must be read-only, low-cost, typed, bounded,
    and slot-bound, and a tool that may mutate can never register as one.
- **How to explain it in an interview.** "We separated 'read my files' from
  'read my cloud account', because only one of them can leak the account."

### boto3/botocore sessions and credential resolution

- **What it is.** An AWS SDK session holds configuration and finds
  credentials through a *credential chain*:
  - environment variables;
  - the shared credentials file;
  - the config file (SSO, `aws login`, roles, credential processes);
  - the container endpoint;
  - the EC2 instance metadata service at `169.254.169.254`.

  Each SDK "provider" handles one of these and labels its result (`sso`,
  `login`, `env`, `shared-credentials-file`, …).
- **Why it matters.** Whatever answers first wins. The order and the labels
  are SDK internals, so we read them in botocore's source instead of
  guessing.
- **How Agent Factory uses it.** `BotoAwsBackend`:
  - never asks for a default session;
  - builds exactly one provider (the approved kind, for one named,
    allowlisted profile);
  - points the session's credentials file at nowhere;
  - passes the resolved credential to the client explicitly.
- **How to explain it in an interview.** "We don't let the SDK go looking
  for credentials; we hand it the one we approved."

### Why implicit credential chains are dangerous in autonomous agents

- **What it is.** Relying on "whatever credentials the SDK finds".
- **Why it matters.**
  - On a developer machine the chain finds long-term admin keys, a stray
    environment variable, or another project's profile.
  - On a server it finds the instance's role.
  - An autonomous agent can't notice that it's acting as the wrong identity,
    and it acts at machine speed.
- **How Agent Factory uses it.** Fail closed instead:
  - a profile missing → not configured, never "use default";
  - static keys, unknown kinds, or expired credentials → refused before any
    call;
  - tests plant static keys in the environment and the credentials file, and
    fail if any other provider is consulted.
- **How to explain it in an interview.** "Ambient credentials are a
  confused-deputy bug waiting for an agent."

### Temporary credentials and IAM Identity Center

- **What it is.** Credentials that expire (minutes to hours) and carry a
  session token, instead of long-term access keys. IAM Identity Center is
  AWS's single sign-on: a person signs in (`aws sso login`), and the SDK
  exchanges that sign-in for short-lived role credentials from a
  *permission set* assigned to one account.
- **Why it matters.** A leaked temporary credential expires on its own, and
  a permission set can be made exactly as narrow as one tool needs.
- **How Agent Factory uses it.**
  - Only temporary kinds are accepted, and only with a known expiry more than
    a minute away.
  - The recommended E2 identity is a new `agent-factory-dev` profile backed
    by an Identity Center permission set that denies everything.
  - `aws login` was the other option, but botocore needs an extra library
    (`awscrt`) for it.
- **How to explain it in an interview.** "Every key we use dies within the
  hour, and the role behind it can do one thing."

### Workload roles

- **What it is.** In the cloud, the running service has an IAM role, and
  AWS delivers rotating credentials to it through the container endpoint
  or the instance metadata service.
- **Why it matters.** No key exists to leak. But a workload role is broad by
  nature, because the whole service shares it.
- **How Agent Factory uses it (design).**
  - The worker's role may only assume per-capability-family roles, with
    session tags.
  - The workload's own credentials are never used directly: `iam-role` and
    `container-role` are refused as credential kinds.
  - The API process gets no AWS role.
- **How to explain it in an interview.** "The service's identity is a
  springboard into narrow roles, never the identity that acts."

### STS and `GetCallerIdentity`

- **What it is.** STS (Security Token Service) issues and describes AWS
  credentials. `GetCallerIdentity` answers "who am I?": the account, the
  ARN, and the user ID of whatever signed the request.
- **Why it matters.** It's the safest possible first live call:
  - it changes nothing and costs nothing;
  - AWS documents that it needs no permission and that even an explicit
    deny can't block it;
  - it proves that authentication works end to end.
- **How Agent Factory uses it.**
  - `aws.identity.inspect` wraps exactly this operation and returns only the
    principal type and name and the account's last four digits.
  - It never returns the ARN, user ID, or role session name, which for SSO
    is the person's email.
- **How to explain it in an interview.** "Our first cloud call is 'whoami'
  against an identity that is denied everything else."

### Live-tested evidence vs. availability

- **What it is.** Two different claims:
  - `LIVE_TESTED`: this exact capability version once passed an
    *authorized* live check in this environment;
  - `AVAILABLE_NOW`: the probe passed just now.
- **Why it matters.** Unit tests prove code paths, not that AWS accepts our
  identity. A recorded live pass proves that once, but says nothing about
  today's credentials.
- **How Agent Factory uses it.**
  - Live evidence comes only from a hash-chained, reviewed file in the
    repository, appended by an authorized verification. It holds a digest
    of the result, never the result.
  - It counts only for the same version and environment, from a real
    backend.
  - A broken chain voids all of it.
  - Availability comes only from probes.
- **How to explain it in an interview.** "Tested says the code is right,
  live-tested says it worked against the real thing once, available says
  it would work right now; we never let one stand in for another."

## Attention notifications

Telling a person that a run needs them, without letting the message itself
decide anything ([notifications.md](../notifications.md)).

### Notifications vs. authorization

- **What it is.** A notification says "something needs your attention".
  Authorization is a verified person deciding about one exact action. They
  are different jobs, done in different places.
- **Why it matters.** Messages are forwarded, read over shoulders, and
  spoofed. If a message could approve an action, anyone holding it could.
- **How Agent Factory demonstrates it.**
  - The message has only the project's name, the reason, and a dashboard
    link.
  - The decision happens in the authenticated dashboard; a worker then
    re-checks it against the exact pending action.
  - No API route sends a message or accepts a recipient (contract tests).
- **In an interview.** "The text tells you to look. Approving happens after
  you sign in, on the exact action, and a worker re-checks it."

### Why an SMS reply does not approve an action

- **What it is.** "Reply YES to approve" turns a phone number into a
  credential.
- **Why it matters.** Sender numbers can be spoofed, phones are shared or
  stolen, and a reply carries no proof of which exact action was meant or
  whether it changed since.
- **How Agent Factory demonstrates it.**
  - Nothing reads replies.
  - The only way to decide is `decideApproval`, with a verified token and the
    action's fingerprint.
- **In an interview.** "A reply proves someone had the phone, not who they
  are or what they saw. Authority stays behind sign-in and the exact-action
  fingerprint."

### Transactional outbox

- **What it is.** Write "this must be sent" into the same database
  transaction as the change that requires it; send it later, from that
  table.
- **Why it matters.** Sending inside the transaction can send for a change
  that is then rolled back. Sending after the commit can be lost if the
  process dies in between. The outbox has neither problem.
- **How Agent Factory demonstrates it.**
  - The recorder's sync stores the approval request and its notification
    intent in one transaction.
  - Tests roll it back in memory and in PostgreSQL, and neither half
    survives.
  - A sweep re-creates any missing intent from the attention inbox.
- **In an interview.** "Approval required and 'tell the owner' commit
  together. A dispatcher reads the outbox; a crash between them cannot lose
  the alert."

### Delivery semantics

- **What it is.** The honest promise about how many times a message
  arrives: at most once, at least once, or (with help from the receiver)
  effectively once.
- **Why it matters.** "Exactly once" needs the provider to recognise a
  repeat. Without that, a crash after the provider accepted, but before the
  sender recorded it, leaves the sender unable to know.
- **How Agent Factory demonstrates it.**
  - At least once, with deduplication where possible.
  - A real dispatcher process killed after the fake provider accepted is
    recovered as "outcome unknown" and sends again. The person receives two
    alerts, and the dashboard says "possibly more than once".
- **In an interview.** "A duplicate is annoying; a lost approval alert
  leaves the run waiting all night. So: at least once, shown honestly."

### At-least-once delivery

- **What it is.** Retry until success is recorded, accepting that a
  success may happen more than once.
- **Why it matters.** It is the only safe choice when losing a message
  costs more than repeating it, and the receiver cannot deduplicate.
- **How Agent Factory demonstrates it.**
  - An attempt without an answer is `unknown`, never "sent" and never "not
    sent", and it is retried.
  - Each attempt carries the notification's ID as an idempotency key, for
    providers that support one.
- **In an interview.** "Unknown is a real state. I retry it, record that a
  duplicate is possible, and never pretend I know."

### Deduplication

- **What it is.** Giving each logical message a stable identity, so that
  repeats of the same event collapse into one.
- **Why it matters.** Systems re-run code. A checkpoint is synced many times,
  and a sweep re-reads the inbox; neither may send twenty texts for one
  approval.
- **How Agent Factory demonstrates it.**
  - `dedup_key` names the attention event: the approval, or one uncertain
    attempt.
  - The notification ID is derived from it and the destination, and
    `UNIQUE (dedup_key, destination_id)` keeps one row however often it is
    written.
  - Retries reuse that row.
- **In an interview.** "Identity comes from the event, not from when we
  noticed it. Re-recording is a no-op; a second uncertain attempt is a new
  event."

### Retries and backoff

- **What it is.** Trying again after a failure, waiting longer each time,
  with randomness ("jitter"), up to a limit.
- **Why it matters.** Immediate retries hammer a struggling provider, and
  synchronized retries cause storms. Without a limit, a dead address
  retries for ever.
- **How Agent Factory demonstrates it.**
  - 30 s, then doubling to a 30-minute cap, with equal jitter, for at most 8
    attempts (`RetryPolicy`).
  - The delay counts from the end of the attempt, in PostgreSQL's time.
  - Tests check the schedule, the jitter bounds, and the limit.
- **In an interview.** "Exponential backoff with jitter and a ceiling. And
  when it gives up, the approval is still in the dashboard."

### Transient vs. permanent failures

- **What it is.** Classifying a failure by whether trying again could
  succeed.
- **Why it matters.** Retrying a rejected phone number is pointless and can
  get an account flagged; not retrying a timeout loses the message.
- **How Agent Factory demonstrates it.**
  - A closed vocabulary. Timeouts, an unavailable provider, and rate
    limiting are retried; an invalid destination, a configuration rejection,
    and an unsupported channel fail at once.
  - Provider error text is never stored, only the category.
- **In an interview.** "Adapters translate provider errors into our
  categories; the outbox decides what to do from the category alone."

### Provider abstraction

- **What it is.** One small interface (`send(message) -> provider ID`, or a
  classified error) in front of every SMS, email, or push service.
- **Why it matters.** The outbox, retries, and tests must not depend on one
  vendor. Switching from a fake to SES, or from SES to Twilio, should be an
  adapter.
- **How Agent Factory demonstrates it.**
  - `NotificationProvider`, and `FakeNotificationProvider`, which scripts
    success, transient and permanent failures, timeouts, a lost response,
    and hangs.
  - The whole architecture is proven with the fake before any account
    exists.
- **In an interview.** "I built and broke the pipeline with a fake provider
  first. The real provider is the last, smallest piece."

### Dispatcher workers

- **What it is.** A background process that claims queued messages, sends
  them, and records the results.
- **Why it matters.** Sending from request handlers or graph nodes ties
  delivery to their lifetimes. A separate worker keeps going when the
  browser, the API, or a run's worker is gone.
- **How Agent Factory demonstrates it.**
  - It claims with `FOR UPDATE SKIP LOCKED` under a lease, and fences every
    later write with a claim token, as the run workers do.
  - It never holds a transaction open while calling the provider.
  - Tests kill real dispatcher processes at each boundary, and stop
    PostgreSQL under it.
- **In an interview.** "Same pattern as the run workers: short claims,
  leases, fencing, and nothing slow inside a transaction."

### Notification privacy

- **What it is.** Treating phone numbers and email addresses as personal
  data, and keeping message content to the minimum.
- **Why it matters.** Logs, error messages, and API responses leak. A
  message on a lock screen is visible to anyone nearby.
- **How Agent Factory demonstrates it.**
  - Addresses are normalized and stored once, and only ever shown redacted.
    They are never copied into notification rows, events, errors, or API
    responses (tests scan for them).
  - Messages contain no tool, parameter, path, or secret.
- **In an interview.** "The address lives in one table. Everything else
  refers to it by ID, and anything shown to a person is redacted."

### Deep links

- **What it is.** A URL that opens a specific item in an app.
- **Why it matters.** A link that carries a token is a credential in the
  clear: it ends up in browser history, proxies, and shared screenshots.
- **How Agent Factory demonstrates it.**
  - The link is the dashboard's origin plus `/projects/<id>/approvals/<id>`;
    the origin is validated (no credentials, query string, or fragment).
  - Opened without a session it shows sign-in, and afterwards the same
    approval. Tested in Chrome.
- **In an interview.** "The link says where to look, not who you are. Sign-in
  says who you are."

## Testing

### Automated testing

- **What it is.** Code that checks code, run automatically and repeatably.
- **Why it matters.** It lets you change things with confidence, and it's
  the only scalable way to verify AI-written code.
- **How Agent Factory demonstrates it.** At v0.2.0:
  - 392 non-live tests;
  - 16 live Docker smoke tests;
  - 44 live adversarial tests.

  On the unreleased `feature/product-data-foundation` branch there are 516
  non-live tests: the 392 from v0.2.0 plus 124 new product-domain tests.

  On the unreleased `feature/postgres-persistence` branch:
  - 560 non-live tests;
  - 54 PostgreSQL integration tests;
  - 18 live Docker tests: the 16 smoke tests plus 2 that test the boundary
    between the sandbox and the database;
  - the 44 adversarial tests, unchanged.

  On the unreleased `feature/openapi-contract` branch:
  - 595 non-live tests in the project venv: the 560 above plus 35 API
    contract tests;
  - the PostgreSQL (54), live (18), and adversarial (44) suites, unchanged
    and passing.

  On the unreleased `feature/backend-api` branch:
  - 764 non-live tests passing in the project venv, including the
    in-memory HTTP API, conformance, and API unit tests;
  - 121 PostgreSQL integration tests, including the HTTP API scenarios
    against real PostgreSQL through the connection pool;
  - the live (18) and adversarial (44) suites, unchanged and passing.

  Every phase ended with the full suite passing.
- **In an interview.** "Nothing was accepted on the strength of 'the code
  looks right'. Each stage had to pass the full suite, and new behaviour came
  with new tests."

### Unit vs. integration vs. live tests

- **What it is.**
  - *Unit* tests check one piece in isolation, usually with fakes.
  - *Integration* tests check pieces working together.
  - *Live* tests use real external systems.
- **Why it matters.** Unit tests are fast and precise; integration and live
  tests catch what fakes hide. You need all three, kept clearly apart.
- **How Agent Factory demonstrates it.**
  - The default suite never touches Docker; a guard fails any test that
    tries.
  - Engine integration tests run real workspaces and real test processes.
  - Live suites need explicit opt-in flags (`--run-docker-live`,
    `--run-docker-adversarial`) and *fail* rather than skip when Docker is
    missing. See [`docs/testing.md`](../testing.md).
- **In an interview.** "The default suite needs no Docker and takes under
  20 seconds on my machine. Live Docker suites are opt-in, and they refuse to
  silently skip, so a 'pass' always means the containers really ran."

### Mutation testing

- **What it is.** Deliberately breaking the code (a "mutant") and checking
  that the tests notice. If they don't, the tests are weaker than they look.
- **Why it matters.** Passing tests prove nothing if they would also pass
  with the bug.
- **How Agent Factory demonstrates it.**
  - During development, key safety checks were temporarily broken — for
    example, letting the local backend run untrusted code — and the suite
    was confirmed to fail.
  - This was done manually and recorded in commit messages and reports.
    *There is no automated mutation-testing tool in the repository*; adding
    one is possible future work.
  - A related lesson: a code review caught a test whose assertion could
    never fail. Tests need reviewing too.
  - **Product data foundation:** 13 deliberate mutations; the final suite
    catches all of them. Examples are skipping the action-fingerprint
    check, accepting a second conflicting decision, and swallowing storage
    errors. One mutation exposed a missing test, now added: nothing had
    checked that a decision binds to the action the engine will actually
    run. See [`docs/ai-workflow.md`](../ai-workflow.md#3-automated-tests).
  - **PostgreSQL persistence:** 17 deliberate mutations. Examples include
    removing the sync transaction, the identifier collation, the edited
    migration check, the advisory lock, and the credential filter for the
    sandbox. Tests catch 16 of them.
    - The 17th removed the Python fingerprint check from the PostgreSQL
      decision write.
    - It "survived" by design: the database's own binding foreign key still
      refused the forged decision, with the same error.
    - Removing that foreign key is in turn caught.

    That's defence in depth, observed rather than assumed.
- **In an interview.** "For the security-critical checks, we broke each one
  on purpose and confirmed the tests failed. Tests are code, and they can be
  wrong."

### Adversarial testing

- **What it is.** Testing with deliberately hostile inputs or programs,
  trying to break the system's guarantees.
- **Why it matters.** Security claims are only as good as the attacks
  they've survived.
- **How Agent Factory demonstrates it.**
  - [`tests/test_sandbox_adversarial.py`](../../tests/test_sandbox_adversarial.py)
    runs hostile programs (`tests/hostile/*.hpy`) inside the sandbox. They
    attempt host file reads and writes, network access, secret theft, and
    resource exhaustion, among other things.
  - The programs can never run on the host: a guard allows only the Docker
    CLI to start during these tests.
  - The suite found two real defects:
    - an archive-handling crash that reported a copy-in failure as a test
      failure;
    - a CPU-limit kill that was mislabelled.

    Both were fixed.
- **In an interview.** "We wrote hostile programs and proved containment
  against them. That process found and fixed two real bugs, which is why
  it's worth doing."

---

## Security

### Trust boundaries

- **What it is.** The lines between parts of a system that trust each other
  and parts that don't.
- **Why it matters.** Security work is mostly about what crosses those lines
  and how it's checked.
- **How Agent Factory demonstrates it.**
  - The orchestrator is trusted; code written by a real model is untrusted.
  - Trust is tracked explicitly. Providers are untrusted by default, and a
    workspace that ever received untrusted code is marked untrusted for good
    ([`agent_factory/sandbox/types.py`](../../agent_factory/sandbox/types.py),
    [`docs/sandbox.md`](../sandbox.md)).
- **In an interview.** "The key boundary is between the orchestrator and the
  code the AI writes. Anything from a real model is untrusted, and that label
  can only ever get stricter."

### Sandboxing

- **What it is.** Running untrusted code in a restricted environment, so
  whatever it does stays contained.
- **Why it matters.** AI-generated code can be wrong, or steered to be
  malicious. It must not reach your files, network, or secrets.
- **How Agent Factory demonstrates it.** The Docker backend runs code:
  - with no network;
  - with a read-only system;
  - as a non-root user with no capabilities;
  - with the project copied in, so no host folder is shared;
  - with explicit limits;
  - and verifies cleanup afterwards.

  [`docs/sandbox.md`](../sandbox.md) states exactly what this does and does
  not guarantee.
- **In an interview.** "Generated tests run in a hardened container: no
  network, no host mounts, no root, capped resources. The docs also say
  plainly that container escape is outside the guarantee."

### Fail-closed design

- **What it is.** When something is uncertain or broken, stop safely rather
  than continue with weaker protection.
- **Why it matters.** "Fail open" is how a temporarily missing safety check
  becomes a silent, permanent hole.
- **How Agent Factory demonstrates it.**
  - If Docker is down, misconfigured, unhealthy, or fails its self-test,
    untrusted code doesn't run *at all*. It never falls back to running
    locally.
  - The run is `blocked`, consumes no retries, and resumes once fixed.
  - Tests: `tests/test_sandbox_default.py::test_untrusted_code_never_falls_back_to_local`
    and `tests/test_sandbox_adversarial.py::test_14_fail_closed`.
- **In an interview.** "If isolation can't be confirmed, nothing runs. The
  run blocks and can resume later. It never quietly downgrades to running
  code on my machine."

### Least privilege

- **What it is.** Give every component only the permissions its job needs.
- **Why it matters.** It limits the damage when something goes wrong.
- **How Agent Factory demonstrates it.**
  - Each agent has a tool allowlist; the reviewer can't write files.
  - Anything unlisted is denied.
  - Sandboxed code runs non-root, with all capabilities dropped and no
    network.
  - Tests: `tests/test_safety.py::test_default_policy_is_conservative` and
    `test_gateway_denies_unknown_and_unlisted_tools_and_logs_them`.
- **In an interview.** "Permissions are default-deny and per role. The
  reviewer agent literally cannot write a file, because that tool isn't on
  its list."

### Resource limits

- **What it is.** Caps on time, CPU, memory, disk, processes, and output.
- **Why it matters.** A runaway or hostile program shouldn't be able to
  freeze or fill the machine.
- **How Agent Factory demonstrates it.**
  - Every sandboxed execution is capped.
  - The adversarial suite proved each cap live:
    - memory: killed at the limit;
    - disk: writes stopped at exactly 16 MiB;
    - processes: limited to the configured number;
    - output: truncated at 64 KiB;
    - CPU: throttled to 0.5 CPU, and stopped at a 2-second CPU budget.
- **In an interview.** "Each limit was proven with a program designed to
  exceed it, and the host stayed responsive throughout. We measured that
  too."

---

## Version control and releases

### Git branches

- **What it is.** Independent lines of development within one repository.
- **Why it matters.** You can work on something new without disturbing what
  is stable.
- **How Agent Factory demonstrates it.**
  - `master` holds the v0.1 baseline.
  - `v0.2-sandbox` holds the sandbox work.
  - `zoomcamp/alignment` holds the Zoomcamp documentation work, branched
    from the frozen v0.2.0 release.
  - `feature/product-data-foundation` holds the product domain model,
    branched from `zoomcamp/alignment`. It is unreleased and not merged.
  - `feature/postgres-persistence` holds the PostgreSQL storage, branched
    from `feature/product-data-foundation`. It is also unreleased and not
    merged.
  - `feature/openapi-contract` holds the API contract and `AGENTS.md`,
    branched from `feature/postgres-persistence`. Also unreleased.
- **In an interview.** "Each phase got its own branch. The release branches
  stay untouched while new work happens elsewhere."

### Commits

- **What it is.** Saved snapshots of changes, each with a message explaining
  what changed and why.
- **Why it matters.** A good history explains decisions and makes problems
  easy to trace and undo.
- **How Agent Factory demonstrates it.**
  - One commit per stage, with detailed messages that record defects found
    and fixed.
  - The first 13 commits, through the Zoomcamp alignment commit (`203dcda`),
    carry a `Co-Authored-By` trailer naming the AI agent.
  - From the product-data-foundation commit onward, the owner instructed
    that commits use the owner's Git identity with no automatic AI trailer.
    AI assistance is disclosed in [`docs/ai-workflow.md`](../ai-workflow.md)
    instead.
  - From the postgres-persistence commit onward, commit messages also carry
    no AI-assistance line, at the owner's instruction. The documentation is
    the disclosure.
- **In an interview.** "The history reads like a lab notebook: each stage,
  its verification, and any defect found. AI assistance is disclosed:
  early commits carry co-author trailers, and the workflow document
  explains the arrangement for every commit."

### Annotated release tags

- **What it is.** A named, permanent marker on a specific commit. An
  *annotated* tag also stores a message, an author, and a date.
- **Why it matters.** "v0.2.0" always means exactly one tested state of the
  code.
- **How Agent Factory demonstrates it.**
  - `v0.1.0` and `v0.2.0` are annotated tags whose messages summarize what
    was verified.
  - `v0.2.0` was moved once, before it was ever published, to include a
    documentation fix. Published tags should never be moved.
- **In an interview.** "Releases are annotated tags with the verification
  results in the message. We only adjusted a tag before it was ever shared."

### Semantic versioning

- **What it is.** Version numbers as MAJOR.MINOR.PATCH:
  - MAJOR for breaking changes;
  - MINOR for new compatible features;
  - PATCH for fixes.

  Versions starting with 0 mean "still in early development".
- **Why it matters.** Users can tell from the number alone how risky an
  upgrade is.
- **How Agent Factory demonstrates it.** v0.1.0 was the first engine; v0.2.0
  added the sandbox, a new capability. The next number will be chosen when
  the next capability is real (see [ROADMAP.md](ROADMAP.md)).
- **In an interview.** "We're in 0.x, so the API may still change. Minor
  versions mark capabilities; v0.2.0 was the sandbox."

### Reproducibility

- **What it is.** Someone else, or you later, can rebuild and rerun the
  system and get the same results.
- **Why it matters.** Unreproducible results can't be verified or trusted.
- **How Agent Factory demonstrates it.**
  - The sandbox base image is pinned by digest, and the digest was verified
    independently against Docker Hub.
  - The built image is recorded in a lock file, and a stale lock blocks
    execution.
  - The scripted provider makes whole runs deterministic, and the fuzz test
    uses a fixed random seed.
  - **Limitation:** the image lock is machine-specific.
- **In an interview.** "The sandbox image is pinned to a verified digest and
  locked. If its sources change without a rebuild, the system refuses to run
  untrusted code."

### Release engineering

- **What it is.** The process of turning a working branch into a trustworthy
  release.
- **Why it matters.** "It works on my branch" isn't a release. A release is
  a verified, recorded, frozen state.
- **How Agent Factory demonstrates it.** The v0.2.0 checklist:
  - run every suite;
  - run the sandbox self-check;
  - confirm zero leftover containers;
  - verify provenance;
  - make the claims in the documentation match the evidence;
  - confirm a clean tree and that `master` and `v0.1.0` are unchanged;
  - create the annotated tag;
  - freeze it.
- **In an interview.** "The release was gated on evidence: all suites, a
  sandbox check, cleanup verification, and an independent provenance check.
  After that it was frozen."

---

## AI-assisted development: how Agent Factory was built

This section records the workflow factually. The fuller write-up is
[`docs/ai-workflow.md`](../ai-workflow.md). It still lacks the full prompts
and a context file; see
[RUBRIC_MAPPING.md](RUBRIC_MAPPING.md#criterion-2--ai-assisted-development-workflow-max-2).

**Who did what.**

- **The project owner**
  - wrote the product vision and the staged instructions for every phase:
    scope, constraints, explicit "do not" lists, acceptance criteria, and
    mandatory stop points;
  - reviewed each stage's report and approved or amended it;
  - personally authorized sensitive operations:
    - building the sandbox image;
    - network access for the Zoomcamp research;
    - pulling the official PostgreSQL image;
    - installing the database driver into a project virtual environment;
  - independently verified the base image's digest against Docker Hub.
- **An AI coding agent** (Claude Code; the early commits' trailers name
  "Claude Opus 5.5")
  - proposed designs for approval;
  - wrote the code, tests, and documentation;
  - ran the test suites;
  - reported results, including failures and defects it found, at each stop
    point.

**How the AI's output was controlled.**

- **Staged work with stop points.** No phase started without approval of
  the previous one.
- **Verification.** Every claim was backed by tests: unit, integration, live
  Docker, adversarial, and manual mutation checks.
- **Conservative scoping.** Documentation was required to claim only what
  was demonstrated. Several problems were caught and corrected along the
  way:
  - an incorrectly measured piece of test evidence;
  - a vacuous test assertion;
  - claims later found to be stale or too strong.
- **Disclosure.**
  - The first 13 commits carry an AI co-author trailer.
  - Later commits, from product-data-foundation on, carry none, at the
    owner's instruction.
  - [`docs/ai-workflow.md`](../ai-workflow.md) states that the arrangement
    applies to every commit. A commit without a trailer does not mean the
    owner hand-wrote it.

**Context file.** [`AGENTS.md`](../../AGENTS.md) (branch
`feature/openapi-contract`) is the standing instruction file for coding
agents. It holds the architecture boundaries, test commands, frozen refs,
security invariants, and prohibited shortcuts that earlier assignments
repeated by hand.

**Not yet in the repository:** the full original prompts and instructions.
`docs/ai-workflow.md` summarizes their structure and quotes representative
constraints; publishing them in full is the owner's decision.

> **Why this matters.** AI-assisted engineering is credible when the human
> part is real: clear specifications, deliberate approval points, and
> independent verification. The interesting skill on display is *directing
> and verifying* an AI agent, not typing the code.
