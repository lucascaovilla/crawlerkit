"""BaseCrawler — the crawl stage. A new target fills one hook: flow()."""

import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import structlog
from bs4 import BeautifulSoup

from .captcha.base import CaptchaRegistry, Challenge, default_registry
from .errors import BlockedError, PermanentError, TransientError
from .identity import Profile, pick
from .proxy import NullProxyProvider, ProxyProvider
from .transport import Transport

log = structlog.get_logger(__name__)


@dataclass
class RawResponse:
    url: str
    status: int
    text: str
    headers: dict = field(default_factory=dict)


class BaseCrawler(ABC):
    """Owns transport+identity+proxy+captcha; subclass implements only flow().

    No business logic, no parsing here — crawl and return the raw response.
    """

    captcha_hint: Challenge | None = None  # known sitekey when the widget isn't inline

    def __init__(
        self,
        *,
        proxy_provider: ProxyProvider | None = None,
        registry: CaptchaRegistry | None = None,
        verify: bool = True,
        profile: Profile | None = None,
        client_cert: str | None = None,
        max_attempts: int = 3,
        timeout: float = 30.0,
    ):
        self._proxy_provider = proxy_provider or NullProxyProvider()
        self._verify = verify
        self._client_cert = client_cert
        self._fixed_profile = profile
        self.max_attempts = max_attempts
        self._timeout = timeout
        self.registry = registry or default_registry()
        self._build_transport()

    def _build_transport(self) -> None:
        """(Re)create identity + proxy lease + transport — on init and on each rotation."""
        self.profile = self._fixed_profile or pick()
        self.proxy = self._proxy_provider.lease()
        self.transport = Transport(
            self.profile, self.proxy, verify=self._verify, client_cert=self._client_cert,
            timeout=self._timeout,
        )

    def _rotate(self) -> None:
        log.info("rotate_identity_proxy")
        self._proxy_provider.release(self.proxy)
        self._build_transport()

    def close(self) -> None:
        """Release the current proxy lease. Call when done with this crawler (or use it as a
        context manager) so stateful proxy providers can clean up the final lease."""
        self._proxy_provider.release(self.proxy)

    def __enter__(self) -> "BaseCrawler":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- helpers exposed to flow() ---
    def get(self, url: str, **kw):
        return self.transport.get(url, **kw)

    def post(self, url: str, **kw):
        return self.transport.post(url, **kw)

    def solve_captcha(self, source) -> str | None:
        """detect+solve; returns a token, None (no challenge), or raises a CaptchaError
        (UnsupportedCaptcha / CaptchaServiceError / CaptchaNotImplementedError, etc.)."""
        solved = self.registry.solve(source, self.transport, hint=self.captcha_hint)
        return solved.token if solved else None

    def hidden_fields(self, html: str) -> dict:
        """All hidden inputs of the form (JSF ViewState / WebForms __VIEWSTATE postback state)."""
        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception:  # noqa: BLE001
            soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form") if soup else None
        scope = form or soup
        hidden: dict[str, str] = {}
        if scope:
            for inp in scope.find_all("input"):
                name = inp.get("name")
                if name and (inp.get("type") == "hidden" or "ViewState" in name or "VIEWSTATE" in name.upper()):
                    hidden[name] = inp.get("value", "")
        return hidden

    # --- the only required hook ---
    @abstractmethod
    def flow(self, params: dict) -> RawResponse:
        ...

    def run(self, params: dict) -> RawResponse:
        """Run flow() with retry + rotation. TransientError -> back off, retry (same identity);
        BlockedError -> rotate identity+proxy, then retry; PermanentError -> fail fast."""
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                log.info("crawl_start", crawler=type(self).__name__, attempt=attempt)
                raw = self.flow(params)
                log.info("crawl_done", status=raw.status, bytes=len(raw.text))
                return raw
            except PermanentError:
                raise
            except BlockedError as e:
                last = e
                log.warning("blocked", attempt=attempt, error=str(e))
                if attempt < self.max_attempts:
                    self._rotate()
                    self._backoff(attempt)
            except TransientError as e:
                last = e
                log.warning("transient", attempt=attempt, error=str(e))
                if attempt < self.max_attempts:
                    self._backoff(attempt)
        raise last or RuntimeError("crawl failed with no captured error")

    @staticmethod
    def _backoff(attempt: int, cap: float = 30.0) -> None:
        time.sleep(min(2.0**attempt + random.uniform(0, 1), cap))
