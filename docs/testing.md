# Running tests and demos

## Offline supervised coding milestone status

The opt-in [supervised demonstration](offline-supervised-demo.md) has its own
focused file, `tests/test_supervised_demo.py`: fresh validation passed all 91 tests.
The standalone repair completed with one failing five-test execution followed by
one passing five-test execution and a matching scripted review. An in-memory
execution-ID mutation was killed by the intended assertion, followed by a passing
restored baseline. See the [current validation record](offline-supervised-validation.md)
for full regression results, skip reasons and the approved filesystem access needed
to run these checks. Older suite totals below remain historical. The external
temporary-root requirement and all live-suite opt-in guards remain unchanged.

## Hosted reviewer checks

The [CI workflow](../.github/workflows/ci.yml) runs default Python/API checks,
frontend contracts/types/tests/build, PostgreSQL integration and installed-Chrome
workflow/recovery tests. [Publication evidence](zoomcamp/PUBLICATION.md) records
actual runs, including the expiry-race test correction. In that regression, a
consumed approval may lapse before dispatch: the execution must have zero
attempts and effects. The real-time race and deterministic exact-expiry case in
`tests/test_postgres_decisions.py` both enforce that boundary. No live model or
cloud call is needed for these checks.

## Test suite

From the repository root:

```bash
py -3.12 -m pytest          # Windows launcher
python3.12 -m pytest        # elsewhere
py -3.12 -m pytest -v tests/test_executor.py   # one file, verbose
.venv\Scripts\python -m pytest   # the project venv: also runs the API contract and HTTP tests
```

The API contract tests need `requirements-dev.txt` (PyYAML,
openapi-spec-validator, jsonschema), and the HTTP API tests also need
`requirements-api.txt` (FastAPI and friends); both are installed in the
project venv. Under a Python without these packages, those files are
reported as skipped, with the reason.

`pyproject.toml` puts the repo root on `sys.path`, so no install step is
needed. Nothing in the suite needs network access or credentials. Each test
builds its workspace under pytest's `tmp_path`.

**Each run has its own temporary root.** pytest gives every run a numbered
`pytest-<n>` directory under the system temp directory, locked while the run
is alive, so concurrent runs never share or delete each other's files. A
temporary root inside the repository (`--basetemp` or `PYTEST_DEBUG_TEMPROOT`)
is refused before anything is created: test workspaces are git repositories
and must never nest inside this one (`test_temp_isolation.py`, which runs
three pytest processes at once). Until this fix, `pyproject.toml` pinned
`--basetemp=.pytest_tmp`, one directory inside the repository that every run
wiped when it started.

The executor tests use real workspaces and run the calculator's acceptance
tests in a real `unittest` subprocess via `LocalProcessBackend`, which is
used for trusted fixtures only. Only the model is faked.

The default (non-live) suite never uses Docker. The real docker CLI runner is
replaced by one that reports Docker as unavailable, so untrusted runs block,
and any attempt to launch `docker` fails the test. The suite takes about 50
seconds in the venv.

## Coverage of the required behaviours

| Requirement | Tests |
| --- | --- |
| Successful graph execution | `test_executor.py::test_successful_graph_execution`, `test_execution_log_records_required_fields` |
| Test failure -> developer repair | `test_test_failure_routes_back_to_developer`, `test_both_repair_loops_in_one_run` |
| Reviewer rejection -> developer repair | `test_reviewer_rejection_routes_back_to_developer` |
| Retry exhaustion | `test_retry_exhaustion_on_test_failures`, `test_retry_exhaustion_on_review_rejections`, `test_max_steps_backstop` |
| Checkpoint/resume | `test_checkpoint_and_resume_after_interruption`, `test_resume_is_independent_of_executor_instance_for_every_checkpoint`, `test_persistence.py` |
| Approval gate | `test_approval_gate_pauses_then_resumes_when_approved`, `test_approval_gate_rejection_skips_the_action`, `test_wrong_approval_id_is_refused`, `test_safety.py::test_approval_gate_blocks_until_human_decides[*]`, `test_cli.py::test_cli_approval_flow` |
| Invalid graph transition | `test_invalid_transition_fails_the_run`, `test_graph_definition.py::test_undeclared_outcome_is_an_invalid_transition`, `test_invalid_graphs_are_rejected`, `test_unbudgeted_cycle_is_rejected` |
| Conservative permissions | `test_safety.py` (default policy, hard floor, network deny, allowlists, env scrub), `test_developer_cannot_modify_protected_acceptance_tests`, `test_developer_cannot_escape_workspace` |
| Append-only log | `test_persistence.py::test_log_is_append_only_and_hash_chained` |
| Git-friendly workspaces | `test_workspace_git_snapshots`, `test_git_workspace_commits_each_developer_iteration` (skipped if git is absent); confinement in `test_workspace_git.py` (below) |

## Demos

```bash
py -3.12 -m agent_factory demo --fresh                       # default: repair scenario
py -3.12 -m agent_factory demo --scenario full --fresh
py -3.12 -m agent_factory demo --scenario exhaust --fresh    # exits 1: budget exhausted
py -3.12 -m agent_factory demo --scenario approval --fresh   # pauses; follow the printed approve/resume commands
```

| Scenario | Route |
| --- | --- |
| `repair` | planner -> developer (bug) -> tester FAIL -> developer -> tester PASS -> reviewer APPROVED -> done |
| `review` | ... tester PASS -> reviewer CHANGES_REQUIRED -> developer -> tester PASS -> reviewer APPROVED -> done |
| `full` | test-failure repair, then reviewer-rejection repair, then done |
| `exhaust` | developer never fixes the bug; stops after 3 repairs (`exhausted`) |
| `approval` | developer requests `delete_file legacy_calc.py`; run pauses for a human |

Demo workspaces go to `workspaces/demo-<scenario>/` (git-ignored). `--fresh`
deletes and recreates a workspace, but only one that contains an
`.agent_factory/` marker. Use `--no-git` to skip git initialisation.

Inspect a run:

```bash
py -3.12 -m agent_factory status --workspace workspaces/demo-repair --run-id demo
py -3.12 -m agent_factory log    --workspace workspaces/demo-repair --run-id demo
git -C workspaces/demo-repair/project log --oneline
```

## Product domain and persistence tests (unreleased)

These tests are part of the default non-live suite.
[product-domain.md](product-domain.md) describes the model they test.

| File | Covers |
| --- | --- |
| `test_domain_model.py` | Stable and opaque IDs; deterministic JSON round trips; strict schema validation (unknown or missing fields, wrong record type, malformed or newer versions, older versions with no upgrade path) and the upgrade mechanism; JSON-safe, bounded metadata; milestone and run state machines; project-defined milestone catalogs (data engineering, web app, AI project); invalid policies; the actor placeholder |
| `test_domain_approvals.py` | Action fingerprints; approval for action A never authorizes action B; idempotent duplicate, conflicting, and racing decisions; approval transitions; expiry and cancellation; the fields a mobile approval screen needs; a future authenticated actor |
| `test_domain_repositories.py` | **The repository contract, run against both stores**: in memory always, and PostgreSQL in the `[postgres]` variants. Covers: every interface; scoping by project and run; references that must exist; unique natural keys; decision binding; transactions that commit completely or not at all, including nested blocks; illegal updates refused; deterministic ordering (equal timestamps; identifiers by code point); canonical timestamps; time-range queries; the overnight questions |
| `test_domain_engine.py` | Engine integration with real workspaces and real test runs. Covers: project identity across runs; legacy workspaces; meaningful milestones for repair, review, and exhausted runs; the execution log unchanged; v0.2 approval semantics preserved; decisions bound to the engine's pending action; blocked-then-resumed milestones; crash and resume without duplicates; recording failures surfaced, with atomic syncs and gaps filled; reconciliation (a decision stored before a crash is applied; an engine-only decision is recorded; contradictions raise; forged decisions are never applied); project-specific milestones with no core changes |
| `test_persistence_config.py` | Database configuration: URL from the environment or a file, exactly one source, validation, a test configuration that can never pick up the development database, and a password never shown in any representation or error |
| `test_db_sandbox_boundary.py` | With database settings in the orchestrator's environment: trusted fixtures, the docker argv, and every sandbox request of a full run carry nothing about the database. Database settings and credential-bearing URLs are refused even when allowlisted. |

