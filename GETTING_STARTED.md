# Getting started — build a crawler (crawlerkit-core)

A crawlerkit crawler is **two hooks**: `flow()` (crawl → raw response) and `parse()` (raw → a list of
**your own items**). Everything detection-sensitive is inherited from `crawlerkit-core`. This guide
builds one end to end and proves the fingerprint is real. crawlerkit-core is a **standalone library**:
you call `crawler.run(...)` from your own code — no framework, no extra packages required.

## What's automatic vs opt-in
| Capability | How | You do |
|---|---|---|
| Browser **fingerprint / UA / sec-ch-ua** | `BaseCrawler` → browserforge `Profile`, UA snapped to a curl_cffi `impersonate` target | **nothing** |
| **TLS** (verified, AIA-repaired) | per-host CA bundle | nothing |
| **Retry + rotation** | `run()` (transient→retry; blocked→rotate identity+proxy) | raise the right error |
| **Proxy** | `proxy_provider=` | wire one |
| **Captcha** | `captcha_hint` + `solve_captcha()` | wire per target |
| **Client cert** (mutual TLS) | `client_cert=` | provide a `.pfx` |
| **Pacing** | `CRAWLERKIT_MIN_INTERVAL` | set env (optional) |
| **Logging** | `enable_logs = True` on your crawler/parser | off by default |

So "use fingerprinting/UA" needs **zero** code. "Use everything" = wire proxy + captcha (below).

## Install
```bash
pip install crawlerkit-core
# or, in this repo (editable):
pip install -e packages/crawlerkit-core
```

## See it work first
Two runnable demos ship in [`examples/`](examples/) — a complete crawl + parse, and a fingerprint
proof. Run them before writing your own:
```bash
python examples/quotes.py            # crawl quotes.toscrape.com -> list[dict]
python examples/fingerprint_demo.py  # show the real Chrome JA3 on the wire
```

## 1. Scaffold
```
my_crawler/
  crawler.py        # flow()
  parser.py         # parse() -> list[YourItem]
```
Use [`examples/quotes.py`](examples/quotes.py) as a working template.

## 2. `flow()` — the crawl (identity is already on)
```python
from crawlerkit.core import BaseCrawler, RawResponse

class MyCrawler(BaseCrawler):
    def flow(self, params: dict) -> RawResponse:
        r = self.get("https://target/form")                 # browserforge UA + Chrome JA3 + verified TLS, automatically
        data = self.hidden_fields(r.text) | {               # JSF ViewState / ASP.NET __VIEWSTATE captured
            "plate": params["plate"],
        }
        pr = self.post("https://target/form", data=data, headers={"Referer": r.url})
        return RawResponse(url=pr.url, status=pr.status_code, text=pr.text, headers=dict(pr.headers))
```
`self.get/post` go through the fingerprinted transport. `self.profile.impersonate` / `.user_agent` show
the identity in use. (Details: [identity](docs/identity.md), [transport & TLS](docs/transport-tls.md).)

## 3. Proxy (recommended)
```python
from crawlerkit.core.proxy import StaticProxyProvider
MyCrawler(proxy_provider=StaticProxyProvider())          # CRAWLERKIT_PROXIES="http://u:p@host:port,..."
```
Any commercial proxy vendor works too — subclass `ProxyProvider` and return its URL from `lease()`
(a few lines; see [proxy](docs/proxy.md)). The lease is reused by captcha solvers (token scored from
the submitting IP), and rotates with the identity on a block.

## 4. Captcha
The registry detects + solves; `solve_captcha(html)` returns a token (or `None`/`UnsupportedCaptcha`).
```python
from crawlerkit.core.captcha import mcaptcha_hint

class MyCrawler(BaseCrawler):
    captcha_hint = mcaptcha_hint(host="captcha.target.gov.br", sitekey="...")   # if not inline in HTML
    def flow(self, params):
        r = self.get(url)
        token = self.solve_captcha(r.text)        # mCaptcha PoW: own, compute, no key
        ...
```
- **mCaptcha** — works out of the box.
- **reCAPTCHA / hCaptcha** — opt-in token adapters: `reg.register(RecaptchaV2Solver(provider))`.
- **LLM image** — `reg.register(LlmImageSolver(classify=my_vision_model))`.
- **gov.br / Turnstile** — registered **stubs**: `detect()` works, `solve()` raises a clear TODO until you
  implement the browserless crack ([cracking guide](docs/cracking-govbr-turnstile.md)).

