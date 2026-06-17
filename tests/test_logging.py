"""Logging is opt-in: silent by default, emits only when enable_logs=True."""

from structlog.testing import capture_logs

from crawlerkit.core import BaseCrawler, BaseParser, RawResponse
from crawlerkit.core._logging import _null, get_logger


class _Dummy(BaseCrawler):
    def flow(self, params: dict) -> RawResponse:
        return RawResponse(url="https://example.test", status=200, text="ok")


class _LoudCrawler(_Dummy):
    enable_logs = True


class _Parser(BaseParser[dict]):
    render_pdf_enabled = False  # no WeasyPrint; keep it offline

    def parse(self, raw: RawResponse) -> list[dict]:
        return [{"ok": True}]


class _LoudParser(_Parser):
    enable_logs = True


def test_get_logger_selects_null_or_real() -> None:
    assert get_logger(False) is _null
    assert get_logger(False).info("dropped") is None  # no-op swallows the call
    assert get_logger(True) is not _null


def test_crawler_silent_by_default() -> None:
    crawler = _Dummy()
    assert crawler.enable_logs is False
    with capture_logs() as logs:
        crawler.run({})
    assert logs == []


def test_crawler_logs_when_enabled() -> None:
    crawler = _LoudCrawler()
    with capture_logs() as logs:
        crawler.run({})
    events = [e["event"] for e in logs]
    assert "crawl_start" in events
    assert "crawl_done" in events


def test_parser_silent_by_default() -> None:
    raw = RawResponse(url="u", status=200, text="<html></html>")
    with capture_logs() as logs:
        _Parser().run(raw)
    assert logs == []


def test_parser_logs_when_enabled() -> None:
    raw = RawResponse(url="u", status=200, text="<html></html>")
    with capture_logs() as logs:
        _LoudParser().run(raw)
    assert any(e["event"] == "parse_done" for e in logs)
