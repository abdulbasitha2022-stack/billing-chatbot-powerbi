-- SAFE MIGRATION for an existing legacy report_request_metadata table.
-- Step 1: Run this script as a database administrator. It does not delete or rename the old table.
-- Step 2: Compare row counts and inspect copied rows before the final swap.
USE billing;

CREATE TABLE IF NOT EXISTS report_request_metadata_v2 (
    session_id VARCHAR(64) NOT NULL,
    request_id VARCHAR(64) NOT NULL,
    user_id VARCHAR(255) NULL,
    user_role VARCHAR(100) NULL,
    platform VARCHAR(50) NULL,
    view_name VARCHAR(128) NULL,
    request_status VARCHAR(30) NOT NULL DEFAULT 'initiated',
    sql_source VARCHAR(30) NOT NULL DEFAULT 'manual_mock',
    response_time_ms BIGINT UNSIGNED NULL,
    customer_satisfaction BOOLEAN NULL,
    request_ts DATETIME(6) NOT NULL,
    error_message TEXT NULL,
    day_id DATE NOT NULL,
    PRIMARY KEY (request_id),
    KEY idx_rrm2_session_id (session_id),
    KEY idx_rrm2_user_id (user_id),
    KEY idx_rrm2_status (request_status),
    KEY idx_rrm2_view_name (view_name),
    KEY idx_rrm2_day_id (day_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;

INSERT INTO report_request_metadata_v2
    (session_id, request_id, user_id, user_role, platform, view_name, request_status,
     sql_source, response_time_ms, customer_satisfaction, request_ts, error_message, day_id)
SELECT COALESCE(session_id, ''), request_id, NULL, NULL, NULL, view_name,
       CASE WHEN request_status IN ('view_created', 'completed') THEN 'completed'
            WHEN request_status IN ('failed', 'error') THEN 'failed'
            WHEN request_status = 'initiated' THEN 'initiated'
            ELSE 'in_progress' END,
       COALESCE(sql_source, 'manual_mock'), COALESCE(total_runtime_ms, sql_runtime_ms), NULL,
       request_ts, error_message, DATE(request_ts)
FROM report_request_metadata
ON DUPLICATE KEY UPDATE
    session_id = VALUES(session_id), view_name = VALUES(view_name),
    request_status = VALUES(request_status), sql_source = VALUES(sql_source),
    response_time_ms = VALUES(response_time_ms), error_message = VALUES(error_message),
    day_id = VALUES(day_id);

-- Verify before swapping:
-- SELECT (SELECT COUNT(*) FROM report_request_metadata) AS old_count,
--        (SELECT COUNT(*) FROM report_request_metadata_v2) AS new_count;
-- SELECT * FROM report_request_metadata_v2 ORDER BY request_ts DESC LIMIT 20;
-- After verification, stop the API and run this once to preserve the old table as a backup:
-- RENAME TABLE report_request_metadata TO report_request_metadata_legacy,
--              report_request_metadata_v2 TO report_request_metadata;
-- Then re-grant the app account:
-- GRANT SELECT, INSERT, UPDATE ON billing.report_request_metadata TO 'view_builder'@'%';
-- FLUSH PRIVILEGES;
