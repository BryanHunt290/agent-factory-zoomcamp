# Dashboard reliability: failure and recovery evidence

What happens to a person using the dashboard ([frontend.md](frontend.md))
when something underneath them fails, and what happens to their work.

Every result on this page comes from `tests/test_dashboard_reliability.py`
and `tests/test_dashboard_e2e.py`. They use the installed Google Chrome, the
built dashboard served by `vite preview`, the real FastAPI process,
PostgreSQL, and real worker processes. **Nothing in them mocks the API.**
A failure is injected by killing a real process, stopping this project's
PostgreSQL container, quitting Chrome, or shaping one request on the
browser's network. A shaped request either reaches the real API or reaches
nothing; no API answer is ever made up.

The Vitest component tests (`frontend/src/**/*.test.tsx`) run the UI against
a scripted API. They prove UI behaviour, **not** reliability, and nothing on
this page relies on them.

## What every scenario checks

Each scenario records the following, and asserts all of it (with
`DASHBOARD_EVIDENCE_DIR` set it also writes one JSON file per scenario):

- the initial state;
- the failure injected;
- what the person saw (banner, system status, whether decisions were enabled);
- what kept running;
- the recovery;
- the final authoritative state, read from PostgreSQL rather than from the browser;
- whether anything was lost;
- whether any action ran twice.

At the end, every browser is checked (`assert_clean`): it made requests only
to the dashboard's own loopback origin, no token appeared in any URL or
console message, and there was no dialog and no page error. What the browser
stores is checked by the end-to-end suite (below).

"Calculator run" is a real `worker run` process advancing the calculator
workflow until it asks to delete `legacy_calc.py`. "Notice run" is a real
worker loop whose approved action appends one line to a notice oracle file,
so a duplicated effect would show as a second line.

## Results

| # | Scenario | Initial state | Failure injected | What the person saw | What kept running | Recovery | Final authoritative state | Lost | Duplicated |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | Frontend server restart (`test_frontend_server_restart`) | Calculator run awaiting approval; the approval open on a phone | `vite preview` (bundle and `/api` proxy) killed like a crash | "Connection lost — showing data from 12:34:25 PM … Decisions are disabled until it reconnects."; status "API unavailable"; last data kept on screen | API (ready), PostgreSQL, worker; the run kept waiting | Server restarted on the same port; the open page reconnected without a reload; after a reload the tab was still signed in; the person approved | Run completed; approved by Dashboard Owner; one execution, succeeded on attempt 1; file deleted | No | No (1 POST) |
| 2 | Browser quits and reopens (`test_browser_quits_and_reopens`) | Calculator run awaiting approval; the confirmation step open | Chrome quit completely, first with the confirmation open (nothing sent), then right after a decision was recorded | After reopening: the sign-in screen (the token lived only in the closed tab); after signing in: "Approved", "Action succeeded (attempt 1)" | API, PostgreSQL, and the worker, which finished the run with no browser open | Reopened Chrome and signed in again; everything shown came from the API | Run completed; approved once; one execution, succeeded on attempt 1 | No | No (1 POST) |
| 3 | API restart (`test_api_restart`) | Calculator run awaiting approval, open on a phone | API process killed like a crash | "Connection lost — …"; status "API unavailable"; decisions disabled | PostgreSQL and the worker; the run kept waiting | API restarted on the same port; the page reconnected by itself; the person approved | Run completed; approved once; succeeded on attempt 1 | No | No |
| 4 | PostgreSQL outage (`test_database_outage`, needs `--run-docker-live`) | Calculator run awaiting approval, open on a phone | `docker stop agent-factory-postgres-dev` (the guarded container serving the test database) | "Agent Factory is temporarily unavailable — showing data from …"; status "Database unavailable"; decisions disabled; `/readyz` 503 `database_unavailable` | The API (answering 503, not ready) and the worker (retrying with backoff; 3 retries logged) | Container started again; the API, worker, and page reconnected by themselves; the person approved | Run completed; approved once; succeeded on attempt 1 (read through a fresh connection) | No | No |
| 5 | Worker killed after sending; another takes over (`test_worker_killed_after_sending_and_another_takes_over`) | A notice approved in the dashboard; worker 1 sent it and had not recorded the outcome | Worker 1 killed like a crash between the effect and recording it | "Action sent (attempt 1) / Being sent", then after takeover "Reconciliation required — Agent Factory attempted this action but cannot prove whether it completed." | API, PostgreSQL, dashboard; worker 2 took over after the 3 s lease expired (fencing generation 3) | The person answered "It happened"; worker 2 finished the run | Run completed; one execution, succeeded on attempt 1; **1 notice sent** | No | No: worker 2 did not resend an action whose outcome was unknown |
| 6a | Decision's connection refused (`test_network_refuses_the_decision`) | Notice run awaiting approval | The decision POST aborted with connection refused (never reached the API) | "Your approval did not reach Agent Factory. Nothing was recorded. You can send it again." | Everything | The person sent the identical decision again | Run completed; approved once; 1 notice | No | No (2 identical POSTs, 1 decision) |
| 6b | Decision times out (`test_the_decision_times_out_before_reaching_the_api`) | Notice run awaiting approval | The decision held on the network until the dashboard's 10 s timeout | "Sending your approval…", then "did not reach Agent Factory. Nothing was recorded. You can send it again." (settled from a fresh API read) | Everything | Sent the same decision again | Run completed; approved once; 1 notice | No | No |
| 6c | Slow network, impatient taps (`test_a_slow_network_delays_the_decision_and_more_taps_send_nothing`) | Notice run awaiting approval | The decision delayed on the network; the person tapped and double-tapped the confirm button meanwhile | "Sending your approval…"; the confirm button disabled | Everything | The delayed request arrived | Run completed; approved once; 1 notice | No | No (exactly 1 POST) |
| 6d | API recorded the decision but its answer was lost (`test_the_api_records_the_decision_but_its_answer_is_lost`) | Notice run awaiting approval | The real API answered 201; the connection was reset before the browser got it | "Your approval reached Agent Factory and is on record." No resend offered | Everything | None needed: the dashboard asked the API | Run completed; approved once; 1 notice | No | No (1 POST) |
| 6e | Decision delivered twice (`test_the_network_delivers_the_decision_twice`) | Notice run awaiting approval | The same POST delivered to the API twice (201, then 200) | "Agent Factory recorded your approval. It was already on record." | Everything | None needed | Run completed; one decision; 1 notice | No | No |
| 6f | "Authorize one retry" delivered twice, answer lost (`test_a_retry_answer_delivered_twice_with_its_answer_lost_authorizes_one_retry`) | An approved notice whose first attempt has an unknown outcome | The reconciliation answer delivered twice (201, 200), then the connection reset | "Your answer reached Agent Factory and is on record." | Everything | None needed | Run completed; execution succeeded on attempt 2; **2 notices** (the first attempt and exactly one authorized retry); 1 answer applied | No | No: one retry, as authorized |
| 7 | Decision sent as the API goes down (`test_a_decision_sent_as_the_api_goes_down`) | Calculator run; the confirmation open, the page just refreshed | API killed after the last refresh, just before the person confirmed | "We couldn't confirm whether your approval reached Agent Factory. Checking …; nothing will be sent again without you."; then the stale banner; **no resend offered** while unknown | PostgreSQL and the worker; the run kept waiting | API restarted; the dashboard read the approval, found nothing on record, and only then offered the same decision again | Run completed; approved once; succeeded on attempt 1 | No | No (2 identical POSTs, 1 decision) |
| 8a | Decision already made in another browser (`test_a_decision_already_made_elsewhere_is_refused_and_shown`) | One approval open on a phone (owner) and a desktop (second reviewer) | The reviewer's view was held stale; the owner approved; then the reviewer confirmed a rejection | "This approval was already decided. Refreshing to show the decision on record. Nothing was recorded by this attempt." | Everything | The reviewer's page refreshed to "Approved by Dashboard Owner" | Run completed; approved by the owner; 1 notice; the rejection was refused (409) | No | No |
| 8b | Approval expires while open (`test_an_approval_that_expires_while_open_cannot_be_approved`) | An approval open on a phone, 30 s before expiry | The page's view held stale while the approval expired; the person then confirmed | "This approval expired before your decision reached Agent Factory. Refreshing. Nothing was recorded by this attempt." | Everything | The page refreshed to "Expired before anyone decided" | Approval expired; no decision | No | No |

