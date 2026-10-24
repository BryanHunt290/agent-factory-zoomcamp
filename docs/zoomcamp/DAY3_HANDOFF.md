# Day 3 submission handoff

This is a preparation checklist, not evidence that Day 3, submission or peer
reviews are complete. Keep the final scope to a working, reproducible application
and an accurate evidence package. Use the [submission checklist](SUBMISSION_CHECKLIST.md),
[rubric mapping](RUBRIC_MAPPING.md) and [reviewer guide](../REVIEWER_GUIDE.md) together.

## 1. Confirm the official cutoff first

- [ ] Sign in to the course platform and verify the project submission deadline,
  its timezone and the separate peer-review deadline. Record the exact timestamps
  and convert them to the submitter's local timezone.
- [ ] Confirm the submission form, required fields and current rubric against
  [the current official project requirements](https://github.com/DataTalksClub/ai-dev-tools-zoomcamp/blob/main/project/README.md)
  and the [current rubric mapping](RUBRIC_MAPPING.md).
- [ ] Set a personal submission time before the official cutoff. October 26 is
  only the working target from the owner's three-day window, not a verified cutoff.

## 2. Verify and capture the reviewer journey

- [ ] From a fresh checkout of the candidate commit, follow the reviewer guide:
  `py -3.12 scripts/reviewer.py setup`, then `py -3.12 scripts/reviewer.py up`.
  On Linux/macOS use `python3.12`. Record platform/runtime versions and the commit.
  Use an independent machine if existing reviewer resources occupy the default
  ports; do not run parallel reviewer checkouts on the same host. Stop an active
  launcher before restarting that checkout, but remember its database stays
  running. Port/ownership refusals protect existing data: do not reset, delete or
  adopt another checkout's resources to bypass them.
- [ ] Sign in using the generated local token file; open the seeded project,
  inspect the exact action and expiry, approve it, and observe the recorded
  decision and the worker's completed execution. Reload and confirm persisted
  history. An approval record alone is not proof that the action executed.
- [ ] Capture a short demonstration and screenshots of the project, pending
  approval and final history. Keep token contents, signing keys, database URLs,
  credential files and unrelated personal data out of the recording. Explain
  that the model is a deterministic fake and the application runs locally.
- [ ] Stop the launcher with Ctrl+C and verify that a restart preserves the
  review history. Do not reset containers or delete volumes to obtain a clean demo.

## 3. Freeze the evidence for the final commit

- [ ] Review all 12 criteria against actual evidence and retain the gaps below.
  No self-estimated score guarantees acceptance or a final grade.
- [ ] Select the exact final public commit. Confirm all CI jobs have completed
  successfully on that SHA; a running job or a green run on an earlier SHA is
  insufficient. Save the run URL, results and any reported skips or limitations.
- [ ] Confirm README links, setup instructions, task/AI-use evidence and the demo
  can be reached by a reviewer. Link the final commit, CI run and demo in the
  submission package. Use [publication scope](PUBLICATION.md) for provenance and
  [Day 2 validation](DAY2_VALIDATION.md) for the recorded local checks.

## 4. Submit and retain proof

- [ ] Submit the final repository/commit and required evidence through the course
  platform before the verified cutoff. A GitHub push is not course submission.
- [ ] Save the platform's submission receipt or confirmation and its timestamp.
  Check that the submitted links and commit match the final evidence package.
- [ ] Complete all **three mandatory peer reviews** by their separate deadline
  and retain the platform's completion status.

## Gaps to keep explicit

- Full application containerization is incomplete: PostgreSQL and execution
  isolation use containers; the reviewer API, worker and frontend run on the host.
- Production identity, public application deployment and CD remain unimplemented
  or unverified. The [deployment runbook](../deployment.md) is preparation only.
- Correlated operational metrics/traces and a viewing interface, verified
  user-impact alerting, and an alert-triggered bounded on-call responder remain
  outstanding. Approval notifications do not satisfy those operational criteria.

Do not claim these items from source publication, a successful local demo or CI.
Do not introduce late unverified runtime changes solely to improve the rubric map.
