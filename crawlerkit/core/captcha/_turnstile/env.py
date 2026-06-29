"""Fake just enough browser environment for the Turnstile challenge JS to run.

The challenge probes the DOM/`navigator`/`screen`/`crypto`/canvas/WebGL to fingerprint the client
and decide pass/fail. Every value here is seeded from `fingerprint.Fingerprint` (which is itself
derived from, and consistent with, the `Profile` already on the wire) — a navigator/UA or
fingerprint/TLS contradiction is exactly what managed Turnstile catches, so this module must never
invent a value that disagrees with the Profile.

Two jobs at once:
  - **serve** the values the challenge reads (window/document/location/navigator/screen/crypto,
    canvas+WebGL stubs, `document.cookie` bridged to the transport's jar, the `cf-turnstile`
    element + the Turnstile callback that captures the token), and
  - **instrument** every property the challenge touches that we DON'T define yet — a Proxy records
    the miss into `globalThis.__undef` so an instrumentation run shows exactly what to fill next.
    The env is filled from that observation, never guessed.

pythonmonkey already supplies `setTimeout`/`setInterval`/`queueMicrotask` (timers), `console`,
`atob`/`btoa` (base64) and `URL`, so this module does not re-implement them.
"""

import json
from urllib.parse import urlsplit


class _CookieBridge:
    """`document.cookie` <-> the transport's curl_cffi cookie jar, scoped to the page host."""

    def __init__(self, transport, page_url):
        self.transport = transport
        self.host = urlsplit(page_url).hostname or ""

    def get(self) -> str:
        jar = getattr(getattr(self.transport, "_session", None), "cookies", None)
        if jar is None:
            return ""
        pairs = []
        for c in jar.jar:
            if not c.domain or c.domain.lstrip(".") in self.host or self.host.endswith(c.domain.lstrip(".")):
                pairs.append(f"{c.name}={c.value}")
        return "; ".join(pairs)

    def set(self, cookie_str: str) -> None:
        # "name=value; Path=/; Domain=..." — only the first pair is the cookie; attrs are advisory.
        if not cookie_str or "=" not in cookie_str:
            return
        first = cookie_str.split(";")[0].strip()
        name, _, value = first.partition("=")
        jar = getattr(getattr(self.transport, "_session", None), "cookies", None)
        if jar is not None and name:
            try:
                jar.set(name.strip(), value.strip(), domain=self.host, path="/")
            except Exception:
                pass


