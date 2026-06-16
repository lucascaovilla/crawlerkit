"""gov.br (sso.acesso.gov.br) — BROWSERLESS solver scaffold.

gov.br SSO is a common gate for Brazilian government sites and is browser-only today.
`detect()` works now; `solve()` is a TODO for a manual, browserless crack — fails loudly.
"""

import re

from .base import CaptchaNotImplementedError, Challenge, Solved

_SIGNATURE = re.compile(r"sso\.acesso\.gov\.br|acesso\.gov\.br|\bgovbr\b", re.I)
_SITEKEY_RE = re.compile(r'data-sitekey=["\']([0-9A-Za-z_-]{8,})["\']')


class GovBrSolver:
    kind = "govbr"

    @classmethod
    def detect(cls, text: str):
        text = text or ""
        if not _SIGNATURE.search(text):
            return None
        m = _SITEKEY_RE.search(text)  # gov.br embeds hCaptcha/reCAPTCHA
        return Challenge(kind=cls.kind, params={"sitekey": m.group(1) if m else None})

    def solve(self, challenge: Challenge, transport) -> Solved:
        # TODO(crawlerkit): implement the BROWSERLESS gov.br SSO authentication.
        # gov.br (sso.acesso.gov.br) is JS-heavy and gated by a captcha (hCaptcha/reCAPTCHA) plus
        # fingerprint checks. Browserless approach to fill in here:
        #   1. Drive the SSO step sequence with the verified curl_cffi transport, carrying cookies
        #      across redirects (login -> authorize -> callback).
        #   2. Solve the embedded captcha via the registry (hCaptcha/reCAPTCHA token solver) OR a
        #      JS-runtime crack of the gov.br challenge script (QuickJS/Node + DOM shim seeded from
        #      the active Profile + proxy IP).
        #   3. Complete the OAuth/SSO redirect; return Solved(token=<session cookie / SSO assertion>).
        # Note: some gov.br services accept ICP-Brasil mutual-TLS client certs — see crawlerkit.core.tls.
        raise CaptchaNotImplementedError(
            f"browserless gov.br solve is a TODO (params={challenge.params!r}) "
            "— implement the SSO/JS-runtime crack"
        )
