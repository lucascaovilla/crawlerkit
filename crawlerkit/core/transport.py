"""Fingerprinted HTTP transport — the only HTTP path.

A `curl_cffi` Session bound to one Profile (TLS/JA3 + UA + header order) + a proxy lease +
per-host verified CA bundle (with AIA repair). TLS/JA3 fingerprint is a property of THIS client,
so it is the foundation, not a plugin. `requests` is intentionally not used (giveaway fingerprint).
"""

import os
import random
import time
from urllib.parse import urlparse

import structlog
from curl_cffi import requests as cffi
from curl_cffi.requests import exceptions as _cffi_exc

from . import tls
from .errors import TransientError
from .identity import Profile
from .proxy import ProxyLease

log = structlog.get_logger(__name__)


class Transport:
    def __init__(self, profile: Profile, proxy: ProxyLease, *, verify: bool = True,
                 client_cert: str | None = None, min_interval: float | None = None,
                 timeout: float = 30.0):
        self.profile = profile
        self.proxy = proxy
        self.verify = verify
        self.client_cert = client_cert  # PEM (cert+key) for ICP-Brasil mutual TLS, or None
        self.timeout = timeout  # default per-request timeout; override per-call via get/post(timeout=...)
        # politeness: minimum seconds between requests (+ up to 25% jitter). 0/None = off.
        self.min_interval = float(
            min_interval if min_interval is not None else os.environ.get("CRAWLERKIT_MIN_INTERVAL", 0)
        )
        self._last = 0.0
        self._ca: dict[str, str] = {}
        self._session = cffi.Session(impersonate=profile.impersonate)
        self._session.headers.update(profile.headers())
        if proxy.url:
            self._session.proxies = {"http": proxy.url, "https": proxy.url}

    def _verify_for(self, url: str):
        if self.verify is False:
            return False
        host = urlparse(url).hostname or ""
        if host not in self._ca:
            self._ca[host] = tls.build_ca_bundle(host)
        return self._ca[host]

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        wait = self.min_interval - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, self.min_interval * 0.25))
        self._last = time.monotonic()

    def request(self, method: str, url: str, **kw):
        kw.setdefault("verify", self._verify_for(url))
        kw.setdefault("impersonate", self.profile.impersonate)
        kw.setdefault("timeout", self.timeout)
        if self.client_cert:
            kw.setdefault("cert", self.client_cert)
        self._throttle()
        log.debug("http", method=method, url=url, proxy=bool(self.proxy.url))
        try:
            return self._session.request(method, url, **kw)
        except _cffi_exc.RequestsError as e:  # network/curl failure -> transient (retryable)
            raise TransientError(f"{method} {url}: {e}") from e

    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw):
        return self.request("POST", url, **kw)
