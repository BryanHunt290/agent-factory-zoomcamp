# How Agent Factory is built: the AI-assisted workflow

Agent Factory is built by **an AI coding agent working under the direction
of the project owner**. This page explains the arrangement plainly, so that
readers can judge the work accordingly:

- who did what;
- how the work is assigned;
- how the AI's output is checked;
- how AI involvement is disclosed.

## Who does what

For the offline supervised coding milestone, Codex assisted with the bounded
implementation, tests, source review and documentation under explicit owner
instructions. The [demonstration](offline-supervised-demo.md) automates only
known fixture work through the existing executor; it does not supervise a real
Codex session or replace owner decisions. Fresh validation passed all 91 focused
tests, completed the standalone repair, and killed an in-memory evidence-check
mutation with a passing restored baseline. An additional observed run confirmed
failure feedback reached the second fake-worker request. The
[validation record](offline-supervised-validation.md) records regression results,
approved filesystem access and the earlier blocked attempts. Application and test
source stayed unchanged during validation.
The earlier workflow and attribution history below are preserved.

For the offline Upwork opportunity-review and host-receipt work, Codex reviewed
the local contracts and templates, corrected Unicode round trips and detail-event
ordering, and added regression coverage for resumed receipt and policy checks.
The [review record](upwork-review-validation.md) separates fresh test evidence
from the earlier U1/U2A reports. This work does not connect to Upwork, send a
message, submit an application, spend Connects or run a real model.

For the [persistent-learning-memory design](design/persistent-learning-memory.md),
Codex reviewed the existing foundations, clarified planned client authorization,
evidence capture and revocation/recovery requirements, and kept historical approval
and cost statements separate from current evidence. The
[design review record](persistent-learning-memory-review.md) documents validation.
This milestone changes documentation only; it implements no memory store or
retrieval path and makes no cloud or model calls.

| Role | Who | Responsibilities |
| --- | --- | --- |
| Project owner | The repository owner (Git identity `BryanHunt290`) | Product vision and requirements. A written, bounded assignment for every phase. Review of every report. Approval, amendment, or rejection. Authorization of sensitive operations. Independent checks. |
| AI coding agent | Claude Code (Anthropic), running in VS Code on the owner's Windows machine. Earlier commit trailers name the model "Claude Opus 5.5". | Inspecting the repository, proposing designs when asked, and writing the code, tests, and documentation. Running the test suites, then reporting results, including failures, defects found, and open questions. |

The AI agent writes most of the code, tests, and documentation, and this
page doesn't suggest otherwise. The owner's contribution is the
specification, the decisions, and the review and authorization of every
stage.

## The loop

Each phase of the project follows the same cycle:

```
 1. Human product requirement     the owner writes a bounded assignment
          |
 2. AI-assisted implementation    the agent builds within the stated constraints
          |
 3. Automated tests               full non-live suite; live suites when execution code changes
          |
 4. Implementation report         what was built, results, defects, gaps, git state
          |
 5. Human review / authorization  approve, amend, or stop; authorize sensitive steps
          |
 6. Next bounded assignment ------> back to 1
```

### 1. Human product requirement

Every assignment is written by the owner, and has the same parts:

- **Goal and scope.** For example: "Create a generic product-domain model
  for projects, runs, milestones/events, and approval requests/decisions."
- **Explicit "do not" constraints.** From the current assignment, verbatim:
  - "Do not add a database yet."
  - "Do NOT implement authentication yet."
  - "Do NOT implement overnight-summary generation yet."
  - "Do not turn every internal log entry into a milestone."
  - "Do not add Upwork-specific logic to Agent Factory."
- **Standing constraints**, repeated across phases:
  - no pushing to any remote;
  - no credentials;
  - no network access unless explicitly authorized;
  - no changes to other projects;
  - no Docker image pulls or builds by the agent;
  - released tags and branches stay frozen.
- **A definition of done** and the exact list of items the report must
  contain.
- **Stop points.** The agent stops and reports at the end of each
  assignment. It never continues into the next phase on its own.

### 2. AI-assisted implementation

The agent works inside those limits. When an assignment asks for a design
first, the agent produces only the design, and code follows only after the
owner approves it. The v0.2 sandbox design, for example, was
[approved with amendments](design/v0.2-sandbox.md) before any code was
written.

### 3. Automated tests

