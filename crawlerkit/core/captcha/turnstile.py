"""Cloudflare Turnstile — BROWSERLESS solver.

`detect()` finds the widget + sitekey in page HTML. `solve()` runs the challenge's own JS in an
embedded V8 engine with a faked browser environment derived from the active `Profile`, routes the
challenge's network calls back through the same `transport` (so JA3/HTTP2/proxy/cookies match the
page fetch), and captures the `cf-turnstile-response` token. Interactive escalation is NOT faked —
it raises `InteractiveChallengeError` so the caller can fall back. The rotation-prone guts live in
`_turnstile/` (engine/env/bridge/fingerprint/widget); this module is the frozen public surface.
"""

import re
import time

from ._turnstile import engine, fingerprint
from ._turnstile.widget import parse_widget
from .base import Challenge, ChallengeEngineError, InteractiveChallengeError, Solved

_SIGNATURE = re.compile(r"challenges\.cloudflare\.com/turnstile|cf-turnstile|turnstile\.render", re.I)
_SITEKEY_RE = re.compile(r'(?:data-sitekey|sitekey)["\']?\s*[:=]\s*["\']([0-9A-Za-z_-]{8,})["\']')

_TOKEN_TTL = 300  # Turnstile tokens are single-use and live ~300s


def turnstile_hint(
    page_url: str,
    html: str,
    *,
    sitekey: str | None = None,
    action: str | None = None,
    cdata: str | None = None,
    pagedata: str | None = None,
) -> Challenge:
    """Build a Turnstile `Challenge` carrying the full solve context.

    Mirrors `mcaptcha_hint`, but the token solve binds to the page origin and reads widget config
    out of the HTML, so it needs the page URL + raw HTML — `Challenge` carries neither on its own.
    Pass everything available (even fields v1 ignores) so the contract doesn't churn as the solver
    grows. Unspecified widget fields are scraped from `html`."""
    return Challenge(
        kind="turnstile",
        params={
            "page_url": page_url,
            "html": html,
            "sitekey": sitekey,
            "action": action,
            "cdata": cdata,
            "pagedata": pagedata,
        },
    )


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
        p = challenge.params
        page_url = p.get("page_url")
        html = p.get("html")
        sitekey = p.get("sitekey")

        # The token binds to the page origin and the widget config lives in the HTML, so both are
        # required. `detect()` alone only yields a sitekey — callers must use `turnstile_hint`.
        if not page_url or not html:
            raise ChallengeEngineError(
                "Turnstile solve needs page_url + html (build the Challenge with turnstile_hint); "
                f"got sitekey={sitekey!r} page_url={page_url!r} html={'set' if html else 'missing'}",
                sitekey=sitekey,
                page_url=page_url,
            )

        widget = parse_widget(
            html, sitekey=sitekey, action=p.get("action"), cdata=p.get("cdata"),
            pagedata=p.get("pagedata"),
        )
        if widget.interactive:
            raise InteractiveChallengeError(
                "Cloudflare served a managed interactive challenge interstitial (not a passive "
                "widget) — out of scope for the browserless solver; route to a fallback",
                sitekey=widget.sitekey or sitekey,
                page_url=page_url,
            )

        fp = fingerprint.derive(transport.profile)
        token = engine.run_challenge(
            page_url=page_url, fingerprint=fp, widget=widget, transport=transport,
            timeout=getattr(transport, "timeout", 30.0),
        )
        return Solved(token=token, expires_at=time.time() + _TOKEN_TTL)
