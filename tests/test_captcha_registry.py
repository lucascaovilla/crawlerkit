"""The default registry ships the built-in solvers and stays quiet on benign pages."""

import pytest

from crawlerkit.core.captcha import (
    CaptchaError,
    CaptchaNotImplementedError,
    CaptchaRegistry,
    CaptchaServiceError,
    Challenge,
    GovBrSolver,
    TurnstileSolver,
    UnsupportedCaptcha,
    default_registry,
)
from crawlerkit.core.errors import CrawlerKitError


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


@pytest.mark.parametrize("exc_type", [CaptchaServiceError, CaptchaNotImplementedError])
def test_captcha_exception_family_is_catchable_as_one_type(exc_type: type) -> None:
    assert issubclass(exc_type, CaptchaError)
    assert issubclass(exc_type, CrawlerKitError)


@pytest.mark.parametrize("solver_cls,kind", [(TurnstileSolver, "turnstile"), (GovBrSolver, "govbr")])
def test_stub_solvers_raise_captcha_not_implemented(solver_cls: type, kind: str) -> None:
    with pytest.raises(CaptchaNotImplementedError):
        solver_cls().solve(Challenge(kind=kind), transport=None)
