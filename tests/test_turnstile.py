"""Offline tests for the browserless Turnstile solver foundations.

No network, no native engine, no live Cloudflare. These cover the gate-independent pure-Python
pieces: the widget parser, the deterministic fingerprint deriver, the `turnstile_hint` contract,
and `TurnstileSolver.solve()`'s orchestration + typed failure paths (which bottom out at
`ChallengeEngineError` until the native V8 module is built).
"""

import pytest

from crawlerkit.core.captcha import (
    Challenge,
    ChallengeEngineError,
    InteractiveChallengeError,
    Solved,
    TurnstileSolver,
    turnstile_hint,
)
from crawlerkit.core.captcha._turnstile import fingerprint
from crawlerkit.core.captcha._turnstile.widget import parse_widget
from crawlerkit.core.identity import Profile

_WIDGET_HTML = """
<html><body>
  <form>
    <div class="cf-turnstile"
         data-sitekey="0x4AAAAAADpvM_lNoEdBJ3cR"
         data-action="login"
         data-cdata="sessionABC"
         data-callback="onSolved"></div>
  </form>
  <script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script>
</body></html>
"""

_INTERSTITIAL_HTML = """
<html><head><script>window._cf_chl_opt = {"cType":"managed","cRay":"abc123"};</script></head>
<body><div class="cf-turnstile" data-sitekey="0x4AAAAAADpvM_lNoEdBJ3cR"></div></body></html>
"""


class _FakeTransport:
    """Minimal transport stand-in: solve() only reads `.profile` and `.timeout` before the
    engine boundary (where the missing native ext stops us)."""

    def __init__(self, profile: Profile):
        self.profile = profile
        self.timeout = 30.0


def _profile() -> Profile:
    return Profile(
        impersonate="chrome131",
        _headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "sec-ch-ua-platform": '"Windows"',
        },
    )


# ---- widget parser ----

def test_parse_widget_implicit_embed() -> None:
    w = parse_widget(_WIDGET_HTML)
    assert w.sitekey == "0x4AAAAAADpvM_lNoEdBJ3cR"
    assert w.action == "login"
    assert w.cdata == "sessionABC"
    assert w.callback == "onSolved"
    assert w.interactive is False


def test_parse_widget_explicit_overrides_scrape() -> None:
    w = parse_widget(_WIDGET_HTML, sitekey="OVERRIDE", action="signup")
    assert w.sitekey == "OVERRIDE"
    assert w.action == "signup"


def test_parse_widget_detects_interactive_interstitial() -> None:
    w = parse_widget(_INTERSTITIAL_HTML)
    assert w.interactive is True
    assert w.pagedata and "managed" in w.pagedata


# ---- fingerprint deriver ----

def test_fingerprint_is_deterministic_per_profile() -> None:
    p = _profile()
    a, b = fingerprint.derive(p), fingerprint.derive(p)
    assert a == b


def test_fingerprint_agrees_with_profile() -> None:
    fp = fingerprint.derive(_profile())
    assert fp.os_family == "windows"
    assert fp.platform == "Win32"
    assert fp.languages == ["pt-BR", "pt", "en"]
    assert fp.language == "pt-BR"
    assert fp.timezone == "America/Sao_Paulo"
    assert "NVIDIA" in fp.webgl_renderer  # windows webgl renderer
    assert fp.avail_height < fp.screen_height  # taskbar gap is a real, checked signal
    assert len(fp.canvas_hash) == 64


def test_fingerprint_linux_profile() -> None:
    p = Profile(
        impersonate="chrome131",
        _headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    fp = fingerprint.derive(p)
    assert fp.os_family == "linux"
    assert fp.platform == "Linux x86_64"
    assert fp.language == "en-US"


# ---- turnstile_hint contract ----

def test_turnstile_hint_carries_full_context() -> None:
    ch = turnstile_hint("https://gov.br/login", _WIDGET_HTML, sitekey="0xABC", action="login")
    assert ch.kind == "turnstile"
    assert ch.params["page_url"] == "https://gov.br/login"
    assert ch.params["html"] == _WIDGET_HTML
    assert ch.params["sitekey"] == "0xABC"
    # carries every field (even ones v1 ignores) so the contract doesn't churn
    assert set(ch.params) == {"page_url", "html", "sitekey", "action", "cdata", "pagedata"}


# ---- solve() orchestration + typed failures ----

def test_solve_requires_page_url_and_html() -> None:
    with pytest.raises(ChallengeEngineError) as exc:
        TurnstileSolver().solve(Challenge(kind="turnstile", params={"sitekey": "0xABC"}), transport=None)
    assert exc.value.sitekey == "0xABC"


def test_solve_interactive_interstitial_raises_typed() -> None:
    ch = turnstile_hint("https://gov.br/login", _INTERSTITIAL_HTML)
    with pytest.raises(InteractiveChallengeError) as exc:
        TurnstileSolver().solve(ch, transport=_FakeTransport(_profile()))
    assert exc.value.page_url == "https://gov.br/login"


def test_solve_passive_widget_reaches_engine_boundary() -> None:
    # A valid passive widget flows through parse -> fingerprint -> engine; with no native ext
    # built it fails typed at the engine boundary (never silently, never a fake token).
    ch = turnstile_hint("https://gov.br/login", _WIDGET_HTML)
    with pytest.raises(ChallengeEngineError):
        TurnstileSolver().solve(ch, transport=_FakeTransport(_profile()))


def test_solved_shape() -> None:
    # guard the public return type the solver promises
    s = Solved(token="t", expires_at=123.0)
    assert s.token == "t" and s.expires_at == 123.0
