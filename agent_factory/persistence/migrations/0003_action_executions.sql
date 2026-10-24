-- 0003_action_executions: durable records of attempting approved actions.
--
-- Approval is not execution (docs/design/action-execution.md). approval_decisions
-- records that a person authorized one exact action; these tables record what
-- Agent Factory knows about attempting it:
--
--   action_executions          one row per approved action that Agent Factory used, with
--                              its state (authorized, dispatching, succeeded, failed,
--                              uncertain, lapsed). Changed only by compare-and-set on
--                              version, so of two writers exactly one succeeds.
--   action_execution_attempts  one row per dispatch, inserted and COMMITTED before the side
--                              effect (write-ahead), closed with its result afterwards.
--   action_reconciliations     a person's determination of one uncertain attempt. For "try
--                              once more" it is also the new, 15-minute authorization.
--
-- The database enforces structure and binding: an execution belongs to a real run and to
-- the approval request of that run; its fingerprint is the APPROVED decision's (a decision
-- that rejected the action, or approved a different one, cannot have an execution); one
-- approved decision has at most one execution; at most one attempt per execution is open;
-- a reconciliation names the exact execution, attempt, and fingerprint. The domain model
-- enforces the state machine and re-validates every row when it is read.
--
-- The run status vocabulary gains awaiting_reconciliation: an approved side effect may
-- already have happened and a person must say whether it did. It is not "blocked", which
-- means an infrastructure or policy problem.

ALTER TABLE runs DROP CONSTRAINT runs_status_valid;
ALTER TABLE runs ADD CONSTRAINT runs_status_valid CHECK (
    status IN ('running', 'awaiting_approval', 'awaiting_reconciliation', 'blocked', 'completed', 'failed', 'exhausted')
);

-- Targets of the composite foreign keys below. Each is implied by an existing key, but a
-- foreign key needs a unique constraint on exactly its columns.
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_run_member_key UNIQUE (project_id, run_id, id);
ALTER TABLE approval_decisions ADD CONSTRAINT approval_decisions_outcome_key
    UNIQUE (project_id, request_id, action_fingerprint, outcome);

