# Final reviewer dependency security patch

On October 24, 2026 UTC, the Day 3 frontend CI installation surfaced two
high-severity vulnerable **package entries**, covering nine underlying advisories.
The initial demo had already been captured. Before selecting the final submission,
the reviewer toolchain was patched and the original capture provenance retained.

| Package | Before | After | Scope |
| --- | --- | --- | --- |
| Vite | 8.3.2 | 8.3.3 | Direct development dependency; serves the local reviewer UI |
| Undici | 8.11.2 | 8.12.2 | Transitive development dependency through jsdom 30.1.2 |

Only these two package entries changed. The other **143 package entries are
unchanged**, with no packages added or removed, no override, and no new direct
Undici dependency. The existing Node version and dependency ranges support both
patched versions. Application source, React dependencies and CI configuration
were not changed.

## Validation

- Clean `npm ci`: 120 packages installed, 121 audited, **0 vulnerabilities**.
- Independent `npm audit --json`: exit 0, **0 vulnerabilities**.
- `npm run check`: generated contract, TypeScript, **57 tests in five files passed**.
- `npm run build`: passed with Vite 8.3.3.
- Reviewer setup re-run using the pinned wheel/npm cache; `pip check` and build passed.
- Documented `py -3.12 scripts/reviewer.py up` startup passed with installed
  Vite 8.3.3 and Undici 8.12.2; authenticated API/proxy and browser reload passed.
- Same completed run, approval decision and successful execution remained, with
  one decision/execution/attempt and no requeued work. The same PostgreSQL
  container/volume stayed running; no data was reset or deleted.

See the [audit result](evidence/dependency-audit.json),
[patched runtime proof](evidence/patched-runtime.json) and
[post-patch screenshot](evidence/10-after-security-patch.jpg).
The final candidate must also complete the full hosted CI suite; its matching
SHA and run are recorded in the submission release and local ready receipt.

## Official advisories

| Package | Primary advisory | Severity | Fixed version in the selected major line |
| --- | --- | --- | --- |
| vite | [GHSA-587h-7wmj-h5pm](https://github.com/vitejs/launch-editor/security/advisories/GHSA-587h-7wmj-h5pm) | high | 8.3.3 |
| vite | [GHSA-698v-4r3x-3vcv](https://github.com/vitejs/launch-editor/security/advisories/GHSA-698v-4r3x-3vcv) | moderate | 8.3.3 |
| vite | [GHSA-r825-w8mm-5pxw](https://github.com/vitejs/launch-editor/security/advisories/GHSA-r825-w8mm-5pxw) | moderate | 8.3.3 |
| vite | [GHSA-9jrq-w75r-8gcw](https://github.com/vitejs/vite/security/advisories/GHSA-9jrq-w75r-8gcw) | low | 8.3.3 |
| vite | [GHSA-rq7h-c2jc-7f22](https://github.com/vitejs/vite/security/advisories/GHSA-rq7h-c2jc-7f22) | moderate | 8.3.3 |
| vite | [GHSA-vfpm-58rq-9qcg](https://github.com/vitejs/vite/security/advisories/GHSA-vfpm-58rq-9qcg) | moderate | 8.3.3 |
| undici | [GHSA-xjj6-f33q-929c](https://github.com/nodejs/undici/security/advisories/GHSA-xjj6-f33q-929c) | high | 8.12.2 |
| undici | [GHSA-4qv8-2p9j-2685](https://github.com/nodejs/undici/security/advisories/GHSA-4qv8-2p9j-2685) | moderate | 8.12.2 |
| undici | [GHSA-f72v-6w45-q472](https://github.com/nodejs/undici/security/advisories/GHSA-f72v-6w45-q472) | moderate | 8.12.2 |

The Vite findings primarily concern development-server/editor middleware; the
reviewer uses loopback `vite preview`. Undici is used through the Node test
environment, while authored frontend requests use browser fetch. These are
source-based reachability observations, not exploit-test results. The affected
packages were patched rather than treating the development dependency label as
proof of safety. No live attack or exploitation was claimed or attempted.

## Capture provenance

The 48-second GIF and screenshots 01–09 retain the original captured toolchain.
Application code is unchanged. Screenshot 10 and the patched-runtime proof were
captured after the security patch; they show the same recorded execution history.
The [manifest](evidence/manifest.json) distinguishes the two capture stages.