# Boot script: seed concrete globals from the fingerprint, wrap the big host objects in a
# miss-logging Proxy, bridge document.cookie, and stand up the cf-turnstile element + token sink.
_BOOT_JS = r"""
'use strict';
(function (FP, PAGE_URL, ORIGIN, WIDGET, cookieGet, cookieSet) {
  globalThis.__undef = globalThis.__undef || [];
  var byId = {};  // id -> element, so getElementById finds widgets Turnstile creates + names

  // Native-function spoof: anti-bot VMs gate "unsupported_browser" on `fn.toString()` containing
  // "[native code]". Our polyfills/stubs leak JS source, so report every non-native function as native.
  (function () {
    if (globalThis.__nativeSpoofed) return;
    globalThis.__nativeSpoofed = true;
    var _ts = Function.prototype.toString;
    var nativeRe = /\{\s*\[native code\]\s*\}/;
    var spoof = function toString() {
      try { var s = _ts.call(this); if (nativeRe.test(s)) return s; } catch (e) {}
      return 'function ' + ((this && this.name) || '') + '() { [native code] }';
    };
    try { Object.defineProperty(spoof, 'toString', { value: spoof, configurable: true }); } catch (e) {}  // hide the override itself
    Function.prototype.toString = spoof;
  })();

  // navigator.userAgentData (Client Hints) — a real Chrome ships non-empty brands + getHighEntropyValues;
  // an empty brands list on a "Chrome" UA is exactly what Turnstile flags as unsupported_browser.
  var __cv = (String(FP.user_agent).match(/Chrome\/(\d+)/) || [])[1] || '133';
  var __brands = [{ brand: 'Chromium', version: __cv }, { brand: 'Google Chrome', version: __cv },
                  { brand: 'Not(A:Brand', version: '99' }];
  var __platVer = FP.os_family === 'windows' ? '15.0.0' : FP.os_family === 'macos' ? '14.5.0' : '6.5.0';
  var __uaData = {
    brands: __brands, mobile: false, platform: FP.os_family_pretty,
    getHighEntropyValues: function (hints) {
      return Promise.resolve({
        architecture: 'x86', bitness: '64', brands: __brands, mobile: false, model: '',
        platform: FP.os_family_pretty, platformVersion: __platVer, uaFullVersion: __cv + '.0.0.0',
        fullVersionList: __brands.map(function (b) { return { brand: b.brand, version: b.version + '.0.0.0' }; }),
        wow64: false,
      });
    },
    toJSON: function () { return { brands: __brands, mobile: false, platform: FP.os_family_pretty }; },
  };
  function __rec(name, p) {  // debug ring buffer of property reads (off unless globalThis.__rec set)
    if (!globalThis.__rec || typeof p === 'symbol') return;
    var L = globalThis.__getLog = globalThis.__getLog || [];
    L.push(name + '.' + String(p)); if (L.length > 500) L.shift();
  }
  function trap(name, base) {
    return new Proxy(base || {}, {
      get(t, p) {
        __rec(name, p);
        if (p in t) return t[p];
        if (typeof p === 'symbol') return undefined;
        globalThis.__undef.push(name + '.' + String(p));
        return undefined;
      },
      set(t, p, v) { t[p] = v; return true; },
    });
  }

  // Permissive trap for the iframe VM: misses return a CHAINABLE no-op stub instead of `undefined`,
  // so the heavily-obfuscated challenge VM (dynamic `Y[key][key](...)` access over DOM it builds)
  // never crashes on a property we didn't model. A single shared stub keeps identity stable and
  // avoids allocation storms; it is callable/constructable and coerces to ''/0. Scoped to the frame
  // (`__trapVoid`) so the page env (api.js) keeps strict `undefined` misses.
  function voidStub() {
    if (globalThis.__VS) return globalThis.__VS;
    var f = function () { return globalThis.__VS; };
    globalThis.__VS = new Proxy(f, {
      get(t, p) {
        if (p === Symbol.toPrimitive) return function () { return 0; };
        if (p === 'then') return undefined;  // not thenable (don't let `await stub` hang)
        // empty-iterable: `for (x of stub)` / `[...stub]` yield nothing instead of throwing "not iterable".
        if (p === Symbol.iterator) return function () { return { next: function () { return { done: true, value: undefined }; } }; };
        if (p === Symbol.toStringTag) return 'Object';
        if (p === 'length') return 0;
        if (p === 'nodeType') return 1;
        if (p === 'toString' || p === 'valueOf') return function () { return ''; };
        // terminate DOM tree/sibling walks so `while(n=n.parentNode){}`-style loops don't spin forever.
        if (p === 'parentNode' || p === 'parentElement' || p === 'offsetParent' || p === 'ownerDocument' ||
            p === 'firstChild' || p === 'lastChild' || p === 'firstElementChild' || p === 'lastElementChild' ||
            p === 'nextSibling' || p === 'previousSibling' || p === 'nextElementSibling' || p === 'previousElementSibling' ||
            p === 'host' || p === 'shadowRoot' || p === 'assignedSlot') return null;
        return globalThis.__VS;
      },
      apply() { return globalThis.__VS; },
      construct() { return globalThis.__VS; },
      has() { return true; },
    });
    return globalThis.__VS;
  }
  function trapVoid(name, base) {
    return new Proxy(base || {}, {
      get(t, p) {
        __rec(name, p);
        if (p in t) return t[p];
        if (typeof p === 'symbol') return undefined;
        globalThis.__undef.push(name + '.' + String(p));
        return voidStub();
      },
      set(t, p, v) { t[p] = v; return true; },
      has() { return true; },
    });
  }
  globalThis.__trapVoid = trapVoid;
  globalThis.__voidStub = voidStub;  // frame.py returns this for missing DOM queries (no null deref)

  globalThis.navigator = trap('navigator', {
    userAgent: FP.user_agent, appVersion: FP.user_agent.replace('Mozilla/', ''),
    appName: 'Netscape', appCodeName: 'Mozilla', product: 'Gecko', productSub: '20030107',
    vendor: 'Google Inc.', vendorSub: '', platform: FP.platform,
    language: FP.language, languages: FP.languages,
    hardwareConcurrency: FP.hardware_concurrency, deviceMemory: FP.device_memory,
    maxTouchPoints: 0, webdriver: false, cookieEnabled: true, onLine: true, pdfViewerEnabled: true,
    doNotTrack: null, plugins: { length: 0 }, mimeTypes: { length: 0 },
    userAgentData: __uaData,
    javaEnabled: () => false, taintEnabled: () => false,
    sendBeacon: globalThis.navigator ? globalThis.navigator.sendBeacon : (() => true),
    permissions: { query: () => Promise.resolve({ state: 'prompt' }) },
  });

  globalThis.screen = trap('screen', {
    width: FP.screen_width, height: FP.screen_height,
    availWidth: FP.avail_width, availHeight: FP.avail_height, availLeft: 0, availTop: 0,
    colorDepth: FP.color_depth, pixelDepth: FP.color_depth,
    orientation: { type: 'landscape-primary', angle: 0 },
  });

  const u = new URL(PAGE_URL);
  globalThis.location = trap('location', {
    href: PAGE_URL, origin: ORIGIN, protocol: u.protocol, host: u.host, hostname: u.hostname,
    port: u.port, pathname: u.pathname, search: u.search, hash: u.hash,
    assign: () => {}, replace: () => {}, reload: () => {}, toString: () => PAGE_URL,
  });

  // Minimal element + the cf-turnstile container the widget binds to. Uses the permissive trap:
  // api.js's requestExtraParams handler fingerprints the DOM (e.g. `l(document.body.parentNode)`,
  // obfuscated probes over the widget wrapper) and silently swallows any throw, which would skip the
  // extra-params reply the iframe VM is waiting on — so DOM misses must return safe stubs, not undefined.
  function makeEl(tag) {
    const el = trapVoid('element', {
      tagName: (tag || 'div').toUpperCase(), nodeType: 1, isConnected: true, name: '', children: [], childNodes: [],
      style: {}, dataset: {}, attributes: {}, classList: { add(){}, remove(){}, contains(){return false;} },
      _listeners: {},
      setAttribute(k, v) { this.attributes[k] = v; if (k === 'id') byId[v] = this; if (k === 'src' && this.tagName === 'IFRAME') __iframeSrc(this, v); if (k.indexOf('data-') === 0) this.dataset[k.slice(5)] = v; },
      get id() { return this._id || this.attributes['id'] || ''; }, set id(v) { this._id = v; this.attributes['id'] = v; byId[v] = this; },
      getAttribute(k) { return (k in this.attributes) ? this.attributes[k] : null; },
      appendChild(c) { this.children.push(c); this.childNodes.push(c); if (c) c._parent = this; return c; },
      removeChild(c) { return c; },
      addEventListener(t, cb) { (this._listeners[t] = this._listeners[t] || []).push(cb); },
      removeEventListener() {}, dispatchEvent() { return true; },
      getBoundingClientRect: () => ({ x: 0, y: 0, top: 0, left: 0, right: 300, bottom: 65, width: 300, height: 65 }),
      // api.js locates the challenge iframe via `widget.shadow.querySelector("#"+widgetId)` and then
      // checks `message.source === thatIframe.contentWindow`; resolve "#id" via the global id registry
      // (the iframe's id is registered there) so that identity check passes.
      querySelector(s) { return (s && s.charAt(0) === '#') ? (byId[s.slice(1)] || null) : null; },
      querySelectorAll(s) { var e = this.querySelector(s); return e ? [e] : []; },
      getElementById(id) { return byId[id] || null; },
      focus(){}, blur(){}, click(){}, remove(){},
      contains(n) { if (n === this) return true; for (var i = 0; i < this.childNodes.length; i++) if (this.childNodes[i] === n) return true; return false; },
      matches() { return false; }, closest() { return null; }, getRootNode() { return globalThis.document; },
      contentWindow: null, contentDocument: null, ownerDocument: null, shadowRoot: null,
      attachShadow(opts) { var sr = makeEl('shadowroot'); sr.host = this; sr.mode = (opts && opts.mode) || 'open'; this.shadowRoot = sr; return sr; },
      insertBefore(n) { this.children.push(n); this.childNodes.push(n); return n; },
      cloneNode() { return makeEl(this.tagName); }, hasAttribute(k) { return k in this.attributes; },
      removeAttribute(k) { delete this.attributes[k]; }, getAttributeNS() { return null; },
      setAttributeNS() {}, replaceChildren() {}, after() {}, before() {}, prepend() {}, append() {},
      get firstChild() { return this.childNodes[0] || null; }, get lastChild() { return this.childNodes[this.childNodes.length - 1] || null; },
      get parentNode() { return this._parent || null; }, get parentElement() { return this._parent || null; },
      get nextSibling() { return null; }, get previousSibling() { return null; },
      get innerHTML() { return ''; }, set innerHTML(v) {}, get outerHTML() { return ''; },
      get textContent() { return ''; }, set textContent(v) {}, get className() { return this._cls || ''; }, set className(v) { this._cls = v; },
    });
    if ((tag || '').toLowerCase() === 'canvas') {
      el.width = 300; el.height = 150;
      // route webgl/webgl2 to the GL context (its vendor/renderer must agree with the OS); a canvas
      // whose getContext('webgl') has no real GL params reads as "no WebGL" => unsupported_browser.
      el.getContext = (type) => (type && String(type).indexOf('webgl') >= 0) ? makeGL() : makeCanvasCtx();
      el.toDataURL = () => 'data:image/png;base64,' + FP.canvas_hash_b64;
      el.toBlob = (cb) => cb && cb({ size: 0, type: 'image/png' });
    }
    if ((tag || '').toLowerCase() === 'iframe') {
      el.contentWindow = trapVoid('iframeWindow', { postMessage: () => {}, location: { href: 'about:blank' } });
      Object.defineProperty(el, 'src', {
        get() { return this._src || ''; },
        set(v) { this._src = v; this.attributes['src'] = v; __iframeSrc(this, v); },
        configurable: true,
      });
    }
    return el;
  }
  // Record every iframe src the page sets, and fire an optional Python hook so the engine can load it.
  function __iframeSrc(el, url) {
    (globalThis.__iframeSrcs = globalThis.__iframeSrcs || []).push(url);
    if (typeof globalThis.__onIframeSrc === 'function') { try { globalThis.__onIframeSrc(el, url); } catch (e) {} }
  }
  function makeCanvasCtx() {
    return trapVoid('canvasCtx', {
      canvas: { width: 300, height: 150 },
      fillRect(){}, clearRect(){}, fillText(){}, strokeText(){}, beginPath(){}, closePath(){},
      moveTo(){}, lineTo(){}, arc(){}, fill(){}, stroke(){}, rect(){}, save(){}, restore(){},
      translate(){}, rotate(){}, scale(){}, setTransform(){}, transform(){},
      measureText: (t) => ({ width: (t ? t.length : 0) * 7 }),
      getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray((w || 1) * (h || 1) * 4), width: w, height: h }),
      putImageData(){}, createLinearGradient: () => ({ addColorStop(){} }),
      createRadialGradient: () => ({ addColorStop(){} }), drawImage(){},
      getParameter(){}, getExtension(){}, getContextAttributes: () => ({}),
    });
  }
  // WebGL parameter probes that must agree with the OS family (vendor/renderer).
  function makeGL() {
    const P = { 37445: FP.webgl_vendor, 37446: FP.webgl_renderer, 7936: 'WebKit',
                7937: FP.webgl_renderer, 7938: 'WebGL 1.0 (OpenGL ES 2.0 Chromium)' };
    return trapVoid('webgl', {
      getParameter: (k) => (k in P) ? P[k] : 0,
      getExtension: (n) => (n === 'WEBGL_debug_renderer_info') ? { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 } : null,
      getSupportedExtensions: () => ['WEBGL_debug_renderer_info'], getShaderPrecisionFormat: () => ({ precision: 23, rangeMin: 127, rangeMax: 127 }),
      createShader: () => ({}), createProgram: () => ({}), createBuffer: () => ({}),
      getContextAttributes: () => ({ alpha: true, antialias: true, depth: true }),
    });
  }

  const cfEl = makeEl('div');
  cfEl.className = 'cf-turnstile';
  if (WIDGET.sitekey) cfEl.setAttribute('data-sitekey', WIDGET.sitekey);
  if (WIDGET.action) cfEl.setAttribute('data-action', WIDGET.action);
  if (WIDGET.cdata) cfEl.setAttribute('data-cdata', WIDGET.cdata);
  // implicit render reads the token callback by NAME from data-callback -> window[name](token).
  cfEl.setAttribute('data-callback', '__turnstileToken');
  // did api.js auto-render a widget? (it names containers cf-chl-widget-*) — used to skip a redundant
  // explicit render in the engine.
  globalThis.__hasWidget = function () { for (var k in byId) if (k.indexOf('cf-chl-widget') === 0) return true; return false; };

  // api.js looks for its OWN <script src=...api.js> (via document.currentScript / scripts) to
  // derive the challenge-platform base URL and warns "Could not find valid script tag" without it.
  const apiScript = makeEl('script');
  apiScript.src = WIDGET.api_js_url; apiScript.type = 'text/javascript'; apiScript.async = true;
  apiScript.setAttribute('src', WIDGET.api_js_url);

  const head = makeEl('head'), body = makeEl('body'), html = makeEl('html');
  html.appendChild(head); html.appendChild(body);  // so document.body.parentNode === <html>
  head.appendChild(apiScript);
  body.appendChild(cfEl);
  const scripts = [apiScript];
  const byClass = { 'cf-turnstile': [cfEl] };

  globalThis.document = trapVoid('document', {
    documentElement: html, head: head, body: body, location: globalThis.location,
    currentScript: apiScript, scripts: scripts,
    readyState: 'complete', visibilityState: 'visible', hidden: false, characterSet: 'UTF-8',
    featurePolicy: { allowsFeature: () => true, allowedFeatures: () => [], features: () => [] },
    title: '', referrer: ORIGIN + '/', URL: PAGE_URL, domain: u.hostname, compatMode: 'CSS1Compat',
    createElement: (t) => makeEl(t), createElementNS: (ns, t) => makeEl(t),
    createTextNode: (t) => ({ nodeType: 3, textContent: t }),
    createEvent: () => ({ initEvent(){} }), createDocumentFragment: () => makeEl('fragment'),
    // DOM walkers api.js uses to fingerprint the page (bn/_n/ssL...). Empty traversal => a sparse
    // but VALID fingerprint, so the handler doesn't throw and the extraParams reply is sent.
    createNodeIterator: (root) => ({ root: root, referenceNode: root, nextNode: () => null, previousNode: () => null, detach(){} }),
    createTreeWalker: (root) => ({ root: root, currentNode: root, nextNode: () => null, previousNode: () => null,
      parentNode: () => null, firstChild: () => null, lastChild: () => null, nextSibling: () => null, previousSibling: () => null }),
    createRange: () => ({ setStart(){}, setEnd(){}, selectNodeContents(){}, getBoundingClientRect: () => ({ width: 0, height: 0 }), getClientRects: () => [] }),
    getElementById: (id) => byId[id] || (id === 'cf-turnstile' ? cfEl : null),
    getElementsByTagName: (t) => (t === 'head' ? [head] : t === 'body' ? [body] :
                                  t === 'script' ? scripts : []),
    getElementsByClassName: (c) => byClass[c] || [],
    querySelector: (s) => (!s ? null : s.indexOf('cf-turnstile') >= 0 ? cfEl :
                           s.indexOf('script') >= 0 ? apiScript : null),
    querySelectorAll: (s) => (!s ? [] : s.indexOf('cf-turnstile') >= 0 ? [cfEl] :
                              s.indexOf('script') >= 0 ? scripts : []),
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => true,
    cookie: '',  // replaced below by an accessor property bridged to the jar
  });
  // document.cookie <-> transport jar (a plain value can't call back into Python).
  Object.defineProperty(globalThis.document, 'cookie', { get: () => cookieGet(), set: (v) => { cookieSet(v); }, configurable: true });

  globalThis.history = { length: 1, state: null, pushState(){}, replaceState(){}, go(){}, back(){}, forward(){} };
  // Web Storage — api.js reads localStorage (session-continuity) while building the extraParams reply.
  function makeStorage() {
    var m = {};
    return { getItem: function (k) { return (k in m) ? m[k] : null; }, setItem: function (k, v) { m[k] = String(v); },
             removeItem: function (k) { delete m[k]; }, clear: function () { m = {}; }, key: function (i) { return Object.keys(m)[i] || null; },
             get length() { return Object.keys(m).length; } };
  }
  globalThis.localStorage = globalThis.localStorage || makeStorage();
  globalThis.sessionStorage = globalThis.sessionStorage || makeStorage();

  // TrustedTypes: the iframe VM builds a policy (createHTML/createScript/createScriptURL) at boot.
  globalThis.trustedTypes = globalThis.trustedTypes || {
    createPolicy: function (name, rules) {
      rules = rules || {};
      return { name: name,
               createHTML: rules.createHTML || function (s) { return s; },
               createScript: rules.createScript || function (s) { return s; },
               createScriptURL: rules.createScriptURL || function (s) { return s; } };
    },
    defaultPolicy: null, emptyHTML: '', emptyScript: '',
    isHTML: function () { return false; }, isScript: function () { return false; }, isScriptURL: function () { return false; },
  };
  globalThis.crypto = globalThis.crypto || trap('crypto', {
    getRandomValues: (a) => { for (let i = 0; i < a.length; i++) a[i] = Math.floor(Math.random() * 256); return a; },
    randomUUID: () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16); }),
    subtle: { digest: () => Promise.resolve(new ArrayBuffer(32)) },
  });

  // WebGL hookpoint for canvas.getContext('webgl'); overrides the 2d ctx when asked.
  globalThis.__makeGL = makeGL;
  globalThis.__makeEl = makeEl;  // frame.py reuses the element factory to build the iframe document
  globalThis.__trap = trap;      // …and the miss-logging Proxy, so frame DOM misses hit __undef too
  globalThis.__byId = function (id) { return byId[id] || null; };  // shared id registry lookup

  // DOM interface constructors. api.js does `x instanceof HTMLScriptElement` etc. on these bare
  // globals (a ReferenceError, NOT a trappable property access), so they must exist — AND its
  // `instanceof` helper honours `Ctor[Symbol.hasInstance]`, so we make instanceof actually work for
  // our plain element objects by keying it to their tagName. (Cloudflare's api.js finds its own
  // script tag via `P(document.currentScript, HTMLScriptElement)` — this is what makes that pass.)
  (function () {
    // interface -> expected tagName for instanceof; absent = "any element" (nodeType === 1).
    var TAG = { HTMLScriptElement: 'SCRIPT', HTMLIFrameElement: 'IFRAME', HTMLCanvasElement: 'CANVAS',
      HTMLDivElement: 'DIV', HTMLSpanElement: 'SPAN', HTMLImageElement: 'IMG', HTMLInputElement: 'INPUT',
      HTMLFormElement: 'FORM', HTMLBodyElement: 'BODY', HTMLHeadElement: 'HEAD', HTMLAnchorElement: 'A',
      HTMLStyleElement: 'STYLE', HTMLLinkElement: 'LINK' };
    var ELEMENTISH = { Node: 1, Element: 1, HTMLElement: 1, HTMLUnknownElement: 1, EventTarget: 1, SVGElement: 1 };
    var names = ['EventTarget','Node','Element','HTMLElement','HTMLDivElement','HTMLSpanElement',
      'HTMLScriptElement','HTMLIFrameElement','HTMLCanvasElement','HTMLImageElement','HTMLInputElement',
      'HTMLFormElement','HTMLBodyElement','HTMLHeadElement','HTMLAnchorElement','HTMLStyleElement',
      'HTMLLinkElement','HTMLUnknownElement','SVGElement','Document','HTMLDocument','Window','Navigator',
      'Screen','Location','History','Storage','CSSStyleDeclaration','DOMTokenList','NamedNodeMap',
      'NodeList','HTMLCollection','Attr','CharacterData','Text','Comment','DocumentFragment','ShadowRoot',
      'Event','UIEvent','MouseEvent','KeyboardEvent','TouchEvent','PointerEvent','FocusEvent','InputEvent',
      'MessageEvent','CustomEvent','ErrorEvent','PromiseRejectionEvent','MessageChannel','MessagePort',
      'DOMRect','DOMRectReadOnly','DOMPoint','DOMException','XPathResult','Range','Selection',
      'CanvasRenderingContext2D','WebGLRenderingContext','WebGL2RenderingContext','ImageData','Path2D',
      'OffscreenCanvas','TextMetrics','MediaQueryList','PerformanceObserver','FontFace'];
    for (var i = 0; i < names.length; i++) {
      if (globalThis[names[i]] !== undefined) continue;
      var f = (function (n) { var fn = function () {}; fn.prototype = {}; fn.toString = function () { return 'function ' + n + '() { [native code] }'; }; return fn; })(names[i]);
      var wantTag = TAG[names[i]], anyEl = ELEMENTISH[names[i]];
      if (wantTag || anyEl) {
        (function (ff, want, any) {
          Object.defineProperty(ff, Symbol.hasInstance, { value: function (x) {
            if (!x || typeof x !== 'object') return false;
            if (any) return x.nodeType === 1 || x.tagName !== undefined;
            return x.tagName === want;
          } });
        })(f, wantTag, anyEl);
      }
      globalThis[names[i]] = f;
    }
  })();

  // Observers the challenge may instantiate — must be constructable with the standard methods.
  function makeObserver() { return function (cb) { return { observe: function () {}, unobserve: function () {}, disconnect: function () {}, takeRecords: function () { return []; } }; }; }
  globalThis.MutationObserver = globalThis.MutationObserver || makeObserver();
  globalThis.IntersectionObserver = globalThis.IntersectionObserver || makeObserver();
  globalThis.ResizeObserver = globalThis.ResizeObserver || makeObserver();
  globalThis.Image = globalThis.Image || function () { return makeEl('img'); };

  // NodeFilter / Node constants the DOM-walking fingerprinters (createNodeIterator/TreeWalker) read.
  globalThis.NodeFilter = globalThis.NodeFilter || { SHOW_ALL: 0xFFFFFFFF, SHOW_ELEMENT: 1, SHOW_ATTRIBUTE: 2,
    SHOW_TEXT: 4, SHOW_COMMENT: 128, SHOW_DOCUMENT: 256, FILTER_ACCEPT: 1, FILTER_REJECT: 2, FILTER_SKIP: 3 };
  if (globalThis.Node) {
    globalThis.Node.ELEMENT_NODE = 1; globalThis.Node.TEXT_NODE = 3; globalThis.Node.CDATA_SECTION_NODE = 4;
    globalThis.Node.COMMENT_NODE = 8; globalThis.Node.DOCUMENT_NODE = 9; globalThis.Node.DOCUMENT_TYPE_NODE = 10;
    globalThis.Node.DOCUMENT_FRAGMENT_NODE = 11; globalThis.Node.ATTRIBUTE_NODE = 2;
  }

  globalThis.WebGLRenderingContext = function(){}; globalThis.WebGL2RenderingContext = function(){};
  globalThis.performance = globalThis.performance || { now: () => Date.now(), timeOrigin: Date.now(), getEntriesByType: () => [], mark(){}, measure(){} };
  globalThis.requestAnimationFrame = (cb) => setTimeout(() => cb(Date.now()), 16);
  globalThis.cancelAnimationFrame = () => {};
  globalThis.matchMedia = (q) => ({ matches: false, media: q, addListener(){}, removeListener(){}, addEventListener(){}, removeEventListener(){} });
  globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' });

  // Polyfills for standard web APIs SpiderMonkey lacks but the challenge VM relies on — TextEncoder
  // above all (it UTF-8-encodes the challenge payload; a missing one => garbage bytes => the VM
  // crashes). Real (correct) implementations, not stubs.
  if (typeof globalThis.TextEncoder === 'undefined') {
    globalThis.TextEncoder = function TextEncoder() { this.encoding = 'utf-8'; };
    globalThis.TextEncoder.prototype.encode = function (str) {
      var utf8 = unescape(encodeURIComponent(String(str == null ? '' : str)));
      var arr = new Uint8Array(utf8.length);
      for (var i = 0; i < utf8.length; i++) arr[i] = utf8.charCodeAt(i);
      return arr;
    };
    globalThis.TextEncoder.prototype.encodeInto = function (str, dest) {
      var enc = this.encode(str), n = Math.min(enc.length, dest.length);
      for (var i = 0; i < n; i++) dest[i] = enc[i];
      return { read: str.length, written: n };
    };
  }
  if (typeof globalThis.TextDecoder === 'undefined') {
    globalThis.TextDecoder = function TextDecoder(enc) { this.encoding = enc || 'utf-8'; };
    globalThis.TextDecoder.prototype.decode = function (buf) {
      if (!buf) return '';
      var bytes = (buf instanceof Uint8Array) ? buf : new Uint8Array(buf.buffer || buf);
      var s = ''; for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
      try { return decodeURIComponent(escape(s)); } catch (e) { return s; }
    };
  }
  if (typeof globalThis.structuredClone === 'undefined') {
    globalThis.structuredClone = function (o) { try { return JSON.parse(JSON.stringify(o)); } catch (e) { return o; } };
  }
  if (typeof globalThis.queueMicrotask === 'undefined') {
    globalThis.queueMicrotask = function (cb) { Promise.resolve().then(cb); };
  }
  if (typeof globalThis.Blob === 'undefined') {
    globalThis.Blob = function Blob(parts, opts) {
      this.type = (opts && opts.type) || ''; this.size = 0;
      if (parts) for (var i = 0; i < parts.length; i++) { var p = parts[i]; this.size += (p && (p.length || p.byteLength)) || 0; }
    };
    globalThis.Blob.prototype.text = function () { return Promise.resolve(''); };
    globalThis.Blob.prototype.arrayBuffer = function () { return Promise.resolve(new ArrayBuffer(0)); };
    globalThis.Blob.prototype.slice = function () { return new globalThis.Blob([]); };
    globalThis.Blob.prototype.stream = function () { return null; };
  }
  if (typeof globalThis.ReadableStream === 'undefined') {
    globalThis.ReadableStream = function ReadableStream() { this.locked = false; };
    globalThis.ReadableStream.prototype.getReader = function () { return { read: function () { return Promise.resolve({ done: true, value: undefined }); }, releaseLock: function () {}, cancel: function () { return Promise.resolve(); } }; };
    // pipeTo/pipeThrough/tee are part of the spec; Cloudflare's unsupported_browser gate (PP) rejects
    // when `ReadableStream.prototype.pipeTo === undefined`, so these must exist (real-browser parity).
    globalThis.ReadableStream.prototype.pipeTo = function () { return Promise.resolve(); };
    globalThis.ReadableStream.prototype.pipeThrough = function (t) { return (t && t.readable) || new globalThis.ReadableStream(); };
    globalThis.ReadableStream.prototype.tee = function () { return [new globalThis.ReadableStream(), new globalThis.ReadableStream()]; };
    globalThis.ReadableStream.prototype.cancel = function () { return Promise.resolve(); };
  }
  // URL.createObjectURL/revokeObjectURL: SpiderMonkey's native URL lacks these host methods. The
  // unsupported_browser gate builds `new Worker(URL.createObjectURL(new Blob([...])))` inside a
  // try/catch and rejects if it throws — so createObjectURL must return a blob: URL string.
  if (typeof globalThis.URL !== 'undefined' && typeof globalThis.URL.createObjectURL !== 'function') {
    globalThis.URL.createObjectURL = function () {
      return 'blob:' + ORIGIN + '/' + 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        var r = (Math.random() * 16) | 0; return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16); });
    };
    globalThis.URL.revokeObjectURL = function () {};
  }
  globalThis.devicePixelRatio = FP.pixel_ratio;
  globalThis.innerWidth = FP.screen_width; globalThis.innerHeight = FP.avail_height;
  globalThis.outerWidth = FP.screen_width; globalThis.outerHeight = FP.screen_height;
  globalThis.screenX = 0; globalThis.screenY = 0; globalThis.pageXOffset = 0; globalThis.pageYOffset = 0;
  globalThis.name = ''; globalThis.closed = false; globalThis.origin = ORIGIN;
  globalThis.isSecureContext = true;
  // window.chrome — present on real Chrome; its absence is a headless/unsupported tell.
  globalThis.chrome = globalThis.chrome || {
    runtime: {}, app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
    loadTimes: function () { return {}; }, csi: function () { return {}; },
  };
  // --- message bus (parent <-> iframe postMessage) ---
  // api.js registers window.addEventListener('message', V) and validates e.isTrusted, e.source ===
  // <iframe>.contentWindow, and a Cloudflare e.origin. The iframe VM does window.parent.postMessage(token).
  // We give each window real listener storage + a delivery that builds a trusted MessageEvent. Reused
  // for the iframe window by frame.py via globalThis.__installBus / __deliverMessage.
  globalThis.__installBus = function (win) {
    // assign a fresh store UNCONDITIONALLY: on a permissive (trapVoid) iframe window `win.__listeners`
    // would read back a truthy stub, so `|| {}` must not be used or listeners get swallowed.
    win.__listeners = {};
    win.onmessage = null; win.onerror = null; win.onload = null; win.onunhandledrejection = null;
    win.addEventListener = function (type, cb) { if (!cb) return; (win.__listeners[type] = win.__listeners[type] || []).push(cb); };
    win.removeEventListener = function (type, cb) { var a = win.__listeners[type]; if (a) { var i = a.indexOf(cb); if (i >= 0) a.splice(i, 1); } };
    win.dispatchEvent = function (ev) { var a = win.__listeners[ev && ev.type]; if (a) a.slice().forEach(function (f) { try { f.call(win, ev); } catch (e) {} }); return true; };
    return win;
  };
  globalThis.__deliverMessage = function (targetWin, data, origin, sourceWin) {
    // schedule async like a real postMessage; build a TRUSTED event (synthetic events are isTrusted:false,
    // which api.js rejects — we own the object so we force it true).
    setTimeout(function () {
      var ev = { type: 'message', data: data, origin: origin || '', source: sourceWin || null,
                 isTrusted: true, ports: [], lastEventId: '', target: targetWin, currentTarget: targetWin,
                 preventDefault: function () {}, stopPropagation: function () {}, stopImmediatePropagation: function () {} };
      var rec = function (e) { (globalThis.__busErrs = globalThis.__busErrs || []).push(((e && e.name) || 'Error') + ': ' + ((e && e.message) || String(e)) + '\n' + ((e && e.stack) || '')); };
      var a = (targetWin.__listeners && targetWin.__listeners['message']) || [];
      a.slice().forEach(function (f) { try { f.call(targetWin, ev); } catch (e) { rec(e); } });
      if (typeof targetWin.onmessage === 'function') { try { targetWin.onmessage(ev); } catch (e) { rec(e); } }
    }, 0);
  };
  globalThis.__installBus(globalThis);
  globalThis.postMessage = function (data, origin) { globalThis.__deliverMessage(globalThis, data, origin, globalThis); };
  globalThis.window = globalThis; globalThis.self = globalThis; globalThis.top = globalThis; globalThis.parent = globalThis;
  globalThis.frames = globalThis; globalThis.globalThis = globalThis;

  // Token sink: Turnstile invokes the page callback (or onload) with the response token.
  globalThis.__token = null;
  globalThis.__turnstileToken = function (t) { globalThis.__token = t; };
})(
  __FP, __PAGE_URL, __ORIGIN, __WIDGET, __cookieGet, __cookieSet
);
"""


