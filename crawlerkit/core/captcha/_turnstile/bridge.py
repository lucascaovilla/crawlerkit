"""Route the challenge's network calls back through the crawler's `Transport`.

The whole point of the browserless solver is that Cloudflare sees ONE consistent client: the
challenge's own requests (load api.js, POST the challenge-platform `flow`/`pat` endpoints, beacon
telemetry) must ride the SAME curl_cffi session as the page fetch — same JA3/HTTP2, same proxy,
same cookie jar. So we replace the JS engine's built-in networking (pythonmonkey ships an
aiohttp-backed `XMLHttpRequest`/`fetch` that would leak a Python TLS fingerprint and a separate
cookie store) with shims that call straight into `transport`.

pythonmonkey lets JS call a Python callable synchronously, and curl_cffi is itself synchronous, so
the bridge is a plain blocking call: JS `fetch(url, opts)` -> Python `request()` -> a Response-like
JS object. Cookies set by any response land in `transport._session.cookies.jar` and are replayed on
the next request automatically (shared session), which is exactly the browser behaviour Cloudflare
checks for. Every request is logged so an instrumentation run can dump the exact challenge sequence.
"""

import base64
import json

# Content types we hand to JS as text; everything else goes back base64 (arrayBuffer path).
_TEXT_CT = ("text/", "application/json", "application/javascript", "text/javascript",
            "application/x-www-form-urlencoded", "+json", "image/svg")


def _is_text(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(m in ct for m in _TEXT_CT)


class _RequestBridge:
    """Python side of the bridge. Holds the transport + a request log; JS calls `.do_request`."""

    def __init__(self, transport, log):
        self.transport = transport
        self.log = log
        self.requests: list[dict] = []

    def do_request(self, method: str, url: str, headers_json: str, body):
        """Invoked from JS. Returns a JSON string the JS shim parses into a Response.

        Args come from JS as JS values; pythonmonkey hands us str for strings. `body` may be a
        JS string or None. `headers_json` is a JSON object string (possibly "null")."""
        method = (method or "GET").upper()
        try:
            headers = json.loads(headers_json) if headers_json and headers_json != "null" else {}
        except (ValueError, TypeError):
            headers = {}

        kw: dict = {}
        if headers:
            kw["headers"] = {str(k): str(v) for k, v in headers.items()}
        if body is not None and method not in ("GET", "HEAD"):
            kw["data"] = body if isinstance(body, (bytes, bytearray)) else str(body)

        self.requests.append({"method": method, "url": url})
        if self.log:
            self.log("bridge.request", method=method, url=url)

        resp = self.transport.request(method, url, **kw)

        ct = resp.headers.get("content-type", "") if resp.headers else ""
        if _is_text(ct):
            body_out, is_b64 = resp.text, False
        else:
            body_out, is_b64 = base64.b64encode(resp.content).decode("ascii"), True

        return json.dumps({
            "status": resp.status_code,
            "statusText": getattr(resp, "reason", "") or "",
            "url": str(getattr(resp, "url", url)),
            "headers": {k.lower(): v for k, v in (resp.headers.items() if resp.headers else [])},
            "body": body_out,
            "isBase64": is_b64,
        })


# JS shims: a fetch() returning a Response-like object, and an XMLHttpRequest class, both backed by
# the single Python `__pmRequest` callable. Installed over pythonmonkey's own (network-leaking) ones.
_SHIM_JS = r"""
'use strict';
(function (rawRequest) {
  function call(method, url, headers, body) {
    // rawRequest is synchronous (curl_cffi blocks); JSON in / JSON out.
    return JSON.parse(rawRequest(method || 'GET', String(url), headers ? JSON.stringify(headers) : 'null', body == null ? null : String(body)));
  }
  function decodeBody(r) {
    if (!r.isBase64) return r.body;
    const bin = atob(r.body); const u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return u8;
  }
  function makeHeaders(obj) {
    return { get: (k) => (k && k.toLowerCase() in obj) ? obj[k.toLowerCase()] : null,
             has: (k) => k && k.toLowerCase() in obj,
             forEach: (cb) => { for (const k in obj) cb(obj[k], k); } };
  }
  globalThis.fetch = function (url, opts) {
    opts = opts || {};
    return new Promise((resolve, reject) => {
      try {
        const r = call(opts.method, (url && url.url) ? url.url : url, opts.headers, opts.body);
        const textBody = r.isBase64 ? null : r.body;
        resolve({
          ok: r.status >= 200 && r.status < 300, status: r.status, statusText: r.statusText,
          url: r.url, redirected: false, headers: makeHeaders(r.headers),
          text: () => Promise.resolve(textBody == null ? '' : textBody),
          json: () => Promise.resolve(JSON.parse(textBody)),
          arrayBuffer: () => Promise.resolve(decodeBody(r).buffer),
          clone() { return this; },
        });
      } catch (e) { reject(e); }
    });
  };
  function XHR() {
    this.readyState = 0; this.status = 0; this.responseText = ''; this.response = '';
    this._method = 'GET'; this._url = ''; this._headers = {}; this._respHeaders = {};
    this.onreadystatechange = null; this.onload = null; this.onerror = null;
    this.withCredentials = false; this.timeout = 0;
  }
  XHR.prototype.open = function (m, u) { this._method = m; this._url = u; this.readyState = 1; if (this.onreadystatechange) this.onreadystatechange(); };
  XHR.prototype.setRequestHeader = function (k, v) { this._headers[k] = v; };
  XHR.prototype.getResponseHeader = function (k) { return this._respHeaders[String(k).toLowerCase()] || null; };
  XHR.prototype.getAllResponseHeaders = function () { return Object.entries(this._respHeaders).map(([k, v]) => k + ': ' + v).join('\r\n'); };
  XHR.prototype.abort = function () {};
  XHR.prototype.send = function (body) {
    try {
      const r = call(this._method, this._url, this._headers, body);
      this.status = r.status; this.statusText = r.statusText; this._respHeaders = r.headers;
      this.responseText = r.isBase64 ? '' : r.body; this.response = this.responseText;
      this.responseURL = r.url; this.readyState = 4;
      if (this.onreadystatechange) this.onreadystatechange();
      if (this.onload) this.onload();
    } catch (e) { if (this.onerror) this.onerror(e); else throw e; }
  };
  globalThis.XMLHttpRequest = XHR;
  // sendBeacon is fire-and-forget telemetry; route it but ignore the result.
  if (!globalThis.navigator) globalThis.navigator = {};
  globalThis.navigator.sendBeacon = function (url, data) { try { call('POST', url, null, data); return true; } catch (e) { return false; } };
})(globalThis.__pmRequest);
"""


def install(pm, transport, *, log=None) -> _RequestBridge:
    """Wire transport-backed `fetch`/`XMLHttpRequest`/`sendBeacon` into the pythonmonkey global.

    Returns the `_RequestBridge` so the caller can read `.requests` (the captured sequence)."""
    bridge = _RequestBridge(transport, log)
    pm.eval("(function (fn) { globalThis.__pmRequest = fn; })")(bridge.do_request)
    pm.eval(_SHIM_JS)
    return bridge
