-- =============================================================================================
-- Migration 002: report_request_metadata -> request_id is the FIRST column and the PRIMARY KEY
--
--   Run as an administrator, in the billing database (after migration 001):
--       mysql -u root -p billing < migrations/002_request_id_first.sql
--
--   * Non-destructive: no row is deleted or changed. The column is only moved; its type stays
--     VARCHAR(64) so existing 32-character ids and the new 12-character ids both fit.
--   * Safe to run more than once: each step checks first and does nothing if already done.
--   * The application does not depend on the column order (it always names its columns), so the
--     backend keeps working before and after this script. MySQL rebuilds the table to move a
--     column, so run it at a quiet moment on a large table.
--   * If the table's primary key is on a different column, the script STOPS without changing
--     anything - check the table by hand in that case.
-- =============================================================================================

-- ---- Step 1: primary key -------------------------------------------------------------------
-- pk_cols = the primary key's columns, e.g. 'request_id' (NULL when the table has no primary key).
SET @pk_cols := (SELECT GROUP_CONCAT(column_name ORDER BY seq_in_index)
                 FROM information_schema.statistics
                 WHERE table_schema = DATABASE() AND table_name = 'report_request_metadata'
                   AND index_name = 'PRIMARY');

-- A primary key on other columns: stop instead of guessing.
SET @sql := IF(@pk_cols IS NOT NULL AND @pk_cols <> 'request_id',
               'SELECT * FROM `MIGRATION_STOPPED_report_request_metadata_primary_key_is_not_request_id`',
               'SELECT ''step 1: primary key ok'' AS info');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- No primary key at all: add it. This fails (and changes nothing) if request_id has duplicates
-- or NULLs; find them with
--   SELECT request_id, COUNT(*) FROM report_request_metadata GROUP BY request_id HAVING COUNT(*) > 1;
SET @sql := IF(@pk_cols IS NULL,
               'ALTER TABLE report_request_metadata ADD PRIMARY KEY (request_id)',
               'SELECT ''step 1: nothing to add'' AS info');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- Step 2: move request_id to the first position -----------------------------------------
SET @position := (SELECT ordinal_position FROM information_schema.columns
                  WHERE table_schema = DATABASE() AND table_name = 'report_request_metadata'
                    AND column_name = 'request_id');

SET @sql := IF(@position > 1,
               'ALTER TABLE report_request_metadata MODIFY COLUMN request_id VARCHAR(64) NOT NULL FIRST',
               'SELECT ''step 2: request_id is already first'' AS info');
PREPARE stmt FROM @sql; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ---- Step 3: check the result ---------------------------------------------------------------
-- Expect request_id first, with Key = PRI, and the same row count as before.
SHOW COLUMNS FROM report_request_metadata;
SELECT COUNT(*) AS rows_in_table FROM report_request_metadata;
