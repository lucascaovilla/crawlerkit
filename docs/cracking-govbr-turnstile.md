# Cracking gov.br & Turnstile (browserless)

`GovBrSolver` and `TurnstileSolver` ship as **scaffolds**: `detect()` works, but `solve()` is a
documented `# TODO` that raises `CaptchaNotImplementedError`. They are first-class registry slots so
the pipeline already recognizes the challenge and fails *loudly* — you fill in the browserless crack.

Files: `crawlerkit/core/captcha/{turnstile,govbr}.py`.

## Why they're stubs
Both mint a token by executing **obfuscated, fingerprint-bearing JavaScript** in the page — not a
replicable math operation (unlike mCaptcha's PoW). There is no clean browserless formula; you either
run the challenge JS yourself or get a passive token from a clean identity.

## The two browserless mechanisms
1. **Passive token (try first, cheap).** A clean impersonated identity (a coherent `Profile` + a
   residential proxy IP) often receives a valid token with **no interactive challenge** — especially
   Turnstile in non-interactive/managed mode. Always attempt this before the JS crack.
2. **JS-runtime crack (the real work).** Execute the widget's challenge script in a JS engine with a
   minimal DOM/navigator shim seeded from the active `Profile` (UA, `sec-ch-ua`, languages, screen) and
   the proxy IP. Candidate engines:
     - `py-mini-racer` / `quickjs` (embedded V8/QuickJS) — fast, in-process, but you supply the DOM shim.
     - a Node subprocess with `jsdom` — heavier DOM, slower, extra runtime dep.

## Implementing `TurnstileSolver.solve()`
`detect()` already returns `Challenge(params={"sitekey": ...})`. In `solve()`:

1. **Passive:** request `https://challenges.cloudflare.com/turnstile/v0/api.js` and the challenge
   endpoint for the sitekey using `transport` (so the JA3 + proxy match); if a `cf-turnstile-response`
   comes back without interaction, return it.
2. **JS-runtime:** fetch the challenge bundle, run it in the engine with the DOM/navigator shim, harvest
   the `cf-turnstile-response`.
3. `return Solved(token=<cf-turnstile-response>, expires_at=now+300)` (Turnstile tokens ~300s, single use).

Inject the token into the form field the page expects (usually `cf-turnstile-response`).

## Implementing `GovBrSolver.solve()`
gov.br SSO (`sso.acesso.gov.br`) is JS-heavy and gated by a captcha (hCaptcha/reCAPTCHA) + fingerprint
checks. In `solve()`:

1. **Drive the SSO sequence** with `transport`, carrying cookies across redirects (login → authorize →
   callback). Keep the same identity+proxy throughout.
2. **Solve the embedded captcha** — reuse the registry: if gov.br embeds hCaptcha/reCAPTCHA, delegate to
   an (opt-in) token adapter; or JS-runtime-crack the gov.br challenge script.
3. **Complete the OAuth/SSO redirect**; return `Solved(token=<session cookie / SSO assertion>)` (or set
   the session cookies on `transport._session` and return the marker the crawler needs).
4. **Client certs:** some gov.br services accept ICP-Brasil mutual TLS — load a `.pfx` via
   `crawlerkit.core.tls.client_cert_from_pfx` and pass `client_cert=` to the crawler; that may bypass the
   interactive captcha entirely.

## Wiring & testing
- They're already in `default_registry()`. To swap in your implementation, just edit the `solve()` body
  — the `kind` (`"turnstile"`/`"govbr"`) keeps the registry mapping intact.
- Unit-test `detect()` against captured HTML (works today). Integration-test `solve()` against a live
  endpoint once implemented; assert a usable token + that the subsequent form POST succeeds.
- Keep the failure typed: raise `CaptchaServiceError` for transient backend errors and let it bubble so
  `BaseCrawler.run()`'s retry can react.

## Honest expectations
This is a maintenance treadmill — providers rotate their JS and detection. Budget for ongoing upkeep,
and prefer the passive path + a strong identity/proxy before investing in the JS-runtime crack.
