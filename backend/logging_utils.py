"""
Logging helpers shared by the API server (backend/main.py) and the desktop
sidecar (backend/desktop.py).

Credentials must never reach a log file. The browser can only pass the access
token of a WebSocket in the query string (/ws/market/AAPL?token=<JWT>), and
uvicorn logs that URL on every handshake ('... "WebSocket /ws/...?token=..."
[accepted]') and in its access log; market-data clients log request URLs that
can carry API keys (Finnhub: ?token=, NewsAPI: ?apiKey=). RedactTokens rewrites
such values to "[redacted]" before any handler writes the record.

This module must not import backend.config: the desktop launcher uses it before
the environment that backend.config reads has been prepared.
"""
import logging
import re
from collections.abc import Mapping
from typing import Iterable

REDACTED = "[redacted]"

# key=value pairs whose value is a credential: token=, access_token=, refresh_token=,
# id_token=, api_key=, apikey=, apiKey= (any key ending in "token" or "api[_]key").
_SECRET_PARAM = re.compile(
    r"((?<![A-Za-z0-9])[A-Za-z0-9_]*(?:token|api_?key)=)(?!\[redacted\])[^&\s\"'<>#]+",
    re.IGNORECASE,
)

# The loggers that carry request URLs: uvicorn's access log and its error log (the
# WebSocket handshake lines), and httpx (outbound requests, INFO level).
URL_LOGGERS = ("uvicorn.access", "uvicorn.error", "httpx")


def redact(text: str) -> str:
    """`text` with every credential query parameter value replaced by [redacted]."""
    return _SECRET_PARAM.sub(r"\1" + REDACTED, text)


def _redact_arg(value):
    return redact(value) if isinstance(value, str) else value


class RedactTokens(logging.Filter):
    """
    Redacts credential query parameters in log records.

    The record's structure is kept whenever possible: string arguments are
    redacted one by one, because some formatters unpack record.args themselves
    (uvicorn's AccessFormatter expects its five access-log arguments). Only if a
    credential is still visible afterwards (it came from a non-string argument such
    as an httpx.URL) is the record reduced to its redacted, pre-formatted message.
    Never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.msg, str):
                record.msg = redact(record.msg)
            args = record.args
            if isinstance(args, tuple):
                record.args = tuple(_redact_arg(a) for a in args)
            elif isinstance(args, Mapping):
                record.args = {k: _redact_arg(v) for k, v in args.items()}
            message = record.getMessage()
        except Exception:       # a broken format string is the formatter's problem
            return True
        if _SECRET_PARAM.search(message):
            record.msg, record.args = redact(message), None
        return True


def install_token_redaction(logger_names: Iterable[str] = URL_LOGGERS) -> RedactTokens:
    """
    Attach one RedactTokens filter to each named logger (idempotent). Logger
    filters see every record logged on that logger, whatever handlers a logging
    config (uvicorn's, gunicorn's, ...) later attaches, and logging.config never
    removes them.
    """
    shared = RedactTokens()
    for name in logger_names:
        target = logging.getLogger(name)
        if not any(isinstance(f, RedactTokens) for f in target.filters):
            target.addFilter(shared)
    return shared
