"""Block detector: status codes and challenge-page markers map to the right error class."""

from types import SimpleNamespace

import pytest

from crawlerkit.core.errors import (
    BlockedError,
    CrawlerError,
    CrawlerKitError,
    PermanentError,
    TransientError,
    raise_for_block,
)


def _resp(status: int = 200, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(status_code=status, text=text)


@pytest.mark.parametrize("status", [403, 429])
def test_block_status_raises_blocked(status: int) -> None:
    with pytest.raises(BlockedError):
        raise_for_block(_resp(status=status))


def test_challenge_marker_raises_blocked() -> None:
    with pytest.raises(BlockedError):
        raise_for_block(_resp(status=200, text="<title>Just a moment...</title>"))


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_error_raises_transient(status: int) -> None:
    with pytest.raises(TransientError):
        raise_for_block(_resp(status=status))


def test_clean_response_does_not_raise() -> None:
    assert raise_for_block(_resp(status=200, text="<html>ok</html>")) is None


@pytest.mark.parametrize("exc_type", [CrawlerError, TransientError, BlockedError, PermanentError])
def test_crawl_errors_are_catchable_as_crawler_kit_error(exc_type: type) -> None:
    assert issubclass(exc_type, CrawlerKitError)
