# Current Zoomcamp rubric mapping

Reviewed 2026-10-23 against the [official current source](https://raw.githubusercontent.com/DataTalksClub/ai-dev-tools-zoomcamp/main/project/README.md).
It states 12 criteria, 24 total points, and 10 points to pass. Three peer reviews
are also required. The course platform controls deadlines and final assessment.
This replaces the older 14-criterion mapping, preserved in
[HISTORICAL_RUBRIC_MAPPING.md](HISTORICAL_RUBRIC_MAPPING.md).
No score is awarded here: test files or historical results alone do not prove
that this checkout works. Fresh outcomes are in [DAY1_VALIDATION.md](DAY1_VALIDATION.md) and [DAY2_VALIDATION.md](DAY2_VALIDATION.md).

| Criterion | Max | Evidence and remaining work |
| --- | ---: | --- |
| Problem description | 2 | README now states the user problem, workflow, behavior, and limitations. |
| Product specification | 2 | [Specification](PRODUCT_SPEC.md) exists. [Day 2 tasks](DAY2_TASKS.md) record acceptance criteria and real development-agent contributions. Public publication remains outstanding. |
| Agent team specification | 2 | [AI workflow](../ai-workflow.md), graph and agent modules describe responsibilities and review loops. |
| Context management | 2 | Root AGENTS.md links modular architecture, process, security, API, database and frontend docs. |
| Application | 2 | React frontend, FastAPI, PostgreSQL and workers passed the local browser workflows after Docker recovery; see the fresh validation record. The reviewer launcher now supplies one-command local startup after setup; see [reviewer guide](../REVIEWER_GUIDE.md). |
| Testing | 2 | Python unit/integration and frontend tests exist. Fresh results and unavailable suites are recorded separately; `scripts/reviewer.py check --browser` combines the offline, database, frontend and browser checks. |
| Containerization | 2 | sandbox_image/Dockerfile packages execution isolation only. Full application plus database Compose startup remains missing. |
| Deployment | 2 | Local-only; no verified public URL or deployed demonstration. [Deployment preparation](../deployment.md) covers identity, hosting, backups and rollback; production configuration remains outstanding. |
| CI/CD | 3 | PR/push CI is implemented with immutable action pins and validated by actionlint. Hosted execution awaits source publication; no CD or public deployment is claimed. |
| Observability | 2 | API emits redacted structured logs and request IDs. Correlated metrics/traces and a collection/viewing UI remain missing. |
| Alerting on user impact | 1 | No verified operational alert. Approval notifications do not demonstrate a user-impact alert. |
| On-call agent | 2 | No verified alert-triggered remediation. Coding orchestration is not an on-call responder. |

Prioritize reproducibility and passing evidence before optional feature depth.
The passing threshold is not proof of submission readiness or a guaranteed grade.
