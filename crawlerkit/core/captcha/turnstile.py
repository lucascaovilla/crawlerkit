"""Cloudflare Turnstile — BROWSERLESS solver scaffold.

`detect()` works today (finds the widget + sitekey). `solve()` is a TODO for a manual,
browserless crack — it fails loudly (NotImplementedError), never silently.
"""

import re

from .base import CaptchaNotImplementedError, Challenge, Solved

_SIGNATURE = re.compile(r"challenges\.cloudflare\.com/turnstile|cf-turnstile|turnstile\.render", re.I)
_SITEKEY_RE = re.compile(r'(?:data-sitekey|sitekey)["\']?\s*[:=]\s*["\']([0-9A-Za-z_-]{8,})["\']')


class TurnstileSolver:
    kind = "turnstile"

    @classmethod
    def detect(cls, text: str):
        text = text or ""
        if not _SIGNATURE.search(text):
            return None
        m = _SITEKEY_RE.search(text)
        return Challenge(kind=cls.kind, params={"sitekey": m.group(1) if m else None})

    def solve(self, challenge: Challenge, transport) -> Solved:
        # TODO(crawlerkit): implement the BROWSERLESS Cloudflare Turnstile solve.
        # Turnstile mints a `cf-turnstile-response` token by running obfuscated,
        # fingerprint-bearing widget JS. Browserless approach to fill in here:
        #   1. First try the PASSIVE path — a clean impersonated identity (the active Profile +
        #      proxy IP) often receives a token with no interactive challenge. Attempt that first.
        #   2. Otherwise fetch the widget bundle (turnstile/v0/api.js + the challenge for
        #      params["sitekey"]) and execute its JS in a JS runtime (QuickJS via py-mini-racer,
        #      or a Node subprocess) with a minimal DOM/navigator shim seeded from the active
        #      Profile (UA, sec-ch-ua, screen, languages) and the leased proxy IP.
        #   3. return Solved(token=<cf-turnstile-response>, expires_at=now+~300).
        raise CaptchaNotImplementedError(
            f"browserless Turnstile solve is a TODO (params={challenge.params!r}) "
            "— implement the passive/JS-runtime crack"
        )
