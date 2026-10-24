# Offline supervised milestone: resumption validation record

## Current outcome

**COMPLETE for the closed-fixture offline validation scope.** The owner selected
finishing validation of the offline supervised coding demo. Fresh focused tests,
the intended evidence-check mutation, the actual standalone repair command,
retained-evidence verification and the full default regression suite succeeded.
No implementation or test changes were needed. This does not validate a real
Codex adapter, arbitrary generated code, live services or unattended execution.

Validation checkout: `feature/opportunity-review-u1` at
`03e95d99a81e05f3730a579d127f4b45a0c05b88`. The two source/test SHA-256 values
in the historical record below matched this validation resumption's baseline and
final files. A later test-file whitespace cleanup is recorded under local commit
scope below. The branch name differs from the earlier record because validation resumed
in the existing checkout; no branch was switched or created during validation.

## Fresh execution and access results

The sandbox preflight strictly resolved the normal temporary root successfully,
then failed the temporary fixture-marker round trip with `PermissionError`,
errno 13 / WinError 5, exit 1. An explicitly approved outside-sandbox rerun passed
all probe steps, exit 0. The validation commands below also used approved
outside-sandbox execution. This establishes that those commands ran with that
access; it does not establish a repair to the original sandbox denial.

No temporary-root/environment override, ACL change, installation, alternate
application backend, network/Docker guard change or live-suite opt-in was used.
Test/demo workspaces remained outside the repository. Only validation receipts
and scripts were saved in the repository's ignored `.local/` evidence directory.

| Validation | Actual result |
| --- | --- |
| Focused: `.venv/Scripts/python.exe -B -m pytest tests/test_supervised_demo.py -q -p no:cacheprovider --tb=short --maxfail=3` | Exit 0; **91 passed in 10.21 s**, no skips |
| Execution-ID mutation baseline | Exit 0; **1 passed** |
| Process-local execution-ID mutation | Expected pytest exit 1; **1 failed**, specifically `DID NOT RAISE EvidenceError` at the intended call-phase assertion |
| Restored execution-ID baseline | Exit 0; **1 passed**; source bytes unchanged |
| Standalone: `.venv/Scripts/python.exe -B -m agent_factory.supervised_demo --scenario repair` | Exit 0; completed, **2 attempts / 1 repair**; stderr empty |
| Retained standalone report, checkpoint and audit verification | Passed; final bindings revalidated, **50 audit entries** verified, no fixture Git repository or commit |
| Additional observed repair entry-point run | Exit 0; original fake adapter received first-attempt test failure in the second request; **50 audit entries** verified |
| Default: `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3 -rs` | Exit 0; **1,832 passed, 466 skipped in 132.10 s**; no failures, errors or warnings reported |

The default result was measured in the combined working tree, including the
unrelated Upwork test additions. It is not a measured clean-checkout test count
for the isolated demo commit. The 91 focused tests cover the demo itself; the
later commit-time cleanup changes only a trailing blank line in the test file.

The default suite's skips comprise **465 unrequested opt-in cases** and **one
Windows symlink-privilege case** (`tests/test_sandbox_staging.py:105`). PostgreSQL,
Docker live/adversarial, browser, stress, real model and AWS suites were not
requested. These are absent live-validation evidence, not live passes. No real
model, AWS, marketplace, notification or deployment action was activated.

The targeted mutation removed only `or not execution.get("execution_id")`
from an in-memory copy of `SupervisorRecords.check_test_result`. The test updates
all integrity digests, isolating the semantic execution-ID requirement. The
function descriptor was restored in a `finally` block; baseline, mutant and
restored checks ran in separate processes with `-B`. No on-disk module or cached
bytecode was edited. The local runner rejected setup/collection failures and
required the intended assertion failure before recording the mutation as killed.

## Fresh acceptance evidence

| Requirement | Result and scope |
| --- | --- |
| Fixed task, protected tests/controls and exact fixture bytes | PASS: focused suite checks modified criteria, paths, content, adapters and state; source unchanged |
| Existing Executor, Model Gateway, Tool Gateway and SandboxManager | PASS: actual focused and standalone execution through the existing paths; trusted local fixtures only |
| Durable dispatch distinct from file application; no redispatch on resume | PASS: focused interruption, resume and approval tests |
| One initial attempt plus at most two repairs, including resume | PASS: test/review/mixed exhaustion and checkpoint tests |
| Unknown worker outcome stops repetition | PASS: uncertain-dispatch tests |
| Exact approval pause, rejection, expiry and mismatched ID refusal | PASS: focused cases; fixture approver labels do not authenticate a human |
| Real failed candidate followed by passing repair | PASS: standalone first attempt ran 5 tests with only `test_subtract` failing; second ran 5 and passed |
| Failure feedback reaches repair request | PASS: separate pass-through observer compared the second request's feedback with the first execution's failure list and bounded output; ordinary CLI JSON alone does not prove this |
| Bound tests, complete diff, scripted review and completion | PASS: standalone checkpoint/report re-read, differing dispatch/candidate identities, no review on failed attempt, matching approved review and final completion on second attempt |
| Missing/stale/mismatched/zero-test evidence refused | PASS: focused suite plus execution-ID mutation |
| Simulated worker failures and timeouts | PASS: focused cases; no real provider-failure evidence claimed |
| Focused, standalone, mutation and default validation | PASS: exact results above |

