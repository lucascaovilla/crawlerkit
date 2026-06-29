"""Live Turnstile end-to-end test — Detran-PA infraction consult (in-repo, was poc-infra-pa).

This is the durable "turnstile test": prime cookie -> fetch the form -> solve the Cloudflare
Turnstile on the SAME Transport session (cookies/JA3/proxy shared with the challenge requests) ->
POST placa/renavam + the minted token. The whole pipeline except the solver engine is proven; it
consults the moment the browserless engine mints a server-valid `cf-turnstile-response`.

    # the browserless Node+jsdom+V8 sidecar engine under test:
    CRAWLERKIT_TURNSTILE_ENGINE=node python examples/detran_pa.py --data plate=SZT3I75 renavam=01433191358

Success = "consulted OK — status=200 …"   Failure = "CAPTCHA FAIL: …" (the solver raised).
Needs crawlerkit-core installed (editable: `uv pip install -e ".[dev]"`). Egress is direct.
Hard rule: NO browser/CDP/Selenium and NO third-party solver in this path — browserless + independent.
"""

from crawlerkit.core import BaseCrawler, RawResponse
from crawlerkit.core.captcha import turnstile_hint

PORTAL_HOME = "https://www.detran.pa.gov.br/"
FORM_URL = (
    "https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/"
    "servicos/infracao/indexConsultaInfracao.jsf"
)
FORM_ORIGIN = "https://sistemas-renavam.detran.pa.gov.br"


class DetranPA(BaseCrawler):
    """The whole target-specific crawl is flow(): prime -> form -> solve Turnstile -> POST."""

    def flow(self, params: dict) -> RawResponse:
        # 1. PRIME: portal home sets `veio_da_home`; without it the .jsf 302s to the portal.
        self.get(PORTAL_HOME)
        # 2. FORM: now serves the real ~7KB form with the Turnstile widget.
        page = self.get(FORM_URL, headers={"Referer": PORTAL_HOME})
        # 3. SOLVE: the hint carries page_url + the live HTML; the solver's challenge requests reuse
        #    this primed session (same cookies/JA3/proxy as the page fetch).
        token = self.solve_captcha(page.text, hint=turnstile_hint(page_url=FORM_URL, html=page.text))
        # 4. SUBMIT: hidden fields (ViewState) + placa/renavam + the Turnstile token.
        data = self.hidden_fields(page.text) | {
            "indexForm": "indexForm",
            "placa": params["placa"],
            "renavam": params["renavam"],
            "cf-turnstile-response": token,
            "confirma": "Confirmar",
        }
        pr = self.post(FORM_URL, data=data, headers={"Origin": FORM_ORIGIN, "Referer": FORM_URL})
        return RawResponse(url=FORM_URL, status=pr.status_code, text=pr.text, headers=dict(pr.headers))


def _parse_data(pairs: list[str]) -> dict:
    out: dict[str, str] = {}
    for p in pairs:
        key, _, value = p.partition("=")
        out[key.strip()] = value.strip()
    if "plate" in out and "placa" not in out:  # CLI says `plate`; flow() expects `placa`
        out["placa"] = out.pop("plate")
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    from crawlerkit.core.captcha import CaptchaError

    ap = argparse.ArgumentParser(description="Run the Detran-PA infraction crawl once (live Turnstile).")
    ap.add_argument("--data", nargs="+", required=True, metavar="KEY=VALUE",
                    help="e.g. --data plate=SZT3I75 renavam=01433191358")
    ap.add_argument("--timeout", type=float, default=40.0, help="per-request timeout (s)")
    ap.add_argument("--verbose", action="store_true", help="emit the request/solve logs")
    args = ap.parse_args(argv)

    params = _parse_data(args.data)
    if not params.get("placa") or not params.get("renavam"):
        ap.error("need plate=<placa> and renavam=<renavam> in --data")

    if args.verbose:
        DetranPA.enable_logs = True  # class attr the transport reads when it's built
    crawler = DetranPA(timeout=args.timeout)
    try:
        raw = crawler.run(params)
    except CaptchaError as e:
        print(f"CAPTCHA FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    finally:
        crawler.close()

    print(f"consulted OK — status={raw.status} bytes={len(raw.text)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
