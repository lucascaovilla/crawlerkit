"""BaseParser — the parse stage. A new target fills one hook: parse().

Pure + item-local: no network beyond fetching static assets for the optional PDF, no
cross-item state. Operates on the RawResponse the crawler returned (or a replayed one).
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar
from urllib.parse import urlparse

from ._logging import get_logger
from .base_crawler import RawResponse

#: What ``parse()`` yields — your own model, a dataclass, a ``dict``, anything.
#: crawlerkit-core stays dependency-free: it never dictates the output type.
T = TypeVar("T")

# Print fixups: hide leftover form inputs, landscape, fit wide tables.
_PDF_FIXUP_CSS = """
input { display: none !important; }
@page { size: A4 landscape; margin: 1.2cm; }
table { font-size: 9px; table-layout: fixed; width: 100%; }
td, th { overflow-wrap: anywhere; }
"""


def render_pdf(html: str, base_url: str, *, enable_logs: bool = False) -> bytes:
    """HTML -> PDF (WeasyPrint, no browser). Fetches remote CSS over a verified, AIA-repaired
    TLS connection (curl_cffi + crawlerkit.core.tls). No `requests`."""
    from curl_cffi import requests as cffi
    from weasyprint import CSS, HTML, default_url_fetcher

    from . import tls

    log = get_logger(enable_logs)

    def fetcher(url: str, **kw):
        if url.startswith(("http://", "https://")):
            host = urlparse(url).hostname or ""
            try:
                r = cffi.get(url, verify=tls.build_ca_bundle(host), timeout=30, impersonate="chrome131")
                ct = r.headers.get("content-type", "")
                out = {"string": r.content, "redirected_url": str(r.url)}
                mime = ct.split(";")[0].strip()
                if mime:
                    out["mime_type"] = mime
                return out
            except Exception as e:  # noqa: BLE001 — a missing asset must not kill the PDF
                log.warning("pdf_asset_skipped", url=url, error=str(e))
                return {"string": b"", "mime_type": "text/plain"}
        return default_url_fetcher(url, **kw)

    return HTML(string=html, base_url=base_url, url_fetcher=fetcher).write_pdf(
        stylesheets=[CSS(string=_PDF_FIXUP_CSS)]
    )


class BaseParser(ABC, Generic[T]):
    """Parse stage. Subclass with your own item type: ``class MyParser(BaseParser[MyModel])``
    (or ``BaseParser[dict]``). ``parse()`` returns ``list[T]``; the type is yours, not the lib's."""

    render_pdf_enabled: bool = True
    enable_logs: bool = False  # opt-in: set True on your subclass to emit structlog logs

    @property
    def log(self):
        """structlog logger when ``enable_logs`` is True, else a silent no-op logger."""
        return get_logger(self.enable_logs)

    @abstractmethod
    def parse(self, raw: RawResponse) -> list[T]:
        ...

    def pdf(self, raw: RawResponse) -> bytes | None:
        if not self.render_pdf_enabled:
            return None
        return render_pdf(raw.text, base_url=raw.url, enable_logs=self.enable_logs)

    def run(self, raw: RawResponse) -> tuple[list[T], bytes | None]:
        items = self.parse(raw)
        self.log.info("parse_done", count=len(items))
        return items, self.pdf(raw)
