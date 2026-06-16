# Proxy

`crawlerkit.core.proxy` leases an egress URL to the transport. The **same lease is reused by captcha
solvers**, so a risk-scored token is minted from the IP that will submit it.

## Providers
```python
from crawlerkit.core.proxy import NullProxyProvider, StaticProxyProvider

NullProxyProvider()                       # direct egress
StaticProxyProvider()                     # round-robin CRAWLERKIT_PROXIES (or a list arg)
```
Pass one to a crawler: `MyCrawler(proxy_provider=StaticProxyProvider())`. Empty/absent credentials →
a direct (`url=None`) lease, so code paths stay uniform in dev.

For a commercial residential/datacenter proxy vendor, write a small `ProxyProvider` (see "Writing a
provider" below) — most vendors only differ in how the username string is built.

## Rotation
`BaseCrawler` re-leases the proxy (with a fresh identity) on a `BlockedError`. Implement health checks
/ ban-tracking by subclassing `ProxyProvider` and overriding `lease()`/`release()`. `release()` is
called automatically on rotation (the lease being replaced) and from `BaseCrawler.close()` (the final
lease) — override it to signal "done with this session" to a stateful vendor backend.

## Writing a provider
```python
from crawlerkit.core.proxy import ProxyProvider, ProxyLease

class MyProvider(ProxyProvider):
    def lease(self, key=None) -> ProxyLease:
        return ProxyLease(url="http://user:pass@host:port")
```

### Sticky sessions
Many vendors support pinning a sticky egress IP by folding a session id into the proxy username, so
retries for the same crawl item reuse the same IP. `lease(key=...)` is called with `key` set to
whatever your crawler passes (e.g. the crawl item), so you can seed the session id from it:

```python
class StickyProvider(ProxyProvider):
    """Generic sticky-session pattern — adapt the username format to your vendor's docs."""

    def __init__(self, user: str, password: str, host: str, port: str):
        self.user, self.password, self.host, self.port = user, password, host, port
        self._n = 0

    def lease(self, key: str | None = None) -> ProxyLease:
        self._n += 1
        session_id = key or f"s{self._n}"
        user = f"{self.user}-session-{session_id}"  # exact format varies by vendor/plan
        return ProxyLease(url=f"http://{user}:{self.password}@{self.host}:{self.port}")
```
