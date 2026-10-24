# Agent Factory — product specification

| | |
| --- | --- |
| Status | Draft for review. Written on `zoomcamp/alignment`; statuses updated on `feature/product-data-foundation`, `feature/postgres-persistence`, `feature/backend-api`, and `feature/auth-worker` (all unreleased) |
| Baseline release | `v0.2.0` (`bca41376`) |
| Scope | What the product must do, independent of technology choices |

Each requirement has a stable ID and a **v0.2.0 status**:

| Status | Meaning |
| --- | --- |
| Implemented | Exists in v0.2.0 and is covered by tests |
| Partial | Some of it exists |
| Planned | Does not exist yet |

Statuses describe the repository, not intentions. Evidence for
"Implemented" items is listed in [RUBRIC_MAPPING.md](RUBRIC_MAPPING.md) and
the project docs.

Where the unreleased `feature/product-data-foundation` branch changes
something, the cell adds a **Branch:** note. Changes from the later
`feature/postgres-persistence` branch are marked **Postgres branch:**. The
v0.2.0 status stays as it was.

> **Why this document exists.** A specification says *what* the product
> must do and *how we will know* it does it, before anyone decides *how* to
> build it. That keeps implementation choices (frameworks, cloud providers)
> replaceable, and gives every future change a requirement to point back to.

## 1. Problem statement

Handing software work to AI coding tools today usually means one of two
things:

- **Sitting with the tool** and approving every step, which saves little
  time.
- **Letting it run unattended**, which risks unbounded loops, unsafe
  commands, leaked credentials, and actions nobody approved.

Developers need a middle ground. They should be able to delegate a whole
development task: planning, building, testing, repairing, and reviewing.
The work should proceed **autonomously within explicit bounds**, and only
call the developer back when a decision genuinely needs human judgment or
authorization.

## 2. Target user

The primary user is an **individual developer or small-team lead**. They
want to delegate well-specified development work to AI agents, often for
hours or overnight, and still keep control over anything risky or
irreversible. They review outcomes, not every step.

Secondary user: an **engineer debugging a run**, who needs the full
detailed record of what happened.

## 3. Product vision

The user defines a project and hands it to Agent Factory. The agents plan,
build, and test it, and when tests fail they repair and retest. They then
review the result.

Routine engineering decisions proceed automatically within configured
permissions. When human judgment or authorization is needed, Agent Factory:
1. pauses safely;
2. notifies the user on their phone;
3. waits for an authenticated decision;
4. resumes according to that decision.

The user can leave Agent Factory working overnight. In the morning they can
see a readable summary of what was accomplished, without reading raw logs.

```
USER DEFINES PROJECT -> PLAN -> BUILD -> TEST --fail--> REPAIR -> TEST ...
                                          \--pass--> REVIEW
  at any point: JUDGMENT NEEDED? -- no --> continue
                                 \- yes -> pause -> notify -> mobile approval
                                           -> approve: resume | reject: follow policy
```

## 4. Primary user story

> **As a** developer with a well-defined software task,
> **I want to** hand the task to Agent Factory and walk away,
> **so that** the work gets planned, built, tested, repaired, and reviewed
> without me approving routine steps, **while** I am still asked, securely
> and from my phone, before anything risky or irreversible happens.

## 5. Functional requirements

### Project definition and the autonomous loop

