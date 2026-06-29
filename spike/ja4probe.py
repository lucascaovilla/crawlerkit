"""Is the default_headers=False JA4 change a real regression or per-connection shuffle noise?
Run each mode 3x; if a mode's JA4 is stable but differs between modes -> real. If it varies within a
mode -> noise (Chrome shuffles extensions; JA4 should normalize but tls.peet may not)."""
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport
from crawlerkit.core.captcha._turnstile.node_engine import _chrome_ordered_headers

URL = "https://tls.peet.ws/api/all"
p = pick(); t = Transport(p, NullProxyProvider().lease(), timeout=20.0, enable_logs=False)
sess = t._session
sidecar = {"Referer": "https://x/", "Sec-Fetch-Dest": "iframe", "Sec-Fetch-Mode": "navigate",
           "Sec-Fetch-Site": "cross-site", "sec-ch-ua-arch": '"x86"'}
ordered = _chrome_ordered_headers(t, sidecar)

def ja4(**kw):
    return sess.get(URL, **kw).json().get("tls", {}).get("ja4")

print("DEFAULT (default_headers=True):")
for _ in range(3): print("  ", ja4())
print("CF-PATH (default_headers=False):")
for _ in range(3): print("  ", ja4(headers=ordered, default_headers=False))
