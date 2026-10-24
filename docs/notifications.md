# Attention notifications (v1)

When Agent Factory needs a person (an approval to decide, or an action whose
outcome someone must verify), it can tell them without the dashboard being
open:

```text
Agent Factory works overnight
  -> a run pauses: approval required          (attention item stored)
  -> NotificationOutbox: one durable intent   (same transaction)
  -> dispatcher -> provider -> "Agent Factory needs your attention"
  -> the person opens the dashboard and signs in
  -> reviews the exact action -> APPROVE / REJECT
  -> a worker re-checks the decision and continues
```

**A notification alerts. It never authorizes.** It says "something needs
your attention" and links to the dashboard. Only the authenticated dashboard
asks "what exactly is requested?" and "do you approve?". No SMS reply, email
link, push button, or URL can approve, reject, or answer anything
([safety.md](safety.md)).

Status: **phase 1**. The outbox, dispatcher, fake provider, and dashboard
status are built and tested. **No real provider is integrated, and no real
message has ever been sent.** That needs the owner's authorization (see
[Real providers](#real-providers)).

## 1. Design check: notification channels

**Before anything was built, these channels and providers were compared.**
Nothing was contacted, configured, or signed up for. Prices and provider
rules below are approximate, from public information as last known. **They
have not been checked for this report**, because no network access was
authorized. Verify every figure on the provider's own pricing and
registration pages before buying anything.

| | SMS: Amazon SNS | SMS: Twilio | Email: Amazon SES | Web/browser push | Native mobile push |
| --- | --- | --- | --- | --- | --- |
| Wakes a person at 1 AM | Likely (an SMS alert; Do Not Disturb can still silence it) | Likely (same) | Depends on mail settings; often silent overnight | Only if the browser or installed web app may notify; iOS only for a Home-Screen web app | Yes, best control (time-sensitive or critical alerts need Apple approval) |
| Latency | Seconds, usually | Seconds, usually | Seconds to minutes | Seconds, best effort | Seconds, best effort |
| Phone usability | Any phone, no app | Any phone, no app | Any mail app | Needs the browser or web app set up on the phone | Needs our own app |
| Cost (approximate) | Per message (fractions of a cent in the US) plus carrier fees, plus an origination number | Per segment (about a cent in the US) plus carrier fees, plus a phone number (about $1–2 a month) | About $0.10 per 1,000 emails | Free (browser vendors' push services) | Free to send; Apple Developer Program about $99 a year |
| Account and registration | AWS account. A US destination needs a registered origination identity (toll-free verification or 10DLC), which takes days to weeks. New accounts start in an SMS sandbox (verified numbers only) with a small monthly spend limit | Twilio account. US: A2P 10DLC brand and campaign registration, or toll-free verification. Trial accounts reach verified numbers only and prefix messages | AWS account. A verified sender identity. New accounts start in a sandbox (verified recipients only, low daily quota), which is **enough for a single owner** | A VAPID key pair (self-generated), a service worker, HTTPS (localhost exempt) | A native iOS/Android app; APNs/FCM credentials |
| Provider dependency | AWS | Twilio | AWS | Browser vendors (FCM, Mozilla, Apple) | Apple, Google |
| Authentication implications | AWS credentials only in the dispatcher (later an IAM role on AWS) | An API key or auth token only in the dispatcher | Same as SNS | Subscriptions are per browser and must be bound to a principal. **A notification button must never approve**: it may only open the dashboard | Same as web push |
| Delivery status | Delivery logs (CloudWatch, needs an IAM role) | Status by polling the message, or a webhook (needs a public endpoint: **not** while the API is loopback-only) | Delivery, bounce, and complaint events through SNS or EventBridge (AWS resources) | The push service accepts it (201); no "delivered" receipt | Limited |
| Provider-side idempotency | None known for SMS publish | None known for message creation | None known for SendEmail | Topic/collapse keys replace, not deduplicate | Collapse IDs replace earlier ones |
| Retries | Ours (the outbox) | Ours | Ours | Ours | Ours |
| Privacy | Phone number and text reach AWS and carriers | Phone number and text reach Twilio and carriers | Address and text reach AWS and mail servers | Endpoint URLs; payload can be encrypted end-to-end | Device tokens |
| Local development | The fake provider; a real send needs credentials | The fake provider; trial account | The fake provider; sandbox | Localhost works; Playwright cannot easily verify OS notifications | Not practical yet |
| Fits the future AWS deployment | Yes (IAM roles, same account) | Yes, as an external SaaS | Yes (IAM roles, same account) | Needs a public HTTPS dashboard and a service worker under the strict CSP | Needs an app |

### Recommendation

1. **First real channel: email through Amazon SES.**
   - It has the least friction for one owner: the SES sandbox is enough,
     because the owner verifies their own address, and no phone number has
     to be registered.
   - It is the cheapest, and it fits the AWS deployment (IAM roles, no
     long-lived keys once on AWS).
   - Weakness: an email may not wake anyone. Mitigate it on the phone (a VIP
     or priority sender), and use the dashboard's attention list as the
     authority.
2. **Then SMS through Amazon SNS,** once a US origination identity is
   registered. SMS is the channel most likely to wake someone at 1 AM.
   Twilio is the fallback if SNS registration stalls: it is about as
   expensive, its message status can be polled without a public endpoint, and
   its own setup and registration take a comparable effort.
3. **Later: push,** as browser push for an installed web app, or as native
   push. Both wait for a public HTTPS deployment, and native push for an app.

The channel model already has `email`, `sms`, and `push`, so adding a
provider is an adapter, not a redesign.

## 2. Architecture

```text
 recorder.sync (worker, fenced by its claim)          dispatcher process (notifications dispatch)
 ──────────────────────────────────────────           ───────────────────────────────────────────
 BEGIN                                                 sweep (backstop) ── attention inbox ─► enqueue
   approval request / uncertain attempt recorded       recover expired leases (unknown attempts)
   notifications: INSERT ... ON CONFLICT DO NOTHING    claim due ── FOR UPDATE SKIP LOCKED, lease, token+1
 COMMIT  (both, or neither)                            still needed? rate limit?
                                                       begin attempt ── write-ahead ─────────── COMMIT
         notifications (PostgreSQL) ◄──────────────►   provider.send(...)      (no transaction open)
                                                       finish ── fenced by claim_token ──────── COMMIT
```

- **Nothing that creates attention sends anything.** Graph nodes, API
  handlers, and the dashboard never call a provider. The recorder writes an
  *intent*; the dispatcher, a separate process, sends it. A static test
  allows only the dispatcher package and the operator CLI to import the
  provider code.
- **The dispatcher does not need the browser, the API, or a worker.** It
  needs PostgreSQL and the provider.
- **Code:**
  - `domain/notifications.py`: the model, content, retry policy, and
    addresses;
  - `domain/notification_repositories.py`: the interfaces and in-memory
    store;
  - `persistence/notifications.py` and migration `0007`: PostgreSQL;
  - `notifications/`: the dispatcher, the provider boundary, and the fake
    provider.

## 3. The outbox

### Schema (migration `0007_notifications.sql`)

| Table | Holds |
| --- | --- |
| `notification_destinations` | `id` (`nds-`), `principal_id`, `channel` (`email`, `sms`, `push`), `address` (normalized), `status` (`active`, `disabled`), `policy` (`immediate`), times |
| `notifications` | `id` (`ntf-`, derived), `dedup_key`, `schema_version` (1), `project_id`, `run_id`, `approval_id`, `attention_kind`, `execution_id` and `attention_attempt` (reconciliations), `destination_id`, `principal_id`, `channel`, `severity` (`urgent`), safe `title` and `body`, `link_path`, `status` (`pending`, `sending`, `sent`, `failed`, `skipped`), `attempts`, `created_at`, `available_at`, `next_attempt_at`, `last_attempt_at`, `sent_at`, `provider_message_id`, `failure_category`, `claimed_by`, `claim_token`, `lease_expires_at`, `updated_at` |
| `notification_attempts` | one row per hand-over: `attempt`, `dispatcher_id`, `claim_token`, `started_at`, `ended_at`, `outcome` (`accepted`, `transient_failure`, `permanent_failure`, `unknown`), `failure_category`, `provider_message_id` |

The database enforces:

- one notification per `(dedup_key, destination_id)`;
- a reference to the real approval request of that run, and to the real
  attempt of the execution, for a reconciliation;
- a `link_path` that is a dashboard path, with no host, query string, or
  fragment;
- exactly one owner and lease while `sending`, and no lease longer than
  five minutes;
- a reason for every failure or skip, from a closed vocabulary;
- at most one open attempt per notification.

A notification names its destination; **it never copies the address**.
Content holds no tool, parameter, path, credential, or token.

### Transaction boundary

- **Approval required.** The recorder's `sync` opens the approval request
  and enqueues its notification intents **in the same transaction**. A crash
  keeps both or neither (tested: a rollback in memory and in PostgreSQL).
- **Reconciliation required.** The execution becomes `uncertain` in the
  execution ledger's own write-ahead transaction, which the engine keeps free
  of product concerns. The intent is written by the next `sync`, in that
  sync's transaction. Two things close the gap between the two:
  - **the dispatcher's sweep**, which reads the attention inbox and enqueues
    an intent for any item that has none (enqueue is idempotent);
  - **`Executor.advance` re-syncing a run that is still waiting.**
- **The run's checkpoint.** It is committed before its `sync`. If a process
  died between the two, the approval request (and with it the attention item
  and the intent) stayed missing until `db reconcile`. Phase 1 found this
  pre-existing gap, and `advance` now re-syncs a waiting run (sync is
  idempotent and fenced). See docs/workers.md.

### Deduplication

`dedup_key` names the attention event, not a delivery:

- `decision_required:<project>:<approval>`: one per approval request;
- `reconciliation_required:<project>:<execution>:<attempt>`: one per
  uncertain attempt. Attempt 2 is a new event, and is notified again.

The notification ID is derived from `(dedup_key, destination)`. Syncing the
same state again, the sweep, or two writers at once insert with
`ON CONFLICT DO NOTHING`, so twenty internal events about one approval make
one notification. **A retry reuses the same notification row**; it only adds
an attempt.

## 4. The dispatcher

```bat
set AGENT_FACTORY_DATABASE_URL_FILE=.local\database.url
set AGENT_FACTORY_NOTIFICATIONS_MODE=development
.venv\Scripts\python -m agent_factory notifications dispatch --provider fake --fake-record .local\notifications.jsonl --dashboard-url http://127.0.0.1:4173
```

- **Claims.** One short transaction: `UPDATE ... FROM (SELECT ... FOR UPDATE
  SKIP LOCKED)`. It sets `sending`, the dispatcher's ID, `claim_token + 1`,
  and a lease of at most five minutes, in PostgreSQL's `now()`. Racing
  dispatchers never get the same row (tested with four threads, and with two
  processes).
- **No transaction is ever open during a provider call.** The attempt row is
  committed first (write-ahead), then the provider is called, then the
  result is recorded.
- **Fencing.** `begin_attempt`, `finish`, `defer`, and `skip` change a row
  only for its current `(claimed_by, claim_token)` while it is `sending`.
  `begin_attempt` also needs the lease to still be valid. When a lease
  expires, `recover_expired` takes the row back as a new generation, so the
  old dispatcher's late result changes nothing (tested).
- **Fail closed on the database.** No provider is called without a committed
  attempt. If PostgreSQL is down, the dispatcher sends nothing and retries
  with backoff (tested by stopping this project's PostgreSQL container).
- **Settings:** `--lease`, `--send-timeout` (under half the lease),
  `--poll`, `--sweep`, `--batch`, `--max-attempts`, `--retry-base`,
  `--retry-cap`, `--dashboard-url`.
- **Fail closed on configuration.** Without `--provider` it refuses to start
  ("no real notification provider is configured yet"). `--provider fake`
  needs `AGENT_FACTORY_NOTIFICATIONS_MODE=development`.

### Retries and failure classes

| Class | Categories | What happens |
| --- | --- | --- |
| Transient | `timeout`, `provider_unavailable`, `rate_limited`, `outcome_unknown` | Retried after `30 s × 2^(n-1)`, capped at 30 min, with "equal jitter" (between half and all of the delay). The delay counts from the end of the attempt. |
| Permanent | `invalid_destination`, `provider_rejected_configuration`, `unsupported_destination` | Failed at once; never retried. |
| Attempt limit | `max_attempts` | After 8 attempts (about an hour of retries). |
| Skipped | `attention_resolved`, `destination_disabled`, `recipient_not_permitted` | Checked just before sending: an approval already decided or expired, a turned-off destination, or a person who may no longer act. |

**A notification failure never removes the attention item.** The run stays
paused, and the dashboard's attention list and approval page still show it
(tested at every layer).

### Delivery semantics and crash boundaries

**At least once, with duplicate suppression where possible. Never exactly
once.** Agent Factory cannot know whether a provider accepted a message whose
answer it never received, and it does not guess. A duplicate alert is
annoying; a lost one can leave a run waiting all night.

| Crash at | What is durable | What happens next (tested with real processes killed) |
| --- | --- | --- |
| attention committed, intent missing | the attention item | the sweep creates the intent; sent once |
| intent committed, no dispatcher | `pending` | sent when a dispatcher starts |
| claimed, before any attempt | `sending`, 0 attempts | lease expires; recovered as due now; sent once |
| attempt begun, provider not reached | an open attempt | recovered: attempt `unknown`; retried; delivered once |
| **provider accepted, result not recorded** | an open attempt | recovered: attempt `unknown`; retried; **delivered twice**; shown as "possibly more than once" |
| result recorded, then killed | `sent` | nothing is sent again |
| retry scheduled, then killed | `pending`, next attempt time | the next dispatcher retries on time |
| two dispatchers race | | one claim per row; each delivered once |

Every attempt passes the notification's ID as `idempotency_key`. A provider
that honours such a key would suppress the duplicate above; the fake can
simulate one (`honor_idempotency`). **None of the compared providers is
known to offer one** for these sends, so a duplicate remains possible.

## 5. Recipients, preferences, and privacy

- **Destinations belong to principals.** A notification goes to every
  active destination of every active principal that holds the permission to
  act on that attention:
  - `approvals:decide` for an approval;
  - `actions:reconcile` for a reconciliation.

  Nobody else is told. Nothing a client sends chooses a recipient.
- **v1 has no preferences UI and no API.** The local operator manages
  destinations:

  ```bat
  .venv\Scripts\python -m agent_factory notifications destination add --principal-id prn-... --channel email --address you@example.com
  .venv\Scripts\python -m agent_factory notifications destination list
  .venv\Scripts\python -m agent_factory notifications destination disable --id nds-...
  .venv\Scripts\python -m agent_factory notifications status --project-id prj-... --approval-id apr-...
  ```

- **Addresses are personal data.**
  - Email: the domain is lower-cased.
  - SMS: E.164 (`+` and country code).
  - They are shown only redacted (`d***@example.com`, `+***4477`).
  - They are never in a notification row, an event, an API response, or an
    error message (tested).
  - Provider credentials are never stored with them.
- **Policy.** Each destination has a `policy`, and every notification a
  `severity`. v1 has one of each: `immediate` and `urgent`. **Urgent
  attention is sent day or night**, because overnight autonomy is the point.
  The model leaves room for quiet hours, digests, and lower severities. When
  they come, approvals and reconciliations must still never be silently
  suppressed: at most deferred to a channel that reaches the person.
- **Storm protection.** Deduplication handles repeated internal events. On
  top of that, at most 20 attempts per destination per hour; the rest **wait
  (they are deferred, never dropped)** until the window allows them.
  Distinct attention items are never merged or suppressed.

## 6. Content and links

```text
Agent Factory needs your attention.

Project: Client ETL Pipeline
Reason: Approval required            (or: Action outcome needs verification)

Open Agent Factory to review.

http://127.0.0.1:4173/projects/prj-.../approvals/apr-...
```

- The project's name is the only free text. Control and bidirectional
  characters are removed and whitespace collapsed, so it cannot forge a line;
  it is clipped to 60 characters.
- **No tool, parameter, path, database detail, credential, token, or client
  data** appears in a notification. Tests check delivered messages for
  forbidden strings.
- **The link authorizes nothing.**
  - It is the configured dashboard origin plus `/projects/<id>/approvals/<id>`.
  - The origin is checked: no credentials, path, query string, or fragment,
    and `https` unless it is loopback.
  - Opened without a session, it shows sign-in; after signing in, the person
    lands on that approval (tested in Chrome).
  - It carries no token or secret.

## 7. API and dashboard

- `ApprovalRequest.notifications` (OpenAPI-first, read-only, under
  `approvals:read`) gives one entry per attention event, for
  `getApproval` and `listPendingApprovals`. Each entry holds:
  - `about`, `attempt`, and `state` (`queued`, `sending`, `retrying`,
    `sent`, `failed`, `skipped`);
  - counts, and times;
  - `outcome_unknown`;
  - a closed-vocabulary `failure`.

  It never holds a recipient, an address, a provider, or the message.
- **No route sends anything.** There is no notification endpoint, and no
  request body has a recipient field (contract tests). Notifications arise
  only from product events.
- **The approval page** shows a "Notifications" fact, for example "The
  decision: Notification sent 5 min ago" or "Notification could not be
  delivered. This request still waits here." It is informational: it never
  enables or disables a decision. The attention item stays authoritative.

## 8. Tests

| Suite | File | Proves |
| --- | --- | --- |
| Default (in memory) | `tests/test_notifications.py` | Content, identities, addresses, retry timing, the outbox through a real worker and recorder, rollback, eligibility, every fake outcome, storm protection, fencing, events without addresses, import and route boundaries, sandbox credential names |
| Default (HTTP) | `tests/test_api_notifications.py` | The contract's status field, validated against `openapi.yaml`; no address in responses; no sending route; vocabularies equal the domain's |
| PostgreSQL | `tests/test_postgres_notifications.py` | The outbox through a fenced worker, rollback, constraints, the claim index, racing claimers, two dispatcher processes, fencing, database-clock retries, the seven crash boundaries with real processes killed |
| Browser (real stack) | `tests/test_dashboard_notifications.py` | The overnight story with no browser open, the deep link through sign-in, frontend/API/worker/dispatcher outages, PostgreSQL outage (`--run-docker-live`) |

Results are in [dashboard-reliability.md](dashboard-reliability.md#notifications).

## Real providers

Phase 1 stops here. **Integrating a real provider needs the owner's
explicit authorization** for each of the following:

1. **An account and its configuration** (for the recommended SES):
   - an AWS account and a region;
   - a verified sender identity, and the owner's address verified as a
     recipient (SES sandbox);
   - an IAM identity allowed only `ses:SendEmail`/`ses:SendRawEmail` for
     that identity.
2. **Credentials.**
   - For local development: an access key in the dispatcher's environment or
     AWS credentials file, **never** in the repository, `.local/` commits,
     an agent prompt, or the sandbox. The sandbox already rejects `AWS_*`,
     `TWILIO*`, `*SID*`, `SES_*`, `SNS_*`, `SMTP*`, and every
     `AGENT_FACTORY_*` name.
   - On AWS later: an IAM role, with no long-lived key.
3. **A dependency.** Either `boto3` (pinned in a new
   `requirements-notifications.txt`; installing it is a network action), or
   a small standard-library SigV4 client (no dependency, more code to
   verify). The recommendation is `boto3`.
4. **Network egress** from the dispatcher process only, to the provider's
   HTTPS endpoint.
5. **One real test message** to the owner's own address, sent only when
   authorized.
6. **For SMS later:** a registered US origination identity (toll-free
   verification or 10DLC), raising the account's SMS spend limit, and the
   owner's number. All are paid or need a review.

The adapter maps provider answers onto the categories above:

| Provider answer | Category |
| --- | --- |
| Throttling | `rate_limited` |
| A rejected recipient | `invalid_destination` |
| Access or configuration errors | `provider_rejected_configuration` |
| Timeouts and dropped connections after sending | `timeout`, outcome unknown |
| 5xx before acceptance | `provider_unavailable` |

## Models and notifications

A notification never carries anything a model produced beyond the project's
name, and model credentials never appear in notification content, rows, or
events. The two credential sets (a model key file, a notification provider's
credentials) are separate. Both live only in the trusted orchestrator
process that needs them, and the sandbox rejects both
([model-providers.md](model-providers.md)).

## Known limits

- No real provider. Nothing has been sent outside this machine.
- "Sent" means a provider accepted the message, not that the person saw it.
  No delivery receipts yet (they need provider-side resources, or a public
  webhook the loopback API cannot offer).
- One host. The dispatcher's lease and fencing are proven on one PostgreSQL;
  the same rules hold for several hosts, but that is not tested.
- No quiet hours, digests, or per-person channel preferences (designed only).
- A provider without idempotency keys can deliver a duplicate after a crash
  or a lost response. This is shown honestly and accepted.
