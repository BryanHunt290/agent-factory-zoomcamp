# Frontend (Dashboard v1)

| | |
| --- | --- |
| Status | **Dashboard v1 implemented.** Local development only, behind the development token issuer. |
| Branch | `feature/dashboard-v1` (from `feature/auth-worker` at `2ad2b6e`) |
| Scope | Observe, approve, reconcile. No project creation, run start, or run control from the dashboard. |
| Not production | No production identity provider, no public binding, no deployment. The sign-in screen says LOCAL DEVELOPMENT. |
| Reliability | Failure and recovery evidence: [dashboard-reliability.md](dashboard-reliability.md) |

The dashboard is a **disposable view**. PostgreSQL and the API are the
source of truth; the browser keeps only the current tab's development token.
Closing the browser, clearing its storage, or restarting the frontend server
changes nothing about any project, run, approval, or action (tested in a
real browser: [dashboard-reliability.md](dashboard-reliability.md)).

## 1. Running it

Needs Node.js 20 or later (built with 24.18) and the API with development
authentication ([authentication.md](authentication.md)).

```bat
rem Once: the frontend's packages, exactly as pinned in frontend\package-lock.json
cd frontend
npm ci
cd ..

rem The API, with development authentication (see docs/authentication.md for the one-time setup)
set AGENT_FACTORY_AUTH_MODE=development
set AGENT_FACTORY_AUTH_DEV_ISSUER_DIR=.local\dev-issuer
set AGENT_FACTORY_DATABASE_URL_FILE=.local\database.url
.venv\Scripts\python -m agent_factory api serve

rem In another window: a token for an enrolled person (written to a file, never printed)
.venv\Scripts\python -m agent_factory auth dev-issuer token --subject me --name "Me" --minutes 120 --out .local\dev-token.txt

rem The dashboard: the production bundle behind the strict Content Security Policy
cd frontend
npm run build
npm run preview
rem   -> http://127.0.0.1:4173   (npm run dev serves http://127.0.0.1:5173 for editing)
```

Open the address, paste the token (or choose `.local\dev-token.txt`), and
sign in. To see something move, queue a run and start a worker
([workers.md](workers.md)):

```bat
.venv\Scripts\python -m agent_factory worker enqueue --project-id PRJ --scenario approval
.venv\Scripts\python -m agent_factory worker run
```

The frontend server proxies `/api/`, `/healthz`, and `/readyz` to
`AGENT_FACTORY_API_ORIGIN` (default `http://127.0.0.1:8000`). It refuses to
start unless that is `http://` on a loopback host with no path
(`frontend/vite.config.js`; tested with four non-loopback origins).

Frontend checks, from `frontend/`:

| Command | What |
| --- | --- |
| `npm test` | Vitest: unit and component tests (jsdom) |
| `npm run typecheck` | `tsc --noEmit`, strict |
| `npm run contract:check` | `src/api/schema.ts` is exactly what `openapi.yaml` generates |
| `npm run contract:generate` | Regenerate it after a contract change |
| `npm run check` | All three |

The browser, end-to-end, and reliability suites are pytest suites:
[testing.md](testing.md) and section 14.

## 2. Architecture

```text
 Chrome (phone or desktop)          one origin: http://127.0.0.1:4173
   │  the bundle: index.html, one script, one stylesheet (no CDN, no inline script)
   │  fetch /api/v1/..., /readyz      Authorization: Bearer <development token>
   ▼
 vite preview (Node)                static files + security headers + proxy to loopback only
   ▼
 FastAPI (python -m agent_factory api serve, 127.0.0.1:8000)
   │  verifies the token, maps it to an enrolled principal, checks one permission per operation
   ▼
 PostgreSQL  ◄────────────  workers (python -m agent_factory worker run)
```

- **Same origin.** The browser only ever talks to the frontend server, which
  proxies API paths to the API on loopback. The API needs no CORS settings
  and was not changed; it still refuses foreign `Host` headers.
- **The API records intent; workers execute.** The dashboard sends decisions
  and reconciliation answers; it never runs, retries, or reconstructs
  anything. Approval is not execution: the dashboard shows what workers
  report through the API.
- **No API change.** Dashboard v1 uses the existing v1 contract
  (`openapi.yaml`) unchanged.