Coverage gaps below remain limitations, not demonstrated implementation defects:
approval expiring after decision but before dispatch in this demo; parameter
mismatch beyond request ID; interruption during partial application or after
approved deletion; and switching between recognized fixture bytes during testing.
The existing core guards were not changed. Production authentication, hostile-host
authenticity and adversarial verdict integrity remain outside this demonstration.

## Current evidence retention and preservation

Local receipts are under `.local/offline-supervised-validation-20261020-b1465f3a/`:
`baseline.json`, sandbox/approved preflight receipts, `focused.txt`, mutation
baseline/mutant/restored outputs and `mutation-receipt.json`, `standalone-report.json`,
empty `standalone-stderr.txt`, `observed-report.json`, `demo-verification.json`,
`default-suite.txt`, validation scripts and `preservation.json`. Standalone fixture
workspaces are retained externally at the private locations in the local reports.
Raw paths and model-request bodies are not copied into this documentation.

The validation baseline covered 385 tracked/nonignored files and all local
branch/tag targets. At validation closeout, comparison permitted changes only to this record,
`docs/offline-supervised-demo.md`, `README.md`, `docs/testing.md` and
`docs/ai-workflow.md`; all other baseline files and all branch/tag targets matched.
The working tree then included the pre-existing uncommitted demo and unrelated
Upwork work. No staging, commit, stash, reset, push or remote operation occurred
during validation.
An auxiliary history-preservation check initially failed because its helper
assumed CRLF line endings. Comparing the original LF bytes passed against the
baseline SHA-256; the historical body itself was unchanged.

## Local commit scope

The owner subsequently authorized a local commit of the validated demonstration
and its documentation, with unrelated Upwork changes excluded and no push.
The existing `feature/offline-supervised-demo` branch was selected from the same
validated base revision. The commit contains only `agent_factory/supervised_demo.py`,
`tests/test_supervised_demo.py`, this record, `docs/offline-supervised-demo.md`,
`README.md`, `docs/testing.md` and `docs/ai-workflow.md`. Application source matches
the validation hash. Staging exposed an extra blank line at the test file's EOF;
it was removed, with equality of the Python AST checked before and after. No test
assertion or application behavior changed. The committed test SHA-256 is
`40af9778e0146ba5f548574a282d65829bd4a432b927b7d4df5e12104aa8cd25`.
Commit-time documentation wording was also adjusted.
Private receipts, fixture workspaces and unrelated additions remain outside this
commit. All imported dependencies are already tracked independently of Upwork.

After the EOF cleanup, the full default command above was rerun in the combined
checkout: **1,832 passed, 466 skipped in 129.04 s**, exit 0, with no failures,
errors or warnings reported. The skips remained 465 unrequested opt-in cases and
one Windows symlink-privilege case. The staged whitespace check passed after the
cleanup. Local commit-preparation receipts are retained separately under
`.local/offline-supervised-commit-20261021/`.

## Historical record boundary

Everything below is the preserved **2026-10-13 blocked resumption record**.
Its references to "current", "this resumption" and future authorization describe
that earlier session, not the successful validation above. Earlier reports and
local evidence were retained; fresh execution supersedes their open validation
status without rewriting what happened.

## Outcome

**PARTIAL: source and documentation review completed; execution validation BLOCKED.**
The owner authorized a bounded resumption on the existing branch. The single
current-session preflight failed before creating scratch data. Focused tests,
mutation, standalone execution and the default suite were not launched.
The offline milestone is not complete.

No implementation defect was demonstrated by source review within the closed,
trusted-fixture scope. No source or test file was edited in this resumption.
Source inspection does not establish fresh runtime behavior.

## Reviewed code identity

Branch: `feature/offline-supervised-demo`.
Starting Git revision: `03e95d99a81e05f3730a579d127f4b45a0c05b88`.
The source and test files were untracked, so the revision alone does not identify
them. Starting SHA-256 values:

| File | SHA-256 |
| --- | --- |
| `agent_factory/supervised_demo.py` | `4650BCBE03D080D1BE5848336EC8B3FE49462DFCC12DBCE8FFD2720FDDA81810` |
| `tests/test_supervised_demo.py` | `097EAAD6B9837ED94637DA2065138EDF2CDEFAA7F0272003B54E8CF8B61B675C` |

