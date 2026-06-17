# crawlerkit-core

[![PyPI version](https://img.shields.io/pypi/v/crawlerkit-core.svg)](https://pypi.org/project/crawlerkit-core/)
[![Python versions](https://img.shields.io/pypi/pyversions/crawlerkit-core.svg)](https://pypi.org/project/crawlerkit-core/)
[![CI](https://github.com/lucascaovilla/crawlerkit/actions/workflows/ci.yml/badge.svg)](https://github.com/lucascaovilla/crawlerkit/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

A **standalone, browserless** crawler base (`crawlerkit.core`): fingerprinted **curl_cffi** transport,
per-host TLS with **AIA repair** + `.pfx` client certs, **browserforge** identity (UA snapped to the
impersonate target), proxy providers, a pluggable **captcha** registry, an error taxonomy with
retry+rotation, and the `BaseCrawler.flow()` / `BaseParser.parse()` hooks. Zero non-PyPI dependencies —
`parse()` returns **your own type**, not one the library dictates.

## Install

```bash
pip install crawlerkit-core
```

## Use

```python
from crawlerkit.core import BaseCrawler, BaseParser, RawResponse, Transport, Profile
from crawlerkit.core.captcha import default_registry, McaptchaPowSolver, mcaptcha_hint
from crawlerkit.core.proxy import StaticProxyProvider, ProxyProvider
from crawlerkit.core.errors import BlockedError, TransientError, raise_for_block
```

**HTTP is curl_cffi only — `requests` is never used.** Deps: curl_cffi, browserforge, cryptography,
certifi, selectolax, lxml, beautifulsoup4, weasyprint, structlog.

## Logging

Logging is **opt-in and off by default** — crawlerkit emits nothing unless you ask. Set
`enable_logs = True` on your crawler or parser to turn on structlog events:

```python
class MyCrawler(BaseCrawler):
    enable_logs = True   # default is False
```

**Build a crawler:** [GETTING_STARTED.md](GETTING_STARTED.md). **Run the demos:**
[`examples/`](examples/) (`quotes.py` — a full crawl+parse; `fingerprint_demo.py` — identity proof).
Reference: [`docs/`](docs/) (identity, transport-tls, proxy, captcha, cracking-govbr-turnstile, errors,
api). License: MIT.
