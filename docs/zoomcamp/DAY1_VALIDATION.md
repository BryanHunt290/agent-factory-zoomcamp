# Day 1 validation — 2026-10-23 (America/Denver)

**Current status: Docker startup recovered; local database and browser verification
passed.** The recovery section below records 352 PostgreSQL tests, all 44 browser
tests, and 3 additional database restart/isolation tests, all passing with no skips.
The earlier failed attempt is retained as history. Clean-clone packaging and
one-command startup remain outstanding.

Baseline: `f9fb35c`; milestone branch: `codex/zoomcamp-day1-readiness`.
This is fresh local evidence, not a production or clean-clone certification.

| Check | Result |
| --- | --- |
| `.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3` | 1,848 passed, 466 skipped in 140.54 seconds. Skips are opt-in database/browser/Docker/stress/live-cloud/live-model suites and one Windows symlink-privilege test. |
| `npm.cmd run check` in frontend | Contract types match; TypeScript passed; 57 tests passed across 5 files. |
| `npm.cmd run build` in frontend | Production bundle built successfully. |
| CLI `demo --scenario repair --workspace <new external temporary directory>` | Completed six graph steps: five acceptance tests initially had one failure, repair yielded all five passing, reviewer approved; 36 hash-chain-verified log entries. Scripted fake provider, real local fixture execution. |
| PostgreSQL opt-in suite | Failed at first setup: local test database connection timed out on port 55432; 1 error, 1,915 deselected. No claim of passing database tests. |
| Docker engine check | Initial Linux engine named pipe absent. Docker Desktop launch attempted; subsequent container listing did not return and was interrupted. No images pulled/built, containers removed, or database reset. |
| Browser/full-stack validation | Not completed because PostgreSQL is unavailable. Frontend unit tests are not substituted for this evidence. |

## Changes and limits

Updated README with the user problem, architecture, honest reviewer entry points
and limitations. Replaced the old checklist and mapping with current-rubric
documents, keeping historical copies. Marked the old official snapshot as
historical, corrected the stale backend feature description, and documented
AI assistance. No application-code failure was established by the passing
offline checks; no runtime code or safety guard was changed.

## Original resume instructions (blocker resolved below)

Restore Docker Desktop's Linux engine first. List only Agent Factory containers:
`docker ps -a --filter name=agent-factory-postgres`. Confirm the existing project's
test database is available at its configured local endpoint; do not create a
replacement or use another project's database. Do not print URL-file contents.
Then run in PowerShell from the repository root:

```powershell
$env:AGENT_FACTORY_TEST_DATABASE_URL_FILE = '.local/test-database.url'
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -m postgres --run-postgres
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -m browser --run-browser --run-postgres tests/test_dashboard_browser.py tests/test_dashboard_e2e.py
```

The harness launches the real loopback API and dashboard, installed Chrome and
workers against isolated test schemas. Docker-outage reliability tests are a
separate opt-in operation. Keep failed attempts in this record when adding results.

At the initial attempt, the remaining risks included full-stack verification and reproducible setup,
public review access, full-system containerization, CI/CD and deployment.
No submission, publication, paid call or external message was performed.

## Docker diagnosis and final repository checks

The Docker Desktop backend log for this attempt reports a startup crash while
initializing the Inference manager: its `dockerInference` socket under the user's
local Docker runtime directory cannot be removed/accessed by the system. This
explains the unavailable engine and database. The underlying filesystem cause
has not been established. No factory reset or shared Docker configuration change
was attempted; restoring Docker is the next prerequisite for full-stack testing.

`git diff --check` passed. All seven frozen branch/tag references match the
pre-work values. Six existing documentation files were modified and three new
documentation files were added; changes remain uncommitted. Pre-existing
`.codex/` and unrelated private local files remain untouched.

## Recovery and completed local verification — 2026-10-23

The owner explicitly authorized a data-preserving Docker startup repair and the
database/browser tests. A separate read-only agent reviewed the repair scope and
test selection. Docker issue reports [#554](https://github.com/docker/desktop-feedback/issues/554)
and [#460](https://github.com/docker/desktop-feedback/issues/460) matched the local
failure: inaccessible stale Windows AF_UNIX socket reparse points.

The bounded `docker desktop stop --timeout 20` command timed out. A later process
check confirmed that Desktop and backend processes were gone before repair.
Only the two directories containing the verified zero-byte stale sockets were
renamed, preserving all their contents:

- `%LOCALAPPDATA%/Docker/run.preserved-20261023-222616`
- `%LOCALAPPDATA%/docker-secrets-engine.preserved-20261023-222616`

Docker recreated its runtime sockets on startup. Data-disk paths, sizes and
timestamps, and the settings checksum were unchanged immediately before restart.
No factory reset, pruning, disk deletion, WSL unregister, image pull/build or
container/volume replacement was performed. This is a targeted recovery, not a
guarantee that this Docker version will never encounter stale sockets again.

Docker Engine reported version `29.6.1`. Both existing containers were visible.
Only `agent-factory-postgres-dev` was started. Its original container identity was verified unchanged, with the original
`agent-factory-postgres-dev-data` named volume. The unrelated database container remained
stopped. The browser outage tests stop/start only the guarded project database
container; they never remove it or its volume.

| Fresh check after repair | Result |
| --- | --- |
| PostgreSQL integration, excluding browser and Docker-live tiers | 352 passed, 0 skipped, 1962 deselected in 175.40 seconds |
| All four browser files, including reliability and fake-notification workflows | 44 passed, 0 skipped in 485.87 seconds; JUnit: 0 failures, 0 errors |
| Remaining PostgreSQL restart and sandbox/database boundary cases | 3 passed, 0 skipped in 12.07 seconds |

Commands (PowerShell, from repository root, executed serially):

```powershell
$env:AGENT_FACTORY_TEST_DATABASE_URL_FILE = '.local/test-database.url'
$env:AGENT_FACTORY_TEST_POSTGRES_CONTAINER = 'agent-factory-postgres-dev'
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider -m 'postgres and not browser and not docker_live' --run-postgres --tb=short --maxfail=3
$env:DASHBOARD_EVIDENCE_DIR = '.local/zoomcamp-day1-20261023/browser-evidence'
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3 -m browser --run-browser --run-postgres --run-docker-live tests/test_dashboard_browser.py tests/test_dashboard_e2e.py tests/test_dashboard_reliability.py tests/test_dashboard_notifications.py
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3 --run-postgres --run-docker-live tests/test_worker_restart_live.py tests/test_db_sandbox_boundary_live.py
```

These checks use temporary test schemas, the real loopback API/frontend,
installed Chrome and real workers. Model and notification providers remain
fake; no real message or paid model/cloud call is involved. Browser traffic
outside loopback is blocked by the test harness. General sandbox/adversarial,
stress, live-cloud and live-model suites were not part of this repair validation.
The two boundary tests used the existing locked sandbox image. Before running
them, the inventory of existing `agent-factory.exec` containers was empty, so
their cleanup could affect only newly created test containers. The original
PostgreSQL container and volume were verified again after the test runs; the
unrelated database container remained stopped.

Two local JUnit reports and 21 per-scenario recovery records are retained in the ignored
`.local/zoomcamp-day1-20261023/` directory for inspection. They are local test
evidence, not published artifacts.

Local verification is now complete for Day 1's requested scope. Clean-clone setup,
a single-command launcher/test entry point, full-stack containers, public review
access, CI/CD and deployment remain delivery work; these test results do not
establish a published or production-ready submission.
