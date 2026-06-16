# Captcha

`crawlerkit.core.captcha` is a **pluggable, own-first** registry. Three outcomes when a source
(HTML or response) is checked: no challenge → `None`; challenge + solver → `Solved{token, expires_at}`;
challenge + no solver → `UnsupportedCaptcha`. Tokens are single-use and solved on submit.

```python
from crawlerkit.core.captcha import default_registry
reg = default_registry()         # {mcaptcha, turnstile, govbr}
token = reg.solve(html, transport, hint=optional_challenge)   # via BaseCrawler.solve_captcha()
```

## Built-in own solvers
- **`McaptchaPowSolver`** — mCaptcha proof-of-work, pure compute, no key. Byte layout guarded by a
  captured-oracle `self_test()`. Retries the broker's transient errors and raises `CaptchaServiceError`
  on persistent failure. Detection: the mCaptcha widget URL; or pass `mcaptcha_hint(host, sitekey)`.
- **`LlmImageSolver`** — own image solver. Fetches the challenge image over the verified transport and
  classifies it with a **pluggable vision LLM** (inject a `classify(image_bytes, prompt) -> str`).
  Ships OCR + grid prompts (ported). The crawler builds the `Challenge(params={"image_url"|"image_bytes"})`.

```python
from crawlerkit.core.captcha import LlmImageSolver
reg.register(LlmImageSolver(classify=my_vision_model))
```

## Optional token adapters (opt-in)
reCAPTCHA v2/v3 and hCaptcha are solved browserlessly by POSTing `site_key` + `url` to a third-party
token provider. They are **not** in `default_registry` (they reintroduce a paid dependency) — register
them only when configured:

```python
from crawlerkit.core.captcha import RecaptchaV2Solver, HcaptchaSolver
reg.register(RecaptchaV2Solver(provider)).register(HcaptchaSolver(provider))
```
`provider` is any object implementing `TokenProvider` (`solve_recaptcha_v2/v3`, `solve_hcaptcha`) — e.g.
an adapter around an existing 2captcha/anticaptcha client. The crawler supplies `params["url"]`.

## gov.br & Turnstile — browserless stubs
`GovBrSolver` and `TurnstileSolver` are registered so **detection works**, but `solve()` is a documented
`# TODO` that raises `NotImplementedError` until you implement the browserless crack. See
[Cracking gov.br & Turnstile](cracking-govbr-turnstile.md).

## Writing a solver
```python
from crawlerkit.core.captcha import Challenge, Solved

class MySolver:
    kind = "mycaptcha"
    @classmethod
    def detect(cls, text):           # signature scan -> Challenge | None
        return Challenge(kind=cls.kind, params={...}) if "marker" in text else None
    def solve(self, challenge, transport) -> Solved:
        ...
        return Solved(token="...", expires_at=None)
reg.register(MySolver())
```
