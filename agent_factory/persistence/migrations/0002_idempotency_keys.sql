-- 0002_idempotency_keys: the stored response of each request sent with an Idempotency-Key.
--
-- The API's createProject stores, in the same transaction as the project it
-- creates, the response it returned. A retry with the same key and the same
-- request gets that response back instead of creating a second project. The
-- primary key settles concurrent attempts: exactly one insert can win, and the
-- loser's whole transaction (its project included) rolls back
-- (docs/backend.md#idempotency).
--
-- Nothing secret is stored: request_hash is a SHA-256 over the operation and
-- its validated request body (a project's name, template, and description),
-- and response_body is the project exactly as the client already received it.
--
-- No index beyond the primary key: lookups are always by (scope, key). Purging
-- expired rows scans the table, which holds about a day of project creations;
-- docs/database.md#indexes.

CREATE TABLE idempotency_keys (
    scope           text COLLATE "C" NOT NULL,
    key             text COLLATE "C" NOT NULL,
    request_hash    text COLLATE "C" NOT NULL,
    response_status integer          NOT NULL,
    response_body   jsonb            NOT NULL,
    created_at      timestamptz      NOT NULL,
    expires_at      timestamptz      NOT NULL,
    CONSTRAINT idempotency_keys_pkey PRIMARY KEY (scope, key),
    CONSTRAINT idempotency_keys_scope_format CHECK (scope ~ '^[a-z][a-z0-9_]{0,63}$'),
    CONSTRAINT idempotency_keys_key_format CHECK (key ~ '^[A-Za-z0-9_-]{16,128}$'),
    CONSTRAINT idempotency_keys_hash_format CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT idempotency_keys_status_success CHECK (response_status BETWEEN 200 AND 299),
    CONSTRAINT idempotency_keys_body_is_object CHECK (jsonb_typeof(response_body) = 'object'),
    CONSTRAINT idempotency_keys_expiry_order CHECK (expires_at > created_at)
);
COMMENT ON TABLE idempotency_keys IS 'Responses of requests sent with an Idempotency-Key, replayed to retries. No secrets.';