def install(pm, *, fingerprint, page_url: str, widget, transport, log=None) -> _CookieBridge:
    """Install the faked browser env into the pythonmonkey global. Returns the `_CookieBridge`
    (kept alive by the caller so the `document.cookie` accessor keeps working)."""
    fp = fingerprint
    origin = "{0}://{1}".format(urlsplit(page_url).scheme or "https", urlsplit(page_url).netloc)
    fp_json = {
        "user_agent": fp.user_agent, "platform": fp.platform,
        "os_family_pretty": {"windows": "Windows", "macos": "macOS", "linux": "Linux"}.get(fp.os_family, "Windows"),
        "language": fp.language, "languages": fp.languages,
        "hardware_concurrency": fp.hardware_concurrency, "device_memory": fp.device_memory,
        "screen_width": fp.screen_width, "screen_height": fp.screen_height,
        "avail_width": fp.avail_width, "avail_height": fp.avail_height,
        "color_depth": fp.color_depth, "pixel_ratio": fp.pixel_ratio,
        "webgl_vendor": fp.webgl_vendor, "webgl_renderer": fp.webgl_renderer,
        # canvas hash bytes -> base64 so toDataURL returns a stable, plausible payload.
        "canvas_hash_b64": _hex_to_b64(fp.canvas_hash),
    }
    widget_json = {
        "sitekey": widget.sitekey, "action": widget.action, "cdata": widget.cdata,
        "api_js_url": "https://challenges.cloudflare.com/turnstile/v0/api.js",
    }

    cookies = _CookieBridge(transport, page_url)
    pm.eval(f"globalThis.__FP = {json.dumps(fp_json)};")
    pm.eval(f"globalThis.__PAGE_URL = {json.dumps(page_url)};")
    pm.eval(f"globalThis.__ORIGIN = {json.dumps(origin)};")
    pm.eval(f"globalThis.__WIDGET = {json.dumps(widget_json)};")
    pm.eval("(function (g, s) { globalThis.__cookieGet = g; globalThis.__cookieSet = s; })")(
        cookies.get, cookies.set
    )
    pm.eval(_BOOT_JS)
    return cookies


def _hex_to_b64(hex_str: str) -> str:
    import base64
    try:
        return base64.b64encode(bytes.fromhex(hex_str)).decode("ascii")
    except ValueError:
        return ""