CREATE TABLE action_executions (
    id                 text COLLATE "C" NOT NULL,
    project_id         text COLLATE "C" NOT NULL,
    run_id             text COLLATE "C" NOT NULL,
    request_id         text COLLATE "C",
    decision_outcome   text,
    node               text             NOT NULL,
    visit              integer          NOT NULL,
    agent              text             NOT NULL,
    action_tool        text             NOT NULL,
    action_category    text             NOT NULL,
    action_params      jsonb            NOT NULL,
    action_fingerprint text COLLATE "C" NOT NULL,
    effect_class       text             NOT NULL,
    idempotency        text,
    max_attempts       integer          NOT NULL,
    key_retention_s    integer,
    state              text             NOT NULL,
    version            integer          NOT NULL,
    authorization_seq  integer          NOT NULL,
    authorized_at      timestamptz      NOT NULL,
    authorized_until   timestamptz,
    attempts           integer          NOT NULL,
    dispatches         integer          NOT NULL,
    key_generation     integer          NOT NULL,
    open_attempt       integer,
    owner              text,
    created_at         timestamptz      NOT NULL,
    updated_at         timestamptz      NOT NULL,
    finished_at        timestamptz,
    result             jsonb,
    failure            jsonb,
    lapse_reason       text,
    CONSTRAINT action_executions_pkey PRIMARY KEY (id),
    -- One approved decision cannot authorize two independent executions.
    CONSTRAINT action_executions_one_per_approval UNIQUE (project_id, request_id),
    -- Target of the reconciliation's binding foreign key.
    CONSTRAINT action_executions_fingerprint_key UNIQUE (id, action_fingerprint),
    CONSTRAINT action_executions_run_fkey FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, run_id),
    CONSTRAINT action_executions_request_fkey FOREIGN KEY (project_id, run_id, request_id)
        REFERENCES approval_requests (project_id, run_id, id),
    -- The execution's action is the one the person APPROVED: same request, same fingerprint,
    -- outcome approved. A rejected or differently-bound decision has no matching row.
    CONSTRAINT action_executions_approved_binding_fkey
        FOREIGN KEY (project_id, request_id, action_fingerprint, decision_outcome)
        REFERENCES approval_decisions (project_id, request_id, action_fingerprint, outcome),
    CONSTRAINT action_executions_approval_bound CHECK (
        (request_id IS NULL AND decision_outcome IS NULL)
        OR (request_id IS NOT NULL AND decision_outcome = 'approved')
    ),
    CONSTRAINT action_executions_id_format CHECK (id ~ '^axe-[0-9a-f]{20}$'),
    CONSTRAINT action_executions_fingerprint_format CHECK (action_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT action_executions_category_valid CHECK (
        action_category IN (
            'read_workspace', 'write_workspace', 'execute_local', 'execute_sandboxed', 'destructive_delete',
            'spend_money', 'production_deploy', 'credential_change', 'external_communication', 'network_access'
        )
    ),
    CONSTRAINT action_executions_params_is_object CHECK (jsonb_typeof(action_params) = 'object'),
    CONSTRAINT action_executions_state_valid CHECK (
        state IN ('authorized', 'dispatching', 'succeeded', 'failed', 'uncertain', 'lapsed')
    ),
    CONSTRAINT action_executions_effect_valid CHECK (effect_class IN ('read_only', 'idempotent', 'non_idempotent')),
    CONSTRAINT action_executions_idempotency_valid CHECK (
        (effect_class = 'idempotent' AND idempotency IN ('natural', 'keyed'))
        OR (effect_class <> 'idempotent' AND idempotency IS NULL)
    ),
    CONSTRAINT action_executions_key_retention CHECK (
        (idempotency = 'keyed') = (key_retention_s IS NOT NULL) AND (key_retention_s IS NULL OR key_retention_s > 0)
    ),
    -- Class C: one automatic attempt per authorization.
    CONSTRAINT action_executions_non_idempotent_single CHECK (effect_class <> 'non_idempotent' OR max_attempts = 1),
    CONSTRAINT action_executions_counters CHECK (
        visit >= 1 AND max_attempts BETWEEN 1 AND 10 AND version >= 1 AND authorization_seq >= 1
        AND key_generation >= 1 AND attempts >= 0 AND dispatches BETWEEN 0 AND max_attempts AND dispatches <= attempts
    ),
    CONSTRAINT action_executions_dispatch_owned CHECK (
        (state = 'dispatching') = (open_attempt IS NOT NULL)
        AND (state = 'dispatching') = (owner IS NOT NULL)
        AND (open_attempt IS NULL OR open_attempt = attempts)
    ),
    CONSTRAINT action_executions_finished_iff_final CHECK (
        (state IN ('succeeded', 'failed', 'lapsed')) = (finished_at IS NOT NULL)
    ),
    CONSTRAINT action_executions_result_iff_succeeded CHECK (
        (state = 'succeeded') = (result IS NOT NULL) AND (result IS NULL OR jsonb_typeof(result) = 'object')
    ),
    CONSTRAINT action_executions_failure_iff_failed CHECK (
        (state = 'failed') = (failure IS NOT NULL) AND (failure IS NULL OR jsonb_typeof(failure) = 'object')
    ),
    CONSTRAINT action_executions_lapse_reason CHECK ((state = 'lapsed') = (lapse_reason IS NOT NULL)),
    CONSTRAINT action_executions_time_order CHECK (
        updated_at >= created_at AND (authorized_until IS NULL OR authorized_until > authorized_at)
    )
);
COMMENT ON TABLE action_executions IS 'What Agent Factory knows about attempting one approved, exact action. Approval is not execution.';

