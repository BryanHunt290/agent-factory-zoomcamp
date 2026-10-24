-- 0007_notifications: attention notifications, through a transactional outbox (docs/notifications.md).
--
-- A notification ALERTS; it never AUTHORIZES. It says "Agent Factory needs your attention" and links
-- to the dashboard, where the person signs in and decides. No column here can approve anything.
--
-- * notification_destinations: where a principal wants to be told (a channel and an address). Owned
--   by the principal, managed by the local operator (CLI), never chosen by an API client. An address
--   is never copied into a notification row: a notification names its destination.
-- * notifications: the outbox. One row per (attention event, destination) - the dedup_key names
--   the event ("decision_required:<project>:<approval>" or
--   "reconciliation_required:<project>:<execution>:<attempt>") - written in the same transaction
--   that makes the attention event durable, so a crash can never keep one without the other. A
--   dispatcher process claims rows (FOR UPDATE SKIP LOCKED) under a short lease, calls a provider
--   OUTSIDE any transaction, and records the result fenced by claim_token.
-- * notification_attempts: one row per hand-over to a provider, written BEFORE the provider is
--   called (write-ahead). An attempt still open when its dispatcher's lease expired is closed as
--   'unknown': the provider may have accepted it. Delivery is at least once; never exactly once.
--
-- Content is deliberately minimal (title, body: the project's name and the reason) and a link_path
-- inside the dashboard: never a host, query string, token, tool parameter, or credential.

CREATE TABLE notification_destinations (
    id           text COLLATE "C" NOT NULL,
    principal_id text COLLATE "C" NOT NULL,
    channel      text             NOT NULL,
    address      text             NOT NULL,
    status       text             NOT NULL,
    policy       text             NOT NULL,
    created_at   timestamptz      NOT NULL,
    updated_at   timestamptz      NOT NULL,
    CONSTRAINT notification_destinations_pkey PRIMARY KEY (id),
    CONSTRAINT notification_destinations_principal_fkey FOREIGN KEY (principal_id) REFERENCES principals (id),
    -- One row per address per channel per principal.
    CONSTRAINT notification_destinations_address_key UNIQUE (principal_id, channel, address),
    CONSTRAINT notification_destinations_id_format CHECK (id ~ '^nds-[0-9a-f]{20}$'),
    CONSTRAINT notification_destinations_channel_valid CHECK (channel IN ('email', 'sms', 'push')),
    CONSTRAINT notification_destinations_address_length CHECK (length(address) BETWEEN 3 AND 320),
    CONSTRAINT notification_destinations_status_valid CHECK (status IN ('active', 'disabled')),
    -- v1 sends urgent attention immediately, day or night. Quiet hours and digests are designed, not built.
    CONSTRAINT notification_destinations_policy_valid CHECK (policy IN ('immediate')),
    CONSTRAINT notification_destinations_time_order CHECK (updated_at >= created_at)
);
COMMENT ON TABLE notification_destinations IS 'Where a principal is told that something needs attention. Addresses are personal data: never logged, never in API responses.';

CREATE TABLE notifications (
    id                  text COLLATE "C" NOT NULL,
    dedup_key           text COLLATE "C" NOT NULL,
    schema_version      smallint         NOT NULL,
    project_id          text COLLATE "C" NOT NULL,
    run_id              text COLLATE "C" NOT NULL,
    approval_id         text COLLATE "C" NOT NULL,
    attention_kind      text             NOT NULL,
    execution_id        text COLLATE "C",
    attention_attempt   integer,
    destination_id      text COLLATE "C" NOT NULL,
    principal_id        text COLLATE "C" NOT NULL,
    channel             text             NOT NULL,
    severity            text             NOT NULL,
    title               text             NOT NULL,
    body                text             NOT NULL,
    link_path           text COLLATE "C" NOT NULL,
    status              text             NOT NULL,
    attempts            integer          NOT NULL,
    created_at          timestamptz      NOT NULL,
    available_at        timestamptz      NOT NULL,
    next_attempt_at     timestamptz,
    last_attempt_at     timestamptz,
    sent_at             timestamptz,
    provider_message_id text,
    failure_category    text,
    claimed_by          text COLLATE "C",
    claim_token         bigint           NOT NULL,
    lease_expires_at    timestamptz,
    updated_at          timestamptz      NOT NULL,
    CONSTRAINT notifications_pkey PRIMARY KEY (id),
    -- Deduplication: one notification per attention event per destination, however often the event is recorded.
    CONSTRAINT notifications_dedup_key UNIQUE (dedup_key, destination_id),
    -- About a real approval of this run; a reconciliation also names a real attempt of its execution.
    CONSTRAINT notifications_approval_fkey FOREIGN KEY (project_id, run_id, approval_id)
        REFERENCES approval_requests (project_id, run_id, id),
    CONSTRAINT notifications_attempt_fkey FOREIGN KEY (execution_id, attention_attempt)
        REFERENCES action_execution_attempts (execution_id, attempt),
    CONSTRAINT notifications_destination_fkey FOREIGN KEY (destination_id) REFERENCES notification_destinations (id),
    CONSTRAINT notifications_principal_fkey FOREIGN KEY (principal_id) REFERENCES principals (id),
    CONSTRAINT notifications_id_format CHECK (id ~ '^ntf-[0-9a-f]{20}$'),
    CONSTRAINT notifications_dedup_key_format CHECK (
        dedup_key ~ '^(decision_required|reconciliation_required):[A-Za-z0-9:_.-]{1,200}$'
    ),
    CONSTRAINT notifications_schema_version_valid CHECK (schema_version = 1),
    CONSTRAINT notifications_attention_kind_valid CHECK (attention_kind IN ('decision_required', 'reconciliation_required')),
    CONSTRAINT notifications_reconciliation_names_attempt CHECK (
        (attention_kind = 'reconciliation_required') = (execution_id IS NOT NULL AND attention_attempt IS NOT NULL)
        AND (execution_id IS NULL) = (attention_attempt IS NULL)
    ),
    CONSTRAINT notifications_channel_valid CHECK (channel IN ('email', 'sms', 'push')),
    CONSTRAINT notifications_severity_valid CHECK (severity IN ('urgent')),
    CONSTRAINT notifications_title_length CHECK (length(title) BETWEEN 1 AND 80),
    CONSTRAINT notifications_body_length CHECK (length(body) BETWEEN 1 AND 500),
    -- A path inside the dashboard: no host, no query string, no fragment, nothing that could authorize.
    CONSTRAINT notifications_link_path_format CHECK (
        link_path ~ '^/projects/prj-[0-9a-f]{20}/approvals/apr-[0-9a-f]{16}$'
    ),
    CONSTRAINT notifications_status_valid CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'skipped')),
    CONSTRAINT notifications_attempts_range CHECK (attempts >= 0),
    CONSTRAINT notifications_claim_token_range CHECK (claim_token >= 0),
    CONSTRAINT notifications_failure_category_valid CHECK (failure_category IS NULL OR failure_category IN (
        'timeout', 'provider_unavailable', 'rate_limited', 'outcome_unknown',
        'invalid_destination', 'provider_rejected_configuration', 'unsupported_destination', 'max_attempts',
        'attention_resolved', 'destination_disabled', 'recipient_not_permitted'
    )),
    CONSTRAINT notifications_provider_message_id_length CHECK (provider_message_id IS NULL OR length(provider_message_id) <= 200),
    -- Exactly a sending notification has a dispatcher and a lease, and a lease is never longer than five minutes.
    CONSTRAINT notifications_sending_has_owner CHECK (
        (status = 'sending') = (claimed_by IS NOT NULL) AND (claimed_by IS NULL) = (lease_expires_at IS NULL)
    ),
    CONSTRAINT notifications_claimed_by_format CHECK (claimed_by IS NULL OR claimed_by ~ '^ndp-[0-9a-f]{20}$'),
    CONSTRAINT notifications_lease_window CHECK (
        lease_expires_at IS NULL OR lease_expires_at <= updated_at + interval '5 minutes'
    ),
    CONSTRAINT notifications_sent_has_time CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
    CONSTRAINT notifications_unfinished_is_due CHECK ((status IN ('pending', 'sending')) = (next_attempt_at IS NOT NULL)),
    CONSTRAINT notifications_final_says_why CHECK (status NOT IN ('failed', 'skipped') OR failure_category IS NOT NULL),
    CONSTRAINT notifications_skip_reason CHECK (
        status <> 'skipped' OR failure_category IN ('attention_resolved', 'destination_disabled', 'recipient_not_permitted')
    ),
    CONSTRAINT notifications_time_order CHECK (updated_at >= created_at AND available_at >= created_at)
);
COMMENT ON TABLE notifications IS 'The notification outbox: one durable intent per attention event and destination. Alerts only; authorizes nothing.';

