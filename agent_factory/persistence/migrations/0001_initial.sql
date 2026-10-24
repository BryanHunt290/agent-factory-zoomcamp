-- 0001_initial: product records for projects, runs, milestones, and approvals.
--
-- Applied once per schema by agent_factory/persistence/migrator.py, inside a
-- single transaction, with search_path set to the target schema (default
-- agent_factory). docs/database.md explains the mapping from the domain model.
--
-- Principles:
--   * The database guards structure and relationships: keys, references,
--     uniqueness, allowed values, JSON shapes, and time ordering.
--   * The domain model guards behaviour: state-machine transitions, text
--     limits, and fingerprint computation. Rows are re-validated by the domain
--     model whenever they are read.
--   * Identifiers use COLLATE "C": comparison and ordering by code point,
--     independent of the server's locale (the same order Python uses).
--   * Timestamps are timestamptz, an absolute instant. Agent Factory writes
--     and reads them in UTC.

CREATE TABLE projects (
    id               text COLLATE "C" NOT NULL,
    name             text             NOT NULL,
    created_at       timestamptz      NOT NULL,
    template         text,
    description      text             NOT NULL,
    milestone_policy jsonb            NOT NULL,
    metadata         jsonb            NOT NULL,
    CONSTRAINT projects_pkey PRIMARY KEY (id),
    CONSTRAINT projects_id_format CHECK (id ~ '^prj-[0-9a-f]{20}$'),
    CONSTRAINT projects_policy_is_object CHECK (jsonb_typeof(milestone_policy) = 'object'),
    CONSTRAINT projects_metadata_is_object CHECK (jsonb_typeof(metadata) = 'object')
);
COMMENT ON TABLE projects IS 'A project handed to Agent Factory. Stable random id; never a filesystem path.';

CREATE TABLE runs (
    project_id    text COLLATE "C" NOT NULL,
    run_id        text COLLATE "C" NOT NULL,
    number        integer          NOT NULL,
    graph_name    text             NOT NULL,
    status        text             NOT NULL,
    created_at    timestamptz      NOT NULL,
    updated_at    timestamptz      NOT NULL,
    status_reason text             NOT NULL,
    CONSTRAINT runs_pkey PRIMARY KEY (project_id, run_id),
    CONSTRAINT runs_project_fkey FOREIGN KEY (project_id) REFERENCES projects (id),
    CONSTRAINT runs_number_key UNIQUE (project_id, number),
    CONSTRAINT runs_run_id_format CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$'),
    CONSTRAINT runs_number_positive CHECK (number >= 1),
    CONSTRAINT runs_status_valid CHECK (
        status IN ('running', 'awaiting_approval', 'blocked', 'completed', 'failed', 'exhausted')
    ),
    CONSTRAINT runs_updated_after_created CHECK (updated_at >= created_at)
);
COMMENT ON TABLE runs IS 'One execution of a project''s workflow. Engine run ids are unique per project only.';

CREATE TABLE milestones (
    id           text COLLATE "C" NOT NULL,
    project_id   text COLLATE "C" NOT NULL,
    run_id       text COLLATE "C" NOT NULL,
    sequence     integer          NOT NULL,
    key          text COLLATE "C" NOT NULL,
    type         text COLLATE "C" NOT NULL,
    category     text             NOT NULL,
    title        text             NOT NULL,
    summary      text             NOT NULL,
    status       text             NOT NULL,
    created_at   timestamptz      NOT NULL,
    occurred_at  timestamptz      NOT NULL,
    source_node  text,
    source_agent text,
    source_step  integer,
    metadata     jsonb            NOT NULL,
    resolves_id  text COLLATE "C",
    CONSTRAINT milestones_pkey PRIMARY KEY (id),
    CONSTRAINT milestones_run_fkey FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, run_id),
    CONSTRAINT milestones_sequence_key UNIQUE (project_id, run_id, sequence),
    CONSTRAINT milestones_key_key UNIQUE (project_id, run_id, key),
    -- Exists so that the foreign key below can require "a milestone of the same run".
    CONSTRAINT milestones_run_member_key UNIQUE (project_id, run_id, id),
    CONSTRAINT milestones_resolves_fkey FOREIGN KEY (project_id, run_id, resolves_id)
        REFERENCES milestones (project_id, run_id, id),
    CONSTRAINT milestones_id_format CHECK (id ~ '^ms-[0-9a-f]{20}$'),
    CONSTRAINT milestones_sequence_positive CHECK (sequence >= 1),
    CONSTRAINT milestones_category_valid CHECK (category IN ('workflow', 'deliverable', 'approval', 'lifecycle')),
    CONSTRAINT milestones_status_valid CHECK (status IN ('planned', 'in_progress', 'completed', 'failed', 'blocked')),
    CONSTRAINT milestones_occurred_after_created CHECK (occurred_at >= created_at),
    CONSTRAINT milestones_source_step_valid CHECK (source_step IS NULL OR source_step >= 0),
    CONSTRAINT milestones_metadata_is_object CHECK (jsonb_typeof(metadata) = 'object'),
    CONSTRAINT milestones_not_self_resolving CHECK (resolves_id IS NULL OR resolves_id <> id)
);
COMMENT ON TABLE milestones IS 'Human-facing progress of a run. occurred_at is when the current status was reached.';

