"""mCaptcha PoW: typed failures instead of bare builtin exceptions, plus the byte-layout oracle."""

import pytest

from crawlerkit.core.captcha import CaptchaError, CaptchaTimeoutError, CaptchaUnsolvedError
from crawlerkit.core.captcha.mcaptcha import McaptchaPowSolver


def test_self_test_oracle_passes() -> None:
    McaptchaPowSolver.self_test()  # raises AssertionError on layout drift


def test_solve_pow_raises_captcha_timeout_error() -> None:
    # max_seconds=0: any elapsed time at the first periodic checkpoint trips it. Difficulty is
    # astronomically high so a solution is never found before that checkpoint, regardless of
    # how fast the CPU running this test is.
    with pytest.raises(CaptchaTimeoutError) as exc_info:
        McaptchaPowSolver.solve_pow("salt", "string", difficulty=10**30, max_seconds=0)
    assert isinstance(exc_info.value, CaptchaError)


def test_solve_pow_raises_captcha_unsolved_error() -> None:
    # max_iters is well below the first time-based checkpoint (2**18), so the loop exhausts its
    # iteration budget without ever consulting the wall clock — deterministic on any hardware.
    with pytest.raises(CaptchaUnsolvedError) as exc_info:
        McaptchaPowSolver.solve_pow("salt", "string", difficulty=10**30, max_iters=1000, max_seconds=120)
    assert isinstance(exc_info.value, CaptchaError)
