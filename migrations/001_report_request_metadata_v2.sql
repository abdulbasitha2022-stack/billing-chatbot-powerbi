-- =============================================================================================
-- Migration 001: report_request_metadata -> ONE ROW PER REPORT REQUEST (13 columns)
--
--   Run as an administrator, in the billing database, with the chatbot backend STOPPED:
--       mysql -u root -p billing < migrations/001_report_request_metadata_v2.sql
--
--   * Non-destructive: an old-shape table is RENAMED to report_request_metadata_legacy_20261027
--     (all its data stays there), never dropped or truncated.
--   * Safe to run more than once: it detects what already exists.
--   * Old rows are copied into the new table. Their user_id/user_role stay NULL (the old
--     requested_by value came from the browser and cannot be trusted); platform is set to
--     'Power BI' (the POC only had that client); day_id is computed with the offset below.
--   * If any statement fails the script stops (mysql default). Nothing before the failing step is
--     undone, and re-running it after fixing the problem is safe.
-- =============================================================================================

-- EDIT THIS: the offset of your application time zone (config_billing.yaml -> app.timezone) from UTC.
-- India = '+05:30'. Only used to compute day_id for rows that already exist.
SET @app_utc_offset := '+05:30';

SET @legacy := 'report_request_metadata_legacy_20261027';

-- ---- Step 1: if the table exists in the OLD shape, rename it (keep as backup) ---------------
SET @tbl_exists    := (SELECT COUNT(*) FROM information_schema.tables  WHERE table_schema = DATABASE() AND table_name = 'report_request_metadata');
SET @has_day_id    := (SELECT COUNT(*) FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'report_request_metadata' AND column_name = 'day_id');
SET @legacy_exists := (SELECT COUNT(*) FROM information_schema.tables  WHERE table_schema = DATABASE() AND table_name = @legacy);

-- Old table present but the backup name is already taken: stop instead of guessing.
SET @sql := IF(@tbl_exists = 1 AND @has_day_id = 0 AND @legacy_exists = 1,
               'SELECT * FROM `MIGRATION_STOPPED_backup_table_report_request_metadata_legacy_20261027_already_exists`',
               'SELECT ''step 1: no conflict'' AS info');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @sql := IF(@tbl_exists = 1 AND @has_day_id = 0 AND @legacy_exists = 0,
               CONCAT('RENAME TABLE report_request_metadata TO ', @legacy),
               'SELECT ''step 1: nothing to rename'' AS info');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- Step 2: create the new table (does nothing if it already exists in the new shape) ------
CREATE TABLE IF NOT EXISTS report_request_metadata (
    session_id            VARCHAR(64)     NOT NULL,                 -- the conversation (NOT unique: one session, many reports)
    request_id            VARCHAR(64)     NOT NULL,                 -- one report-generation request
    user_id               VARCHAR(255)    NULL,                     -- VERIFIED user only; NULL when identity is unknown
    user_role             VARCHAR(100)    NULL,                     -- VERIFIED role only
    platform              VARCHAR(50)     NULL,                     -- Power BI (Tableau only when implemented)
    view_name             VARCHAR(128)    NULL,                     -- vw_<description>_<YYYYMMDD>_<session>
    request_status        VARCHAR(30)     NOT NULL DEFAULT 'initiated',   -- initiated | in_progress | completed | failed
    sql_source            VARCHAR(30)     NULL,                     -- e.g. groq_api, claude_api, manual_mock, chart_change
    response_time_ms      BIGINT UNSIGNED NULL,                     -- processing time of the whole request
    customer_satisfaction BOOLEAN         NULL,                     -- NULL not rated | 1 satisfied | 0 unsatisfied
    request_ts            DATETIME(6)     NOT NULL,                 -- UTC
    error_message         TEXT            NULL,
    day_id                DATE            NOT NULL,                 -- request date in the application time zone
    PRIMARY KEY (request_id),
    KEY idx_rrm_session (session_id),
    KEY idx_rrm_user    (user_id),
    KEY idx_rrm_status  (request_status),
    KEY idx_rrm_view    (view_name),
    KEY idx_rrm_day     (day_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

-- ---- Step 3: copy the old rows over (only if the new table is still empty) ------------------
SET @legacy_exists := (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = @legacy);
SET @new_rows      := (SELECT COUNT(*) FROM report_request_metadata);

SET @sql := IF(@legacy_exists = 1 AND @new_rows = 0,
    CONCAT(
      'INSERT INTO report_request_metadata ',
      '(session_id, request_id, user_id, user_role, platform, view_name, request_status, sql_source, ',
      ' response_time_ms, customer_satisfaction, request_ts, error_message, day_id) ',
      'SELECT COALESCE(NULLIF(session_id, ''''), ''unknown''), request_id, NULL, NULL, ''Power BI'', view_name, ',
      ' CASE request_status ',
      '   WHEN ''view_created'' THEN ''completed'' ',
      '   WHEN ''failed'' THEN ''failed'' WHEN ''error'' THEN ''failed'' ',
      '   WHEN ''received'' THEN ''initiated'' WHEN ''clarifying'' THEN ''initiated'' ',
      '   WHEN ''awaiting_visual_choice'' THEN ''initiated'' WHEN ''awaiting_manual_sql'' THEN ''initiated'' ',
      '   ELSE ''in_progress'' END, ',
      ' LEFT(sql_source, 30), GREATEST(total_runtime_ms, 0), NULL, ',
      ' COALESCE(request_ts, UTC_TIMESTAMP(6)), error_message, ',
      ' DATE(CONVERT_TZ(COALESCE(request_ts, UTC_TIMESTAMP(6)), ''+00:00'', ''', @app_utc_offset, ''')) ',
      'FROM ', @legacy),
    'SELECT ''step 3: nothing to copy'' AS info');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- Step 4: permissions for the chatbot's restricted account --------------------------------
-- A table that was renamed/created does not inherit table-level grants. If your account is
-- different (or the host is not '%'), change the user below. Needed: SELECT, INSERT, UPDATE.
GRANT SELECT, INSERT, UPDATE ON billing.report_request_metadata TO 'view_builder'@'%';
-- The conversation log (created earlier by chatbot_schema.sql) needs INSERT, SELECT:
-- GRANT INSERT, SELECT ON billing.chat_conversation_log TO 'view_builder'@'%';

-- ---- Step 5: check the result ---------------------------------------------------------------
SELECT COUNT(*) AS rows_in_new_table FROM report_request_metadata;
SHOW COLUMNS FROM report_request_metadata;
