"""Run the Cloudflare challenge iframe — where the token is actually minted.

api.js (the parent) only creates an `<iframe src=…/cdn-cgi/challenge-platform/…>` and listens for a
`message`. The real work lives in THAT iframe: its HTML embeds `window._cf_chl_opt` (config + an
encrypted blob) and a single self-running VM script that does one XHR to the challenge-platform, runs
the obfuscated proof-of-work/fingerprint check, and `window.parent.postMessage(<token payload>)`.

So we: fetch the iframe URL through the same `transport` (shared JA3/cookies), build a window for the
iframe out of the iframe element's own `contentWindow` (so the parent's `e.source ===
iframe.contentWindow` identity check passes), and run the VM with `window`/`self`/`parent`/`top`/
`document`/`location`/`globalThis` shadowed to the iframe's — the VM never touches the real
`globalThis` (verified: 0 refs; it uses `window.`/`window.parent.`), so one shared SpiderMonkey global
is enough, no realm isolation needed. The VM's `parent.postMessage` is captured two ways: delivered to
the page's real `message` listener (so api.js's own handler fires the widget callback -> `__token`) AND
sniffed directly for the token (`__frameToken`) as a fallback.
"""

import re

# inline <script>…</script> (no src) and <script src="…"> in document order.
_SCRIPT_RE = re.compile(r'<script\b([^>]*)>(.*?)</script>', re.I | re.S)
_SRC_RE = re.compile(r'\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)

# JS installed once: builds the iframe window and runs VM scripts inside it.
_FRAME_JS = r"""
'use strict';
globalThis.__frameToken = globalThis.__frameToken || null;
globalThis.__frameErr = globalThis.__frameErr || null;

// Heuristic token sniff: a Turnstile response looks like "<prefix>.<long base64url>".
globalThis.__extractToken = function (data) {
  var re = /^[\w-]+\.[\w./+-]{24,}$/;
  function scan(v, depth) {
    if (v == null || depth > 4) return null;
    if (typeof v === 'string') return re.test(v) ? v : null;
    if (typeof v === 'object') { for (var k in v) { try { var r = scan(v[k], depth + 1); if (r) return r; } catch (e) {} } }
    return null;
  }
  return scan(data, 0);
};

// The iframe window. Behaves like env.py's trapVoid (own prop -> value; miss -> void stub + __undef
// log; `has` always true) BUT additionally mirrors function-valued `window.X = fn` writes onto the real
// engine global. In a real page `window === globalThis`, so the VM's `window.runProgram = fn; … ;
// runProgram(...)` (bare call) just works; here `window` is a plain object, so without this bridge the
// unqualified call is a ReferenceError. Only functions are mirrored (the VM's bare-callable globals:
// runProgram + the trusted-types wrappers) — never data, to avoid polluting/clobbering engine globals.
globalThis.__makeFrameWindow = function () {
  return new Proxy({}, {
    get: function (t, p) {
      if (p in t) return t[p];
      if (typeof p === 'symbol') return undefined;
      globalThis.__undef.push('iframeWindow.' + String(p));
      return globalThis.__voidStub();
    },
    set: function (t, p, v) {
      t[p] = v;
      if (typeof v === 'function' && typeof p === 'string') { try { globalThis[p] = v; } catch (e) {} }
      return true;
    },
    has: function () { return true; },
  });
};

globalThis.__bootFrame = function (el, url) {
  var u = new URL(url);
  var cw = globalThis.__makeFrameWindow();
  globalThis.__installBus(cw);

  var ifloc = globalThis.__trapVoid('frameLoc', { href: url, origin: u.origin, protocol: u.protocol,
                host: u.host, hostname: u.hostname, port: u.port, pathname: u.pathname, search: u.search,
                hash: '', assign: function () {}, replace: function () {}, reload: function () {}, toString: function () { return url; } });

  // permissive iframe element factory: real element methods (appendChild/setAttribute/style/...) with
  // chainable no-op stubs for anything unmodelled, so the obfuscated VM's DOM walking never crashes.
  function fEl(t) { return globalThis.__trapVoid('frameEl', globalThis.__makeEl(t)); }
  var ifdocBase = {
    nodeType: 9, location: ifloc, URL: url, documentURI: url, domain: u.hostname,
    referrer: globalThis.__PAGE_URL || '', readyState: 'complete', visibilityState: 'visible',
    hidden: false, characterSet: 'UTF-8', charset: 'UTF-8', compatMode: 'CSS1Compat',
    head: fEl('head'), body: fEl('body'), documentElement: fEl('html'), title: '',
    styleSheets: { length: 0, item: function () { return null; } },
    forms: [], images: [], links: [], embeds: [], scripts: [], all: [], fonts: { ready: null, check: function () { return true; } },
    createElement: function (t) { return fEl(t); }, createElementNS: function (ns, t) { return fEl(t); },
    createTextNode: function (s) { return { nodeType: 3, textContent: s, data: s }; },
    createDocumentFragment: function () { return fEl('fragment'); },
    createEvent: function () { return { initEvent: function () {} }; },
    getElementsByTagName: function () { return []; }, getElementsByClassName: function () { return []; },
    // resolve created elements via the shared id registry; for a miss return a permissive stub (not
    // null) so the VM's `doc.querySelector(x).innerText` / `.innerHTML` reads don't throw on null.
    getElementById: function (id) { return globalThis.__byId(id) || globalThis.__voidStub(); },
    querySelector: function (s) { var r = (s && s.charAt(0) === '#') ? globalThis.__byId(s.slice(1)) : null; return r || globalThis.__voidStub(); },
    querySelectorAll: function () { return []; },
    addEventListener: function () {}, removeEventListener: function () {}, dispatchEvent: function () { return true; },
  };
  Object.defineProperty(ifdocBase, 'cookie', { get: function () { return globalThis.__cookieGet ? globalThis.__cookieGet() : ''; },
                                               set: function (v) { if (globalThis.__cookieSet) globalThis.__cookieSet(v); }, configurable: true });
  var ifdoc = globalThis.__trapVoid('frameDoc', ifdocBase);

  // parent handle: the iframe's window.parent. Capture the posted token, and ALSO deliver it to the
  // real page window so api.js's own 'message' handler runs (source MUST be this contentWindow).
  var parentHandle = globalThis.__trapVoid('frameParent', {
    postMessage: function (data, origin) {
      try { var t = globalThis.__extractToken(data); if (t && !globalThis.__frameToken) globalThis.__frameToken = t; } catch (e) {}
      // a real browser sets e.origin to the SENDER's actual origin (this iframe's), NOT the
      // targetOrigin arg the VM passed ('*'); api.js rejects '*' as "wrong origin".
      globalThis.__deliverMessage(globalThis, data, u.origin, cw);
    },
    location: globalThis.__trapVoid('parentLoc', { href: globalThis.__PAGE_URL || '', origin: globalThis.__ORIGIN || '' }),
    origin: globalThis.__ORIGIN || '', postMessageBus: true,
    addEventListener: function () {}, removeEventListener: function () {}, frameElement: null,
  });

  // populate the iframe window (reuse the page's navigator/screen/crypto/timers/network — same browser).
  cw.window = cw; cw.self = cw; cw.globalThis = cw; cw.parent = parentHandle; cw.top = parentHandle; cw.frameElement = el;
  cw.location = ifloc; cw.document = ifdoc; cw.origin = u.origin;
  // parent -> iframe: api.js replies via iframe.contentWindow.postMessage(...). The VM checks
  // `e.source === window.parent`, and the VM's window.parent is OUR parentHandle — so the event source
  // must be parentHandle (not the raw page window), or the VM drops api.js's replies.
  cw.postMessage = function (data, origin) {
    globalThis.__deliverMessage(cw, data, globalThis.__ORIGIN || '', parentHandle);
  };
  cw.navigator = globalThis.navigator; cw.screen = globalThis.screen; cw.crypto = globalThis.crypto;
  cw.trustedTypes = globalThis.trustedTypes;
  cw.performance = globalThis.__trapVoid('framePerf', { now: function () { return Date.now(); }, timeOrigin: Date.now(),
    getEntriesByType: function () { return []; }, getEntriesByName: function () { return []; }, getEntries: function () { return []; },
    mark: function () {}, measure: function () {}, clearMarks: function () {}, clearMeasures: function () {},
    timing: {}, memory: { usedJSHeapSize: 10000000, totalJSHeapSize: 20000000, jsHeapSizeLimit: 2190000000 } });
  cw.history = globalThis.__trapVoid('frameHist', { length: 1, state: null, scrollRestoration: 'auto',
    pushState: function () {}, replaceState: function () {}, go: function () {}, back: function () {}, forward: function () {} });
  cw.atob = globalThis.atob; cw.btoa = globalThis.btoa;
  cw.setTimeout = globalThis.setTimeout; cw.clearTimeout = globalThis.clearTimeout;
  cw.setInterval = globalThis.setInterval; cw.clearInterval = globalThis.clearInterval;
  cw.queueMicrotask = globalThis.queueMicrotask; cw.Promise = globalThis.Promise;
  cw.requestAnimationFrame = globalThis.requestAnimationFrame; cw.cancelAnimationFrame = globalThis.cancelAnimationFrame;
  cw.XMLHttpRequest = globalThis.XMLHttpRequest; cw.fetch = globalThis.fetch;
  cw.devicePixelRatio = globalThis.devicePixelRatio;
  cw.innerWidth = globalThis.innerWidth; cw.innerHeight = globalThis.innerHeight;
  cw.outerWidth = globalThis.outerWidth; cw.outerHeight = globalThis.outerHeight;
  cw.matchMedia = globalThis.matchMedia; cw.getComputedStyle = globalThis.getComputedStyle;
  cw.isSecureContext = true; cw.name = (el && el.name) || '';
  try { cw[Symbol.toStringTag] = 'Window'; } catch (e) {}  // Object.prototype.toString.call(window) === '[object Window]'
  cw.MessageChannel = globalThis.MessageChannel; cw.Event = globalThis.Event;
  cw.NodeFilter = globalThis.NodeFilter; cw.Node = globalThis.Node;
  cw.localStorage = globalThis.localStorage; cw.sessionStorage = globalThis.sessionStorage;
  cw.matchMedia = globalThis.matchMedia; cw.getComputedStyle = globalThis.getComputedStyle;

  // Expose the real SpiderMonkey globals on the iframe `window` UNCONDITIONALLY (a permissive read of
  // `cw[name]` returns a truthy stub, so `||` won't work) — the VM fingerprints by reading these off
  // `window` (`window.URL`, `window.Blob`, `window.BigInt`, ...).
  ['URL','URLSearchParams','Blob','File','FileReader','FormData','Headers','Request','Response',
   'ReadableStream','WritableStream','TransformStream','AbortController','AbortSignal','BigInt',
   'BigInt64Array','BigUint64Array','TextEncoder','TextDecoder','Promise','Proxy','Reflect','Symbol',
   'WeakMap','WeakSet','WeakRef','Map','Set','JSON','Math','Date','RegExp','Array','Object','Function',
   'String','Number','Boolean','Error','TypeError','RangeError','SyntaxError','Uint8Array','Uint8ClampedArray',
   'Int8Array','Uint16Array','Int16Array','Uint32Array','Int32Array','Float32Array','Float64Array',
   'ArrayBuffer','SharedArrayBuffer','DataView','Intl','structuredClone','queueMicrotask','btoa','atob',
   'TextEncoderStream','TextDecoderStream','CompressionStream','DecompressionStream','crypto','chrome',
   'TextEncoder','TextDecoder','structuredClone','queueMicrotask','Blob','ReadableStream'
  ].forEach(function (n) { if (globalThis[n] !== undefined) cw[n] = globalThis[n]; });

  // browser-only APIs SpiderMonkey lacks — the VM probes for them; benign stubs keep it from crashing.
  cw.PerformanceObserver = function () { return { observe: function () {}, disconnect: function () {}, takeRecords: function () { return []; } }; };
  cw.Worker = function () { return { postMessage: function () {}, terminate: function () {}, addEventListener: function () {}, removeEventListener: function () {}, onmessage: null }; };
  cw.SharedWorker = cw.Worker;
  cw.speechSynthesis = { getVoices: function () { return []; }, speak: function () {}, cancel: function () {}, pause: function () {}, resume: function () {}, addEventListener: function () {}, removeEventListener: function () {} };
  cw.indexedDB = { open: function () { return { addEventListener: function () {}, onsuccess: null, onerror: null }; }, deleteDatabase: function () {} };
  cw.MutationObserver = globalThis.MutationObserver; cw.IntersectionObserver = globalThis.IntersectionObserver;
  cw.ResizeObserver = globalThis.ResizeObserver; cw.requestIdleCallback = function (cb) { return setTimeout(function () { cb({ didTimeout: false, timeRemaining: function () { return 50; } }); }, 1); };
  cw.cancelIdleCallback = function () {};

  el.contentWindow = cw; el.contentDocument = ifdoc;
  globalThis.__dbgCw = cw;  // DEBUG: lets probes inspect the iframe window post-run
  return cw;
};

// Run one VM script with the iframe window shadowing every ambient global (incl. globalThis). Strict
// mode is deliberate: the VM's `Y = this || self` must land on the iframe window — in strict mode a
// plain call's `this` is `undefined`, so `Y = self` (= the cw param) everywhere. (Sloppy mode would make
// `this` the real engine global and break that identity.) Bare globals the VM defines via
// `window.fn = …` and then calls unqualified (`runProgram(...)`) are bridged by __makeFrameWindow's
// set-mirror, since here `window` is a plain object, not the realm global.
globalThis.__runFrameScript = function (cw, src) {
  try {
    var fn = new Function('window', 'self', 'parent', 'top', 'document', 'location', 'navigator',
                          'frameElement', 'globalThis',
                          '"use strict";' + src + '\n//# sourceURL=cf-frame-vm.js');
    fn.call(cw, cw, cw, cw.parent, cw.top, cw.document, cw.location, cw.navigator, cw.frameElement, cw);
  } catch (e) { globalThis.__frameErr = String(e && e.stack || e); throw e; }
};
"""


def install(pm) -> None:
    """Install the frame runtime helpers into the pythonmonkey global (once per solve)."""
    pm.eval(_FRAME_JS)


def load_frame(pm, el, url, *, transport, page_url, log=None) -> None:
    """Fetch the challenge iframe + run its VM scripts inside the iframe element's window.

    Synchronous: kicks the VM off (its XHR + timers run on the engine's asyncio loop afterward). The
    token surfaces asynchronously via `__token` (api.js callback) or `__frameToken` (direct sniff)."""
    origin = "https://challenges.cloudflare.com"
    resp = transport.get(url, headers={"Referer": page_url, "Sec-Fetch-Dest": "iframe",
                                       "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "cross-site"})
    if log:
        log("frame.fetch", url=url, status=resp.status_code, bytes=len(resp.text or ""))
    html = resp.text or ""

    # boot the iframe window from the element api.js created (identity must match for postMessage).
    pm.eval("(function (el, url) { return globalThis.__bootFrame(el, url); })")(el, url)

    for attrs, body in _SCRIPT_RE.findall(html):
        m = _SRC_RE.search(attrs)
        if m:  # external script: fetch through the transport, then run
            src_url = m.group(1)
            if src_url.startswith("//"):
                src_url = "https:" + src_url
            elif src_url.startswith("/"):
                src_url = origin + src_url
            elif not src_url.startswith("http"):
                src_url = url.rsplit("/", 1)[0] + "/" + src_url
            code = transport.get(src_url, headers={"Referer": url}).text
        else:
            code = body
        if not code or not code.strip():
            continue
        import os as _os
        if _os.environ.get("CF_DUMP_VM"):
            with open(_os.environ["CF_DUMP_VM"], "w", encoding="utf-8") as _f:
                _f.write(code)
        pm.eval("(function (cw, src) { globalThis.__runFrameScript(cw, src); })")(
            pm.eval("(function (el) { return el.contentWindow; })")(el), code
        )
