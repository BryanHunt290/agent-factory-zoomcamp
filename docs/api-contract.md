# API contract (v1, draft)

| | |
| --- | --- |
| Status | **Draft contract implemented by the local FastAPI backend**, with authentication and per-operation permissions (branch `feature/auth-worker`, unreleased; loopback only) |
| Contract | [`openapi.yaml`](../openapi.yaml) (OpenAPI 3.1.0, `info.version` 1.0.0-draft) |
| Tests | [`tests/test_openapi_contract.py`](../tests/test_openapi_contract.py): document validity, safety and examples; runtime HTTP/conformance and PostgreSQL integration are described in [testing.md](testing.md) |
| Not included | Frontend, browser sign-in, a configured production identity provider, notifications, deployment, automatic worker decision consumption |

## Contents

1. [What an API is, and why Agent Factory needs one](#what-an-api-is-and-why-agent-factory-needs-one)
2. [Contract-first development and OpenAPI](#contract-first-development-and-openapi)
3. [Resources and endpoints](#resources-and-endpoints)
4. [The primary experience, end to end](#the-primary-experience-end-to-end)
5. [HTTP methods, and what is deliberately absent](#http-methods-and-what-is-deliberately-absent)
6. [Status codes](#status-codes)
7. [Errors](#errors)
8. [Approval decisions: security, idempotency, concurrency](#approval-decisions-security-idempotency-concurrency)
9. [Reconciliation, the execution timeline, and the attention inbox](#reconciliation-the-execution-timeline-and-the-attention-inbox)
10. [Idempotency](#idempotency)
11. [Pagination](#pagination)
12. [Filtering](#filtering)
13. [Time](#time)
14. [Versioning](#versioning)
15. [API model, domain model, database row](#api-model-domain-model-database-row)
16. [Workers and the API](#workers-and-the-api)
17. [Authentication and permissions](#authentication-and-permissions)
18. [Design decisions](#design-decisions)
19. [Testing the contract](#testing-the-contract)
20. [Implementation status and remaining gaps](#implementation-status-and-remaining-gaps)

## What an API is, and why Agent Factory needs one

An **API** (application programming interface) is the agreed way for one
program to ask another for data or actions. A **web API** does it over HTTP:

- the client sends a request (a method, a path, maybe a JSON body);
- the server answers with a status code and, usually, a JSON body.

Agent Factory has a command-line tool and a local HTTP backend. The product
still needs:

- a **dashboard** that shows projects and overnight progress;
- a **phone screen** for approving a risky action from anywhere;
- **workers** that run the agents somewhere else.

These pieces run in different places, so they need one shared, stable way to
talk to each other. That is the API. The contract milestone defined it;
the backend milestone implements it without adding a UI or remote access.

## Contract-first development and OpenAPI

**Contract-first** means writing the API's exact shape before any server
code: paths, inputs, outputs, and errors. It has several benefits:

- The frontend and backend can then be built in parallel against the same
  promise.
- Disagreements are cheap to fix while the API is still just a document.
- The domain model came first ([product-domain.md](product-domain.md)) and
  the database next ([database.md](database.md)). The contract therefore
  describes things that already exist.

**OpenAPI** is the standard, machine-readable format for such a contract:

- [`openapi.yaml`](../openapi.yaml) uses **OpenAPI 3.1**, whose schemas are
  standard JSON Schema 2020-12.
- Tools can render it as documentation, generate client code, and validate
  requests and responses against it.
- The tests here already check examples against it.

## Resources and endpoints

REST organizes an API around **resources**: things with stable IDs and URLs,
acted on through standard HTTP methods. The resources are the existing
domain entities, plus templates:

| Resource | Identified by | URL |
| --- | --- | --- |
| Project | `prj-…`, random, never a path | `/api/v1/projects/{project_id}` |
| Run | `run_id`, unique **within its project** | `/api/v1/projects/{project_id}/runs/{run_id}` |
| Milestone | listed per project or per run | `…/milestones` |
| Approval request | `apr-…`, unique **within its project** | `/api/v1/projects/{project_id}/approvals/{approval_id}` |
| Approval decision | at most one per request | `…/approvals/{approval_id}/decision` |
| Reconciliation | at most one answer per uncertain attempt of the approval's action | `…/approvals/{approval_id}/reconciliation` |
| Attention inbox | what needs a person now, across projects | `/api/v1/attention` |
| Template | name | `/api/v1/templates` |

Runs and approvals are **nested under projects** because their IDs are only
unique within a project. The engine's approval ID is a hash of run, node,
visit, tool, and parameters, so two projects running the same workflow can
produce the same one.

| operationId | Method and path | What it is for |
| --- | --- | --- |
| `listProjects` | `GET /api/v1/projects` | Dashboard: each project's status, latest run, pending approvals, and activity |
| `createProject` | `POST /api/v1/projects` | Create a project from a template |
| `getProject` | `GET /api/v1/projects/{project_id}` | One project: current status and milestone vocabulary |
| `listTemplates` | `GET /api/v1/templates` | The template names `createProject` accepts |
| `listRuns` | `GET /api/v1/projects/{project_id}/runs` | A project's runs, newest first |
| `getRun` | `GET /api/v1/projects/{project_id}/runs/{run_id}` | One run's status, and what it is waiting for |
| `listProjectMilestones` | `GET /api/v1/projects/{project_id}/milestones` | Progress across runs; the overnight view |
| `listRunMilestones` | `GET /api/v1/projects/{project_id}/runs/{run_id}/milestones` | One run's timeline |
| `listPendingApprovals` | `GET /api/v1/approvals?status=pending` | The approval inbox, optionally for one project |
| `getApproval` | `GET /api/v1/projects/{project_id}/approvals/{approval_id}` | The approval screen |
| `decideApproval` | `POST /api/v1/projects/{project_id}/approvals/{approval_id}/decision` | Approve or reject one exact action |
| `listAttention` | `GET /api/v1/attention` | Everything that needs a person: decisions to make and uncertain actions to check |
| `reconcileAction` | `POST /api/v1/projects/{project_id}/approvals/{approval_id}/reconciliation` | Say whether an approved action with an unknown outcome happened |

How the required read operations are covered:

| Required read | Answered by |
| --- | --- |
| 1. List projects | `listProjects` |
| 2. Get one project | `getProject` |
| 3. Runs of a project | `listRuns` |
| 4. Get one run | `getRun` |
| 5. Milestones of a project | `listProjectMilestones` |
| 6. Milestones of a run | `listRunMilestones` |
| 7. Milestones by time range | `since` and `until` on either milestone listing |
| 8. Overnight-summary information | `listProjectMilestones` with a time range, plus `getProject` (status) and `listAttention` (what still needs a person) |
| 9. Pending approvals | `listPendingApprovals` |
| 10. One approval request | `getApproval` |
| 11. Current status of a project or run | `status` on `getProject`, `listProjects`, and `getRun` |

## The primary experience, end to end

The example data in `openapi.yaml` follows this story. The user is in New
York (EDT, UTC-4), so "11:42 PM" is `03:42Z`.

**1. The dashboard.**
`GET /api/v1/projects?activity_since=2026-10-02T02:00:00.000000Z` returns,
for each project:
- `status`, which shows "Running" or "Completed";
- `pending_approvals`, which shows "1 approval waiting";
- `activity.milestones`, which shows "7 milestones overnight".

One request draws the whole dashboard.

**2. Overnight progress.**
`GET /api/v1/projects/prj-…/milestones?since=…T02:00:00Z&until=…T11:00:00Z`
returns seven milestones in time order:

- plan ready;
- implementation ready;
- tests **failed**;
- implementation ready, whose `follows` is the failed test step: a
  **repair**;
- tests passing, whose `resolves_milestone_id` points at the failure;
- review approved;
- human decision **blocked**, whose `approval_id` links to the approval.

The client turns these into "Plan completed", "Tests failed", "Repair
completed", and so on, and shows times in local time.

**3. The approval.**
`GET /api/v1/projects/prj-…/approvals/apr-…` returns what the approval
screen shows:
- the project, agent, and node;
- the exact action and its fingerprint;
- the reason;
- the plain-language risk;
- the request time.

**4. The decision.** The user taps APPROVE, and the phone sends:

```http
POST /api/v1/projects/prj-…/approvals/apr-…/decision
{"outcome": "approved", "action_fingerprint": "sha256:f0fd…", "comment": "Approved from mobile"}
```

The server answers `201 Created` with the approval, which now has
`status: approved` and an embedded decision. A worker later applies the
decision; see [Workers and the API](#workers-and-the-api).

## HTTP methods, and what is deliberately absent

| Method | Meaning | Used for |
| --- | --- | --- |
| GET | Read. Safe (changes nothing) and repeatable. | Every read |
| POST | Create or submit. Not idempotent by itself. | `createProject`, `decideApproval` |
| PUT, PATCH, DELETE | Replace, modify, delete | **Not used anywhere in v1** |

**Absent by design.** Easy CRUD on every table would undo the safety model,
so these are not offered:

| Not offered | Why |
| --- | --- |
| Editing or deleting milestones (e.g. `PUT /milestones/{id}` to change "failed" to "completed") | Agent Factory produces milestones from what really happened. They are a record, not a form. |
| Starting, stopping, resuming, or editing runs; editing graph state or checkpoints | Workers own execution. A client could otherwise skip steps or approval gates. |
| Executing commands, scripts, or sandbox jobs | The sandbox runs only code produced by the workflow, under its own controls |
| Changing an approval request, its action, or its fingerprint | The fingerprint is how a decision is bound to one exact action |
| Setting a decision's time or actor, or marking an identity as verified | The server decides these; the actor is the authenticated principal |
| Creating principals or changing permissions | Identity administration is a local CLI task for the owner, never an HTTP operation |
| Deleting projects | Not needed yet; history should not silently disappear |
| Reading credentials, sandbox environments, file paths, or Docker internals | No resource carries them; the tests enforce it |

`tests/test_openapi_contract.py` pins all of this:

- **The surface is fixed.** The complete set of operations is pinned, so
  any new endpoint is a deliberate contract change.
- **The only writes** are `createProject` and `decideApproval`.
- **Run and milestone paths** are GET-only.
- **No graph or command routes:** no path contains segments such as
  `state`, `checkpoint`, `command`, `shell`, or `sandbox`.
- **Request fields are limited** to `name`, `template`, `description`,
  `outcome`, `action_fingerprint`, and `comment`.

## Status codes

| Code | Meaning here |
| --- | --- |
| **200 OK** | A read succeeded; or a decision that was already recorded was repeated (a safe retry) |
| **201 Created** | A project was created, or a decision was recorded by this request |
| **202 Accepted** | **Not used in v1.** It means "accepted, will be done later". Recording a decision is complete when the response is sent, so 201 is accurate; the run *resuming* is asynchronous and visible in the run's status. 202 would fit a future "start a run" request. |
| **400 Bad Request** | The request can't be understood: not JSON, a missing `Idempotency-Key`, or an invalid cursor |
| **401 Unauthorized** | No access token, or one that was refused (`WWW-Authenticate: Bearer`) |
| **403 Forbidden** | Authenticated, without the operation's permission |
| **404 Not Found** | The project, run, or approval in the path does not exist |
| **409 Conflict** | The request conflicts with current state: a fingerprint mismatch, already decided differently, expired or cancelled, or an `Idempotency-Key` that is still in progress or was already used with a different body |
| **422 Unprocessable Content** | Valid JSON, or a query parameter, that breaks the rules: a missing field, an unknown field such as `decided_at`, an unknown template, or an out-of-range value |
| **500 Internal Server Error** | Something unexpected. The details stay in the server logs, under `request_id`. |
| **429 Too Many Requests** | Too many refused credentials from this client, or too many decisions by this principal; retry after `Retry-After` |
| **503 Service Unavailable** | The database cannot be reached, or the identity provider's keys cannot be obtained. Nothing was recorded; retry after `Retry-After`. |

Not used: **412** (no conditional requests yet; see [Design decisions](#design-decisions))
and **415** (a wrong content type is folded into 400 `malformed_request`).

## Errors

Every 4xx and 5xx response uses one format: **problem details** (RFC 9457),
served as `application/problem+json`:

```json
{
  "type": "urn:agent-factory:problem:approval_already_decided",
  "title": "Approval already decided",
  "status": 409,
  "detail": "This approval was already rejected.",
  "instance": "/api/v1/projects/prj-…/approvals/apr-…/decision",
  "code": "approval_already_decided",
  "request_id": "req_J2h8sD5kWq3E",
  "approval": { "...": "the approval as it now stands, with the recorded decision" }
}
```

Each field serves a different reader:

| Field | For |
| --- | --- |
| `code` | Program logic. Stable, snake_case, and listed in `ErrorCode`. |
| `title`, `detail` | People |
| `request_id` | Debugging. It matches the server's logs, so a user can quote it. |
| `errors` | `validation_failed`: one entry per field (`location`, `pointer`, `message`) |
| `approval` | `approval_already_decided`: what was actually recorded |

**Never in an error:** stack traces, exception text, credentials, file paths,
database connection details, or sandbox internals. The schema is closed
(`additionalProperties: false`), so nothing else can be added.

| Code | Status | Meaning | Client action |
| --- | --- | --- | --- |
| `malformed_request` | 400 | Not JSON / wrong content type | Fix the request |
| `idempotency_key_required` | 400 | `createProject` without the header | Send a new key |
| `invalid_cursor` | 400 | Cursor malformed, expired, or for other filters | Restart without a cursor |
| `authentication_required` | 401 | No access token was sent | Sign in |
| `invalid_token` | 401 | A token was sent and refused, for any reason (the response does not say which) | Sign in again |
| `forbidden` | 403 | Authenticated, without the permission; `required_permission` names it | Ask the owner for the permission |
| `too_many_requests` | 429 | Too many refused credentials, or decisions | Wait `Retry-After` seconds |
| `project_not_found` / `run_not_found` / `approval_not_found` | 404 | Nothing at that ID | Refresh the list |
| `action_fingerprint_mismatch` | 409 | The screen showed a different action | Reload the approval, decide again |
| `approval_already_decided` | 409 | Someone decided differently first | Show `approval` |
| `approval_expired` / `approval_cancelled` | 409 | Too late to decide | Refresh |
| `idempotency_key_in_use` | 409 | The same key is still being processed | Retry the same request shortly |
| `idempotency_key_reused` | 409 | Same key, different body | Don't retry; send the new request with a new key |
| `validation_failed` | 422 | See `errors` | Fix the fields |
| `unknown_template` | 422 | No such template | Use `listTemplates` |
| `internal_error` | 500 | Unexpected | Report `request_id` |
| `storage_unavailable` | 503 | Database unreachable | Retry after `Retry-After` |
| `authentication_unavailable` | 503 | The identity provider's keys cannot be obtained | Retry after `Retry-After` |

## Action categories

`ActionCategory` is a closed vocabulary, the same in the contract, the
API, the domain, the frontend types, and the database constraints.

**`cloud_read`** (added in Stage E1, contract-first) is read-only access to
an external cloud system through a typed, trusted adapter. It isn't a local
`read_workspace`: it uses the network and a cloud identity, and may disclose
cloud data, but must not mutate the cloud system.
- **Its rule depends on the environment:** allowed in development, approval
  by default in staging (configurable), and approval in production.
- **Approval requests:** an approval request for a cloud read therefore
  shows `cloud_read` as its category.
- **Other cloud actions:** cloud mutations use the dangerous categories, and
  identity, credential, and organization changes are never approvable
  ([capabilities.md](capabilities.md)).

## Approval decisions: security, idempotency, concurrency

`decideApproval` is the most sensitive operation: it is how a risky action
becomes allowed. These are its rules:

1. **Exact-action binding.**
   - The request must include the `action_fingerprint` the user was shown.
   - The server records the decision only if it equals the stored request's
     fingerprint, a SHA-256 over the tool, category, and every parameter.
   - Otherwise it returns 409 `action_fingerprint_mismatch` and records
     nothing. A stale screen, or an altered request, cannot approve
     something else.
2. **One decision per request.**
   - An identical repeat (same outcome, fingerprint, and person) returns
     **200** with the stored decision.
   - Anything else returns **409** `approval_already_decided`, with the
     current state.
   - Two decisions arriving at the same moment are settled exactly as if
     they had arrived one after the other: a row lock and a unique
     constraint in the database
     ([database.md](database.md#transactions)).
3. **Server-controlled fields.** The decision time is the server's clock.
   The actor is the authenticated principal (`kind: user`, `verified:
   true`). The request schema has neither field, and is closed, so a body
   that includes `decided_at`, `actor`, `decided_by`, `verified`, or
   `user_id` is refused with 422.
4. **Who and what.** Deciding requires `approvals:decide` (403 otherwise,
   before anything is looked up). Authentication proves who decided, and the
   fingerprint proves what they decided; an identical repeat from another
   principal is 409, not 200.
5. **Recording is not executing.** The endpoint stores the decision and
   returns. A **worker** applies it later. Before resuming, it checks that
   the stored fingerprint still equals the fingerprint of the exact action
   the engine is paused on. **A database row saying "approved" never
   authorizes anything by itself.**
6. **Expiry and cancellation.** A request that expired or was cancelled
   can't be decided (409).
   - Every request expires 12 hours after it was requested: server time,
     and the instant itself counts as expired.
   - An approved action must also *begin* before `expires_at`, or it is
     never sent.
   - Expiry never renews. Asking again is a new request, from a new visit of
     the workflow step.
   - `status` shows `expired` as soon as a pending request's expiry has
     passed, and the inbox no longer lists it.
7. **The decision wakes the run.** The decision and the run's wake-up commit
   in one transaction (a transactional outbox), so a worker claims the run
   within its poll interval. Nothing polls a waiting run in between: its only
   other wake-up is its approval's expiry ([workers.md](workers.md)).

## Reconciliation, the execution timeline, and the attention inbox

Added within v1 (E5), additively. **Approval is not execution**: after a
decision, `getApproval` also shows what happened to the action.

**`execution` and `timeline`.**
- `execution` is the record of attempting the approved action: its state
  (`authorized`, `dispatching`, `succeeded`, `failed`, `uncertain`,
  `lapsed`), its latest attempt, an open retry window, and any recorded
  reconciliation answer. It is null until a worker first uses the approval.
- `timeline` lists every step, oldest first: requested, approved or
  rejected or expired, each dispatch and its outcome, an uncertain outcome
  and the reconciliation it required, the answer, a retry authorization, a
  retry, and the resolution.
- Each entry names the person who decided, or the worker (and its fencing
  generation) that sent an attempt.
- The timeline is built from structured records (the approval, its
  decision, the execution ledger, reconciliations), never from raw logs, so
  it holds no parameters beyond the approval's own action, no paths, and no
  internals.

**`reconcileAction`** answers "did it happen?" for an action whose outcome is
unknown (`execution.state` is `uncertain`, and `attention` is
`reconciliation_required`). The rules mirror decisions:

1. **Exact binding.** The body echoes `execution_id`, `attempt`, and
   `action_fingerprint`. If they don't match, the response is 409 and
   nothing is recorded:
   - an attempt that is no longer the uncertain one: `reconciliation_stale`;
   - a different action: `action_fingerprint_mismatch`;
   - nothing uncertain: `reconciliation_not_required`.
2. **One answer per attempt.** An identical repeat by the same person is 200.
   Anything else is 409 `reconciliation_already_decided`, even after a worker
   applied the first.
3. **Server-controlled fields.** Time and actor (the authenticated
   principal), as for decisions. The request schema is closed.
4. **A separate permission.** `actions:reconcile`. `approvals:decide` does
   not imply it, and neither implies the other. Answers count, with
   decisions, against the per-principal limit.
5. **Recording is not executing.** The answer and the run's wake-up commit
   together. A worker that holds the run's claim re-checks the answer and
   applies it to the execution ledger (the ActionExecution protocol), then
   continues the run.
6. **Retry is a new authorization.** `did_not_happen_retry` authorizes one
   more attempt for 15 minutes, counted from the person's decision. The
   original approval is never reactivated. If no worker starts the attempt
   in time, it lapses and nothing is sent.

The resource is the approval, not a separate execution URL, because the
phone's screens are approval-centric, and every approved action has
exactly one execution, enforced by a database constraint. The body's
`execution_id` still names it exactly.

**`listAttention`** answers "what needs me now?" in one list:
`decision_required` (pending and not expired) and `reconciliation_required`
(uncertain, with no answer recorded yet for its attempt).
- It is ordered by when each item began needing a person, with one cursor.
- The server decides membership, including expiry, so a client never
  inspects engine state.
- One endpoint instead of two queries: a client would otherwise need two
  cursors and a client-side merge, and uncertain actions have no other
  listing. `listPendingApprovals` stays, unchanged, for decisions only.

## Model usage (read only)

`Run.model_usage` (schemas `ModelUsage` and `ModelUsageByModel`) reports the
run's model calls, failed calls, tokens, and the provider/model of each.
A call whose answer never arrived counts at its reserved maximum.
`estimated_cost_usd` is `null` without configured prices. No prompt, answer,
or credential is ever exposed. There is no new operation or permission
(`runs:read`).

## Notification status (read only)

`ApprovalRequest.notifications` says whether people were told about this
approval: one entry per attention event (the decision, and each uncertain
attempt). Each entry gives:

- `about`, `attempt`, and `state` (`queued`, `sending`, `retrying`, `sent`,
  `failed`, `skipped`);
- counts, and times;
- `outcome_unknown`: an attempt ended without the provider's answer, so a
  duplicate may have arrived;
- a closed-vocabulary `failure`.

It is in `getApproval` and `listPendingApprovals`, under `approvals:read`.
There is no new operation and no new permission.

- **Never exposed:** recipients, addresses, providers, message text, or
  provider error text.
- **No sending operation.** No route sends a message, and no request body
  names a recipient. Notifications arise only from product events
  ([notifications.md](notifications.md)). Contract tests check both.
- **Informational.** A notification never authorizes anything; the approval's
  `attention` and `status` stay authoritative.

## Idempotency

An operation is **idempotent** if doing it twice has the same effect as
doing it once. Mobile networks retry requests, so a tap must not turn into
two actions.

| Operation | Strategy |
| --- | --- |
| Every GET | Safe and idempotent by definition |
| `decideApproval` | **Natural idempotency: the approval itself is the key.** It can hold one decision. A retried identical request returns that decision (200), and a different one is refused (409). No header is needed. |
| `createProject` | **`Idempotency-Key` header, required** (the IETF HTTP API working group's draft standard). See the rules below. |

**Rules for `createProject`.**

- **Same key, same body:** returns the original response, with
  `Idempotent-Replayed: true`. A retry never creates a second project.
- **Same key, different body:** 409 `idempotency_key_reused`. The key is
  already bound to another request; send the new one with a new key. The
  stored record is unchanged, so the original request still replays.
- **Same key while the first request is still processing:** 409
  `idempotency_key_in_use`.
- **Retention:** keys are kept for at least 24 hours.

**Why not use the key for decisions too?** A key could be fresh on every
tap, and that must never allow a second, conflicting decision. The
approval's single-decision rule is stronger and simpler. Idempotency is
never a way around a conflict.

## Pagination

Collections that grow (projects, runs, milestones, the approval inbox) are
paginated. Templates are few, and single resources are never paginated.

```http
GET /api/v1/projects/prj-…/milestones?since=…&limit=50
→ {"items": [ … 50 milestones … ], "next_cursor": "eyJr…"}
GET /api/v1/projects/prj-…/milestones?since=…&limit=50&cursor=eyJr…
→ {"items": [ … ], "next_cursor": null}        # null: the last page
```

**Cursor (keyset) pagination, not offset.**

- Offset paging (`?page=3`) shifts when new items arrive, so items are
  skipped or repeated, and it gets slower deeper in.
- A cursor encodes the position: the sort key of the last item, such as
  `(occurred_at, run_id, sequence)` for milestones. It continues exactly
  there, whatever was inserted meanwhile. The same database index that
  sorts the query also finds the page.
- **The cursor is opaque.** Clients must not build or decode it.
- **It is tied to its filters.** Reusing it with different filters returns
  400 `invalid_cursor`.
- **Limits:** `limit` is 1–100 (default 50).
- **No totals.** Counting a large, changing set for every page is expensive
  and immediately stale.

| Collection | Order |
| --- | --- |
| Projects | Newest first (`created_at`, then `id`) |
| Runs | Newest first (run `number`) |
| Milestones | Chronological (`occurred_at`, `run_id`, `sequence`) |
| Pending approvals | Oldest first (`requested_at`, project, `id`) |

**One subtlety.** A milestone's `occurred_at` moves forward when its status
changes, for example from blocked to completed. So a milestone can appear
again, with its new status, on a later page of the same listing. Clients
keep the latest copy of each `id`.

## Filtering

A small, fixed set of filters, not a query language:

| Endpoint | Filters |
| --- | --- |
| Milestone listings | `since` (inclusive), `until` (exclusive; must be after `since`), `status` (repeatable), `category` (repeatable) |
| Run milestones | The run is in the path. It returns 404 if the run does not exist, rather than an empty list. |
| `listPendingApprovals` | `status=pending` (required; the only value in v1), `project_id` |
| `listProjects`, `getProject` | `activity_since`: adds a count of recent milestones per project |

Repeat a parameter for several values, e.g. `?status=failed&status=blocked`.
Invalid filter values return 422 `validation_failed`.

## Time

**UTC policy.**

- Every timestamp is **UTC ISO 8601 ending in `Z`**, for example
  `2026-10-02T06:16:00.000000Z`.
- Responses always use six fractional digits.
- Query parameters accept 0–6 fractional digits, but must end in `Z`.

**Local display.** Clients convert to the user's time zone for display. The
API never depends on the server's time zone.

## Versioning

- **Version in the path.** Every path starts with `/api/v1`.
- **Within v1, additive changes only:**
  - new endpoints;
  - new optional response fields;
  - new error codes;
  - new values in enums that are documented as growable.

  Clients ignore unknown response fields.
- **Breaking changes** (removing or renaming fields, changing meaning,
  adding a required request field) go to `/api/v2`. v1 is kept for a
  deprecation period, announced with the standard `Deprecation` and
  `Sunset` response headers.
- **Draft status.** `info.version` is `1.0.0-draft`. Until the first backend
  ships there are no clients, so v1 may still change; after that, the rules
  above apply.

One version is enough for now; multiple versions are not being built ahead
of need.

## API model, domain model, database row

They are deliberately different:

- **Database rows** are shaped for integrity and queries.
- **Domain objects** are shaped for rules.
- **API models** are shaped for **what a client needs**.

| API schema | Built from (domain) | Stored in (database) | Notable differences |
| --- | --- | --- | --- |
| `ProjectSummary` | `Project`, latest `Run`, counts | `projects`, `runs`, `approval_requests`, `milestones` (aggregates) | No domain class or table has this shape; `status` is derived from the latest run |
| `Project` | `Project` and its policy's catalog | `projects` (policy in JSONB) | `milestone_types` exposes only the catalog, not the node rules; `metadata` is not exposed |
| `Run` | `Run`, plus its pending request | `runs`, `approval_requests` | `graph_name` becomes `workflow`; `status_reason` is curated (see gaps) |
| `Milestone` | `Milestone` | `milestones` | `key`, `created_at`, and raw `metadata` are hidden; `follows` and `approval_id` are extracted from metadata; `resolves_id` becomes `resolves_milestone_id` |
| `ApprovalRequest` | `ApprovalRequest`, project name, `ApprovalDecision` | `approval_requests`, `projects`, `approval_decisions` | Decision embedded; `context` not exposed |
| `ApprovalDecision` | `ApprovalDecision` | `approval_decisions` | `actor` becomes `decided_by` (four columns in the database) |
| `ApprovalDecisionRequest` | Arguments of `ApprovalService.decide` | — | No time or actor fields: the server supplies them |
| `Problem` | Domain errors mapped to codes | — | Never contains exception text |

How domain errors become API errors:

| Domain error | API response |
| --- | --- |
| `RecordNotFoundError` | 404 `*_not_found` |
| `ApprovalBindingError` | 409 `action_fingerprint_mismatch` |
| `ConflictError` (already decided) | 409 `approval_already_decided` |
| `ApprovalExpiredError`; `StatusTransitionError` on expired or cancelled requests | 409 `approval_expired` / `approval_cancelled` |
| `DomainValidationError` | 422 `validation_failed` |
| `StorageUnavailableError` | 503 `storage_unavailable` |
| Anything else | 500 `internal_error` |

## Workers and the API

The owner chose a **distributed approval architecture**: the API and the
workers that run the agents never call each other directly. They meet in
PostgreSQL.

```
PHONE / WEB UI
      |  POST .../decision  {outcome, action_fingerprint}
      v
API ── ApprovalService ──> PostgreSQL: decision + request status + the run's wake-up
      |                     (one transaction; fingerprint must equal the stored request's)
      | 201 Created
      v
(the user sees "Approved")

WORKER (claims the run: FOR UPDATE SKIP LOCKED, within its poll interval)
      |  Executor.advance(run_id)
      v
reads the stored decision ──> checks: the exact pending action, the fingerprint, the request's
      |                        status, the expiry, the decider (contradiction: refuse, run parked)
      v
applies the decision to the checkpoint ──> resumes the graph
      |
      v
the engine's gateway re-checks the request ID (a hash of run, node, visit, tool, parameters)
before the tool runs
```

**Built and tested end to end (E5).** `ApprovalService` records and
validates, and a worker applies the decision under the run's claim, after
its own checks (`tests/test_e2e_human_decisions.py`: an authenticated HTTP
decision, then a real worker, then the action's single effect). Workers
claim runs through PostgreSQL alone, under leases and fencing tokens; the
API never reads a claim or changes its owner (its one write is the wake-up)
and offers no worker endpoint ([workers.md](workers.md)).

**Run status with workers.** A run queued for a worker, being advanced, or
interrupted reports `running` ("in progress": not paused, not finished).
Which worker owns it is internal, so the contract needed no change.

**Who owns what:**
- the API owns **user intent**: decisions, new projects;
- workers own **execution**: runs, checkpoints, milestones;
- PostgreSQL is the boundary between them.

## Authentication and permissions

**Implemented** (branch `feature/auth-worker`); the full design is in
[authentication.md](authentication.md).

- **Scheme.** `components.securitySchemes.bearerAuth`: HTTP bearer,
  `bearerFormat: JWT`, `x-agent-factory-status: implemented`, applied to
  every operation. Tokens are OAuth 2.0 access tokens from an OpenID Connect
  provider, verified by the backend (signature, issuer, audience, expiry). No
  homegrown login protocol exists. The scheme's description says what is
  **not** done yet: no production identity provider is configured, and
  browser sign-in comes with the frontend.
- **Permissions.** Each operation names the one permission it requires in
  `x-permission`, and the `Permission` schema lists the vocabulary:

  | Permission | Operations |
  | --- | --- |
  | `projects:read` | `listProjects`, `getProject`, `listTemplates` |
  | `projects:create` | `createProject` |
  | `runs:read` | `listRuns`, `getRun` |
  | `milestones:read` | `listProjectMilestones`, `listRunMilestones` |
  | `approvals:read` | `listPendingApprovals`, `getApproval`, `listAttention` |
  | `approvals:decide` | `decideApproval` |
  | `actions:reconcile` | `reconcileAction` (E5) |

  Runs and milestones were `projects:read` in the draft; they have their
  own permissions now, so a principal can be given the project list without
  the timelines.
- **Errors.** Every operation documents 401 (`Unauthorized`, with a
  required `WWW-Authenticate` header), 403 (`Forbidden`; the problem's
  `required_permission` names the missing permission), and 429
  (`TooManyRequests`, with `Retry-After`). 503 also covers
  `authentication_unavailable`.
- **Actor.** A decision's `decided_by` is the authenticated principal:
  `kind: user`, its `prn-` ID, `verified: true`.
- **Infrastructure endpoints** (`/healthz`, `/readyz`) stay outside the
  contract and need no credential; they expose no data.

## Design decisions

| Question | Decision | Why |
| --- | --- | --- |
| Dedicated overnight-summary endpoint, or derived by the client? | **Derived by the client** from milestone queries, plus small dashboard counts (`activity_since`) | The smallest clean contract; the milestone listing already answers every overnight question. A summary endpoint can come with narrative summaries later. |
| Start runs through the API? | **Not in v1** | A run exists only once the engine creates it. Workers and a claim protocol now exist (`run_claims`, filled by the local `worker enqueue` command), but the contract has no run request, and run status has no "queued" value (a queued run reports `running`). A 202 endpoint needs a run-request resource first. **Decision for the owner.** |
| A templates endpoint? | **Yes**, read-only | `createProject` needs valid template names, and clients can use the milestone vocabularies |
| Approvals global or per project? | **Read and decide per project**; **inbox global** with a `project_id` filter | Approval IDs are per project; an inbox across projects is what the phone needs |
| Fingerprint mismatch: 409, 412, or 422? | **409** | The body is valid; it conflicts with the resource's state. 412 is reserved for conditional-request headers. |
| `Idempotency-Key` reused with a different body: 409 or 422? | **409** (owner decision; the first implementation used 422) | The new request conflicts with the server state already bound to that key. Both idempotency conflicts are 409; clients tell them apart by `code`: `idempotency_key_in_use` means retry the same request, `idempotency_key_reused` means use a new key. |
| Decision response | **201** recorded now / **200** already recorded, both returning the approval with its decision | Tells a retrying client exactly what happened |
| Error format | **RFC 9457** problem details with `code` and `request_id` | A standard, which tools understand, plus stable codes |
| Schemas open or closed? | **Closed** (`additionalProperties: false`) | Requests can't smuggle fields; responses list exactly what is exposed. Clients still tolerate future additions. |
| Optimistic concurrency (ETags) | Not needed in v1 | Nothing is updated in place. The decision is a compare-and-set on the fingerprint. Future in-place updates (e.g. renaming a project) would use `ETag` / `If-Match` and 412. |
| No health, single-milestone, or decision-GET product endpoints | Left out of `/api/v1` | Health/readiness are separate infrastructure endpoints (`/healthz`, `/readyz`); no new product resources were added |
| `status_reason` | Curated text only | The domain stores raw engine messages, which may contain paths or internals. The backend must map them to a fixed set of safe messages. |

## Testing the contract

The original tests continue checking the contract document itself:

```bat
.venv\Scripts\python -m pytest tests/test_openapi_contract.py -v
```

They need `requirements-dev.txt` (PyYAML, openapi-spec-validator,
jsonschema) in the project venv. Under a Python without them, the file is
skipped with that reason.

| Area | Checks |
| --- | --- |
| Validity | The official **OpenAPI 3.1 schema** (openapi-spec-validator); every `$ref` resolves; no unused components |
| Surface | The exact operation set; unique operationIds; `/api/v1` everywhere; path parameters use the domain's ID schemas |
| Approval safety | The fingerprint is required; time, actor, `verified`, and other fields are refused (validated with jsonschema); only allowlisted request fields; 201 / 200 / 409 semantics |
| Absent capabilities | Only two writes; runs and milestones read-only; no graph, command, or sandbox routes or fields |
| Errors | Every 4xx/5xx is a closed `Problem`; example codes and types agree |
| Time and pagination | Every `*_at`, `since`, and `until` is the UTC `Timestamp`, whose pattern equals the domain's; the page shape and limit/cursor pairing are consistent |
| Domain agreement | ID patterns and enums equal the domain's. Example fingerprints, decision IDs, and risk texts are recomputed with the domain code. |
| Secrets | No secret or internal field names in any schema; no host paths or credentials in any example |
| Authentication | Implemented, applied to everything, every operation naming one permission from the domain's vocabulary, 401/403/429 on every operation, `WWW-Authenticate` required on 401; no "PLANNED" label remains |
| Examples | All 22 request and response examples validate against their schemas (JSON Schema 2020-12, with format checks) |

Runtime conformance also checks the exact product route set and operation
IDs, and validates actual HTTP status/body pairs against the checked-in
schemas, including RFC 9457 errors. It does not substitute FastAPI's
generated schema for this file. A test also compares every route's
permission check with its operation's `x-permission`, and the authorization
tests exercise 401, 403, and 429 against the contract on both stores.

## Implementation status and remaining gaps

[backend.md](backend.md) documents the implemented FastAPI/Uvicorn service,
Pydantic validation, PostgreSQL pool, repository keyset queries, durable
idempotency migration, existing-ID workspace binding, safe status views,
request IDs, JSON logs and operational health checks.

The owner approved these narrow corrections from the initial contract
baseline: explicit 422 responses for invalid query inputs on `listProjects`,
`getProject` and `listRuns`, and stable `route_not_found` /
`method_not_allowed` problem codes for HTTP routing errors. No product
operation was added or redesigned. Same-key/different-body project creation
answers **409 `idempotency_key_reused`** (owner decision, replacing the
first implementation's 422; the code name is unchanged).

**Additive change (durable action execution, owner-approved).** `RunStatus`
and `ProjectStatus` gained `awaiting_reconciliation`: an approved action may
already have happened and Agent Factory cannot tell, so a person must say
whether it did before the run continues
([design](design/action-execution.md)). It is not `blocked`, which means an
infrastructure or policy problem. The contract had to change to stay
truthful: the engine's run states are reported as they are, and the
contract tests require the enums to equal the engine's. Its `status_reason`
is a fixed sentence. No endpoint, field, or schema shape changed. The
milestone vocabulary of new projects also includes the engine type
`action_execution` ("Approved action"); its summaries are fixed sentences
naming only the tool, never its parameters. Reconciling an action through
the API (`reconcileAction`, permission `actions:reconcile`) was added in E5
([above](#reconciliation-the-execution-timeline-and-the-attention-inbox)).

**Authentication and permissions (owner-approved).** The planned
`bearerAuth` scheme is now implemented and enforced; `x-planned-permission`
became `x-permission`, runs and milestones got `runs:read` and
`milestones:read`, and these were added: the `Permission` schema, the
`WWWAuthenticate` header, the `TooManyRequests` response (on every
operation), the error codes `invalid_token`, `too_many_requests`, and
`authentication_unavailable`, and the problem's optional
`required_permission`. Additive within v1: no path, request field, or
success response changed.

Remaining gaps are a configured production identity provider and browser
sign-in; the API for reconciling
uncertain actions; a queued-run model if API run-start is requested; an
approval-expiry policy (the engine already refuses to begin a dispatch after
an approval expires); distributed rate limits; frontend integration; and
production deployment/configuration. No CORS policy for a separate frontend
has been enabled. **The API listens on 127.0.0.1 only.**
