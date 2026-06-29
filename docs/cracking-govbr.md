# Cracking gov.br SSO (browserless)

`GovBrSolver` (`crawlerkit/core/captcha/govbr.py`) is a **scaffold**: `detect()` works, `solve()`
raises `CaptchaNotImplementedError`. It is a first-class registry slot (`kind="govbr"`) so the
pipeline recognizes the challenge and fails *loudly* — you fill in the browserless crack.

This is the federal SSO at `sso.acesso.gov.br`, a separate concern from Cloudflare Turnstile. The
two are **not** coupled: `GovBrSolver` may *optionally* delegate to `TurnstileSolver` when a gov.br
page embeds a Turnstile, but the dependency is one-way (govbr → turnstile, never the reverse). See
[cracking-turnstile.md](cracking-turnstile.md).

## Implementing `GovBrSolver.solve()`
gov.br SSO is JS-heavy and gated by a captcha (hCaptcha/reCAPTCHA) + fingerprint checks. In `solve()`:

1. **Drive the SSO sequence** with `transport`, carrying cookies across redirects (login → authorize →
   callback). Keep the same identity+proxy throughout.
2. **Solve the embedded captcha** — reuse the registry: if gov.br embeds hCaptcha/reCAPTCHA, delegate to
   an (opt-in) token adapter; if it embeds Turnstile, delegate to `TurnstileSolver`; or JS-runtime-crack
   the gov.br challenge script.
3. **Complete the OAuth/SSO redirect**; return `Solved(token=<session cookie / SSO assertion>)` (or set
   the session cookies on `transport._session` and return the marker the crawler needs).
4. **Client certs:** some gov.br services accept ICP-Brasil mutual TLS — load a `.pfx` via
   `crawlerkit.core.tls.client_cert_from_pfx` and pass `client_cert=` to the crawler; that may bypass the
   interactive captcha entirely.

## Honest expectations
This is a maintenance treadmill — gov.br rotates its JS and detection. Budget for ongoing upkeep, and
prefer a strong identity/proxy (and client certs where accepted) before investing in a JS-runtime crack.
