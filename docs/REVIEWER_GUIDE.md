# Reviewer setup and demonstration

This is a local review environment for Agent Factory, with real PostgreSQL,
FastAPI, React and workers, and a deterministic fake model. It requires no cloud
account or model API key. It does not demonstrate real-model coding quality.

## One-command Docker startup

Prerequisites: Git to obtain the source, a running Docker Linux engine, Docker
Compose v2 with `--wait` support, and network access for the first build. Python
and Node are installed inside the application image. Use a local Docker context.

```sh
git clone https://github.com/BryanHunt290/agent-factory-zoomcamp.git
cd agent-factory-zoomcamp
docker compose up --build -d --wait
```

Open **http://127.0.0.1:15473**. The application generates its own development
identity and seeds the calculator approval workflow. Copy its token to a private
local file, then select that file on the sign-in screen. First create `.local`
if absent (PowerShell: `New-Item -ItemType Directory -Force .local`;
Linux/macOS: `mkdir -p .local`), then copy:

```sh
docker compose cp app:/state/token.txt .local/docker-token.txt
```

The token is never printed. Do not commit or share this file. It expires after
three hours; refresh it and copy the file again.

```sh
docker compose exec -T app python scripts/docker_runtime.py token
docker compose cp app:/state/token.txt .local/docker-token.txt
```

Follow the Walkthrough below. The API, worker and built frontend run together in
one supervised application container; PostgreSQL has its own container. Explicit
bootstrap runs before the application servers, so API startup still does not
migrate a database. The API's loopback guard, authentication and approval checks
remain enforced. Only the frontend port is published, bound to `127.0.0.1`.

```sh
docker compose ps
docker compose logs --tail 60 app
docker compose stop
docker compose up -d --wait
```

Normal stop/start preserves the project, decision, execution and workspace
history in named volumes. Keep the same Compose project name and directory;
using a different name intentionally creates an independent review environment.
Do not use `down -v`, `volume rm`, or volume-pruning commands to stop a session.
The stack does not mount the source checkout, existing reviewer data or Docker
socket. Its database has no published host port. The existing host review
database on 55433 and development database on 55432 are separate.

The deterministic fake model and trusted calculator fixture are the supported
Docker demonstration. No paid model, cloud action or real notification provider
is configured. Untrusted code still requires the separate execution sandbox;
without a Docker socket in this application container it fails closed.

If a build or health check fails, inspect `docker compose ps -a` and the relevant
service logs. Resolve the reported issue and rerun startup with the same project
and volumes. Do not regenerate missing credentials over retained database data.
The [Docker validation record](zoomcamp/DOCKER_STARTUP.md) records the tested
platform, actual startup and persistence results and remaining limitations.

## Host launcher prerequisites

- Python 3.12 (validated locally with 3.12.10).
- Node.js 24.18.0 or a compatible newer Node 24 release, with npm.
- Git and a running Docker Linux engine.
- Google Chrome for the optional real-browser test tier.
- Network access for the initial package/image download. No services are published
  beyond this machine's loopback interface.

Clone the public submission source into a new directory:

```powershell
git clone https://github.com/BryanHunt290/agent-factory-zoomcamp.git
cd agent-factory-zoomcamp
```

From the repository root, install the reviewer environment once:

```powershell
py -3.12 scripts/reviewer.py setup
```

On Linux/macOS use `python3.12` in place of `py -3.12`. Setup creates a dedicated
virtual environment without global Python packages, installs the checked-in
requirements and npm lockfile, and prepares the pinned PostgreSQL image.

Start the complete local workflow with one command:

```powershell
py -3.12 scripts/reviewer.py up
```

Use the URL and token-file path printed by the launcher. The token itself is not
printed. Select that file on the dashboard sign-in screen. This is development
authentication for local review only.

## Walkthrough

1. Open the seeded project and its pending approval.
2. Inspect the exact requested action, reason, risk, expiry and execution history.
3. Approve the scripted deletion. The UI waits for the API to confirm the decision.
4. Observe the worker execute that approved action and complete the calculator run.
5. Reload the page: the project and decision are reconstructed from PostgreSQL.

Alternatively, choose Reject and verify that the rejection is recorded and the
deletion is refused: `legacy_calc.py` remains and no execution attempt is recorded
for that action. The calculator scenario can still complete after skipping the
refused deletion. A completed run does not mean that a rejected action executed.

The example acts only on its generated calculator workspace. The dashboard does
not offer arbitrary command execution or general-purpose project/run creation.
The application still enforces action fingerprints, permission checks, expiry and
durable execution records.

For the host launcher, keep its terminal open while reviewing. Ctrl+C stops its API, frontend
and worker children. It preserves the database container, volume and review
history. Do not use Docker volume-pruning commands to stop a review session.

## Host regression verification

Stop any active `up` session with Ctrl+C before running these commands, and run
them one at a time. Its database and review history remain available; concurrent
launcher commands are refused.

```powershell
py -3.12 scripts/reviewer.py smoke
py -3.12 scripts/reviewer.py check
py -3.12 scripts/reviewer.py check --browser
```

The smoke check starts the actual stack, authenticates and confirms the seeded
workflow, then stops its child services. The checks use a separate test database
and random temporary schemas, not the persisted reviewer data. Browser checks
drive installed Chrome. They exclude the deliberate Docker-container restart
cases; Day 1's separate full reliability validation is documented in
[the validation record](zoomcamp/DAY1_VALIDATION.md).

## Isolation and troubleshooting

The launcher uses its own `.local/reviewer/` state and ownership-labeled Docker
resources. It never adopts another project's database or removes an existing
container/volume. The existing development database on port 55432 is not used.
Occupied ports or ownership mismatches stop startup with an error.

Do not copy `.local/`, the generated token/private key, or database URL files into
a submission. A reviewer generates their own. Use the logs named by the launcher
to diagnose a failed startup; never paste credentials into an issue.

If Docker is unavailable, start its Linux engine and retry. If setup fails,
resolve the package or runtime error and rerun setup; do not enable global Python
packages or disable a failing test. See [deployment preparation](deployment.md)
for the separate requirements before any public hosting.
