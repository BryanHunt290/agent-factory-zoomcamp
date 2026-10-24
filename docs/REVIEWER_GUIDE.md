# Reviewer setup and demonstration

This is a local review environment for Agent Factory, with real PostgreSQL,
FastAPI, React and workers, and a deterministic fake model. It requires no cloud
account or model API key. It does not demonstrate real-model coding quality.

## Prerequisites

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

Keep the launcher terminal open while reviewing. Ctrl+C stops its API, frontend
and worker children. It preserves the database container, volume and review
history. Do not use Docker volume-pruning commands to stop a review session.

## Verification

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
