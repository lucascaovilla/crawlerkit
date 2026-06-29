"""The default registry ships the built-in solvers and stays quiet on benign pages."""

import pytest

from crawlerkit.core.captcha import (
    CaptchaError,
    CaptchaNotImplementedError,
    CaptchaRegistry,
    CaptchaServiceError,
    Challenge,
    ChallengeEngineError,
    GovBrSolver,
    Solved,
    TurnstileSolver,
    UnsupportedCaptcha,
    default_registry,
)
from crawlerkit.core.errors import CrawlerKitError


class _CapturingSolver:
    """Solver that records the Challenge it receives (to assert detect/hint merge)."""

    kind = "turnstile"

    def __init__(self) -> None:
        self.seen: Challenge | None = None

    @classmethod
    def detect(cls, text: str):
        # an inline widget: detect only sees the markup, yields a sitekey, can't know the URL
        return Challenge(kind=cls.kind, params={"sitekey": "FROM_HTML"}) if "cf-turnstile" in text else None

    def solve(self, challenge: Challenge, transport) -> Solved:
        self.seen = challenge
        return Solved(token="t")


def test_registry_merges_inline_detect_with_hint_same_kind() -> None:
    solver = _CapturingSolver()
    reg = CaptchaRegistry().register(solver)
    # hint carries the context detect can't see; sitekey left None to prove detect's value wins
    hint = Challenge(kind="turnstile", params={"sitekey": None, "page_url": "https://x/y", "html": "<h>"})
    reg.solve("<div class='cf-turnstile'></div>", transport=None, hint=hint)
    assert solver.seen is not None
    assert solver.seen.params["page_url"] == "https://x/y"  # from hint
    assert solver.seen.params["html"] == "<h>"              # from hint
    assert solver.seen.params["sitekey"] == "FROM_HTML"     # detect's non-None value wins


def test_registry_ignores_hint_of_different_kind() -> None:
    solver = _CapturingSolver()
    reg = CaptchaRegistry().register(solver)
    reg.solve("<div class='cf-turnstile'></div>", transport=None, hint=Challenge(kind="other", params={"x": 1}))
    assert solver.seen.params == {"sitekey": "FROM_HTML"}  # unmerged; different kind


def test_registry_uses_hint_when_nothing_detected() -> None:
    solver = _CapturingSolver()
    reg = CaptchaRegistry().register(solver)
    hint = Challenge(kind="turnstile", params={"sitekey": "HINTED", "page_url": "u", "html": "h"})
    reg.solve("<html>no widget</html>", transport=None, hint=hint)  # detect -> None
    assert solver.seen.params == {"sitekey": "HINTED", "page_url": "u", "html": "h"}


def test_default_registry_has_builtin_solvers() -> None:
    reg = default_registry()
    assert isinstance(reg, CaptchaRegistry)
    assert {"mcaptcha", "turnstile", "govbr"} <= set(reg._solvers)


def test_detect_returns_none_on_benign_page() -> None:
    reg = default_registry()
    assert reg.detect("<html><body>no challenge here</body></html>") is None


def test_solve_returns_none_when_no_challenge() -> None:
    reg = default_registry()
    # No challenge in the source and no hint -> nothing to solve, transport never touched.
    assert reg.solve("<html>ok</html>", transport=None) is None


def test_unsupported_captcha_is_a_captcha_error() -> None:
    reg = default_registry()
    with pytest.raises(UnsupportedCaptcha) as exc_info:
        reg.solve("ignored", transport=None, hint=Challenge(kind="not-registered"))
    assert isinstance(exc_info.value, CaptchaError)
    assert isinstance(exc_info.value, CrawlerKitError)


@pytest.mark.parametrize(
    "exc_type", [CaptchaServiceError, CaptchaNotImplementedError, ChallengeEngineError]
)
def test_captcha_exception_family_is_catchable_as_one_type(exc_type: type) -> None:
    assert issubclass(exc_type, CaptchaError)
    assert issubclass(exc_type, CrawlerKitError)


def test_govbr_stub_raises_captcha_not_implemented() -> None:
    with pytest.raises(CaptchaNotImplementedError):
        GovBrSolver().solve(Challenge(kind="govbr"), transport=None)


def test_turnstile_without_page_context_raises_engine_error() -> None:
    # A bare challenge (sitekey only, e.g. from detect()) lacks the page_url + html the token
    # solve binds to — fail loudly and typed, before ever touching transport.
    with pytest.raises(ChallengeEngineError):
        TurnstileSolver().solve(Challenge(kind="turnstile", params={"sitekey": "0xABC"}), transport=None)