CREATE TABLE approval_requests (
    project_id         text COLLATE "C" NOT NULL,
    id                 text COLLATE "C" NOT NULL,
    run_id             text COLLATE "C" NOT NULL,
    node               text             NOT NULL,
    agent              text             NOT NULL,
    action_tool        text             NOT NULL,
    action_category    text             NOT NULL,
    action_params      jsonb            NOT NULL,
    action_fingerprint text COLLATE "C" NOT NULL,
    reason             text             NOT NULL,
    risk               text             NOT NULL,
    requested_at       timestamptz      NOT NULL,
    status             text             NOT NULL,
    context            jsonb            NOT NULL,
    expires_at         timestamptz,
    updated_at         timestamptz,
    CONSTRAINT approval_requests_pkey PRIMARY KEY (project_id, id),
    CONSTRAINT approval_requests_run_fkey FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, run_id),
    -- Exists so that a decision can reference the request together with its exact action.
    CONSTRAINT approval_requests_action_key UNIQUE (project_id, id, action_fingerprint),
    CONSTRAINT approval_requests_id_format CHECK (id ~ '^apr-[0-9a-f]{16}$'),
    CONSTRAINT approval_requests_fingerprint_format CHECK (action_fingerprint ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT approval_requests_category_valid CHECK (
        action_category IN (
            'read_workspace', 'write_workspace', 'execute_local', 'execute_sandboxed', 'destructive_delete',
            'spend_money', 'production_deploy', 'credential_change', 'external_communication', 'network_access'
        )
    ),
    CONSTRAINT approval_requests_params_is_object CHECK (jsonb_typeof(action_params) = 'object'),
    CONSTRAINT approval_requests_context_is_object CHECK (jsonb_typeof(context) = 'object'),
    CONSTRAINT approval_requests_status_valid CHECK (
        status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')
    ),
    CONSTRAINT approval_requests_expiry_after_request CHECK (expires_at IS NULL OR expires_at > requested_at),
    -- A pending request has never changed status; a final one records when it did.
    CONSTRAINT approval_requests_updated_iff_final CHECK ((status = 'pending') = (updated_at IS NULL))
);
COMMENT ON TABLE approval_requests IS 'A protected action waiting for, or having received, a human decision. id is the engine''s exact-action request id.';

CREATE TABLE approval_decisions (
    id                 text COLLATE "C" NOT NULL,
    project_id         text COLLATE "C" NOT NULL,
    request_id         text COLLATE "C" NOT NULL,
    action_fingerprint text COLLATE "C" NOT NULL,
    outcome            text             NOT NULL,
    decided_at         timestamptz      NOT NULL,
    actor_kind         text             NOT NULL,
    actor_id           text             NOT NULL,
    actor_display_name text             NOT NULL,
    actor_verified     boolean          NOT NULL,
    comment            text             NOT NULL,
    CONSTRAINT approval_decisions_pkey PRIMARY KEY (id),
    -- One decision per request: approval_requests 1 --- 0..1 approval_decisions.
    CONSTRAINT approval_decisions_request_key UNIQUE (project_id, request_id),
    -- Exact-action binding: a decision can exist only for the fingerprint its request
    -- carries, and once it exists that fingerprint can no longer be changed.
    CONSTRAINT approval_decisions_binding_fkey FOREIGN KEY (project_id, request_id, action_fingerprint)
        REFERENCES approval_requests (project_id, id, action_fingerprint),
    CONSTRAINT approval_decisions_id_format CHECK (id ~ '^apd-[0-9a-f]{20}$'),
    CONSTRAINT approval_decisions_outcome_valid CHECK (outcome IN ('approved', 'rejected')),
    CONSTRAINT approval_decisions_actor_kind_valid CHECK (actor_kind IN ('unauthenticated', 'user', 'system')),
    CONSTRAINT approval_decisions_unauthenticated_unverified CHECK (NOT (actor_kind = 'unauthenticated' AND actor_verified))
);
COMMENT ON TABLE approval_decisions IS 'The single, final human decision on one approval request.';

-- Indexes beyond those that primary keys and unique constraints already create.
-- Each one serves a query Agent Factory makes; see docs/database.md#indexes.

-- "What happened between 22:00 and 07:00?": MilestoneQuery with since/until, ordered by
-- (occurred_at, run_id, sequence). Within a project (run_id, sequence) is unique, so the
-- index order is the complete query order.
CREATE INDEX milestones_project_occurred_idx ON milestones (project_id, occurred_at, run_id, sequence);

-- "What is waiting for my approval?": a project's pending requests, oldest first. Partial:
-- decided requests, the large majority over time, are not in the index at all.
CREATE INDEX approval_requests_pending_idx ON approval_requests (project_id, requested_at, id)
    WHERE status = 'pending';
