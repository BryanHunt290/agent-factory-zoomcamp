-- 0008_model_usage: each run's model usage, for the API and the dashboard (docs/model-providers.md).
--
-- The source of truth is the run's usage ledger (model_usage.jsonl next to its checkpoint), written
-- before and after every model call so that budgets survive a restart. Each checkpoint carries a
-- summary of it, and the recorder stores that summary here in the checkpoint's sync transaction.
-- Counts only: never a prompt, a model answer, or a credential. estimated_cost_usd is NULL unless the
-- operator configured prices: Agent Factory never claims a dollar figure it cannot compute.

CREATE TABLE run_model_usage (
    project_id         text COLLATE "C" NOT NULL,
    run_id             text COLLATE "C" NOT NULL,
    calls              integer          NOT NULL,
    failed_calls       integer          NOT NULL,
    input_tokens       bigint           NOT NULL,
    output_tokens      bigint           NOT NULL,
    cached_tokens      bigint           NOT NULL,
    estimated_cost_usd numeric(14, 6),
    by_model           jsonb            NOT NULL,
    updated_at         timestamptz      NOT NULL,
    CONSTRAINT run_model_usage_pkey PRIMARY KEY (project_id, run_id),
    CONSTRAINT run_model_usage_run_fkey FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, run_id),
    CONSTRAINT run_model_usage_counts_valid CHECK (
        calls >= 0 AND failed_calls >= 0 AND failed_calls <= calls
        AND input_tokens >= 0 AND output_tokens >= 0 AND cached_tokens >= 0
    ),
    CONSTRAINT run_model_usage_cost_valid CHECK (estimated_cost_usd IS NULL OR estimated_cost_usd >= 0),
    CONSTRAINT run_model_usage_by_model_is_array CHECK (jsonb_typeof(by_model) = 'array' AND jsonb_array_length(by_model) <= 20)
);
COMMENT ON TABLE run_model_usage IS 'A run''s model calls and tokens so far (a summary of its usage ledger). Counts only.';
