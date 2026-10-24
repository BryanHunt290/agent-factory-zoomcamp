-- 0005_run_claims: which worker process owns each run, for how long, and in which generation.
--
-- Several worker processes may share this database (docs/workers.md). A worker may change a
-- run only while it holds the run's claim: one row per run, naming the owning worker, a lease
-- that ends at a PostgreSQL time, and a fencing token.
--
--   worker_id          the owner: a random id per worker PROCESS (wkr- + 20 hex), never a
--                      hostname. NULL when nobody owns the run.
--   fencing_token      the ownership generation. 0 until the first claim; every claim adds 1,
--                      so it only ever increases. Renewing or releasing never changes it.
--                      Every write a worker makes to the run is conditional on its token still
--                      being the current one (persistence/claims.py), so a stale worker (41)
--                      cannot change anything once a newer owner (42) exists.
--   lease_expires_at   the owner's lease, in PostgreSQL time (now() + lease). The lease is valid
--                      while now() < lease_expires_at and has expired once lease_expires_at <=
--                      now(). Only the database clock decides; worker clocks are never compared.
--   claimed_at,        when the current (or last) ownership began, and the owner's last
--   last_heartbeat_at  confirmed renewal. Kept after a release, for diagnosis.
--   available_at       NULL: no worker should pick the run up (it waits for a person, is
--                      blocked, or has finished). Otherwise the run wants a worker from that
--                      time on. It stays set while the run is owned, so the run of a worker that
--                      dies becomes claimable again as soon as the dead worker's lease expires.
--
-- Claiming is one short transaction (docs/workers.md, "claim query"): find the oldest available
-- run with no live owner, lock it with FOR UPDATE SKIP LOCKED, assign the worker, add 1 to the
-- token, set the lease, commit. The graph runs afterwards, outside any transaction.
--
-- No worker registry: correctness depends only on these rows. A worker's liveness is its lease.

CREATE TABLE run_claims (
    project_id        text COLLATE "C" NOT NULL,
    run_id            text COLLATE "C" NOT NULL,
    worker_id         text COLLATE "C",
    fencing_token     bigint           NOT NULL,
    claimed_at        timestamptz,
    lease_expires_at  timestamptz,
    last_heartbeat_at timestamptz,
    available_at      timestamptz,
    created_at        timestamptz      NOT NULL,
    updated_at        timestamptz      NOT NULL,
    CONSTRAINT run_claims_pkey PRIMARY KEY (project_id, run_id),
    -- A claim belongs to a real run. Runs are never deleted, so this never has to search the table.
    CONSTRAINT run_claims_run_fkey FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, run_id),
    CONSTRAINT run_claims_worker_id_format CHECK (worker_id IS NULL OR worker_id ~ '^wkr-[0-9a-f]{20}$'),
    CONSTRAINT run_claims_fencing_token_range CHECK (fencing_token >= 0),
    -- Generation 0 exactly when the run has never been claimed.
    CONSTRAINT run_claims_claimed_iff_generation CHECK ((fencing_token = 0) = (claimed_at IS NULL)),
    -- An owner always has a lease; a run without an owner has none.
    CONSTRAINT run_claims_owner_has_lease CHECK ((worker_id IS NULL) = (lease_expires_at IS NULL)),
    CONSTRAINT run_claims_owner_has_heartbeat CHECK (worker_id IS NULL OR last_heartbeat_at IS NOT NULL),
    -- A lease ends after the claim and the renewal that set it, and is never longer than five
    -- minutes (the longest lease the configuration accepts; docs/workers.md, "leases").
    CONSTRAINT run_claims_lease_window CHECK (
        lease_expires_at IS NULL
        OR (lease_expires_at > claimed_at
            AND lease_expires_at > last_heartbeat_at
            AND lease_expires_at <= last_heartbeat_at + interval '5 minutes')
    ),
    CONSTRAINT run_claims_heartbeat_after_claim CHECK (last_heartbeat_at IS NULL OR last_heartbeat_at >= claimed_at),
    CONSTRAINT run_claims_time_order CHECK (updated_at >= created_at)
);
COMMENT ON TABLE run_claims IS 'Which worker process owns a run, until when (PostgreSQL time), and in which generation (fencing token).';

-- Serves the claim query: available_at <= now() ORDER BY available_at, project_id, run_id. Only
-- runs that want a worker are indexed. Owned runs stay in it (their available_at stays set), so
-- a claim scan also reads the rows of runs being worked on: at most one per worker.
CREATE INDEX run_claims_claimable_idx ON run_claims (available_at, project_id, run_id) WHERE available_at IS NOT NULL;
