"""Parse the Turnstile widget config out of page HTML.

Two embed shapes carry the config the challenge JS binds to:

  - implicit:  ``<div class="cf-turnstile" data-sitekey=".." data-action=".." data-cdata="..">``
  - explicit:  a ``turnstile.render(el, { sitekey: "..", action: "..", cData: ".." })`` call.

A managed/interactive *interstitial* (the full Cloudflare challenge page) is a different beast:
it ships a ``window._cf_chl_opt = {...}`` blob. We detect that and flag it ``interactive`` so the
solver can bail with a typed error instead of pretending a widget is present.

Pure parsing, no network — unit-testable against captured HTML.
"""

import re
from dataclasses import dataclass

# data-sitekey="..." / sitekey: "..." (shared shape with TurnstileSolver.detect)
_SITEKEY_RE = re.compile(r'(?:data-sitekey|sitekey)["\']?\s*[:=]\s*["\']([0-9A-Za-z_-]{8,})["\']')
_ACTION_RE = re.compile(r'(?:data-action|action)["\']?\s*[:=]\s*["\']([^"\']{1,64})["\']')
# cData carries an opaque customer payload; Cloudflare spells the attribute data-cdata, the JS cData.
_CDATA_RE = re.compile(r'(?:data-cdata|cData)["\']?\s*[:=]\s*["\']([^"\']{1,2048})["\']')
_CALLBACK_RE = re.compile(r'data-callback["\']?\s*[:=]\s*["\']([^"\']{1,128})["\']')

# Full managed-challenge interstitial page (not a normal embed) — an interactive solve.
_INTERSTITIAL_RE = re.compile(r"window\._cf_chl_opt\s*=", re.I)
_CF_CHL_OPT_RE = re.compile(r"window\._cf_chl_opt\s*=\s*(\{.*?\})\s*;", re.S)


@dataclass(frozen=True)
class Widget:
    sitekey: str | None
    action: str | None = None
    cdata: str | None = None
    callback: str | None = None
    pagedata: str | None = None
    interactive: bool = False  # True == full interstitial challenge page, not a widget embed


def parse_widget(
    html: str,
    *,
    sitekey: str | None = None,
    action: str | None = None,
    cdata: str | None = None,
    pagedata: str | None = None,
) -> Widget:
    """Extract the widget config from ``html``. Explicitly-passed values win over parsed ones,
    so a caller that already knows the sitekey/action can override the page scrape."""
    html = html or ""
    sk = sitekey or _first(_SITEKEY_RE, html)
    act = action or _first(_ACTION_RE, html)
    cd = cdata or _first(_CDATA_RE, html)
    cb = _first(_CALLBACK_RE, html)

    interactive = bool(_INTERSTITIAL_RE.search(html))
    pd = pagedata or _first(_CF_CHL_OPT_RE, html)

    return Widget(sitekey=sk, action=act, cdata=cd, callback=cb, pagedata=pd, interactive=interactive)


def _first(rx: re.Pattern, text: str) -> str | None:
    m = rx.search(text)
    return m.group(1) if m else None