## Durable action execution tests (unreleased)

These prove that approval is not execution
([design](design/action-execution.md), [safety.md](safety.md#durable-execution-approval-is-not-execution)).
They are in the default suite; files marked `[postgres]` also run against
PostgreSQL with `--run-postgres`.

| File | Proves |
| --- | --- |
| `test_execution_domain.py` | Stable execution IDs bound to project, run, approval, and fingerprint; idempotency keys stable across retries; the six states and exactly their transitions; illegal changes refused, never repaired; the class policy (read-only and idempotent retry, keyed only inside the provider's window, non-idempotent becomes uncertain); bounded retries; reconciliation (three choices; a retry is a new 15-minute authorization bound to the prior attempt and fingerprint; only a person; `actions:reconcile`); strict, versioned records; bounded results |
| `test_tool_semantics.py` | Every built-in tool declares its class with a rationale; undeclared dangerous tools are refused at registration and cannot be declared read-only; undeclared ordinary tools are class C; inconsistent declarations are refused |
| `test_execution_ledger.py` `[postgres]` | **One ledger contract over three stores** (checkpoint file, in memory, PostgreSQL): idempotent, durable authorization; the product store binds an execution to an *approved* decision for the same action, one per approval; `begin` is durable before it returns (seen through a second connection); a stale writer never dispatches; **six concurrent contenders: exactly one dispatch**; keyed retries reuse the key; a dispatch must begin before expiry; reconciliation idempotent and never overwritten; impossible changes refused; tampered, contradictory, or torn ledger files refused; a filesystem error (a full disk) makes the checkpoint ledger unavailable, never wrong, with no path in the message; a run bound to one kind of ledger; PostgreSQL refusing invalid rows on raw SQL |
| `test_action_execution.py` `[postgres]` | The engine. **Regression: a node with two approvals runs its first approved action once** (checkpoint, memory, PostgreSQL); an identical repeated call in one visit is one effect. **Regression: a crash after a non-idempotent side effect never resends it** (real child process killed inside the tool; restart; `awaiting_reconciliation`, one notice). **Audit: the dispatch is in the hash-chained log although the process died inside the tool.** The crash matrix (below). Reconciliation flows: it happened, did not happen, retry within 15 minutes, retry expired. A swallowed reconciliation signal still pauses the run; an approval the re-run node no longer asks for lapses; an expired approval cannot dispatch; an unavailable ledger blocks the run and the outcome is recovered from the log; a filesystem error at the write-ahead blocks the run (it used to fail it) and sends nothing; a run cannot switch ledgers; the run lock (in process, and across a real second process); repeated approval processing; milestones. PostgreSQL: losing the connection before the write-ahead commit dispatches nothing; losing it after the side effect never resends it; real process death with the PostgreSQL ledger |

**The crash matrix.** `tests/crash_child.py` runs the fixture in a child
process that ends with `os._exit(137)` (no cleanup, like `kill -9`) at a named
boundary; the test then resumes in a fresh executor and counts real effects in
the fixture's oracle files:

| Boundary | Class C (`send_notice`): after restart | Class B keyed (`charge`): after restart |
| --- | --- | --- |
| Before the dispatch record | Completes; 1 notice | Completes; 1 charge |
| After the dispatch record | **Awaiting reconciliation; 0 notices** (a safe false positive) | Completes; 1 charge |
| Immediately before the tool | Awaiting reconciliation; 0 notices | Completes; 1 charge |
| After the tool returns, before the outcome is recorded | **Awaiting reconciliation; 1 notice, never resent** | Completes; **2 calls, 1 charge** (same key, deduplicated) |
| After the outcome is in the log, before the ledger | Completes, recovered from the log; 1 notice | Completes; 1 charge |
| After the ledger outcome | Completes, replayed; 1 notice | Completes; 1 charge |
| Before the node checkpoint | Completes, replayed; 1 notice | Completes; 1 charge |
| After the node checkpoint | Completes; 1 notice | Completes; 1 charge |

A keyed retry after the provider's window becomes uncertain instead of
reusing the key.

**Manual mutation check (E1-E2).** Thirteen safety properties were broken on
purpose, one at a time; each made a test fail, then the code was restored:
dispatching before the durable record; omitting the audit entry; ignoring the
compare-and-set version; resending a non-idempotent action; re-running a
succeeded action; ignoring an outcome already on disk; dispatching after
expiry; a retry window longer than 15 minutes; running an approved action
without a ledger; accepting an undeclared dangerous tool; resuming a
database-backed run without its database; executing without an approved
decision; ignoring an uncertain action an agent swallowed.

Two of the thirteen are caught by a second line of defence rather than by the
targeted assertion: without the version check, the in-memory store's stale
writer hits the attempt-uniqueness check (`ConflictError`); and with the replay
branch skipped, the state machine refuses to restart a succeeded execution, so
the run fails. A stronger replay mutant, which really runs a recorded success
again past the ledger, is caught by the effect counts: the two-approval
regression sees the file deleted twice, and the crash matrix sees two notices.

**Restore the bytecode, too.** Python trusts a cached `.pyc` whose source has
the same size and modification second. The 30-minute retry-window mutant has
the same length as the original and was restored within a second, so later
runs kept executing the mutant although the source was correct. The next full
run caught it: four retry-window tests failed. After a mutation check, delete
the `__pycache__` bytecode of the modules you changed (or run the check with
`PYTHONDONTWRITEBYTECODE=1`), and confirm that the targeted tests pass on the
restored code. The E1-E2 check was then repeated that way, with a passing
baseline before every mutant; all thirteen were killed again.

The final regression also found that a filesystem error while writing the
checkpoint ledger *failed* the run instead of blocking it (Windows had briefly
denied a replace). The ledger now reports such errors as unavailability, and
the atomic write retries a transient Windows denial (`test_persistence.py`).
Six more mutants cover that code, and all six are killed: a raw write error,
in a ledger change or in the binding marker; a path in the message; no retry;
a persistent denial swallowed; and a retry where a denial is a real permission
problem.

## Workspace git confinement tests (unreleased)

`test_workspace_git.py` nests each workspace inside a bystander repository
(one commit, one staged file, local config). Every scenario requires that
repository's `.git` to be byte-for-byte unchanged afterwards
([safety.md](safety.md#process-and-git-isolation)).

| Scenario | Proves |
| --- | --- |
| A healthy nested workspace | Commits land in `project/.git`, with the workspace's own identity |
| `project/.git` deleted | `add`, `commit`, `status`, and `config` are refused; `git_commit` raises instead of skipping; nothing re-creates the repository |
| Nested, with its `.git` missing | A control shows that unconfined git now finds the parent. Every workspace command is refused, and the parent's index, config, and `HEAD` are unchanged |
| `.git` replaced or malformed | Refused: a `.git` file or a junction pointing at the parent, a plain file, an empty directory, a corrupt `HEAD`, or `core.worktree` or `commondir` pointing at the parent |
| Initialization | Refused over an existing `.git` entry |
| The incident, replayed | `.git` lost between `config user.name` and `config user.email`: refused, and the parent's config is unchanged |
| Lost after the check | The repository, or the whole workspace, deleted between the check and the command: the command fails and never redirects |
| Inherited `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` | Ignored: the commit lands in the workspace |
| Concurrent damage (stress) | A thread deletes and re-creates `.git` during 60 commits; the parent is unchanged |

**Manual mutation check.** Twelve mutants were run. Nine are killed:
inherited `GIT_*` variables kept; no check before commands; commands neither
pinned nor capped; the root or the common directory not compared;
initialization over an existing `.git`; `git_commit` skipping a damaged
repository; no guard against a temporary root inside the repository; a shared
temporary root. Two of them send a real commit toward the bystander
repository. Without the check, git follows a `.git` file pointing at the
parent, and the test sees the commit succeed. Without pinning or the ceiling,
a lost repository makes the commit land in the parent, and the byte-for-byte
comparison catches it. Three are caught by another layer, by design: commands
unpinned but still capped, no ceiling, and no plain-directory check.

## Stress tests (opt-in)

Slow repetitions that hunt transient failures. They are opt-in like the live
suites (marker `stress`); with the flag, a skip fails:

```bat
.venv\Scripts\python -m pytest -m stress --run-stress -v
```

| Test | Repeats |
| --- | --- |
| `test_executor.py::test_resume_from_every_checkpoint_under_repetition` | 25 rounds of stopping after each node and resuming in a fresh executor (175 resumes) |
| `test_persistence.py::test_rapid_checkpoint_rewrites_survive_transient_replace_denials` | 3,000 real checkpoint rewrites |
| `test_workspace_git.py::test_a_workspace_damaged_concurrently_never_redirects_git_to_the_parent` | Concurrent `.git` damage during 60 commits |

**An unexplained failure: an unconfirmed transient.** During E1-E2,
`test_resume_is_independent_of_executor_instance_for_every_checkpoint` failed
once in a full run, and its traceback was not kept. It has not recurred in
any later full run, in 33 targeted runs, in the stress rounds above, or in
280 stop-and-resume cycles under the original conditions (the temporary
directory inside the repository, and no replace retry).
A probe found 5 transient replace denials in 9,000 rapid rewrites inside the
repository's working tree, all cleared within 50 ms, and none in 9,000 outside
it. The bounded replace retry and the move of test temporary roots out of the
repository address that kind of failure. Whether it caused this one is
**not shown**.

## Authentication and authorization tests (unreleased)

Every token in these tests is really signed, by the TEST ONLY issuer in
`tests/auth_support.py` (EC and RSA keys generated in memory for the run), and
checked by the production verifier. Files marked `[postgres]` also run
against PostgreSQL with `--run-postgres` ([authentication.md](authentication.md)).

| File | Proves |
| --- | --- |
| `test_auth_tokens.py` | A valid token establishes issuer and subject (EC and RSA). Refused: malformed and encrypted-looking tokens; unsigned (`alg: none`); **HMAC signed with the published RSA public key (algorithm confusion)**; an algorithm outside the allowlist; another key's signature; a changed payload; an unknown or non-text `kid`; a key used with another algorithm; a 1024-bit RSA key; `crit` headers; a missing required `typ`; expired (leeway tolerated); not yet valid (`nbf`, `iat`); another issuer, including a trailing slash or other case; another audience; a missing `exp`, `iat`, `sub`, `aud`, or `iss`; an invalid subject; too long a lifetime. A policy cannot allow HMAC or `none`. Key sets: only public signature keys; cached; refreshed once for a rotated key but not for floods of invented key IDs; fail closed when unobtainable, after at most an hour of stale keys; https only |
| `test_auth_settings.py` | Production is the default and an unconfigured API does not start (also through `api serve`); production refuses http, credentials in URLs, HMAC, and the development issuer in every form; production and development settings are never active together, in either mode; development needs an explicit opt-in and refuses production settings; development tokens verify from the public keys alone; **a production configuration pointed at the development keys still refuses development tokens**; the development CLI works only in development mode and never prints a token |
| `test_auth_http.py` `[postgres]` | 401 `authentication_required` without a credential; **the same 401 `invalid_token`, with the same text, for every refused credential** (malformed, other key, expired, wrong issuer or audience, another issuer's keys, not enrolled, disabled); malformed headers; a disabled principal refused until enabled; identity is issuer and subject, never email or name. **Every operation requires exactly its documented permission** (all but that one: 403 naming it; only that one: allowed), and each route's single check matches the contract; `approvals:read` does not allow deciding; `approvals:decide` does not allow reconciling; creating does not allow reading; token claims and headers grant nothing; authorization comes before any lookup and before the fingerprint's 409. A decision records the authenticated principal; a client cannot send actor, `decided_by`, `verified`, `user_id`, or `decided_at`; the fingerprint is still required (409 when wrong); an identical retry is 200 for the same principal and 409 for another. Rate limits per client and per principal (429 with `Retry-After`). Tokens and `Authorization` headers never reach logs or responses. PostgreSQL: the audit query in authentication.md, run as written |
| `test_auth_principals.py` `[postgres]` | Issuer and subject map to one stable principal; the same subject from another issuer is someone else; identity never changes while email and name do; permissions come from a closed vocabulary and need an enrolled principal; **identity, permissions, and disabled state survive a reconnect**. PostgreSQL refuses invalid principal rows, unknown permissions, orphan grants, and any `user` decision not naming its enrolled principal, on raw SQL |
| `test_auth_boundary.py` | Authentication settings and token canaries never reach a real trusted local process; a sandbox request never carries them, even allowlisted, including a token under an innocent name; the engine, domain, sandbox, tools, and agents never import authentication code; an authenticated decision stores the principal and never the token |

`test_sandbox_docker.py` also refuses authentication settings and a JWT
value in the final `docker create` arguments, and `test_postgres_migrations.py`
checks that both authentication lookups use their indexes.

**Manual mutation check (E3).** Twenty properties were broken on purpose, one
at a time, with a passing baseline first and bytecode writing disabled; each
made a test fail, and the code was restored byte for byte: a refused
credential proceeding unauthenticated; the permission check removed;
`approvals:read` accepted for deciding; signature verification disabled; the
issuer check removed; the audience check removed; the expiry check removed;
the algorithm allowlist ignored; a disabled principal accepted; every
principal given every permission; a decision recorded without the
authenticated principal; request models accepting unknown fields (an actor);
development authentication as the default; production accepting the
development issuer's settings; the `Authorization` header logged; the log
redactor keeping tokens; the decision quota removed; the refused-credential
limit removed; the sandbox accepting a token under an innocent name; and a
`user` decision stored without an enrolled principal.

## Worker coordination tests (unreleased)

Workers, claims, leases, and fencing ([workers.md](workers.md)). The
PostgreSQL files need `-m postgres --run-postgres` and a real database:
nothing about coordination is proved with mocks. The in-memory claims in the
default suite pin down the rules, but prove nothing about processes.

| File | Proves |
| --- | --- |
| `test_worker_claims.py` | The claim model: worker IDs are random per process and never a hostname; the lease policy's defaults (60 s, 20 s heartbeats) and bounds (2 s to 5 min, at least three heartbeats per lease); the local deadline counts from the send time and ends before the lease; fences need a real run, worker, and generation 1 or more; impossible claim records are refused; **the boundary instant belongs to expiry**; fencing admits only the current generation of the current worker with a valid lease; refusals say why (superseded, expired, released); `OwnershipLost` is not an `Exception`. The in-memory repository: one claim per run, +1 per claim, takeover only after expiry, oldest first, finished runs never claimed, renewal only while valid, release only for the current generation, and generation 41 refused once 42 exists |
| `test_worker_engine.py` | The executor under a scripted ownership: ownership proved before **every** checkpoint write; a lost ownership writes no checkpoint and records no outcome (not failed, not blocked), and the next owner re-runs the uncommitted node; an agent catching `Exception` cannot swallow `OwnershipLost`; hand-back between nodes (`run_yielded`) and continuation by the next owner; an ownership for another run refused before anything is touched; a claimed executor never creates a run; runs waiting for a person, blocked, or finished are left untouched (nothing written); **no dispatch when ownership is lost after the write-ahead record**, with `action_not_dispatched` as evidence, which the next owner recovers before sending the action once |
| `test_worker_loop.py` | The lifecycle on the in-memory store: claim, advance, release by outcome; a run waiting for a decision released untouched, to be woken at its approval's expiry; a step budget hands the run back; **graceful shutdown** finishes the node in progress and releases the run for another worker; a worker that loses its claim stops at once and does not release it; a busy run (`RunLocked`) and a run this worker cannot run are offered again (or not) as documented; database failures back off (doubling, capped) and open a new session; events hold no paths, URLs, or exception text, and refuse unknown names and reasons; settings are validated. The lease keeper: renewal moves the local deadline from the send time; a refused renewal loses the claim for good; an unreachable database never extends the deadline. Claim ownership: refused once doubtful, a database error counts as lost, `should_yield` on stop, budget, and doubt. The CLI's first signal stops gracefully and the second at once |
| `test_worker_cli.py` | `worker run` refuses unsafe settings before touching the database. `[postgres]`: `worker enqueue` creates and queues runs, and queues an existing one; **two `worker run` processes** share two queued runs of one project, each finished once, never at the same time (a run whose workspace is busy is released `run_busy` and claimed again); each prints its own worker ID; the database URL is never printed |
| `test_postgres_claims.py` `[postgres]` | Claim, renew, release on PostgreSQL; lease length measured by the database clock; the database refuses impossible claim rows (raw SQL); a finished run is never claimed. **The acceptance test: generation 41 is refused once 42 exists, although worker A's own clock says its lease is valid.** Every write of the stale generation is refused and leaves nothing (prove, run status, product sync, milestone, approval request, authorization, write-ahead, even a read); the current generation may write; stale heartbeat and stale release refused; the same worker's previous generation refused; an expired lease refuses writes and renewal before any takeover; **the expiry instant belongs to the next owner** (one transaction, one `now()`); tokens increase by exactly one per claim; a claimer skips a locked row instead of waiting; **a fenced write in progress holds off a takeover**; real-time expiry and takeover. Races (threads released by a barrier, repeated): 2 and 10 workers for one run, 10 workers for 10 runs, renewal against takeover. Through the engine: a stale worker cannot write its checkpoint or run state; cannot record a dispatch, so nothing is sent; loses the run after the write-ahead, so the tool never runs; an action in flight during a takeover is recovered from its evidence, never resent. Connection drops: a dropped heartbeat connection is replaced while the database is healthy; a worker that cannot reach the database stops advancing and is taken over. No worker session sits idle in a transaction while a run is advanced |
| `test_worker_processes.py` `[postgres]` | **Real worker processes** (`tests/worker_child.py`): a worker dying inside its claim transaction claims nothing; one dying right after its claim commit keeps the run until its lease expires, then is taken over; four processes race and exactly one ever owns the run; a process killed mid-run is taken over after its lease expires; **a zombie process** (never renews, always believes its lease) wakes after a takeover, cannot record its in-flight action's outcome, never advances the checkpoint, and the next owner recovers the action from the zombie's own evidence without resending it; a real Ctrl+Break (SIGTERM elsewhere) stops a worker after its node and releases the run |
| `test_worker_restart_live.py` `[docker_live, postgres]` | **A real PostgreSQL restart** while a worker owns a run: during the outage the owner sends nothing, commits nothing, and records no outcome; afterwards another worker takes over with generation 2 and sends the approved notice exactly once. It restarts only a container named `agent-factory-postgres-*` that publishes the test database's port, and refuses any other |

`test_postgres_migrations.py` checks migration 0005's table, constraints, and
index, and that the claim, fence check, renewal, and release statements, as
the code sends them, are served by their indexes.

**Manual mutation check (E4).** Fifteen defects were made on purpose, one at
a time, against a passing baseline, with bytecode writing disabled and the
module's `.pyc` dropped after every write. Each made its named test fail, and
the code was restored byte for byte:

1. the fence check ignoring the token;
2. the claim's expiry comparison `<=` turned into `<`;
3. the fence check without the lease-expiry condition;
4. a heartbeat accepted from a stale worker;
5. a release accepted from a stale worker;
6. the token not incremented on takeover;
7. the claim without `FOR UPDATE SKIP LOCKED`;
8. the dispatch forgetting its ownership check;
9. a checkpoint written without proving ownership;
10. a fenced transaction skipping its proof;
11. statements outside a transaction left unfenced;
12. `OwnershipLost` as an ordinary `Exception`;
13. renewal allowed after expiry;
14. an unreachable database extending the local deadline;
15. an outcome logged before ownership was proved.

The first eight are the representative defects the E4 brief listed.

## Human decisions tests (unreleased, E5)

Approval expiry, reconciliation, the execution timeline, the attention
inbox, and workers consuming decisions ([workers.md](workers.md#human-decisions-wake-ups-and-consumption)).
Every flow runs a real `Worker` loop; the action fixture's oracle file counts
real effects. PostgreSQL files need `-m postgres --run-postgres`.

| File | Proves |
| --- | --- |
| `test_worker_decisions.py` | On the in-memory store: a paused run sleeps until a decision wakes it, and its action runs exactly once; every consumed decision names the person (principal) and the worker (`wkr-…@N`); a rejection reaches the engine, the graph follows it, and the request is never asked again; an undecided approval expires on time (not one second early; the instant itself is expired) and its action never runs; a decision made in time but applied after the expiry is never used; an approval applied in time cannot *begin* after the expiry; a decision that no longer matches the exact pending action (parameters, node, agent) or whose request's status disagrees is refused and the run parked; an approval by someone disabled or stripped of `approvals:decide` since is applied as refused, while a rejection always applies; the local operator may decide, a system actor never approves; the three reconciliation answers, including a retry that is a new 15-minute authorization from the person's decision (in time: exactly one more attempt; at 15 minutes: lapses, nothing sent); an answer by someone without `actions:reconcile` is never applied, whatever recorded it; **the overnight report's questions answered from structured records alone** |
| `test_api_human_decisions.py` | Both stores, over HTTP: the phone view (summary, attention, execution, a timeline naming the worker and its generation); expiry shown, listed nowhere, and refused at the instant; disclosure canaries; a sensitive action refused rather than shown redacted; answers: 201 with server time and the principal, the run woken, no execution changed; repeats, conflicts, stale or mismatched bindings (409, nothing recorded), server-controlled and malformed fields (422), concurrent answers; `approvals:decide` and `actions:reconcile` separated in both directions; the shared per-principal limit; decisions wake their run and refused ones wake nothing; the inbox's order, filter, and cursors |
| `test_e2e_human_decisions.py` | Both stores: **an authenticated person decides or answers over HTTP, a real worker claims the run and applies it**, and the oracle counts one effect (or none for a rejection, two for an authorized retry); the timeline afterwards |
| `test_postgres_decisions.py` `[postgres]` | **11 races** (C1-C11) and **10 real process deaths** (K1-K10, `tests/decision_child.py`), listed in [workers.md](workers.md#human-decisions-e5) |
| `test_cli_decisions.py` `[postgres]` | Each command a separate process: `approve --database` and `reconcile-action --database` record intent only; `resume --database` applies under its own claim; an operator command refuses a run a worker holds and changes nothing |
| `test_postgres_claims.py`, `test_worker_claims.py` | A wake-up during a claim survives every kind of release (both stores); the wake-up counter waits for a claim in progress |
| `test_postgres_migrations.py` | Migration 0006's tables, constraints, and indexes; its backfill on a database built by 0001-0005; each new index serves its query |
| `test_api_conformance.py` | The new models match the contract; the vocabularies match it; **no API module reaches execution machinery** |
| `test_action_execution.py` | Two runs of one workspace never change its files at the same time |

**Manual mutation check (E5).** The same procedure as E4 (scratchpad runner,
bytecode disabled, `.pyc` dropped after every write, files restored byte for
byte and checked by SHA-256). Every defect made its named tests fail:

| # | Defect | Caught by |
| --- | --- | --- |
| M1 | The worker skips fingerprint revalidation | `test_worker_decisions.py::test_a_decision_that_no_longer_matches_…[params]` |
| M2 | An expired approval accepted by the worker | `…::test_a_decision_made_in_time_but_reaching_a_worker_after_expiry_is_never_used` |
| M3 | The API starts executing the approved action | `test_api_conformance.py::test_the_api_records_intent_…`, `test_e2e_human_decisions.py::test_an_http_approval_…[memory]` |
| M4 | `approvals:decide` accepted for reconciliation | `test_api_human_decisions.py::test_approvals_decide_does_not_allow_reconciling` |
| M5 | A stale worker consumes a decision (no ownership proof before applying) | `test_postgres_decisions.py::test_c9_…` |
| M6 | The reconciliation attempt check removed | `test_api_human_decisions.py::test_an_answer_not_bound_to_the_waiting_attempt_…[later-attempt]`, `…_after_an_automatic_retry_is_stale` |
| M7 | A retry reuses the original authorization | `test_worker_decisions.py::test_a_retry_that_reaches_a_worker_after_its_window_never_dispatches` |
| M8 | The retry window made permanent | the same |
| M9 | The run not made eligible after a decision | `test_api_human_decisions.py::test_a_decision_wakes_its_run_…`, `test_e2e_human_decisions.py` |
| M10 | Duplicate worker consumption (a held run can be claimed again) | `test_postgres_decisions.py::test_c1_…` |
| X1 | An expired approval accepted by the API | `test_api_human_decisions.py::test_the_expiry_instant_itself_is_expired` |
| X2 | An approved action may begin after its approval expired | `test_worker_decisions.py::test_an_approval_applied_in_time_cannot_begin_after_it_expires` |
| X3 | A reconciliation answer does not wake the run | `test_api_human_decisions.py::test_an_answer_is_201_…` |
| X4 | A release overwrites a wake-up that arrived during the claim (PostgreSQL) | `test_postgres_decisions.py::test_c2_…`, `::test_c8_…` |
| X5 | The same, in memory | `test_worker_claims.py::test_a_wake_up_during_a_claim_survives_…` |
| X6 | A run stays parked after its answer was applied before a crash | `test_postgres_decisions.py::test_k8_…` |
| X7 | The request's status not re-checked against its decision | `test_worker_decisions.py::test_a_decision_whose_request_status_disagrees_is_refused` |
| X8 | The decider's permission not re-checked when applied | `…::test_an_approval_by_someone_who_may_no_longer_decide_…[revoke]`, `…::test_a_reconciliation_by_someone_who_may_no_longer_reconcile_…` |
| X9 | Reconciliation answers escape the per-principal limit | `test_api_human_decisions.py::test_decisions_and_answers_share_one_per_principal_limit` |
| X10 | Timeline ties sorted by kind instead of causal order | `test_e2e_human_decisions.py::test_an_uncertain_action_is_answered_over_http_…` |
| X11 | The inbox lists expired requests (PostgreSQL) | `test_api_human_decisions.py::test_a_pending_approval_past_its_expiry_…[postgres]` |
| X12 | Two runs of one workspace may run at the same time | `test_action_execution.py::test_two_runs_of_one_workspace_never_change_its_files_at_the_same_time` |

M1-M10 are the ten defects the E5 brief listed; X1-X12 guard the other
protections E5 added, including the three defects found and fixed during
E5 (X4/X5, X6, X12).

## API contract tests (unreleased)

`tests/test_openapi_contract.py` checks [`openapi.yaml`](../openapi.yaml)
itself: is the *contract* valid and safe? It starts no server. Whether the
backend *implements* the contract is a separate question, answered by the
HTTP API tests below. Details are in
[api-contract.md](api-contract.md#testing-the-contract).

- **Validity:** the official OpenAPI 3.1 schema; every `$ref` resolves; no
  unused components.
- **Surface:** the exact operation set; unique operationIds; the only writes
  are `createProject` and `decideApproval`; runs and milestones are
  read-only; no graph, command, or sandbox routes.
- **Approval safety:** the fingerprint is required; no time, actor, or
  verification fields in requests; 201/200/409 semantics.
- **Shared rules:** a single error model; UTC timestamps; consistent
  pagination; the implemented bearer authentication, one `x-permission`
  per operation, and 401/403/429 everywhere; no secret or internal fields.
- **Domain agreement:**
  - ID patterns and enums equal the domain's;
  - every example validates against its schema;
  - example fingerprints, decision IDs, and risk texts are recomputed with
    the domain code.

## Dashboard tests (unreleased)

The dashboard ([frontend.md](frontend.md)) has four layers of tests. Only the
last three use the real API, and only those count as reliability evidence.

| Layer | Command | What it uses |
| --- | --- | --- |
| Unit and component (Vitest, 53) | `npm run check` in `frontend/` (also checks the generated contract types and runs `tsc`) | jsdom and a scripted fake API; **not** reliability evidence |
| Contract and source policy (7) | part of the default suite (`tests/test_frontend_contract.py`) | `openapi.yaml`, `package.json`, `package-lock.json`, the sources |
| Browser (16) and end to end (7) | `pytest -m browser --run-browser --run-postgres tests/test_dashboard_browser.py tests/test_dashboard_e2e.py` | the installed Chrome, the real API, PostgreSQL, real workers |
| Reliability (14) | `pytest -m browser --run-browser --run-postgres --run-docker-live tests/test_dashboard_reliability.py` | as above, plus this project's PostgreSQL container, which one scenario stops and starts |

Set `AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\test-database.url` first.
Set `DASHBOARD_EVIDENCE_DIR` to a directory outside the repository to have
each reliability scenario write what it observed as JSON; the results are
summarised in [dashboard-reliability.md](dashboard-reliability.md).

The browser suites need Node.js, `frontend/node_modules`, Playwright
(`requirements-browser.txt`), and an installed Google Chrome. They never
download a browser. Requested and unavailable, they fail instead of skipping.

## Model provider tests (unreleased)

`tests/test_anthropic_compat.py` pins the adapter's `claude-sonnet-5-5`
request shape:

- no `temperature`;
- `tool_choice` auto;
- `thinking` `between_tools`;
- `max_tokens`;
- the key only in its header.

It also covers refusals, spend limits, rate limits, overload, prose answers,
invalid answers, the upper-bound token estimate, and the dollar ceiling
checked before sending. Everything runs offline against a loopback
stand-in, including whole runs through the adapter.

It also proves the separation between **model-provider tools** and **Agent
Factory capabilities**
([model-providers.md](model-providers.md#4a-model-provider-tools-versus-agent-factory-capabilities)):

- the version-1 prompts keep their released digests;
- version 2 has its own digests, and states that capability names are not
  callable and that `submit_result` is the only callable tool;
- every request offers `submit_result` alone, with the manifest kept as
  description;
- project text that claims new tools changes neither the tools nor the
  system prompt;
- an unexpected tool call is ignored, never executed, and only counted
  (whether its name is in the manifest), never its name or arguments.

The live escape test passes only once a real answer was received and passed
the schema.

| Suite | Command | Calls a provider? |
| --- | --- | --- |
| Default | `tests/test_model_providers.py`, `tests/test_api_model_usage.py` | No: the conftest guard refuses every non-loopback connection, and the adapter is tested against a loopback fake server |
| PostgreSQL | `pytest -m postgres --run-postgres tests/test_postgres_model_usage.py` | No |
| Live (opt-in; **owner authorization required**) | `pytest -m "model_live and docker_live" --run-model-live --run-docker-live -v -s` | Yes, under a hard budget (10 calls, 100k input and 16k output tokens, and $0.25 per test; $0.50 for the suite, checked before each call). Requested and unconfigured, it fails |

The `--run-model-live` option lifts the network guard for that suite alone.
Details: [model-providers.md](model-providers.md#13-tests).

## Capabilities

Both suites are in the default suite: offline, no model, no network, no AWS. Details are in
[capabilities.md](capabilities.md) and [aws-capabilities.md](aws-capabilities.md).

| File | What it proves |
| --- | --- |
| `tests/test_capabilities.py` | Registration and validation (malformed, duplicate, contradictory, foreign namespace, tool mismatch, weaker semantics); immutability; evidence with provenance (documentation is never evidence; `TESTED` only for a test that exists; `LIVE_TESTED` only from a recorded run of that version); availability and misbehaving probes; agent views; the unchanged model manifest; deterministic assessment; explicit pack enablement; the gateway denying unregistered, unavailable, and unpermitted capabilities; approval through the ActionGuard; rules that only tighten; hostile project text; the Core and Git packs keeping every tool as before |
| `tests/test_aws_pack.py` | The fake identity and S3 metadata capabilities; canaries removed; unexpected and oversized results rejected; designated buckets checked before any call; fixed error categories; every availability state; no real backend before Stage E; long-term and unknown credential kinds refused, with no fallback to `default`, `internal-dev`, another profile, or the environment; no environment or profile read; only typed operations; no SDK, process, shell, or HTTP import; approval and the ActionGuard; the mutation placeholder never executable; exact IAM actions; no credential in a model request (a real adapter run against the loopback stand-in); `AWS_*` refused by the sandbox |

**Mutation check (19 mutants, all caught):**
- a duplicate capability accepted;
- an unavailable capability executable;
- capabilities offered to the model as provider tools;
- a disabled pack auto-enabled;
- the permission check bypassed;
- approval metadata weakened;
- ActionExecution semantics weakened;
- an arbitrary AWS method accepted;
- an AWS CLI path introduced;
- a static-key credential accepted;
- a binding to `default` allowed;
- an unbound slot falling back to another profile;
- a fake secret returned unredacted;
- documentation counted as `LIVE_TESTED`;
- an unregistered capability resolved;
- a failing probe counted as available;
- credential-like metadata keys kept;
- the designated-bucket check removed;
- `TESTED` claimed without the test existing.

The duplicate-ID mutant first survived, because another rule refused the same test cases. A test that
only that rule can fail was added.

### Stage E1: `cloud_read` and the real AWS backend (offline)

`tests/test_aws_boto.py` is in the default suite. It never calls AWS.
- **A synthetic AWS home:** a temporary `HOME`, config and credentials files. Static keys are planted in
  the environment and in the credentials file to prove nothing falls back to them; the endpoint
  overrides and the container credential URI are set to tempt a fallback.
- **botocore's `Stubber`** in place of HTTP.
- **A socket recorder** that fails on any name resolution or connection.
- **Patched credential providers**, so a test fails if the environment, file, process, container,
  instance-metadata, or role providers are even consulted.

| Area | What it proves |
| --- | --- |
| Vocabulary | `cloud_read` is identical in `openapi.yaml`, the API schemas, `ActionCategory`, the generated frontend types, and migration 0009 |
| Policy | development allows; staging is configurable (approval by default); production requires approval and refuses an allowing policy; no agent has a cloud tool by default |
| Invariants | a `cloud_read` capability is read-only, low-cost, trusted-adapter, slot-bound, read-verb IAM, bounded in and out; a mutating tool can't register as `cloud_read`; a network mutation can't follow "allow"; IAM changes are a denied class |
| Activation | the real backend is ignored, and never consulted, without `real_backend_authorized`; nothing in the product constructs it; the allowlist can't hold `default` or `internal-dev` |
| Credentials | static, process, role-chain, workload, web-identity, and unknown profiles are refused before any provider runs, on the stubbed and the real path; an SSO profile resolves only through the SSO provider and fails closed offline without a token; `aws login` fails closed without `awscrt`; a missing profile never falls back to `default`; wrong environment, unexpected profile, and unsupported region are refused first; static, unknown, untokened, expired, about-to-expire, and expiry-less credentials are refused; the shared credentials file is never opened |
| The call | exactly `GetCallerIdentity`, on `https://sts.us-west-2.amazonaws.com`, with the explicit credential, region and two attempts; a safe summary with no account, ARN, user ID, email, key or token; a static check of the source for no default session, no other operation, and no dynamic dispatch |
| Errors | AWS error codes and SDK exceptions map to fixed categories; no message text reaches a tool result or the log; an invalid answer is refused |
| Availability | the probe's order and its states; available is not live-tested |
| Model and sandbox | no profile, slot, account, ARN, key, token or IAM action in a manifest or view; every `AWS_*` name refused by the sandbox; `.aws/` withheld from model context |
| Network guard | the suite's own guard refuses `169.254.169.254`, `169.254.170.2`, `sts.us-west-2.amazonaws.com`, `aws-mcp.us-west-2.api.aws`, and `aws.amazon.com` |

`tests/test_capabilities.py` adds the live-evidence tests: an authorized record of the exact version and
environment counts, while fakes, a mismatched authorization, another version or environment, an
undeclared backend, a removed record, an edited record and documentation don't. The repository holds no
record.

**Mutation check, E1 (25 mutants, all caught):**
- `cloud_read` treated as a local read;
- production cloud reads auto-allowed (two ways);
- a mutating tool, or a mutating capability, labelled `cloud_read`;
- an IAM mutation allowed;
- the real backend auto-enabled;
- a default session, and the default credential chain;
- a fallback to the `default` profile;
- an environment-credential fallback;
- static keys accepted;
- an unknown credential type accepted;
- the raw identity response returned;
- dynamic STS dispatch;
- a metadata-sourced workload role accepted;
- a fake backend writing live evidence;
- an undeclared backend counted as live;
- a version mismatch, and an environment mismatch, accepted as live evidence;
- a tampered evidence chain accepted;
- the shared credentials file opened;
- endpoint overrides honoured;
- an expired credential accepted;
- raw SDK error text propagated.

The tampered-chain mutant first survived, because editing a record also broke its own hash. A test that
removes an earlier record, which only the chain check catches, was added. The 19 Stage B–D mutants were
rerun and all are still caught.

### Stage E2a: the AWS live verification harness (offline)

`tests/test_aws_live_harness.py` (79 tests) is in the default suite. It never calls AWS and never reads an
AWS credential: a synthetic AWS home, botocore's `Stubber`, and the live network guard with a fake resolver
and connector, so the allowlist is proved without a packet. It drives `tests/aws_live_support.py`, the
same harness the opt-in live test runs, through the product path: Capability Registry, Tool Gateway,
`aws.identity.inspect`, `BotoAwsBackend`, then the evidence writer.

The live test itself, `tests/test_aws_live.py` (marker `aws_live`, option `--run-aws-live`), is Stage E2b and
**has never been run**: it needs the owner's authorization. Without the option it is skipped. With it, a skip
fails, the conftest allows no process but two read-only git commands, and it replaces the network guard
with the three-host allowlist (docs/aws-capabilities.md, section 10).

| Area | What it proves |
| --- | --- |
| Opt-in | the live file holds exactly one test, marked `aws_live`, mapped to `--run-aws-live`; without the flag the default guard is in place |
| Default guard | no test may *resolve* a non-loopback name (a DNS query is network traffic too), nor connect to one: the three live hosts, the global STS endpoint, S3, the AWS MCP server, and the metadata addresses are all refused; loopback still works |
| Live guard | exactly three `us-west-2` hosts on 443; any other name, port, literal IP, metadata address (IPv4 and IPv6), or allowed name resolving to a private or metadata address is refused and recorded; every socket entry point is covered; the conftest installs it with the real resolver |
| Processes | the live suite refuses `aws sso login`, `aws sts`, `aws configure sso`, Docker, and any other git command |
| The product path | the call goes `call_capability("aws.identity.inspect")` -> `call("aws_identity_inspect")` -> the tool's `run`, never the tool or backend directly; exactly two STS calls (probe, capability) on `https://sts.us-west-2.amazonaws.com`; one credential resolution from `agent-factory-dev` only; one gateway record (`cloud_read`, `allow`, `ok`) |
| Evidence | one record with only the documented fields, no account, ARN, user ID, principal name, profile, slot, key, or token; read back as `LIVE_TESTED` for version 1 in `dev` only; staged, then atomically renamed; a stubbed run can never write the repository's file |
| Failures (each writes nothing) | authorization missing or credential-like; dirty tree or no commit; static-key variables set (named, never valued); a missing, static, or credential-process profile (in the pre-flight, and again in the backend with no credential resolved); any plan but the approved one (`default`, `internal-dev`, another profile, Region, slot, or environment); an expired, about-to-expire, expiry-less, static, or unsupported credential; AccessDenied, ExpiredToken, throttling, a service error, or a malformed answer (the gateway is never called); no SSO sign-in, through the real SSO provider, offline; a metadata connection a library swallowed; an unexpected identity (another permission set, an IAM user, root); identifying text in the logs; a capability version mismatch; an authorization for another capability version or environment; a crash mid-write; a failed check after staging |

**Mutation check, E2a (31 mutants; every failure class caught):**
- the live marker removed, and the opt-in flag removed;
- static credentials accepted (backend kind, pre-flight profile kind, pre-flight variables);
- a wrong profile accepted (plan, and a missing profile not refused);
- a wrong Region accepted (plan, and the backend's Region allowlist);
- the Tool Gateway bypassed (the tool run directly, with and without its record check);
- another capability substituted (the call, and the constant);
- the network allowlist broadened (an extra host, any `amazonaws.com` name, any port);
- a metadata endpoint permitted (connect, DNS rebinding, the default guard resolving AWS names);
- evidence written before verification, written without staging, and a crash leaving a staged file;
- the full identity written into evidence (the writer embedding the result, the full account in the summary);
- a failed verification marked `LIVE_TESTED` (summary unchecked, a network violation ignored, an unavailable
  probe ignored, log leaks ignored);
- a capability version mismatch accepted (the harness, and the registry trusting any version);
- test hooks allowed to write the repository's evidence.

Two survived at first, each because another layer caught the same cases: removing the plan's Region
check (the allowlist-Region check still refused `us-east-1`), and putting the full account in the summary
(the product's result filter still masks every 12-digit number). With the backing layer also removed, both
are caught by the harness tests, as is a full ARN placed in the summary. One mutant (test hooks writing the
repository's evidence) first wrote a stub-made record into `evidence/`. The record was deleted. The test now
points the harness at a temporary stand-in, and the mutation script fails if `evidence/` ever appears.

## Notification tests (unreleased)

| Suite | Command | What it uses |
| --- | --- | --- |
| In memory (default suite) | `tests/test_notifications.py`, `tests/test_api_notifications.py` | A real executor, recorder, and worker loop over the in-memory store; the fake provider; the API over the in-memory store, validated against `openapi.yaml` |
| PostgreSQL | `pytest -m postgres --run-postgres tests/test_postgres_notifications.py` | Real PostgreSQL; real `notifications dispatch` processes (and a claiming child, `tests/notification_child.py`) killed like a crash at each delivery boundary |
| Real stack | `pytest -m browser --run-browser --run-postgres --run-docker-live tests/test_dashboard_notifications.py` | Chrome, the API, the frontend server, PostgreSQL, workers, and a real dispatcher; the PostgreSQL scenario stops and starts this project's container |

The fake provider contacts nothing: it appends what it would have sent to a
record file, which the tests read. Results:
[dashboard-reliability.md](dashboard-reliability.md#notifications), and the
crash boundaries in [notifications.md](notifications.md#delivery-semantics-and-crash-boundaries).

## HTTP API tests (unreleased)

These drive the local backend ([backend.md](backend.md)) over ASGI with
FastAPI's `TestClient`: routing, validation, dependency injection, error
handling, and the request-ID middleware all run exactly as under uvicorn.
No handler is called as a Python function. `tests/api_support.py`'s
`ContractClient` validates **every** response (status code, media type,
body, documented headers) against the checked-in `openapi.yaml`, never
against FastAPI's generated schema, and checks that every 4xx/5xx is an
RFC 9457 problem quoting the response's `X-Request-Id`.

| File | Proves |
| --- | --- |
| `test_api_http.py` | Every product scenario, run **twice**: against the in-memory store (default suite) and against real PostgreSQL through the production connection pool (`[postgres]`). Project creation (201, identical retry replays the original, same key + different body is 409 `idempotency_key_reused` and leaves the original replayable, 8 concurrent identical requests create exactly one project, replay after an API restart with a new pool); workspace identity equals the API's project ID and names never become paths; pagination of projects, runs, milestones and the approval inbox; tampered cursors and cursors reused with other filters or operations refused; half-open time ranges and status filters; cross-project and project-scoped approvals; decisions (201, identical retry 200, conflicting 409, fingerprint mismatch 409, expired/cancelled 409, concurrent conflicting decisions settle on one); clients cannot set actor, verification, time, status or ID; recording a decision changes no run; secret, DSN and host-path canaries never appear in any response. |
| `test_api_conformance.py` | The running app against `openapi.yaml`: exactly the 11 operations plus `/healthz` and `/readyz`; per operation the operationId, path/query/header parameters, success status and response model; every error code a documented error response names is sent with that response's status (the implementation's code-to-status map against the contract); an invalid query value on every operation with query parameters is a documented 422; every API model has exactly its contract schema's fields, required set, and closed (`additionalProperties: false`) shape; generated `/docs`, `/redoc`, `/openapi.json` absent; every undocumented method on every path is 405 with no side effect; no wildcard CORS; loopback default bind and refused public bind; the API's own secrets (cursor key file, database URL, future tokens) refused by the sandbox environment; unreachable database → safe 503 and not ready; `[postgres]`: exhausted pool → 503 then recovery, unmigrated schema → not ready. |
| `test_api_unit.py` | Boundary details: Host allowlist (DNS rebinding), strict JSON bodies (duplicate keys, NaN, non-JSON media types, size), unknown keys never echoed, unhandled errors disclose nothing in responses or logs, structured access-log fields, server-generated request IDs, lifespan open/close, exact-action disclosure checks, cursor expiry and signing-key binding, log redaction. |
| `test_domain_api_storage.py` | The repository queries and idempotency storage the API needs, with one contract for both stores (`[postgres]` for the database). |
| `test_workspace_binding.py` | `workspace_for_project`: an API-created project's ID becomes the workspace identity; invalid IDs, another project's workspace, another template, occupied directories and redirected roots are refused. |

Security-critical checks were also verified by **manual mutation**: removing
the cursor filter binding, the idempotency body comparison, or the closed
request models each makes the corresponding test fail (then restored).

## PostgreSQL integration tests

These run against a real PostgreSQL; nothing is mocked. They are opt-in
(marker `postgres`). With the flag, a skip or an unavailable database
**fails**. They need the project venv with the driver, and the test database
from [database.md](database.md#local-development-setup):

```bat
set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\test-database.url
.venv\Scripts\python -m pytest -m postgres --run-postgres
```

This reports 247 passed and 3 skipped (it includes the `[postgres]`
variants of the HTTP API, action-execution, and worker tests above). The
three skipped tests are the live tests below, which also need
`--run-docker-live`: the two database-boundary tests and the database
restart. A test that belongs to two opt-in suites runs only when both flags
are given.

**Isolation.** Each test gets a fresh, randomly named schema, migrated from
scratch and dropped afterwards; if the server restarted during the test (the
live restart test), the fixture reconnects to drop it. The fixtures refuse a
database whose name lacks "test", or that is not on this machine.

| File | Proves |
| --- | --- |
| `test_domain_repositories.py` `[postgres]` | The same contract as the in-memory store, with the same results |
| `test_postgres_migrations.py` | A fresh schema builds from migrations, and the build is repeatable. The store refuses an unmigrated schema and never migrates itself. Edited migrations and newer databases are refused. A failed migration leaves nothing behind. Concurrent migrators don't interleave. CHECK vocabularies equal the Python enums. Every documented query is served by its index. |
| `test_postgres_store.py` | Every record survives closing and reconnecting. JSON metadata and UTC timestamps round-trip, whatever the session time zone. Overnight time-range queries work. Approval A never authorizes B. Duplicate, conflicting, and concurrent decisions behave correctly. A decision write that fails halfway leaves nothing. The **database itself** refuses invalid states (raw SQL). A tampered row fails validation when read. A lost connection is reported. Passwords never appear in errors. |
| `test_postgres_claims.py`, `test_worker_processes.py`, `test_worker_cli.py` | Worker coordination: see [Worker coordination tests](#worker-coordination-tests-unreleased) |
| `test_postgres_engine.py` | Graph behaviour is unchanged with PostgreSQL, and its records match the in-memory store's. Records survive **three separate Agent Factory processes** (start, approve, resume, via the CLI). The run is reconciled after the database connection was killed mid-run. A decision stored before a crash is applied by reconcile. A sync that fails halfway leaves no partial records. |

The live boundary test needs Docker *and* PostgreSQL:

```bat
.venv\Scripts\python -m pytest -m "docker_live and postgres" --run-docker-live --run-postgres
```

`test_db_sandbox_boundary_live.py`:

- **Control:** the orchestrator connects to the database.
- **Probe:** with the database URL and password in the orchestrator's
  environment, untrusted code in the real sandbox sees no database variable
  and cannot reach the database by any route.
- **End to end:** an untrusted run in Docker records its milestones in
  PostgreSQL.

## Sandbox tests (v0.2, Tier 0)

| File | Covers |
| --- | --- |
| `test_sandbox_core.py` | Types and bounds, request validation, `run_sandboxed` cleanup guarantees, `LocalProcessBackend` (trusted only, time/output limits, env scrub, monotonic deadlines), `RunTestsTool` mapping |
| `test_sandbox_policy.py` | Trust tracking, `SandboxManager` fail-closed gate (no fallback, mislabelled local backend, unhealthy/janitor/self-test), BLOCKED/resume with no retries consumed, schema v1 upgrade |
| `test_sandbox_staging.py` | Copy-in staging: junctions, hardlinks, symlinks (via `lstat`), special files, reserved names, caps, swap-after-check, malicious tar members |
| `test_sandbox_docker.py` | Golden and fuzzed `docker create` argv, forbidden-option rejection, endpoint validation, probe, subcommand allowlist (never pull/build), lifecycle, limit mapping (incl. CPU soft/hard limits), verified cleanup, janitor, self-test, executor integration, all via an in-memory fake CLI |
| `test_sandbox_default.py` | Default routing: untrusted code goes to Docker when healthy and is blocked when Docker is unavailable, unhealthy, or misconfigured, never falling back to local execution; trusted fixtures stay local and never contact Docker |
| `test_sandbox_image.py` | af-entry protocol logic (run on the host against temp dirs), the human-only image builder (fake CLI), and the image lock (missing, invalid, or stale fails closed) |

An autouse fixture in `tests/conftest.py` fails any test that launches a
`docker` executable, resolves a non-loopback name, or opens a non-loopback
socket. Tier-0 tests therefore cannot create containers, pull images, or reach
the network (not even with a DNS query).

## Live Docker smoke tests (v0.2 Stage E)

`tests/test_sandbox_live.py` (marker `docker_live`) needs a running local
Docker engine and the built sandbox image. Without the option below, these
tests are skipped with a reason. **With** it, an unavailable engine/image or
any skip is a failure:

```bash
py -3.12 -m pytest -m docker_live --run-docker-live -v
```

The live tests only validate compatibility assumptions:
- local context, image identity, `--pull never`
- uid, capabilities, read-only root, network `none`, tmpfs
- copy-in and af-entry, output capture, inspect timestamps, cleanup
- `--log-driver none` with attach, `--ulimit fsize` units

They also cover staging headroom and run untrusted runs end to end, both with
an explicit manager and through the default routing. They are not the
adversarial acceptance suite.

**Database-boundary and restart tests.** `tests/test_db_sandbox_boundary_live.py`
adds two `docker_live` tests that also need PostgreSQL (`--run-postgres`), and
`tests/test_worker_restart_live.py` adds a third, which restarts this
project's PostgreSQL container while a worker owns a run.
Without that flag they are skipped rather than failed: the rule that any
skip fails applies only when *every* suite a test belongs to was requested.
The full live run is:

```bat
.venv\Scripts\python -m pytest -m docker_live --run-docker-live --run-postgres -v
```

## Live adversarial acceptance suite (v0.2)

`tests/test_sandbox_adversarial.py` (marker `docker_adversarial`) runs
deliberately hostile programs from `tests/hostile/*.hpy`. They are not
importable on the host, and they run **only** through `SandboxManager` ->
`DockerBackend`. A guard fails the test before any hostile code is sent
unless every backend that could run untrusted code is a CONTAINER-level
security sandbox. During these tests the host may launch nothing but the
`docker` CLI.

```bash
py -3.12 -m pytest -m docker_adversarial --run-docker-adversarial -v -s
```

The command fails if:
- Docker is unavailable
- the image in Docker doesn't match `sandbox_image/image.lock.json`
- isolation is below CONTAINER
- any test skips
- any Agent Factory container remains at the end

The categories covered:
- host file read and write escape (against a random host canary)
- network escape, host env/secret theft
- wall-clock timeout, child-process survival
- memory, disk and PID exhaustion, output flooding
- malformed paths/archives
- concurrent-run isolation
- the cleanup matrix (including orchestrator interruption and janitor recovery)
- fail-closed rechecks, including the default routing with Docker unreachable
- CPU: `--cpus` quota (measured CPU/wall ratio and throttling), and the
  CPU-time limit (SIGXCPU at the soft limit; SIGKILL at the hard limit when
  SIGXCPU is ignored)

`-s` prints the `[adv]` evidence lines.

## HTTP fixture traceback closeout

The skills baseline reported 1,645 passed / 466 skipped, exit 0, with a background
traceback from `test_model_providers.py`. Its original complete log was not found
in the scoped repository test/documentation/tool log inventory; the supplied report
does not establish its original exception type. A fresh full default run before
the fix collected 2,111 tests: 1,645 passed, 466 skipped, exit 0 in 101.98 seconds,
with empty stderr. That passing run alone did not resolve the incident.

One focused reproduction ran
`tests/test_model_providers.py::test_the_adapter_enforces_its_deadline_and_its_size_limit`
with `-s -v`, then kept the process alive for four seconds after `pytest.main`
returned. The test passed in 1.59 seconds, exit 0, but complete stderr recorded
`ConnectionAbortedError: [WinError 10053]` during request handling at
`FakeMessagesApi.Handler.do_POST` -> `self.wfile.write(raw)` -> socket `sendall`.
This reproduced the defect at the reported source; it is not a recovered original
transcript or proof that the original exception type was identical.

The client disconnect is intentional: a scripted reply sleeps for three seconds
while the provider deadline is one second. The old fixture called only `shutdown`,
which stops accepting requests but does not close the listener or wait for default
daemon request handlers. The sleeping handler outlived fixture teardown, then wrote
to the aborted connection. `socketserver` printed its exception without failing
pytest. The serving thread was untracked; the reproduction proves a surviving
request handler, not a surviving accept-loop thread or subprocess. No fixture
subprocess is involved.

The fix is confined to [test_model_providers.py](../tests/test_model_providers.py).
Non-daemon request handlers are joined by `server_close`; the listener is closed,
and the named serving thread is retained and joined. The request-error hook records
exceptions, and `close()` fails the owning test for unexpected handler errors.
Only `BrokenPipeError`, `ConnectionResetError` or `ConnectionAbortedError` during
response-header/body writes for a scripted delayed response can be accepted, and
only when the test explicitly expects it. Teardown asserts the exact disconnect
count. A normal response or absent expectation cannot excuse these errors. No
blanket warning/stderr suppression, retry, skip, runtime-provider change or guard
change was introduced.

Two deterministic regressions failed against the original fixture: 2 failed,
52 deselected, exit 1 in 1.24 seconds. Its listener remained open; an injected
`RuntimeError` did not make `close()` fail. Six new regression cases now cover
closed listener/joined serving thread, unexpected handler-error propagation, the
three explicit expected disconnect types, and rejection of a normal-response
disconnect even when a delayed disconnect was expected. The real timeout test
also requires exactly one observed delayed disconnect before teardown completes.

Final focused command:
`.venv\Scripts\python -m pytest -v tests/test_model_providers.py tests/test_anthropic_compat.py`.
89 collected, 89 passed, no failures/skips/warnings or background tracebacks,
exit 0 in 30.29 seconds, with empty complete stderr. Three focused sessions were
run: reproduction, deterministic failing regressions, and final focused validation.
The post-fix complete default command was `.venv\Scripts\python -m pytest`:
2,117 collected, 1,651 passed, 466 skipped, no failed tests, exit 0 in 105.94 seconds.
No warning summary or background traceback appeared, and complete stderr was empty.
The skips cover unrequested opt-in suites plus one symlink-privilege skip. They are
not live verification passes. The six extra passes are the new regression cases.

Every run captures complete stdout and stderr separately in an isolated
`agent-factory-skills-closeout-*` system temporary directory, plus result JSON
containing the exact command and actual pytest exit status. The reproduction exits
with `pytest.main`'s result after the observation window. Other runs use
`subprocess.run([project_venv_python, "-m", "pytest", ...])` with binary log files;
no shell pipeline can replace pytest's exit status. Raw logs remain outside Git.
Pytest workspace isolation is unchanged, and these sessions ran sequentially.

No external service, real model, credential, Docker, PostgreSQL or browser was used;
network traffic was limited to temporary loopback fixtures. Live, database, browser,
Docker/adversarial and stress suites were not run. This changes a local test fixture,
not application features, security policy or test-runner guards; it does not claim
opt-in suites passed. Skills loading has no bearing on the HTTP defect.

## Complete Compose reviewer check

The [Docker milestone](zoomcamp/DOCKER_STARTUP.md) adds runtime guard regression
tests and a CI lane that builds and starts the actual Compose stack, verifies
authentication and a trusted-fixture approval, then checks retained execution
history after stop/start. This check operates only on the newly created Compose
project. Existing database/browser suites remain separate and use their guarded
test databases; no live model, cloud provider or hostile-code suite is enabled.
