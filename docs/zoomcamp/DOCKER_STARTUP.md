# Docker reviewer startup: task and validation record

Scope approved 2026-10-24: improve full application containerization, preserve
existing containers and data, rerun checks, and update the submitted commit only
after matching CI passes. The prior submitted commit is `ea02081`.

## Acceptance criteria agreed before implementation

- A fresh clone with a running Docker Linux engine and Compose starts the
  frontend, authenticated API, worker and PostgreSQL with one command.
- The host needs no Python or Node installation for this Docker startup path.
- The dashboard is published only on host loopback; API authentication, local
  bind restrictions, exact-action approval and worker fencing stay enforced.
- Database records, signing identity and generated workspaces survive a normal
  stop/start. Restart does not duplicate projects, decisions or executions.
- The new stack uses its own named volumes and project resources. It does not
  adopt, reset, remove, restart or migrate existing reviewer/development data.
- Credentials are generated locally, retained privately and excluded from the
  build context, logs and source. Live model/cloud providers stay disabled.
- New runtime guards have regression coverage. CI builds and exercises the
  actual Compose stack, including authentication, approval and persistence.
- The existing Python, database, frontend and real-browser checks pass. The
  official course form changes only after complete CI passes for the chosen SHA.

## Actual development collaboration

- Integration agent: acceptance criteria, reviewer documentation, live validation,
  source review, publication and the final verified course-form update.
- Implementation agent: application image, Compose services and lifecycle code.
- Test/CI agent: independent lifecycle/guard tests and real Compose CI smoke.
- Preservation agent: Docker Desktop readiness, before/after resource inventory
  and independent review of the changes.

The agents read this task record and the repository's modular instructions.
Implementation and review findings, exact check results and limitations are
recorded below as they become available; planned checks are not passing evidence.

## Validation

Validated locally on October 24, 2026 on Windows with Docker Desktop 4.82.0,
Linux Docker Engine 29.6.1 and Compose 5.3.0 (linux/amd64 containers). The
application image runs Python 3.12.10 and Node 24.18.0; PostgreSQL reports 17.11.

| Check | Observed result |
| --- | --- |
| Existing Python default suite | 1,889 passed, 467 skipped, one warning; 133.50 s |
| New runtime and smoke guard tests | 95 passed; 0.91 s |
| Isolated mutation audit | All 11 deliberate regressions caught; original source hashes unchanged. Its baseline was 94 tests; the final additional test checks build-context credential exclusions. |
| Frontend contracts, types, unit tests and build | Passed; 57 tests in five files; production bundle built |
| PostgreSQL integration, excluding browser and Docker-live tiers | 353 passed, 2,093 deselected, one warning; 175.25 s |
| Real installed-Chrome browser workflows, excluding Docker-live tiers | 42 passed, 2,408 deselected, one warning; 430.21 s |
| Docker configuration and CI lint | `docker compose config --quiet`, `git diff --check` and actionlint 1.7.7 passed |
| Fresh Compose approval and restart check | Passed; [machine-readable evidence](evidence/docker-compose-smoke.json) |
| Exact documented startup command | `docker compose up --build -d --wait` passed in a separate default project; app and DB healthy, frontend published only at 127.0.0.1:15473 |
| Manual browser check | Signed in with the generated token file; live API-backed dashboard shows the seeded pending approval; [screenshot](evidence/docker-dashboard.png) |

The default and database/browser suites were collected while the independent
new guard tests were still being completed; their deselected counts therefore
differ. New guard tests were run separately on the final files. Hosted CI runs
all final test files together on the selected commit. Counts are separate tiers,
not a count of unique behaviors. The warning is the existing Starlette/httpx
TestClient deprecation. Opt-in hostile sandbox, live model/cloud and stress
suites were not enabled; the new Compose check covers its own database restart.

The full-stack check created a fresh, uniquely named Compose project, verified
anonymous requests receive 401, approved only the fixed `legacy_calc.py` action,
observed one recorded successful execution attempt, and stopped/started both
application and database. Project/run/decision/execution/timeline records,
container IDs and volume names were unchanged. The original valid token still
worked, and hashes of the calculator source/tests, workspace record, checkpoint
and execution log were unchanged. It then stopped only its new services and
retained every volume.