Verification has several layers:

- **The full non-live suite** must pass at every stage. It needs no Docker,
  network, or credentials, and a guard fails any test that tries to use
  Docker or the network.
- **Live Docker suites** (`docker_live`, `docker_adversarial`) are run when
  execution or sandbox code changes. They are opt-in, and fail rather than
  skip when Docker is missing ([testing.md](testing.md)).
- **Manual mutation checks** cover safety-critical logic. The agent breaks a
  check on purpose and confirms that the suite fails, then restores the
  code. There is no automated mutation-testing tool in the repository.
  - Product-domain milestone: 13 deliberate mutations were made, across the
    recorder, the approval service, the state machines, schema validation,
    and the executor hook. Examples include skipping the fingerprint check,
    accepting a conflicting second decision, ignoring expiry, reopening a
    failed milestone, accepting unknown fields or newer schema versions, and
    swallowing recording failures. The final suite catches all 13.
  - One of them was caught only after a missing test was added. Nothing had
    directly tested that a decision is bound to the action the engine will
    execute; `test_decision_binds_to_the_action_the_engine_will_execute`
    now does.
  - PostgreSQL persistence milestone: 17 deliberate mutations, covering:
    - the sync transaction, reconciliation, and the decision race;
    - identifier collation and the binding foreign key;
    - in-memory rollback and references;
    - the sandbox credential filters;
    - the edited-migration check, the advisory lock, and timestamp
      handling.

    The tests catch 16. The 17th removed the Python fingerprint check from
    the PostgreSQL decision write. The database's own constraint still
    refused the forged decision, which is the intended defence in depth.
  - API contract milestone: 10 deliberate breaks of `openapi.yaml`, all
    caught by the contract tests. The breaks were:
    - a milestone-editing endpoint;
    - a client-supplied decision time;
    - an optional fingerprint;
    - local-time timestamps;
    - a wrong example fingerprint;
    - a non-problem 404;
    - authentication presented as implemented;
    - a filesystem path in a response;
    - a dangling reference;
    - an open request schema.
  - Backend API milestone: 3 deliberate breaks of security-critical API
    logic, all caught by the HTTP tests: removing the cursor's filter
    binding, removing the idempotency body comparison, and opening the
    request models to unknown fields (which would let a client send its
    own actor or decision time).
  - Durable action execution (E1-E2): 13 deliberate breaks, all caught:
    dispatching before the durable record, omitting the audit entry,
    ignoring the compare-and-set version, resending a non-idempotent action,
    re-running a succeeded action, ignoring an outcome already on disk,
    dispatching after expiry, a longer retry window, running an approved
    action without a ledger, accepting an undeclared dangerous tool,
    resuming a database-backed run without its database, executing without
    an approved decision, and ignoring an uncertain action an agent swallowed
    ([testing.md](testing.md#durable-action-execution-tests-unreleased)).
  - One restore was silently incomplete. The retry-window mutant had the
    same length as the original and was restored within the same second, so
    Python kept running its cached bytecode. The final full run caught it:
    four retry-window tests failed although the source was correct. After
    the stale `__pycache__` file was deleted, the check was repeated with
    bytecode writing disabled and a passing baseline before every mutant;
    all 13 were killed again. A stronger replay mutant (really re-running a
    recorded success) was added, and the effect counts catch it.
  - The same final run exposed a defect: a filesystem error while writing
    the checkpoint ledger failed the run (terminal) instead of blocking it,
    contrary to the documented fail-closed rule. It is fixed, with engine,
    ledger, and atomic-write tests and 6 more mutants, all caught. One
    mutant exposed a weak assertion: the engine test alone missed a path in
    the error message, because on Windows the message shows the path with
    doubled backslashes. The assertion now checks markers without
    separators.
  - Workspace git confinement (a corrective commit after E1-E2): 12 mutants.
    Nine are caught by the tests. Three are caught by another layer, by
    design. Two of the killed mutants send a real commit toward the
    bystander repository, and the tests catch both
    ([testing.md](testing.md#workspace-git-confinement-tests-unreleased)).
  - Authentication and authorization (E3): 20 deliberate breaks, all
    caught, among them an authentication bypass, a removed permission
    check, `approvals:read` accepted for deciding, signature verification
    disabled, the issuer check removed, the decision's actor not taken from
    the authenticated principal, development authentication active in
    production, and the `Authorization` header logged
    ([testing.md](testing.md#authentication-and-authorization-tests-unreleased)).
    One mutant first did not run at all: its anchor text appeared twice. The
    agent reported that rather than counting it, targeted the exact line,
    and re-ran it.

### 4. Implementation report

The report follows the owner's list. It records:

- what was built, and the test results;
- design issues discovered, and remaining gaps;
- the git status and commit SHA;
- confirmation that frozen tags and branches are unchanged.

Failures and corrections are reported, not smoothed over. Past reports
recorded, for example:

- a mis-measured piece of test evidence;
- a test assertion that could never fail;
- two real defects found by the adversarial suite;
- documentation claims later found to be stale;
- an incident the agent caused itself, and the agent's first, wrong
  attribution of its effect to the owner (below).

### 5. Human review and authorization

The owner reads the report and either:

- accepts it;
- amends the plan;
- authorizes a specific sensitive operation.

The owner made and authorized these sensitive steps:

- the owner built the sandbox Docker image (image builds are human-initiated
  only);
- the owner independently verified the sandbox base image's digest against
  Docker Hub;
- the owner explicitly authorized network access for the Zoomcamp
  requirements research, and limited it to named actions.
- **PostgreSQL milestone.**
  - The assignment required the agent to stop and ask before any image pull
    or network access. The agent found that no PostgreSQL image and no
    database driver were present locally, stopped, and asked, proposing
    options.
  - The owner authorized exactly three things:
    - one pull of the official `postgres:17` image;
    - installing `psycopg[binary]`, pinned;
    - installing it into a project virtual environment rather than the
      global Python.
  - The agent recorded the image's digest, and the versions installed.
- **API contract milestone.**
  - The contract tests needed a YAML parser, and none was installed. The
    agent stopped and asked, offering three options: the official
    validator, PyYAML only, or JSON-style YAML with no install.
  - The owner authorized PyYAML and `openapi-spec-validator`, installed
    into the project venv and pinned in `requirements-dev.txt`.
- **Backend API milestone.**
  - The agent first checked what was already installed. FastAPI, Pydantic,
    Starlette, Uvicorn, psycopg-pool and HTTPX were present in the project
    venv at the pinned versions in `requirements-api.txt`, so no install
    or network access was needed in the session that finished the
    milestone.
  - Docker Desktop was not running, and the PostgreSQL and live sandbox
    suites need it. Starting it could also have started other projects'
    containers, so the agent stopped and asked. The owner authorized the
    agent to start Docker Desktop. The agent then started only
    `agent-factory-postgres-dev` and left the unrelated unrelated database container
    container stopped.
  - The milestone brief asked for 409 when an `Idempotency-Key` is reused
    with a different body, while the checked-in contract said 422
    `idempotency_key_reused`. The milestone shipped with 422 and flagged
    the difference for review. The owner then decided on **409** ("the new
    request conflicts with the server state already associated with that
    idempotency key"), applied as a separate, narrow correction commit on
    `feature/backend-api`; the code name is unchanged
    ([backend.md](backend.md#durable-project-creation-and-workspace-identity)).
- **Durable action execution (design, then E1-E2).**
  - The owner asked for a design-only analysis first. The agent reproduced
    the defect with an experiment (an approved action run twice: by a node
    with two approvals, and after a crash), wrote
    [design/action-execution.md](design/action-execution.md), and stopped
    for review. The owner approved it with eight decisions (state names, a
    distinct `awaiting_reconciliation` status, the `actions:reconcile`
    permission, a checkpoint ledger without a database, dispatch before
    expiry, a 15-minute retry authorization, tool semantics classes, and the
    future direction).
  - Implementing the owner's run status required an OpenAPI change: the
    contract tests require its run-status enums to equal the engine's. The
    brief said to stop and report before changing the contract, so the
    agent stopped and asked; the owner approved the additive change.
  - No dependency, network access, image pull, or build was needed.
  - **An incident caused by the agent.** While hunting an intermittent test
    failure, the agent ran four pytest sessions at once. They shared
    `.pytest_tmp` inside the repository, which every session wiped when it
    started. One session deleted a test workspace's `.git` mid-test, so that
    workspace's next git commands ran in Agent Factory's own repository.
    They set a repository-local `user.email`, and very likely staged every
    changed file. No commit was made and no ref moved. The agent first
    attributed the staging to the owner's editor, then found the cause and
    corrected that in its report.
  - The permission system blocked the agent from changing the git
    configuration. The owner explicitly authorized removing only that
    override. The owner then required a separate, narrow fix before any
    further work: workspace git confinement that fails closed, and test runs
    that are safe to run concurrently
    ([safety.md](safety.md#process-and-git-isolation),
    [testing.md](testing.md#workspace-git-confinement-tests-unreleased)).
- **Authentication and authorization (E3).**
  - The brief required a design check first, and a stop before any
    substitute for production authentication. The agent evaluated an
    external OpenID Connect provider, locally issued sessions, and a
    homegrown scheme, and found that verifying signed tokens needs a
    cryptography library the project did not have. It stopped and asked
    three questions. The owner authorized one network install of PyJWT with
    `cryptography` (pinned afterwards), chose the external-provider
    architecture, and chose explicit enrollment of principals by the owner
    over just-in-time provisioning.
  - No identity provider was contacted or configured, and no account or
    credential was created. Tests use a TEST ONLY issuer whose keys exist
    only in memory; a development issuer exists for local use and is refused
    in production.
  - The contract changes (implemented `bearerAuth`, `x-permission`, 401,
    403, 429, new error codes) were within the brief, which asked for
    `openapi.yaml` to describe the implemented security model.
- **Approval and reconciliation consumption (E5).**
  - The brief left one design decision to the agent, with a preferred
    direction: how local operator commands avoid racing workers. The agent
    took the preferred direction (database-backed `approve` and
    `reconcile-action` record intent only; `resume`, `demo`, and `db
    reconcile` claim the run first) and documented it before relying on it
    ([workers.md](workers.md#operator-commands)).
  - The agent's own tests found three defects before commit, each fixed
    and then guarded by a mutation check:
    - a decision committed while a worker held the run could be lost to
      that worker's release (found by designing the race tests; fixed with a
      wake-up counter);
    - a run whose reconciliation answer was applied just before a crash
      stayed waiting for ever (found by a real-process crash test);
    - two workers could advance two runs of one project at once in its
      shared workspace. The E4 test that exposed it began failing every
      time; the agent ran the committed E4 code in a temporary worktree
      outside the repository, found it failing one time in three there too
      (a latent race, not a new one), and fixed it with a workspace lock.
  - No dependency, network access, image pull, or build was needed. No
    frontend was built, and nothing contacted an external service: every
    "side effect" is a line in a test oracle file.

### 6. Next bounded assignment

Only after review does the next assignment begin.

## Context file for coding agents

[`AGENTS.md`](../AGENTS.md) at the repository root is the standing context
any coding agent should read before working here. It covers:

- the architecture boundaries;
- the exact test commands, and which suites to run for which change;
- the frozen refs;
- the security invariants;
- the database and sandbox rules;
- the prohibited shortcuts;
- the AI-disclosure policy above.

It turns rules that were previously repeated in each assignment into one
maintained file. Each assignment still adds its own scope and stop point.

## Phases so far

| Phase | Owner's assignment | Agent's output | Verification | Outcome |
| --- | --- | --- | --- | --- |
| v0.1 engine | Graph-based orchestration with checkpoints, an append-only log, approval gates, and a fake provider | Architecture proposal, engine, agents, tools, tests, docs | Full suite and scripted demo | Accepted; tagged `v0.1.0` |
| v0.2 design | Sandbox threat model and design only, no code | Design report | Owner review | Approved with amendments |
| v0.2 stages B–E | Sandbox core, routing, Docker logic, image definition | Code and tests behind fakes; a human-run image build | Tier-0 suite; live smoke tests (16 at `v0.2.0`) | Accepted per stage |
| v0.2 adversarial acceptance | 14 hostile-code categories, never run on the host | Hostile fixtures and a live suite | 44 live adversarial tests; 2 defects found and fixed | Accepted |
| v0.2 release | CPU acceptance, Docker as default for untrusted code, public security claims | Code, docs, release checklist | All suites; owner's digest check | Tagged `v0.2.0` (frozen) |
| Zoomcamp alignment | Requirements research and gap analysis | Specification, rubric mapping, roadmap, learning notes | Sources and retrieval date recorded | Docs only |
| Product data foundation | Domain model, persistence boundary, milestone emission | `agent_factory/domain/`, engine hooks, tests, [product-domain.md](product-domain.md) | Full non-live and live suites; mutation checks | Committed (`41ccff8`); awaiting owner review |
| PostgreSQL persistence | Durable product records in PostgreSQL: schema, migrations, repositories, transactions, reconciliation, security boundary | `agent_factory/persistence/`, migrations, CLI `db` commands and `--database`, tests, [database.md](database.md) | 54 tests against a real PostgreSQL; full non-live, live, and adversarial suites; mutation checks | Committed (`ac5c5e7`); awaiting owner review |
| API contract | Contract-first v1 API for the dashboard, mobile approval, and backend; distributed approval architecture; `AGENTS.md` | [`openapi.yaml`](../openapi.yaml) (OpenAPI 3.1), [api-contract.md](api-contract.md), contract tests, [`AGENTS.md`](../AGENTS.md) | Official OpenAPI 3.1 validation and 35 contract tests; full regression; contract mutation checks | Committed (`4407291`); awaiting owner review |
| Backend API | Local FastAPI backend implementing `openapi.yaml` on PostgreSQL: pooling, durable idempotency, cursor pagination, recorded (not executed) approval decisions, RFC 9457 errors, request IDs, liveness/readiness, loopback only | `agent_factory/api/`, `persistence/pool.py`, migration `0002`, workspace binding, HTTP and conformance tests, [backend.md](backend.md) | HTTP tests against both stores, runtime conformance against `openapi.yaml`, 121 PostgreSQL tests, full non-live, live and adversarial suites, a real uvicorn run on 127.0.0.1, mutation checks | Committed on `feature/backend-api`; awaiting owner review |
| Durable action execution (design, E1-E2) | Approval is not execution: record and protect the execution of every approved action; replay instead of re-run; human reconciliation for uncertain non-idempotent actions | [design/action-execution.md](design/action-execution.md); `domain/executions.py`, `engine/actions.py`, `engine/execution_store.py`, migration `0003`, tool semantics, `reconcile-action` | Domain, ledger (three stores), engine, crash-matrix (real process death), race, and PostgreSQL connection-loss tests; full non-live, PostgreSQL, live smoke, and adversarial suites; 13 mutation checks | Committed (`7f5fcd6`) on `feature/auth-worker`; awaiting owner review |
| Workspace git confinement (corrective) | Workspace git can never act on another repository; concurrent test runs are safe; investigate an unexplained transient | `workspace.py`, `tools/git.py`, `pyproject.toml`, `tests/conftest.py`, `test_workspace_git.py`, `test_temp_isolation.py`, opt-in stress tests | Confinement tests against a bystander repository, three concurrent pytest runs, 12 mutants, the stress suite, full non-live suite | Committed (`34e75c1`) on `feature/auth-worker`; awaiting owner review |
| Authentication and authorization (E3) | Real authentication and server-side authorization for the future web and phone app; approval decisions record who made them | [authentication.md](authentication.md); `api/auth/`, `domain/principals.py`, migration `0004`, `auth` CLI, `openapi.yaml` security model | Real signed tokens in every test, a permission matrix over all 11 operations, both stores, sandbox canaries, a real uvicorn run in development mode, 20 mutants; full non-live, PostgreSQL, live smoke, and adversarial suites | Committed (`f0f733c`) on `feature/auth-worker`; awaiting owner review |
| Worker coordination (E4) | Several worker processes share PostgreSQL without two owning a run: claims, leases, heartbeats, fencing tokens, takeover, crash recovery; a stale worker cannot mutate the run | [workers.md](workers.md); `domain/claims.py`, `persistence/claims.py`, migration `0005`, `engine/ownership.py`, `worker/`, `worker run` and `worker enqueue` | Unit tests; 13 race and failure scenarios against a real PostgreSQL (threads and forced interleavings); real worker processes that die, stall as zombies, and stop on Ctrl+Break; a real database restart; 15 mutants; full non-live, PostgreSQL, live smoke, and adversarial suites | Committed (`43d281b`) on `feature/auth-worker`; awaiting owner review |
| Approval and reconciliation consumption (E5) | Complete the human-in-the-loop backend: approval expiry, decisions consumed by workers with every re-check, rejection semantics, a reconciliation API with a 15-minute retry authorization, an execution timeline, an attention inbox, operator-command safety; no frontend | `openapi.yaml` (2 operations, contract-first), migration `0006`, `domain/{reconciliation,attention,timeline}.py`, the recorder's re-checks, `Executor.advance`, `worker/operator.py`, the API's reconciliation and inbox | 11 races and 10 real process deaths on PostgreSQL, end-to-end HTTP-to-worker tests in both stores, 22 mutants, full non-live, PostgreSQL, live smoke, and adversarial suites | Committed on `feature/auth-worker`; awaiting owner review |
| Dashboard v1 | Mobile-first dashboard to observe, approve, and reconcile, with failure and recovery evidence; owner-authorized npm and pip installs; no API change | `frontend/` (React, TypeScript, Vite), [frontend.md](frontend.md), [dashboard-reliability.md](dashboard-reliability.md), browser, E2E, and reliability suites | 53 Vitest unit and component tests (not counted as reliability evidence); 7 contract and source-policy tests; 16 browser, 7 E2E, and 14 reliability tests against real Chrome, the API, PostgreSQL, and workers, the reliability suite run three times; 10 frontend mutants | Committed (`322d407`) on `feature/dashboard-v1`; awaiting owner review |
| Attention notifications v1, phase 1 | Notify the owner when a run needs judgment, without the dashboard open, through a durable transactional outbox and a dispatcher; compare SMS, email, and push channels and providers first; never put approval authority in a notification; stop before any real provider, account, credential, or send | [notifications.md](notifications.md) (with the provider comparison), migration `0007`, `domain/notifications.py`, `domain/notification_repositories.py`, `persistence/notifications.py`, `notifications/` (dispatcher, provider boundary, fake provider), `notifications` CLI, a contract-first read-only `notifications` field, the dashboard's notification line; a pre-existing gap fixed (a waiting run is re-synced by `Executor.advance`) | In-memory, HTTP, and PostgreSQL tests, including seven crash boundaries with real processes killed; 7 real-stack browser scenarios, including outages of every component; the full regression (default, PostgreSQL, browser, live smoke, adversarial, `sandbox check`); 19 manual mutants | Committed (`d6905e8`) on `feature/notifications-v1`; real provider awaiting owner authorization |
| Real model provider v1, foundation | Replace the fake-only limitation without weakening any boundary: compare model providers first; a provider-neutral layer with minimal context, versioned prompts, strict structured output, capability manifests, budgets, usage accounting, bounded retries, prompt-injection defences, explicit activation; stop before any real call | [model-providers.md](model-providers.md) (the provider comparison, with a disclosure that the assistant is itself an Anthropic model), `providers/` (gateway, budget, guard, context, capabilities, config, the Anthropic adapter), `agents/prompts.py`, a strict `agents/schema.py`, migration `0008`, a contract-first `Run.model_usage`, the project page's usage line, an opt-in `--run-model-live` suite | Offline tests: injected failures through real executor runs, secret canaries, a compromised fake model obeying prompt injection, the adapter against a loopback server; PostgreSQL usage tests; 11 manual mutants; the full regression | Committed on `feature/real-model-v1`; the first live call awaits owner authorization |
| Runtime provider selection | Choose the first runtime provider and model independently of the development-time assistant, from current official sources, with Bedrock given serious consideration; decision only | [design/runtime-provider-selection.md](design/runtime-provider-selection.md) (with a disclosure that the assistant is an Anthropic model) | Official documentation and pricing pages read 2026-10-06; unverifiable figures marked as such | Committed (`3010d13`); owner approved the Claude API with `claude-sonnet-5-5` |
| Sonnet 5.5 compatibility fix | Make the existing adapter match the approved model's documented API: no temperature, no forced tool use, the documented thinking setting, spend limits block, refusals fail, a conservative estimate, the $0.50 live ceiling enforced before each call; offline only | `providers/anthropic.py`, `providers/base.py` (`ProviderSpendLimit`, `ProviderRefusal`), `providers/budget.py` (upper-bound estimate), `providers/gateway.py`, `tests/test_anthropic_compat.py`, the live suite's ceiling | Offline adapter tests, including whole runs through the adapter against a loopback server; 9 mutants; the full regression; zero provider calls | Committed (`4483a80`) on `feature/real-model-v1` |
| First live acceptance | Run the controlled live suite with the owner's key, $0.25 per run and $0.50 per suite; stop on authentication failure; report defects before fixing them | Two owner-approved corrections: the escape test requires a validated real answer (`39d2dc6`); unusable answers have their structure audited, and the re-ask names the missing tool call (`bb3cee3`) | Three sessions failed on the key (401, $0); one failed at the planner (no tool call, about $0.02); the next passed 2/2 ($0.07) but showed Claude calling capability names as tools | Committed; the live suite passed |
| Prompt/tool reliability correction | Separate model-provider tools from Agent Factory capabilities in new prompt versions, without exposing any capability as a provider tool; audit unexpected calls as counts only; re-run the live suite | `agents/prompts.py` (`planner/2`, `developer/2`, `reviewer/2`; version 1 unchanged), the adapter's manifest label and counts, offline tests, [model-providers.md](model-providers.md#4a-model-provider-tools-versus-agent-factory-capabilities) | Offline tests; 10 mutants; the full regression; live suite 2/2 with 6 of 6 answers valid at the first attempt and 0 unexpected tool calls (about $0.07) | Committed (`3e4b150`) on `feature/real-model-v1` |
| Capability Registry + AWS, Phase A | Read-only discovery and current official AWS documentation; design a provider-neutral Capability Registry and an AWS boundary; install, configure, or call nothing in AWS | [design/capability-registry-aws.md](design/capability-registry-aws.md), learning notes | Local checks printed names, versions, and setting *names* only, never credential values; official AWS pages read 2026-10-06, unverifiable items marked; `aws sts get-caller-identity` deliberately not run | Committed (`f4f640f`) on `feature/capability-registry-aws`; approved |
| Capability Registry + AWS, Stages B–D | A provider-neutral Capability Registry (typed definitions, evidence, availability, agent views, assessment); Core and Git tool packs without behaviour change; an AWS tool pack with a FAKE backend only; no AWS access of any kind | `capabilities/`, `tools/packs.py`, `aws/`, the gateway's capability checks, `build_executor` wiring, `requirements-aws.txt` (pinned from installed metadata, not imported), [capabilities.md](capabilities.md), [aws-capabilities.md](aws-capabilities.md) | 81 offline tests (including a real adapter run against the loopback stand-in with the AWS pack enabled); 19 mutants, one of which first survived and gained a dedicated test; the full regression; zero AWS API calls, no AWS profile read | Committed (`873c188`) on `feature/capability-registry-aws`; approved |
| Capability Registry + AWS, Stage E1 | `cloud_read` contract-first with per-environment policy; the real `BotoAwsBackend` (STS GetCallerIdentity only), explicitly activated, never a default session or chain, tested offline only; the LIVE_TESTED evidence mechanism; exact instructions for a new temporary identity; no AWS call or authentication | `openapi.yaml`, API schemas, frontend types, migration `0009`, `safety/permissions.py`, `aws/boto.py`, `capabilities/live_evidence.py`, [aws-capabilities.md](aws-capabilities.md) | Verified the installed SDK versions match the pins (nothing installed); read botocore's own source for credential-provider labels and resolution order; official AWS docs read-only; offline tests with Stubber, synthetic config, a socket recorder, and patched providers; 25 new mutants (one first survived and gained a dedicated test), the 19 earlier ones rerun; the full regression; zero AWS API calls | Committed on `feature/capability-registry-aws`; awaiting owner review |

## Disclosure and commit attribution

All commits are made with the owner's Git identity.

| Commits | How AI assistance is disclosed |
| --- | --- |
| The first 13 commits, from `8cfcd55` (v0.1 baseline) to `203dcda` (Zoomcamp alignment) | Each carries a `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` trailer |
| The product-data-foundation commit (`41ccff8`) | No trailer. Its message ended with a one-line plain-text note that it was AI-assisted. |
| From the postgres-persistence commit onward | No trailer, and no AI-assistance line in the commit message. Disclosure is this page and the [learning notes](zoomcamp/LEARNING_NOTES.md#ai-assisted-development-how-agent-factory-was-built). |

The owner's instructions, verbatim:

> "Do not add AI co-author trailers to commits automatically. Use the
> repository owner's configured Git identity for commits unless explicitly
> instructed otherwise."

> "Do not add an AI-assistance line to the commit message unless it is
> already part of an explicitly documented repository commit policy."

This repository's commit policy is therefore: the owner's identity, no AI
trailer, and no AI line in commit messages. AI assistance is disclosed here,
in the documentation.

A commit without a trailer **does not** mean the owner wrote that code by
hand. The working method above applies to every commit in this repository.

## Not yet in the repository

- **The owner's full assignment texts.** This page summarizes their
  structure and quotes representative constraints. Whether to publish the
  full texts is the owner's decision.
- **A general automated mutation-testing pipeline.** Bounded mutation checks
  are recorded in the validation documents. Hosted CI is now enabled; its current
  evidence is recorded in the Day 2 section below.

## Zoomcamp Day 1 readiness

At the owner's request, Codex inspected the implementation, compared its old
submission documents with the current official rubric, ran existing checks and
the scripted repair demo, and updated reviewer documentation. No additional
agent team was spawned for this milestone. The owner set scope and submission
priority; fresh verification and limitations are recorded in
[Day 1 validation](zoomcamp/DAY1_VALIDATION.md).

In the Day 1 continuation, the owner authorized a data-preserving Docker repair.
Codex used a separate read-only agent to review the stale-socket repair and full
browser-test selection. The primary agent preserved the runtime socket folders,
verified engine recovery, and ran the database and browser suites serially.
Results and remaining submission gaps are in the same Day 1 validation record.

## Zoomcamp Day 2 delivery

The owner authorized a reproducible reviewer setup, one-command startup, CI and
deployment preparation. Codex used a primary integration agent plus three
bounded subagents: `reviewer_design` implemented the local launcher;
`ci_deployment` prepared CI, deployment documentation and reviewer instructions;
`review_docker_repair` independently audited isolation and worked on safety
mutation checks. The primary agent integrated fixes and performed clean setup
and platform validation. These are development-time collaborators, not evidence
that Agent Factory's runtime agents deployed or operated the application.

Concrete review findings shaped the delivery: the browser harness uses installed
Chrome; hosted CI must exclude local-container restart cases; a rejected
calculator deletion can coexist with a completed run, so the walkthrough checks
the decision and execution records separately. CI action references were checked
against official upstream commits, and actionlint 1.7.7 passed on the staged
workflow using an existing image with network access disabled.

The [Day 2 task record](zoomcamp/DAY2_TASKS.md) links acceptance criteria to
artifacts and assigned roles. The [Day 2 validation](zoomcamp/DAY2_VALIDATION.md)
records actual test, audit and mutation outcomes; assignment alone is not proof
of a successful check. See the [reviewer guide](REVIEWER_GUIDE.md),
[CI workflow](../.github/workflows/ci.yml) and
[deployment runbook](deployment.md). The owner subsequently authorized public
source publication: [the repository](https://github.com/BryanHunt290/agent-factory-zoomcamp) and this task/evidence record are
published at [initial commit `40cea253`](https://github.com/BryanHunt290/agent-factory-zoomcamp/commit/40cea25324288e1e6ef6b2e8180dc3f85e797b63). Hosted CI is enabled;
[the initial run](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions/runs/40065197804) passed all Python, frontend and PostgreSQL/browser jobs
without application or workflow code fixes. Counts and limits are recorded in
[publication evidence](zoomcamp/PUBLICATION.md). Final submission-commit selection,
production identity, public runtime deployment, CD and course submission remain
outstanding. The earlier workflow history above is preserved.

The public repository starts with a fresh source snapshot of the application,
tests, CI and reviewer documentation. The private authoring Git history and
unrelated personal/business skill catalog were excluded; public context
documents retain the application's engineering and safety boundaries. This
publication step did not deploy the application. Historical development
references above describe the authoring history, not commits included in the
new public Git history.

The publication follow-up CI run exposed an intermittent expiry-race test
assertion. The coordinating agent and CI agent independently identified the
missing accepted-before-expiry / lapsed-before-dispatch outcome. A deterministic
PostgreSQL regression reproduced the old assertion failure, then verified the
corrected outcome checks. An in-process expiry-guard bypass was rejected by the
new zero-effect assertion, and the restored case passed. Only test coverage
changed; application authorization logic and CI selectors remain intact. The
[publication record](zoomcamp/PUBLICATION.md#follow-up-approval-expiry-race-regression)
preserves the failed run, diagnosis and validation.
