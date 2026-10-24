# M1 Persistent Learning Memory — future architecture

Status: **PLANNED**, reviewed against local source on 2026-10-21 at `7963be1`.
No M1 schema, memory repository, capture pipeline, client membership model or
retrieval hook is implemented in this checkout. Existing foundations below are
dependencies, not evidence that persistent learning works.

The original 2026-10-16 design recorded owner approval of this architecture and
a separately specified offline M1A assignment after U2 validation. That approval
statement is retained as historical context, not independently verified here.
It excluded cloud resources, paid models, production changes, commits and pushes.
The current owner request separately authorizes this documentation review and a
local commit if ready; it does not request implementation. Future work follows
its applicable bounded assignment. Memory is advisory and never authorizes
execution. See the [current review record](../persistent-learning-memory-review.md).

## Inspected foundations and gaps

Atomic per-run checkpoints, hash-chained execution logs, idempotent
ProgressRecorder synchronization, repository interfaces, worker leases/fencing,
exact-action approvals, the action execution ledger, Docker sandbox, bounded
model context and secret guard are existing foundations. Capture only committed
outcomes. Hash chains/digests are not independent proof against a writer capable
of rewriting evidence. A completed graph with a BLOCKED review is not success.
Current principal permissions are operation-level: project IDs do not establish
client isolation. No durable S3 memory adapter or client-scoped memory exists.

| Existing dependency | Local implementation and test evidence | Limit for M1 |
| --- | --- | --- |
| Checkpoints and hash-chained logs | [checkpoint](../../agent_factory/engine/checkpoint.py), [log](../../agent_factory/engine/log.py), [persistence tests](../../tests/test_persistence.py) | Local durability/integrity checks do not authenticate a hostile evidence producer. |
| Product synchronization and worker ownership | [recorder](../../agent_factory/domain/recorder.py), [claims](../../agent_factory/domain/claims.py), [worker tests](../../tests/test_worker_loop.py) | A claim binds execution ownership, not client membership. |
| Operation permissions | [principals](../../agent_factory/domain/principals.py), [API policy](../../agent_factory/api/auth/policy.py), [authorization tests](../../tests/test_auth_http.py) | No client-scoped memory grants exist. |
| Exact-action dispatch and isolation | [ActionGuard](../../agent_factory/engine/actions.py), [SandboxManager](../../agent_factory/sandbox/manager.py), [ledger tests](../../tests/test_execution_ledger.py), [sandbox policy tests](../../tests/test_sandbox_policy.py) | A remembered approval or repair never replaces current approval, ownership or isolation checks. |
| Bounded model requests | [ContextBuilder](../../agent_factory/providers/context.py), [RequestGuard](../../agent_factory/providers/guard.py), [ModelGateway](../../agent_factory/providers/gateway.py), [provider tests](../../tests/test_model_providers.py) | No memory hook; these are not a general detector of arbitrary client data or secrets. |
| Typed AWS boundary | [backend](../../agent_factory/aws/backend.py), [pack](../../agent_factory/aws/pack.py), [offline backend tests](../../tests/test_aws_boto.py) | Existing S3 metadata fixtures do not implement memory object reads/writes. |

These test references establish existing coverage, not fresh live validation.
All remaining architecture and acceptance requirements below are proposed.

## Architecture and lifecycle

Committed evidence -> trusted capture coordinator -> sanitize -> classify ->
verify -> repository interface -> fake/filesystem (first), S3 (future) ->
authorized bounded retrieval -> advisory context -> existing execution controls
-> evaluation. The engine remains free of SQL and SDKs. Agents never access a
store directly. Future agent-facing capabilities must use the existing gateway.
Do not ingest whole workspaces, prompts, logs or checkpoints automatically.

Capture uses at-least-once delivery with idempotent publication. Its deduplication
key binds trusted client/project scope and the committed source event identity;
the evidence digest is checked separately. Identical retries return the same
reference; different content for the same source identity is a conflict, not a
new lesson. A crash before publication leaves no retrievable record. Sanitize
through allowlisted fields before storage: reject uncertain secrets, client
payloads, personal information, host paths and authenticated URLs. Error logs
contain reason codes only. Classify success, failure, repair, test outcome or
workflow improvement, with explicit scope and applicability. Verify evidence
identity, integrity, completeness and environment; preserve skips and unknowns.
Model assertions cannot verify themselves. Store immutable versioned sanitized
records. Retrieve only after authorization, verification, applicability, expiry
and revocation checks. Apply as bounded cited data, never system instructions.
Evaluate resulting tests, regressions and feedback; supersede/revoke contradicted
lessons rather than rewriting history.

