# crawlerkit-core examples

Two runnable, self-contained demos. Both use **only** `crawlerkit-core` — no other package.

```bash
pip install -e .          # from the crawlerkit-core package root
python examples/quotes.py            # a complete crawl + parse
python examples/fingerprint_demo.py  # proof the Chrome fingerprint is real
```

- **`quotes.py`** — a full crawler: `flow()` walks https://quotes.toscrape.com page by page,
  `parse()` returns `list[dict]` (`{text, author, tags}`). Identity, TLS, retry and rotation are
  automatic. Swap `BaseParser[dict]` for `BaseParser[YourModel]` to parse into your own type.
- **`fingerprint_demo.py`** — hits a TLS echo and prints the generated UA/impersonate next to the
  UA + JA3/JA4/HTTP2 the server actually saw. They match, and the JA3 is a real Chrome's. Override
  the target with `ECHO_URL=https://httpbin.org/headers`.
