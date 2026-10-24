-- 0006_human_decisions: approval expiry, recorded reconciliation decisions, and the attention inbox.
--
-- E5 completes the human-in-the-loop workflow (docs/workers.md, docs/product-domain.md):
--
-- * Approvals expire. A pending request can be decided, and an approved action can begin, only
--   before expires_at. Pending requests stored before this migration, without an expiry, get the
--   default validity (12 hours after they were requested), so none stays decidable for ever; a
--   CHECK keeps every pending request expiring from now on.
-- * reconciliation_decisions: a person's answer about one uncertain attempt ("it happened", "it
--   did not happen", "it did not happen, try once more"), recorded by the API or the CLI as
--   intent. A worker that holds the run's claim applies it to the execution ledger
--   (action_reconciliations); recording it changes no execution. One decision per attempt.
-- * action_reconciliations gains principal_id: an applied reconciliation by an authenticated user
--   names its enrolled principal, as approval decisions do (0004).
-- * run_claims gains a wake-up counter: a decision that arrives while a worker holds the run is
--   never lost by that worker's release (docs/workers.md, "wake-ups").
-- * Two partial indexes serve the attention inbox ("what needs a person?").

UPDATE approval_requests
SET expires_at = requested_at + interval '12 hours'
WHERE status = 'pending' AND expires_at IS NULL;
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_pending_expires CHECK (
    status <> 'pending' OR expires_at IS NOT NULL
);

-- Target of the decision's run foreign key below (implied by the primary key on id).
ALTER TABLE action_executions ADD CONSTRAINT action_executions_run_member_key UNIQUE (project_id, run_id, id);

CREATE TABLE reconciliation_decisions (
    project_id         text COLLATE "C" NOT NULL,
    run_id             text COLLATE "C" NOT NULL,
    execution_id       text COLLATE "C" NOT NULL,
    attempt            integer          NOT NULL,
    action_fingerprint text COLLATE "C" NOT NULL,
    choice             text             NOT NULL,
    decided_at         timestamptz      NOT NULL,
    actor_kind         text             NOT NULL,
    actor_id           text             NOT NULL,
    actor_display_name text             NOT NULL,
    actor_verified     boolean          NOT NULL,
    principal_id       text COLLATE "C",
    comment            text             NOT NULL,
    -- One decision per uncertain attempt; its index also serves every lookup (by execution and attempt).
    CONSTRAINT reconciliation_decisions_pkey PRIMARY KEY (execution_id, attempt),
    -- About a real attempt, of an execution of this very run, for that execution's exact action.
    CONSTRAINT reconciliation_decisions_attempt_fkey FOREIGN KEY (execution_id, attempt)
        REFERENCES action_execution_attempts (execution_id, attempt),
    CONSTRAINT reconciliation_decisions_run_fkey FOREIGN KEY (project_id, run_id, execution_id)
        REFERENCES action_executions (project_id, run_id, id),
    CONSTRAINT reconciliation_decisions_binding_fkey FOREIGN KEY (execution_id, action_fingerprint)
        REFERENCES action_executions (id, action_fingerprint),
    CONSTRAINT reconciliation_decisions_principal_fkey FOREIGN KEY (principal_id) REFERENCES principals (id),
    CONSTRAINT reconciliation_decisions_choice_valid CHECK (
        choice IN ('it_happened', 'did_not_happen', 'did_not_happen_retry')
    ),
    -- Only a person can say what happened in the outside world; never "system".
    CONSTRAINT reconciliation_decisions_actor_kind_valid CHECK (actor_kind IN ('unauthenticated', 'user')),
    CONSTRAINT reconciliation_decisions_unauthenticated_unverified CHECK (
        NOT (actor_kind = 'unauthenticated' AND actor_verified)
    ),
    -- A user decision is an authenticated principal's: verified, and principal_id = actor_id.
    CONSTRAINT reconciliation_decisions_user_is_principal CHECK (
        (actor_kind = 'user') = (principal_id IS NOT NULL)
        AND (principal_id IS NULL OR (principal_id = actor_id AND actor_verified))
    ),
    CONSTRAINT reconciliation_decisions_comment_length CHECK (length(comment) <= 1000)
);
COMMENT ON TABLE reconciliation_decisions IS 'A person''s answer about one uncertain attempt, recorded as intent; a worker applies it under the run''s claim.';

ALTER TABLE action_reconciliations ADD COLUMN principal_id text COLLATE "C";
ALTER TABLE action_reconciliations ADD CONSTRAINT action_reconciliations_principal_fkey
    FOREIGN KEY (principal_id) REFERENCES principals (id);
ALTER TABLE action_reconciliations ADD CONSTRAINT action_reconciliations_user_is_principal CHECK (
    (actor_kind = 'user') = (principal_id IS NOT NULL)
    AND (principal_id IS NULL OR (principal_id = actor_id AND actor_verified))
);

-- Wake-ups (a decision recorded, a run enqueued) count up wake_seq; a claim copies it into
-- claimed_wake_seq. Both are written under the claim row's lock, so a release that finds
-- wake_seq > claimed_wake_seq knows a wake-up committed after its claim, whatever the commit
-- order of the transactions involved, and leaves the run available at once instead of applying
-- its own plan (which might be "not before the approval expires").
ALTER TABLE run_claims ADD COLUMN wake_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE run_claims ADD COLUMN claimed_wake_seq bigint NOT NULL DEFAULT 0;
ALTER TABLE run_claims ADD CONSTRAINT run_claims_wake_seq_valid CHECK (0 <= claimed_wake_seq AND claimed_wake_seq <= wake_seq);

-- The attention inbox, decision side: pending requests across all projects in inbox order
-- (requested_at, project_id, id). Also serves listPendingApprovals without a project filter.
CREATE INDEX approval_requests_attention_idx ON approval_requests (requested_at, project_id, id)
    WHERE status = 'pending';
-- The attention inbox, reconciliation side: uncertain executions in inbox order
-- (since = updated_at, project_id, approval). Uncertain executions are rare, so the index is tiny.
CREATE INDEX action_executions_uncertain_idx ON action_executions (updated_at, project_id, request_id)
    WHERE state = 'uncertain';