([captcha](docs/captcha.md))

## 5. `parse()` — raw → your items
`BaseParser` is generic: subclass it with the type **you** want. crawlerkit-core never imposes a model —
return your own dataclass, a pydantic model, or plain `dict`s.
```python
from dataclasses import dataclass
from crawlerkit.core import BaseParser, RawResponse
from selectolax.parser import HTMLParser

@dataclass
class Quote:
    text: str
    author: str

class MyParser(BaseParser[Quote]):                       # or BaseParser[dict]
    def parse(self, raw: RawResponse) -> list[Quote]:
        tree = HTMLParser(raw.text)                      # selectolax / bs4 over raw.text
        return [Quote(text=n.css_first(".text").text(), author=n.css_first(".author").text())
                for n in tree.css(".quote")]
```
Item-local, no network. An optional `render_pdf()` (WeasyPrint, no browser) is inherited; set
`render_pdf_enabled = False` to skip it. See [`examples/quotes.py`](examples/quotes.py) for the full version.

## 6. Client certificate (optional, mutual TLS, e.g. ICP-Brasil)
```python
from crawlerkit.core.tls import client_cert_from_pfx
MyCrawler(client_cert=client_cert_from_pfx("cert.pfx", "password"))
```

## 7. Errors & pacing
Raise `PermanentError` for non-retryable cases (bad input); call `raise_for_block(resp)` to turn a
403/429/challenge page into a rotation. Set `CRAWLERKIT_MIN_INTERVAL=2` for polite pacing. ([errors](docs/errors.md))

## 8. Run it + SEE the fingerprint
```python
crawler = MyCrawler(proxy_provider=StaticProxyProvider())
raw = crawler.run({"plate": "ABC1234"})       # retry + rotation built in
items, pdf = MyParser().run(raw)              # items: list[YourItem]; pdf: bytes | None
```
Drive that loop however you like — a script, a cron, your own queue consumer. The bundled demo proves
the identity is real:
```bash
python examples/fingerprint_demo.py
```
```
== crawlerkit identity (generated) ==
  impersonate : chrome133a
  user-agent  : Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/133.0.0.0 Safari/537.36
  sec-ch-ua   : "Google Chrome";v="133", "Not.A/Brand";v="8", "Chromium";v="133"
== echoed back by https://tls.peet.ws/api/all (status 200) ==
  user_agent   : ...Chrome/133.0.0.0...
  ja3_hash     : cb57fad91117278bf769e04070e10039
  ja4          : t13d1516h2_8daaf6152771_d8a2da3f94cd
  http2_akamai : 52d84b11737d980aef856699f885ca86
```
The echoed UA matches, and the JA3/JA4/HTTP2 values are a **real Chrome's** (curl_cffi impersonate) — not
a Python HTTP client's. That is fingerprinting + UA in action, automatically.

## 9. Logging (optional)
Logging is **off by default** — the library emits nothing unless you ask. Turn it on per class by
setting `enable_logs = True`:
```python
class MyCrawler(BaseCrawler):
    enable_logs = True            # structlog events: crawl_start/done, blocks, rotations
```
The same flag works on a `BaseParser` subclass, and on a standalone `Transport(..., enable_logs=True)`.

## Checklist
- [ ] `flow()` returns a `RawResponse` — identity/UA/TLS already applied
- [ ] proxy provider wired
- [ ] captcha handled (`solve_captcha`, or a registered solver)
- [ ] `parse()` returns `list[YourItem]` (your model/dataclass/dict)
- [ ] `fingerprint_demo.py`-style echo (or your target) shows a real Chrome fingerprint
- [ ] you call `crawler.run(...)` from your own code/loop
