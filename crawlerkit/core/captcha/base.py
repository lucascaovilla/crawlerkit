"""Captcha detection + a registry of our own solvers.

Three outcomes when a source (HTML or response) is checked:
  - no challenge        -> registry.solve returns None
  - challenge + solver  -> Solved{token, expires_at}
  - challenge, no solver -> raise UnsupportedCaptcha

A solver produces a token; the backend (compute / LLM-image / JS-runtime) is its own business.
Tokens are single-use and solved on submit (never pre-solved).

Every captcha-stage failure is a `CaptchaError` (itself a `crawlerkit.core.errors.CrawlerKitError`),
so callers can catch one type for "captcha solving failed" instead of guessing which builtin
exception a given solver happens to raise:
  - `UnsupportedCaptcha`         -> challenge detected, no solver registered for its kind.
  - `CaptchaServiceError`        -> the captcha backend/compute step failed (often transient).
  - `CaptchaTimeoutError`        -> a solve ran out of wall-clock time.
  - `CaptchaUnsolvedError`       -> a solve exhausted its attempt/iteration budget unsolved.
  - `CaptchaNotImplementedError` -> detected, but this solver's `solve()` is a stub (fails loudly).
"""

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable

from ..errors import CrawlerKitError


@dataclass
class Challenge:
    kind: str
    params: dict = field(default_factory=dict)


@dataclass
class Solved:
    token: str
    expires_at: float | None = None  # absolute epoch seconds, from the challenge's own ttl


class CaptchaError(CrawlerKitError):
    """Root of every captcha-stage exception."""


class UnsupportedCaptcha(CaptchaError):
    def __init__(self, kind: str):
        super().__init__(f"no solver registered for captcha kind: {kind}")
        self.kind = kind


class CaptchaServiceError(CaptchaError):
    """The captcha backend returned an unexpected/error response (often transient)."""


class CaptchaTimeoutError(CaptchaServiceError):
    """A solve ran out of wall-clock time before finding a solution."""


class CaptchaUnsolvedError(CaptchaServiceError):
    """A solve exhausted its attempt/iteration budget without finding a solution."""


class CaptchaNotImplementedError(CaptchaError):
    """Detected, but this solver's `solve()` is a stub — fails loudly, never silently."""


class InteractiveChallengeError(CaptchaError):
    """The widget escalated to a real interactive challenge (a click/puzzle the browserless
    solver does not fake). The caller should route to a fallback (LLM solver, external service,
    or a real browser). Never paired with a fabricated token."""

    def __init__(self, message: str, *, sitekey: str | None = None, page_url: str | None = None):
        super().__init__(message)
        self.sitekey = sitekey
        self.page_url = page_url


class ChallengeEngineError(CaptchaError):
    """The JS challenge engine could not run the challenge to a token — e.g. the native
    extension isn't built/installed, or a Cloudflare rotation broke the faked environment.
    Loud, with enough context (sitekey, page_url) to debug."""

    def __init__(self, message: str, *, sitekey: str | None = None, page_url: str | None = None):
        super().__init__(message)
        self.sitekey = sitekey
        self.page_url = page_url


@runtime_checkable
class Solver(Protocol):
    kind: str

    @classmethod
    def detect(cls, text: str) -> Optional[Challenge]:
        ...

    def solve(self, challenge: Challenge, transport) -> Solved:
        ...


class CaptchaRegistry:
    def __init__(self) -> None:
        self._solvers: dict[str, Solver] = {}

    def register(self, solver: Solver) -> "CaptchaRegistry":
        self._solvers[solver.kind] = solver
        return self

    def detect(self, source) -> Optional[Challenge]:
        text = source if isinstance(source, str) else getattr(source, "text", "") or ""
        for solver in self._solvers.values():
            ch = solver.detect(text)
            if ch is not None:
                return ch
        return None

    def solve(self, source, transport, *, hint: Optional[Challenge] = None) -> Optional[Solved]:
        challenge = self._resolve(self.detect(source), hint)
        if challenge is None:
            return None
        solver = self._solvers.get(challenge.kind)
        if solver is None:
            raise UnsupportedCaptcha(challenge.kind)
        return solver.solve(challenge, transport)

    @staticmethod
    def _resolve(detected: Optional[Challenge], hint: Optional[Challenge]) -> Optional[Challenge]:
        """Combine a detected challenge with a caller hint.

        `detect()` only sees HTML text, so for an inline widget it yields just what's in the
        markup (e.g. a sitekey) — it can't know the page URL or carry the raw HTML. The hint
        supplies exactly that missing context. When both are the SAME kind, merge them so the
        hint's context survives while detect's freshly-scraped values win where present (e.g. a
        rotated sitekey). When there's no hint (or a different kind), prefer detected, else hint —
        identical to the old `detect(source) or hint`."""
        if detected is None:
            return hint
        if hint is None or hint.kind != detected.kind:
            return detected
        merged = {**hint.params, **{k: v for k, v in detected.params.items() if v is not None}}
        return Challenge(kind=detected.kind, params=merged)


def default_registry() -> CaptchaRegistry:
    """Registry with the built-in own-solvers: mCaptcha PoW (working) + gov.br/Turnstile
    browserless stubs (detect works, solve raises CaptchaNotImplementedError until cracked).
    Optional token-adapters (reCAPTCHA/hCaptcha) and the LLM image solver are opt-in —
    register them yourself when configured."""
    from .govbr import GovBrSolver
    from .mcaptcha import McaptchaPowSolver
    from .turnstile import TurnstileSolver

    return (
        CaptchaRegistry()
        .register(McaptchaPowSolver())
        .register(TurnstileSolver())
        .register(GovBrSolver())
    )
