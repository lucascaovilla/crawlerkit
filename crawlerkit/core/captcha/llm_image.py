"""Own image-captcha solver: fetch the challenge image over the verified transport, classify with
a pluggable vision LLM, return the answer/token. Provider-agnostic — inject a `classify` callable
`(image_bytes, prompt) -> str`. Prompts are intentionally minimal starting points.

Image captchas are target-specific, so the crawler builds the Challenge with the image location
(`params["image_url"]` or `params["image_bytes"]`); `detect()` returns None.
"""

from .base import CaptchaServiceError, Challenge, Solved

OCR_PROMPT = (
    "This image is a CAPTCHA. Read the characters exactly. Respond with ONLY the characters "
    "(letters/digits), no spaces, no explanation."
)
# 3x3 / 4x4 grid-selection prompts (hCaptcha / reCAPTCHA) — add more grid-size variants here
# as you wire up additional grid flows.
GRID_3X3_PROMPT = (
    "A reference image sits above a 3x3 grid (tiles numbered 1-9, left-to-right, top-to-bottom). "
    "Return the tile numbers that clearly and fully match the reference, separated by '/', e.g. '2/5/9'. "
    "If none match, return 'none'. No other text."
)


class LlmImageSolver:
    kind = "image"

    def __init__(self, classify, *, prompt: str = OCR_PROMPT):
        # classify: Callable[[bytes, str], str]
        self._classify = classify
        self._prompt = prompt

    @classmethod
    def detect(cls, text: str):
        return None  # the crawler constructs the image Challenge explicitly

    def solve(self, challenge: Challenge, transport) -> Solved:
        img = challenge.params.get("image_bytes")
        if img is None:
            url = challenge.params.get("image_url")
            if not url:
                raise CaptchaServiceError("LlmImageSolver needs params['image_url'] or ['image_bytes']")
            img = transport.get(url, timeout=30).content
        answer = (self._classify(img, challenge.params.get("prompt", self._prompt)) or "").strip()
        if not answer:
            raise CaptchaServiceError("vision LLM returned an empty answer")
        return Solved(token=answer)
