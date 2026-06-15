"""Crawl error taxonomy + a response block-detector.

`BaseCrawler.run()` reacts by class:
  - `TransientError`  -> back off, retry with the SAME identity (network blip / timeout / 5xx).
  - `BlockedError`    -> rotate identity + proxy, then retry (anti-bot block: 403/429/challenge page).
  - `PermanentError`  -> fail fast, no retry (bad input / unrecoverable).
Anything else propagates unchanged.

`CrawlerKitError` is the root of every exception type crawlerkit raises (crawl-stage `CrawlerError`
here, captcha-stage `CaptchaError` in `crawlerkit.core.captcha.base`) — catch it to mean "any
crawlerkit-specific failure."
"""

import re


class CrawlerKitError(Exception):
    """Root of every exception type crawlerkit raises."""


class CrawlerError(CrawlerKitError):
    """Base class for crawl-stage errors."""


class TransientError(CrawlerError):
    """A transient failure (network blip, timeout, 5xx) — retry unchanged."""


class PermanentError(CrawlerError):
    """An unrecoverable failure (bad input, hard 4xx) — do not retry."""


class BlockedError(CrawlerError):
    """An anti-bot block (403/429 or a challenge page) — rotate identity+proxy and retry."""


# Common interstitial/anti-bot markers (Cloudflare, Akamai, Incapsula, generic).
_BLOCK_MARKERS = re.compile(
    r"just a moment|attention required|access denied|/cdn-cgi/challenge|"
    r"cf-error-details|akamai|incapsula|request unsuccessful",
    re.I,
)


def raise_for_block(response) -> None:
    """Opt-in guard: raise `BlockedError`/`TransientError` if a response looks blocked.

    Call from `flow()` after a request whose 200 you don't fully trust. Detects 403/429 and
    common challenge-page markers (-> blocked) and 5xx (-> transient).
    """
    status = getattr(response, "status_code", 0) or 0
    text = getattr(response, "text", "") or ""
    if status in (403, 429) or _BLOCK_MARKERS.search(text[:4000]):
        raise BlockedError(f"anti-bot block detected (status={status})")
    if status >= 500:
        raise TransientError(f"server error (status={status})")
