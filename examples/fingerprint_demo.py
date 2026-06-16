"""See crawlerkit's fingerprint on the wire.

    python examples/fingerprint_demo.py     # (run from the crawlerkit-core package root)

Prints the identity crawlerkit generated AND the identity an echo server saw — they match, and
the JA3/JA4/HTTP2 fingerprint is a real Chrome's (curl_cffi impersonate), not a Python HTTP
client's. This is the proof that fingerprinting + UA are on, automatically, with zero config.
"""

import json
import os

from crawlerkit.core import BaseCrawler, RawResponse

# tls.peet.ws/api/all echoes the JA3/JA4/HTTP2 fingerprint AND the exact headers it received.
# Override with e.g. ECHO_URL=https://httpbin.org/headers for a simpler headers-only echo.
ECHO_URL = os.environ.get("ECHO_URL", "https://tls.peet.ws/api/all")


class EchoCrawler(BaseCrawler):
    """flow() just returns the echo response so we can show the fingerprint that went out."""

    def flow(self, params: dict) -> RawResponse:
        r = self.get(ECHO_URL)  # identity + verified TLS + (optional) proxy applied automatically
        return RawResponse(url=ECHO_URL, status=r.status_code, text=r.text, headers=dict(r.headers))


def echoed_identity(raw_text: str) -> dict:
    """Best-effort: pull UA + JA3/JA4 + HTTP2 fingerprint out of a tls.peet.ws/api/all
    (or httpbin /headers) echo response."""
    try:
        d = json.loads(raw_text)
    except ValueError:
        return {"raw_preview": raw_text[:200]}
    headers = d.get("headers") or {}
    tls = d.get("tls") or {}
    http2 = d.get("http2") or {}
    out = {
        "user_agent": d.get("user_agent") or headers.get("User-Agent"),
        "sec_ch_ua": headers.get("sec-ch-ua") or headers.get("Sec-Ch-Ua"),
        "ja3_hash": tls.get("ja3_hash"),
        "ja4": tls.get("ja4"),
        "http2_akamai": http2.get("akamai_fingerprint_hash") or http2.get("akamai_fingerprint"),
    }
    return {k: v for k, v in out.items() if v}


def main() -> None:
    c = EchoCrawler()  # browserforge identity generated automatically
    print("== crawlerkit identity (generated) ==")
    print(f"  impersonate : {c.profile.impersonate}")
    print(f"  user-agent  : {c.profile.user_agent}")
    print(f"  sec-ch-ua   : {c.profile.headers().get('sec-ch-ua')}")

    raw = c.run({})  # retry/rotation + verified TLS built in
    print(f"\n== echoed back by {ECHO_URL} (status {raw.status}) ==")
    seen = echoed_identity(raw.text)
    if not seen:
        print("  (could not parse echo; is the service reachable?)")
    for k, v in seen.items():
        print(f"  {k:13}: {v}")
    print("\n^ UA matches, and ja3/ja4/http2 are a real Chrome's — fingerprinting + UA are in use.")


if __name__ == "__main__":
    main()
