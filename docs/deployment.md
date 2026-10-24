# Deployment preparation

Prepared 2026-10-23 for the Zoomcamp submission. **This is a source-based
delivery plan, not a deployed service or an authorization to provision one.**
The reproducible local reviewer environment is the submission's current runnable
target. Its development issuer, loopback API and fake model/notification providers
remain local. No public URL, production identity, cloud resource or CD run is
claimed by this document.

The owner's Day 2 scope is reviewer setup, startup, CI and deployment preparation.
The existing [architecture boundaries](../AGENTS.md),
[authentication design](authentication.md), [sandbox](sandbox.md) and
[AWS capability limits](aws-capabilities.md) remain authoritative. The runtime
capability named `aws.deploy.execute` has no implementation; this plan does not
activate it. The [AWS deployment safety skill](skills/aws-deployment-safety/SKILL.md)
informs the offline preparation below.

## Delivery artifacts and current evidence

| Artifact | What it supplies | What still has to be demonstrated |
| --- | --- | --- |
| [Reviewer launcher](../scripts/reviewer.py) | Local setup/start/test entry point, isolated from the owner's existing development data | Fresh-machine results recorded with exact commands and dependency versions |
| [CI workflow](../.github/workflows/ci.yml) | PR/push/manual Python, frontend, PostgreSQL and real-browser checks; JUnit and frontend candidate artifacts | An actual successful hosted run on the submitted commit |
| Existing authentication modules | Signed access-token verification, principal permissions and development/production separation | A configured production provider and browser sign-in |
| Existing Docker execution sandbox | Fail-closed isolation for untrusted code | A reviewed, locked sandbox image on the future runtime host |
| This runbook | Target architecture, release gates, state handling and rollback | Approved target, budget, infrastructure implementation and deployment acceptance |

CI is **continuous integration only**. There is no automatic deployment job,
cloud credential, write-scoped repository token, container registry publication,
or `pull_request_target` execution. The fixed CI database password is a synthetic
fixture for the disposable loopback service, never a runtime credential. Each
database test uses a new schema in `agent_factory_ci_test`.

The workflow uses Python 3.12.10, Node 24.18.0, Ubuntu 24.04 and the PostgreSQL 17
image digest already recorded in [database.md](database.md). Node 24.18.0 satisfies
the locked frontend dependency tree; the older broad "Node 20+" guidance is
insufficient for all current transitive dependencies. The installed Chrome on
the hosted runner is checked before Playwright uses `channel="chrome"`; no
browser download is added. This browser version and the hosted runner image
can change, so they are logged dependencies rather than an immutable OS image.

The PostgreSQL and browser lanes explicitly exclude `docker_live`. The restart
tests intentionally require a named Agent Factory development container, and
the sandbox boundary tests require its separate locked execution image. These
remain local opt-in evidence in [Day 1 validation](zoomcamp/DAY1_VALIDATION.md).
Stress, live model, live cloud and hostile sandbox suites are also outside CI.
Installing the pinned AWS SDK enables its existing offline Stubber tests; it
does not configure or authorize an AWS call.

Action references are immutable commits verified against official releases:

