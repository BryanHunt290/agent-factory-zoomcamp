-- 0009_cloud_read_category: the cloud_read action category (docs/capabilities.md, openapi.yaml ActionCategory).
--
-- cloud_read is read-only access to an external cloud system through a typed, trusted adapter. The
-- category vocabulary must be the same in the contract, the API, the domain, the frontend, and here, so
-- the two CHECK constraints that list the categories (0001 approval_requests, 0003 action_executions) are
-- replaced with ones that include it. Applied migrations are never edited; this one only widens the set.

ALTER TABLE approval_requests DROP CONSTRAINT approval_requests_category_valid;
ALTER TABLE approval_requests ADD CONSTRAINT approval_requests_category_valid CHECK (
    action_category IN (
        'read_workspace', 'write_workspace', 'execute_local', 'execute_sandboxed', 'destructive_delete',
        'spend_money', 'production_deploy', 'credential_change', 'external_communication', 'network_access',
        'cloud_read'
    )
);

ALTER TABLE action_executions DROP CONSTRAINT action_executions_category_valid;
ALTER TABLE action_executions ADD CONSTRAINT action_executions_category_valid CHECK (
    action_category IN (
        'read_workspace', 'write_workspace', 'execute_local', 'execute_sandboxed', 'destructive_delete',
        'spend_money', 'production_deploy', 'credential_change', 'external_communication', 'network_access',
        'cloud_read'
    )
);