Times in the "saw" column are from one run; the other wording is asserted.

### Also shown by the end-to-end suite

`test_every_browser_shows_the_servers_state_and_the_browser_holds_none`:

- An approval is open in two browsers (a phone as the owner, a desktop as a
  second reviewer). The owner approves; the desktop follows by polling,
  shows "Approved by Dashboard Owner", and no longer offers a decision.
- Each browser holds exactly its token in `sessionStorage`, and nothing in
  `localStorage`, IndexedDB, caches, or cookies.
- Clearing the phone's `sessionStorage` and reloading only signs that tab
  out. The approval on the server is unchanged. Signing in again shows the
  same server-held decision and comment.

The other E2E tests show an approval and a rejection made in the dashboard
consumed by the real `worker run`, and each of the three reconciliation
answers consumed by a real worker loop.

## Repeated runs

The reliability suite was run three times in a row, with `--run-docker-live`.
There is no automatic retry: no rerun plugin is installed, and a scenario
that fails once counts as failed.

| Run | Command | Result |
| --- | --- | --- |
| 1 | browser, E2E, and reliability suites together | 37 passed (14 reliability), 7 min 3 s |
| 2 | reliability suite | 14 passed, 4 min 2 s |
| 3 | reliability suite | 14 passed, 4 min 3 s |

## Notifications

`tests/test_dashboard_notifications.py` extends the same rules to attention
notifications ([notifications.md](notifications.md)):

- **The real stack:** the API, the frontend server, PostgreSQL, real
  workers, a real `notifications dispatch` process, and Chrome.
- **The provider is the fake.** It contacts nothing and appends what it would
  have delivered to a record file: the person's "phone".
- **Every message the phone received is checked:** the link must be the
  dashboard's origin plus an approval path, and the text must contain no
  token, tool, parameter, path, or address.

