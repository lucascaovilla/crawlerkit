"""One-off live capture of the Detran PA form page (now Cloudflare Turnstile).

Does ONLY the GET-prime step: fetches the form, saves the live HTML + cookie jar + the detected
sitekey to tests/fixtures/turnstile/. This is the offline fixture AND the phase-0 spike input.
No token solve (engine not built yet). Direct egress.

    python scripts/capture_turnstile.py
"""

import json
import pathlib
import sys

from crawlerkit.core.captcha._turnstile.widget import parse_widget
from crawlerkit.core.captcha.turnstile import TurnstileSolver
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport

FORM_URL = (
    "https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/"
    "servicos/infracao/indexConsultaInfracao.jsf"
)
FORM_ORIGIN = "https://sistemas-renavam.detran.pa.gov.br"
OUT = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "turnstile"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = pick()
    t = Transport(profile, NullProxyProvider().lease(), timeout=30.0)
    print(f"[capture] impersonate={profile.impersonate} UA={profile.user_agent[:60]}...")
    # PRIME: the .jsf form 302s to the portal unless the session already holds `veio_da_home`,
    # which the portal home sets. GET the home first, THEN the form serves real HTML (~7KB).
    t.get("https://www.detran.pa.gov.br/")
    r = t.get(FORM_URL, headers={"Referer": "https://www.detran.pa.gov.br/"})
    html = r.text

    (OUT / "detran_pa_form.html").write_text(html, encoding="utf-8")
    cookies = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in t._session.cookies.jar
    ]
    (OUT / "detran_pa_form.cookies.json").write_text(json.dumps(cookies, indent=2), encoding="utf-8")

    ch = TurnstileSolver.detect(html)
    w = parse_widget(html)
    print(f"[capture] status={r.status_code} bytes={len(html)}")
    print(f"[capture] turnstile detected={ch is not None} sitekey={ch.params.get('sitekey') if ch else None}")
    print(f"[capture] widget action={w.action!r} cdata={w.cdata!r} interstitial={w.interactive}")
    print(f"[capture] cookies={[c['name'] for c in cookies]}")
    print(f"[capture] saved -> {OUT}")
    # quick signal of what kind of page came back
    low = html.lower()
    for marker in ("cf-turnstile", "challenges.cloudflare.com", "just a moment", "_cf_chl_opt", "mcaptcha"):
        if marker in low:
            print(f"[capture] marker present: {marker}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
