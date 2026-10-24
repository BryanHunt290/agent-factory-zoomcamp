# Execution sandbox (v0.2)

This page is the authoritative statement of what the v0.2 execution sandbox
guarantees and what it does not. The design and threat model are in
[design/v0.2-sandbox.md](design/v0.2-sandbox.md).

> **Status: released as v0.2.0** (local tag on branch `v0.2-sandbox`).
> Every claim below has been demonstrated by the test suites named next to it.
> The live suites ran on **one machine**: Windows 11 Home, Docker Desktop
> 29.6.1 with the WSL2 engine (Linux kernel 6.18, cgroup v2).

## Scope

The sandbox protects the **host** from **untrusted project code**: code
written into a workspace by any model provider except the maintainer-scripted
`FakeProvider`.

In Agent Factory, project code is executed in exactly one place: the
tester's `run_tests` tool. That is the code path these guarantees cover. The
orchestrator itself (executor, agents, gateway, workspace, logs) is trusted
and runs on the host.

## What v0.2 provides

| Guarantee | How it is enforced | Demonstrated by |
| --- | --- | --- |
| **Container-level isolation for untrusted generated code** | Untrusted code requires isolation `CONTAINER` or stronger. The default routing sends it to `DockerBackend` with the approved image, and nothing else is eligible. | `test_sandbox_default.py`; live `test_default_routing_executes_untrusted_code_in_docker`; adversarial categories 1–15 |
| **Network-disabled execution** | `--network none`: only `lo` exists; no routes, no DNS | adversarial 3 (DNS, TEST-NET, `host.docker.internal`, gateway, Docker-internal addresses, IPv6) |
| **Filesystem separation from the host** | Copy-in only (the project is streamed in as a tar; no bind mounts, nothing copied back); read-only root filesystem; writable space only in size-capped `noexec,nosuid,nodev` tmpfs that is discarded with the container | adversarial 1–2 (host canary unread and unchanged), 11 (malformed archives and paths) |
| **No host environment or credentials** | The container environment is built only from explicit allowlisted values; secret-like names are always rejected. Since the unreleased `feature/postgres-persistence` branch, database settings (`DATABASE_URL`, `*_DSN`, `PGPASSFILE`, `PGSERVICEFILE`) and any value holding a URL with embedded credentials are rejected too. Since `feature/auth-worker`, so are the API's authentication settings and token-like names (`AGENT_FACTORY_AUTH_*`, `JWT`, `BEARER`, `OIDC`, `JWK`) and any value shaped like a JWT, under any name. Since `feature/notifications-v1`, every Agent Factory setting (`AGENT_FACTORY_*`) and notification providers' account settings (`AWS_*`, `TWILIO*`, `*SID*`, `SES_*`, `SNS_*`, `SMTP*`) are rejected too, even if allowlisted ([notifications.md](notifications.md#real-providers)). | adversarial 4 (canary `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AWS_SECRET_ACCESS_KEY`, `AF_CANARY_SECRET` never visible); `test_db_sandbox_boundary*.py` (the product database's URL and password never visible or reachable); `test_auth_boundary.py` and `test_sandbox_docker.py` (authentication settings and token canaries refused by request validation and by the `docker create` check, and absent from a real local process); `test_notifications.py::test_provider_credentials_can_never_reach_the_sandbox` (provider credential names refused) |
| **Bounded CPU, memory, disk, PIDs, time, and output** | See [Resource limits](#resource-limits) | adversarial 5–10 and 15 |
| **Verified cleanup** | Every execution ends with `rm --force` and a check that no container with its label remains. An unverifiable cleanup marks the backend unhealthy and blocks further untrusted execution. A janitor removes leftovers from crashed orchestrators. | adversarial 13 (cleanup after success, failure, timeout, OOM, disk, PIDs, output, af-entry rejection, interruption, abandoned container) and 14 |
| **Fail-closed behaviour** | No qualifying backend that is available, healthy, correctly configured, and passing its isolation self-test means nothing runs. There is never a fallback to `LocalProcessBackend` for untrusted code. | adversarial 14 (Docker missing, remote `DOCKER_HOST`, missing image, stale lock, failed self-test, unverified cleanup, default routing with Docker unreachable); `test_sandbox_default.py`; `test_sandbox_policy.py` |
| **Resumable `blocked` runs** | Infrastructure and policy failures move the run to `blocked` without committing the node, so no developer repair retry is consumed. `resume` continues once isolation is available. | `test_sandbox_policy.py`, `test_sandbox_default.py`, adversarial 14 |

## What v0.2 does not guarantee

1. **Docker, kernel, and container escape are outside the security
   guarantee.**
   - Containers share the Docker Desktop VM's Linux kernel. A kernel, container
     runtime, or Docker Desktop vulnerability could let code leave the
     container, and that VM can reach the Windows drives.
   - No escape or exploit testing was done.
   - Docker's Enhanced Container Isolation is not used.
2. **Test-verdict forgery is not solved.**
   - Generated code runs inside the tests that judge it. It can make failing
     tests appear to pass, for example by patching `unittest` or printing
     fake results.
   - The sandbox protects the host, not the honesty of the result. The
     reviewer agent is the only other check.
3. **Image builds may contact the registry for metadata.** Builds may have
   made a manifest metadata lookup for the pinned base. No layers were
   pulled, and the base is used only by its independently verified digest
   (see [Base-image provenance](#base-image-provenance)).
4. **Docker Desktop is the only live-tested strong backend.** Only one OS,
   engine, and version has been exercised (see Status above).
5. **Backend health is not persisted across process restart.** A backend
   marked unhealthy in one process is trusted again by a new process once its
   janitor and isolation self-test pass.
6. **The image lock is machine-specific.**
   - `sandbox_image/image.lock.json` names an image ID that exists only on the
     machine where it was built.
   - Elsewhere, DockerBackend is unavailable and untrusted runs block until a
     human rebuilds the image.

Also:

7. **Some limit labels are inferred.**
   - PID exhaustion and part of disk exhaustion are recognised from stderr
     text.
   - A SIGKILL that the orchestrator did not send and that is not an OOM kill
     is attributed to the CPU-time hard limit.
   - Untrusted code can imitate these signals or af-entry's reserved exit
     codes. That only changes how its own failure is labelled, or blocks its
     own run. It cannot lift a limit.
8. **The CPU-time limit applies per process** (RLIMIT_CPU). The total CPU of a
   process tree is bounded by `--cpus` × the wall-clock timeout.
9. **tmpfs usage counts toward the memory limit.**
10. **Code inside the container can see its own environment details:** kernel
    version, Docker VM mount paths in `/proc/self/mountinfo`, and its own
    files. No host files were readable in testing.
11. **`LocalProcessBackend` is not a sandbox.** Trusted fixtures run as
    ordinary local processes with the user's rights, and only time and output
    are bounded there.
12. **Not live-tested:**
    - Staging with real symlinks; creating them needs privileges on the test
      machine. Covered by injected-`lstat` tests and af-entry's live symlink
      rejection.
    - Linux and macOS hosts.
13. **CLI approvals are not authenticated**, unchanged from v0.1. The HTTP
    API (unreleased) authenticates every request
    ([authentication.md](authentication.md)).

## Routing (default backend)

`SandboxManager.default()` holds `[LocalProcessBackend, DockerBackend(approved image)]`.
`build_executor()` and `RunTestsTool` both use it unless a manager is passed in.

| Code | Runs with | If that is not possible |
| --- | --- | --- |
| Trusted fixture (scripted `FakeProvider`, template scaffold) | `LocalProcessBackend`. Docker is never contacted. | Any other eligible backend |
| **Untrusted** (any other provider) | **`DockerBackend` only**, with the image from `sandbox_image/image.lock.json` | **Run `blocked`.** Covers Docker unavailable, unhealthy, misconfigured, remote context, image missing, stale, or not the approved one, and a failed self-test. |

`LocalProcessBackend` is never eligible for untrusted code. The manager
rejects it, even if it were mislabelled, and the backend also refuses
untrusted requests itself.

## Resource limits

| Limit | Default | Mechanism | When exceeded |
| --- | --- | --- | --- |
| Wall-clock time | 120 s | Monotonic host deadline, then `docker kill`; the container and its whole PID namespace are removed | `timed_out` / `time` |
| CPU rate | 1 CPU | `--cpus` (CFS quota) | Throttled. Measured: 4 busy processes got 0.50 CPU at `--cpus 0.5` and 1.00 at `--cpus 1` |
| CPU time | `timeout + 5` s per process, or `cpu_time_s` | RLIMIT_CPU: soft limit sends SIGXCPU; hard limit (soft + 2 s) sends SIGKILL | `limit_exceeded` / `cpu`. Measured: stopped at 1.94 s of CPU for a 2 s limit (exit 152); with SIGXCPU ignored, at 3.99 s for the 4 s hard limit (exit 137) |
| Memory | 512 MiB, including tmpfs | `--memory` = `--memory-swap` | `limit_exceeded` / `memory` (Docker `OOMKilled`) |
| Disk | `/work` 256 MiB, `/tmp` 64 MiB, single file ≤ `/work` size | tmpfs sizes, RLIMIT_FSIZE, read-only root | `limit_exceeded` / `disk` |
| Staged project input | ≤ min(½ `/work`, ¼ memory) in 4 KiB pages | Host-side staging; the rest of `/work` stays writable | Rejected before any container (run `failed`) |
| Processes | 128 | `--pids-limit` | `limit_exceeded` / `pids` |
| Output | 1 MB per stream | Host-side cap; the container is killed on overflow | `limit_exceeded` / `output` |

Limits hit by the code are real test failures: the tester reports `fail` and
the developer gets a repair attempt. Infrastructure failures never count as
test failures.

## Outcome handling

| Situation | Result | Retry consumed? |
| --- | --- | --- |
| No qualifying backend: unavailable, misconfigured, self-test failed, or policy denies | Run `blocked` | No |
| Docker daemon error, container never started, af-entry copy-in failure (exit 120–123) | Run `blocked` | No |
| Cleanup could not be verified | Run `blocked`; backend unhealthy until `recover()` | No |
| Code exceeded time, CPU, memory, disk, PIDs, or output | Tester reports `fail` | Yes |
| Invalid request or unsafe workspace content (links, unsafe names, oversize) | Run `failed`; nothing executed | No |

## Trust

- `ModelProvider.produces_trusted_code` defaults to **False**. Only
  `FakeProvider` sets it to True.
- Each workspace records its trust in `.agent_factory/workspace.json`.
  - Template scaffolds start as `trusted_fixture`.
  - The executor downgrades the workspace to `untrusted` before running any
    node with an untrusted provider.
  - There is no way back to trusted.
  - A missing or corrupt record counts as untrusted.

## Components (`agent_factory/sandbox/`)

| Module | Role |
| --- | --- |
| `types.py` | `ExecutionRequest`, `ResourceLimits` (bounded), `ExecutionResult`, `IsolationLevel`, `CodeTrust`, error hierarchy |
| `validation.py` | Interpreter allowlist (`python`, `python3`), argv bounds, env allowlist (secret-looking names, database and authentication settings, credential-bearing URL values, and JWT-shaped values always rejected), relative working dir, network `none` only |
| `process.py` | Bounded subprocess runner: monotonic deadline, per-stream output cap, kill on overflow |
| `base.py` | `SandboxBackend` ABC; `run_sandboxed()` always calls `destroy()` and raises `SandboxCleanupError` if cleanup is not verified |
| `manager.py` | `SandboxManager`: default routing, isolation gate, no downgrade, test doubles opt-in, janitor + self-test on first use, unhealthy tracking, concurrency limit |
| `local.py` | `LocalProcessBackend`: trusted fixtures only, **not a sandbox** |
| `fake.py` | `FakeSandboxBackend` test double; executes nothing |
| `staging.py` | Copy-in snapshot. Regular files only; symlinks, junctions, reparse points, hardlinks, special files, and unsafe, reserved, non-NFC, or invisible-character names rejected; `fstat` re-check; size/count/depth caps; deterministic tar |
| `docker.py` | `DockerBackend`: hardened `docker create` argv (golden-tested and re-validated before every create), local-endpoint check, subcommand allowlist (no pull/build/run/exec/cp), inspect mapping, verified cleanup, janitor, isolation self-test |
| `config.py` | Reads `sandbox_image/image.lock.json`; missing, invalid, or stale means unavailable |
| `image.py` | Human-only image preparation (CLI); never imported on the agent-run path |

## Sandbox image

| File | Purpose |
| --- | --- |
| `sandbox_image/Dockerfile` | `FROM <digest-pinned base> AS base` then `FROM scratch` + `COPY --from=base / /`, which drops the base image's ENV/CMD/labels. Adds `af-entry`, strips setuid/setgid bits, sets numeric user `10001:10001`, adds a `/work` mount point. No `# syntax=` directive. |
| `sandbox_image/af-entry` | Copy-in protocol v1, described below |
| `sandbox_image/image.lock.json` | The approved image: image ID, digest-pinned base, Dockerfile and af-entry hashes |

af-entry runs these steps, and any failure exits with a reserved code
(120–123) that the host treats as an infrastructure error:

1. Read exactly `--archive-bytes` from stdin, independent of stdin EOF.
2. Verify `--archive-sha256`.
3. Extract regular files only into `/work`: no traversal, links, devices, or
   duplicates; `O_EXCL`/`O_NOFOLLOW`; fixed 0644 permissions.
4. Replace stdin with `/dev/null`.
5. `execvp` the argv (no shell).

### Base-image provenance

**Independently verified.** On 2026-10-02 the maintainer confirmed against
Docker Hub that the official `python:3.12.10-slim` image has index digest
`sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db`.
That exactly matches `base_image_id` in `sandbox_image/image.lock.json`, the
digest the sandbox image is built from.

### Human-run image preparation

Agent runs can never build, pull, or update images. DockerBackend's
subcommand allowlist excludes those operations, and the builder module is
not imported on the agent-run path (enforced by a test).

```bash
# 1. read-only: identify a locally present base image by its full id
py -3.12 -m agent_factory sandbox inspect-base --base sha256:<64 hex>

# 2. HUMAN-INITIATED: build offline (--pull=false, --network none, --no-cache)
py -3.12 -m agent_factory sandbox build-image --base sha256:<64 hex> --confirm

# 3. probe + isolation self-test of the recorded image (one short-lived container)
py -3.12 -m agent_factory sandbox check
```

If the base image is not present locally, `inspect-base` and `build-image`
stop and print the exact digest a human would have to authorize pulling.
Changing the Dockerfile or `af-entry` makes the lock stale, so DockerBackend
becomes unavailable until the image is rebuilt.

### Live findings on Docker Desktop 29.6.1 (WSL2 engine)

| Assumption | Result |
| --- | --- |
| `--log-driver none` with `docker start --attach` | Works; stdout and stderr are captured separately |
| `--ulimit fsize` units | Bytes: writes stop at exactly the limit with `EFBIG` (Python ignores `SIGXFSZ`) |
| `--ulimit cpu` with soft = hard | The kernel sends SIGKILL, not SIGXCPU, so the hard limit is now soft + 2 s |
| `docker create --interactive` stdin semantics | `Config.StdinOnce` is `true`; af-entry does not depend on it |
| tmpfs options | `/work`, `/tmp`: `rw,nosuid,nodev,noexec,size=…,mode=700,uid=10001,gid=10001`; noexec enforced |
| Overhead (create + start/copy-in + inspect + rm/verify) | ~0.6 s median per execution |

## Using a custom manager

```python
from agent_factory.sandbox import SandboxManager

manager = SandboxManager.default()  # what build_executor() uses anyway
executor = build_executor(template, provider, workspace, sandbox=manager)
```

A manager built from explicit backends follows the same rules. Backends
below the required isolation level, and test doubles unless
`allow_test_doubles=True`, are never used for untrusted code.
