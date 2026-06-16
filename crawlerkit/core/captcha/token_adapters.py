"""OPTIONAL token-captcha adapters: reCAPTCHA v2/v3 + hCaptcha via a third-party token provider.

Browserless (POST site_key + url -> token; no DOM). These reintroduce a paid third party, so they
are OPT-IN — NOT registered in `default_registry()`. Wire them only when configured:

    reg.register(RecaptchaV2Solver(provider)).register(HcaptchaSolver(provider))

`provider` is any object implementing `TokenProvider` (e.g. an adapter around a paid
captcha-solving API such as AntiCaptcha or 2Captcha). `detect()` finds the sitekey; the crawler supplies params["url"]
(the page URL the provider needs) via a hint.
"""

import re
from typing import Protocol, runtime_checkable

from .base import CaptchaServiceError, Challenge, Solved


@runtime_checkable
class TokenProvider(Protocol):
    def solve_recaptcha_v2(self, site_key: str, url: str, **kw) -> str: ...
    def solve_recaptcha_v3(self, site_key: str, url: str, **kw) -> str: ...
    def solve_hcaptcha(self, site_key: str, url: str, **kw) -> str: ...


def _sitekey(text: str, marker: str) -> str | None:
    if marker not in (text or "").lower():
        return None
    m = re.search(r'(?:data-sitekey|sitekey|render)["\']?\s*[:=]\s*["\']?([0-9A-Za-z_-]{20,})', text or "")
    return m.group(1) if m else None


class _BaseTokenSolver:
    kind = ""
    _marker = ""

    def __init__(self, provider: TokenProvider):
        self._p = provider

    @classmethod
    def detect(cls, text: str):
        sk = _sitekey(text, cls._marker)
        return Challenge(kind=cls.kind, params={"sitekey": sk}) if sk else None

    def _require(self, challenge: Challenge) -> tuple[str, str]:
        sk = challenge.params.get("sitekey")
        url = challenge.params.get("url")  # supplied by the crawler (provider needs the page URL)
        if not sk or not url:
            raise CaptchaServiceError(f"{self.kind} needs params['sitekey'] and ['url']")
        return sk, url


class RecaptchaV2Solver(_BaseTokenSolver):
    kind = "recaptcha_v2"
    _marker = "recaptcha"

    def solve(self, challenge: Challenge, transport) -> Solved:
        sk, url = self._require(challenge)
        return Solved(token=self._p.solve_recaptcha_v2(sk, url))


class RecaptchaV3Solver(_BaseTokenSolver):
    kind = "recaptcha_v3"
    _marker = "recaptcha"

    def solve(self, challenge: Challenge, transport) -> Solved:
        sk, url = self._require(challenge)
        return Solved(token=self._p.solve_recaptcha_v3(sk, url, action=challenge.params.get("action")))


class HcaptchaSolver(_BaseTokenSolver):
    kind = "hcaptcha"
    _marker = "hcaptcha"

    def solve(self, challenge: Challenge, transport) -> Solved:
        sk, url = self._require(challenge)
        return Solved(token=self._p.solve_hcaptcha(sk, url))
