# Public source and CI publication

The owner authorized this public repository and hosted CI on 2026-10-23
(America/Denver; 2026-10-24 UTC). It starts with a standalone snapshot of the
validated Agent Factory working tree. Historical commit IDs, branch names and
release tags in older evidence refer to the original authoring repository;
that history is not imported here.

The runtime application, full test suites, dependency pins, OpenAPI contract,
reviewer launcher and CI workflow are retained. The public copy omits private
local state, credentials, unrelated account evidence and a separate business's
commerce skill catalog. AGENTS.md and SKILLS.md describe the application scope;
application authorization and execution boundaries are unchanged. Selected
historical documents omit private host inventory while preserving test outcomes.

Local validation is recorded in [Day 1](DAY1_VALIDATION.md) and
[Day 2](DAY2_VALIDATION.md). Their publication status describes those earlier
checks. This record supplies the subsequent hosted evidence.

## Verified first hosted run

- Public repository: [BryanHunt290/agent-factory-zoomcamp](https://github.com/BryanHunt290/agent-factory-zoomcamp), default branch `main`.
- Tested source: [`40cea25324288e1e6ef6b2e8180dc3f85e797b63`](https://github.com/BryanHunt290/agent-factory-zoomcamp/commit/40cea25324288e1e6ef6b2e8180dc3f85e797b63).
- Workflow: [Reviewer checks, run 40065197804](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions/runs/40065197804), triggered by the initial push.
- Result: **all three jobs succeeded**. Completed 2026-10-24 at 05:21:57 UTC.
- Environment: GitHub-hosted Ubuntu 24.04, Python 3.12.10, Node 24.18.0,
  the pinned PostgreSQL 17 image and installed Chrome 155.0.8059.39.

| Check | Observed result |
| --- | --- |
| Default Python/API suite | 1,887 passed, 468 skipped, one warning; 130.31 seconds |
| Frontend | Contract generation check, TypeScript, 57 tests in five files, and production build passed |
| PostgreSQL integration | 352 passed, 2,003 deselected, one warning; 193.72 seconds |
| Real-browser workflows and recovery | 42 passed, 2,313 deselected, one warning; 445.14 seconds |
| Dependency consistency | `pip check` passed in both Python jobs |

The Python skips are the default suite's opt-in tiers and platform-specific cases.
Database and browser checks were explicitly requested and passed without skips.
The reported warning is the existing Starlette TestClient/httpx deprecation.
Deliberate local-container restart tests are excluded from hosted CI because its
disposable service container is not the guarded local development container;
the separate Day 1 validation records those recovery checks. Stress, hostile
sandbox, live model and live cloud tiers are not covered by this hosted run.

JUnit reports and the frontend candidate bundle were uploaded successfully with
seven-day artifact retention. The initial hosted run required no application or workflow repair. The documentation follow-up adds clone instructions, repairs links
to excluded material, records these results, and prepares the
[Day 3 handoff](DAY3_HANDOFF.md). Later commits run the same workflow; check the
[Actions page](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions)
for a completed successful run on the exact final submission SHA.

## Follow-up: approval-expiry race regression

The documentation commit `3d67e14` triggered
[run 40066138018](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions/runs/40066138018).
Python and frontend passed, but PostgreSQL reported **one failed, 351 passed**:
`test_c6_a_decision_racing_the_expiry_never_runs_the_action_after_it[-0.05]`.
The browser step did not run because the database step failed. This failed run
is retained as evidence; the earlier green run does not establish later results.

The test assumed that zero effects always means the approval was refused during
decision consumption. It omitted a valid third outcome: a decision is consumed
before expiry, then the execution ledger refuses dispatch at or after expiry.
The persisted execution is `LAPSED` with `authorization_expired`, zero attempts
and no effect. The same boundary already had an in-memory regression.

[The PostgreSQL regression](../../tests/test_postgres_decisions.py) now verifies
all three outcomes explicitly. Successful dispatch must still begin strictly
before expiry and produce exactly one effect. The safe-lapse outcome requires
zero attempt rows, zero dispatch events and zero effects; refusal during
consumption requires an expired decision and no execution record. A new case
uses a movable application clock to force dispatch exactly at expiry after
approval consumption, while PostgreSQL's lease clock remains real.

Local validation reproduced the old assertion failure deterministically, then
passed all **25 decision race/crash tests**. A process-local mutation that allowed
expired authorization to dispatch failed the new zero-effect assertion as
intended. No source mutation persisted; the final focused regression passed
again with the guard restored. Application code, workflow selectors and safety
guards are unchanged. The test correction is mirrored in the original authoring
checkout. The corrective push completed the hosted workflow successfully, as
recorded below.

## Verified corrective hosted run

- Tested source: [`e571dc3a8c965cdc4d24cb5d83bdc5bdf579ffd7`](https://github.com/BryanHunt290/agent-factory-zoomcamp/commit/e571dc3a8c965cdc4d24cb5d83bdc5bdf579ffd7).
- Workflow: [Reviewer checks, run 40066705063](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions/runs/40066705063).
- Result: **all three jobs succeeded**, completing by 2026-10-24 at 05:43:42 UTC.

| Check | Observed result |
| --- | --- |
| Default Python/API suite | 1,887 passed, 469 skipped, one warning; 106.48 seconds |
| Frontend | Contract generation check, TypeScript, 57 tests in five files, and production build passed |
| PostgreSQL integration | 353 passed, 2,003 deselected, one warning; 143.89 seconds |
| Real-browser workflows and recovery | 42 passed, 2,314 deselected, one warning; 424.21 seconds |

The additional PostgreSQL case is the deterministic expiry-boundary regression
above. Database and browser selections passed without skips. The default-suite
skips, Starlette TestClient/httpx warning and excluded live tiers retain the scope
described for the first run. This verifies the corrective source commit, not a
later Day 3 evidence commit or the final submitted SHA. Any later submission
commit still needs its own completed successful CI result.

## Remaining submission boundaries

A public source repository and passing CI do not establish a deployed application
or production identity configuration. Full application containerization, public
hosting, CD and the operational monitoring gaps remain explicit in the
[rubric mapping](RUBRIC_MAPPING.md). Final demonstration capture, course cutoff
verification, submission confirmation and three peer reviews remain on the
[submission checklist](SUBMISSION_CHECKLIST.md).
