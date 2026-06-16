# Transport & TLS

## Transport (`crawlerkit.core.transport`)
The single HTTP path: a `curl_cffi` Session bound to one `Profile` + a `ProxyLease` + a per-host
verified CA bundle. `requests` is never used.

```python
from crawlerkit.core.transport import Transport
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider

t = Transport(pick(), NullProxyProvider().lease(), verify=True)
r = t.get("https://example.gov.br")   # impersonate + verified CA applied automatically
```
Per request it sets `impersonate`, the verified CA bundle for the host, a timeout (`timeout=` on
`Transport`/`BaseCrawler`, default 30s — override per-call too, e.g. `crawler.get(url, timeout=60)`),
and the client cert if configured. **Pacing**: set `min_interval` (or `CRAWLERKIT_MIN_INTERVAL`) for a
minimum gap between requests (+25% jitter). **Errors**: curl/network failures are raised as
`crawlerkit.core.errors.TransientError` so `BaseCrawler.run()` retries them.

## TLS with AIA repair (`crawlerkit.core.tls`)
Many Brazilian gov hosts serve only their leaf certificate and omit the intermediate, so stock TLS
verification fails (`unable to get local issuer certificate`). `build_ca_bundle(host)`:

1. opens the host, reads the leaf's **AIA "CA Issuers"** URL;
2. fetches the missing intermediate(s), following the chain to a trusted root;
3. concatenates them with certifi's roots and caches the bundle per host (`CRAWLERKIT_CA_DIR`).

Verification stays **on** — this is the secure fix, not `verify=False`. (An `insecure` escape hatch
exists but is off by default.)

## Client certificates (ICP-Brasil mutual TLS)
Some services (SERPRO/SEFAZ/RENAINF and some gov.br flows) require a PKCS#12 client cert.

```python
from crawlerkit.core.tls import client_cert_from_pfx
from crawlerkit.core.base_crawler import BaseCrawler

pem = client_cert_from_pfx("cert.pfx", "password")   # -> combined PEM (key+cert+chain), chmod 600
crawler = MyCrawler(client_cert=pem)                  # curl_cffi `cert=` on every request
```
`client_cert_from_pfx` uses `cryptography` (no pyOpenSSL) and writes a `cert+key+chain` PEM suitable
for curl_cffi's `cert=`.