The capture coordinator reads committed evidence through existing repository
interfaces after its source transaction finishes. A checkpoint commit means
recording within the store's failure model, not that a test passed or an external
action occurred. Checkpoint save and product synchronization are separate steps
in the [executor](../../agent_factory/engine/executor.py); product records can
lag until [reconciliation](../../tests/test_domain_engine.py). Capture must name
its authoritative source and require consistent referenced outcomes; pending or
contradictory synchronization cannot become verified evidence. Filesystem
durability also inherits the [Windows rename limitation](../../agent_factory/util.py);
M1B must specify and test its supported crash/recovery guarantees.

A trusted verifier binds its decision to an exact scoped revision and evidence
digest, recording the authoritative source and method. Imported verification
labels and model evaluations cannot promote themselves. Synthetic fixtures stay
explicitly synthetic and never establish live, commercial or security capability.

Verification is proposed/verified/rejected, independently of lifecycle
active/superseded/revoked/expired. A verified failure does not verify its proposed
repair. Repair evidence needs a demonstrated before-failure, relevant after-pass
and applicable regressions. Approval of a workflow improvement is scoped evidence,
not transferable approval for future actions.

## Planned schema and storage

Strict factory-experience-v1 JSON, unknown fields rejected, UTC timestamps,
bounded collections, initial 64 KiB UTF-8 cap on both input and canonical record.
Reject duplicate keys, invalid Unicode, excessive depth and non-finite numbers.
Define and test exact field/count/depth limits before M1A implementation.
Fields: opaque identity/revision and previous digest; trusted client/project
scope; category, sanitized statement,
applicability/contraindications; run/node/visit and revision references; test
outcomes and artifact digests; capture/sanitizer versions and evidence type;
verification status, verifier/method/time and limitations; lifecycle, timestamps,
expiry/supersession/revocation; canonical digest. Separate evaluation records
reference exact revisions. No executable instructions, credentials, raw prompts,
complete test output or approval tokens. A digest proves neither origin nor truth.

Planned S3 key:
`memory/v1/clients/<opaque-client>/projects/<opaque-project>/<id>/<revision>.json`

Immutable application revisions plus proposed bucket versioning support recovery.
A current-status manifest is authoritative for retrieval. Verification,
supersession, revocation and publication compare its current generation and exact
revision; stale writers fail. Revoked/superseded revisions cannot become active
through retry, recapture or a delayed verification result. Missing or inconsistent
status makes the record unavailable. Corrections require a new reviewed revision
under the lifecycle policy, never an implicit reset of the old status.
Identity-level revocation/deletion tombstones also block later revisions of that
identity; any replacement requires an explicit authorized lifecycle decision.

Write the immutable object, verify checksum, then conditionally publish its
reference. Orphans are unavailable and have bounded cleanup. Deterministic
identities support retries; conflicting content fails closed. The filesystem
phase must prove the same publication/status rules under its lock. PostgreSQL
cannot make an S3 write atomic; a future hybrid adapter needs explicit recovery
for each boundary, with no exactly-once claim. A digest or S3 ETag alone is not
evidence authenticity or authorization.

Start fake/filesystem. Add PostgreSQL metadata when concurrent workers,
transactional verification/revocation or scoped query load justify it. Store
memberships, composite client-scoped keys/foreign keys, object references/digests,
transitions and evaluations. RLS may add defense in depth; test pooled connection
scope leakage. No embeddings or vector database in M1.

## Isolation and security

Derive scope from a trusted client/project membership mapping and an explicitly
permitted principal or service operation, never model input. A project ID or
worker claim alone grants no client access; execution ownership and memory
authorization are separate checks. Scope every lookup, cache key, reference,
deduplication identity and evaluation. Missing or ambiguous scope denies access.
M1A uses synthetic explicitly configured grants; real client ingestion stays
blocked until membership and verification authority are designed and tested.

M1 retrieval stays within the current client and project. Cross-project/client
retrieval and shared collections are outside M1. A future general lesson would
need separate sanitization, provenance treatment and owner-reviewed publication
and access policy; this design does not relax the existing prohibition on sending
another project's content to a model.

Recheck current authorization, revision status, expiry and applicability just
before context assembly and on every resumed use. Cache entries carry scope and
revision/status generation; cached content cannot substitute for current status.
Memory belongs only in bounded, cited `ModelRequest.inputs` assembled through
`ContextBuilder`, then checked by `RequestGuard` and `ModelGateway`. It never
enters prompts' system instructions or capability manifests. Before M1C, fix
aggregate item/byte/token limits within the existing model budget and define
deterministic selection. Record cited revisions for evaluation, not raw context.
Before M1C, define and test ordering between revocation and authorization of each
model dispatch, including retries. Previously assembled or queued context cannot
bypass the current scope, revision status and expiry checks.

