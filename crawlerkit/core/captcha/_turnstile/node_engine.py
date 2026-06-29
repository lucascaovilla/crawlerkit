"""Drive the Node+jsdom+V8 Turnstile sidecar from Python.

The sidecar (`sidecar/sidecar.mjs`) runs Cloudflare's `api.js` + the challenge iframe VM in real V8 over
a jsdom DOM — browserless, but with a battle-tested DOM/shadow-DOM and V8 engine semantics the
hand-stubbed pythonmonkey env couldn't match. All of the challenge's network (`fetch`/`XMLHttpRequest`/
`sendBeacon`) is proxied back here over stdio and executed on the crawler's curl_cffi `Transport`, so
Cloudflare still sees ONE client: same JA3/HTTP2/cookies/proxy as the page fetch.

Protocol (newline-delimited JSON, bodies base64): Python sends a `config` line, then services `net`
requests with `net-res`/`net-err`; the sidecar finishes with `result` (token), `interactive`, `error`,
or `timeout`. Engine choice stays behind this module + `engine.py` so `TurnstileSolver.solve` is frozen.
"""

import base64
import json
import os
import select
import shutil
import subprocess
import threading
import time

from ..base import CaptchaTimeoutError, ChallengeEngineError, InteractiveChallengeError

API_JS_URL = "https://challenges.cloudflare.com/turnstile/v0/api.js"
_SIDECAR = os.path.join(os.path.dirname(__file__), "sidecar", "sidecar.mjs")


def _find_node() -> str:
    node = shutil.which("node")
    if not node:
        raise ChallengeEngineError(
            "node (the Turnstile jsdom sidecar runtime) is not on PATH — install Node.js >= 18"
        )
    return node


class _Reader:
    """Newline-delimited reader over a subprocess pipe with an absolute deadline (select-based)."""

    def __init__(self, fileobj):
        self._f = fileobj
        self._buf = b""

    def readline(self, deadline: float):
        while b"\n" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            r, _, _ = select.select([self._f], [], [], remaining)
            if not r:
                return None
            chunk = os.read(self._f.fileno(), 65536)
            if not chunk:  # EOF
                if self._buf:
                    line, self._buf = self._buf, b""
                    return line
                return b""  # distinct from None (timeout): EOF
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b"\n")
        return line


def _do_request(transport, method, url, headers, body_b64):
    method = (method or "GET").upper()
    kw: dict = {}
    if headers:
        kw["headers"] = {str(k): str(v) for k, v in headers.items()}
    body = None
    if body_b64 and method not in ("GET", "HEAD"):
        body = base64.b64decode(body_b64)
        kw["data"] = body
    _dbg = os.environ.get("CF_SIDECAR_DEBUG")
    _flow = "/flow/" in url
    if _dbg and _flow:
        _h = kw.get("headers") or {}
        _ct = _h.get("content-type") or _h.get("Content-Type")
        print(f"[flow-req] {method} ct={_ct!r} bodyLen={len(body) if body else 0} hdrKeys={list(_h.keys())}")
    _t0 = time.monotonic()
    try:  # bound the request so a hanging/streaming flow endpoint can't block forever
        resp = transport.request(method, url, timeout=15, **kw)
    except TypeError:
        resp = transport.request(method, url, **kw)
    if _dbg and _flow:
        print(f"[flow-resp] status={resp.status_code} elapsed={time.monotonic() - _t0:.1f}s len={len(resp.content or b'')}")
    return {
        "status": resp.status_code,
        "statusText": getattr(resp, "reason", "") or "",
        "url": str(getattr(resp, "url", url)),
        "headers": {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])},
        "bodyB64": base64.b64encode(resp.content).decode("ascii"),
    }


def _stream_request(transport, msg, send_line):
    """Run one request STREAMING and emit net-head -> net-chunk* -> net-end (or net-err) so the sidecar's
    XHR can fire incremental `readyState=3` events. The challenge's `…/flow/…` endpoint is a long-poll
    that trickles its body; a blocking `.content` read never returns and the VM overruns. Streaming hands
    the VM each chunk as it arrives, exactly like a real browser, so it can act on the first one."""
    rid = msg["id"]
    method = (msg.get("method") or "GET").upper()
    url = msg["url"]
    kw: dict = {}
    h = msg.get("headers")
    if h:
        kw["headers"] = {str(k): str(v) for k, v in h.items()}
    # NB: curl_cffi default_headers=False is a TRAP here — it stops merging our session sec-ch-ua and lets
    # curl-impersonate's C-level identity leak as `sec-ch-ua: "HeadlessChrome"` (the lib is built from
    # headless chromium). Far worse than the minor header-order/Sec-Fetch-User imperfections it would fix.
    # Keep default_headers=True so our "Google Chrome" sec-ch-ua + exact JA4/Akamai win.
    if msg.get("bodyB64") and method not in ("GET", "HEAD"):
        kw["data"] = base64.b64decode(msg["bodyB64"])
    sess = getattr(transport, "_session", None)
    try:
        if sess is not None:
            resp = sess.request(method, url, stream=True, **kw)
        else:  # no streaming session available — fall back to a single buffered response
            r = _do_request(transport, method, url, h, msg.get("bodyB64"))
            send_line({"t": "net-head", "id": rid, "status": r["status"], "statusText": r["statusText"],
                       "url": r["url"], "headers": r["headers"]})
            send_line({"t": "net-chunk", "id": rid, "dataB64": r["bodyB64"]})
            send_line({"t": "net-end", "id": rid})
            return
        headers = {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])}
        send_line({"t": "net-head", "id": rid, "status": resp.status_code,
                   "statusText": getattr(resp, "reason", "") or "", "url": str(getattr(resp, "url", url)),
                   "headers": headers})
        n = 0
        _t0 = time.monotonic()
        _flowdbg = os.environ.get("CF_SIDECAR_DEBUG") and "/flow/" in url
        for chunk in resp.iter_content(chunk_size=16384):
            if chunk:
                if _flowdbg and n == 0:
                    print(f"[flow-stream] first chunk @ {time.monotonic() - _t0:.1f}s ({len(chunk)}B) status={resp.status_code}")
                n += len(chunk)
                send_line({"t": "net-chunk", "id": rid, "dataB64": base64.b64encode(chunk).decode("ascii")})
        try:
            resp.close()
        except Exception:
            pass
        if _flowdbg:
            print(f"[flow-stream] DONE {method} status={resp.status_code} bytes={n} elapsed={time.monotonic() - _t0:.1f}s")
        send_line({"t": "net-end", "id": rid})
    except Exception as e:
        send_line({"t": "net-err", "id": rid, "msg": f"{type(e).__name__}: {e}"})


