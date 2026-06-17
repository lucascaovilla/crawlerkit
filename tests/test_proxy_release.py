"""ProxyProvider.release() is a documented extension hook — confirm it actually fires."""

import pytest

from crawlerkit.core import BaseCrawler, RawResponse
from crawlerkit.core.errors import BlockedError
from crawlerkit.core.proxy import ProxyLease, ProxyProvider


class _RecordingProvider(ProxyProvider):
    def __init__(self) -> None:
        self.leased: list[ProxyLease] = []
        self.released: list[ProxyLease] = []

    def lease(self, key: str | None = None) -> ProxyLease:
        lease = ProxyLease(url=f"http://proxy-{len(self.leased)}")
        self.leased.append(lease)
        return lease

    def release(self, lease: ProxyLease) -> None:
        self.released.append(lease)


class _AlwaysBlocked(BaseCrawler):
    def flow(self, params: dict) -> RawResponse:
        raise BlockedError("simulated block")


def test_release_called_on_each_rotation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("crawlerkit.core.base_crawler.time.sleep", lambda *_: None)
    provider = _RecordingProvider()
    crawler = _AlwaysBlocked(proxy_provider=provider, max_attempts=3)
    first_lease = provider.leased[0]

    with pytest.raises(BlockedError):
        crawler.run({})

    # 3 attempts -> 2 rotations -> 2 releases (the final lease is never rotated away mid-run)
    assert len(provider.released) == 2
    assert provider.released[0] is first_lease
    assert provider.released[1] is provider.leased[1]


def test_close_releases_the_current_lease() -> None:
    provider = _RecordingProvider()
    crawler = _AlwaysBlocked(proxy_provider=provider)
    current = crawler.proxy

    crawler.close()

    assert provider.released == [current]


def test_context_manager_releases_on_exit() -> None:
    provider = _RecordingProvider()
    with _AlwaysBlocked(proxy_provider=provider) as crawler:
        current = crawler.proxy

    assert provider.released == [current]
