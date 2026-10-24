# Offline supervised coding demonstration

**Offline validation complete for the closed fixtures.** The opt-in demonstration
completed the actual repair scenario, all 91 focused tests passed, and an
execution-evidence mutation was killed by the intended assertion. The full default
suite passed with **1,832 passed and 466 skipped**. See the
[validation record](offline-supervised-validation.md) for full regression results,
source hashes, the acceptance matrix and the preserved earlier filesystem failures.

## Scope and command

This demonstration supervises a closed, maintainer-authored `FakeCodexAdapter`.
It does not connect Codex, Muse, another model provider, or a live service.
It selects its own `ProjectTemplate` explicitly and leaves the standard graph,
ordinary demo commands, and provider defaults unchanged.

From the repository root, the implemented standalone entry point is:

```powershell
.venv/Scripts/python.exe -m agent_factory.supervised_demo --scenario repair
```

**Standalone execution is validated for the closed repair fixture.** The command
exited 0 with two attempts and one repair; retained checkpoints, evidence bindings
and the 50-entry audit chain were independently re-read and verified. The sandbox
still denied the temporary fixture round trip during preflight. An explicitly approved
outside-sandbox preflight and validation runs succeeded without changing temporary
roots, filesystem permissions or application guards.

The entry point calls `tempfile.mkdtemp` using the existing process temporary
directory, then creates a new `fixture` child outside the repository. It retains
that disposable workspace and prints a JSON report. Neither the repository nor
the fixture receives a commit; fixture creation uses `git=False`. An existing
root or a root inside the repository is refused. The test harness separately
refuses repository-local temporary roots. Neither guard may be bypassed to
obtain a result. Existing sandbox and configuration settings are unchanged.

## How the components work together

The flow is fixed task -> worker dispatch -> separate change application ->
independent tests and diff -> scripted review -> bounded repair -> verified
completion or a human pause.

The following locators refer to
[supervised_demo.py](../agent_factory/supervised_demo.py); they identify actual
calls and checks, rather than imports or roadmap entries.

| Responsibility | Implementation locator |
| --- | --- |
| Fixed requirements, five acceptance checks, edit/protection lists, test command and limits | `task_package`, line 129; `create_demo`, line 589 writes the package outside `project/` |
| Pure scripted worker output | `FakeCodexAdapter`, line 98; `DemoAgent.run`, line 515 calls the existing `Agent.ask`/Model Gateway path |
| Durable dispatch identity and saved proposal | `SupervisorRecords.operation`, line 353; `begin` writes `started` before the provider call, then `proposal` saves the validated result |
| Separate change application | `DemoAgent.run`, lines 528 and 530 call `delete_file` and `write_file` through the Tool Gateway |
| Independent test execution | `DemoAgent.run`, line 534 calls `run_tests`; `open_demo`, line 579 supplies `SandboxManager.default().run` to `CoreToolPack` |
| Complete bounded snapshot and additions/deletions diff | `snapshot`, line 143; `candidate_diff`, line 182 |
| Evidence binding and completion checks | `SupervisorRecords.verified`, line 300; `check_test_result`, line 324; `completion_report`, line 605 |
| Existing orchestration, checkpoints, audit and approval handling | `open_demo`, line 581 constructs the existing `Executor` with the demo graph, agents, policy and registry |
| One shared repair budget | `demo_graph`, line 544 sets `developer.max_visits=3`, including the initial attempt |

The coding role cannot apply files itself. Supervisor operations are restricted
by role; the application role receives only the saved proposal. The existing
executor validates state updates, owns visit counts and checkpoints, and handles
approval decisions. Approved deletion uses the existing action guard and ledger.

The supervisor checks exact canonical edit paths and exact known fixture bytes
before local test execution. Unknown adapters, scenarios and arbitrary code are
refused. Acceptance tests, task criteria, fixture security settings and supervisor
controls are protected. The fixture's `.codex/config.toml` is a synthetic protected
file in the disposable project; it is not the repository or global configuration.

The task, dispatch, candidate, full bounded diff, test result and review carry
matching digests. Test evidence requires the fixed command, exactly five tests,
consistent exit/verdict fields, a completed sandbox execution and verified cleanup.
Missing, stale or mismatched evidence blocks completion. The worker's summary
and a reviewer's approval alone cannot complete the run.

## Worked repair example and stopping behaviour

The fresh standalone report and retained evidence demonstrate this sequence:

1. Attempt 1 proposes the known calculator fixture with `subtract(5, 3)` returning
   8. The application step writes it; the independent runner executes five real
   fixture tests. The result is four passes and one failure,
   `test_subtract`. No review is recorded for that failed candidate.
