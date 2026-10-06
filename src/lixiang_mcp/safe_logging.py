"""Allowlist logging: no payload, token, identifier, coordinate, exception or traceback logs."""

import logging

EVENTS = frozenset({"operation_confirmed", "operation_failed", "operation_unknown"})


class SafeLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name.startswith("lixiang_mcp")
            and isinstance(record.msg, str)
            and record.msg in EVENTS
        ):
            record.msg = str(record.msg)
        else:
            record.msg = "external_event_redacted"
        record.args = ()
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(SafeLogFilter())
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
