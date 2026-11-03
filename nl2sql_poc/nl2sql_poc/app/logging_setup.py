"""Backend logging: console + rotating file, set up once at startup from the `logging:` config.

Every line carries the chat session id and report request id of the work it belongs to
(`sid=` / `rid=`, "-" when unknown). Request handlers bind them with bind_session() /
bind_request(); a background thread must bind them itself (context does not follow threads).

Nothing secret is ever logged: no API keys, passwords, gateway secret or result rows.
"""
# ----------------------------------------------------------------------------------------------
# FILE OVERVIEW (for learners)
# Who calls it: chatbot_api.py calls configure_logging(cfg, BASE) once at startup, and calls
#   bind_session / bind_request / clear_context while handling each HTTP request.
# What it does: every module does `log = logging.getLogger(...)` and log.info(...). Those calls
#   only CREATE log records; this file decides WHERE they go (console + logs/backend_billing.log),
#   in what FORMAT, and at which LEVEL (config logging.level).
#
# Libraries in plain words:
#   * contextvars - a ContextVar is like a global variable, but each thread (and each async task)
#     sees its own value. So two users chatting at the same time each get their own session id
#     on their log lines, without passing the id into every function.
#   * RotatingFileHandler - writes log lines to a file; when the file reaches maxBytes it is
#     renamed to backend_billing.log.1 (older ones shift to .2, .3, ...) and a fresh file is
#     started. Only backupCount old files are kept, so the logs never fill the disk.
# ----------------------------------------------------------------------------------------------
from __future__ import annotations

import contextvars
import logging
import logging.handlers
import re
from pathlib import Path

# Layout of every log line, e.g.
#   2026-11-01 10:00:00,123 INFO    app.db [sid=abc rid=-] Database engine ready: ...
# %(levelname)-7s pads the level to 7 characters so the columns line up.
FORMAT = "%(asctime)s %(levelname)-7s %(name)s [sid=%(session_id)s rid=%(request_id)s] %(message)s"
# Our loggers: chatbot_api ("chatbot", "chatbot.*"), app/* ("app.*") and the script itself.
OWN_LOGGERS = ("chatbot", "app", "__main__", "chatbot_api")

# The current chat session id and report request id for whatever code is running right now.
_session = contextvars.ContextVar("log_session_id", default=None)
_request = contextvars.ContextVar("log_request_id", default=None)
# The visual polls these every second or two; successful polls are only logged at DEBUG.
# The regex matches Flask/werkzeug access-log lines like
#   "GET /session/<id>/progress HTTP/1.1" 200    or    "GET /health HTTP/1.1" 304
_POLL_LINE = re.compile(r'"GET /(?:session/[^/ ]+/(?:progress|transcript)|health)\b[^"]*" (?:200|304) ')


# Set / reset the ids that will appear as sid= and rid= on every following log line written by
# this thread. `or None` turns an empty string into None (shown as "-").
def bind_session(session_id) -> None:
    _session.set(session_id or None)


def bind_request(request_id) -> None:
    _request.set(request_id or None)


def clear_context() -> None:
    _session.set(None)
    _request.set(None)


# A logging Filter runs for every record before it is written. This one never blocks a record
# (returns True); it just copies the current ids onto it so FORMAT's %(session_id)s works.
class _ContextFilter(logging.Filter):
    def filter(self, record):
        record.session_id = _session.get() or "-"
        record.request_id = _request.get() or "-"
        return True


# Drops successful polling lines from Flask's access log unless the level is DEBUG
# (returning False from filter() means "do not write this record").
class _QuietPolls(logging.Filter):
    def __init__(self, level):
        super().__init__()
        self.level = level

    def filter(self, record):
        return self.level <= logging.DEBUG or not _POLL_LINE.search(record.getMessage())


# "debug" / "INFO" -> logging.DEBUG / logging.INFO. getLevelName returns a string for unknown
# names, in which case the default (INFO) is used.
def _level(value, default=logging.INFO) -> int:
    level = logging.getLevelName(str(value or "").strip().upper())
    return level if isinstance(level, int) else default


def configure_logging(cfg: dict | None, base_dir) -> Path | None:
    """Attach the console and file handlers. Safe to call again (replaces its own handlers).
    Returns the log file path, or None when file logging is off."""
    lcfg = (cfg or {}).get("logging") or {}
    level = _level(lcfg.get("level"))
    root = logging.getLogger()   # the root logger: every other logger passes records up to it
    # Remove handlers added by an earlier call (marked with _chatbot_handler below), so calling
    # this twice does not write every line twice.
    for handler in [h for h in root.handlers if getattr(h, "_chatbot_handler", False)]:
        root.removeHandler(handler)
        handler.close()

    handlers = [logging.StreamHandler()]   # StreamHandler = print to the console (stderr)
    log_path = None
    # File output only when config logging.file is set; the path is relative to the backend
    # folder (base_dir). max_mb is converted to bytes for RotatingFileHandler.
    if lcfg.get("file"):
        log_path = Path(base_dir, lcfg["file"])
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_path, maxBytes=int(float(lcfg.get("max_mb", 10)) * 1024 * 1024),
            backupCount=int(lcfg.get("backup_count", 5)), encoding="utf-8"))
    formatter = logging.Formatter(FORMAT)
    for handler in handlers:
        handler.setFormatter(formatter)
        handler.addFilter(_ContextFilter())
        handler._chatbot_handler = True
        root.addHandler(handler)

    # Third-party libraries stay at WARNING so DEBUG does not flood the file with HTTP internals.
    root.setLevel(logging.WARNING)
    for name in OWN_LOGGERS:
        logging.getLogger(name).setLevel(level)
    werkzeug = logging.getLogger("werkzeug")          # Flask's one-line-per-request access log
    werkzeug.setLevel(max(level, logging.INFO))
    for old in [f for f in werkzeug.filters if isinstance(f, _QuietPolls)]:
        werkzeug.removeFilter(old)
    werkzeug.addFilter(_QuietPolls(level))
    # Python `warnings.warn(...)` messages (e.g. deprecation notices) also go to the log.
    logging.captureWarnings(True)
    return log_path