Future cloud: temporary identities; separate ingest/read/retention roles;
resource/prefix-scoped S3 and KMS access; no runtime IAM/bucket-policy changes;
private buckets, public-access blocking, TLS-only access and SSE-KMS. Credentials
never enter agents, sandboxes, prompts or logs. Retrieved data cannot add tools,
fetch URLs, execute commands, override policy or suppress approval. Malformed,
unauthorized, stale or integrity-uncertain memory is refused. Optional memory
outage may yield explicit unavailable context while normal controls remain intact.

S3 remains a future adapter. Its fixed operations must preserve the typed
`AwsBackend`/capability boundary, explicit credential-slot activation, policy
checks and execution semantics; no generic SDK, CLI or agent access is added.
The existing metadata-read capability cannot retrieve or persist memory bodies.
Cloud mutations must not be classified as `cloud_read`; no deployment or IAM
operation becomes available from this design. Region, temporary identity,
operation permissions, recovery and cost limits need review before activation.

Filesystem durability needs root confinement, symlink/traversal defenses, atomic
writes, locking and restrictive permissions. Same-user hostile processes are not
isolated by those measures. Initial proposed retention: `proposed` records for
30 days, `verified` records for 180 days with review after 90 days, subject to
client requirements.
Expiry excludes retrieval at the expiry instant; there is no cached stale fallback.
Revocation cannot retract a model request already sent, but must exclude the
record from subsequent requests, including resumed runs. Purging covers versions,
indexes, derived evaluations, context caches and backups under explicit retention
policy; retain only minimal sanitized deletion evidence. Restored backups stay
unavailable until current revocation/deletion records have been reapplied. Local
purge does not delete content already sent to an authorized model provider;
provider handling must be resolved before real client data is admitted.
Do not add immutable retention locks before deletion obligations are understood.

## Validation plan

Contract/version/size/depth/duplicate keys/timestamp/digest failures; secret
canaries in nested inputs, URLs and errors; cross-client references and caches;
forged verification, skips, partial evidence, blocked outcomes and approval vs
execution; injection attempting policy/credential/tool escalation; duplicate
capture, crashes, conflicting revisions, stale writers, orphan/index failures;
expiry/revocation/compatibility and bounded retrieval; unchanged policy, sandbox
and capability evidence; evaluation and contradiction handling. Shared backend
contract tests, security mutation checks, repository-required regressions.
No tests were executed during the original design-only review.
Future acceptance also includes concurrent verify/revoke races, recapture after
revocation, membership changes between retrieval and use, resumed stale caches,
queued model retries after revocation, backup restoration after deletion, and
crash/retry at each publication boundary.
These tests do not exist yet; the current default suite validates only existing
functionality. See the separate review record for the fresh regression result.

## Historical cost assumptions (not a current quote or approved budget)

The following assumptions and reported source-review date are preserved from the
original design. This offline review checks their arithmetic only; it does not
verify current AWS rates, free allowances or service behavior. US East was an
illustration, not an approved deployment region.

Illustrative US East S3 Standard: 10 GB including revisions, 10,000 writes and
100,000 reads/month. At $0.023/GB-month, $0.005/1,000 writes and $0.0004/1,000
reads, approximately $0.32/month. One customer-managed KMS key initially $1/month,
110,000 operations at $0.03/10,000 add $0.33 before free allowances: approximately
$1.65/month storage/encryption subtotal. Key rotation can increase key charges.
The original review suggested $5–15/month as a preliminary small-pilot allowance;
that is historical context, not a current recommendation, approved budget or hard
cap. Excludes compute, audit, transfer, backups, tax and PostgreSQL.
Dedicated PostgreSQL requires separate region/availability/sizing estimate.
Reconfirm rates before deployment using [S3 pricing](https://aws.amazon.com/s3/pricing/)
and [KMS pricing](https://aws.amazon.com/kms/pricing/) (original record reports
review on 2026-10-16). No cloud operations occur in this design review or the
proposed offline M1A–M1D work.

## Incremental roadmap

All stages remain planned. Each requires its applicable bounded implementation
assignment; committing this design does not start a stage.

1. M1A: strict schemas, synthetic capture, sanitization, fake store, client scope,
   verification/lifecycle separation and security tests, isolated from U2.
2. M1B: filesystem durability, crash recovery, revocation, retention and committed
   evidence capture.
3. M1C: bounded advisory retrieval into context, application/evaluation and policy
   bypass mutation checks.
4. M1D: offline S3 adapter contract and reviewed IAM/encryption/retention artifacts;
   PostgreSQL only when justified.
5. M1E: separately authorized cloud pilot with approved scope, temporary identity,
   resources, cost bounds and cleanup.
