"""The request timeout has a configurable default (was hardcoded at 30s) with per-call override."""

from crawlerkit.core import BaseCrawler, RawResponse
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport


class _Dummy(BaseCrawler):
    def flow(self, params: dict) -> RawResponse:
        return RawResponse(url="https://example.test", status=200, text="ok")


def test_transport_defaults_to_30s() -> None:
    t = Transport(pick(), NullProxyProvider().lease())
    assert t.timeout == 30.0


def test_transport_timeout_is_configurable() -> None:
    t = Transport(pick(), NullProxyProvider().lease(), timeout=5)
    assert t.timeout == 5

    captured = {}

    def fake_request(method, url, **kw):
        captured.update(kw)
        return None

    t._session.request = fake_request
    t.request("GET", "https://example.test")
    assert captured["timeout"] == 5


def test_per_call_timeout_overrides_the_instance_default() -> None:
    t = Transport(pick(), NullProxyProvider().lease(), timeout=5)
    captured = {}

    def fake_request(method, url, **kw):
        captured.update(kw)
        return None

    t._session.request = fake_request
    t.request("GET", "https://example.test", timeout=60)
    assert captured["timeout"] == 60


def test_base_crawler_threads_timeout_into_its_transport() -> None:
    crawler = _Dummy(timeout=12)
    assert crawler.transport.timeout == 12
