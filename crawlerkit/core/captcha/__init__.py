from .base import (
    CaptchaError,
    CaptchaNotImplementedError,
    CaptchaRegistry,
    CaptchaServiceError,
    CaptchaTimeoutError,
    CaptchaUnsolvedError,
    Challenge,
    ChallengeEngineError,
    InteractiveChallengeError,
    Solved,
    UnsupportedCaptcha,
    default_registry,
)
from .govbr import GovBrSolver
from .llm_image import LlmImageSolver
from .mcaptcha import McaptchaPowSolver, mcaptcha_hint
from .token_adapters import HcaptchaSolver, RecaptchaV2Solver, RecaptchaV3Solver, TokenProvider
from .turnstile import TurnstileSolver, turnstile_hint

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
    "InteractiveChallengeError",
    "ChallengeEngineError",
    "CaptchaRegistry",
    "default_registry",
    # own solvers
    "McaptchaPowSolver",
    "mcaptcha_hint",
    "LlmImageSolver",
    # browserless Turnstile (engine-backed; needs the native ext to mint tokens)
    "TurnstileSolver",
    "turnstile_hint",
    # browserless stub (TODO crack)
    "GovBrSolver",
    # optional token-adapters (opt-in)
    "TokenProvider",
    "RecaptchaV2Solver",
    "RecaptchaV3Solver",
    "HcaptchaSolver",
]
