# Day 2 reviewer and delivery validation

Validation performed on 2026-10-23 (America/Denver) against the working tree on
`codex/zoomcamp-day2-reviewer`, based on `f9fb35c`. This record distinguishes
local evidence from hosted CI and public deployment, which had not run at that
point. The subsequent repository publication and hosted results are recorded in
[PUBLICATION.md](PUBLICATION.md). The results below remain the original local
validation evidence.

## Delivered scope

- `scripts/reviewer.py setup` installs the five pinned Python requirement files
  in an isolated venv and installs/builds the npm lockfile. No global Python
  packages are used. The pytest dependency closure now has four additional
  exact pins: iniconfig, packaging, pluggy and pygments.
- `up` starts loopback API, frontend and worker services, with an independently
  owned PostgreSQL container/volume/network, generated local credentials and a
  seeded deterministic approval workflow. Migrations are an explicit launcher
  bootstrap step, not application startup behavior.
- `smoke` checks anonymous rejection, authenticated API access through the
  frontend proxy and a persisted approval pause, then stops its child services.
  `check --browser` combines the existing Python, PostgreSQL, frontend and
  installed-Chrome checks. No live model, cloud or notification provider runs.
- `.github/workflows/ci.yml` defines Python, frontend and database/browser jobs
  using pinned action commits, Python 3.12.10 and Node 24.18.0. Artifacts retain
  test results and the candidate frontend bundle. No deploy job or secret is
  configured.
- [Reviewer instructions](../REVIEWER_GUIDE.md), [deployment preparation](../deployment.md)
  and [task acceptance criteria](DAY2_TASKS.md) describe delivered behavior and
  remaining work.

## Reproducibility method

A fresh directory was exported from tracked working-tree source plus the new
launcher, tests, CI and reviewer documents available at validation start. Final
evidence records were added afterward. The export excluded `.git`, `.local`, `.venv`, `node_modules`,
credentials and unrelated untracked files. The exported source was used for
setup and tests; existing project environment packages were not reused.

The actual command was `python scripts/reviewer.py setup --offline --wheelhouse DIR`.
The wheelhouse contained the pinned dependencies downloaded beforehand; npm
used its cache and PostgreSQL used the already cached digest-pinned image.
This proves a clean install from the pinned inputs, not availability of the
public package servers for a future reviewer. The normal setup command obtains
those inputs when needed. `pip check` and the frontend build passed. The same
setup also passed in the actual implementation checkout.

The generated reviewer database uses port 55433, separate from the pre-existing
development database on 55432. API and dashboard use 18000 and 14173. Workspaces
are generated outside the source checkout. Credentials and history are retained
across startup; no reset, prune or existing-volume removal was used.

## Results

| Check | Result |
| --- | --- |
| Clean Windows default suite | 1,889 passed, 466 skipped; 134.12 seconds. |
| Clean Windows PostgreSQL integration | 352 passed, 2,003 deselected; 178.03 seconds. |
| Frontend contract, types, component/unit checks | Passed, including 57 tests in five files. |
| Production frontend build | Passed. |
| Linux default suite in a fresh Python 3.12.10 container | 1,887 passed, 468 skipped; 108.74 seconds. JUnit confirms all 41 launcher tests ran. |
| Real-browser tier | 42 passed, 2313 deselected, 1 warning in 451.55s (0:07:31). |

Windows default skips cover opt-in database/browser/Docker/live/stress tiers and
one unavailable symlink privilege. Linux additionally skips three Windows-only
junction cases while running the symlink case. These are explicit exclusions,
not failed checks relabeled as passing. Each Python suite reported one existing
Starlette deprecation warning about the pinned TestClient/httpx combination.
The passing dependency set was retained rather than changing it during delivery.

The Linux check used the already installed Python 3.12.10 slim image, installed
Git and the pinned requirement files inside a new disposable container, and ran
the same default suite against the source-only export. No host Python packages,
Docker socket or credentials were mounted. It validates Python portability;
Linux-hosted PostgreSQL and Chrome jobs still require the actual hosted CI run.

The main-checkout smoke also passed after the clean-copy checks finished. Its
reviewer database is ready for the documented `up` command. The clean-copy
database was stopped with its container, volume and history retained. Raw test
logs, Linux JUnit and source hashes are saved locally under
`.local/zoomcamp-day2-20261023/` (ignored by Git). The original development
container ID and volume are unchanged and running; the unrelated unrelated database container
container remains stopped.

Two consecutive corrected smoke runs passed using the same project and existing
`review-approval` run, both at `awaiting_approval`. Each verified the real
frontend API proxy and HTTP 401 for anonymous product access. Neither recorded
a human decision or requeued a terminal run. The first two diagnostic attempts
had failed readiness; the cause and fix are recorded below rather than counted
as passing runs.

The CI YAML passed actionlint 1.7.7 using the already installed image with
networking disabled and only the workflow mounted read-only. Action references
were verified against official upstream release commits. This does not prove
a hosted GitHub Actions run.

## Defects found and corrected

1. Two positive process-guard fixtures assumed Windows paths. They now construct
   native paths, allowing the same strict guard assertions to run on Linux.
   No process/network guard or test selector was weakened.
2. Inherited libpq `PG*` variables could override a nominal loopback connection.
   The launcher clears them in both child environments and its own process,
   including invocation from inside its dedicated venv.
3. PostgreSQL URI query parameters could override the validated URI authority.
   All three generated URL files now reject query, fragment and unexpected
   scheme before the database driver is imported.
4. A teardown error for one child could leave other services alive. Cleanup now
   visits every owned child, tolerates an already vanished process group, and
   reports remaining errors using service names only.
5. The smoke helper requested JSON from Vite's HTML root, causing HTTP 404 and a
   readiness timeout. It now accepts HTML and JSON while still requiring HTTP
   200. A focused regression covers the request header.

Independent audit re-reviewed the boundary fixes. In isolated temporary copies,
the pre-HTML-fix baseline passed all 40 launcher tests. Removing `PG*` stripping
caused three expected failures; bypassing current-process sanitization caused
one; accepting URI query overrides caused three; aborting cleanup early caused
three. Each failed at its intended assertion. Those mutation runs blocked
subprocess/network operations and did not alter the actual source. The HTML
readiness regression brings the final launcher test count to 41.

## Remaining limits and submission gates

- Hosted CI is unverified. The local implementation has no Git remote. The
  existing private portfolio repository explicitly contains documentation
  rather than the runnable source; it has not been overwritten or repurposed.
- There is no public deployment or production browser sign-in. The deployment
  runbook is a concrete preparation artifact; production identity, hosting,
  TLS routing, operational verification and CD remain unimplemented gates.
- The host-based launcher is not full application containerization. PostgreSQL
  and execution isolation use containers; API, worker and frontend run on the host.
- Browser checks intentionally exclude Docker-container restart scenarios.
  Day 1's separate full browser/outage evidence remains in [DAY1_VALIDATION.md](DAY1_VALIDATION.md).
- Frozen branches and release tags are unchanged. Unrelated untracked files and
  the existing development and unrelated containers are preserved. No source has
  been pushed, published or deployed, and no new release commit is claimed.
- Record the final submission commit, hosted evidence, demonstration and exact
  course cutoff before submission. Complete the separate peer reviews afterward.