def _fp_dict(fp):
    """Whitelist the fingerprint fields the sidecar needs to shape navigator/screen (jsonable)."""
    keys = ("user_agent", "platform", "language", "languages", "hardware_concurrency", "device_memory",
            "screen_width", "screen_height", "avail_width", "avail_height", "color_depth",
            "pixel_ratio", "timezone", "vendor", "webgl_vendor", "webgl_renderer")
    out = {}
    for k in keys:
        v = getattr(fp, k, None)
        if v is not None:
            out[k] = v
    return out


def run_challenge(*, page_url, fingerprint, widget, transport, form_html="", timeout: float = 30.0) -> str:
    """Run the challenge in the Node sidecar; return a `cf-turnstile-response`. Same error contract as
    the pythonmonkey engine (`ChallengeEngineError`/`InteractiveChallengeError`/`CaptchaTimeoutError`)."""
    node = _find_node()
    cfg = {
        "t": "config",
        "pageUrl": page_url,
        "apiJsUrl": API_JS_URL,
        "formHtmlB64": base64.b64encode((form_html or "").encode("utf-8")).decode("ascii"),
        "sitekey": getattr(widget, "sitekey", None),
        "action": getattr(widget, "action", None),
        "cdata": getattr(widget, "cdata", None),
        "fingerprint": _fp_dict(fingerprint),
        "timeoutMs": int(timeout * 1000),
        "debug": bool(os.environ.get("CF_SIDECAR_DEBUG")),
    }
    proc = subprocess.Popen([node, _SIDECAR], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    reader = _Reader(proc.stdout)
    requests: list[dict] = []
    write_lock = threading.Lock()

    def send_line(obj):  # serialize stdin writes (the request threads + the main loop all write)
        data = (json.dumps(obj) + "\n").encode("utf-8")
        with write_lock:
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except Exception:
                pass

    try:
        send_line(cfg)
        deadline = time.monotonic() + float(timeout) + 8.0
        while True:
            line = reader.readline(deadline)
            if line is None:
                raise CaptchaTimeoutError(
                    f"sidecar produced no token within {timeout}s (sitekey="
                    f"{getattr(widget, 'sitekey', None)}); requests={[r['url'] for r in requests]}")
            if line == b"":  # EOF
                err = (proc.stderr.read() or b"").decode("utf-8", "replace")[-2000:]
                raise ChallengeEngineError(f"Turnstile sidecar exited early. stderr:\n{err}")
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # stray non-JSON line (defensive)
            t = msg.get("t")
            if t == "net":
                requests.append({"method": msg.get("method"), "url": msg.get("url")})
                # each request streams on its own thread: a long-poll/streaming endpoint must not block the
                # message loop or other requests (a real browser runs them concurrently + incrementally).
                threading.Thread(target=_stream_request, args=(transport, msg, send_line), daemon=True).start()
            elif t == "result":
                token = str(msg.get("token") or "")
                if not token:
                    raise CaptchaTimeoutError("sidecar returned an empty token")
                return token
            elif t == "interactive":
                raise InteractiveChallengeError(
                    "Turnstile escalated to an interactive challenge — out of scope for the browserless "
                    "solver; route to a fallback", sitekey=getattr(widget, "sitekey", None),
                    page_url=page_url)
            elif t == "timeout":
                raise CaptchaTimeoutError(str(msg.get("detail") or "sidecar timeout"))
            elif t == "error":
                raise ChallengeEngineError(
                    f"Turnstile sidecar error: {msg.get('msg')}", sitekey=getattr(widget, "sitekey", None),
                    page_url=page_url)
            elif t == "log":
                print(f"[sidecar] {msg.get('msg')}")  # debug visibility (only when sidecar emits)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
