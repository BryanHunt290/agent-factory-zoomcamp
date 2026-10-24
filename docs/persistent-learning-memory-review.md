# Persistent-learning-memory design review

Date: 2026-10-21. Branch: `codex/persistent-learning-memory-design`.
Review baseline: `7963be1eb7fab37817905781a988a024c947d507`, the separate offline
Upwork commit. Scope: review and correct the remaining design document, record
the findings and AI assistance, and make a separate local documentation commit.

## Decision and status

The [design](design/persistent-learning-memory.md) is ready to retain as a reviewed
**future architecture** after the clarifications below. It is not an implemented
memory feature, an activation approval, or a complete implementation contract.
M1A through M1E remain planned in this checkout. No memory schema, store, capture
pipeline, client membership model or retrieval hook was found in the application
and test inventory. Existing worker, repository and model controls are foundations
only; branch names and design text are not implementation evidence.

The original 2026-10-16 approval statement and cost assumptions are preserved as
reported history. This review did not independently authenticate that earlier
approval or re-fetch its pricing sources. The current owner's request authorizes
this separate local documentation commit. No implementation stage was started.

## Findings resolved in the design

| Finding | Clarification |
| --- | --- |
| Worker ownership could be mistaken for client authorization | Require a separate trusted membership/grant mapping. M1A uses synthetic grants; missing scope denies access. M1 retrieval stays within the current client and project. |
| Committed evidence was underspecified | Separate authoritative outcome evidence from checkpoint recording, product synchronization, approval and execution. Preserve the Windows rename limitation and block verification of inconsistent or unreconciled evidence. |
| Imported records could appear to verify themselves | A trusted verifier binds status to an exact scoped revision and evidence digest. Synthetic evidence stays synthetic; a verified failure does not verify its proposed repair. |
| Revocation could be defeated by stale writers, recapture or recovery | Require conditional status generations, tombstones, checks at use and every model dispatch/retry, resumed-cache checks and reapplication of revocation/deletion records before restored backups become available. |
| Storage durability and retries needed explicit semantics | Require at-least-once capture with idempotent publication, separate source identity/content checks, inaccessible orphans, conflict rejection and crash recovery at each publication boundary. |
| Existing guards could be mistaken for a memory sanitizer | State their actual limits and route future advisory data through ContextBuilder, RequestGuard and ModelGateway. Define total retrieval budgets before M1C. Memory never changes prompts' system instructions, capability manifests or execution authority. |
| S3 and cost wording could imply deployment readiness | Preserve the typed AWS boundary and mark object storage, temporary identity, region, permissions and cost limits as future work. Historical arithmetic is checked; rates and budgets are not approved or freshly verified. |

Independent read-only reviews checked source correspondence and the security,
isolation and lifecycle requirements. The design now links the relevant existing
code and tests. Future acceptance tests are explicitly distinguished from tests
that already exist. No application, test, API contract, database migration,
dependency or runtime configuration changed.

## Validation

The repository requires its default suite for documentation changes. The fresh
run used the existing virtual environment, normal external temporary roots and
all default network/Docker guards:

```text
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3 -rs
```

Result: **1,848 passed, 466 skipped in 128.46 s**, exit 0. Of the skips, 465
were opt-in suites not requested and one required Windows symlink privileges.
No live suite was requested. Existing offline tests do not establish any M1
behavior. No new mutation check was needed because this milestone changes no
security-critical implementation.

Documentation validation passed for all 62 local link targets, whitespace,
UTF-8 encoding, the historical estimate's arithmetic and exact commit scope.
Using only the
stated historical inputs: S3 storage/requests total $0.32/month; the assumed KMS
key and operations total $1.33/month; combined $1.65/month. These calculations
do not verify current pricing, request-to-KMS ratios, free allowances or omitted
costs. No external links were fetched.

Command output, the original design snapshot and SHA-256 preservation records
are retained locally under the ignored `.local/memory-design-review-20261021/`.
They are not part of the commit. The commit contains only the design, this review
record and the corresponding `docs/ai-workflow.md` disclosure. Private `.codex/`
configuration and unrelated private local files remain untracked and
unchanged. The committed supervised-demo and Upwork files are preserved except
for the additive AI-workflow disclosure. Existing branch/tag refs, including
frozen refs and both earlier milestone branches, are unchanged.

## Remaining implementation decisions

Before their respective stages, specify the complete schema and limits,
verification authority, production membership model, lifecycle transition
contract, supported filesystem failure model, aggregate retrieval budgets and
evaluation criteria. Client-specific retention and provider data handling require
review before admitting real client data. Shared collections remain outside M1.
Cloud credentials, resources, pricing and cost bounds require their own reviewed
stage; the existing S3 metadata fixture is not a memory adapter.

These are explicit future prerequisites, not claims validated by today's tests.
No cloud request, real model call, credential discovery, installation, deployment,
external message or push occurred. The installed `aws-deployment-safety` skill
informed the offline cloud review; its
[repository reference](skills/aws-deployment-safety/SKILL.md) grants no execution
authority.
