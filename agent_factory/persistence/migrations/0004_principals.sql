-- 0004_principals: who may use the API, and what each may do.
--
-- Authentication proves who a request comes from (docs/authentication.md). These tables
-- hold what Agent Factory knows about that identity:
--
--   principals             one row per enrolled identity. Its identity is (issuer, subject),
--                          the pair an OpenID Connect provider never reassigns, mapped once
--                          to a stable internal id. display_name and email are shown to
--                          people and may change; they never identify anyone.
--   principal_permissions  the permissions granted to a principal, from a closed vocabulary.
--                          No permission implies another.
--
-- The owner enrolls principals and grants permissions with the local CLI; no HTTP route
-- writes these tables. A disabled principal cannot authenticate: the API reads its status
-- on every request. Principals are disabled, never deleted, so the decisions they made keep
-- naming them.
--
-- An approval decision by an authenticated user now references its principal: such a
-- decision carries principal_id, which must name an enrolled principal and equal the actor
-- id, and no other decision carries one. Earlier decisions (the unauthenticated local CLI)
-- are unchanged.

CREATE TABLE principals (
    id           text COLLATE "C" NOT NULL,
    issuer       text COLLATE "C" NOT NULL,
    subject      text COLLATE "C" NOT NULL,
    display_name text,
    email        text,
    status       text             NOT NULL,
    created_at   timestamptz      NOT NULL,
    updated_at   timestamptz      NOT NULL,
    CONSTRAINT principals_pkey PRIMARY KEY (id),
    -- One principal per external identity. Its index serves the lookup every authenticated
    -- request makes: WHERE issuer = $1 AND subject = $2.
    CONSTRAINT principals_external_identity_key UNIQUE (issuer, subject),
    CONSTRAINT principals_id_format CHECK (id ~ '^prn-[0-9a-f]{20}$'),
    -- Printable ASCII without spaces. Lengths are separate checks: PostgreSQL's regular
    -- expressions allow repetition counts up to 255 only.
    CONSTRAINT principals_issuer_format CHECK (issuer ~ '^[!-~]+$' AND length(issuer) <= 512),
    CONSTRAINT principals_subject_format CHECK (subject ~ '^[!-~]+$' AND length(subject) <= 255),
    CONSTRAINT principals_display_name_length CHECK (display_name IS NULL OR length(display_name) BETWEEN 1 AND 200),
    CONSTRAINT principals_email_length CHECK (email IS NULL OR length(email) BETWEEN 1 AND 320),
    CONSTRAINT principals_status_valid CHECK (status IN ('active', 'disabled')),
    CONSTRAINT principals_time_order CHECK (updated_at >= created_at)
);
COMMENT ON TABLE principals IS 'An enrolled identity: an OpenID Connect issuer and subject, mapped to a stable internal id. Never keyed by email.';

CREATE TABLE principal_permissions (
    principal_id text COLLATE "C" NOT NULL,
    permission   text COLLATE "C" NOT NULL,
    granted_at   timestamptz      NOT NULL,
    -- Its index serves the permission lookup every authenticated request makes:
    -- WHERE principal_id = $1 (the leading column).
    CONSTRAINT principal_permissions_pkey PRIMARY KEY (principal_id, permission),
    CONSTRAINT principal_permissions_principal_fkey FOREIGN KEY (principal_id) REFERENCES principals (id),
    CONSTRAINT principal_permissions_permission_valid CHECK (permission IN (
        'projects:read', 'projects:create', 'runs:read', 'milestones:read',
        'approvals:read', 'approvals:decide', 'actions:reconcile'
    ))
);
COMMENT ON TABLE principal_permissions IS 'What a principal may do: one row per granted permission, from a closed vocabulary.';

ALTER TABLE approval_decisions ADD COLUMN principal_id text COLLATE "C";
-- No index on principal_id: no query filters decisions by principal, and principals are
-- never deleted, so the foreign key never has to search this table.
ALTER TABLE approval_decisions ADD CONSTRAINT approval_decisions_principal_fkey
    FOREIGN KEY (principal_id) REFERENCES principals (id);
-- A user decision is an authenticated principal's: verified, and principal_id = actor_id.
-- No other decision names a principal.
ALTER TABLE approval_decisions ADD CONSTRAINT approval_decisions_user_is_principal CHECK (
    (actor_kind = 'user') = (principal_id IS NOT NULL)
    AND (principal_id IS NULL OR (principal_id = actor_id AND actor_verified))
);
