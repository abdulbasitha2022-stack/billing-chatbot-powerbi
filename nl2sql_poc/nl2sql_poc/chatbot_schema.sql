-- Run once against the billing database:  mysql -u root -p billing < chatbot_schema.sql
-- Supersedes report_requests_schema.sql from the earlier, simpler version - this one tracks
-- finished reports (with the view that was created) rather than every in-flight message.

CREATE TABLE IF NOT EXISTS report_views (
    view_id      INT AUTO_INCREMENT PRIMARY KEY,
    session_id   VARCHAR(64) NOT NULL,
    question     TEXT NOT NULL,         -- the final, fully-clarified business question
    sql_text     TEXT NOT NULL,
    view_name    VARCHAR(128) NOT NULL UNIQUE,
    assumptions  TEXT,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Can CREATE VIEW and SELECT (needed to define a view over the tables) - nothing else.
-- No INSERT/UPDATE/DELETE on data, no DROP, no CREATE TABLE. Verified on a real MySQL
-- instance: this user can create a view and is blocked on all of those by the server
-- itself, not just by application code.
CREATE USER IF NOT EXISTS 'view_builder'@'%' IDENTIFIED BY 'view_builder';
GRANT SELECT, CREATE VIEW ON billing.* TO 'view_builder'@'%';

-- view_builder also needs to INSERT into report_views (its own bookkeeping table only).
GRANT INSERT, SELECT ON billing.report_views TO 'view_builder'@'%';

-- Note: Power BI's existing billing_readonly user does NOT need a new grant here - MySQL's
-- schema-level "GRANT SELECT ON billing.*" (already run when the billing DB was set up)
-- covers views created later in the same schema automatically. Verified.

-- ---------------------------------------------------------------------------------------------
-- Conversation audit log: every question the user asked and every answer the assistant gave.
-- Run this section once (it is safe to re-run). The chatbot also writes the same records to
-- logs/conversations_billing.jsonl, so nothing is lost if this table does not exist yet.
CREATE TABLE IF NOT EXISTS chat_conversation_log (
    log_id        BIGINT AUTO_INCREMENT PRIMARY KEY,
    session_id    VARCHAR(64)  NOT NULL,   -- one chat window / conversation
    seq           INT          NOT NULL,   -- message order inside the session
    request_id    VARCHAR(64)  NULL,       -- links to report_request_metadata.request_id
    role          VARCHAR(16)  NOT NULL,   -- 'user' (question/answer) or 'assistant'
    message_kind  VARCHAR(32)  NULL,       -- text | clarify | report | error | status
    intent        VARCHAR(32)  NULL,       -- chat, new_report, modify_report, change_chart, ...
    message_text  TEXT         NOT NULL,   -- the full message exactly as shown in the chat
    view_name     VARCHAR(128) NULL,       -- set on the message that announced a report
    page_name     VARCHAR(255) NULL,       -- the Power BI page that was created
    chart_type    VARCHAR(32)  NULL,
    requested_by  VARCHAR(255) NULL,
    created_at    DATETIME     NOT NULL,   -- UTC
    INDEX idx_chat_log_session (session_id, seq),
    INDEX idx_chat_log_request (request_id)
);
GRANT INSERT, SELECT ON billing.chat_conversation_log TO 'view_builder'@'%';
FLUSH PRIVILEGES;


-- ============================================================================
-- Conversation audit log: every question the user asked and every answer given.
-- Run this once (as an administrator), then restart chatbot_api.py.
-- Until it exists the chatbot still works; it just writes logs/conversations_billing.jsonl only.
-- ============================================================================
CREATE TABLE IF NOT EXISTS chat_conversation_log (
    log_id        BIGINT       NOT NULL AUTO_INCREMENT PRIMARY KEY,
    session_id    VARCHAR(64)  NOT NULL,            -- one chat window / conversation
    seq           INT          NOT NULL,            -- order of the message inside the session
    request_id    VARCHAR(64)  NULL,                -- links to report_request_metadata.request_id
    role          VARCHAR(16)  NOT NULL,            -- 'user' (question/answer) or 'assistant'
    message_kind  VARCHAR(32)  NULL,                -- text, clarify, report, error, status
    intent        VARCHAR(32)  NULL,                -- chat, explain, clarify, choose_chart, new_report, ...
    message_text  TEXT         NOT NULL,
    view_name     VARCHAR(128) NULL,                -- set on the message that announced a report
    page_name     VARCHAR(255) NULL,
    chart_type    VARCHAR(32)  NULL,
    requested_by  VARCHAR(255) NULL,
    created_at    DATETIME     NOT NULL,            -- UTC
    INDEX idx_conv_session (session_id, seq),
    INDEX idx_conv_request (request_id),
    INDEX idx_conv_view (view_name)
);

GRANT INSERT, SELECT ON billing.chat_conversation_log TO 'view_builder'@'%';

-- ---- Handy queries --------------------------------------------------------
-- Every conversation, newest first:
--   SELECT session_id, MIN(created_at) AS started, COUNT(*) AS messages, SUM(role='user') AS questions
--   FROM chat_conversation_log GROUP BY session_id ORDER BY started DESC;
-- One conversation, in order:
--   SELECT seq, role, message_kind, message_text, view_name, page_name
--   FROM chat_conversation_log WHERE session_id = '<session id>' ORDER BY seq;
-- Everything behind one generated view (questions, answers, SQL):
--   SELECT c.seq, c.role, c.message_text, m.resolved_request, m.generated_sql, m.view_name
--   FROM report_request_metadata m JOIN chat_conversation_log c ON c.request_id = m.request_id
--   WHERE m.view_name = 'vw_refunds_by_reason_2026_3fa91c' ORDER BY c.seq;


-- ============================================================================
-- report_request_metadata: ONE ROW PER REPORT-GENERATION REQUEST (not per chat message).
-- Use this only for a NEW install. If the table already exists in the old shape,
-- CREATE TABLE IF NOT EXISTS will NOT change it: run migrations/001_report_request_metadata_v2.sql instead.
-- request_status: initiated -> in_progress -> completed | failed
-- ============================================================================
CREATE TABLE IF NOT EXISTS report_request_metadata (
    session_id            VARCHAR(64)     NOT NULL,
    request_id            VARCHAR(64)     NOT NULL,
    user_id               VARCHAR(255)    NULL,
    user_role             VARCHAR(100)    NULL,
    platform              VARCHAR(50)     NULL,
    view_name             VARCHAR(128)    NULL,
    request_status        VARCHAR(30)     NOT NULL DEFAULT 'initiated',
    sql_source            VARCHAR(30)     NULL,
    response_time_ms      BIGINT UNSIGNED NULL,
    customer_satisfaction BOOLEAN         NULL,
    request_ts            DATETIME(6)     NOT NULL,
    error_message         TEXT            NULL,
    day_id                DATE            NOT NULL,
    PRIMARY KEY (request_id),
    KEY idx_rrm_session (session_id),
    KEY idx_rrm_user    (user_id),
    KEY idx_rrm_status  (request_status),
    KEY idx_rrm_view    (view_name),
    KEY idx_rrm_day     (day_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4;

GRANT SELECT, INSERT, UPDATE ON billing.report_request_metadata TO 'view_builder'@'%';
