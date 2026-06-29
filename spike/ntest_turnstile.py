"""Durable Turnstile sidecar harness (untracked; survives session-scratchpad rotation).

Run:  CF_SIDECAR_DEBUG=1 CF_TIMEOUT=18 [CF_ACCESS_LOG=/path] \
      PYTHONPATH=/home/caovilla/Projects/crawlerkit .venv/bin/python spike/ntest_turnstile.py
"""
import os
from crawlerkit.core.captcha._turnstile import node_engine, fingerprint
from crawlerkit.core.captcha._turnstile.widget import parse_widget
from crawlerkit.core.captcha.base import CaptchaTimeoutError, ChallengeEngineError, InteractiveChallengeError
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport

FORM_URL = ("https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/"
            "servicos/infracao/indexConsultaInfracao.jsf")


def main():
    profile = pick()
    t = Transport(profile, NullProxyProvider().lease(),
                  timeout=float(os.environ.get("TRANSPORT_TIMEOUT", "20")), enable_logs=False)
    t.get("https://www.detran.pa.gov.br/")
    r = t.get(FORM_URL, headers={"Referer": "https://www.detran.pa.gov.br/"})
    html = r.text
    widget = parse_widget(html)
    print(f"[ntest] form bytes={len(html)} sitekey={widget.sitekey} interactive={widget.interactive}")
    fp = fingerprint.derive(profile)
    try:
        token = node_engine.run_challenge(page_url=FORM_URL, fingerprint=fp, widget=widget,
                                          transport=t, form_html=html,
                                          timeout=float(os.environ.get("CF_TIMEOUT", "25")))
        print(f"[ntest] *** TOKEN *** {token[:48]}... len={len(token)}")
    except (CaptchaTimeoutError, ChallengeEngineError, InteractiveChallengeError) as e:
        print(f"[ntest] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
