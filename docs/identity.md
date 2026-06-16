# Identity & fingerprint

`crawlerkit.core.identity` produces a coherent browser `Profile`: a curl_cffi `impersonate` target
plus a realistic, ordered header set whose UA and `sec-ch-ua` **match** that target.

## Why the snap
curl_cffi's `impersonate` owns the TLS/JA3 + HTTP2 fingerprint and ships a coherent UA. browserforge
generates real-world header sets but tracks the very latest Chrome (e.g. 147), which curl_cffi may not
impersonate. So `ProfileGenerator`:

1. generates a header set via `browserforge.headers.HeaderGenerator` (desktop, Windows/Linux, pt-BR);
2. reads the generated Chrome major;
3. picks the nearest supported impersonate target (`chrome120/124/131/133a`);
4. **snaps** the UA + `sec-ch-ua` version to that target.

Result: UA, `sec-ch-ua`, and JA3 all agree. If browserforge is unavailable, a static coherent
`chrome131` profile is returned.

## Use
```python
from crawlerkit.core.identity import ProfileGenerator, pick

p = ProfileGenerator().generate()
p.impersonate          # "chrome133a"
p.user_agent           # "...Chrome/133.0.0.0..."
p.headers()            # ordered dict incl. UA, sec-ch-ua, Accept-*, Sec-Fetch-*, Accept-Language

# BaseCrawler calls pick() once per instance; make a fresh crawler per crawl
# (as the examples do) and each crawl gets a freshly rotated profile.
```

`Accept-Encoding` keeps `br, zstd` — curl_cffi decodes both natively, which is more authentic than
stripping them.

## Rotation
Each `generate()` is randomized → rotation. `BaseCrawler` rotates the profile **together with the
proxy** on a `BlockedError` (identity + egress as a pair). To pin a profile, pass `profile=` to the
crawler.

## Extending the impersonate map
Pass your own list — no need to edit library source as curl_cffi adds targets (Safari/Edge/Firefox
can be added the same way):

```python
from crawlerkit.core.identity import ProfileGenerator, available_chrome_targets

# curated, hand-picked list
gen = ProfileGenerator(impersonate_targets=[(120, "chrome120"), (136, "chrome136")])

# or: everything the installed curl_cffi's BrowserType enum reports (best-effort; not all listed
# targets are necessarily well-verified yet — review before trusting it in production)
gen = ProfileGenerator(impersonate_targets=available_chrome_targets())
```
Keep the list ascending by major; the snap chooses the highest target ≤ the generated major. Falls
back to the curated built-in 4-target list when `impersonate_targets` isn't given.