| Path (`frontend/`) | What |
| --- | --- |
| `src/api/schema.ts` | Generated from `openapi.yaml` by `scripts/generate-contract.mjs` (SHA-256 of the contract in its header) |
| `src/api/types.ts` | Readable aliases of the generated types |
| `src/api/client.ts` | **The only module that calls `fetch`, and the only one that sets `Authorization`** (a test enforces both) |
| `src/api/errors.ts` | Failure kinds and the plain-language message for each |
| `src/auth/session.ts` | The development token in `sessionStorage` |
| `src/lib/polling.ts` | Bounded polling with backoff, a pause while hidden, and freshness |
| `src/lib/router.tsx` | A small History API router (five screens, no router library) |
| `src/lib/labels.ts`, `format.ts` | Status words, symbols, and times |
| `src/screens/` | Sign-in, dashboard, attention inbox, project detail, approval detail |
| `src/screens/approval/` | The decision and reconciliation panels, and the submission state machine |
| `src/components/` | Shared pieces: status badges, the stale-data banner, error notices, system status, lists |
| `vite.config.js` | Build, the loopback-only proxy, and the security headers (Node only; never bundled) |

Runtime dependencies are `react` and `react-dom` only: no router, state,
styling, or component library, no analytics, no external fonts.

## 3. Screens

| Screen | Path | Shows |
| --- | --- | --- |
| Sign-in | any, when signed out | LOCAL DEVELOPMENT notice; token field or token file; why the last session ended |
| Dashboard | `/` | Needs your attention (first); a summary of the period (completed, failed, repaired, blocked, uncertain, running); active projects; recent milestones; other projects. Period: 12 h, 24 h, or 7 days |
| Attention inbox | `/attention` | Everything that needs a person, oldest first, each marked **Approval required** or **Reconciliation required**, with project, summary, waiting time, and expiry |
| Project | `/projects/{id}` | Status, what needs you there, runs (with a link to a pending approval, and a line of model usage when the run called a model: the provider/model, calls, tokens, and a cost only when one is configured), and milestones for the period, newest first ("Repaired an earlier failure" marked) |
| Approval | `/projects/{id}/approvals/{id}` | Headline state; what will happen (summary and the exact parameters); why; risk; context (project, run and its status, agent and workflow step, requested, expires, decision, notifications); technical details (fingerprint, IDs) folded away; the decision or reconciliation panel; what happened to the action and its timeline |

Every screen rebuilds what it shows from the API. Nothing about the workflow
is stored in the browser (tested: storage holds only the token; no cookies,
IndexedDB databases, or caches).

## 4. Mobile design

- Designed at 375 × 812 first; one column, with a two-column layout from
  720 px. No horizontal scrolling on any screen at 375 px or 1280 px
  (browser-tested), including long project names and deep paths, which wrap
  (`overflow-wrap: anywhere`).
- Touch targets at least 44 px (buttons 48 px), checked in the browser for
  the decision buttons, the reconciliation choices, and inbox items.
- System fonts only. Status is always words plus a symbol; color only
  reinforces it.
- The decision sits after the facts it depends on (what, why, risk), and a
  first tap never sends anything.

## 5. API client

`ApiClient` (`src/api/client.ts`) is the single data-access layer.

- One method per operation the dashboard uses (`listProjects`, `getProject`,
  `listRuns`, `getRun`, `listProjectMilestones`, `listAttention`,
  `getApproval`, `decideApproval`, `reconcileAction`, `readiness`), typed by
  the generated contract.
- Requests: same origin, `credentials: "omit"`, `cache: "no-store"`,
  `redirect: "error"`, `referrerPolicy: "no-referrer"`, and a 10 s timeout
  (an `AbortController`).
- Failures become an `ApiError` with a kind: `network`, `timeout`, `gateway`
  (the frontend server's proxy reporting the API down: a 5xx that is not a
  problem document), `problem` (the API's RFC 9457 answer, with `code` and
  `request_id`), `unexpected`, or `signed_out`.
- **It never retries a write by itself.** A decision that may have been lost
  is settled by asking the API (section 8). Reads are retried by the poller.