-- The dispatcher's claim query: due pending rows, oldest due first. Only unfinished rows are indexed.
CREATE INDEX notifications_due_idx ON notifications (next_attempt_at, id) WHERE status = 'pending';
-- Recovery: rows whose dispatcher's lease may have expired.
CREATE INDEX notifications_sending_idx ON notifications (lease_expires_at, id) WHERE status = 'sending';
-- The approval view's notification status.
CREATE INDEX notifications_approval_idx ON notifications (project_id, approval_id, created_at);
-- Storm protection: recent attempts per destination.
CREATE INDEX notifications_destination_idx ON notifications (destination_id);

CREATE TABLE notification_attempts (
    notification_id     text COLLATE "C" NOT NULL,
    attempt             integer          NOT NULL,
    dispatcher_id       text COLLATE "C" NOT NULL,
    claim_token         bigint           NOT NULL,
    started_at          timestamptz      NOT NULL,
    ended_at            timestamptz,
    outcome             text,
    failure_category    text,
    provider_message_id text,
    CONSTRAINT notification_attempts_pkey PRIMARY KEY (notification_id, attempt),
    CONSTRAINT notification_attempts_notification_fkey FOREIGN KEY (notification_id) REFERENCES notifications (id),
    CONSTRAINT notification_attempts_attempt_range CHECK (attempt >= 1),
    CONSTRAINT notification_attempts_dispatcher_format CHECK (dispatcher_id ~ '^ndp-[0-9a-f]{20}$'),
    CONSTRAINT notification_attempts_claim_token_range CHECK (claim_token >= 1),
    -- An attempt ends exactly when its outcome is recorded, and 'unknown' is an honest outcome.
    CONSTRAINT notification_attempts_ended_iff_outcome CHECK ((ended_at IS NULL) = (outcome IS NULL)),
    CONSTRAINT notification_attempts_outcome_valid CHECK (
        outcome IS NULL OR outcome IN ('accepted', 'transient_failure', 'permanent_failure', 'unknown')
    ),
    CONSTRAINT notification_attempts_failure_category_valid CHECK (failure_category IS NULL OR failure_category IN (
        'timeout', 'provider_unavailable', 'rate_limited', 'outcome_unknown',
        'invalid_destination', 'provider_rejected_configuration', 'unsupported_destination'
    )),
    CONSTRAINT notification_attempts_accepted_without_failure CHECK (
        (outcome = 'accepted') IS NOT TRUE OR failure_category IS NULL
    ),
    CONSTRAINT notification_attempts_time_order CHECK (ended_at IS NULL OR ended_at >= started_at)
);
COMMENT ON TABLE notification_attempts IS 'Each hand-over of a notification to a provider, recorded before the call; unknown outcomes stay unknown.';

-- At most one open attempt per notification.
CREATE UNIQUE INDEX notification_attempts_one_open ON notification_attempts (notification_id) WHERE ended_at IS NULL;
-- Storm protection: attempts in the last window.
CREATE INDEX notification_attempts_started_idx ON notification_attempts (started_at);
