# crawlerkit-core

The browserless crawler base: fingerprinted **curl_cffi** transport, per-host TLS with **AIA repair** +
`.pfx` client certs, **browserforge** identity (UA snapped to the impersonate target so UA ⟺ JA3 never
drift), proxy providers, a pluggable **captcha** registry, an error taxonomy with retry + rotation, and
the `BaseCrawler.flow()` / `BaseParser.parse()` hooks.

```python
from crawlerkit.core import BaseCrawler, BaseParser, RawResponse, Transport, Profile
from crawlerkit.core.captcha import default_registry, McaptchaPowSolver, mcaptcha_hint
from crawlerkit.core.proxy import StaticProxyProvider, ProxyProvider
from crawlerkit.core.errors import BlockedError, TransientError, raise_for_block
```

**HTTP is curl_cffi only — `requests` is never used.** A self-contained library with no non-PyPI
dependencies: `parse()` returns your own type (`BaseParser[T]`), and you drive `crawler.run(...)` from
your own code.

## Start here
**Build a crawler:** see `GETTING_STARTED.md` (package root) — the end-to-end walkthrough. Two runnable
demos live in `examples/` (`quotes.py`, `fingerprint_demo.py`).

## Reference
- [Identity & fingerprint](identity.md) · [Transport & TLS](transport-tls.md) · [Proxy](proxy.md)
- [Captcha](captcha.md) · [Cracking gov.br & Turnstile](cracking-govbr-turnstile.md) · [Errors & retry](errors.md)
- [API reference](api.md)

License: MIT.
