-- =============================================================================================
-- Migration 003: saved_reports (the "Save report" button on generated report pages)
--
--   Run as an administrator, in the billing database:
--       mysql -u root -p billing < migrations/003_saved_reports.sql
--
--   * Additive only: creates one new table and grants the view_builder account access to it.
--   * Safe to run more than once (CREATE TABLE IF NOT EXISTS; GRANT is idempotent).
--   * Until it is run the chatbot keeps working; only saving / listing saved reports fails.
--
-- One row per saved report, keyed by the report request it was generated for. page_definition is
-- a JSON copy of the Power BI page's files, so a deleted page can be written back exactly as it
-- was. The data itself is NOT copied: the restored page imports the live view (view_name).
-- Rows stay until the user deletes them; deleting a row never touches the page or the view.
-- =============================================================================================
CREATE TABLE IF NOT EXISTS saved_reports (
    request_id      VARCHAR(64)  NOT NULL,          -- report_request_metadata.request_id
    session_id      VARCHAR(64)  NULL,              -- the chat the report came from
    user_id         VARCHAR(255) NULL,              -- owner (verified user), NULL in demo mode
    user_role       VARCHAR(100) NULL,              -- owner's role (the owner in demo mode)
    title           VARCHAR(255) NOT NULL,          -- page tab name when saved
    page_id         VARCHAR(64)  NOT NULL,          -- PBIR page folder name when saved
    visual_type     VARCHAR(32)  NULL,              -- lineChart, barChart, ..., tableEx
    view_name       VARCHAR(128) NOT NULL,          -- the MySQL view the page reads (live data)
    model_table     VARCHAR(160) NOT NULL,          -- 'billing vw_...' table in the semantic model
    sql_text        TEXT         NULL,              -- the view's SQL (role re-check, view recreation)
    request_text    TEXT         NULL,              -- the business question behind the report
    page_definition LONGTEXT     NOT NULL,          -- JSON {"page.json": "...", "visuals/<id>/visual.json": "..."}
    table_tmdl      MEDIUMTEXT   NULL,              -- the model table's .tmdl file
    saved_at        DATETIME     NOT NULL,          -- UTC
    last_opened_at  DATETIME     NULL,              -- UTC
    PRIMARY KEY (request_id),
    KEY idx_saved_user (user_id, saved_at),
    KEY idx_saved_role (user_role, saved_at)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

GRANT SELECT, INSERT, UPDATE, DELETE ON billing.saved_reports TO 'view_builder'@'%';
-- Saving reads the view's SQL from the registry (already granted by chatbot_schema.sql):
GRANT SELECT ON billing.report_views TO 'view_builder'@'%';
FLUSH PRIVILEGES;

-- Check:
SHOW COLUMNS FROM saved_reports;
