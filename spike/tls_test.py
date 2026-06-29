"""Run the WHOLE Turnstile challenge through bogdanfinn tls-client (non-headless Chrome TLS) instead of
curl_cffi, to test whether higher TLS-engine fidelity flips us from the hard path to the easy path.

Adapter exposes the minimal `transport` interface node_engine needs (request / _session.request with a
streamed-in-one-chunk response / cookies). Easy path needs no streaming, so this suffices."""
import os
import tls_client
from crawlerkit.core.identity import Profile, pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport
from crawlerkit.core.captcha._turnstile import node_engine, fingerprint
from crawlerkit.core.captcha._turnstile.widget import parse_widget
from crawlerkit.core.captcha.base import CaptchaTimeoutError, ChallengeEngineError, InteractiveChallengeError

CID = os.environ.get("TLS_CID", "chrome_138")
MAJOR = CID.split("_")[1]
UA = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{MAJOR}.0.0.0 Safari/537.36"
BASE = {
    "sec-ch-ua": f'"Google Chrome";v="{MAJOR}", "Not.A/Brand";v="8", "Chromium";v="{MAJOR}"',
    "sec-ch-ua-mobile": "?0", "sec-ch-ua-platform": '"Linux"',
    "User-Agent": UA, "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
}
FORM_URL = ("https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/"
            "servicos/infracao/indexConsultaInfracao.jsf")


class _Resp:
    def __init__(self, r):
        self.status_code = r.status_code
        self.headers = dict(r.headers or {})
        c = r.content
        self.content = c if isinstance(c, (bytes, bytearray)) else (r.text or "").encode("utf-8", "replace")
        self.url = r.url
        self.reason = ""

    def iter_content(self, chunk_size=16384):
        yield self.content

    def close(self):
        pass


class TlsTransport:
    def __init__(self, cid):
        self.s = tls_client.Session(client_identifier=cid, random_tls_extension_order=True)
        self.profile = Profile(impersonate=cid, _headers=dict(BASE))
        self._session = self  # node_engine uses transport._session.request(...)

    def _merge(self, headers):
        m = dict(BASE)
        for k, v in (headers or {}).items():
            m[k] = v
        return m

    def request(self, method, url, headers=None, data=None, timeout=None, stream=False, **kw):
        r = self.s.execute_request(method=str(method).upper(), url=url,
                                   headers=self._merge(headers), data=data, allow_redirects=True)
        return _Resp(r)

    def get(self, url, headers=None, **kw):
        return self.request("GET", url, headers=headers)


def main():
    # prime + form via the working curl_cffi Transport (it does the AIA cert repair detran needs); the
    # Turnstile challenge itself only hits challenges.cloudflare.com (normal chain) and runs via tls-client.
    prof = pick()
    t1 = Transport(prof, NullProxyProvider().lease(), timeout=20.0, enable_logs=False)
    t1.get("https://www.detran.pa.gov.br/")
    r = t1.get(FORM_URL, headers={"Referer": "https://www.detran.pa.gov.br/"})
    html = r.text
    print(f"[tls] form status={r.status_code} bytes={len(html)}")
    widget = parse_widget(html)
    print(f"[tls] sitekey={widget.sitekey} interactive={widget.interactive}")

    t = TlsTransport(CID)
    print(f"[tls] challenge engine client={CID}")
    fp = fingerprint.derive(t.profile)
    try:
        token = node_engine.run_challenge(page_url=FORM_URL, fingerprint=fp, widget=widget,
                                          transport=t, form_html=html,
                                          timeout=float(os.environ.get("CF_TIMEOUT", "20")))
        print(f"[tls] *** TOKEN *** {token[:48]}... len={len(token)}")
    except (CaptchaTimeoutError, ChallengeEngineError, InteractiveChallengeError) as e:
        print(f"[tls] {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