CREATE TABLE action_execution_attempts (
    execution_id      text COLLATE "C" NOT NULL,
    attempt           integer          NOT NULL,
    authorization_seq integer          NOT NULL,
    owner             text             NOT NULL,
    idempotency_key   text COLLATE "C",
    dispatched_at     timestamptz      NOT NULL,
    ended_at          timestamptz,
    result            text,
    detail            text             NOT NULL,
    CONSTRAINT action_execution_attempts_pkey PRIMARY KEY (execution_id, attempt),
    CONSTRAINT action_execution_attempts_execution_fkey FOREIGN KEY (execution_id) REFERENCES action_executions (id),
    CONSTRAINT action_execution_attempts_positive CHECK (attempt >= 1 AND authorization_seq >= 1),
    CONSTRAINT action_execution_attempts_key_format CHECK (
        idempotency_key IS NULL OR idempotency_key ~ '^af1-axe-[0-9a-f]{20}-k[1-9][0-9]{0,5}$'
    ),
    CONSTRAINT action_execution_attempts_result_valid CHECK (
        result IS NULL OR result IN ('succeeded', 'failed', 'not_dispatched', 'indeterminate')
    ),
    CONSTRAINT action_execution_attempts_ended_iff_result CHECK ((ended_at IS NULL) = (result IS NULL)),
    CONSTRAINT action_execution_attempts_time_order CHECK (ended_at IS NULL OR ended_at >= dispatched_at)
);
COMMENT ON TABLE action_execution_attempts IS 'One dispatch of an approved action, committed before the side effect.';

-- At most one attempt in flight per execution. A constraint more than a query aid.
CREATE UNIQUE INDEX action_execution_attempts_one_open
    ON action_execution_attempts (execution_id) WHERE ended_at IS NULL;

CREATE TABLE action_reconciliations (
    execution_id           text COLLATE "C" NOT NULL,
    attempt                integer          NOT NULL,
    action_fingerprint     text COLLATE "C" NOT NULL,
    choice                 text             NOT NULL,
    decided_at             timestamptz      NOT NULL,
    actor_kind             text             NOT NULL,
    actor_id               text             NOT NULL,
    actor_display_name     text             NOT NULL,
    actor_verified         boolean          NOT NULL,
    comment                text             NOT NULL,
    retry_authorized_until timestamptz,
    -- One determination per uncertain attempt; never edited.
    CONSTRAINT action_reconciliations_pkey PRIMARY KEY (execution_id, attempt),
    CONSTRAINT action_reconciliations_attempt_fkey FOREIGN KEY (execution_id, attempt)
        REFERENCES action_execution_attempts (execution_id, attempt),
    -- The person reconciled exactly this execution's action.
    CONSTRAINT action_reconciliations_binding_fkey FOREIGN KEY (execution_id, action_fingerprint)
        REFERENCES action_executions (id, action_fingerprint),
    CONSTRAINT action_reconciliations_choice_valid CHECK (
        choice IN ('it_happened', 'did_not_happen', 'did_not_happen_retry')
    ),
    -- Only a person can say what happened in the outside world; never "system".
    CONSTRAINT action_reconciliations_actor_kind_valid CHECK (actor_kind IN ('unauthenticated', 'user')),
    CONSTRAINT action_reconciliations_unauthenticated_unverified CHECK (
        NOT (actor_kind = 'unauthenticated' AND actor_verified)
    ),
    CONSTRAINT action_reconciliations_retry_window CHECK (
        (choice = 'did_not_happen_retry') = (retry_authorized_until IS NOT NULL)
        AND (retry_authorized_until IS NULL OR retry_authorized_until > decided_at)
    )
);
COMMENT ON TABLE action_reconciliations IS 'A person''s determination of one uncertain attempt; a retry here is a new authorization.';

-- "Which executions does this run have?": recovery when a run resumes, lapsing the unused
-- authorizations of a finished node visit, and deriving milestones on every checkpoint.
-- Lookups by execution, attempt, or reconciliation use the primary keys.
CREATE INDEX action_executions_run_idx ON action_executions (project_id, run_id);
