# Step 1 - database + backend (session end, feedback, metadata, view names, identity)

The Power BI visual is NOT part of this step. Your current visual keeps working with this backend
(its "New chat" button now ends the old session instead of deleting it). The End button and the
Satisfied / Unsatisfied buttons arrive in Step 2.

## 1. How it works (what I found, what I changed)
* Visual -> `POST /chat` -> `ChatService` (chatbot_api.py) -> `ConversationEngine` (plans the
  message) -> `Pipeline` (SQL) -> `ReportStore` (MySQL) -> `PowerBIService` (page).
* Sessions live in memory (`app/session.py`); the visual stores only its session id and restores
  the transcript from the server. That part is unchanged.

## 2. report_request_metadata: one row per report request
Columns (in order): session_id, request_id (PK), user_id, user_role, platform, view_name,
request_status, sql_source, response_time_ms, customer_satisfaction, request_ts, error_message, day_id.
Chat messages are NOT stored here (they stay in `chat_conversation_log`). The full question and
SQL stay in the existing `report_views` registry.

| request_status | meaning |
|---|---|
| `initiated` | row created when the request is recognised; may still be clarifying or waiting for the chart choice |
| `in_progress` | SQL generation started / view being created |
| `completed` | view created AND the Power BI page generated (or page generation switched off) |
| `failed` | anything went wrong, including "query returned no rows" and "session ended before the report was finished"; `error_message` says why |

* One row per request: inserted once, then only UPDATEd. Retries, clarification turns, the chart
  question and feedback never insert another row. A chart change is a new page, so it is its own
  request (`sql_source = 'chart_change'`, same `view_name`).
* `response_time_ms` = processing time of the whole request: the arrival-to-response time of each
  of its turns added up (the user's thinking time is NOT counted). The background Power BI
  reload/refresh that runs after the reply is not included.
* `request_ts` is UTC. `day_id` is the date of `request_ts` in `app.timezone` (config), also used
  in the view name, so both always agree.
* Existing table: run `migrations/001_report_request_metadata_v2.sql` (non-destructive, see below).

## 3. View names
`vw_<description>_<YYYYMMDD>_<session>` e.g. `vw_refunds_by_reason_20261027_f5ba1970c8cf`.
Description = the report title, lowercased, only `a-z 0-9 _`, shortened first when needed (MySQL
limit 64). Session = first 12 letters/digits of the session id. If the name is already used (this
session, the database, or a race at CREATE VIEW) a request-derived suffix is added:
`..._f5ba1970c8cf_ab12`. The same final name is used for CREATE VIEW, `report_views`, the metadata
row, the Power BI table and the reply. Names are validated (`^vw_[a-z0-9_]+$`, <= 64) before use.

## 4. Sessions, feedback, identity (backend part)
* `POST /session/<id>/end` ends a session: history and records are kept; an open request is closed
  as `failed`; further `/chat` or `/feedback` calls get `409 {"session_ended": true}`. If a request
  is still running, the session ends right after it finishes (answer `202 {"pending": true}`).
  An ended session is never returned by `/session/latest`. Nothing ends a session except
  this call, "Finish session", or the old New chat (`/session/<id>/reset`).
* `POST /feedback` saves TRUE/FALSE on that request's row (NULL until rated). Last click wins; the
  same value again changes nothing; a report of another session cannot be rated.
* Identity: only a trusted gateway can set user_id / user_role (config `identity.provider:
  trusted_proxy` + a shared secret). Names sent by the browser are ignored. Default = NULL.

## 5. API changes
| Route | Change |
|---|---|
| `POST /chat` | new `action: "feedback_followup"` with `choice: "return"\|"finish"`; optional `platform: "powerbi"`; ended session -> 409 |
| `POST /feedback` | NEW. body `{session_id, request_id, satisfaction: "satisfied"\|"unsatisfied"}`. 200 `{status: "ok"\|"unchanged", customer_satisfaction, changed, message, messages[], updated[]}`; 400 bad input; 403 other user's session; 404 unknown session/request; 409 ended; 500 `{message}` (no internals) |
| `POST /session/<id>/end` | NEW. body `{reason?}`. 200 `{ended: true}`; 202 `{pending: true}`; 403; 404 |
| `POST /session/<id>/reset` | now ends the session (never deletes); response `{ok, deleted: false, ended}` |
| `GET /session/<id>/transcript`, `/progress` | add `ended` (and `end_requested`) |
| `GET /requests/<id>`, `/audit/requests/<id>` | return the new 13 columns |
| `POST /complete-report` | now 404 unless `chat.allow_mock_sql` is true (it is a test hook) |
Error texts sent to the visual no longer contain exception details; they go to the backend log.

## 6. Install (PowerShell, in the nl2sql_poc folder that holds chatbot_api.py)
1. Stop the backend (Ctrl+C). Back up: `mysqldump -u root -p billing > billing_backup.sql`
2. Edit `@app_utc_offset` at the top of the migration if your time zone is not +05:30.
3. `mysql -u root -p billing < migrations\001_report_request_metadata_v2.sql`
   (the old table is kept as `report_request_metadata_legacy_20261027`)
4. Copy the files from this zip over your project (see the file list in the answer).
5. `pip install -r requirements.txt`   (adds `tzdata`, needed for time zones on Windows)
6. Check `config_billing.yaml`: `app.timezone` (set to your/ the client's zone).
7. `python chatbot_api.py`          Tests: `python -m pytest -q`

## 7. Not verified here (needs your environment)
* The migration and the new SQL against a REAL MySQL (checked only mechanically and with a fake driver).
* Power BI Desktop (nothing in this step changes the visual or the report files).
## 8. Assumptions and limits
* No trusted user identity exists in Power BI custom visuals without extra work (Microsoft Entra ID
  token validation is NOT implemented). Until a gateway or that integration exists, user_id and
  user_role are NULL. Tableau is not supported; `platform` accepts only "Power BI".
* Sessions are in memory: after a backend restart or 12 h of inactivity the live session is gone
  (the audit tables remain). Requests left waiting when Power BI is just closed stay `initiated`.
* Session ids are not secret: GET transcript/progress are not identity-checked.
