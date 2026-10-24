# Final submission package

**Status: demo, screenshots and restart evidence prepared; course submission not performed.**
The owner confirmed the certificate name as Bryan Hunt. Use the exact SHA and
matching complete CI run in the [verified submission release](https://github.com/BryanHunt290/agent-factory-zoomcamp/releases/tag/zoomcamp-submission-2026-10-24)
and local `FINAL_SUBMISSION_READY.md` receipt. The release is published only after
CI passes; if it is absent, final selection is still pending. This source file
does not attempt to embed its own commit hash.

## Verified deadlines and requirements

| Requirement | Official time (UTC) | America/Denver |
| --- | --- | --- |
| Project 1 submission | October 26, 2026, 23:00 UTC | October 26, 5:00 p.m. MDT |
| Three mandatory peer reviews | November 2, 2026, 23:00 UTC | November 2, 4:00 p.m. MST |

The local offset changes between these deadlines when daylight saving time ends.
Sources: [Project 1 course page](https://courses.datatalks.club/ai-dev-tools-2026/project/project1)
and [course calendar](https://courses.datatalks.club/ai-dev-tools-2026/calendar.ics).
The [current official rubric](https://github.com/DataTalksClub/ai-dev-tools-zoomcamp/blob/main/project/README.md)
has 12 criteria, 24 available points and a 10-point passing threshold; this is
not a self-awarded score or a guarantee of acceptance. See the
[current evidence mapping](RUBRIC_MAPPING.md).

## Values for the course form

| Field | Value to submit |
| --- | --- |
| Public repository URL — required | https://github.com/BryanHunt290/agent-factory-zoomcamp |
| Exact commit — required | Copy the full 40-character SHA from the verified submission release / local ready receipt; never submit a moving branch name |
| Certificate name — required | **Bryan Hunt** — exact spelling confirmed by the owner |
| Hours spent — optional | Not provided; leave blank unless the owner supplies an estimate |
| Learning-in-public evidence — optional | Not provided; include only genuine owner-approved links |
| FAQ contribution evidence — optional | Not provided; include only genuine contribution links |

Copy the evaluated commit's exact SHA rather than a moving branch name. At final
handoff, the local `FINAL_SUBMISSION_READY.md` receipt will record that SHA, its
completed green CI run and the form values; it is prepared only after verification.
This checked-in package does not embed its own future commit hash. Do not use an
earlier commit simply because its checks passed. This is a preparation aid, not
an automated form submission or authorization to invent missing values.

## Reviewer summary

Agent Factory coordinates planner, developer, tester and reviewer agents through
a durable software-development workflow. A React dashboard and authenticated
FastAPI backend expose PostgreSQL-backed runs, exact-action approvals and
execution history; leased workers apply decisions and advance work. The local
reviewer demo uses a deterministic fake model, and untrusted code requires the
Docker sandbox. The demo demonstrates orchestration and safety behavior rather
than real-model coding quality or a publicly deployed service.

Start with the [reviewer guide](../REVIEWER_GUIDE.md), then the
[product specification](PRODUCT_SPEC.md), [task record](DAY2_TASKS.md) and
[AI-assistance disclosure](../ai-workflow.md).

## Evidence and final handoff

| Evidence | Status / record |
| --- | --- |
| Evaluated public commit and branch | Exact evaluated commit URL is recorded in the verified submission release / local ready receipt after matching CI passes |
| Complete CI on that commit | Matching completed run, tested SHA, job results and scope limits are recorded in the verified submission release / local ready receipt |
| Dependency audit | Vite/Undici security patches validated; clean install and audit report 0 vulnerabilities; [patch record](DEPENDENCY_PATCH.md) |
| Fresh reviewer startup | Passed with cached pinned dependencies in a fresh public-checkout environment; [commands and versions](DAY3_VALIDATION.md) |
| Approval and completed execution | Captured: recorded approval, succeeded execution, one attempt, completed run; [demo](evidence/README.md) |
| Reload/restart persistence | Passed: same project/run/decision/execution, exactly one attempt; [before/after proof](evidence/persistence.json) |
| Demo and screenshot index | [48-second captured demo, ten screenshots and hashes](evidence/README.md) |
| Course submission receipt | **PENDING:** platform confirmation and timestamp after submission |
| Three peer reviews | **PENDING:** platform completion status by the separate deadline |

[Publication history](PUBLICATION.md) and [Day 2 validation](DAY2_VALIDATION.md)
retain earlier results. They do not replace evidence for the exact final commit.

## Short demonstration script

Use this walkthrough alongside the [captured demo](evidence/README.md). The clip
shows the application flow; commands, CI and limitations are documented here.
Sign-in and restart downtime are excluded; restart proof is recorded separately.

1. **Problem and scope:** explain that risky actions need an exact human decision
   and durable execution evidence. Identify this as the local fake-model demo.
2. **Project and approval:** begin screen capture after sign-in. Show the seeded
   project, pending action, reason, fingerprint/risk and expiry; do not show the
   token file, token contents, signing key, database URL or unrelated local data.
3. **Decision:** approve the displayed scripted deletion in its generated
   calculator workspace. Wait for the API-confirmed decision; do not describe
   the approval itself as completed execution.
4. **Execution:** show the worker's resulting execution record and completed run.
   Explain the separation between permission to act and evidence that it acted.
5. **Persistence:** reload, then stop/restart the same launcher and show that the
   same project, decision and execution history remain. Preserve its database,
   volume and workspace; do not reset resources for a cleaner recording.
6. **Reproduction and limits:** show the reviewer commands and final matching CI
   run, then briefly name the gaps below. Capture screenshots of the pending
   approval and final history only after checking them for sensitive content.

For a fresh independent checkout, follow the [Day 3 handoff](DAY3_HANDOFF.md):
existing reviewer resources may occupy the fixed ports even after the launcher
stops. Use an independent machine rather than running parallel reviewer copies
or bypassing ownership checks.

## Known gaps to disclose

- Full application containerization remains incomplete: PostgreSQL and execution
  isolation use containers; the reviewer API, worker and frontend run on the host.
- Production identity, a public application deployment and CD are not established.
  The [deployment runbook](../deployment.md) is preparation, not deployment evidence.
- Correlated operational metrics/traces and a viewing interface, verified
  user-impact alerting and an alert-triggered bounded responder remain missing.
  Approval notifications do not demonstrate these operational criteria.
- The reviewer workflow uses the deterministic fake model. It does not prove
  real-model coding quality, live cloud actions or real notification delivery.

## Before pressing Submit

- [ ] Fill the evaluated commit's exact SHA from the verified local ready receipt;
  use the confirmed certificate name **Bryan Hunt**. Optional hours remain blank.
- [ ] Confirm the exact public SHA includes the intended code and evidence and
  has a completed passing CI run; review the final rubric claims and demo links.
- [ ] Submit through the course platform before the verified cutoff and retain
  its receipt. Publishing to GitHub alone does not submit the course project.
- [ ] Complete the three assigned peer reviews by November 2 at 23:00 UTC.