- [checkout v7.0.1](https://github.com/actions/checkout/commit/3d3c42e5aac5ba805825da76410c181273ba90b1)
- [setup-python v7.1.0](https://github.com/actions/setup-python/commit/6d4fd22b20465ccc0f7354476588e8df7c9ca0e0)
- [setup-node v7.1.0](https://github.com/actions/setup-node/commit/949feb2413d6458794dcd2491c4babbbce0c15c1)
- [upload-artifact v7.0.2](https://github.com/actions/upload-artifact/commit/cf430e030ddbb5b0abf93d22962f4752f3646cd9)

The [official Ubuntu 24.04 runner inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)
lists installed Chrome at preparation time. Setup actions select the exact
Python/Node versions used in the verified local environment. A local validation
or YAML parse is not a substitute for a hosted CI run.

## Proposed first deployment: one host with durable state

Keep the first runtime on one Linux x86-64 host, matching the implemented worker
and checkpoint ownership model. An AWS EC2 host in `us-west-2` is the proposed
target, consistent with the project's AWS-first roadmap. The owner must choose
the account, domain, identity provider, instance and budget before provisioning.
No default AWS profile, credentials file or account has been inspected.

Use one instance with at least two vCPUs and 4 GiB RAM as the sizing hypothesis;
prove it with a single-run load check before accepting it. Store PostgreSQL and
workspace/checkpoint files on an encrypted durable block volume distinct from
release code. Begin with one API process and one worker. Do not introduce Lambda,
multiple worker hosts or an autoscaling group: they do not preserve the current
host-local workspace lock and sandbox assumptions without further design.

The proposed request path is a TLS reverse proxy on the same host, serving a
built frontend and forwarding same-origin `/api/` requests to the API at
`127.0.0.1:8000`. PostgreSQL is host-private. Workers expose no listener and use
database infrastructure identity rather than a browser bearer token. The proxy
must never have the Docker socket. Only the dedicated trusted worker service can
reach the local Docker daemon for the locked execution sandbox.

**That public proxy is not implemented or enabled by Day 2.** The current API
rejects foreign Host headers, trusts no forwarded headers, and has process-local
rate limits. The frontend has a development-token sign-in form. A public proxy
configuration must be reviewed with these boundaries; simply rewriting Host or
putting a tunnel in front of development mode is not an approved deployment.
The Vite preview server is a local reviewer convenience, not the production
web server.

Proposed host layout, to be created only in an approved deployment:

```text
/opt/agent-factory/releases/<git-sha>/    immutable source, venv and frontend bundle
/opt/agent-factory/current              active release pointer
/var/lib/agent-factory/postgres/        durable PostgreSQL data
/var/lib/agent-factory/workspaces/      checkpoints, action logs and project files
/var/lib/agent-factory/backups/         restricted staging for paired recovery backups
/etc/agent-factory/                    root-owned runtime configuration
/run/agent-factory/secrets/            service-readable secret files, never source
```

Use separate operating-system users for the proxy, API and worker. Split database
roles so the API records intent, the worker claims and writes progress, and a
maintenance role alone performs migrations and principal enrollment. The current
local database credentials are not a production role model. Backup access must
be distinct from application write access. Keep development keys, tokens,
workspaces and `.local/` entirely out of release archives.

## Production identity and exposure gates

These are implementation/verification gates, not switches to turn on today:

1. Choose an OIDC provider and create its application/API configuration through
   an explicitly approved account action. Verify its issuer, audience, signing
   algorithm, access-token format and JWKS rotation. Standard `aud` access tokens
   fit the existing verifier; provider-specific claims need a reviewed code
   change and tests before use.
2. Implement the browser authorization flow and logout. If sessions use cookies,
   add Secure/HttpOnly/SameSite handling and CSRF protection. The current manual
   development-token form is not production sign-in.
3. Configure the API with `AGENT_FACTORY_AUTH_MODE=production`,
   `AGENT_FACTORY_AUTH_ISSUER`, `AGENT_FACTORY_AUTH_AUDIENCE`,
   `AGENT_FACTORY_AUTH_JWKS_URL` and `AGENT_FACTORY_AUTH_ALGORITHMS` with an
   explicit permitted asymmetric algorithm.
   Leave `AGENT_FACTORY_AUTH_DEV_ISSUER_DIR` unset. Provision database and cursor
   secret file paths using `AGENT_FACTORY_DATABASE_URL_FILE` and
   `AGENT_FACTORY_API_CURSOR_SECRET_FILE`; never put values in artifacts or logs.
4. Enroll named reviewer principals with only needed permissions. Prove that
   missing/expired/wrong-issuer tokens fail, insufficient permissions fail, and
   the permission check occurs before record existence is disclosed. Verify the
   production configuration rejects development tokens.
5. Review TLS termination, allowed Host values, same-origin routing, proxy
   provenance and rate-limit client identity together. Add acceptance tests
   before changing any bind/Host boundary. Keep the API bound to loopback.
6. Leave real models, cloud tool adapters, purchasing and real notifications
   disabled for the first demo unless separately authorized. A working fake
   provider demo is sufficient to exercise the application's workflow.

The owner also needs a concrete cost proposal before activation: instance-hours,
durable-volume size, backups, transfer, DNS/TLS, logs and identity-provider costs.
No current provider prices or account quota are assumed. Record an approved
monthly ceiling, review date, storage/log retention and a shutdown procedure.
Limit instance count to one, worker concurrency to one, sandbox resource limits
to the existing policy, and all paid provider calls to disabled. Budget alarms
are notifications rather than a hard spending cap.

## Release and acceptance runbook

The following sequence is for the later approved deployment. Until all preceding
gates pass, use the local reviewer environment and leave public deployment marked
incomplete in the submission checklist.

1. Select an exact submission commit. Require all three CI jobs to pass on that
   SHA, inspect the JUnit reports and store the hosted run URL. Record dependency
   versions, frontend artifact digest, database migration identifiers and locked
   sandbox image digest. Do not release a working-tree snapshot with local keys.
2. Build a release candidate from that commit on a clean machine, using the pinned
   requirement files and `npm ci`; run `npm run check` and `npm run build` in
   `frontend/`. Keep a manifest containing the commit, input file hashes and
   output hashes. The source checkout, API dependencies and compiled frontend
   must describe the same commit. Do not use a floating branch as a release ID.
3. Validate production configuration without public ingress. Confirm a real
   provider token verifies and a development token fails. Test the chosen login
   flow, principal enrollment and JWKS failure/rotation behavior. This entails
   real identity-provider access and waits for the approved deployment stage.
4. Stop new run submission, let the worker finish its current safe step, then
   stop the worker. Preserve any uncertain action in reconciliation; never mark
   it successful to make a release proceed. Confirm there is no active claimed
   execution before a consistent database/workspace backup.
5. Back up PostgreSQL and the matching checkpoint/workspace directories together.
   Record their common release/state checkpoint and hashes. Restore the pair to
   a separate location and verify it before relying on the backup. A database
   backup alone cannot reconstruct host-local action/checkpoint state.
6. Run the existing migration command explicitly under the maintenance role:
   `python -m agent_factory db migrate`. The API never migrates on startup.
   Check migration checksums and stop on a conflict. Never edit an applied
   migration or automatically roll one back.
7. Activate the candidate release pointer, start one API and then one worker,
   and perform private checks before enabling the reviewed TLS endpoint. Verify
   `/healthz`, `/readyz`, an authenticated project read, a new fake-provider
   calculator run, an exact-action approval and a recovery/reconciliation path.
   Confirm the UI and API record agree; an approval record is not proof of
   execution. Keep the workspace, database and cursor key persistent across a
   process restart.
8. Inspect redacted structured logs and the database audit records. Fail the
   release for credential exposure, authorization bypass, duplicate execution,
   migration mismatch, lost ownership or unavailable sandbox. Record latency,
   error rate and worker/queue behavior; deployment acceptance needs actual
   observations, not only successful process startup.
9. Only after those checks pass, enable the reviewed public route for the scoped
   reviewer accounts. Record the URL, deployed commit, acceptance evidence and
   rollback target. Capture the demo without token/key screens or private data.

A later CD workflow may consume that exact tested artifact through a protected
GitHub environment with human approval and a scoped temporary infrastructure
identity. It must not deploy pull request code, grant the runtime a generic cloud
administrator, or rebuild a different artifact after approval. The current CI
workflow has no deployment permission or cloud identity.

## Rollback and failure handling

Keep the previous release and the paired backup until the new release's acceptance
window completes. The operator records the reason, active SHA, migrations and
whether any external action may have started before choosing a recovery path.

- **Application-only rollback:** if the database schema and checkpoint format
  are backward compatible, disable new submission, stop workers safely, switch
  the release pointer to the previously accepted SHA, and restart the API and
  worker. Repeat readiness, authentication and a bounded fake-provider workflow.
  Reuse the existing database, workspace and cursor key; do not reset containers.
- **Incompatible state/migration:** keep the service closed. Restore the paired
  database/workspace backup into separate recovery locations and validate it
  before switching. Preserve the failed candidate's state for reconciliation.
  Do not overwrite the only copy or run a destructive migration downgrade.
- **Possible action already dispatched:** never rewind state and automatically
  retry it. Preserve the execution ledger and require the existing reconciliation
  path. Restoring a backup does not undo an external effect, even if a later
  deployment eventually enables real adapters.
- **Identity or TLS failure:** leave public ingress closed. A fallback to the
  development issuer, wildcard CORS or a public API bind is not a recovery plan.
- **Sandbox failure:** stop advancing untrusted-code work. Repair/revalidate the
  sandbox; the local process backend is not a fallback for untrusted code.

Do not delete volumes, prune Docker, unregister WSL, recreate existing development
containers, or copy the owner's development database into a public demo as part
of rollout or rollback. Local development data is outside the future release's
scope.

## What remains before submission

Verify the clean reviewer command and the exact submitted commit first. Then
publish the authorized source/CI changes, obtain a passing hosted run, and record
its URL. Public deployment, production identity, login, remote runtime validation,
CD, operational metrics/traces and alerting remain separate uncompleted work until
implemented and tested. The course's deployment points must not be claimed from
this preparation document alone.
