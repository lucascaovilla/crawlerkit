#!/usr/bin/env python3
"""Live instrumentation run for the Turnstile engine — harvest, not a pass/fail gate.

Primes the Detran PA session (home -> form), parses the live widget, then runs the pythonmonkey
engine against the real challenge. The engine fetches api.js + the challenge-platform endpoints
through the transport, so this exercises the WHOLE path. On no-token it raises CaptchaTimeoutError
whose message carries the request sequence + the unstubbed property accesses + any render error —
that output is the evidence the env/bridge are filled from. Direct egress; use the 3.11 venv.

    .venv/bin/python scripts/harvest_turnstile.py
"""

import sys

from crawlerkit.core.captcha._turnstile import engine, fingerprint
from crawlerkit.core.captcha._turnstile.widget import parse_widget
from crawlerkit.core.captcha.base import (
    CaptchaTimeoutError,
    ChallengeEngineError,
    InteractiveChallengeError,
)
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport

FORM_URL = (
    "https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/"
    "servicos/infracao/indexConsultaInfracao.jsf"
)


def main() -> int:
    profile = pick()
    t = Transport(profile, NullProxyProvider().lease(), timeout=20.0, enable_logs=True)
    print(f"[harvest] impersonate={profile.impersonate}")
    # prime: home sets veio_da_home, then the form serves the real ~7KB page.
    t.get("https://www.detran.pa.gov.br/")
    r = t.get(FORM_URL, headers={"Referer": "https://www.detran.pa.gov.br/"})
    html = r.text
    widget = parse_widget(html)
    print(f"[harvest] form status={r.status_code} bytes={len(html)} sitekey={widget.sitekey} "
          f"action={widget.action!r} interactive={widget.interactive}")
    if not widget.sitekey:
        print("[harvest] no sitekey in form HTML — prime/route failed, aborting")
        return 2

    fp = fingerprint.derive(profile)
    try:
        token = engine.run_challenge(
            page_url=FORM_URL, fingerprint=fp, widget=widget, transport=t, timeout=20.0
        )
        print(f"[harvest] *** TOKEN MINTED *** {token[:40]}... (len={len(token)})")
        return 0
    except InteractiveChallengeError as e:
        print(f"[harvest] INTERACTIVE escalation (expected fallback): {e}")
        return 1
    except CaptchaTimeoutError as e:
        print(f"[harvest] TIMEOUT (evidence below):\n{e}")
        return 1
    except ChallengeEngineError as e:
        print(f"[harvest] ENGINE ERROR (evidence):\n{e}")
        return 1
    finally:
        t.close() if hasattr(t, "close") else None


if __name__ == "__main__":
    sys.exit(main())