The tested and default-running runtime files both have SHA256
`11c26dc359cb409f11b2de2e16814322127021d6304997f62d36b9776746e161`,
matching the final `scripts/docker_runtime.py` source.

## Preservation and review findings

Docker Desktop initially failed on inaccessible temporary connector sockets.
Only its two runtime connector directories were renamed to retained siblings,
after verifying Docker processes had stopped. No reset, prune, database deletion,
WSL unregister, disk move or existing-container restart was performed. The
private before/after inventory tracks the five pre-existing containers and five
volumes; test resources and the new default Compose stack have separate names.
The database/browser suites used a newly generated test database on loopback
port 55434, never the existing development or reviewer databases.

Problems found and fixed during implementation and independent review:

1. Direct script execution could import an unrelated `scripts` namespace. The
   smoke runner now imports its adjacent helper and has an entrypoint regression.
2. Docker's internal-only bridge did not publish the test host port. PostgreSQL
   remains on the private internal network; the app also joins its own dashboard
   bridge, publishing only a loopback port. The initial failed test database was
   stopped and retained, then a new isolated test database passed the suites.
3. Vite's default config loader attempted to write into the read-only image. The
   container uses the supported native config loader; the read-only root remains.
4. Ownership and the process lock must be checked before touching readiness
   state. A second startup cannot remove the active service's health record.
5. Partial or mixed volume sets, missing retained signing identity, changed DB
   role privileges, and existing project resources must fail without replacement.
6. Timed-out Docker checks must stop their owned command process tree before
   stopping the new stack. Remote named pipes are rejected and the verified
   local Docker endpoint is pinned for later operations.
7. Readiness-file cleanup failures must still close every supervised child.
8. The build context excludes credentials even inside an allowed source tree.

The failed frontend-startup project was stopped with its data retained. No
failure was hidden with skips or weakened assertions. The passing result is from
a separate fresh stack after the fixes.

## Reproduce and interpret the evidence

From a checkout with Python 3.12 available, the extra full-stack check is:

```sh
python scripts/compose_smoke.py --report .local/compose-smoke/result.json
```

It builds the same app, chooses a new project, and stops it after the check while
retaining volumes. If the normal dashboard already uses 15473, add `--port 15474`.
This test helper's host Python requirement does not apply to ordinary Docker
startup, which requires only Docker/Compose after obtaining the source.
The fourth [CI job](../../.github/workflows/ci.yml) runs the same check on a fresh
hosted Linux runner alongside the existing Python, frontend and browser jobs.

The official rubric's full containerization criterion is supported: the frontend,
API, worker and database start through Compose in one command. This is the local
deterministic reviewer environment. It adds no production identity, public
deployment, CD, observability stack or on-call responder. It has no Docker socket;
arbitrary untrusted code remains blocked when its separate sandbox is unavailable.
The dashboard bridge provides normal network connectivity; live provider
activation remains disabled, rather than being claimed impossible at the network.

Base images are pinned by verified manifest digest, and Python/npm dependencies
use the checked-in pins/lockfile. Debian utility packages resolve from the base
distribution's repositories, so this is not a claim of bit-for-bit image builds.
Only amd64 containers were exercised; arm64 has not been independently verified.

Evidence SHA256:

- `docker-compose-smoke.json`: `8622369c0b7e970006b676525d6dd204870ed5fa36452db099275278453056ac`
- `docker-dashboard.png`: `5a1717ba9b69100f20599be0a7a2b60b5ee8699ba5fe4beceb2264873d05435b`

## CI and course-submission gate

The prior saved submission is `ea02081`. The owner authorized replacing it only
after complete CI passes for the new exact commit. The candidate's live CI status
is in [GitHub Actions](https://github.com/BryanHunt290/agent-factory-zoomcamp/actions);
the final selected SHA, completed run and updated course confirmation are recorded
after publication in the local submission receipt. Earlier green runs never
substitute for the candidate's own passing CI. The existing release tag and its
demo remain historical evidence for the prior submission and are not moved.


Final preservation comparison: all five pre-existing containers remained stopped
with their original IDs, images, mounts, restart policies and state timestamps.
All five original volume metadata hashes and seven original network identities
matched the baseline. This verifies resource metadata and unchanged container
state; it is not a checksum of database contents. Only newly created test stacks
were started/stopped during validation; the new default dashboard remains running.
