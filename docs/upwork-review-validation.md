# Offline Upwork review validation

Date: 2026-10-21. Review branch: `codex/upwork-offline-review`, based on
`30275af654683fc10953475bc7c4fd21bb22777f` (the validated supervised-demo commit).
This record covers the previously uncommitted U1 opportunity review and U2A
fake-host receipts, the repairs found during review, and fresh offline checks.
The original [U1](opportunity-review-u1-validation.md) and
[U2A](upwork-host-receipt-u2a-validation.md) records remain unchanged historical
reports; their earlier runs were not independently authenticated by this review.

## Findings and repairs

1. **Valid Unicode could fail a round trip.** Intake accepted UTF-8 source within
   100,000 bytes, but ASCII-escaped serialization could expand it beyond that
   bound. Fake receipts had the same expansion problem at their 120,000-byte
   boundary. Normalized observations and receipt wire data now use compact UTF-8
   JSON. Observation intake checks the normalized byte size as well as the input
   size and rejects invalid Unicode surrogates with its constant error. Content
   digests and HMAC calculation retain their original ASCII canonicalization.
2. **Job-ID order could contradict acquisition time.** A valid multi-job batch
   could produce decreasing detail-event timestamps because the fake host emitted
   details in job-ID order. It now emits details by observation time, breaking
   ties by job ID. Observation and search-result identities remain sorted by job
   ID. The verifier still rejects signed events whose timestamps go backwards,
   without consuming the outstanding request.

Seven U1 and four U2A regression cases cover these repairs. Five additional
real-executor cases check wrong-run use, resume immediately before and exactly
at receipt expiry, and unchanged versus changed assessment policy before resumed
reporting. They check checkpointed outcomes and zero gateway/provider calls.
Final independent review found no additional actionable defect within the
documented trusted-configuration and process-local fake-host boundaries.

## Fresh validation

All commands ran from the repository root with the existing environment and
offline test guards. No dependencies were installed and no live opt-in flags
were enabled. Pytest used its normal external temporary roots.

| Check | Result |
| --- | --- |
| Initial existing U1/U2A tests before repairs | 90 passed in 0.53 s, exit 0. The missing cases were not covered by this baseline. |
| New U1 cases against old behavior (reviewer run) | 7 failed, 47 deselected: two Unicode round trips, four surrogate cases, and one normalized-size overflow. |
| U1 authoring validation (reviewer runs) | One sandbox run had 48 passed and 3 temporary-directory setup errors; the subsequently approved rerun had 54 passed in 0.24 s. No guards or temporary roots were changed. |
| New U2A cases against old behavior (reviewer run) | 3 failed, 1 passed, 43 deselected: chronological generation and both Unicode round trips failed; signed backwards-time rejection already passed. |
| U2A after repairs (reviewer run) | 47 passed in 0.34 s. |
| New executor boundary cases (reviewer run) | 5 passed in 0.27 s. |
| Combined final focused run | **106 passed in 0.69 s**, exit 0. |
| Full default offline suite | **1,848 passed, 466 skipped in 127.16 s**, exit 0. |
| Manual guard mutations | All six mutations caused the intended assertion failure; every baseline and restored run passed. |

The final focused command was:

```text
.venv/Scripts/python.exe -B -m pytest tests/test_opportunity_review.py tests/test_upwork_host_receipts.py tests/test_upwork_review_boundaries.py -q -p no:cacheprovider --tb=short --maxfail=3
```

The default command was:

```text
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3 -rs
```

These are separate runs, not additive counts of unique tests. Local command
output and preservation hashes are retained under the ignored
`.local/upwork-review-validation-20261021/` directory, outside the commit.
Of the 466 skips, 465 were opt-in suites not requested and one required Windows
symlink privileges. No requested live suite was skipped. All 41 local Markdown
links in the updated design, this record and the AI-workflow document resolved;
their whitespace checks passed.

## Manual mutation checks

Each check used an isolated Python process to replace one function in memory,
run the relevant test, and restore the original function in `finally`. Source
files were never rewritten and their hashes were checked afterward. All runs
used `-B`, so mutated bytecode was never written. A passing baseline, exactly one
intended call-phase `DID NOT RAISE` failure, and a passing restored run were
required; setup errors did not count as a successful mutation check.

| Deliberately removed guard | Test detecting it | Result |
| --- | --- | --- |
| Offline-only observation mode | `test_malformed_fields_fail_closed_without_echo` | Expected failure; restored pass |
| Search/detail/job identity equality | `test_malformed_fields_fail_closed_without_echo` | Expected failure; restored pass |
| Normalized observation byte cap | `test_normalized_acquisition_must_fit_the_same_byte_bound` | Expected failure; restored pass |
| Receipt HMAC comparison | `test_forgery_does_not_consume_request` | Expected failure; restored pass |
| Receipt single-use check | `test_authentication_and_single_use` | Expected failure; restored pass |
| Receipt event chronology | `test_signed_out_of_order_events_rejected_without_consuming_request` | Expected failure; restored pass |

## Scope and limitations

The commit adds the offline contracts, assessment, fake acquisition/receipt code,
two explicit templates, their tests, and documentation. It does not change the
engine, Tool Gateway, ActionGuard, capability registry, sandbox, provider layer,
database, API or dashboard. No PostgreSQL, Docker, browser, stress, AWS-live or
model-live suite was requested; these integrations are not validated by this
offline review.

The supervised-demo files, private `.codex/` configuration,
unrelated private local files, and persistent-learning-memory design were
checked against their initial hashes and preserved. The latter three remain
outside this commit. Existing branch/tag targets, including frozen refs and the
supervised-demo branch, are preserved; this work uses its own local branch.

U1 provenance remains an unverified offline fixture. U2A authenticates only the
paired fake host, which can fabricate observations; it does not verify Upwork
data, account access, or current vendor schemas. Tool/server strings in fixtures
are assertions, not verified live integration evidence. Receipt replay protection
and accepted objects are process-local; there is no durable replay ledger,
remote identity provisioning or cross-process resume. Reports preserve unknown
facts and human/approval requirements, never authorize execution or applications,
and may be BLOCKED even when the generic graph is COMPLETED.

No real model, marketplace request, proposal, message, Connects spend, delivery,
scheduler, deployment or push occurred. Live integration remains a separately
authorized milestone. See the [workflow design](design/upwork-supervised-workflow.md)
and [AI-assistance disclosure](ai-workflow.md).
