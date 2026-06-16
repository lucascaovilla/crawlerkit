from .base import (
    CaptchaError,
    CaptchaNotImplementedError,
    CaptchaRegistry,
    CaptchaServiceError,
    CaptchaTimeoutError,
    CaptchaUnsolvedError,
    Challenge,
    Solved,
    UnsupportedCaptcha,
    default_registry,
)
from .govbr import GovBrSolver
from .llm_image import LlmImageSolver
from .mcaptcha import McaptchaPowSolver, mcaptcha_hint
from .token_adapters import HcaptchaSolver, RecaptchaV2Solver, RecaptchaV3Solver, TokenProvider
from .turnstile import TurnstileSolver

__all__ = [
    "Challenge",
    "Solved",
    # exception taxonomy (all subclass CaptchaError)
    "CaptchaError",
    "UnsupportedCaptcha",
    "CaptchaServiceError",
    "CaptchaTimeoutError",
    "CaptchaUnsolvedError",
    "CaptchaNotImplementedError",
    "CaptchaRegistry",
    "default_registry",
    # own solvers
    "McaptchaPowSolver",
    "mcaptcha_hint",
    "LlmImageSolver",
    # browserless stubs (TODO crack)
    "TurnstileSolver",
    "GovBrSolver",
    # optional token-adapters (opt-in)
    "TokenProvider",
    "RecaptchaV2Solver",
    "RecaptchaV3Solver",
    "HcaptchaSolver",
]
