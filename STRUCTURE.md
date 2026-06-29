# crawlerkit-core — structure

The standalone, browserless crawler base. Importable as **`crawlerkit.core`**. Open source; depends on
no other crawlerkit package.

```
crawlerkit-core/
├─ crawlerkit/core/            # the importable package  →  import crawlerkit.core
│  ├─ __init__.py              # public exports: BaseCrawler, BaseParser, RawResponse, Transport, Profile
│  ├─ base_crawler.py          # BaseCrawler (the flow() hook) + run() retry/rotation; RawResponse
│  ├─ base_parser.py           # BaseParser[T] — generic parse() -> list[T]; inherited render_pdf()
│  ├─ transport.py             # fingerprinted curl_cffi session — the ONLY HTTP path
│  ├─ identity.py              # browserforge Profile; UA/sec-ch-ua snapped to the impersonate target
│  ├─ tls.py                   # per-host CA bundle + AIA repair + .pfx client certs (mutual TLS)
│  ├─ proxy.py                 # proxy leasing/providers (Null, Static); shared with captcha solvers
│  ├─ cookies.py               # optional cookie-jar persistence across crawls
│  ├─ errors.py                # Transient/Permanent/Blocked taxonomy + raise_for_block()
│  └─ captcha/                 # captcha detection + an own-first solver registry
│     ├─ __init__.py           # exports the registry + every solver
│     ├─ base.py               # Challenge/Solved, CaptchaRegistry, default_registry()
│     ├─ mcaptcha.py           # mCaptcha proof-of-work solver (compute backend, no third party)
│     ├─ token_adapters.py     # OPTIONAL reCAPTCHA v2/v3 + hCaptcha via a token provider
│     ├─ llm_image.py          # OWN image-captcha solver — inject a vision-LLM classify() callable
│     ├─ turnstile.py          # Cloudflare Turnstile — detect() works; solve() = browserless TODO stub
│     └─ govbr.py              # gov.br SSO — detect() works; solve() = browserless TODO stub
├─ examples/                   # runnable, contracts-free demos (not shipped in the wheel)
│  ├─ __init__.py
│  ├─ quotes.py                # a full crawler: crawl quotes.toscrape.com -> parse -> list[dict]
│  ├─ fingerprint_demo.py      # prove the real Chrome JA3/UA on the wire (tls.peet.ws echo)
│  └─ README.md                # how to run both
├─ docs/                       # standalone mkdocs site (mkdocstrings API ref)
│  ├─ index.md                 # overview / landing
│  ├─ identity.md              # the browserforge↔impersonate snap
│  ├─ transport-tls.md         # transport + TLS/AIA + client certs
│  ├─ proxy.md                 # proxy providers + rotation
│  ├─ captcha.md               # the registry + solvers
│  ├─ cracking-turnstile.md    # browserless Turnstile solver (engine-backed)
│  ├─ cracking-govbr.md        # gov.br SSO stub solver
│  ├─ errors.md                # error taxonomy + retry/rotation
│  └─ api.md                   # auto API reference
├─ GETTING_STARTED.md          # build-a-crawler walkthrough (flow → parse → run)
├─ README.md                   # one-screen overview
├─ mkdocs.yml                  # docs-site config (builds standalone)
└─ pyproject.toml              # distribution metadata + deps (PyPI only: curl_cffi, browserforge, …)
```

## Why the `crawlerkit/core/` nesting (not redundant)
The distribution is `crawlerkit-core` but the code lives one level down at `crawlerkit/core/`. That is
deliberate **PEP 420 namespace packaging**: `crawlerkit` is a *shared namespace* that several separately
installed distributions (`crawlerkit-core`, `-contracts`, `-rabbitmq`) each contribute one subpackage to.
There is intentionally **no `crawlerkit/__init__.py`** — its absence is what lets the independently
installed packages merge into one `crawlerkit.*` tree at import time. `pyproject.toml` enforces this with
`[tool.setuptools.packages.find] namespaces = true`.

## Not here / optional
- **No private deps.** core never imports `crawlerkit.contracts`/`.rabbitmq`/`.k8s` (CI-guarded). `parse()`
  returns *your* type via `BaseParser[T]` — a dataclass, a pydantic model, or a plain `dict`.
- **`turnstile.py` / `govbr.py`** ship as scaffolds: detection works, solving raises a clear `TODO` until
  you implement the browserless crack.
- `examples/` and `docs/` are not part of the installed wheel — they live in the repo.
