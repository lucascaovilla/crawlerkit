# Errors & retry

`crawlerkit.core.errors` defines a small taxonomy under one root, `CrawlerKitError` — catch it for
"any crawlerkit-specific failure," crawl-stage or captcha-stage. `BaseCrawler.run()` reacts to each
crawl-stage class:

| Exception | Meaning | `run()` reaction |
|---|---|---|
| `TransientError` | network blip / timeout / 5xx | back off, **retry with the same identity** |
| `BlockedError` | anti-bot block (403/429/challenge page) | **rotate identity + proxy**, then retry |
| `PermanentError` | bad input / unrecoverable | **fail fast** (no retry) |
| anything else | unexpected | propagates |

The transport already raises `TransientError` for curl/network failures, so connection blips retry for
free. To classify a *200-but-blocked* page, call the opt-in guard from `flow()`:

```python
from crawlerkit.core.errors import raise_for_block

def flow(self, params):
    r = self.get(url)
    raise_for_block(r)   # -> BlockedError on 403/429/"Just a moment"/challenge markers; TransientError on 5xx
    ...
```

## The retry/rotation loop
```python
crawler = MyCrawler(max_attempts=3)         # default 3
raw = crawler.run(params)
```
On `BlockedError`, `run()` calls `_rotate()` (a fresh `Profile` + a fresh proxy lease + a new
`Transport`) before retrying — identity and egress rotate together. Back-off is exponential with
jitter, capped at 30s. Raise `PermanentError` from `flow()`/`parse()` for non-retryable conditions
(e.g. invalid plate) so you don't waste attempts.

## Captcha errors
`crawlerkit.core.captcha` adds its own subtree under one root, `CaptchaError`:

| Exception | Meaning |
|---|---|
| `UnsupportedCaptcha` | challenge detected, no solver registered for its kind |
| `CaptchaServiceError` | the captcha backend/compute step failed (often transient) |
| `CaptchaTimeoutError` | a solve ran out of wall-clock time (subclass of `CaptchaServiceError`) |
| `CaptchaUnsolvedError` | a solve exhausted its attempt/iteration budget unsolved (subclass of `CaptchaServiceError`) |
| `CaptchaNotImplementedError` | detected, but this solver's `solve()` is a stub (e.g. `TurnstileSolver`/`GovBrSolver`) |

Decide per crawler whether to map any of these to `BlockedError`/`TransientError` for the retry loop.
