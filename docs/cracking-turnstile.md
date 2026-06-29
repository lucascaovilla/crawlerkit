# Cracking Cloudflare Turnstile (browserless)

`TurnstileSolver` is a **standalone core solver**, exactly like `McaptchaPowSolver` — it has no
dependency on the gov.br solver (the SSO solver may *optionally* delegate to it; see
[cracking-govbr.md](cracking-govbr.md)). It is **engine-backed**: `solve()` runs the challenge's
own JS in an embedded V8 engine with a faked browser environment derived from the active `Profile`,
routes the challenge's network calls back through `transport`, and captures the
`cf-turnstile-response` token. Until the native V8 module is built, `solve()` fails *loudly* and
*typed* at the engine boundary (`ChallengeEngineError`) — never silently, never with a fake token.

Files: `crawlerkit/core/captcha/turnstile.py` (public surface) + `crawlerkit/core/captcha/_turnstile/`
(rotation-prone guts).

## Why this isn't a formula
Turnstile mints a token by executing **obfuscated, fingerprint-bearing JavaScript** in the page —
not a replicable math operation (unlike mCaptcha's PoW). We do **not** reimplement Cloudflare's VM
(it is rotated, per-session bytecode). We run *their* JS, with no real browser anywhere.

## Architecture
- **Engine of record:** a vendored Rust + `deno_core`/V8 module compiled into the `crawlerkit-core`
  wheel as `crawlerkit._turnstile_engine` (via maturin + cibuildwheel). deno_core gives a real async
  event loop + an op layer — the natural home for the `fetch`/`XMLHttpRequest` shims that call back
  into `transport`. Imported lazily, so a missing/broken native ext only fails an actual solve.
- **Env emulation** (`_turnstile/env.py`, built from the phase-0 spike): `window`/`document`/
  `navigator`/`screen`/`crypto`/canvas/WebGL/timers, plus `document.cookie` backed by the transport
  cookie jar. Filled incrementally from what the live challenge actually reads — instrument
  undefined-property accesses, stub only what's touched, expect drift on rotation.
- **Fingerprint** (`_turnstile/fingerprint.py`): `Profile` is thin (UA + impersonate + headers), so
  the screen/platform/languages/hardwareConcurrency/timezone/canvas/WebGL values are **derived
  deterministically** from it (seed = UA + impersonate). Same `Profile` → same fingerprint; every
  value agrees with the UA/JA3 on the wire. A navigator↔UA or fingerprint↔TLS mismatch is exactly
  what managed Turnstile catches.
- **Network bridge** (`_turnstile/bridge.py`): every challenge request goes through the *same*
  `transport`, inheriting its JA3/HTTP2 fingerprint, proxy, and cookies. A split between the page
  fetch and the challenge requests is an instant flag.

## The contract — `turnstile_hint`
The token solve binds to the page **origin** and reads widget config out of the **HTML**, neither of
which a bare `Challenge` carries. Build the challenge with `turnstile_hint`, which packs the full
context (every field, even ones v1 ignores, so the contract doesn't churn):

```python
from crawlerkit.core.captcha import turnstile_hint
ch = turnstile_hint(page_url, html, sitekey=..., action=..., cdata=..., pagedata=...)
token = registry.solve(html, transport, hint=ch).token   # cf-turnstile-response
```

Because the widget is usually **inline**, `detect()` also fires and yields a sitekey-only challenge.
`CaptchaRegistry.solve` **merges** the two of the same kind: detect's freshly-scraped sitekey wins,
the hint's page_url + html survive. From a `BaseCrawler.flow()`, pass the dynamic hint per call:

```python
r = self.get(FORM_URL)
token = self.solve_captcha(r.text, hint=turnstile_hint(page_url=FORM_URL, html=r.text))
```

`solve()` then: validates page_url + html → parses the widget → bails with
`InteractiveChallengeError` if Cloudflare served a managed interstitial → derives the fingerprint →
runs the challenge in V8 → returns `Solved(token, expires_at=now+300)` (Turnstile tokens ~300s,
single use). Inject the token into the form field the page expects (usually `cf-turnstile-response`).

## Phase 0 gate (do this first)
Before any Rust work, prove the challenge runs to a token at all: `spike/turnstile_spike.py`
(throwaway, py_mini_racer/V8). Keep the learnings (the undefined-access list, the request sequence),
throw the code away. See `spike/README.md`.

## Wiring & testing
- In `default_registry()` under `kind="turnstile"` — standalone, no other solver involved.
- Offline (`tests/test_turnstile.py`, no network/engine): the widget parser, the deterministic
  fingerprint deriver, the `turnstile_hint` contract, the detect+hint merge, and `solve()`'s typed
  failure paths. Once a real session is captured, freeze it as a fixture + a replay-transport double
  and test the env/VM run offline so a Cloudflare rotation shows up in CI.
- Live (gated, `@pytest.mark.live` + `CRAWLERKIT_LIVE`): solve the live target's sitekey end to end
  through a real `Transport`; assert the token passes siteverify + the subsequent form POST succeeds.
  The integration target is the Detran PA crawler in `poc-infra-pa` (a JSF form that moved from
  mCaptcha to Turnstile).
- Typed failures, no log-string parsing: `InteractiveChallengeError` (escalated — caller falls back),
  `ChallengeEngineError` (engine missing/failed), `CaptchaTimeoutError` (no token in time), and
  `BlockedError`/`TransientError` for network blocks mid-solve so upstream retry/rotation reacts.

## Honest expectations
This breaks when Cloudflare rotates the challenge — expected, and why the env stubs + VM runner are
isolated in `_turnstile/`. A rotation should be a 1-2 file patch, not a rewrite; the public surface
(`TurnstileSolver.solve` + the registry entry) stays frozen. Prefer a clean identity/proxy: the
realistic win is the non-interactive/managed case a clean fingerprint passes without a human click.
