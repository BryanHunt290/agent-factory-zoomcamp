# Day 2 delivery tasks and acceptance criteria

Owner assignment: provide a reproducible reviewer environment, one-command
startup and CI, then prepare deployment for the Zoomcamp submission. This local
task record was prepared on 2026-10-23. It is not a published issue tracker.
The [Day 2 validation record](DAY2_VALIDATION.md) supplies actual commands,
outcomes, defects and remaining limits; source preparation alone does not prove
acceptance or a successful hosted run.

| ID | Task and acceptance criteria | Delivery evidence / status |
| --- | --- | --- |
| D2-1 | Reproduce setup without global Python packages: install exact requirement pins and npm lockfile; use the pinned PostgreSQL image; generate local credentials without printing them. | [Launcher](../../scripts/reviewer.py) and [reviewer guide](../REVIEWER_GUIDE.md) prepared. Clean-install results belong in the validation record. |
| D2-2 | Start the real API, frontend and worker with one command after setup; seed an approval workflow; bind only to loopback; preserve reviewer data and existing unrelated containers when stopping. | `reviewer.py up`, isolated reviewer resources and [launcher boundary tests](../../tests/test_reviewer.py). Verify the authenticated approval journey separately from process startup. |
| D2-3 | Supply one test entry point for Python, frontend and PostgreSQL, with an explicit installed-Chrome browser tier; keep test schemas separate from persistent reviewer records. | `reviewer.py check` and `check --browser`; [verification instructions](../REVIEWER_GUIDE.md#verification). Docker-restart and live-provider tiers remain separately opt-in. |
| D2-4 | Define PR/push CI with immutable action pins, read-only repository permissions, pinned runtimes and disposable loopback PostgreSQL; run default Python, frontend and browser/database checks. | [CI workflow](../../.github/workflows/ci.yml) prepared; actionlint 1.7.7 passed locally with networking disabled. Hosted activation and passing run URL remain outstanding. |
| D2-5 | Prepare a concrete deployment plan covering production identity, same-origin TLS routing, durable state, acceptance, paired-state backup and rollback; preserve existing loopback and fake-provider defaults. | [Deployment runbook](../deployment.md) prepared. No production identity, public endpoint, provisioning or CD execution is claimed. |
| D2-6 | Independently inspect launcher isolation and resource ownership; exercise safety assertions with bounded tests and mutation checks; integrate findings without weakening existing guards. | Independent audit assigned to `review_docker_repair`; [boundary tests](../../tests/test_reviewer.py) and the validation record retain the actual findings and mutation outcomes. |
| D2-7 | Keep reviewer instructions, rubric status and AI-use disclosure consistent with observed results, preserving historical evidence. | [AI workflow](../ai-workflow.md#zoomcamp-day-2-delivery), [submission checklist](SUBMISSION_CHECKLIST.md), this task record and final validation. Public task-tracker publication remains outstanding. |

## Actual agent collaboration

The owner selected the scope and deadline. Codex split the authorized local work
between agents in the same chat; these are development-time roles, distinct from
Agent Factory's runtime planner/developer/tester/reviewer graph.

| Agent role | Assigned work and concrete contribution |
| --- | --- |
| Primary integration agent (`root`) | Repository inspection, integration of staged changes, clean dependency and platform validation, fixes, evidence recording and final acceptance decisions within the owner's scope. |
| Reviewer environment agent (`reviewer_design`) | Implemented the reviewer launcher and its setup/start/smoke/check commands with a dedicated local environment, loopback services and retained reviewer state. |
| CI and deployment agent (`ci_deployment`) | Implemented CI and the deployment runbook; verified action commit pins against official releases; validated CI with actionlint; checked environment names and local links against source. Corrected the walkthrough so a recorded rejection is not mistaken for action execution. |
| Independent audit agent (`review_docker_repair`) | Reviewed launcher safety separately from its author and worked on bounded mutation evidence. Specific defects, fixes and results are recorded in Day 2 validation rather than inferred from the role assignment. |

One source-backed finding changed the documentation: the calculator developer
skips a rejected deletion and can still complete its other work. The walkthrough
now checks the rejection record, preserved file and absence of an execution
attempt, rather than treating final run status as proof that an action ran.
Another CI finding was that the existing browser harness requires installed
Google Chrome and that database-restart tests cannot target the generated hosted
service container. The CI selectors and guide make those limits explicit.

## Remaining delivery work

- Select and authorize publication of the runnable source repository and exact
  submission commit; no issue URL or public tracker status is invented here.
- Activate the committed workflow in that repository and record a passing hosted
  run on the submitted commit. Local actionlint and tests do not establish this.
- Complete the production identity, login, exposure and runtime gates before
  public deployment; implement and verify CD only after its target is approved.
- Capture the final demonstration, verify the course cutoff, submit and complete
  the required peer reviews. Use the submission checklist for those decisions.