The existing untracked configuration was hashed locally to verify preservation;
its contents and hash are not part of portfolio documentation.

## One preflight and stopping decision

The probe selected its current process `TEMP`, required matching `TMP` and
unset `TMPDIR`, under the session-declared temporary-write permission.
The package-specific value varied between tool invocations; no earlier package
directory was reused. Effective filesystem behavior still required verification.

The full submitted block is retained locally. Its interpreter invocation and
immediate exit capture were:

```powershell
$preflight | & '.venv/Scripts/python.exe' -I -S -B -
$preflightExit = $LASTEXITCODE
Write-Output "Preflight Python exit code: $preflightExit"
exit $preflightExit
```

Python version: **3.12.10**. Standard library only; no environment override.

| Step | Actual result |
| --- | --- |
| Select current absolute temporary root | PASS |
| Reject symlink/junction ancestors | PASS: no linked ancestor reported |
| Strictly resolve the existing selected temporary root | FAIL: PermissionError, errno 13, WinError 5 |
| Ordinary nested directory / marker round trip | NOT RUN |
| Normal tempfile.mkdtemp / fixture child / marker round trip | NOT RUN |

Failed operation: `Path(current_TEMP).resolve(strict=True)`.
Exception: `[WinError 5] Access is denied` for that selected temporary root.
Python exit **1**; PowerShell exit **1**. No approval required.
No scratch directory or marker was created. The local raw receipt retains the
exact private path and exception; neither belongs on GitHub.

This does not identify the backend or cause. The previous project-path
inside/outside comparison was not repeated. No alternate root, escalation,
outside-sandbox retry, ACL change, installation or repair was attempted.

Unchanged guards: `tests/conftest.py:213` resolves explicit basetemp and
PYTEST_DEBUG_TEMPROOT and rejects repository-local roots;
`supervised_demo.py:589` requires a new external fixture root;
`:639` uses normal tempfile.mkdtemp behavior. An external path alone is
insufficient: resolution, containment, link checks and private-directory
operations must all work.

## Execution results

These are requested commands, **not commands executed in this resumption**.
No basetemp or temporary-environment override was supplied.

| Validation | Command / target | Result |
| --- | --- | --- |
| Focused | `.venv/Scripts/python.exe -m pytest tests/test_supervised_demo.py` | BLOCKED; exit/counts N/A |
| Mutation baseline | `.venv/Scripts/python.exe -m pytest 'tests/test_supervised_demo.py::test_semantically_invalid_evidence_is_refused_even_with_updated_digests[execution_id]'` | BLOCKED; exit/counts N/A |
| Process-local mutation | Disable only execution-ID predicate in memory, then invoke the selected test | NOT RUN; INCONCLUSIVE |
| Standalone | `.venv/Scripts/python.exe -m agent_factory.supervised_demo --scenario repair` | BLOCKED; exit N/A |
| Default non-live suite | `.venv/Scripts/python.exe -m pytest` | BLOCKED; exit/counts N/A |

There are no new passed/failed/skipped/error counts. No pytest warning or
background traceback was produced because these processes were not launched.
The preflight PermissionError is the current execution failure. No command
requested escalation.

Historical developer reports remain separate: focused suite 91 passed; later
standalone fixture-creation failure; mutation inconclusive; older default
1,651 passed / 466 skipped predates the milestone changes.

## Mutation review

The current target exists at `tests/test_supervised_demo.py:301`. It removes
the execution ID at line 311, recomputes evidence/review/checkpoint digests,
and requires EvidenceError at line 334. The semantic guard is
`SupervisorRecords.check_test_result`, `supervised_demo.py:328`.

A future authorized mutation should remove only
`or not execution.get("execution_id")` from an in-memory copy of that function,
preserve other checks, and restore it in finally. Use a separate process with
bytecode writes disabled; do not edit the on-disk guard. A passing unmodified
baseline is required first.

A conclusive kill is the intended raises assertion failing with **DID NOT RAISE
EvidenceError**. Syntax, import, collection, setup or unrelated failures are
inconclusive. No mutation execution or conclusive result occurred here.

## Acceptance matrix

PASS means the explicitly named source/documentation inspection passed.
Behavioral requirements remain BLOCKED until fresh runtime evidence exists.
Source/test locators below refer to supervised_demo.py/test_supervised_demo.py
unless another file is named.