| ID | Requirement | v0.2.0 status |
| --- | --- | --- |
| FR-001 | The user can define a project: goal, requirements, acceptance tests or criteria, starting files, files the agents must not change, and resource limits. | Partial: through code-level project templates (`ProjectTemplate`, `TaskSpec`); no user-facing way to create one |
| FR-002 | The user can start a run for a project, after which it proceeds without further input until it finishes or needs judgment. | Implemented (CLI; deterministic provider) |
| FR-003 | The system produces a structured plan: steps, files, and acceptance criteria. | Implemented with the scripted provider |
| FR-004 | The system implements the plan by writing project files inside the project workspace. | Implemented with the scripted provider |
| FR-005 | The system runs the project's tests and records a structured test report. | Implemented |
| FR-006 | When tests fail, the system sends structured failure details back to implementation and retries. | Implemented |
| FR-007 | The system reviews the implementation against the task and plan, and either approves it or requests specific changes. | Implemented with the scripted provider |
| FR-008 | Every repair loop has a configurable budget. When a budget is exhausted the run stops with a recorded reason, never looping indefinitely. | Implemented |
| FR-009 | At least one real AI model provider can drive planning, implementation, and review, within a cost budget. | Partial. **Real model v1, foundation (branch):** a Model Gateway with budgets, a usage ledger, context limits, a secret guard, versioned prompts, strict schemas, and an Anthropic adapter tested offline only. **No real model has driven a run yet**: the first live call awaits the owner's authorization ([model-providers.md](../model-providers.md)). |
| FR-010 | A run can be interrupted at any point and resumed later without repeating completed steps. | Implemented |
| FR-011 | Each project lives in its own workspace with version-control snapshots of the agents' changes. | Implemented |
| FR-012 | New kinds of projects plug in through templates without changing the core workflow. | Implemented. **Branch:** templates also define the project's own milestone types |

### Human-in-the-loop and approvals

