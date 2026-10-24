# U2A offline host-receipt checkpoint

2026-10-16; feature/opportunity-review-u1 at 03e95d99a81e05f3730a579d127f4b45a0c05b88.
No commit/push, paid model, live transport, marketplace action or cloud resource.

## Implementation

New host_receipts.py defines upwork-host-receipt-v1 and an explicit offline fake
host/verifier pair with a fresh process-local HMAC key. One-use requests bind
host/session/account identity, run, nonce, query digest, server/tool, acquisition
bounds and five-minute expiry. Strict signed events preserve call/trace/job IDs,
timestamps, source references and result digests. Complete search/detail coverage
is required; at most one search and three detail reads. Unknown fields, malformed
inputs, invalid signatures, stale/partial/inconsistent responses fail closed.
Session capacity is bounded to 16 issued requests. No keys are loaded from disk,
environment, credentials or the default AWS chain. Secret/source prose stays in
memory and is never projected into reports/checkpoints. Errors are constant codes.

The host_receipt_review template wraps U1's validate -> assess -> review-report
graph, uses the same CapabilityAssessment and no scoring/pricing. It rechecks the
accepted receipt and run at every stage, binds checkpoints to the receipt digest,
preserves blocked reports and approval requirements, and denies all agent tools.
Resume needs the same in-memory session/template. Session loss/expiry fails closed;
re-consuming a receipt is replay. A completed graph is not an approved review.

The separate Opportunity Agent bridge was inspected read-only earlier in U2:
local_app/upwork_bridge.py and opportunity_core/mcp_ingest.py. It matches native
tool calls/results and bounds discovery, but emits no authenticated Factory receipt.
It was not modified or launched. This new contract is not a claim that the live
server implements it; endpoint/tool labels in fixtures are scoped assertions.

## Exact validation results

Command: .venv/Scripts/python.exe -B -m pytest tests/test_upwork_host_receipts.py
tests/test_opportunity_review.py -q -p no:cacheprovider --tb=short --maxfail=3

- Initial authoring run: 36 passed, 3 setup errors; bounded parameter IDs repaired.
- Sandbox focused run after ID repair: 37 passed, 3 errors (WinError 5 accessing
  pytest's normal temporary directory). No guards or temporary roots changed.
- Explicitly approved rerun outside sandbox: 90 passed in 0.48 s.
- Default regression: 1832 passed, 466 skipped in 122.74 s, exit 0, using
  .venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3.
- First signature mutation survived because the test also changed a result digest.
  The test was strengthened to alter only the MAC; no production change needed.
- Signature-check removal: 1 failed in 0.14 s, expected DID NOT RAISE.
- Replay-check removal: 1 failed in 0.15 s, expected DID NOT RAISE.
- Both mutations restored in finally blocks, byte-for-byte; -B throughout.
- Final focused suite after test strengthening: 90 passed in 0.48 s.

The full suite preceded the test-only signature strengthening. The final focused
run covers it. Skips: opt-in PostgreSQL, Docker/adversarial, browser, AWS/model-live,
stress and one Windows symlink privilege test. No services were activated.
Focused tests include concurrent replay, malformed/forged object instances,
session loss, expiry, authenticated hostile producer data, partial responses,
secret/instruction canaries, real executor persistence, resume, changed checkpoint
binding and propagation of U1 BLOCKED results. Generic engine/security unchanged.

## Preservation and limits

All 18 pre-existing U1/original dirty or untracked files were SHA-256 compared to
the U2 baseline and were unchanged, including the original nine. All existing
branch/tag targets were unchanged. No stash, reset, staging or commit occurred.
New files: host_receipts.py, host_receipt_review.py, test_upwork_host_receipts.py,
this record, and docs/design/persistent-learning-memory.md (approved M1 design).

Authenticated means paired fake-host authentication, NOT verified live Upwork data.
A trusted signer can fabricate source observations; MACs cannot prove vendor truth.
There is no durable replay ledger or cross-process resumption, remote host adapter,
production key provisioning or actual MCP transport. This is a completed offline
contract checkpoint, not an executable live smoke test. Next live preparation must
bind actual native host events, independent identity provisioning, explicit bounds
and owner authorization. Marketplace writes remain absent/disabled.
