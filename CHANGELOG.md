## v0.2.2 (2026-06-16)

### Fix

- **release**: re-tag as 0.2.2 — re-pointed v0.2.1 after tagging the wrong commit, and PyPI's filename-reuse policy blocks retrying the same version

## v0.2.1 (2026-06-16)

### Fix

- **release**: re-tag as 0.2.1 — 0.1.0/0.2.0 are permanently blocked from re-upload by PyPI's filename-reuse policy after the project was deleted and re-created

## v0.2.0 (2026-06-16)

### Feat

- **core**: gate logs behind enable_logs (default off)
- **core**: add opt-in logging helper

## v0.1.0 (2026-06-16)

### Feat

- **core**: add BaseCrawler, BaseParser, and package exports
- **captcha**: expose captcha public API
- **captcha**: add optional token-adapter and image solvers
- **captcha**: add browserless turnstile and gov.br stubs
- **captcha**: add mcaptcha proof-of-work solver
- **captcha**: add registry, challenge model, and detection
- **core**: add optional cookie-jar persistence
- **core**: add fingerprinted curl_cffi transport
- **core**: add per-host TLS bundle with AIA repair
- **core**: add proxy providers and leasing
- **core**: add browserforge identity profiles
- **core**: add crawl error taxonomy and block detector
