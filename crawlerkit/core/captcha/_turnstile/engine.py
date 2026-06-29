"""Run the Turnstile challenge JS in an embedded JS engine and harvest the token.

Engine of record: **pythonmonkey** (SpiderMonkey) — a normal pip dependency, imported LAZILY so a
missing/broken engine only fails an actual Turnstile solve, never an unrelated `import crawlerkit`.
It is kept behind this one module so the engine is swappable (deno_core/V8 later) without touching
`TurnstileSolver`. (Supersedes the earlier native-Rust/`crawlerkit._turnstile_engine` plan.)

`run_challenge` boots a faked browser env (`env.py`) seeded from the profile fingerprint, wires the
challenge's networking to the crawler transport (`bridge.py`), evals Cloudflare's `api.js`, lets the
widget auto-render, and pumps pythonmonkey's event loop until the Turnstile callback hands back a
`cf-turnstile-response` token or `timeout` elapses. The cross-origin challenge-platform iframe is
the rotation-prone part; what the challenge actually reads/requests is observed via the env's
`__undef` log + the bridge request list (surfaced on failure), never guessed.

pythonmonkey's async event loop is driven on CPython **3.11** (its timer binding segfaults on 3.14),
so `run_challenge` spins its own asyncio loop; `TurnstileSolver.solve` stays synchronous.
"""

import asyncio
import time

from ..base import CaptchaTimeoutError, ChallengeEngineError, InteractiveChallengeError
from . import bridge as _bridge
from . import env as _env
from . import frame as _frame

API_JS_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js"

# Detected post-hoc: the challenge escalated to a real interactive widget (visible checkbox/puzzle).
_INTERACTIVE_MARKERS = ("interactive", "managed-interactive", "/cdn-cgi/challenge-platform/h/")


def _load_pm():
    try:
        import pythonmonkey as pm
    except ImportError as e:  # engine not installed for this interpreter
        raise ChallengeEngineError(
            "pythonmonkey (the Turnstile JS engine) is not importable — install it into the active "
            "venv (`uv pip install pythonmonkey`) on CPython 3.11 (its async loop segfaults on 3.14)"
        ) from e
    return pm


def run_challenge(*, page_url: str, fingerprint, widget, transport, timeout: float = 30.0) -> str:
    """Run the challenge to a `cf-turnstile-response` token. Raises:

      - `ChallengeEngineError`      engine missing, or it could not run the challenge JS.
      - `InteractiveChallengeError` the challenge escalated to an interactive solve.
      - `CaptchaTimeoutError`       no token within ``timeout``.
    """
    pm = _load_pm()
    try:
        return asyncio.run(_drive(pm, page_url, fingerprint, widget, transport, timeout))
    except (ChallengeEngineError, InteractiveChallengeError, CaptchaTimeoutError):
        raise
    except Exception as e:  # anything the JS engine throws -> typed, with context
        raise ChallengeEngineError(
            f"Turnstile challenge JS failed to run: {type(e).__name__}: {e}",
            sitekey=getattr(widget, "sitekey", None), page_url=page_url,
        ) from e