- 201 and 200 answers to a decision are told apart ("recorded now" versus "it
  was already on record").
- A 401 ends the session (the token is forgotten and the sign-in screen says
  why), except for the sign-in check itself.

## 6. Authentication (LOCAL DEVELOPMENT only)

This is **not** the production phone sign-in; that needs a real identity
provider and is a later milestone.

- The person pastes a development token, or picks its file. The dashboard
  checks its shape, then asks the API (`GET /api/v1/projects?limit=1` with
  that token) and keeps it only if the API did not refuse it. A refused token
  is never stored.
- The token is kept in **`sessionStorage`**: it survives a refresh in the
  same tab and disappears when the tab closes. Never `localStorage`, cookies,
  URLs, or query strings (tested in the browser). If storage is unavailable
  it is kept in memory only.
- It is sent only in the `Authorization` header (tested on every API request
  in the browser), never logged (the source has no `console.` calls; the
  browser tests watch the console), never shown, and never part of the
  bundle (the bundle is searched for it).
- Nothing about identity is decided in the browser: the API verifies the
  token and authorizes every request. The frontend has no bypass, and hiding
  a button is never the protection (a person without `approvals:decide` who
  sends a decision gets a 403, explained, and nothing is recorded:
  browser-tested).
- Ends with: "Sign out", closing the tab, the token expiring (the API allows
  30 s of clock skew past `exp`), or the person being disabled. The last two
  take effect at the next poll (browser-tested).

## 7. Polling and stale data

- **Bounded polling** (`usePolling`): one load at a time per resource; the
  next is scheduled only after the previous finished. Attention: every 5 s.
  Active views (a running or waiting run, a moving approval): 5 s. Settled
  views: 15–30 s. Readiness: 10 s.
- **Backoff** after failures: the interval, then ×2 per failure, capped at
  15 s; the first success resets it.
- **Paused while the tab is hidden**, including a load that finishes after the
  tab was hidden; loaded at once when it is visible again or the browser
  reports it is back online.
- **Immediately after a decision**, the approval and its run are reloaded.
- **Errors never erase the display.** The last good data stays, marked stale:

  > Connection lost — showing data from 6:40:20 AM. The dashboard keeps
  > trying and will update by itself. Decisions are disabled until it
  > reconnects.

  When the API answers 503 (its database or key source is down) the banner
  starts "Agent Factory is temporarily unavailable — showing data from …".
  **Decisions are disabled while data is stale.**

## 8. Approval UX

1. **Approve…** or **Reject…** opens a confirmation that restates the action,
   the project, the requesting agent, and the expiry. Focus moves to it.
2. The confirming button (**Yes, approve this action** / **Yes, reject this
   action**) ignores taps for 400 ms after the confirmation appears, so a
   double tap on the first button cannot confirm.
3. While the request is in flight the controls are disabled and the screen
   says "Sending your approval to Agent Factory…". A second tap sends nothing
   (an in-flight guard as well as the disabled button).
4. The request carries the **fingerprint exactly as the API supplied it**; the
   dashboard never computes or edits one.
5. **Nothing is shown as decided until the API confirms it.** After every
   outcome the approval and run are reloaded, and the screen shows that
   state.

What happens when the answer does not arrive (each case is tested in a real
browser against the real API in [dashboard-reliability.md](dashboard-reliability.md)):

| What happened | What the person sees | Can they send again? |
| --- | --- | --- |
| 4xx: the API refused (409 conflict, 403, 429, …) | The reason; for a conflict "Nothing was recorded by this attempt"; the request ID under Support details | No; the screen refreshes to the state on record |
| No answer, or a 5xx | "We couldn't confirm whether your approval reached Agent Factory. Checking with Agent Factory; nothing will be sent again without you." | Not yet |
| … then a reload that **started after the failure** shows the same decision on record | "Your approval reached Agent Factory and is on record." | No |
| … shows a different decision on record | "A different decision is on record. Nothing more was sent." | No |
| … shows the request still waiting, nothing recorded | "Your approval did not reach Agent Factory. Nothing was recorded. You can send it again." | Yes: only **the same decision**, which the API accepts once (an identical repeat is a no-op 200) |
| … shows the request closed (expired, cancelled) | "This can no longer be decided. Your approval was not recorded." | No |

A 5xx is treated as "unknown", not as a refusal, because the API can commit
a decision and still fail to answer (a defect found while building the
reliability suite, and fixed). Data loaded before the failure is never used
to decide: it may predate the decision.

### Notification status

The approval's context shows **Notifications**: one line per attention
event, from `ApprovalRequest.notifications`. For example:

- "The decision: Notification sent 5 min ago";
- "Whether attempt 1 happened: Notification retrying — the provider was
  unavailable";
- "Notification could not be delivered — the address or number was refused.
  This request still waits here.";
- "Notification sent — possibly more than once";
- with none, "None sent. This page and the attention list always show what
  needs you."

It is **informational only**:

- It never enables, disables, or changes a decision. The attention item is
  the authority (tested).
- It names no recipient or address, which the API never provides.
- The dashboard sends no notifications.

A notification's link opens the approval. Without a session it shows
sign-in, and after signing in the same path is shown (tested in Chrome,
[notifications.md](notifications.md)).

## 9. Reconciliation UX

For an approved action whose outcome is unknown (the attention item
**Reconciliation required**):

- The panel opens with "**Agent Factory attempted this action but cannot
  prove whether it completed.** It will not guess: the run waits until
  someone who checked says what happened.", the attempt number, and when it
  was sent.
- Three answers, **none preselected**:
  - **It happened**: recorded as done; never sent again.
  - **It didn't happen — don't retry**: the run continues without it.
  - **It didn't happen — authorize one retry**: explained as a **new
    authorization**: Agent Factory may perform the action one more time,
    and the retry must begin within 15 minutes of the answer or it lapses.
    Confirming it needs an explicit checkbox ("I checked that the action did
    not happen, and I authorize one new attempt for 15 minutes.") and a
    button labelled **Authorize one retry**.
- **Review answer…** then a confirmation, with the same 400 ms guard,
  in-flight guard, and unknown-outcome handling as decisions. The answer is
  bound to the execution, the attempt, and the fingerprint the API supplied.

## 10. Execution timeline

"What happened to the action" shows the execution state (authorized, being
sent, succeeded, did not take effect, outcome unknown, never sent), the
attempt count, an open retry window, and the recorded answer (and whether a
worker has applied it). The timeline lists each step in causal order, from
the API's structured fields only: requested; approved or rejected (by whom);
sent or retried (by which worker and its generation, the claim's fencing
token); succeeded, failed, not sent, or outcome unknown; a person must
confirm; the answer; a retry authorized (and until when); resolved.

## 11. System status and errors

The header shows one status, from `GET /readyz` (no credential) and the
signed-in attention poll:

| Status | When |
| --- | --- |
| Agent Factory is running | Ready |
| API unavailable | No answer: the API or the frontend server is down, or the network |
| Database unavailable | `/readyz` says the database is unavailable |
| API not ready | `/readyz` says not ready for another reason (e.g. the schema is not current) |
| Sign-in checks unavailable | Ready, but signed-in requests fail with `authentication_unavailable` (the API cannot verify tokens) |

A refused or expired token is not a status: the dashboard returns to the
sign-in screen and says "Your session is no longer valid."

| Answer | Message |
| --- | --- |
| 401 | Your session is no longer valid. Sign in again with a new development token. |
| 403 | You don't have permission to perform this action. It requires the permission *x*. |
| 409 | A specific explanation per conflict code, e.g. "This approval was already decided. Refreshing to show the decision on record." |
| 429 | Too many attempts. Try again shortly. (and the wait, when the API gives one) |
| 503 | Agent Factory is temporarily unavailable. |
| No answer | Agent Factory is unreachable right now. / did not answer in time. |

The API's `request_id` is kept under "Support details". No stack trace,
path, or internal detail is ever shown (the API sends none).

## 12. Security

- **Untrusted text.** Every API string is rendered as text by React. The
  source has no `dangerouslySetInnerHTML`, `innerHTML`, `eval`, or
  `new Function` (a test enforces it). HTML-like payloads in project names,
  reasons, and milestones display as literal text with no element created and
  no script run (component- and browser-tested).
- **Content Security Policy** on every response of the preview server:
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'`,
  plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, `Cross-Origin-Opener-Policy: same-origin`, a
  restrictive `Permissions-Policy`, and `Cache-Control: no-store`. The dev
  server relaxes only `style-src` (Vite injects styles) and allows its own
  reload WebSocket.
- **No inline script, no CDN.** `index.html` loads one same-origin module.
  The browser tests route all non-loopback traffic to a dead proxy and check
  that every request stayed on the dashboard's origin.
- **No secrets in the bundle.** The built files are searched for the test
  database URL and password, the development token, the signing key, PEM
  headers, AWS key patterns, `AGENT_FACTORY_` names, and the names of the
  local secret files. The source never reads `import.meta.env`.
- **CORS stays closed.** The API has no CORS configuration; the browser
  reaches it through the same-origin proxy only.

## 13. Accessibility

- axe-core (WCAG 2.0 and 2.1, A and AA) reports **no violations** on the
  sign-in screen, the dashboard, the inbox, a project, an approval, a
  reconciliation, and the retry confirmation, at 375 × 812 and 1280 × 800
  (`test_every_screen_passes_automated_accessibility_checks`). axe is loaded
  from `node_modules` through the DevTools protocol, so the page's CSP stays
  strict during the check.
- A decision can be made with the keyboard alone, with a visible focus
  outline; focus moves to each confirmation step. A skip link leads to the
  main content. Statuses are announced through `role="status"` regions;
  errors use `role="alert"`.
- Automated checks do not prove full accessibility: there has been no
  screen-reader or manual audit.

## 14. Tests

| Layer | Where | Count | Proves | Real API? |
| --- | --- | --- | --- | --- |
| Unit | `frontend/src/**/*.test.ts` | part of 56 | Formatting, labels, routes, the submission state machine, error classification, polling timing | No |
| Component | `frontend/src/screens/screens.test.tsx` | part of 56 | Screens over a scripted fake API: deliberate decisions, no optimism, conflicts, lost answers, stale data, permission, reconciliation rules, XSS, sign-in storage, notification status | No (scripted) |
| Contract | `tests/test_frontend_contract.py` | 7 | Types generated from the current `openapi.yaml`; exact dependency pins from registry.npmjs.org; source policy (no HTML from data, no `fetch`/`Authorization` outside the client, no `localStorage`, no external URLs, strict CSP, loopback proxy) | No |
| Browser | `tests/test_dashboard_browser.py` | 16 | Real Chrome against the real API and PostgreSQL: sign-in rules, expiry and disabling, layout at both sizes, axe, keyboard, XSS, the served bundle and headers, loopback-only proxy, a missing permission | Yes |
| End to end | `tests/test_dashboard_e2e.py` | 7 | Approve and reject consumed by the real `worker run`; all three reconciliation answers consumed by a real worker loop; two browsers and storage deletion; the inbox | Yes, with workers |
| Reliability | `tests/test_dashboard_reliability.py` | 14 | The eight failure scenarios ([dashboard-reliability.md](dashboard-reliability.md)) | Yes, with workers |
| Notifications | `tests/test_dashboard_notifications.py` | 7 | The overnight story with no browser open, the link through sign-in, frontend/API/worker/dispatcher/PostgreSQL outages ([notifications.md](notifications.md)) | Yes, with workers and a real dispatcher |

**Manual mutation check.** Ten deliberate breaks of the decision,
reconciliation, polling, and sign-in logic were each made, run against
Vitest, and restored:

- a different decision on record treated as "not recorded";
- a closed approval treated as "not recorded";
- an answer for another attempt treated as "not recorded";
- controls enabled while an outcome is still unknown;
- stale data not disabling decisions;
- an unknown outcome settled from data loaded before the failure;
- the fingerprint altered;
- the token kept in `localStorage`;
- signing out leaving the token stored;
- no polling backoff.

All ten are caught. The fourth at first went unnoticed, because nothing
checked the controls in the window between a lost answer and the reload
that settles it. The lost-answer component test now asserts that they stay
disabled.

**The component tests are not reliability evidence**: they run the UI
against a scripted API. Reliability claims rest only on the browser, E2E,
and reliability suites, which use no mocked API.

```bat
set AGENT_FACTORY_TEST_DATABASE_URL_FILE=.local\test-database.url
.venv\Scripts\python -m pytest -m browser --run-browser --run-postgres tests/test_dashboard_browser.py tests/test_dashboard_e2e.py tests/test_dashboard_reliability.py
rem the PostgreSQL-outage scenario also needs Docker (it stops and starts this project's container):
.venv\Scripts\python -m pytest -m browser --run-browser --run-postgres --run-docker-live tests/test_dashboard_reliability.py
```

The browser suites build the bundle into a temporary directory, start the
API (development authentication, a fresh test schema), the frontend server,
and workers as real processes, and drive the installed Google Chrome through
Playwright. No browser is downloaded (`playwright install` is never run).
Requested and unavailable (no Chrome, no Node, no `node_modules`) they fail;
they never skip.

## 15. Stack decision (recorded before implementation)

### What the stack had to support

- A responsive web UI, designed for a 375 px phone first, then desktop.
- Calls to the existing authenticated API (bearer tokens from the
  development issuer), through one client module, typed from `openapi.yaml`.
- Bounded polling, stale-data warnings, and in-flight decision states.
- Real browser tests, including failure injection and process restarts
  orchestrated around the browser.
- A later AWS deployment (a static bundle behind a CDN is the simple path).
- Maintenance by one developer, and the course rubric's frontend criterion:
  "functional, well structured, with centralized backend communication, and
  tests covering core logic, with clear instructions for running them".

### Options evaluated

| | React + TypeScript + Vite | Server-rendered HTML (FastAPI + Jinja2, maybe htmx) | Vanilla TypeScript, no framework | Preact + TypeScript + Vite |
| --- | --- | --- | --- | --- |
| Installs for the app itself | npm: react, react-dom; build and test tools | none (Jinja2 is present); htmx would have to be vendored | npm: TypeScript and test tools | npm: preact; build and test tools |
| Authentication | The browser sends the bearer token: the real verification path, unchanged | The page server must hold the user's credential (a cookie session: a new session and CSRF surface), or the browser sends tokens anyway and the pages still need JavaScript | Same as React | Same as React |
| Live state, stale warnings, in-flight decisions | Natural: component state, re-render on poll | Needs hand-written client JavaScript anyway | Hand-written DOM updates and escaping (XSS-prone) | Natural |
| Types from `openapi.yaml` | `openapi-typescript` generates them | Python side only | Same as React | Same as React |
| Unit and component tests | Vitest + Testing Library | Template tests; client JavaScript untested | Harder to test rendering | Vitest + Testing Library |
| Separation | A separate, disposable frontend process; the API is unchanged | UI inside the API process, or a second Python web app | Separate | Separate |
| AWS later | Static files | A server to run | Static files | Static files |

Server-rendered HTML was the genuine competitor (no new package for the pages
themselves). It lost because it cannot exercise the bearer-token path without
client JavaScript or a new cookie-session layer, its stale-data and in-flight
states would need untyped, untested client code anyway, and it would put the
UI into the API process. Browser tests need an install in every option.

### Decision: React + TypeScript + Vite, kept small

- Runtime dependencies `react` and `react-dom` only.
- **No React plugin.** Vite 8 compiles JSX with its built-in Oxc transformer
  (`oxc.jsx.runtime: "automatic"` in `vite.config.js`). No Babel toolchain and
  no inline fast-refresh preamble, which would fight the CSP; edits reload the
  page instead of hot-swapping components.
- Same-origin proxy to the loopback API (section 2).
- Browser tests in Python (Playwright for Python under pytest), because the
  E2E and reliability tests orchestrate the API, workers, the frontend server,
  and the guarded PostgreSQL container, which pytest fixtures already do.
  Playwright drives the installed Chrome (`channel="chrome"`).
- axe-core from the frontend's own `node_modules`.

### Installed (exact versions; the owner authorized these installs)

npm, in `frontend/` from registry.npmjs.org, pinned exactly in
`package.json`, with `package-lock.json` committed (145 packages, every one
from registry.npmjs.org with a sha512 integrity hash):

| Package | Version | Kind |
| --- | --- | --- |
| react, react-dom | 19.3.0 | runtime |
| vite | 8.3.2 | dev |
| typescript | 5.9.3 | dev (openapi-typescript 7 needs TypeScript 5) |
| @types/react, @types/react-dom | 19.3.0 | dev |
| openapi-typescript | 7.13.0 | dev |
| vitest | 5.0.3 | dev |
| jsdom | 30.1.2 | dev |
| @testing-library/react | 16.3.3 | dev |
| @testing-library/dom | 10.4.2 | dev |
| axe-core | 4.13.0 | dev |

pip, into `.venv` from pypi.org, pinned in `requirements-browser.txt`:
`playwright==1.63.0`, `greenlet==3.5.6`, `pyee==13.0.1`.

Network activity was limited to those installs and one npm metadata query
(`npm view typescript@5 version`), used to find the TypeScript 5 release
after the first install resolved TypeScript 7, which openapi-typescript does
not support. No browser was downloaded, and nothing is fetched at runtime.

## 16. Known limits

- Development authentication only; tokens are short-lived files minted on the
  machine. Production phone sign-in needs an identity provider (later).
- Polling, not push: changes appear within one polling interval (about 5 s
  on active views).
- No real notification provider yet: the notification outbox and dispatcher exist ([notifications.md](notifications.md)), but only the development fake provider delivers anything, so in practice a person still opens the dashboard to see what needs them.
- English only; times in the browser's locale and time zone.
- Not tested on physical phones or in browsers other than Chrome; mobile is
  Chrome's device emulation at 375 × 812 (touch enabled).
- No screen-reader or manual accessibility audit.
