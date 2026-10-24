# Day 3 validation — October 24, 2026 UTC

Day 3 prepared the submission evidence without changing application code or CI
configuration. A [final security patch](DEPENDENCY_PATCH.md) updates only Vite
8.3.2 → 8.3.3 and Undici 8.11.2 → 8.12.2. Application source matches public commit
`e571dc3a8c965cdc4d24cb5d83bdc5bdf579ffd7`. The evidence commit must pass its own
complete CI before it is selected for submission; the verified SHA and run are
recorded separately in the submission release and local ready receipt.

## Fresh reviewer environment

The public checkout had no reviewer environment. Setup created a new isolated
virtual environment, PostgreSQL instance and generated calculator workspace.
Python 3.12.10, Node 24.18.0 and npm 11.16.0 were used on Windows with the existing
Docker Linux engine and pinned PostgreSQL 17 image.

```powershell
py -3.12 scripts/reviewer.py setup --offline --wheelhouse <cached-Day-2-wheelhouse>
.local/reviewer/.venv/Scripts/python.exe -u scripts/reviewer.py up
```

This was a fresh environment using cached pinned wheels, npm packages and image,
not a claim of new network downloads. Setup passed `pip check`, installed 120 npm
packages and built the frontend. The normal reviewer command was then verified
on restart: `py -3.12 scripts/reviewer.py up`.

To free the fixed ports, only the previously verified reviewer process tree was
stopped using the launcher's Windows tree-cleanup method. Its PostgreSQL container
was stopped normally and retained with its volume, network, credentials and
workspace. The separate development database on port 55432 and unrelated
containers were untouched. No reset, prune, deletion or adoption of old resources
was used. The new public checkout owns its separate reviewer instance.

## Real browser journey

The browser signed in using the generated token file; no credentials appear in
the published captures. The following live state was observed:

| Item | Evidence |
| --- | --- |
| Project | `prj-3443e74ecde6d072ae05` |
| Run | `review-approval` |
| Model | `fake / fake-deterministic-1` |
| Pending action | `delete_file`, path `legacy_calc.py`, generated calculator fixture |
| Approval | `apr-a54b55169e3c29e8` |
| Approval requested / expires | 2026-10-24 05:49:15.621517 UTC / 17:49:15.621517 UTC |
| Decision recorded | 2026-10-24 05:50:44.698364 UTC |
| Action dispatched / succeeded | 05:50:45.015527 UTC / 05:50:45.026128 UTC |
| Execution | `axe-80933d736057b803f7ba`, succeeded, exactly one attempt |
| Final run | Completed after four steps; UI reports five passing calculator fixture tests |

The fixture was inspected and a recovery copy preserved before approval. It was
generated demonstration data, not an existing user project. Approval was checked
separately from dispatch and success. Reload showed the completed run and
persisted history. See the [48-second demo and ten screenshots](evidence/README.md).
The GIF uses real capture segments at 2 fps; capture gaps, restart downtime and
two transient viewport-resize frames are omitted. Full-page captures preserve the
detail. No fabricated UI, real model call or public deployment is claimed.

## Restart and persistence

Ctrl+C stopped the public reviewer's API, worker and frontend. Both application
ports were verified closed; the launcher printed that all data was retained.
The controlling shell returned 1 for the interrupt; cleanup was verified by the
service/port state rather than inferred from that exit code.

The PostgreSQL container, volume and start time stayed unchanged. Restart with
the documented `py -3.12 scripts/reviewer.py up` succeeded. API, browser and a
read-only database transaction confirmed:

- Same project, completed run, run update timestamp and approval decision.
- Same successful execution and five timeline events.
- Exactly one decision, one execution and one execution-attempt row.
- No queued work (`available_at` null), no worker owner and no second execution.

The sanitized [before/after proof](evidence/persistence.json) records these
assertions. Raw local process/credential-path diagnostics are excluded from the
public repository. The new public reviewer app remains available locally. After patching the two
dependencies, setup and documented startup were repeated, then API, browser and
read-only database checks again verified the same history. See the
[patched runtime record](evidence/patched-runtime.json) and tenth screenshot.

## Documentation and delivery checks

The official Project 1 cutoff is October 26, 2026 at 23:00 UTC / 5 p.m. Denver MDT.
Three peer reviews are due November 2 at 23:00 UTC / 4 p.m. Denver MST. Verified
against the [course page](https://courses.datatalks.club/ai-dev-tools-2026/project/project1)
and [official calendar](https://courses.datatalks.club/ai-dev-tools-2026/calendar.ics).
The form requires public repository URL, exact commit and certificate name;
the owner confirmed **Bryan Hunt**. Optional hours remain blank.

The current 12-criterion rubric was reviewed without claiming a guaranteed score.
Documentation now separates historical live-provider acceptance from this fake
demo, explains sequential reviewer commands and links actual development-agent
collaboration. Full application containers, production identity/deployment/CD,
operational metrics/traces UI, user-impact alerts and a bounded on-call responder
remain gaps. Earlier full-suite counts are in [publication history](PUBLICATION.md);
the final CI run must validate the new evidence commit too.

This record proves preparation and local validation. It does not claim a course
submission receipt or completion of the three mandatory peer reviews.
