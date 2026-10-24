# U1 offline validation record

Date: 2026-10-16. Implementation branch: `feature/opportunity-review-u1`, created
at `03e95d99a81e05f3730a579d127f4b45a0c05b88`. No commit or push was made.

## Results

| Check | Exact result |
| --- | --- |
| Initial focused authoring run | 40 passed, 4 setup errors; parameter IDs were then bounded to avoid enormous test names/output. |
| Focused sandbox run with bounded IDs | 41 passed, 2 errors: Windows denied access to pytest's normal temporary directory; both real-workspace tests could not set up. |
| Same focused command, explicitly approved outside the sandbox | 43 passed in 0.16 s. No guards disabled or test roots redirected. |
| Default offline regression, sandbox | Stopped at configured maxfail=3: 1 skipped, 3 temporary-directory setup errors. |
| Same regression command, explicitly approved outside sandbox | **1,785 passed, 466 skipped in 124.74 s**, exit 0. |
| Expanded focused suite | **47 passed**; four checkpoint-resume/contract cases were added after full-suite collection and tested separately. No implementation change accompanied those cases. |
| Identity-guard mutation | Removing search/detail/job ID equality produced the expected mismatch-test failure: 1 failed, 46 passed. Restored baseline: 47 passed. |
| Offline-only mutation | Removing mode=offline_fake validation produced the expected live-mode-test failure: 1 failed, 46 passed. Restored final baseline: **47 passed in 0.22 s**. |

Commands (from the repository root):

```text
.venv/Scripts/python.exe -B -m pytest tests/test_opportunity_review.py -q -p no:cacheprovider --tb=short
.venv/Scripts/python.exe -B -m pytest -q -p no:cacheprovider --tb=short --maxfail=3
```

The default run used the then-current 43-case U1 suite; the final focused run
covered all 47 cases. These are separate runs, not additive counts of unique
tests. Mutations were restored in finally blocks. Bytecode generation was
disabled with -B throughout the new module's tests and mutation runs.

Skipped regression checks were opt-in PostgreSQL, Docker/adversarial, browser,
AWS/model-live, and stress coverage, plus one Windows symlink privilege check.
No PostgreSQL service, Docker engine, browser, cloud, or paid model was activated.
Those integrations are untested by U1. Core execution, security, capability,
provider and persistence code were not changed; no new tool pack was added.

## Preserved boundaries and limitations

The nine pre-existing modified/untracked files were hashed before branching and
checked again at closeout. Existing branch and tag targets were captured before
branching and checked again; only the new U1 branch was added. The unfinished
supervised-demo source, tests, documentation and private configuration were not
edited, staged, reset or stashed. U1 adds files only; other repositories were not
accessed during implementation. Existing historical validation documents remain
unchanged and are not superseded by this record.

U1 is offline fixture integration, not live acquisition. Source provenance is
explicitly unauthenticated. Reports omit source text and preserve unknown facts,
approval/human requirements, and missing mappings. They never authorize an
application or execution. A BLOCKED review finishes the generic graph; its domain
status must be checked separately from the generic COMPLETED run status.

No scoring/pricing, proposal draft, message, Connects spend, commitment, code
execution, delivery, scheduling, hosted UI, or live authentication is implemented.
The next safe milestone is independent review of the handoff/receipt boundary and
an offline host-adapter contract test. Any later live read smoke test needs an
explicitly enabled, bounded transport; no current configuration can enable it.
