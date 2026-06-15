"""Proxy leasing.

The leased egress is what the transport binds AND what any captcha solver uses — so a
risk-scored token is minted from the same IP that will submit it. Ships Null + Static
providers; for a commercial vendor, subclass `ProxyProvider` (see docs/proxy.md).
"""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyLease:
    url: str | None  # e.g. "http://user:pass@host:port", or None for direct


class ProxyProvider:
    def lease(self, key: str | None = None) -> ProxyLease:
        raise NotImplementedError

    def release(self, lease: ProxyLease) -> None:  # noqa: B027 — optional hook
        pass


class NullProxyProvider(ProxyProvider):
    """Direct egress (no proxy)."""

    def lease(self, key: str | None = None) -> ProxyLease:
        return ProxyLease(url=None)


class StaticProxyProvider(ProxyProvider):
    """Round-robin a fixed list (arg, or CRAWLERKIT_PROXIES env, comma-separated)."""

    def __init__(self, proxies: list[str] | None = None):
        self._proxies = proxies or [
            p.strip() for p in os.environ.get("CRAWLERKIT_PROXIES", "").split(",") if p.strip()
        ]
        self._i = 0

    def lease(self, key: str | None = None) -> ProxyLease:
        if not self._proxies:
            return ProxyLease(url=None)
        url = self._proxies[self._i % len(self._proxies)]
        self._i += 1
        return ProxyLease(url=url)