| Requirement | Status | Evidence or missing gate |
| --- | --- | --- |
| Preserve repository, branch, prior work and controls | PASS | Initial Git checks exit 0; no ref, staging, source or test changes |
| Identify uncommitted code | PASS | Revision plus hashes above |
| Reuse existing engine, gateways, workspace, checkpoints, audit and sandbox routing | PASS | Actual calls at source :571-586; existing Executor integration reviewed |
| Explicit opt-in; no standard-default change | PASS | Separate module entry point :630; no standard factory/graph edits |
| Fake adapter and closed maintainer fixtures only | PASS | Source :98, :258, :389, :571; dynamic checks still BLOCKED |
| Fixed criteria and command outside worker authority | BLOCKED | Package :129, role policy :557; tests :469, :481 |
| Separate durable dispatch and application | BLOCKED | Source :367, :389, :523; test :152 |
| No duplicate dispatch on approval resume | BLOCKED | Source :372; tests :152, :176 |
| One initial attempt plus at most two repairs total; no fourth dispatch | BLOCKED | Source :546; tests :122, :135 |
| Repair budget survives checkpoint/resume | BLOCKED | Existing Executor visit checkpoints; test :135 |
| Uncertain worker outcome stops repetition | BLOCKED | Source :374; test :254 |
| Exact valid approval, pause, no auto-acceptance | BLOCKED | Existing ToolGateway/ActionGuard; tests :176, :208, :218, :231 |
| Worker cannot alter tests, criteria, security or supervisor controls | BLOCKED | Source :199, :258; tests :396, :408, :435 |
| Independent tests and complete diff match baseline/candidate | BLOCKED | Source :300, :416; test :64 |
| Matching scripted review and finalizer; no worker self-certification | BLOCKED | Source :338, :441, :605; test :453 |
| Missing/stale/mismatched/zero-test evidence refused | BLOCKED | Source :300, :324; tests :272, :301, :361 |
| Timeouts and worker failures handled safely | BLOCKED | Source :111; tests :239, :378 |
| Successful completion with supporting evidence | BLOCKED | Test :64 exists; no fresh execution |
| Actual failing candidate, repair feedback, passing repair and matching review | BLOCKED | Test :103; feedback forwarding :515; standalone receipt absent |
| Fresh focused validation | BLOCKED | Preflight failed |
| Evidence-validation mutation | NOT RUN | Historical result remains INCONCLUSIVE |
| Actual standalone demonstration | BLOCKED | No substitution with a pytest scenario |
| Post-change full default suite | BLOCKED | Focused prerequisite unavailable |
| Original milestone guide and documentation review | PASS | Guide, README, testing and AI-workflow documentation reviewed |
| Real Codex, Muse, iPhone, cloud and unattended execution | NOT RUN | Future work outside this authorization |

## Reuse evidence and safety limitations

The tester calls the existing run_tests tool (`supervised_demo.py:534`);
open_demo supplies SandboxManager.default().run to CoreToolPack (`:579`).
`tools/testing.py:108` builds the execution request. The existing Executor
constructs ToolGateway/ModelGateway (`engine/executor.py:665`, `:680`),
uses CheckpointStore (`:143`) and existing audit paths (`:656`,
`providers/gateway.py:155`). Workspace protections and action approval remain
in their existing components.

Trusted fixtures route to the local backend (`sandbox/manager.py:64`).
That backend is not a security sandbox for arbitrary generated code.
The local approval API accepts an unauthenticated approver label
(`engine/executor.py:359`, `docs/sandbox.md:90`): exact-action/expiry checks
are not authenticated owner identity. Scripted review is scenario-driven;
hashes provide consistency checks, not authenticity against a hostile host.

Additional focused coverage worth adding after execution becomes available:
approved decisions expiring before dispatch; action mismatch beyond request ID;
interruption during partial application or after approved deletion; and changing
to different known fixture bytes during testing. These are coverage gaps, not
demonstrated implementation defects.

The default harness excludes Docker live/adversarial, PostgreSQL, browser,
stress, model-live and AWS-live suites unless opted in
(`tests/conftest.py:158`, `:255`). None was enabled. Real isolation,
database process death/fencing, authenticated UI decisions, provider
compatibility and adversarial verdict integrity lack fresh evidence here.

Active MXC, effective runtime permissions and the complete applicable approval
rules remain **NOT VERIFIED**.

## Local retention and next action

Local-only records are in
`.local/offline-supervised-resumption-20261013-7fdac9e2/`:
baseline.json, preflight.ps1, preflight-output.txt, preflight-receipt.json and
README.md. These were saved after the failure from the tool receipt; they are
record files, not a test scratch workspace. No previous evidence was deleted.
Final verification results and hashes are retained alongside them.

The smallest next action requires separate authorization: obtain a supported
correction for the current sandbox's strict-resolution denial, then authorize
one bounded preflight in that corrected session before further validation.
No broader access, backend switch, alternative root or weakened guard is proposed.
No implementation-side workaround was established by this review.
