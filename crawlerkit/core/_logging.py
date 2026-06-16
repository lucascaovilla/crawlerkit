"""Opt-in logging.

crawlerkit-core stays silent unless asked: a `BaseCrawler`/`BaseParser` subclass sets
``enable_logs = True`` (or `Transport(..., enable_logs=True)`). When off — the default — every
log call is swallowed by a no-op logger, so an unconfigured structlog never prints to stdout.
"""

import structlog

_logger = structlog.get_logger("crawlerkit.core")


def _noop(*args, **kwargs) -> None:
    return None


class _NullLogger:
    """Swallows every ``.info()``/``.warning()``/``.debug()``/... call. Returned when disabled."""

    def __getattr__(self, _name):
        return _noop


_null = _NullLogger()


def get_logger(enabled: bool):
    """The real structlog logger when ``enabled``, else a no-op that drops every call."""
    return _logger if enabled else _null
