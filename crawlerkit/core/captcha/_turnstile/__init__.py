"""Private internals for the browserless Cloudflare Turnstile solver.

The public surface (`TurnstileSolver`, `turnstile_hint`) lives in
`crawlerkit/core/captcha/turnstile.py` and stays frozen. These modules carry the
rotation-prone guts so a Cloudflare challenge rotation is a 1-2 file patch:

  - `widget`      parse the Turnstile embed (sitekey/action/cData) + interstitial pagedata.
  - `fingerprint` derive a stable, profile-consistent browser fingerprint from a `Profile`.
  - `env`         faked browser globals (window/document/navigator/screen/crypto/canvas/WebGL,
                  document.cookie<->jar, the cf-turnstile element + token sink), seeded from
                  `fingerprint` and instrumented (logs every unstubbed access to `__undef`).
  - `bridge`      `fetch`/`XMLHttpRequest`/`sendBeacon` shims that route the challenge's requests
                  through the crawler `Transport` (same JA3/HTTP2/proxy/cookies as the page).
  - `engine`      boots pythonmonkey (SpiderMonkey), installs env+bridge, evals Cloudflare's
                  api.js, and pumps the event loop until the token appears.

The env is filled from what the instrumentation run reports the live challenge actually reads —
never guessed. The cross-origin challenge-platform iframe is the rotation-prone part.
"""
