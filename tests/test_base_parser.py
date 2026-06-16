"""BaseParser[T] returns the caller's own type and honours render_pdf_enabled."""

from selectolax.parser import HTMLParser

from crawlerkit.core import BaseParser, RawResponse

HTML = """
<div class="quote"><span class="text">Be yourself</span></div>
<div class="quote"><span class="text">Stay hungry</span></div>
"""


class _DictParser(BaseParser[dict]):
    render_pdf_enabled = False  # keep the test offline: no WeasyPrint, no asset fetch

    def parse(self, raw: RawResponse) -> list[dict]:
        tree = HTMLParser(raw.text)
        return [{"text": n.css_first(".text").text(strip=True)} for n in tree.css(".quote")]


def _raw() -> RawResponse:
    return RawResponse(url="https://example.test", status=200, text=HTML)


def test_parse_returns_callers_type() -> None:
    items = _DictParser().parse(_raw())
    assert items == [{"text": "Be yourself"}, {"text": "Stay hungry"}]


def test_pdf_disabled_returns_none() -> None:
    assert _DictParser().pdf(_raw()) is None


def test_run_returns_items_and_no_pdf() -> None:
    items, pdf = _DictParser().run(_raw())
    assert len(items) == 2
    assert pdf is None