2. The failure routes back to the developer with structured feedback. Attempt 2
   proposes the known corrected fixture. All five fixture tests pass.
3. A separate scripted reviewer approves the bound passing evidence. The finalizer
   rechecks the candidate, test and review bindings before completion. Both
   attempts remain recorded; the report shows two attempts and one repair.

The test path invokes real `unittest` subprocesses through `SandboxManager`'s
local backend for trusted fixtures. That local backend is not a security sandbox.
Worker timeout/failure/interruption scenarios and the reviewer's decisions are
simulated; they are not real provider failures or independent human review. The
review verdict is selected by scenario and does not assess arbitrary code quality.
An additional observed run of the same entry point passed requests unchanged to
the original fake adapter and confirmed that the second developer request received
the first attempt's failure list and bounded output. That observation is separate
from the ordinary standalone CLI report, which does not contain model requests.

| Scenario | Source-defined outcome |
| --- | --- |
| `success`, `repair` | Verified completion, respectively zero or one repair; normal CLI exit 0 |
| `test_exhaust`, `review_exhaust`, `mixed_exhaust` | At most three worker dispatches total, then `exhausted`; normal CLI exit 2 |
| `approval` | Pause before deleting `legacy_calc.py`, before candidate writes; normal CLI exit 2 |
| `worker_failure`, `timeout`, `malformed` | Simulated worker failure/timeout/invalid output; no candidate application or completion |
| `uncertain` | Simulated interruption leaves the durable `started` record; CLI exit 2; resume blocks without redispatch |

The shared developer visit limit covers test-only, review-only and mixed repairs.
Checkpoint/resume preserves it. A saved `proposed` result is reused if interrupted
before the developer checkpoint; a `started` dispatch without a known result
blocks rather than being sent again. Known idempotent file writes can resume
application without consuming another coding attempt.

For an already retained workspace, the implemented resume entry point is:

```powershell
.venv/Scripts/python.exe -m agent_factory.supervised_demo --resume <retained-workspace>
```

This calls `Executor.advance("demo")`; it never creates an approval. Without a
decision, an approval scenario remains paused and does not redispatch its worker.
The existing local operator API is `Executor.decide_approval`, followed by resume;
the demo CLI deliberately provides no automatic approval option. Focused tests
supply explicit fixture-owner decisions and check absent, rejected, expired and
mismatched decisions. The local API accepts an unauthenticated approver label:
its exact-action and expiry checks do not authenticate the owner's identity.
This is not a new authenticated production approval UI.

## Retained evidence

Relative to the printed fixture workspace, the relevant artifacts are:

```text
project/                                  bounded fixture content
.agent_factory/workspace.json             workspace marker
.agent_factory/supervised/task.json        fixed task, digest and baseline
.agent_factory/supervised/<run_id>.json    dispatches, proposals, tests and reviews
.agent_factory/runs/<run_id>/checkpoint.json
.agent_factory/runs/<run_id>/execution_log.jsonl
.agent_factory/runs/<run_id>/executions.json  approved-action ledger, when used
```

The standalone run ID is `demo`. The completion report is printed as JSON to
stdout; it is not a separately saved report file. Artifacts are retained rather
than automatically deleted. A failed creation can leave only partial artifacts.

## Validation and limits

[test_supervised_demo.py](../tests/test_supervised_demo.py) covers completion,
repair/exhaustion, restart, approval pauses, missing and inconsistent evidence,
zero tests, protected controls/path aliases, forged state updates, simulated
worker failures, and rejection of real or unknown adapters.

Fresh focused result: `.venv/Scripts/python.exe -B -m pytest tests/test_supervised_demo.py
-q -p no:cacheprovider --tb=short --maxfail=3`, exit 0, **91 passed in 10.21 s**,
with no failures, skips, errors or warnings reported. The selected execution-ID
test passed before and after a process-local mutation. Removing only its semantic
predicate made the test fail with `DID NOT RAISE EvidenceError`; no source or
bytecode was modified. The [validation record](offline-supervised-validation.md)
contains full default-suite results and skip reasons. The older 1,651 passed /
466 skipped run and earlier inconclusive/blocked attempts remain historical.

Trusted scripted fixtures and scripted reviews demonstrate supervision mechanics.
They do not solve adversarial test-verdict forgery, authenticate execution evidence,
or establish safety for arbitrary generated code. Hashes bind data but are not
signatures. Host tampering and concurrent host writers are not covered guarantees.
Real Codex compatibility, authentication, usage/cost controls and approval bridging
remain future verification work, with separate authorization required.

Active MXC, effective runtime permissions, and the complete effective approval-rule
set remain **NOT VERIFIED**. No unattended real execution readiness is claimed.
The demonstrated scope is offline supervision of the closed fixtures only.
