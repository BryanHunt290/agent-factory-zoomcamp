# Submission checklist — current 2026 rubric

Reviewed 2026-10-24 UTC. [Official source](https://raw.githubusercontent.com/DataTalksClub/ai-dev-tools-zoomcamp/main/project/README.md):
12 criteria, 24 points, 10 to pass, plus three mandatory peer reviews.
See [rubric mapping](RUBRIC_MAPPING.md) and fresh [Day 1](DAY1_VALIDATION.md) / [Day 2](DAY2_VALIDATION.md) validation.
The previous checklist is retained as [historical evidence](HISTORICAL_SUBMISSION_CHECKLIST.md).

## Day 1: demonstrate and document the existing application

- [x] Identify the actual implementation checkout and create a milestone branch.
- [x] Describe the user problem, expected behavior and current limitations in README.
- [x] Replace the obsolete rubric mapping with the current 12 criteria.
- [x] Identify existing specification, agent roles and modular context documentation.
- [x] Run the offline CLI repair demonstration successfully.
- [x] Run frontend contract, type, unit/component checks and production build.
- [x] Complete Python baseline validation and record failures/skips honestly.
- [x] Complete PostgreSQL and real-browser workflow validation (see the recovery results in DAY1_VALIDATION.md).
- [x] Verify clean reviewer setup and one-command startup, including repeated startup and the main checkout (DAY2_VALIDATION.md).

## Day 2: packaging and delivery

- [x] Implement a one-command local launcher with isolated database and retained history.
- [x] Add Docker Compose for the API, worker, frontend and PostgreSQL; see [Docker task and validation](DOCKER_STARTUP.md) for checks and limitations.
- [x] Provide `scripts/reviewer.py check --browser` for unit, database, frontend and real-browser checks.
- [x] Record local tasks, acceptance criteria and genuine agent-use evidence in [DAY2_TASKS.md](DAY2_TASKS.md).
- [x] Publish the [task/evidence record](https://github.com/BryanHunt290/agent-factory-zoomcamp/blob/40cea25324288e1e6ef6b2e8180dc3f85e797b63/docs/zoomcamp/DAY2_TASKS.md) with the public source repository.
- [x] Add pinned PR/push CI with Python, frontend, PostgreSQL and browser jobs; actionlint passed.
- [x] Publish runnable source to [the public repository](https://github.com/BryanHunt290/agent-factory-zoomcamp) at initial commit `40cea253`.
- [x] Obtain a passing complete [hosted CI run](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions/runs/40065197804) on initial public commit `40cea253`; all Python, frontend and PostgreSQL/browser jobs passed.
- [x] Prepare the production deployment, identity, backup and rollback runbook in `docs/deployment.md`.
- [ ] Configure production authentication and verify a deployed demo after the hosting decision.
- [ ] Activate CD after production gates; rollback procedure is prepared, not exercised against a live deployment.

## Day 3: evidence and submission

Follow the [Day 3 handoff](DAY3_HANDOFF.md) for the ordered final preparation and evidence checks.

- [x] Verify the exact deadline and timezone on the course platform.
- [x] Re-run the clean setup and the principal user journey.
- [x] Capture a short demonstration and screenshots with no credentials visible.
- [x] Review the 12 criteria against actual evidence; keep missing items explicit.
- [ ] Operational metrics/traces, user-impact alert and bounded responder remain explicit rubric gaps.
- [x] Confirm the [public reviewer-accessible repository](https://github.com/BryanHunt290/agent-factory-zoomcamp).
- [x] Select initial submission `ea02081` after complete CI passed.
- [x] Submit through the official course platform; confirmed SUBMITTED on October 24 at 05:17 MDT.
- [ ] Complete three assigned peer reviews by their separate deadline.

Verified cutoff: **October 26, 2026, 23:00 UTC / 5 p.m. Denver MDT**.
Three peer reviews: **November 2, 2026, 23:00 UTC / 4 p.m. Denver MST**.
See [Day 3 validation](DAY3_VALIDATION.md), [captured evidence](evidence/README.md)
and the [final package](FINAL_SUBMISSION.md). Final commit/CI selection is recorded
after verification in the submission release and local ready receipt; course
updates and peer reviews remain separate actions. The existing saved submission remains valid while an update is being verified.

For a Docker milestone update, select the exact new commit only after all its CI
jobs pass, then retain the updated course confirmation. The latest local receipt
records that post-publication step; historical release tags remain unchanged.
