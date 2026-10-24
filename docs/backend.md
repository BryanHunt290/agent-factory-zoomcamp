# Local FastAPI backend

**Loopback only.** Start on `127.0.0.1:8000`; the server command refuses
non-loopback bind addresses. Every product request needs a signed access
token from the configured OpenID Connect provider and the operation's
permission ([authentication.md](authentication.md)), but no production
identity provider is configured yet and no deployment has been designed: do
not deploy it, put a public proxy in front of it, or create a tunnel.

This unreleased backend implements the 13 product operations in
[`openapi.yaml`](../openapi.yaml), OpenAPI 3.1.0, version `1.0.0-draft`.
The checked-in contract is the source of truth. FastAPI's `/docs`, `/redoc`,
and `/openapi.json` routes are disabled so there is no competing published
schema. A frontend and development browser sign-in now exist, along with a fake
notification provider and an offline-tested real-model adapter. Public deployment,
production sign-in, and live provider validation remain outstanding. Workers ([workers.md](workers.md)) consume
the decisions the API records.

**Workers are not part of the API.** No route claims, renews, or releases a
run, and none starts, stops, or lists workers. Workers reach runs only
through PostgreSQL (`run_claims`, migrations 0005 and 0006), and never
present user bearer tokens. The API never reads a claim and never changes
who owns a run. Its only write to `run_claims` is the wake-up that commits
with a decision (`RunQueue.wake`: the run is wanted from now on). A run
queued for a worker reports `running` ("in progress"), so the contract stays
truthful without a change.

## Start locally

Use Python 3.12 and the project virtual environment. The optional packages
are pinned in [`requirements-api.txt`](../requirements-api.txt), with the
database driver in [`requirements-postgres.txt`](../requirements-postgres.txt)
and contract validators in [`requirements-dev.txt`](../requirements-dev.txt).
The orchestration core still uses only the standard library.

