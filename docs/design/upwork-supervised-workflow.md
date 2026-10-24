# Offline opportunity capability review and host receipts

U1 adds an opt-in, project-specific validate -> assess -> review-report workflow.
It uses the existing Executor, ProjectTemplate and CapabilityAssessment without
changing the engine, sandbox, provider layer, API, database, or tool packs.
The implementation was prepared with Codex assistance under the owner's bounded
U1 assignment. It does not use the separate supervised-demo implementation.

## Scope and boundaries

The Opportunity Agent remains a separate application. U1 does not import it,
copy its scoring/pricing, launch its CLI, or change its deployment. Business
ranking, pricing, proposal generation and approval workflows are not implemented
here. The review explicitly returns null ranking/pricing and false application
readiness/execution authorization.

No live transport exists. FakeAcquisition is a pure in-memory fixture source with
bounded search/get operations; live_acquisition always refuses, irrespective of
configuration. Importing this package registers no tools or template globally.
No environment setting can activate marketplace access. U1 agents have empty
tool allowlists and all categories denied. They never call a model or execute
source content. Generic engine code alone persists the projected agent outputs.

MCP find_jobs search/smart_search/get identifiers are recognized as
provenance data, not callable commands. The connected MCP host is outside the
Factory process. Existing authentication or the separate Opportunity Agent's
deployment does not establish a usable Factory transport.

## Project-owned observation v1

Input is a JSON string, at most 100,000 UTF-8 bytes, with exactly:
`schema_version: opportunity-observation-v1`, `mode: offline_fake`, and `jobs`.
There must be one to three jobs, unique by numeric job ID. Empty batches are
refused; a future live no-results outcome needs a separate transport contract.
Duplicate JSON keys, unknown fields, non-finite values, malformed types and
unsupported versions fail the whole batch. This is not a vendor wire schema or
the separate Opportunity Agent's mcp-opportunity-v2 format.

Normalized observations also fit the same 100,000-byte bound. They use compact
UTF-8 JSON so accented and non-BMP text can pass from fake acquisition back through
intake without ASCII-escape expansion. Invalid Unicode surrogates are refused.
If normalization itself would exceed the bound, intake rejects the batch before
returning it. Observation fingerprints retain their original canonical digest
representation; this serialization change does not change content identity.

Each job has exactly these fields (see contracts.py and the test fixture for the
complete executable contract):

| Field | Meaning and limits |
| --- | --- |
| job_id, url | Numeric ID up to 24 digits; HTTPS Upwork job URL with the same numeric ID after ~02. Only exact upwork.com/www.upwork.com hosts; no userinfo, port or fragment. Queries are discarded, never fetched or persisted. |
| title, description | Nonempty bounded untrusted text, 300/20,000 characters; retained only in memory. Neither is copied into state, reports or task metadata. |
| observed_at | UTC YYYY-MM-DDTHH:MM:SSZ; no future observations; strictly younger than one hour at each stage. |
| provenance | Exact find_jobs tool ID, search or smart_search, get, matching search/detail job IDs, 32-character lowercase hex trace reference. These are consistency checks, not authentication. |
| contract_type | hourly, fixed, or null. |
| terms | Nullable currency (USD only in v1), fixed_amount, hourly_min/max, duration, workload. Money is bounded nonnegative decimal text; incompatible terms and reversed hourly bounds fail. No currency inference. |
| client | Nullable payment_verified, spend_usd, contracts_total, feedback_score/count, hire_rate_percent. Ratings bounded to 5; hire rate bounded to 100. No client names or raw objects. |
| activity | Nullable proposal tier, interviewing, hired, offered. Tiers stay tiers. |
| connects_cost, applied, can_apply | Nullable bounded integer and strict booleans. Already-applied plus can-apply is refused as contradictory. |

Only typed facts, canonical links and SHA-256 digests survive U1 projection. Trace
references and source text do not. Secrets in source text or discarded URL
queries are not persisted. Unknown keys, including credential fields, are
refused with a constant error; rejected values are never echoed. This design
does not claim a general arbitrary-secret detector. The caller must not supply
credentials or put source data into the generic Executor task/metadata arguments.
Registry/policy configuration and requirement mappings are trusted local inputs.

Each job in a ready U1 report is labelled `unverified_offline_fixture`. Neither a
digest, a supplied trace ID nor matching fields authenticates the origin of a
record. The digest binds normalized content for integrity checks; it is not a
signature.

## Assessment and workflow

RequirementMapping is supplied separately by the trusted operator, bound to the
exact observation digest. It contains one to thirty existing Requirement values
with r1..r999 identifiers; no descriptions or source-provided attestations.
Changed source content invalidates the mapping. Missing mappings remain visible
as missing and do not become supported. Mapping completeness is an operator
judgment; U1 does not infer job requirements from prose or tags.

CapabilityAssessment is called directly with the supplied target registry and
assessment policy. It preserves supported, missing, unavailable, requires_human,
requires_approval and evidence. These describe the target installation, not
permission for the U1 agent to call tools. Existing registry TESTED evidence
means a referenced test exists, not that this run executed that test. Portfolio
experience, commercial delivery ability and marketplace eligibility are distinct.

The template is created explicitly:

```python
from agent_factory.factory import build_executor
from agent_factory.providers import FakeProvider
from agent_factory.templates.opportunity_review import opportunity_review_template

# payload: bounded observation JSON; mappings: operator-bound mappings.
# registry and assessment_policy describe the target installation.
template = opportunity_review_template(
    payload, mappings=mappings, registry=registry,
    assessment_policy=assessment_policy,
)
workspace = template.create_workspace(new_workspace_path)
executor = build_executor(template, FakeProvider({}), workspace)
state = executor.start(template.task, run_id="opportunity-review-1")
report = state.review
```

Use a new confined workspace, never a source repository. The report is in the
existing checkpoint's `review` field; no additional report file, SQL table,
HTTP route, CLI command or dashboard feature is added.

validate checkpoints the observation/mapping binding in plan; assess checkpoints
the report digest in implementation; review_report recomputes the assessment and
checks that digest before publishing review. A restart must reconstruct the
same inputs. A changed observation, expired snapshot, mapping or assessment
evidence blocks the report. This is conservative: even changed registry evidence
timestamps can require a fresh run. Registry availability is its supplied
snapshot, not a new live probe. Source contents are never recovered from disk.

REVIEW_READY means the assessment report is available for owner inspection, not
that the job is suitable or an application may be made. Invalid/changed input
produces review.status=BLOCKED. Both paths finish the generic graph, so the
generic run status is COMPLETED even for a blocked review; consumers must read
the domain report status. There is no new engine status or implicit retry.
Malformed constructor configuration may instead raise ObservationError before
a workspace/run is created. Unexpected trusted-component errors still fail the
generic run; U1 does not swallow them.

## U2A: explicit offline host receipts

`integrations/opportunities/host_receipts.py` pairs an explicitly created fake
host and verifier with a fresh in-memory HMAC key. It implements the project-owned
`upwork-host-receipt-v1` contract, not a live MCP client or vendor authentication.
Requests bind the host, session, opaque account binding, run, nonce, exact query,
tool/server and bounds: one search, at most three details and five-minute expiry.
The expiry instant is expired. A session permits at most 16 issued requests.

Acceptance checks the signature, request identity, freshness, complete matching
search/detail coverage and event consistency before consuming the request under
a lock. A forged or inconsistent receipt does not consume it. Replayed requests
fail, including concurrent consumers; accepted receipts are held by that session.
Closing or losing the session invalidates them. This is process-local replay
protection, with no durable replay ledger or cross-process recovery.

Wire receipts are bounded to 120,000 UTF-8 bytes and use compact UTF-8 JSON;
signature calculation retains the existing ASCII canonical representation. The
fake host emits details in observation-time order (job ID breaks ties), while
observations and search-result identities remain sorted by job ID. The verifier
continues to reject signed events whose timestamps go backwards.

`templates/host_receipt_review.py` explicitly wraps the U1 graph. Trusted
coordinator code consumes the receipt before constructing the template. Every
stage then rechecks its accepted receipt, run, session and expiry; resumed stages
also verify the checkpoint's receipt binding. Resume requires the same live
in-memory template/session. U1 still rechecks input and assessment digests.
Wrong-run use, expiry at resumed assessment and changed assessment policy before
reporting produce a BLOCKED domain report. The generic run can still be COMPLETED.

Reports include bounded audit references and label the signer as an authenticated
**offline fake host**, with `live_upwork_verified=False`. A trusted signer can
fabricate observations; authentication does not prove vendor truth. Raw receipt,
MAC, key, query, title and description are not persisted by the template. Agents
have no tool permissions and make no model calls. No live transport, remote key
provisioning, account verification, marketplace write or scheduler is implemented.

## Validation and remaining milestones

Focused tests cover strict fields, duplicate keys, size/depth, timestamps, URL
identity, all-or-nothing batches, unknown values, mapping integrity, approval
retention, secret-free checkpoint projection, empty tool permissions, actual
Executor checkpoint/resume, and source-level absence of execution/transport calls.
The repository's normal offline guards remain enabled. The current
[review and validation record](../upwork-review-validation.md) covers the Unicode
and event-ordering repairs, real-executor boundary regressions and guard mutations.
The historical [U1 record](../opportunity-review-u1-validation.md) and
[U2A record](../upwork-host-receipt-u2a-validation.md) retain their original results.

The offline contract and receipt boundary have been reviewed. A future milestone
would design a bounded host-to-Factory read adapter with independent identity
provisioning and durable replay semantics. Do not relabel these fixtures as
live-tested. A later explicitly authorized smoke check may use one
official search and up to three gets; scheduling remains disabled. Do not reuse
the existing broad native-client bridge as a sandbox escape or give an agent
arbitrary MCP dispatch.

Later milestones may connect Opportunity Agent's ranking/drafting reports,
owner-reviewed work intake, isolated engineering/testing, and delivery manifests.
Proposal previews, submissions, client messages, Connects/boost spending,
contract commitments, production deploys and final delivery each require exact,
expiring owner authorization and existing durable dispatch/reconciliation.
Uncertain non-idempotent outcomes must never be blindly retried. Client code
remains untrusted and needs healthy Docker; there is no local execution fallback.
iPhone/cloud supervision requires separate production identity, hosted-dashboard,
notification, worker ownership and checkpoint-storage designs. The current
loopback API and project boundaries remain unchanged.