| # | Scenario | Initial state | Failure injected | What the person saw | What kept running | Recovery | Final authoritative state | Lost | Duplicated |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| N1 | The overnight story (`test_overnight_two_attention_events_two_notifications_and_the_morning_view`) | No browser open; workers, the API, PostgreSQL, and the dispatcher running | None: the browser stayed closed | On the phone: "Approval required" (calculator), "Approval required" (notice), "Action outcome needs verification" (the notice's unknown outcome). In the morning: both items in the inbox, and each approval's notification history ("The decision: Notification sent", "Whether attempt 1 happened: Notification sent") | Everything except a browser | None needed | 3 events, 3 notifications, each sent once; another run's progress added none | No | No |
| N2 | The link (`test_the_link_leads_through_sign_in_to_the_exact_approval`) | A notification delivered | None | The link without a session: the sign-in screen, no decision controls; after signing in: that exact approval | Everything | None needed | Approved in the dashboard; run completed | No | No |
| N3 | Frontend down (`test_frontend_down_notifications_still_go_out`) | The frontend server stopped | Frontend server killed | The notification arrived | API, PostgreSQL, worker, dispatcher | None needed | Notification sent | No | No |
| N4 | API down (`test_api_down_already_durable_notifications_still_go_out`) | An approval and its notification intent stored, no dispatcher yet | API killed, then the dispatcher started | The notification arrived | PostgreSQL, worker, dispatcher | None needed | Notification sent | No | No |
| N5 | Worker down (`test_worker_down_after_the_attention_event_the_notification_still_goes_out`) | A worker paused a run for approval | Worker killed | The notification arrived | API, PostgreSQL, dispatcher | None needed for the notification | Notification sent; run still awaiting approval | No | No |
| N6 | Dispatcher down (`test_dispatcher_down_the_outbox_waits_and_the_dashboard_shows_it`) | An approval waiting, no dispatcher | The provider accepted, then the dispatcher was killed before recording it | "The decision: Notification queued", then "Notification sent — possibly more than once" | API, PostgreSQL, worker | A second dispatcher recovered the expired lease, recorded the first attempt as unknown, and sent again | Sent; `outcome_unknown: true` | No | **The notification was delivered twice, as designed** (at least once), and the dashboard says so. No approved action was affected. |
| N7 | PostgreSQL down (`test_postgresql_down_the_dispatcher_fails_closed_and_resumes`, needs `--run-docker-live`) | An approval whose first notification attempt failed transiently (retry in 10 s) | `docker stop agent-factory-postgres-dev` | Nothing while PostgreSQL was down; the notification after it returned | The dispatcher, retrying the database and sending nothing (`database_unavailable` events) | PostgreSQL started; the dispatcher reconnected and sent the retry | Sent; attempts `transient_failure`, `accepted` | No | No |

The crash boundaries inside a dispatcher (killed after claiming, after an
attempt began, after the provider accepted, after the result, after
scheduling a retry; two dispatchers racing) are tested with real processes in
`tests/test_postgres_notifications.py`
([notifications.md](notifications.md#delivery-semantics-and-crash-boundaries)).

### Repeated runs (notifications)

All with `--run-docker-live`, and no automatic retries.

| Run | Command | Result |
| --- | --- | --- |
| 1 | notification suite | 7 passed (77 s) |
| 2 | all browser suites together (browser, E2E, reliability, notifications) | 44 passed (8 min 38 s) |
| 3 | notification suite | 7 passed (73 s) |
| 4 | notification suite | **1 failed**, 6 passed: N1 timed out waiting for the notice run's approval |
| 5 | notification suite, after fixing the test | 7 passed (46 s) |
| 6 | notification suite | 7 passed (49 s) |

**Run 4, investigated rather than retried.** The workers' logs show the
cause, and it is not in notifications.

- The calculator's `worker run` and the notice fixture's worker share one
  run queue.
- The calculator worker claimed the notice run four times in a row. It
  cannot open that workspace, so each time it released the run with
  `workspace_unavailable` and a 30-second delay. The fixture worker never
  got it within the test's two minutes.
- Which worker wins each claim is random, hence one failure in four runs.

The test now stops the calculator's worker once its run waits for a person
(it has nothing left to do). The underlying limitation, that runs are not
routed to a worker able to run them, is pre-existing and recorded in
[workers.md](workers.md#limitations). No notification was lost or
duplicated in that run.
## What this does not show

- One host. The API, workers, PostgreSQL, and browser all run on one
  machine; no real network sits between them. Network faults are injected at
  the browser (Playwright request routing), not on a real link.
- Chrome only, headless; mobile is Chrome's 375 × 812 device emulation, not a
  physical phone.
- Development authentication only; production sign-in is not built.
- Polling, not push: the dashboard notices a change or a recovery within one
  polling interval (about 5 s, backing off to 15 s after failures).
- A notice is a line in a test oracle file, not a real external system.
  Whether a real external effect happened still needs a person to check it
  (reconciliation), as scenario 5 shows.
- Exactly-once is not claimed. An approved non-idempotent action whose outcome
  is unknown is never resent automatically; a person settles it.