| ID | Requirement | v0.2.0 status |
| --- | --- | --- |
| FR-013 | Routine actions within configured permissions proceed without asking the user. Anything not explicitly permitted is denied. | Implemented |
| FR-014 | Actions in protected categories require explicit human approval before they happen. At minimum the categories are spending money, production deployment, credential changes, destructive deletion, and external communication. | Implemented (policy covers all five; only destructive deletion currently has a tool) |
| FR-015 | Approval is **exact-action**: a decision applies only to the specific requested action and its parameters. It never grants permission for other or future actions. | Implemented |
| FR-016 | When approval is needed, the run pauses, saves its progress, and waits without losing work. | Implemented |
| FR-017 | After a decision, the run resumes. Approved actions proceed; rejected actions follow the configured policy for that action, for example skipping it. | Implemented (CLI). **Auth branch (E5):** a decision recorded over the API wakes the run, and a worker applies it after re-checking the exact action, its expiry, and the decider; a rejection is a real outcome the graph follows, never a request asked again. Tested end to end, from an authenticated HTTP decision to the action's single effect. |
| FR-018 | Every decision is recorded durably: the request, the decision, who decided, when, and an optional note. | Partial: recorded and tamper-evident, but "who" is free text because there is no authentication. **Branch:** adds a structured decision record, bound to the exact action, with an actor placeholder ready for authentication. It is held in memory only, not yet durably. **Postgres branch:** with `--database`, decisions are stored durably in PostgreSQL, and the database itself enforces one decision per request and exact-action binding. "Who" is still unauthenticated. **Backend branch:** decisions can also be recorded over the local HTTP API (bound to the exact action's fingerprint, time and actor set by the server); the actor is still an unverified placeholder. **Auth branch (E3):** decisions recorded over the API name the authenticated principal (verified, bound in the database to an enrolled identity); CLI decisions remain unverified, so this stays Partial. **E5:** answers about actions with an unknown outcome are recorded the same way (`actions:reconcile`), and every applied decision also names the worker generation that applied it. |
| FR-019 | The authorized user is notified when a run needs judgment, through a channel that reaches a mobile phone (SMS or equivalent). | Partial. **Notifications v1, phase 1 (branch):** a durable outbox written with each approval or reconciliation event, a dispatcher, and a fake provider, tested with real processes and outages. **No real channel yet:** SMS or email waits for the owner's authorization ([notifications.md](../notifications.md)). |
| FR-020 | The notification contains a secure link to the approval interface and never contains secrets. Replying to it cannot approve anything. | Partial. **Phase 1:** the link is a plain dashboard path that leads through sign-in to the approval (tested in Chrome); messages carry no secrets or action details (tested); nothing reads replies. "Secure" in production needs the HTTPS deployment and real sign-in. |
| FR-021 | The approval interface requires authentication before it shows any details or accepts a decision. | Partial. **Auth branch (E3):** the API requires an authenticated identity with `approvals:read` before it shows an approval, and `approvals:decide` before it accepts a decision. There is no interface yet, and no production identity provider is configured. |
| FR-022 | The approval interface shows the exact proposed action, project, requesting agent, reason, and relevant risk and context. | Partial: the system records action, parameters, category, agent, node, and reason; there is no risk summary and no interface. **Branch:** the approval request record also carries the project and a plain-language risk statement. There is still no interface. **Backend branch:** the local API's approval view returns exactly these fields; there is still no screen. **E5:** the view also shows when the approval expires, what happened to the action afterwards (an execution timeline built from structured records), and whether a person must say if an uncertain action happened. Still no screen. |
| FR-023 | The authorized user can approve or reject from a phone. | Planned |
| FR-024 | The user is also told when a run needs attention for non-approval reasons: blocked (isolation unavailable), failed, exhausted, or completed. | Planned (states exist; notifications do not). **E5:** an attention inbox in the API lists what needs a person (decisions, and actions whose outcome is unknown); blocked, failed, and finished runs are not in it yet, and there are no notifications. |

### Progress, milestones and logs

| ID | Requirement | v0.2.0 status |
| --- | --- | --- |
| FR-025 | The system records human-readable **milestones**: plan completed, implementation milestone completed, tests executed, tests failed, repair completed, tests passing, review completed, approval required, approval received, blocked, and project completed. | Planned (only low-level log events exist). **Branch: Partial.** The engine records milestones at workflow boundaries, defined per project, covering every item listed. They are held in memory only and not shown to users. **Postgres branch:** with `--database`, they are durable in PostgreSQL across restarts. They are still not shown to users. |
| FR-026 | The user can request a **summary for a time period**, such as overnight, listing milestones, current status, and anything waiting on them. | Planned. **Branch:** the records and time-range queries a summary needs exist and are tested; summary generation is deliberately not built |
| FR-027 | The system keeps a detailed, append-only, tamper-evident **execution log** of every step, action, result, and transition. | Implemented |
| FR-028 | The user can see each run's current status, position in the workflow, and any waiting or blocking reason. | Partial (CLI `status` per run). **Branch:** run records carry status and a status reason. **Backend branch:** the local API returns each run's status, a curated reason, and the approval it waits for; no UI yet |
| FR-029 | A user interface exposes both milestones and logs, with milestones as the default view. | Planned |
| FR-030 | The user can see all of their projects and runs in one place. | Planned (CLI works per workspace). **Branch:** repository interfaces list projects and their runs; in memory only, no interface. **Postgres branch:** projects and runs are durable and listable across projects in PostgreSQL; there is still no interface. **Backend branch:** the local API lists all projects with their latest run and pending approvals; there is still no UI |

### Safe execution of generated code

| ID | Requirement | v0.2.0 status |
| --- | --- | --- |
| FR-031 | Code produced by an untrusted source (any real model) runs only in an isolated environment of container strength or stronger. | Implemented |
| FR-032 | If that isolation is unavailable, unhealthy, or misconfigured, the code is not run. The run enters a resumable **blocked** state without consuming repair attempts. | Implemented |
| FR-033 | Agents can only read and write inside their project workspace, and cannot modify protected files such as acceptance tests. | Implemented |

## 6. Non-functional requirements

| ID | Requirement | v0.2.0 status |
| --- | --- | --- |
| NFR-001 | **Fail closed.** Any uncertainty about isolation or permission results in not acting, never in a weaker fallback. | Implemented |
| NFR-002 | **Least privilege.** Each agent has only the tools its role needs, and dangerous categories can never be auto-allowed. | Implemented |
| NFR-003 | **Bounded execution.** Untrusted execution is limited in time, CPU, memory, disk, processes, and output, and cannot reach the network. | Implemented |
| NFR-004 | **Durability.** An interruption at any point loses at most the in-progress step. | Implemented |
| NFR-005 | **Auditability.** Logs are tamper-evident and every approval decision is attributable. | Partial (attribution needs authentication). **Auth branch (E3, E5):** API decisions and reconciliation answers name the authenticated principal, and each applied decision and each attempt names the worker generation; CLI decisions remain unverified local actors, so this stays Partial. |
| NFR-006 | **Testability without paid services.** The whole workflow can be exercised deterministically, offline, without credentials. | Implemented |
| NFR-007 | **Provider independence.** Adding or changing a model provider requires no change to workflow logic. | Implemented |
| NFR-008 | **Approval security.** Approvals require authentication, are bound to one request, can be decided once, and cannot be performed by replying to a message. | Partial (exact-action binding exists; authentication and links are planned). **Branch:** the domain also enforces one decision per request and refuses conflicting decisions. **Auth branch (E3):** API approvals require authentication and the `approvals:decide` permission; there are no notification links yet, and no production identity provider. **Notifications v1 (phase 1):** a notification can never decide anything; its link authorizes nothing and leads through sign-in |
| NFR-009 | **Secret hygiene.** No credentials in the repository. Untrusted code never sees host environment secrets. Future provider keys come from an explicit, user-controlled source. | Partial. **Postgres branch:** the database URL comes from explicit configuration (a git-ignored file), is never printed, and is shown, by a live test, never to reach untrusted code |
| NFR-010 | **Cost control.** Model and notification spending is bounded per run, and spending money requires approval. | Partial. Model calls: per-call, per-visit, and per-run token and call budgets, checked before every call and kept in a restart-safe ledger; an optional dollar ceiling when prices are configured. Not yet exercised against a paid provider. |
| NFR-011 | **Comprehensibility.** A non-expert can understand run progress from milestones without reading logs. | Planned |
| NFR-012 | **Mobile usability.** Approval review and decision are fully usable on a phone screen. | Planned |
| NFR-013 | **Timeliness.** Notifications are sent promptly after a pause; a target is to be set when notifications are designed. | Partial. The intent is durable with the pause; a dispatcher claims it within its poll interval (2 s by default) and retries with backoff. No target is set until a real provider exists. |
| NFR-014 | **Reproducibility.** Setup, run, test, and sandbox-image preparation are documented and pinned. | Partial (no deployment instructions; image lock is machine-specific) |
| NFR-015 | **Maintainability.** Concerns (workflow, agents, tools, safety, state, providers, sandbox) stay separated behind interfaces. | Implemented |
| NFR-016 | **Transparency.** Documentation states honestly what is guaranteed, what is not, and how AI tools were used to build the project. | Partial. **Branch:** [`docs/ai-workflow.md`](../ai-workflow.md) documents how AI tools were used |
| NFR-017 | **Portability.** The system runs on the developer's platform of choice. | Partial (verified on Windows 11 with Docker Desktop only) |

## 7. Human-in-the-loop model

Agent Factory distinguishes three kinds of decisions:

| Kind | Example | Who decides | Current support |
| --- | --- | --- | --- |
| **Routine** | Write a file in the workspace; run the tests; retry after a test failure | The system, within permissions (FR-013) | Implemented |
| **Authorization** | Delete a file; spend money; deploy; change credentials; contact someone | The authorized human, per exact action (FR-014, FR-015) | Implemented in the engine (CLI); mobile flow planned |
| **Attention** | Isolation unavailable (blocked); budget exhausted; run failed | A human must fix something or decide what next (FR-024) | States implemented; notifications planned |

Principles:
- **Exact-action approval.** Approving action A never approves action B, a
  repeat of A with different parameters, or anything in the future (FR-015).
- **Rejection is not failure.** A rejected action follows the policy for that
  action, for example skipping it, rather than crashing the run (FR-017).
- **Pausing is safe.** The run is checkpointed before waiting, so a pause can
  last minutes or days (FR-016).

> **Why this matters.** Autonomy is only useful if it is trustworthy. Asking
> about everything wastes the user's time, and asking about nothing
> eventually does something unwanted. Clear categories make "when will it
> interrupt me?" predictable.

## 8. Mobile approval requirement

When Agent Factory needs human judgment:

| Step | Requirement | IDs |
| --- | --- | --- |
| 1 | Pause and checkpoint the workflow | FR-016 |
| 2 | Notify the authorized user | FR-019 |
| 3 | Deliver the notification by SMS or an equivalent mobile mechanism | FR-019 |
| 4 | Include a secure link to the approval interface | FR-020 |
| 5 | Require authentication | FR-021, NFR-008 |
| 6 | Show the exact proposed action | FR-022 |
| 7 | Show project, requesting agent, reason, and risk/context | FR-022 |
| 8 | Allow APPROVE or REJECT from a phone | FR-023, NFR-012 |
| 9 | Record the decision | FR-018 |
| 10 | Resume the graph according to the decision | FR-017 |

Explicitly excluded: approving by replying "YES" to a text message, or any
other unauthenticated decision channel. A message can be read by anyone
holding the phone, forwarded, or spoofed.

> **Why this matters.** The approval interface is the most sensitive surface
> in the product: it is how risky actions become allowed. It must be at
> least as well protected as what it authorizes.

## 9. Milestone and overnight reporting requirement

Agent Factory distinguishes two records:

| Record | Question it answers | Audience | IDs |
| --- | --- | --- | --- |
| **Milestones** | "What did Agent Factory accomplish?" | The user, every day | FR-025, FR-026, NFR-011 |
| **Execution log** | "What exactly happened internally?" | Engineers debugging or auditing | FR-027 |

The v0.2.0 execution log already contains the raw facts that milestones can
be derived from: node results, transitions, approvals, and blocked/completed
events.

**Branch update.** The milestone model is now defined in
[`docs/product-domain.md`](../product-domain.md). Two design points:

- Milestones are derived from the engine's committed checkpoints, not from
  log events.
- The log itself is unchanged.

The overnight summary (FR-026) answers, for a chosen period:
- what was completed;
- what failed and was repaired;
- what is waiting on the user;
- whether anything is blocked.

## 10. Safety requirements

Safety is specified through FR-013–FR-018 and FR-031–FR-033, and through
NFR-001–NFR-003, NFR-008, and NFR-009. The v0.2.0 execution sandbox's exact
guarantees and limitations are stated in [../sandbox.md](../sandbox.md). This
specification does not restate or extend them.

## 11. Initial user journeys

| Journey | Steps | v0.2.0 support |
| --- | --- | --- |
| **J1 Overnight hand-off** | Define project → start run → leave → agents plan/build/test/repair/review → morning summary | Loop: implemented with the scripted provider. Summary: planned |
| **J2 Approval on the go** | Run reaches a protected action → pauses → user notified on phone → logs in → sees exact action → approves → run resumes | Pause/approve/resume: implemented via CLI, and via the local API with a worker resuming the run (E5). Phone, notification, and a production login: planned |
| **J3 Rejection** | As J2, but the user rejects → action skipped per policy → run continues | Implemented via CLI |
| **J4 Blocked environment** | Isolation unavailable → run blocks without consuming retries → user told → fixes environment → resumes | Block/resume: implemented. Notification: planned |
| **J5 Investigation** | Engineer opens a run's detailed log to understand a failure | Implemented via CLI (`log`, hash-chain verification) |

## 12. Acceptance criteria

Status: **Verified** means verified in v0.2.0; **Not yet** means not yet
verifiable.

| ID | Criterion | Requirements | Status |
| --- | --- | --- | --- |
| AC-001 | With the deterministic provider, a run completes plan → build → test → repair → test → review → done with no human input. | FR-002–FR-007 | Verified |
| AC-002 | A failing test sends the exact failing test names back to implementation, and the repaired code passes. | FR-005, FR-006 | Verified |
| AC-003 | A run whose repairs never succeed stops as `exhausted` at its configured budget. | FR-008 | Verified |
| AC-004 | Interrupting a run and resuming it with a fresh executor, from the on-disk checkpoint alone, completes it without repeating completed steps. | FR-010, NFR-004 | Verified |
| AC-005 | A protected action pauses the run before it executes. The action runs only after approval, and approving it does not approve any different action. | FR-014–FR-017 | Verified |
| AC-006 | Rejecting a protected action skips it according to policy, and the run continues. | FR-017 | Verified |
| AC-007 | Untrusted code never runs outside a container-strength sandbox. When the sandbox is unavailable the run is `blocked`, no local fallback happens, and no repair attempt is consumed. | FR-031, FR-032, NFR-001 | Verified |
| AC-008 | The adversarial suite's hostile programs, run in the sandbox, fail to read or modify a host canary, reach the network, see host secrets, or exceed resource limits, and cleanup is verified. | FR-031, NFR-003 | Verified on Docker Desktop (Windows) |
| AC-009 | Any modification of a run's execution log is detected. | FR-027, NFR-005 | Verified |
| AC-010 | When a run pauses for judgment, the authorized user receives a mobile-deliverable notification with a secure link. Replying to the notification cannot decide anything. | FR-019, FR-020 | Not yet: proven end to end with the fake provider only (phase 1) |
| AC-011 | Opening the link without authenticating reveals no action details and cannot decide. | FR-021, NFR-008 | Not yet |
| AC-012 | On a phone, the authenticated user sees the exact action, project, agent, reason, and risk. Approving or rejecting is recorded with their identity and time, and the run resumes accordingly. | FR-018, FR-022, FR-023 | Not yet |
| AC-013 | A decision cannot be replayed to approve a different, repeated, or later action. | FR-015, NFR-008 | Partial (engine binding verified; links not yet built). **Branch:** domain binding verified too |
| AC-014 | After a run, the user sees its milestones in plain language without opening the log. | FR-025, FR-029, NFR-011 | Not yet |
| AC-015 | For a chosen period, the user gets a summary of milestones, current state, and anything awaiting them. | FR-026 | Not yet |
| AC-016 | A real model provider completes the demo project within its cost budget, with all code running sandboxed. | FR-009, NFR-010 | Not yet: the live acceptance test exists (`tests/test_model_live.py`, hard budget) and has not been run |
| AC-017 | The user is notified when a run becomes blocked, exhausted, failed, or completed. | FR-024 | Not yet |

## 13. Non-goals

- **Agents do not deploy to production, spend money, change credentials,
  delete data, or contact anyone without exact-action approval.**
- **Agent Factory does not replace human code review** for work that will
  ship. Generated test results can be faked by generated code; see Known
  limitations.
- **No unauthenticated approval channels**, including SMS replies.
- **It is not a general chat assistant**, and not a model-training or RAG
  product.
- **It is not a multi-tenant hosted service** in the initial versions.
- **Isolation is not an absolute guarantee.** Container or kernel escape is
  outside the security guarantee; see [../sandbox.md](../sandbox.md).

## 14. Known limitations (as of v0.2.0)

- Only a deterministic scripted provider exists. No real model has driven
  the agents yet.
- There is no user interface, no notifications, no authentication, and no
  remote access. The CLI is the only way in.
- There are no milestones or summaries. Only the low-level execution log
  exists. **Branch:** milestone records exist, but they are held in memory
  only and are not shown anywhere. There are still no summaries.
- State is stored as per-run files on the local disk, not in a database.
  **Postgres branch:** product records can be stored in PostgreSQL with
  `--database`. Engine state stays in per-run files.
- Generated code can fake its own test results (test-verdict forgery).
- The sandbox limitations in [../sandbox.md](../sandbox.md) apply:
  - container escape is out of scope;
  - only Docker Desktop on Windows has been tested live;
  - backend health is not persisted across restarts;
  - the image lock is machine-specific.
- Approvals carry no verified identity.

## 15. Future capabilities

- Real model providers with per-run cost budgets.
- Overnight summaries. **Branch:** the milestone model now exists. A
  generic event model was considered and deferred; see
  [`docs/product-domain.md`](../product-domain.md#why-there-is-no-generic-event-entity).
- An API with a published contract; persistent database storage;
  authentication.
- A dashboard, with a mobile-first approval interface.
- A notification service (SMS or equivalent).
- Multiple projects and runs; history.
- Continuous integration and delivery, and a public deployment.
- Stronger isolation backends (for example, virtual-machine level) and
  persisted backend health.
- Defenses against test-verdict forgery, for example an independent test
  runner or protected verification.

[ROADMAP.md](ROADMAP.md) orders these by dependency.