With the local PostgreSQL instance configured as in
[database.md](database.md#local-development-setup), run in PowerShell:

```powershell
$env:AGENT_FACTORY_DATABASE_URL_FILE = '.local\database.url'
.venv\Scripts\python -m agent_factory db migrate
.venv\Scripts\python -m agent_factory api serve
```

The server also needs an authentication configuration, and refuses to
start without one: the production settings of an identity provider, or,
for local use only, the development issuer
([authentication.md](authentication.md#development-and-test-authentication)).
Principals are enrolled with `auth principal add`.

Migration is an explicit maintenance step; starting the API never applies
migrations. Stop with Ctrl+C to close the pool. `--port` changes the local
port; `--pool-max-size` (default 10) and `--pool-timeout` (default 3 seconds)
bound database borrowing. The server does not need Docker access to serve
the API: it queries PostgreSQL and never launches sandbox jobs.

The local environment contains FastAPI 0.142.2, Pydantic 2.13.4,
Starlette 1.3.1, Uvicorn 0.51.0, psycopg 3.3.6, psycopg-pool 3.3.3, and
HTTPX 0.28.1, and PyJWT 2.15.1 with cryptography 50.0.2 for verifying
access-token signatures (owner-approved). HTTPX drives tests in process. No
framework extensions, ORM, task queue, or frontend framework is required.

## Request lifecycle and package structure

Uvicorn is the web server. It accepts an HTTP connection and presents an
ASGI request to FastAPI. ASGI is the interface between the server and the
Python application; FastAPI supplies routing and dependency injection, and
Pydantic checks the data shapes.

```text
HTTP request -> request ID and Host checks -> authentication and the operation's permission
  -> route and input validation -> service -> domain rules -> repository interface -> PostgreSQL
  -> explicit API view -> response validation -> JSON response and access log
```

| Location | Responsibility |
| --- | --- |
| `api/server.py`, `api/config.py` | Local Uvicorn startup, bind restrictions, trusted configuration |
| `api/app.py` | App construction and lifespan; opens and closes the store provider. An application cannot be built without an authenticator |
| `api/auth/` | Token verification, the provider's keys, principals, `require_permission`, rate limits, configuration, the development issuer ([authentication.md](authentication.md)) |
| `api/routes/` | HTTP methods, paths, response codes and headers; thin handlers |
| `api/schemas/` | Closed request/response models and query validation |
| `api/dependencies.py` | Strict JSON parsing, header checks, service injection |
| `api/services/` | One use case per operation, transactions, explicit domain-to-API views |
| `api/stores.py` | Store-provider interface; in-memory adapter for isolated tests |
| `api/pagination.py` | Signed, expiring, query-bound cursor tokens |
| `api/errors.py`, `api/handlers.py` | Contract error codes and safe problem responses |
| `api/middleware.py`, `api/observability.py`, `api/redaction.py` | Request correlation, Host checks, logs, output sanitization |
| `domain/` | Framework-independent records, state machines and repository interfaces |
| `persistence/pool.py`, `persistence/postgres.py` | Bounded connection pool, SQL, driver-error translation |

Routes declare their dependencies rather than constructing database
connections. A service receives an application context with a store
provider, clock, templates and cursor codec. Tests can inject an isolated
store or a fixed clock without teaching the domain about HTTP. Synchronous
service work runs in FastAPI's thread pool so blocking SQL does not occupy
the ASGI event loop.

Routes contain no SQL, filesystem work, Docker calls, graph execution or
checkpoint mutation. Services depend on repository interfaces. The domain
and engine import neither FastAPI nor a database driver.

## Product operations

| Method | Path | Result |
| --- | --- | --- |
| GET | `/api/v1/projects` | Paginated dashboard summaries |
| POST | `/api/v1/projects` | Durable project creation with `Idempotency-Key` |
| GET | `/api/v1/projects/{project_id}` | One project and its milestone vocabulary |
| GET | `/api/v1/templates` | Available templates |
| GET | `/api/v1/projects/{project_id}/runs` | Runs, newest first |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}` | One run and its curated status |
| GET | `/api/v1/projects/{project_id}/milestones` | Project timeline with filters |
| GET | `/api/v1/projects/{project_id}/runs/{run_id}/milestones` | Run timeline with filters |
| GET | `/api/v1/approvals?status=pending` | Decidable approvals (pending, not expired); optional `project_id` filter |
| GET | `/api/v1/attention` | The attention inbox: decisions to make and uncertain actions to answer; optional `project_id` filter |
| GET | `/api/v1/projects/{project_id}/approvals/{approval_id}` | Exact-action approval view, with what happened to the action (execution and timeline) |
| POST | `/api/v1/projects/{project_id}/approvals/{approval_id}/decision` | Record one decision |
| POST | `/api/v1/projects/{project_id}/approvals/{approval_id}/reconciliation` | Record whether an uncertain action happened |

Each operation requires the permission its `x-permission` names
(`projects:read`, `projects:create`, `runs:read`, `milestones:read`,
`approvals:read`, `approvals:decide`, or `actions:reconcile`). There is no endpoint to start a run, edit a milestone, mutate a checkpoint,
change a run's status, or execute a command. There are two infrastructure
endpoints, documented below, outside `/api/v1`.

## Request and response validation

POST bodies must be UTF-8 JSON objects with `Content-Type:
application/json`, no duplicate keys, no non-JSON numeric constants, and at
most 64 KiB. Pydantic validates strict types, lengths, patterns and closed
field sets. Unknown fields, including a decision's actor or timestamp, are
rejected. Query/path validation uses the contract's enums, ID patterns,
UTC timestamps, filters and limits. Responses pass through explicit API
models, so adding a domain field does not automatically publish it.

Run status messages are curated from a fixed vocabulary, including
`awaiting_reconciliation` ("Paused: an approved action may already have
happened ...") and a run blocked because the record of approved actions is
unavailable. `action_execution` milestones get fixed summaries that name the
tool and never its parameters. Domain metadata, raw engine errors and
internal context are not serialized. Text that can
come from execution is sanitized before display. This is a bounded local
interface, not a general data-loss-prevention system: known-secret and
path-canary tests demonstrate specific cases, not detection of every
possible secret in arbitrary human text.

## Connection pooling and transactions

`PostgresStorePool` uses psycopg-pool with 1 initial/minimum connection and
at most 10 by default. Construction opens nothing. ASGI startup calls
`open()` without waiting for PostgreSQL; shutdown calls `close()`. The
process can therefore report not-ready during a database outage.

A service borrows one connection for its unit of work and returns it when
finished. Repository operations in that unit share the connection and its
transactions. Connections are checked before borrowing. A request that
cannot borrow within the configured timeout receives a safe 503; it does
not open an unbounded extra connection. Multi-query views use a consistent
read snapshot. The CLI's existing single-connection store remains useful
for short-lived commands.

## Durable project creation and workspace identity

Project creation fingerprints the canonical validated body using SHA-256.
The operation scope and key identify an idempotency record. Migration
[`0002_idempotency_keys.sql`](../agent_factory/persistence/migrations/0002_idempotency_keys.sql)
adds durable storage for the fingerprint, original status/body and expiry.
The project and its replay record commit in the same transaction. A unique
key settles concurrent claims; a losing request reads the committed winner
instead of creating another project.

| Request | Response |
| --- | --- |
| New valid key/body | 201 and a new project |
| Same key and body within retention | Original 201/body, `Idempotent-Replayed: true` |
| Same retained key, different body | 409 `idempotency_key_reused`; the stored record is unchanged |
| Missing key | 400 `idempotency_key_required` |
| Key still unresolved by a competing transaction | 409 `idempotency_key_in_use` |

Key reuse with a different body is **409 Conflict** (owner decision): the new
request conflicts with the server state already bound to that key. The first
implementation answered 422; the error code `idempotency_key_reused` is
unchanged. Both idempotency conflicts are 409, so clients branch on `code`:
`idempotency_key_in_use` means retry the same request later,
`idempotency_key_reused` means send the new request with a new key.
Retention is at least 24 hours under the default settings; after expiry a
key may create a new project. This is retry protection within that window,
not a permanent exactly-once promise. PostgreSQL storage survives API
restart and reconnection. The full incoming request body is not saved as
an idempotency record; the fingerprint and public response are sufficient.

Creation records a project only; it does not create directories or start a
run. `workspace_for_project` binds a future workspace to the existing
project ID, using an ID-derived directory under the trusted workspace
base. Existing markers cannot silently change identity. Names remain
display labels, never path components. See [product-domain.md](product-domain.md).

## Cursor pagination

Collections use keyset queries, fetching `limit + 1` rows to discover
whether another page exists. Limits are 1-100, default 50; no total count
is returned.

| Collection | Stable ordering key |
| --- | --- |
| Projects | `(created_at, id)`, descending |
| Runs | Run number, descending within a project |
| Milestones | `(occurred_at, run_id, sequence)`, ascending |
| Pending approvals | `(requested_at, project_id, id)`, ascending |

Cursors contain the operation, normalized filter digest, final row's key
and expiry, authenticated with HMAC-SHA256. They are opaque client tokens,
not offsets. Signing detects modification; it does not encrypt the payload.
No credentials belong in it. Reusing a cursor with different filters or a
different operation, changing its bytes, or using it after expiry returns
400 `invalid_cursor`. Page size may change between requests.

By default a random signing key lives only in the API process, so restarting
invalidates outstanding cursors. Configure
`AGENT_FACTORY_API_CURSOR_SECRET_FILE` with a protected file of at least
32 bytes outside every workspace to retain cursors across restarts. The
default cursor lifetime is 24 hours. This key never enters a sandbox.

Milestone filters use `since <= occurred_at < until`, repeated status and
category filters, and project/run scope. A changed milestone can move to a
later page because its `occurred_at` changes; clients keep the latest copy
per ID. Pagination is not a snapshot across separate HTTP requests.

## Approval decisions

The service passes the expected fingerprint to the existing domain
approval service. The database serializes competing writes and stores
the request status and decision atomically. A first valid decision returns
201; an identical retry returns 200 with the original decision. A different
decision, wrong fingerprint, expired request, or cancelled request returns
409. The clock and the actor are server-controlled: the actor is the
authenticated principal (`kind: user`, `verified: true`), never anything the
client sends. An identical retry by another principal is 409, not 200. Only a
principal holding `approvals:decide` reaches this code, at most 30 decisions
a minute (429 beyond that).

The API stops after recording. It never applies a checkpoint, resumes the
graph or executes the action. In the same transaction as the decision it
wakes the run (`RunQueue.wake`, a transactional outbox), so a worker claims
the run within its poll interval and applies the decision under the run's
claim, after re-checking the request, the exact pending action, the
fingerprint, the expiry, the decider, and its own ownership
([workers.md](workers.md#human-decisions-wake-ups-and-consumption)). An
approved database row alone is never execution authority.

**Expiry.** Every approval request expires 12 hours after it was requested
(`domain/approvals.py`, `APPROVAL_VALIDITY`). The API answers 409
`approval_expired` from the expiry instant on, the inbox stops listing the
request, its view shows `status: expired`, and a project's
`pending_approvals` stops counting it. The worker applies the expiry on time
too, and an approval that was decided in time must still be *used* before
its expiry ([api-contract.md](api-contract.md#approval-decisions-security-idempotency-concurrency)).

## Reconciliation, the timeline, and the inbox

**`reconcileAction`** records a person's answer about an approved action
whose outcome is unknown. The service (`api/services/approvals.py`) checks,
in one transaction:
1. the approval exists in this project, and its exact action is safe to
   show (otherwise 500, nothing recorded: the same rule as the approval view);
2. the body's `action_fingerprint` is the approval's (409
   `action_fingerprint_mismatch`), and its `execution_id` is the approval's
   execution, derived from the approval itself (409 `reconciliation_stale`);
3. then the domain's `ReconciliationService`: the execution exists and is
   uncertain (409 `reconciliation_not_required`), the attempt is the one now
   waiting (409 `reconciliation_stale`), and the attempt has no other answer
   (an identical repeat by the same person is 200; anything else is 409
   `reconciliation_already_decided`);
4. it records the answer with server time and the authenticated principal,
   and wakes the run.

It changes no execution: a worker applies the answer through the execution
ledger, and for "try once more" that is a new 15-minute authorization from
the person's decision. Only `actions:reconcile` reaches this code
(`reconciliation_quota`): `approvals:decide` does not imply it. Decisions and
answers share one per-principal limit, 30 a minute by default, so
alternating between them does not double it. The limit is per API process
(in memory): with several API processes each would allow 30; a shared limit
needs shared state, which is future work.

**The phone view.** `getApproval` adds what the brief's phone screen needs
to the approval: `action_summary` (tool and category, never parameters, for
lists), `attention` (`decision_required`, `reconciliation_required`, or
null), `execution` (state, latest attempt, an open retry window, the latest
answer and whether it was applied), and `timeline`. All of it is built in
one read snapshot from structured records (`domain/timeline.py`): the
approval, its decision, the execution ledger's attempts, recorded answers,
and applied reconciliations. Never from the execution log. The exact action
is shown in full, because the approver must see what they approve; if any
parameter looks sensitive (a known secret, a path, a secret-like key), the
whole approval is refused with 500 rather than shown redacted, because a
redacted action cannot be approved exactly (E3 rule, unchanged). The
attempt's internal detail (an exception type), paths, and worker hosts are
never shown: a worker appears only as its random `wkr-` ID and fencing
generation.

**`listAttention`** is one keyset-paginated list across projects, ordered by
when each item began needing a person. It is a `UNION ALL` of two partial
indexes (migration 0006): pending, unexpired requests, and uncertain
executions without an answer for their current attempt.

## Model usage

`getRun` and `listRuns` include `model_usage`: the run's model calls,
failed calls, input, output, and cached tokens, and each provider/model.
It is read from `run_model_usage` (migration `0008`), which the recorder
fills from the checkpoint's usage summary. It holds counts only.
`estimated_cost_usd` is `null` unless prices are configured; it is never an
invoice. The API never calls a model. Tests: `tests/test_api_model_usage.py`
([model-providers.md](model-providers.md)).

## Notification status

`getApproval` and `listPendingApprovals` read the approval's notification
rows (`ProductStore.notifications.list_for_approval`) in the same snapshot as
the approval and summarize them per attention event
(`domain.notifications.summarize`, `views.notifications`). Only states,
counts, times, and closed-vocabulary failures reach the response. The API
never writes a notification and never calls a provider: intents are written
by the recorder in the attention event's own transaction, and a separate
dispatcher process sends them ([notifications.md](notifications.md)). Tests:
`tests/test_api_notifications.py`.

## Errors, request IDs and logging

Product errors use the contract's RFC 9457 `application/problem+json`
format: `type`, `title`, `status`, safe `detail`, `instance`, stable `code`
and `request_id`. Validation adds field locations and safe explanations;
conflicting decisions may include the current public approval view.
Raw exceptions, stack traces, database DSNs and internal filesystem paths
are not returned. Storage failures map to 503 with `Retry-After`; unexpected
errors map to a generic 500. 401 (`authentication_required`,
`invalid_token`), 403 (`forbidden`, naming the required permission), 429
(`too_many_requests`), and 503 (`authentication_unavailable`) are described
in [authentication.md](authentication.md#what-a-client-sees-401-403-429-503).

Every HTTP request gets a new server-generated `req_...` ID, including
rejected requests and infrastructure checks. It is returned as
`X-Request-Id`, included in problem bodies, and propagated through request
logs. Incoming client IDs do not replace it.

Server logs are JSON lines on stderr. Access entries contain UTC `ts`,
request ID, method, matched route template, status, `duration_ms`, and the
authorization outcome: `principal_id`, the `permission` checked, and a safe
`auth_result` (`allowed`, `forbidden`, `expired`, ...). They omit
request/response bodies, headers (the `Authorization` header among them),
query strings and environment dumps. Known process secrets,
credential-bearing URLs, and anything shaped like an access token are
redacted.
HTTP logs are separate from the engine's append-only, hash-chained execution
log; the API never writes that execution log.

## Liveness and readiness

| Endpoint | Meaning | Response |
| --- | --- | --- |
| `GET /healthz` | The process/event loop can answer; does not contact PostgreSQL | 200, `{"status":"ok"}` |
| `GET /readyz` | A connection can be borrowed and the migration schema is current | 200, `{"status":"ready"}`, or 503 with `status: not_ready` and a bounded reason |

Readiness borrows with a short timeout. Failure reasons are
`database_unavailable` or `schema_not_current`, with `Retry-After: 5`;
they do not reveal credentials or server details. These are infrastructure
responses, separate from the product contract. A database outage should
remove readiness without suggesting that restarting a healthy API process
will repair PostgreSQL.

## Local-only

The CLI binds to `127.0.0.1` by default and refuses non-loopback addresses.
Uvicorn proxy headers are disabled. The app restricts accepted Host values,
does not enable wildcard CORS, and requires JSON for writes. Every product
request also needs a valid access token. Public exposure still needs a
deployment design (TLS, a configured identity provider, a review), so do not
forward the port or expose it through a reverse proxy.

**The dashboard needed no API change.** Dashboard v1 ([frontend.md](frontend.md))
reaches the API through its own frontend server, which serves the bundle and
proxies `/api/`, `/healthz`, and `/readyz` to the API on loopback (it refuses
any other target). The browser therefore sees one origin, the API still has
no CORS configuration, and its Host check is unchanged: the proxy presents
the API's own loopback host. The API cannot tell the dashboard from any other
client and trusts it no more: every request carries a bearer token and is
authorized like any other.

## Verification and remaining gaps

Two independent checks matter: the document is valid OpenAPI, and real
HTTP/ASGI responses implement that document. Both suites exist:

| Question | Evidence |
| --- | --- |
| Is the contract valid and safe? | `tests/test_openapi_contract.py` (unchanged) |
| Does the running app implement exactly the contract? | `tests/test_api_conformance.py`: the route set, and per operation the operationId, parameters, success status and response model; every API model against its schema (fields, required set, closed); undocumented methods are 405 |
| Does every real response conform? | `tests/api_support.py` `ContractClient` validates each HTTP response's status, media type, body and documented headers against `openapi.yaml`, and every error against `Problem` |
| Does it work on PostgreSQL? | `tests/test_api_http.py` runs every scenario against memory and, with `-m postgres --run-postgres`, against a fresh migrated schema through `PostgresStorePool`: idempotency races and restart, ordering and pagination, approvals and concurrent decisions, canaries. Pool exhaustion and unmigrated-schema readiness are in `test_api_conformance.py`. |
| Do reconciliation, the inbox, the timeline, and expiry work? | `tests/test_api_human_decisions.py` (both stores): binding and stale answers, identical repeats and conflicts, concurrent answers, server-controlled fields, permission separation in both directions, the shared limit, wake-ups committed with decisions, expiry everywhere it shows, the inbox's order and cursors, timeline content, and disclosure canaries |
| Does an HTTP decision reach the action? | `tests/test_e2e_human_decisions.py` (both stores): an authenticated person decides or answers over HTTP, a real worker claims the run and applies it, and the oracle counts the effect |
| Does the API stay out of execution? | `test_api_conformance.py::test_the_api_records_intent_and_never_reaches_execution_machinery`: no API module imports the engine, tools, sandbox, workers, or agents, or names the ledger's, checkpoints', or claims' writers |
| Does the real server bind to loopback? | `api serve` was run under Uvicorn against the local database. It listened on `127.0.0.1` only, `/readyz` answered ready, and a request with a foreign `Host` header was refused with 400. Tests also cover the CLI default and the refused public bind. |
| Does a real browser work against it, and survive failures? | `tests/test_dashboard_browser.py`, `test_dashboard_e2e.py`, and `test_dashboard_reliability.py` (`-m browser --run-browser --run-postgres`): the real `api serve` (development authentication, a fresh test schema), the frontend server, real workers, and Chrome; including the API killed and restarted, PostgreSQL stopped and started, and requests refused, timed out, dropped after the API answered, and delivered twice ([dashboard-reliability.md](dashboard-reliability.md)) |
| Does the real server authenticate? | `api serve` ran under Uvicorn in development mode against a throwaway test schema: no token 401 `authentication_required` with `WWW-Authenticate`, a refused token 401 `invalid_token`, the owner 200, a reader creating a project 403 naming `projects:create`, the owner 201, `/healthz` 200 without a token; no token in the server log. Without an authentication configuration it refuses to start (tested). |

Manual mutation checks: removing the cursor filter binding, the
idempotency body comparison, or the closed request models each made a test
fail. See [testing.md](testing.md#http-api-tests-unreleased) for commands.

A configured production identity provider, production browser sign-in, run
requests through the API, notifications (push, SMS, email),
production configuration, distributed rate limits, deployment and CI remain
future milestones. An HTTP decision response shows only that the decision was
recorded; the approval's `execution` and `timeline` show what a worker did
with it afterwards.
