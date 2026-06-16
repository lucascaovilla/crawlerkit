"""A complete crawlerkit crawler: crawl + parse, with zero extra dependencies.

    python examples/quotes.py            # (run from the crawlerkit-core package root)

Scrapes https://quotes.toscrape.com (a site built for scraping practice), following the
"Next" pagination, and prints the parsed quotes as plain dicts.

What this shows:
  * `flow()`  — the crawl. `self.get(url)` already carries a browserforge identity, a real
                Chrome JA3 (curl_cffi impersonate) and verified per-host TLS. You write ~2 lines.
  * `parse()` — raw HTML -> `list[dict]`. `BaseParser[dict]` means YOU pick the output type;
                crawlerkit-core never imposes a model. Return your own dataclass/pydantic model
                just as easily: `class QuotesParser(BaseParser[Quote])`.
  * `run()`   — retry on transient errors and identity+proxy rotation on a block, for free.

No proxy or captcha is needed for this target; wire them per the docs when yours needs them.
"""

from urllib.parse import urljoin

from selectolax.parser import HTMLParser

from crawlerkit.core import BaseCrawler, BaseParser, RawResponse

BASE_URL = "https://quotes.toscrape.com/"


class QuotesCrawler(BaseCrawler):
    """Fetch one page. `params["url"]` selects the page (defaults to the first)."""

    def flow(self, params: dict) -> RawResponse:
        url = params.get("url", BASE_URL)
        r = self.get(url)  # identity + verified TLS applied automatically
        return RawResponse(url=url, status=r.status_code, text=r.text, headers=dict(r.headers))


class QuotesParser(BaseParser[dict]):
    """Raw HTML -> a list of {text, author, tags} dicts. Item-local, no network."""

    render_pdf_enabled = False  # plain data extraction; no PDF needed

    def parse(self, raw: RawResponse) -> list[dict]:
        tree = HTMLParser(raw.text)
        quotes = []
        for node in tree.css(".quote"):
            quotes.append({
                "text": _text(node, ".text"),
                "author": _text(node, ".author"),
                "tags": [t.text() for t in node.css(".tag")],
            })
        return quotes

    @staticmethod
    def next_page(raw: RawResponse) -> str | None:
        """Absolute URL of the next page, or None on the last page."""
        link = HTMLParser(raw.text).css_first("li.next a")
        href = link.attributes.get("href") if link else None
        return urljoin(raw.url, href) if href else None


def _text(node, sel: str) -> str:
    found = node.css_first(sel)
    return found.text(strip=True) if found else ""


def crawl_all(max_pages: int = 10) -> list[dict]:
    """Walk every page via the Next link, parsing each. One fresh crawler per page so each
    crawl gets a freshly rotated identity (exactly how a queue worker would drive it)."""
    parser = QuotesParser()
    all_quotes: list[dict] = []
    url = BASE_URL
    for _ in range(max_pages):
        raw = QuotesCrawler().run({"url": url})  # retry + rotation built in
        all_quotes.extend(parser.parse(raw))
        nxt = parser.next_page(raw)
        if not nxt:
            break
        url = nxt
    return all_quotes


def main() -> None:
    quotes = crawl_all()
    print(f"parsed {len(quotes)} quotes\n")
    for q in quotes[:3]:
        print(f"  {q['text']}")
        print(f"    — {q['author']}  {q['tags']}\n")
    if quotes:
        print("^ a real crawl (fingerprinted, verified TLS) parsed into plain dicts — no private deps.")


if __name__ == "__main__":
    main()
