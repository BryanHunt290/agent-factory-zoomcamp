# Submission checklist — current 2026 rubric

Reviewed 2026-10-23. [Official source](https://raw.githubusercontent.com/DataTalksClub/ai-dev-tools-zoomcamp/main/project/README.md):
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
- [ ] Containerize the API, worker and frontend as well as PostgreSQL (separate remaining rubric gap).
- [x] Provide `scripts/reviewer.py check --browser` for unit, database, frontend and real-browser checks.
- [x] Record local tasks, acceptance criteria and genuine agent-use evidence in [DAY2_TASKS.md](DAY2_TASKS.md).
- [ ] Publish the task/evidence record with the reviewer-accessible source repository.
- [x] Add pinned PR/push CI with Python, frontend, PostgreSQL and browser jobs; actionlint passed.
- [ ] Publish runnable source to the approved repository and obtain a passing hosted CI run.
- [x] Prepare the production deployment, identity, backup and rollback runbook in `docs/deployment.md`.
- [ ] Configure production authentication and verify a deployed demo after the hosting decision.
- [ ] Activate CD after production gates; rollback procedure is prepared, not exercised against a live deployment.

## Day 3: evidence and submission

- [ ] Verify the exact deadline and timezone on the course platform.
- [ ] Re-run the clean setup and the principal user journey.
- [ ] Capture a short demonstration and screenshots with no credentials visible.
- [ ] Review the 12 criteria against actual evidence; keep missing items explicit.
- [ ] Add operational metrics/traces, user-impact alert and bounded responder if feasible.
- [ ] Confirm a public reviewer-accessible repository and exact submission commit.
- [ ] Submit through the course platform and confirm receipt.
- [ ] Complete three assigned peer reviews by their separate deadline.

Working target: October 26, based on the owner's three-day window, not a verified
official cutoff. Keep the scope to one working application. Real cloud tools,
payments, notifications and paid model calls are not necessary for Day 1.