async def _drive(pm, page_url, fingerprint, widget, transport, timeout) -> str:
    # 1. boot networking + env INSIDE the running loop (timers schedule onto this loop).
    bridge = _bridge.install(pm, transport)
    _env.install(pm, fingerprint=fingerprint, page_url=page_url, widget=widget, transport=transport)
    _frame.install(pm)

    # 1b. capture every iframe api.js creates. The token-minting iframe is the cross-origin
    #     `…/cdn-cgi/challenge-platform/…/turnstile/f/…`; the engine fetches + runs its VM (frame.py).
    pending: list[tuple] = []
    seen: set[str] = set()

    def _on_iframe_src(el, url):
        u = str(url)
        if "challenge-platform" in u and "/turnstile/f/" in u and u not in seen:
            seen.add(u)
            pending.append((el, u))

    pm.eval("(function (fn) { globalThis.__onIframeSrc = fn; })")(_on_iframe_src)

    # 2. fetch + eval Cloudflare's api.js (rides the transport, so same JA3/cookies as the page).
    resp = transport.get(API_JS_URL, headers={"Referer": page_url})
    if resp.status_code != 200 or "turnstile" not in (resp.text or "").lower():
        raise ChallengeEngineError(
            f"api.js fetch looks wrong (status={resp.status_code}, {len(resp.text or '')}B) — "
            "Cloudflare may have blocked the transport before the challenge even started",
            sitekey=getattr(widget, "sitekey", None), page_url=page_url,
        )
    pm.eval(resp.text)

    # 3. the widget renders IMPLICITLY: api.js (loaded without ?render=explicit) auto-renders every
    #    `.cf-turnstile` element when the document is ready (ours is `readyState:'complete'`), reading
    #    the widget config from the element's data-* attributes and the token callback from
    #    `data-callback` (env wires that to our token sink). So eval'ing api.js above already kicks the
    #    challenge off — no explicit `turnstile.render` (which would double-render and, with a null
    #    `action`, throw). Only force an explicit render if nothing auto-rendered.
    pm.eval(r"""
    (function () {
      try {
        if (globalThis.__hasWidget && globalThis.__hasWidget()) return;  // implicit render already ran
        if (!globalThis.turnstile || typeof turnstile.render !== 'function') return;
        var el = document.querySelector('.cf-turnstile');
        var opts = { sitekey: (globalThis.__WIDGET && __WIDGET.sitekey) || (el && el.getAttribute('data-sitekey')),
                     callback: globalThis.__turnstileToken };
        if (globalThis.__WIDGET && __WIDGET.action) opts.action = __WIDGET.action;
        if (globalThis.__WIDGET && __WIDGET.cdata) opts.cData = __WIDGET.cdata;
        turnstile.render(el, opts);
      } catch (e) { globalThis.__renderError = String(e); }
    })();
    """)

    # 4. pump the loop in bounded slices until a token appears or we run out of time.
    #    NOT `await pm.wait()`: that blocks until the JS loop is fully IDLE, but a live challenge
    #    keeps timers/fetches pending, so it would never hand control back to check the deadline.
    #    The challenge's timers/promises run on THIS asyncio loop during each `sleep`, so slicing
    #    lets us poll the token sink and enforce `timeout`. `pm.stop()` cancels leftovers on exit.
    try:
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            await asyncio.sleep(0.1)  # JS timers/promise jobs fire here
            while pending:  # a new challenge iframe was created -> fetch + run its VM
                el, url = pending.pop(0)
                _frame.load_frame(pm, el, url, transport=transport, page_url=page_url,
                                  log=getattr(bridge, "log", None))
            token = _read_token(pm)
            if token:
                return token
            _check_interactive(pm, widget, page_url, bridge)
        raise CaptchaTimeoutError(_timeout_detail(pm, widget, page_url, bridge, timeout))
    finally:
        try:
            pm.stop()
        except Exception:
            pass


def _read_token(pm):
    """The real token arrives only via api.js's own message handler, which validates the iframe VM's
    completion message and invokes the widget callback (`data-callback="__turnstileToken"`) -> `__token`.

    We deliberately do NOT trust the heuristic `__frameToken` (it false-matches the `nextRcV` nonce in
    the VM's early `init` message). Eval returns a guaranteed JS *string* ('' when absent): JS
    `null`/`undefined` map to pythonmonkey sentinels that are truthy in Python.
    """
    token = pm.eval("(function(){var t=globalThis.__token;return (typeof t==='string'&&t)?t:'';})()")
    token = str(token)
    return token or None


def _check_interactive(pm, widget, page_url, bridge) -> None:
    """If the challenge swapped to an interactive widget, bail with the typed error."""
    flagged = pm.eval("globalThis.__interactive === true")
    urls = " ".join(r["url"] for r in bridge.requests).lower()
    if flagged or any(m in urls for m in _INTERACTIVE_MARKERS[:2]):
        raise InteractiveChallengeError(
            "Turnstile escalated to an interactive challenge — out of scope for the browserless "
            "solver; route to a fallback",
            sitekey=getattr(widget, "sitekey", None), page_url=page_url,
        )


def _timeout_detail(pm, widget, page_url, bridge, timeout) -> str:
    """Build a debug-rich timeout message: the request sequence + the unstubbed accesses to fill."""
    try:
        undef = pm.eval("JSON.stringify((globalThis.__undef||[]).slice(0,60))")
    except Exception:
        undef = "[]"
    reqs = [f"{r['method']} {r['url']}" for r in bridge.requests]
    render_err = pm.eval("globalThis.__renderError || null")
    frame_err = pm.eval("globalThis.__frameErr || null")
    iframes = pm.eval("JSON.stringify((globalThis.__iframeSrcs||[]))")
    return (
        f"no cf-turnstile-response within {timeout}s (sitekey={getattr(widget, 'sitekey', None)}). "
        f"requests={reqs}. iframes={iframes}. renderError={render_err}. frameError={frame_err}. "
        f"unstubbed_accesses={undef}"
    )
