// Browserless Cloudflare Turnstile engine: runs api.js + the challenge iframe VM in real V8 over jsdom,
// with all networking proxied to crawlerkit's curl_cffi Transport over stdio (see node_engine.py).
//
// Architecture mirrors the pythonmonkey engine (env.py/frame.py) but on real jsdom windows + V8, so the
// challenge's proof-of-work bytecode VM introspects a REAL DOM/shadow-DOM instead of hand stubs:
//   - parent window  = jsdom of the page form; api.js auto-renders `.cf-turnstile`, creates the iframe.
//   - iframe window  = a second jsdom of the challenge-platform HTML; its inline VM runs the PoW + XHR.
//   - postMessage    = a manual bus (jsdom's MessageEvent.isTrusted is false; api.js requires true), so
//                      VM->parent delivers with {isTrusted:true, source:iframeWindow, origin:cf}; api.js's
//                      `e.source === iframe.contentWindow` holds because we pin contentWindow to it.
// Newline-delimited JSON over stdio; bodies base64.
import readline from 'node:readline';
import fs from 'node:fs';
import { JSDOM, ResourceLoader, VirtualConsole } from 'jsdom';
// Real 2D canvas backend (Cairo) so the VM's canvas fingerprint is REAL rendered pixels, not a fixed
// stub (the PoW hashes toDataURL — a constant stub reads as a bot). Optional: fall back to the stub.
let _createCanvas = null;
try { _createCanvas = (await import('canvas')).createCanvas; } catch { _createCanvas = null; }
// The exact window own-property surface of a real Chrome (captured ground truth, refreshable per Chrome
// major). jsdom exposes ~half of it AND leaks internals (`_globalProxy`/…) — a window enumeration is a
// dead-giveaway bot check. We present this list instead.
let CHROME_WIN_PROPS = [];
try { CHROME_WIN_PROPS = JSON.parse(fs.readFileSync(new URL('./chrome_window_props.json', import.meta.url), 'utf8')); } catch { CHROME_WIN_PROPS = []; }

const CF_ORIGIN = 'https://challenges.cloudflare.com';

/* --------------------------- access recorder (Phase 1 instrumentation) ---------------------------
 * DEV-ONLY, gated by CF_ACCESS_LOG=<path>. Wrap the iframe realm's navigator/screen/performance + a
 * curated document/window/canvas/WebGL/getBoundingClientRect surface in logging proxies and record
 * EXACTLY what the PoW reads, in order, with phase markers (each postMessage event + the flow POST).
 * The ordered log is the ground truth of the fingerprint surface — far better than guessing. NEVER set
 * during a production solve (the wrappers' fn.toString leaks + identity changes can perturb behaviour). */
const ACCESS_LOG = process.env.CF_ACCESS_LOG || '';
const VMTRACE = !!process.env.CF_VMTRACE;  // deep-trace the PoW crypto ops (decision-window signal) into ACCESS_LOG
const REAL_TOSTRING = Function.prototype.toString;  // captured BEFORE spoofNatives patches the (Node-shared) FP.toString — clean source for VMTRACE handler dumps
const alog = [];
let aseq = 0;
function asmall(v) {
  try {
    const ty = typeof v;
    if (v === null) return 'null';
    if (ty === 'undefined') return 'undefined';
    if (ty === 'string') return v.length > 90 ? v.slice(0, 90) + '…(' + v.length + ')' : v;
    if (ty === 'number' || ty === 'boolean' || ty === 'bigint' || ty === 'symbol') return String(v);
    if (ty === 'function') return 'ƒ ' + (v.name || '');
    if (Array.isArray(v)) return 'Array(' + v.length + ')';
    return Object.prototype.toString.call(v);
  } catch { return '?'; }
}
function arec(kind, obj, key, val, extra) {
  if (!ACCESS_LOG) return;
  const e = { i: aseq++, dt: Date.now(), kind, o: obj, k: String(key) };
  if (kind === 'get' || kind === 'call' || kind === 'set') { e.ty = typeof val; e.v = asmall(val); }
  if (extra) Object.assign(e, extra);
  alog.push(e);
}
function arecRaw(kind, detail) {  // lightweight log row for VMTRACE (e.g. toString leaks)
  if (!ACCESS_LOG) return;
  alog.push({ i: aseq++, dt: Date.now(), kind, detail });
}
function amark(name, extra) {
  if (!ACCESS_LOG) return;
  alog.push(Object.assign({ i: aseq++, dt: Date.now(), mark: name }, extra || {}));
}
function dumpAccessLog() {
  if (!ACCESS_LOG) return;
  try { fs.writeFileSync(ACCESS_LOG, alog.map((e) => JSON.stringify(e)).join('\n') + '\n'); } catch {}
}
if (ACCESS_LOG) { process.on('exit', dumpAccessLog); setInterval(dumpAccessLog, 800).unref(); }

// walk the prototype chain for a property descriptor (jsdom puts doc/element props 1–2 protos up).
function findDesc(obj, key) {
  let o = obj;
  while (o) { const d = Object.getOwnPropertyDescriptor(o, key); if (d) return d; o = Object.getPrototypeOf(o); }
  return null;
}
// transparent logging Proxy: records top-level get / method-call / `in` / enumeration. Getters + methods
// run on the REAL target (receiver=target) so jsdom internal-slot accessors don't throw "Illegal invocation".
function recProxy(target, label) {
  const wrapCache = new Map();
  return new Proxy(target, {
    get(t, key) {
      let v;
      try { v = t[key]; } catch (e) { arec('get!', label, key, undefined, { err: String((e && e.message) || e) }); throw e; }
      if (typeof key === 'symbol') return v;
      arec('get', label, key, v);
      if (typeof v === 'function') {
        let w = wrapCache.get(key);
        if (!w) {
          w = function (...args) {
            let r;
            try { r = v.apply(t, args); } catch (e) { arec('call!', label, key, undefined, { err: String((e && e.message) || e), args: args.map(asmall) }); throw e; }
            arec('call', label, key, r, { args: args.map(asmall) });
            return r;
          };
          wrapCache.set(key, w);
        }
        return w;
      }
      return v;
    },
    has(t, key) { const r = key in t; if (typeof key !== 'symbol') arec('has', label, key, r); return r; },
    ownKeys(t) { const ks = Reflect.ownKeys(t); arec('ownKeys', label, '*', undefined, { keys: ks.filter((k) => typeof k === 'string') }); return ks; },
  });
}
// instrument one accessor/data prop in place (delegates to the real prototype descriptor, logs get+set).
function instrProp(obj, key, label) {
  const d = findDesc(obj, key);
  if (!d) return;
  const nd = { configurable: true, enumerable: !!d.enumerable };
  if (d.get || d.set) {
    if (d.get) nd.get = function () { let v; try { v = d.get.call(this); } catch (e) { v = undefined; } arec('get', label, key, v); return v; };
    if (d.set) nd.set = function (val) { arec('set', label, key, val); return d.set.call(this, val); };
    else nd.set = function () {};
  } else {
    let val = d.value;
    nd.get = function () { arec('get', label, key, val); return val; };
    nd.set = function (v) { arec('set', label, key, v); val = v; };
  }
  try { Object.defineProperty(obj, key, nd); } catch {}
}
// Install the recorder on ONE realm (parent / iframe / lazily-backed sub-frame). Idempotent + realm-
// labelled, so the log shows WHICH window each read came from — the PoW frequently reads a *pristine*
// fingerprint from a child iframe it creates (anti-tamper), which iframe-only instrumentation misses.
function installAccessRecorder(iwin, realm) {
  if (!ACCESS_LOG || !iwin || iwin.__arecOn) return;
  iwin.__arecOn = true;
  const R = realm || '?';
  const L = (n) => R + ':' + n;
  amark('realm-instrumented', { realm: R });
  const defWin = (k, getter) => { try { Object.defineProperty(iwin, k, { get: getter, configurable: true }); } catch {} };
  // Wrap accessor GETTERS in place (on the instance AND its prototype). This is the authoritative
  // interception point: it catches plain `obj.k` reads AND the anti-instrumentation pattern
  // `Object.getOwnPropertyDescriptor(Proto, k).get.call(obj)` that bypasses a wrapping Proxy. `only`
  // optionally restricts the keys (Document.prototype has hundreds of getters → too noisy unfiltered).
  const wrapGetters = (obj, label, only) => {
    if (!obj) return;
    const names = only || Object.getOwnPropertyNames(obj);
    for (const k of names) {
      let d; try { d = Object.getOwnPropertyDescriptor(obj, k); } catch { continue; }
      if (!d || typeof d.get !== 'function' || d.get.__arecW) continue;
      const og = d.get;
      const ng = function () { let v; try { v = og.call(this); } catch (e) { arec('get!', label, k, undefined, { err: String((e && e.message) || e) }); throw e; } arec('get', label, k, v); return v; };
      ng.__arecW = true;
      try { Object.defineProperty(obj, k, { configurable: true, enumerable: d.enumerable, get: ng, set: d.set }); } catch {}
    }
  };
  // Wrap data-property METHODS, logging call + result (getters can't capture method calls).
  const wrapMethods = (obj, label, keys) => {
    if (!obj) return;
    for (const k of keys) {
      let f; try { f = obj[k]; } catch { continue; }
      if (typeof f !== 'function' || f.__arecW) continue;
      const w = function (...args) { let r; try { r = f.apply(this, args); } catch (e) { arec('call!', label, k, undefined, { err: String((e && e.message) || e) }); throw e; } arec('call', label, k, r, { args: args.map(asmall) }); return r; };
      w.__arecW = true;
      try { Object.defineProperty(obj, k, { configurable: true, writable: true, enumerable: false, value: w }); } catch {}
    }
  };
  // Slim instance Proxy: logs ONLY non-getter reads (undefined probes like `navigator.gpu`, data props)
  // and for-in enumeration — getter reads are already captured by wrapGetters (avoids double-logging).
  const slimProxy = (target, label) => new Proxy(target, {
    get(t, key) {
      const v = t[key];
      if (typeof key === 'string') { let d; try { d = findDesc(t, key); } catch {} if (!d || typeof d.get !== 'function') arec('get', label + '#probe', key, v); }
      return v;
    },
    has(t, key) { const r = key in t; if (typeof key !== 'symbol') arec('has', label, key, r); return r; },
    ownKeys(t) { const ks = Reflect.ownKeys(t); arec('ownKeys', label, '*', undefined, { keys: ks.filter((k) => typeof k === 'string') }); return ks; },
  });

  // navigator: wrap prototype getters (userAgent/appVersion/…) + instance getters (our fingerprint) + methods.
  try { wrapGetters(iwin.Navigator && iwin.Navigator.prototype, L('navigator')); } catch {}
  try { wrapGetters(iwin.navigator, L('navigator')); } catch {}
  try { wrapMethods(iwin.navigator, L('navigator'), ['javaEnabled', 'sendBeacon', 'getGamepads', 'vibrate', 'requestMediaKeySystemAccess', 'getBattery']); } catch {}
  try { const p = slimProxy(iwin.navigator, L('navigator')); defWin('navigator', () => p); } catch (e) { log('arec nav: ' + e); }
  // screen: prototype + instance getters.
  try { wrapGetters(iwin.Screen && iwin.Screen.prototype, L('screen')); } catch {}
  try { wrapGetters(iwin.screen, L('screen')); } catch {}
  try { const p = slimProxy(iwin.screen, L('screen')); defWin('screen', () => p); } catch {}
  // performance: timing/resource methods (data-prop functions).
  try { wrapMethods(iwin.performance, L('performance'), ['now', 'getEntries', 'getEntriesByType', 'getEntriesByName', 'mark', 'measure']); } catch {}
  // document: curated fingerprint getters (both proto levels + instance) + a few methods.
  const DOC_KEYS = ['cookie', 'referrer', 'hidden', 'visibilityState', 'characterSet', 'charset', 'compatMode',
    'designMode', 'domain', 'readyState', 'documentURI', 'URL', 'lastModified', 'title', 'hasFocus', 'fonts'];
  try { wrapGetters(iwin.Document && iwin.Document.prototype, L('document'), DOC_KEYS); } catch {}
  try { wrapGetters(iwin.HTMLDocument && iwin.HTMLDocument.prototype, L('document'), DOC_KEYS); } catch {}
  try { wrapGetters(iwin.document, L('document'), DOC_KEYS); } catch {}
  try { wrapMethods(iwin.document, L('document'), ['hasFocus', 'getSelection', 'elementFromPoint']); } catch {}
  // getBoundingClientRect / getClientRects — the deobf calls these on the widget to compute its centre.
  try {
    const ep = iwin.Element && iwin.Element.prototype;
    ['getBoundingClientRect', 'getClientRects'].forEach((k) => {
      const orig = ep && ep[k];
      if (typeof orig !== 'function') return;
      Object.defineProperty(ep, k, { configurable: true, writable: true, value: function (...a) {
        const r = orig.apply(this, a);
        let dim = ''; try { dim = r ? `x=${r.x} y=${r.y} w=${r.width} h=${r.height}` : String(r); } catch {}
        arec('call', L('Element'), k, r, { el: (this && this.tagName) || '?', cls: (this && this.className) || '', dim });
        return r;
      } });
    });
  } catch {}
  // curated window globals the fingerprint commonly reads.
  ['chrome', 'devicePixelRatio', 'innerWidth', 'innerHeight', 'outerWidth', 'outerHeight', 'screenX', 'screenY',
   'screenLeft', 'screenTop', 'Notification', 'WebGLRenderingContext', 'WebGL2RenderingContext', 'OffscreenCanvas',
   'localStorage', 'sessionStorage', 'indexedDB', 'speechSynthesis', 'visualViewport', 'crypto'].forEach((k) => {
    try {
      const cur = Object.getOwnPropertyDescriptor(iwin, k) || findDesc(iwin, k);
      const getOrig = cur && cur.get ? () => cur.get.call(iwin) : () => (cur ? cur.value : undefined);
      defWin(k, function () { let v; try { v = getOrig(); } catch (e) { v = undefined; } arec('get', L('window'), k, v); return v; });
    } catch {}
  });
  // CF_VMTRACE: deep-trace the crypto the PoW uses. After gpu/UA/natives/surface were ruled out, the only
  // decision-window signal left (access log, between `execute` and the flow POST) is `crypto`. Log what the
  // bytecode actually computes: getRandomValues sizes/outputs + every subtle.* algo/input/output. If the
  // flow-vs-complete branch turns on a PoW value, it shows up here as a digest/sign the easy path uses.
  if (VMTRACE && iwin.crypto) {
    const hex = (b) => { try { const u = b instanceof ArrayBuffer ? new Uint8Array(b) : (b && b.buffer ? new Uint8Array(b.buffer, b.byteOffset, b.byteLength) : b); return Buffer.from(u.slice(0, 24)).toString('hex'); } catch { return '?'; } };
    try {
      const grv = iwin.crypto.getRandomValues && iwin.crypto.getRandomValues.bind(iwin.crypto);
      if (grv) { iwin.crypto.getRandomValues = function (a) { const r = grv(a); arec('call', L('crypto'), 'getRandomValues', undefined, { len: a && a.byteLength, out: hex(a) }); return r; }; try { iwin.__markNative && iwin.__markNative(iwin.crypto.getRandomValues, 'getRandomValues'); } catch {} }
    } catch {}
    try {
      const sub = iwin.crypto.subtle;
      if (sub) ['digest', 'sign', 'verify', 'importKey', 'deriveBits', 'deriveKey', 'encrypt', 'decrypt', 'generateKey'].forEach((m) => {
        const orig = sub[m] && sub[m].bind(sub);
        if (!orig) return;
        sub[m] = function (...args) {
          const algo = (args[0] && (args[0].name || args[0])) || args[0];
          let inLen, inHex;
          try { const data = args[args.length - 1]; if (data && data.byteLength != null) { inLen = data.byteLength; inHex = hex(data); } } catch {}
          arec('call', L('crypto.subtle'), m, undefined, { algo: String(algo), inLen, inHex });
          const p = orig(...args);
          if (p && typeof p.then === 'function') return p.then((res) => { try { arec('call', L('crypto.subtle'), m + ':out', undefined, { outLen: res && res.byteLength, outHex: hex(res) }); } catch {} return res; });
          return p;
        };
      });
    } catch {}
  }
  log('access recorder installed on realm ' + R);
}

/* ----------------------------- stdio + network bridge ----------------------------- */
const rl = readline.createInterface({ input: process.stdin });
const streams = new Map();  // id -> { onHead, onChunk, onEnd, onErr }
let nextId = 1;
let config = null;
let configResolve;
const configReady = new Promise((r) => { configResolve = r; });

function send(obj) { process.stdout.write(JSON.stringify(obj) + '\n'); }
function log(msg) { if (config && config.debug) send({ t: 'log', msg }); }

rl.on('line', (line) => {
  let msg;
  try { msg = JSON.parse(line); } catch { return; }
  if (msg.t === 'config') { config = msg; configResolve(); return; }
  const s = streams.get(msg.id);
  if (!s) return;
  if (msg.t === 'net-head') { s.onHead && s.onHead(msg); }
  else if (msg.t === 'net-chunk') { s.onChunk && s.onChunk(msg.dataB64); }
  else if (msg.t === 'net-end') { streams.delete(msg.id); s.onEnd && s.onEnd(); }
  else if (msg.t === 'net-err') { streams.delete(msg.id); s.onErr && s.onErr(new Error(msg.msg || 'net error')); }
});

// Streaming request: Python emits net-head -> net-chunk* -> net-end. Handlers fire as data arrives — the
// XHR shim uses this for incremental readyState=3 (the `…/flow/…` long-poll trickles its body).
function bridgeStream(method, url, headers, bodyB64, handlers) {
  const id = nextId++;
  log('NET ' + (method || 'GET') + ' ' + String(url).slice(0, 120));
  if (ACCESS_LOG && /\/flow\//.test(String(url))) amark('flow-post', { method, url: String(url), bodyLen: bodyB64 ? Buffer.from(bodyB64, 'base64').length : 0 });
  // VMTRACE: stamp the opcode count at the moment the flow POST fires, so the trace can be sliced to the
  // exact branch (the few opcodes BEFORE this index chose flow-over-complete).
  if (globalThis.__VMTWIN && /\/flow\//.test(String(url)) && globalThis.__VMTWIN.__vmtFlowN == null) globalThis.__VMTWIN.__vmtFlowN = globalThis.__VMTWIN.__vmtN;
  streams.set(id, handlers);
  send({ t: 'net', id, method: method || 'GET', url: String(url), headers: headers || null, bodyB64: bodyB64 || null });
  return id;
}

// Buffered convenience (fetch/sendBeacon): collect the whole stream, resolve like the old single response.
function bridgeRequest(method, url, headers, bodyB64) {
  return new Promise((resolve, reject) => {
    let head = null; const chunks = [];
    bridgeStream(method, url, headers, bodyB64, {
      onHead: (h) => { head = h; },
      onChunk: (b64) => { if (b64) chunks.push(Buffer.from(b64, 'base64')); },
      onEnd: () => {
        const body = Buffer.concat(chunks);
        resolve({ status: head ? head.status : 0, statusText: head ? head.statusText : '', url: head ? head.url : url,
          headers: head ? head.headers : {}, bodyB64: body.toString('base64') });
      },
      onErr: reject,
    });
  });
}

// Synchronous variant is impossible over async stdio; the challenge uses async fetch + async XHR, so a
// Promise-returning bridge is fine. Returns {status, statusText, url, headers, bodyB64}.
const TEXT_CT = ['text/', 'application/json', 'application/javascript', 'text/javascript',
  'application/x-www-form-urlencoded', '+json', 'image/svg'];
const isText = (ct) => TEXT_CT.some((m) => (ct || '').toLowerCase().includes(m));
const b64ToStr = (b) => Buffer.from(b || '', 'base64').toString('utf8');
const strToB64 = (s) => Buffer.from(s == null ? '' : String(s), 'utf8').toString('base64');
// Request bodies may be BINARY (the PoW posts an encrypted ArrayBuffer/typed-array). `String(body)` would
// corrupt it ("[object ArrayBuffer]") -> the server can't parse it -> no response. Encode by real type.
// Tags are checked via Object.prototype.toString + ArrayBuffer.isView so it works across the jsdom realm.
function bodyToB64(b) {
  if (b == null) return null;
  if (typeof b === 'string') return Buffer.from(b, 'utf8').toString('base64');
  const tag = Object.prototype.toString.call(b);
  if (tag === '[object ArrayBuffer]') return Buffer.from(new Uint8Array(b)).toString('base64');
  if (ArrayBuffer.isView(b)) return Buffer.from(b.buffer, b.byteOffset || 0, b.byteLength).toString('base64');
  if (tag === '[object URLSearchParams]' || (b && b.constructor && b.constructor.name === 'URLSearchParams')) return Buffer.from(b.toString(), 'utf8').toString('base64');
  return Buffer.from(String(b), 'utf8').toString('base64');
}

/* ------------------------------- per-window net shims ------------------------------ */
function makeHeaders(obj) {
  const o = obj || {};
  return {
    get: (k) => (k && o[k.toLowerCase()] != null ? o[k.toLowerCase()] : null),
    has: (k) => !!(k && k.toLowerCase() in o),
    forEach: (cb) => { for (const k in o) cb(o[k], k); },
    entries: function* () { for (const k in o) yield [k, o[k]]; },
  };
}

function installNet(win) {
  // A browser auto-attaches Referer/Origin/Sec-Fetch-* to fetch/XHR. The challenge-platform `…/flow/…`
  // POST is a security endpoint that STALLS a submission missing them (the VM only sets cf-chl/cf-chl-ra),
  // which looked like a trust-stall. Add them from the iframe's real URL (set on the window in bootIframe).
  function withBrowserHeaders(h, method) {
    const out = Object.assign({}, h || {});
    const has = (k) => Object.keys(out).some((x) => x.toLowerCase() === k);
    let href = ''; let origin = '';
    try { href = win.__refHref || (win.location && win.location.href) || ''; } catch {}
    try { origin = win.__refOrigin || (win.location && win.location.origin) || ''; } catch {}
    if (href && href !== 'about:blank' && !has('referer')) out['Referer'] = href;
    if (origin && origin !== 'null' && !has('origin') && String(method || 'GET').toUpperCase() !== 'GET') out['Origin'] = origin;
    if (!has('sec-fetch-site')) out['Sec-Fetch-Site'] = 'same-origin';
    if (!has('sec-fetch-mode')) out['Sec-Fetch-Mode'] = 'cors';
    if (!has('sec-fetch-dest')) out['Sec-Fetch-Dest'] = 'empty';
    if (!has('accept')) out['Accept'] = '*/*';
    // high-entropy UA client hints the challenge demands via Critical-CH (coherent with UA + JS uaData)
    const ch = clientHints();
    for (const k in ch) if (!has(k)) out[k] = ch[k];
    if (!has('priority')) out['priority'] = 'u=1, i';  // Chrome H2 priority for fetch/XHR
    return out;
  }
  win.__withBrowserHeaders = withBrowserHeaders;
  win.fetch = function (url, opts) {
    opts = opts || {};
    const u = (url && url.url) ? url.url : url;
    return bridgeRequest(opts.method, u, withBrowserHeaders(opts.headers, opts.method), bodyToB64(opts.body))
      .then((r) => {
        const text = b64ToStr(r.bodyB64);
        return {
          ok: r.status >= 200 && r.status < 300, status: r.status, statusText: r.statusText || '',
          url: r.url || u, redirected: false, type: 'basic', headers: makeHeaders(r.headers),
          text: () => Promise.resolve(text),
          json: () => Promise.resolve(JSON.parse(text)),
          arrayBuffer: () => Promise.resolve(Uint8Array.from(Buffer.from(r.bodyB64 || '', 'base64')).buffer),
          clone() { return this; },
        };
      });
  };

  function XHR() {
    this.readyState = 0; this.status = 0; this.statusText = ''; this.responseText = ''; this.response = '';
    this.responseURL = ''; this.responseType = ''; this._m = 'GET'; this._u = ''; this._h = {}; this._rh = {};
    this.onreadystatechange = this.onload = this.onerror = this.onloadend = this.onprogress = null;
    this.withCredentials = false; this.timeout = 0; this.upload = {};
  }
  XHR.prototype.open = function (m, u) { this._m = m; this._u = u; this.readyState = 1; this.onreadystatechange && this.onreadystatechange(); };
  XHR.prototype.setRequestHeader = function (k, v) { this._h[k] = v; };
  XHR.prototype.getResponseHeader = function (k) { return this._rh[String(k).toLowerCase()] || null; };
  XHR.prototype.getAllResponseHeaders = function () { return Object.entries(this._rh).map(([k, v]) => k + ': ' + v).join('\r\n'); };
  XHR.prototype.abort = function () {};
  XHR.prototype.addEventListener = function (t, cb) { if (t === 'load') this.onload = cb; else if (t === 'error') this.onerror = cb; else if (t === 'readystatechange') this.onreadystatechange = cb; else if (t === 'loadend') this.onloadend = cb; else if (t === 'progress') this.onprogress = cb; };
  XHR.prototype.removeEventListener = function () {};
  // Stream the response: fire readyState 2 (head) -> 3 (per chunk, with partial responseText) -> 4 (end).
  // The challenge's `…/flow/…` POST trickles its body; the VM acts on the first chunk, so a buffered
  // (readyState-4-only) response would deadlock against its overrun watchdog.
  XHR.prototype.send = function (body) {
    const xhr = this; const chunks = [];
    bridgeStream(this._m, this._u, withBrowserHeaders(this._h, this._m), bodyToB64(body), {
      onHead: (h) => {
        xhr.status = h.status; xhr.statusText = h.statusText || ''; xhr._rh = h.headers || {};
        xhr.responseURL = h.url || xhr._u; xhr.readyState = 2; xhr.onreadystatechange && xhr.onreadystatechange();
      },
      onChunk: (b64) => {
        if (b64) chunks.push(Buffer.from(b64, 'base64'));
        const rt = xhr.responseType || '';
        if (rt === '' || rt === 'text') { xhr.responseText = Buffer.concat(chunks).toString('utf8'); xhr.response = xhr.responseText; }
        xhr.readyState = 3; xhr.onreadystatechange && xhr.onreadystatechange();
        xhr.onprogress && xhr.onprogress({ lengthComputable: false, loaded: chunks.reduce((a, c) => a + c.length, 0), total: 0 });
      },
      onEnd: () => {
        const buf = Buffer.concat(chunks); const rt = xhr.responseType || '';
        if (rt === 'arraybuffer') { xhr.response = win.Uint8Array.from(buf).buffer; }
        else if (rt === 'json') { xhr.responseText = buf.toString('utf8'); try { xhr.response = JSON.parse(xhr.responseText); } catch { xhr.response = null; } }
        else { xhr.responseText = buf.toString('utf8'); xhr.response = xhr.responseText; }
        xhr.readyState = 4; xhr.onreadystatechange && xhr.onreadystatechange();
        xhr.onload && xhr.onload(); xhr.onloadend && xhr.onloadend();
      },
      onErr: (e) => { xhr.onerror && xhr.onerror(e); xhr.onloadend && xhr.onloadend(); },
    });
  };
  win.XMLHttpRequest = XHR;

  if (!win.navigator) win.navigator = {};
  try { win.navigator.sendBeacon = (url, data) => { bridgeRequest('POST', url, null, bodyToB64(data)).catch(() => {}); return true; }; } catch {}
}

/* --------------------------- navigator / screen fingerprint ------------------------ */
function applyFingerprint(win, fp) {
  const nav = win.navigator;
  const def = (obj, k, v) => { try { Object.defineProperty(obj, k, { get: () => v, configurable: true }); } catch {} };
  // jsdom's auto-created CHILD-FRAME windows (the challenge iframe realm) do NOT inherit the top JSDOM's
  // configured userAgent — they fall back to jsdom's default "…jsdom/26.1.0". That leaks in every realm
  // (a fatal within-realm bot tell + poisons the flow body). Force the real Chrome UA on every realm here.
  if (fp.user_agent) { def(nav, 'userAgent', fp.user_agent); def(nav, 'appVersion', fp.user_agent.replace(/^Mozilla\//, '')); }
  if (fp.platform) def(nav, 'platform', fp.platform);
  if (fp.language) def(nav, 'language', fp.language);
  if (fp.languages) def(nav, 'languages', Object.freeze(fp.languages.slice ? fp.languages : [fp.language]));
  if (fp.hardware_concurrency) def(nav, 'hardwareConcurrency', fp.hardware_concurrency);
  if (fp.device_memory) def(nav, 'deviceMemory', fp.device_memory);
  if (fp.vendor != null) def(nav, 'vendor', fp.vendor);
  def(nav, 'webdriver', false);
  // surface real Chrome exposes that jsdom omits (0 plugins/mimeTypes is a bot tell — real Chrome has 5/2)
  def(nav, 'maxTouchPoints', 0);
  def(nav, 'productSub', '20030107');
  def(nav, 'vendorSub', '');
  def(nav, 'pdfViewerEnabled', true);
  try {
    const pdfNames = ['PDF Viewer', 'Chrome PDF Viewer', 'Chromium PDF Viewer', 'Microsoft Edge PDF Viewer', 'WebKit built-in PDF'];
    const mt = [{ type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format' },
                { type: 'text/pdf', suffixes: 'pdf', description: 'Portable Document Format' }];
    const plugins = pdfNames.map((n) => ({ name: n, filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: mt.length, item: (i) => mt[i] || null, namedItem: (t) => mt.find((m) => m.type === t) || null }));
    plugins.item = (i) => plugins[i] || null; plugins.namedItem = (n) => plugins.find((p) => p.name === n) || null; plugins.refresh = () => {};
    const mimeTypes = mt.map((m) => ({ ...m, enabledPlugin: plugins[0] }));
    mimeTypes.item = (i) => mimeTypes[i] || null; mimeTypes.namedItem = (t) => mimeTypes.find((m) => m.type === t) || null;
    def(nav, 'plugins', plugins); def(nav, 'mimeTypes', mimeTypes);
  } catch {}
  try {
    const major = (/Chrome\/(\d+)/.exec(fp.user_agent || '') || [, '144'])[1];
    const brands = [{ brand: 'Not(A:Brand', version: '8' }, { brand: 'Chromium', version: major }, { brand: 'Google Chrome', version: major }];
    const uaPlat = fp.os_family === 'windows' ? 'Windows' : (fp.os_family === 'macos' ? 'macOS' : 'Linux');
    def(nav, 'userAgentData', {
      brands, mobile: false, platform: uaPlat,
      getHighEntropyValues: () => Promise.resolve({ architecture: 'x86', bitness: '64', brands,
        fullVersionList: brands.map((b) => ({ brand: b.brand, version: b.brand === 'Not(A:Brand' ? '8.0.0.0' : major + '.0.0.0' })),
        mobile: false, model: '', platform: uaPlat, platformVersion: fp.os_family === 'linux' ? '6.8.0' : '15.0.0', uaFullVersion: major + '.0.0.0' }),
      toJSON: () => ({ brands, mobile: false, platform: uaPlat }),
    });
  } catch {}
  // navigator.gpu (WebGPU) — present on real desktop Chrome incl. Linux; jsdom omits it (the VM probes it
  // right before deciding the flow-POST vs complete branch). Return a realistic adapter, not a null one.
  try {
    if (!nav.gpu) {
      // A real Chrome's webgpu objects each carry a Symbol.toStringTag, so
      // Object.prototype.toString.call(navigator.gpu) === "[object GPU]" (NOT "[object Object]"). A plain
      // object leaks "[object Object]" — a fake-gpu tell the VM reads right before the flow-vs-complete
      // branch. Tag every node of the tree with its real interface name.
      const tag = (o, t) => { try { Object.defineProperty(o, Symbol.toStringTag, { value: t, configurable: true }); } catch {} return o; };
      // Real Chrome's webgpu interfaces put their members on the PROTOTYPE (non-enumerable), so the
      // instance has NO own enumerable props: ground truth (uc, _uc_ifenv.json) is
      // Object.keys(navigator.gpu) === []  and the proto chain toStrings as [GPU, GPU, Object]. A plain
      // object with own methods leaks Object.keys() = [...methods] — a fake-gpu tell. Build proper
      // prototype-backed instances: `proto(tag, members)` -> a prototype tagged `tag` whose members are
      // non-enumerable; `inst(p)` -> an own-prop-free instance of it.
      const proto = (t, members) => { const p = tag({}, t); for (const k in members) { try { Object.defineProperty(p, k, { value: members[k], enumerable: false, configurable: true, writable: true }); } catch {} } return p; };
      const inst = (p) => Object.create(p);
      const FeaturesProto = proto('GPUSupportedFeatures', { size: 12, has: () => true, forEach() {}, keys: function* () {}, values: function* () {}, entries: function* () {}, [Symbol.iterator]: function* () {} });
      const LimitsProto = proto('GPUSupportedLimits', { maxTextureDimension2D: 16384, maxBindGroups: 4, maxBufferSize: 2147483648, maxComputeWorkgroupSizeX: 256 });
      const AdapterInfoProto = proto('GPUAdapterInfo', { vendor: 'intel', architecture: 'gen-9', device: '', description: '' });
      const DeviceProto = proto('GPUDevice', { features: inst(FeaturesProto), limits: inst(LimitsProto), queue: tag({}, 'GPUQueue'), destroy() {}, createBuffer: () => tag({}, 'GPUBuffer'), addEventListener() {} });
      const AdapterProto = proto('GPUAdapter', {
        features: inst(FeaturesProto), limits: inst(LimitsProto), info: inst(AdapterInfoProto), isFallbackAdapter: false,
        requestDevice: () => Promise.resolve(inst(DeviceProto)),
        requestAdapterInfo: () => Promise.resolve(inst(AdapterInfoProto)),
      });
      const WgslProto = proto('WGSLLanguageFeatures', { size: 4, has: () => true, forEach() {}, keys: function* () {}, values: function* () {}, entries: function* () {}, [Symbol.iterator]: function* () {} });
      const GpuProto = proto('GPU', {
        requestAdapter: () => Promise.resolve(inst(AdapterProto)),
        getPreferredCanvasFormat: () => 'bgra8unorm',
        wgslLanguageFeatures: inst(WgslProto),
      });
      def(nav, 'gpu', inst(GpuProto));
    }
  } catch {}
  const scr = win.screen;
  if (fp.screen_width) { def(scr, 'width', fp.screen_width); def(scr, 'availWidth', fp.avail_width || fp.screen_width); }
  if (fp.screen_height) { def(scr, 'height', fp.screen_height); def(scr, 'availHeight', fp.avail_height || fp.screen_height); }
  if (fp.color_depth) { def(scr, 'colorDepth', fp.color_depth); def(scr, 'pixelDepth', fp.color_depth); }
  try { win.innerWidth = fp.screen_width || 1920; win.innerHeight = fp.avail_height || 1080; } catch {}
  try { win.outerWidth = fp.screen_width || 1920; win.outerHeight = fp.screen_height || 1080; } catch {}
  if (fp.pixel_ratio) { try { Object.defineProperty(win, 'devicePixelRatio', { get: () => fp.pixel_ratio, configurable: true }); } catch {} }
}

/* --------------------- browser APIs jsdom lacks (challenge probes them) ------------ */
function augmentWindow(win) {
  const W = win;
  // window.chrome — present on every real Chrome; its absence is a headless/bot tell. jsdom lacks it.
  if (!W.chrome) {
    W.chrome = {
      runtime: {}, app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed', NOT_INSTALLED: 'not_installed' }, RunningState: { CANNOT_RUN: 'cannot_run', READY_TO_RUN: 'ready_to_run', RUNNING: 'running' } },
      loadTimes: function () { return { requestTime: Date.now() / 1000, startLoadTime: Date.now() / 1000, commitLoadTime: Date.now() / 1000, finishLoadTime: Date.now() / 1000, firstPaintTime: Date.now() / 1000, navigationType: 'Other', wasFetchedViaSpdy: true, wasNpnNegotiated: true, npnNegotiatedProtocol: 'h2', wasAlternateProtocolAvailable: false, connectionInfo: 'h2' }; },
      csi: function () { return { startE: Date.now(), onloadT: Date.now(), pageT: 1000, tran: 15 }; },
    };
  }
  // jsdom tags the iframe document as [object Document]; real Chrome is [object HTMLDocument].
  try { if (W.document && Object.prototype.toString.call(W.document) !== '[object HTMLDocument]') Object.defineProperty(W.document, Symbol.toStringTag, { value: 'HTMLDocument', configurable: true }); } catch {}
  if (typeof W.matchMedia !== 'function') {
    W.matchMedia = (q) => ({ matches: false, media: q || '', onchange: null, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return false; } });
  }
  if (typeof W.IntersectionObserver !== 'function') {
    W.IntersectionObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} takeRecords() { return []; } };
  }
  if (typeof W.ResizeObserver !== 'function') {
    W.ResizeObserver = class { constructor() {} observe() {} unobserve() {} disconnect() {} };
  }
  if (typeof W.PerformanceObserver !== 'function') {
    W.PerformanceObserver = class { constructor() {} observe() {} disconnect() {} takeRecords() { return []; } };
  }
  if (typeof W.requestIdleCallback !== 'function') {
    W.requestIdleCallback = (cb) => W.setTimeout(() => cb({ didTimeout: false, timeRemaining: () => 50 }), 1);
    W.cancelIdleCallback = () => {};
  }
  // Cloudflare's `unsupported_browser` gate (fn PP) requires all of these — jsdom lacks Worker,
  // URL.createObjectURL and ReadableStream. Stub them (the gate only checks construct-without-throw +
  // ReadableStream.prototype.pipeTo !== undefined; it never runs the worker).
  if (typeof W.Worker !== 'function') {
    W.Worker = class { constructor() {} postMessage() {} terminate() {} addEventListener() {} removeEventListener() {} get onmessage() { return null; } set onmessage(_) {} };
    W.SharedWorker = W.Worker;
  }
  if (W.URL && typeof W.URL.createObjectURL !== 'function') {
    W.URL.createObjectURL = () => 'blob:' + (W.location ? W.location.origin : 'https://x') + '/' + 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => { const r = (Math.random() * 16) | 0; return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16); });
    W.URL.revokeObjectURL = () => {};
  }
  if (typeof W.ReadableStream !== 'function') {
    W.ReadableStream = class { constructor() { this.locked = false; } getReader() { return { read: () => Promise.resolve({ done: true, value: undefined }), releaseLock() {}, cancel: () => Promise.resolve() }; } pipeTo() { return Promise.resolve(); } pipeThrough(t) { return (t && t.readable) || new W.ReadableStream(); } tee() { return [new W.ReadableStream(), new W.ReadableStream()]; } cancel() { return Promise.resolve(); } };
  }
  // crypto.subtle (SHA-256 etc. for the PoW) — jsdom omits it; wire Node's WebCrypto.
  try { if (W.crypto && !W.crypto.subtle && globalThis.crypto && globalThis.crypto.subtle) Object.defineProperty(W.crypto, 'subtle', { value: globalThis.crypto.subtle, configurable: true }); } catch {}
  try { if (W.crypto && typeof W.crypto.randomUUID !== 'function' && globalThis.crypto) W.crypto.randomUUID = () => globalThis.crypto.randomUUID(); } catch {}
  // jsdom's performance has only now()/timeOrigin — the challenge calls getEntries()/getEntriesByType()
  // (resource-timing fingerprint) + mark/measure. Stub them (empty timings -> a benign, if thin, signal).
  const perf = W.performance;
  if (perf && typeof perf.getEntries !== 'function') {
    const set = (k, v) => { try { perf[k] = v; } catch { try { Object.defineProperty(perf, k, { value: v, configurable: true }); } catch {} } };
    // A real challenge-iframe realm has at least its own PerformanceNavigationTiming entry; jsdom has none
    // (empty getEntries reads as "document not really loaded"). Synthesize a faithful navigation entry.
    const navEntry = () => {
      const url = (() => { try { return W.__refHref || (W.location && W.location.href) || 'about:blank'; } catch { return 'about:blank'; } })();
      return { name: url, entryType: 'navigation', startTime: 0, duration: 312, type: 'navigate', initiatorType: 'navigation',
        nextHopProtocol: 'h2', redirectCount: 0, workerStart: 0, redirectStart: 0, redirectEnd: 0,
        fetchStart: 1.2, domainLookupStart: 1.2, domainLookupEnd: 1.2, connectStart: 1.2, secureConnectionStart: 3.1, connectEnd: 18.4,
        requestStart: 19.1, responseStart: 41.7, responseEnd: 44.2, transferSize: 261300, encodedBodySize: 261002, decodedBodySize: 261002,
        domInteractive: 180, domContentLoadedEventStart: 181, domContentLoadedEventEnd: 182, domComplete: 305, loadEventStart: 306, loadEventEnd: 308,
        unloadEventStart: 0, unloadEventEnd: 0, toJSON() { return this; } };
    };
    set('getEntries', () => [navEntry()]);
    set('getEntriesByType', (t) => (t === 'navigation' ? [navEntry()] : []));
    set('getEntriesByName', (n) => { const e = navEntry(); return e.name === n ? [e] : []; });
    set('mark', () => {});
    set('measure', () => {});
    set('clearMarks', () => {});
    set('clearMeasures', () => {});
    set('clearResourceTimings', () => {});
    set('setResourceTimingBufferSize', () => {});
    const t0 = Date.now() - 1200;
    set('timing', { navigationStart: t0, fetchStart: t0 + 2, domainLookupStart: t0 + 3, domainLookupEnd: t0 + 4, connectStart: t0 + 5, connectEnd: t0 + 20, secureConnectionStart: t0 + 8, requestStart: t0 + 22, responseStart: t0 + 60, responseEnd: t0 + 80, domLoading: t0 + 85, domInteractive: t0 + 200, domContentLoadedEventStart: t0 + 210, domContentLoadedEventEnd: t0 + 215, domComplete: t0 + 300, loadEventStart: t0 + 305, loadEventEnd: t0 + 310, unloadEventStart: 0, unloadEventEnd: 0 });
    set('navigation', { type: 0, redirectCount: 0 });
  }
  // jsdom has no `innerText` (getter returns undefined; setter makes a junk own-prop) — the VM uses it
  // heavily to read/copy text + the inline <style> into the shadow root. Alias it to textContent.
  try {
    const hp = W.HTMLElement && W.HTMLElement.prototype;
    if (hp && !(Object.getOwnPropertyDescriptor(hp, 'innerText') || {}).get) {
      Object.defineProperty(hp, 'innerText', {
        get() { return this.textContent; }, set(v) { this.textContent = v == null ? '' : String(v); }, configurable: true,
      });
    }
  } catch {}
  // canvas: back 2D with REAL Cairo rendering (node-canvas) so toDataURL/getImageData are real pixels
  // (the PoW hashes them; a fixed stub is an obvious bot tell). WebGL stays a faithful-value stub.
  // Falls back to the no-op stub if node-canvas isn't installed.
  try {
    const proto = W.HTMLCanvasElement && W.HTMLCanvasElement.prototype;
    if (proto) {
      const fp = config.fingerprint || {};
      const STUB_PNG = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQAY3Y2wAAAAAElFTkSuQmCC';
      const ncFor = (el) => {
        const w = Math.max(1, el.width || 300), h = Math.max(1, el.height || 150);
        if (!el.__nc || el.__ncW !== w || el.__ncH !== h) { el.__nc = _createCanvas(w, h); el.__ncW = w; el.__ncH = h; }
        return el.__nc;
      };
      proto.getContext = function (type) {
        if (type && String(type).includes('webgl')) return makeGL(fp);
        if (_createCanvas) { try { return ncFor(this).getContext('2d'); } catch {} }
        return makeCanvas2D();
      };
      proto.toDataURL = function (...a) {
        let r = STUB_PNG;
        if (_createCanvas && this.__nc) { try { r = this.__nc.toDataURL(...a); } catch {} }
        arec('call', 'canvas', 'toDataURL', undefined, { args: a.map(asmall), len: r.length, head: r.slice(0, 40), w: this.width, h: this.height });
        return r;
      };
      proto.toBlob = function (cb) {
        try { if (_createCanvas && this.__nc) { const b = this.__nc.toBuffer('image/png'); return cb && cb({ size: b.length, type: 'image/png', arrayBuffer: () => Promise.resolve(Uint8Array.from(b).buffer) }); } } catch {}
        return cb && cb({ size: 68, type: 'image/png' });
      };
    }
  } catch {}
  // jsdom has NO layout engine → getBoundingClientRect/getClientRects are all-zero. api.js measures the
  // rendered widget (~300x65); a 0x0 box reads as an invisible/headless widget. Give elements a plausible
  // non-zero box (viewport for root, widget-sized otherwise) so layout-derived signals look real.
  try {
    const EP = W.Element && W.Element.prototype;
    if (EP && !EP.__rectFaked) {
      EP.__rectFaked = true;
      const rectFor = (el) => {
        const tag = ((el && el.tagName) || '').toUpperCase();
        let w = 300, h = 65, x = 16, y = 220;
        if (tag === 'HTML' || tag === 'BODY') { w = W.innerWidth || 1280; h = W.innerHeight || 720; x = 0; y = 0; }
        return { x, y, width: w, height: h, top: y, left: x, right: x + w, bottom: y + h, toJSON() { return this; } };
      };
      EP.getBoundingClientRect = function () { return rectFor(this); };
      EP.getClientRects = function () { const r = rectFor(this); const list = [r]; list.item = (i) => (i === 0 ? r : null); return list; };
    }
  } catch {}
}
function makeCanvas2D() {
  return { canvas: { width: 300, height: 150 }, fillRect() {}, clearRect() {}, fillText() {}, strokeText() {}, beginPath() {}, closePath() {}, moveTo() {}, lineTo() {}, arc() {}, fill() {}, stroke() {}, rect() {}, save() {}, restore() {}, translate() {}, rotate() {}, scale() {}, setTransform() {}, transform() {}, clip() {}, measureText: (t) => ({ width: (t ? String(t).length : 0) * 7 }), getImageData: (x, y, w, h) => ({ data: new Uint8ClampedArray((w || 1) * (h || 1) * 4), width: w || 1, height: h || 1 }), putImageData() {}, createLinearGradient: () => ({ addColorStop() {} }), createRadialGradient: () => ({ addColorStop() {} }), createPattern: () => ({}), drawImage() {}, setLineDash() {}, getLineDash: () => [] };
}
// Exact WebGL extension set a real Chrome (ANGLE/Intel/Mesa) exposes on this machine — captured ground
// truth (poc-infra-pa/tools/selenium_capture/out/env_page.json). A short/wrong list is a fingerprint tell.
const GL_EXTENSIONS = ['ANGLE_instanced_arrays', 'EXT_blend_minmax', 'EXT_clip_control', 'EXT_color_buffer_half_float', 'EXT_depth_clamp', 'EXT_disjoint_timer_query', 'EXT_float_blend', 'EXT_frag_depth', 'EXT_polygon_offset_clamp', 'EXT_texture_compression_bptc', 'EXT_texture_compression_rgtc', 'EXT_texture_filter_anisotropic', 'EXT_texture_mirror_clamp_to_edge', 'EXT_sRGB', 'KHR_parallel_shader_compile', 'OES_element_index_uint', 'OES_fbo_render_mipmap', 'OES_standard_derivatives', 'OES_texture_float', 'OES_texture_float_linear', 'OES_texture_half_float', 'OES_texture_half_float_linear', 'OES_vertex_array_object', 'WEBGL_blend_func_extended', 'WEBGL_color_buffer_float', 'WEBGL_compressed_texture_astc', 'WEBGL_compressed_texture_etc', 'WEBGL_compressed_texture_etc1', 'WEBGL_compressed_texture_s3tc', 'WEBGL_compressed_texture_s3tc_srgb', 'WEBGL_debug_renderer_info', 'WEBGL_debug_shaders', 'WEBGL_depth_texture', 'WEBGL_draw_buffers', 'WEBGL_lose_context', 'WEBGL_multi_draw'];
function makeGL(fp) {
  // enum->value, matching a real WebGL1 context on the captured machine.
  const params = {
    7936: 'WebKit', 7937: 'WebKit WebGL', 7938: 'WebGL 1.0 (OpenGL ES 2.0 Chromium)',
    35724: 'WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)',
    37445: fp.webgl_vendor || 'Google Inc. (Intel)',
    37446: fp.webgl_renderer || 'ANGLE (Intel, Mesa Intel(R) HD Graphics 620 (KBL GT2), OpenGL ES 3.2)',
    3379: 16384, 34024: 16384, 34076: 16384, 3386: new Int32Array([16384, 16384]),
    33901: new Float32Array([1, 1024]), 33902: new Float32Array([1, 7.375]),
    34921: 16, 35660: 16, 35661: 80, 36347: 1024, 36348: 32, 36349: 1024, 35071: 16384, 34930: 16,
    3413: 8, 3414: 8, 3415: 8, 3412: 8, 3411: 8, 3410: 8, 36063: 8, 34852: 8,
  };
  return { canvas: { width: 300, height: 150 }, drawingBufferWidth: 300, drawingBufferHeight: 150,
    getParameter: (p) => { const r = (p in params ? params[p] : 0); arec('call', 'webgl', 'getParameter', r, { args: [p] }); return r; },
    getExtension: (n) => { const r = (n === 'WEBGL_debug_renderer_info' ? { UNMASKED_VENDOR_WEBGL: 37445, UNMASKED_RENDERER_WEBGL: 37446 } : (GL_EXTENSIONS.indexOf(n) >= 0 ? {} : null)); arec('call', 'webgl', 'getExtension', r, { args: [n] }); return r; },
    getSupportedExtensions: () => { const r = GL_EXTENSIONS.slice(); arec('call', 'webgl', 'getSupportedExtensions', r); return r; },
    getContextAttributes: () => ({ alpha: true, antialias: true, depth: true, stencil: false, premultipliedAlpha: true, preserveDrawingBuffer: false, powerPreference: 'default', failIfMajorPerformanceCaveat: false }),
    getShaderPrecisionFormat: () => ({ rangeMin: 127, rangeMax: 127, precision: 23 }), getParameterErr() {},
    createBuffer() { return {}; }, bindBuffer() {}, bufferData() {}, createShader() { return {}; }, shaderSource() {}, compileShader() {}, createProgram() { return {}; }, attachShader() {}, linkProgram() {}, useProgram() {}, getProgramParameter: () => true, getShaderParameter: () => true, getAttribLocation: () => 0, getUniformLocation: () => ({}), enableVertexAttribArray() {}, vertexAttribPointer() {}, viewport() {}, clearColor() {}, clear() {}, enable() {}, disable() {}, drawArrays() {}, readPixels() {} };
}

// Our network/feature shims are plain JS functions — `fn.toString()` reveals their source, a dead
// giveaway vs a real browser where `fetch/Worker/eval/...` report `[native code]`. Give the shims an
// own `toString` (realm-independent; the VM's shims live in the Node realm) that mimics a native fn.
// SHARED across every realm (same Node heap). Anti-tamper in the challenge VM grabs a PRISTINE child
// iframe's `Function.prototype.toString` and calls it on the MAIN realm's functions to bypass a per-realm
// toString spoof: with a per-realm `marked` set, the child's toString doesn't recognise a main-realm shim
// → falls through to the real toString → reveals our source → bot. A module-shared map makes every realm's
// patched toString report [native code] for ANY realm's marked shim — consistent like real natives.
const NATIVE_MARKED = new WeakMap();  // fn -> displayed name
const BOUND_TARGETS = new WeakMap();  // boundFn -> the fn it was bound from (VMTRACE: read past the VM's bound-handler anti-RE)
const VM_STATE = new WeakMap();       // boundHandler -> its bound `this` (the VM state: .g=regs, .i/.j/.l indices, .h=xor key) — lets the tracer read PC + register operands at the branch
function spoofNatives(win) {
  const nativeStr = (name) => 'function ' + (name || '') + '() { [native code] }';
  const marked = NATIVE_MARKED;
  // Override the realm's Function.prototype.toString so BOTH `fn.toString()` and the robust
  // `Function.prototype.toString.call(fn)` report [native code] for our shims (a source-revealing shim
  // is the single clearest bot tell). Unmarked functions fall through to the real toString unchanged.
  let realToString = Function.prototype.toString;
  try {
    const FP = win.Function && win.Function.prototype;
    const orig = FP.toString;
    realToString = orig;
    const patched = function toString() {
      const isM = marked.has(this);
      if (VMTRACE && !isM) { try { const s = orig.call(this); if (s.indexOf('[native code]') < 0) logOnce('toStringLEAK: ' + s.replace(/\s+/g, ' ').slice(0, 90)); } catch {} }
      return isM ? nativeStr(marked.get(this)) : orig.call(this);
    };
    marked.set(patched, 'toString'); marked.set(orig, 'toString');
    Object.defineProperty(FP, 'toString', { value: patched, writable: true, configurable: true });
  } catch {}
  // Just record the fn in the shared marked set; the patched Function.prototype.toString (inherited by every
  // function) returns [native code] for it. Do NOT install an own `toString` — real natives have none, and
  // an own shim-toString is itself a leaky non-native function (toStringing IT reveals our source).
  const mark = (f, name) => { if (typeof f === 'function') marked.set(f, name || f.name || ''); };
  win.__markNative = mark;
  [['fetch', win.fetch], ['XMLHttpRequest', win.XMLHttpRequest], ['Worker', win.Worker], ['SharedWorker', win.SharedWorker],
   ['ReadableStream', win.ReadableStream], ['matchMedia', win.matchMedia], ['IntersectionObserver', win.IntersectionObserver],
   ['ResizeObserver', win.ResizeObserver], ['PerformanceObserver', win.PerformanceObserver], ['requestIdleCallback', win.requestIdleCallback],
   ['sendBeacon', win.navigator && win.navigator.sendBeacon], ['createObjectURL', win.URL && win.URL.createObjectURL],
   ['revokeObjectURL', win.URL && win.URL.revokeObjectURL], ['getHighEntropyValues', win.navigator && win.navigator.userAgentData && win.navigator.userAgentData.getHighEntropyValues],
  ].forEach(([n, f]) => mark(f, n));
  const cp = win.HTMLCanvasElement && win.HTMLCanvasElement.prototype;
  if (cp) { mark(cp.getContext, 'getContext'); mark(cp.toDataURL, 'toDataURL'); mark(cp.toBlob, 'toBlob'); }
  // Our XMLHttpRequest is a shim CLASS — marking the constructor (above) leaves its prototype METHODS as plain
  // JS. The VM uses XHR for the flow POST and tamper-checks `XMLHttpRequest.prototype.open.toString()` (and
  // send/setRequestHeader/addEventListener), which would expose our source. Mark every shim method native.
  const xp = win.XMLHttpRequest && win.XMLHttpRequest.prototype;
  if (xp) ['open', 'send', 'setRequestHeader', 'getResponseHeader', 'getAllResponseHeaders', 'abort', 'addEventListener', 'removeEventListener', 'overrideMimeType'].forEach((m) => mark(xp[m], m));
  // The ~15 marks above are not enough: jsdom implements EVERY DOM method as plain JS whose source reveals
  // its impl (esValue/implSymbol/globalObject), and our own shims (createElement/setTimeout/postMessage/…)
  // leak our internals (__createdIframes/deliver/iwin/logOnce). The challenge VM toStrings core DOM methods
  // it uses to run (appendChild/createElement/attachShadow/addEventListener/postMessage) — any leak = bot.
  // Real Chrome reports [native code] for ALL of them. So sweep the realm: mark every built-in fn whose
  // source isn't already "[native code]". (Runs at realm-setup, BEFORE the VM defines its own functions, so
  // the VM's code toStrings normally.)
  try {
    const isLeaky = (f) => { try { return realToString.call(f).indexOf('[native code]') < 0; } catch { return false; } };
    const visit = (obj) => {
      if (!obj) return;
      let names; try { names = Object.getOwnPropertyNames(obj); } catch { return; }
      for (const k of names) {
        if (k === 'constructor' && obj !== win) continue;
        let d; try { d = Object.getOwnPropertyDescriptor(obj, k); } catch { continue; }
        if (!d) continue;
        for (const f of [d.value, d.get, d.set]) if (typeof f === 'function' && isLeaky(f)) mark(f, typeof k === 'string' ? k : '');
      }
    };
    for (const name of Object.getOwnPropertyNames(win)) {
      if (!/^[A-Z]/.test(name)) continue;            // Web IDL / DOM interface constructors
      let C; try { C = win[name]; } catch { continue; }
      if (typeof C !== 'function') continue;
      if (isLeaky(C)) mark(C, name);
      try { visit(C.prototype); } catch {}
      try { visit(C); } catch {}
    }
    // window's own functions + key instance own functions (our shims live here)
    [win, win.document, win.navigator, win.crypto, win.crypto && win.crypto.subtle, win.performance,
     win.location, win.history, win.localStorage, win.sessionStorage, win.screen].forEach(visit);
  } catch {}
  // VMTRACE only: the VM hides opcode-handler source by BINDING them (bound fn → toString `[native code]`).
  // Record boundFn -> target at bind time so the tracer can read the unbound handler's real source. Wrapper
  // marked native so it doesn't itself become a tell. (Dev-only; patches the Node-shared bind — gated off by default.)
  if (VMTRACE) try {
    const FB = win.Function.prototype.bind;
    if (FB && !FB.__vmtb) {
      const wrapped = function bind() { const r = FB.apply(this, arguments); try { BOUND_TARGETS.set(r, this); const ta = arguments[0]; if (ta && typeof ta === 'object') VM_STATE.set(r, ta); } catch {} return r; };
      wrapped.__vmtb = true; mark(wrapped, 'bind');
      Object.defineProperty(win.Function.prototype, 'bind', { value: wrapped, writable: true, configurable: true });
    }
  } catch {}
}

// Make the window's own-property SURFACE match a real Chrome: add the ~700 missing standard globals as
// plausible native-looking stubs, and override Object.{getOwnPropertyNames,keys}(window) to return exactly
// the real list — hiding jsdom internals (`_globalProxy`/…) and our own plumbing. Call AFTER spoofNatives
// (needs __markNative). The single highest-value gestalt fix: a window enumeration now reads as Chrome.
function matchWindowSurface(win) {
  if (!CHROME_WIN_PROPS.length) return;
  const W = win;
  const realNames = CHROME_WIN_PROPS.slice();
  for (const name of realNames) {
    if (name in W) continue;
    try {
      if (name.startsWith('on')) {
        Object.defineProperty(W, name, { value: null, writable: true, configurable: true, enumerable: true });
      } else if (name[0] >= 'A' && name[0] <= 'Z') {
        const fn = function () { throw new (W.TypeError || TypeError)('Illegal constructor'); };
        try { Object.defineProperty(fn, 'name', { value: name, configurable: true }); } catch {}
        try { fn.prototype = {}; Object.defineProperty(fn.prototype, 'constructor', { value: fn, configurable: true, writable: true }); } catch {}
        if (W.__markNative) W.__markNative(fn, name);
        Object.defineProperty(W, name, { value: fn, writable: true, configurable: true, enumerable: false });
      } else {
        Object.defineProperty(W, name, { value: {}, writable: true, configurable: true, enumerable: true });
      }
    } catch {}
  }
  try {
    const O = W.Object;
    const namesCopy = realNames.slice();
    const origGOPN = O.getOwnPropertyNames;
    const gopn = function getOwnPropertyNames(obj) { return obj === W ? namesCopy.slice() : origGOPN(obj); };
    const origKeys = O.keys;
    const keys = function keys(obj) {
      if (obj !== W) return origKeys(obj);
      return namesCopy.filter((n) => { try { const d = origGOPD(W, n); return !!(d && d.enumerable); } catch { return false; } });
    };
    const origGOPD = O.getOwnPropertyDescriptor;
    Object.defineProperty(O, 'getOwnPropertyNames', { value: gopn, writable: true, configurable: true });
    Object.defineProperty(O, 'keys', { value: keys, writable: true, configurable: true });
    if (W.__markNative) { W.__markNative(gopn, 'getOwnPropertyNames'); W.__markNative(keys, 'keys'); }
  } catch {}
}

/* ------------------------------- manual postMessage bus ---------------------------- */
// Capture 'message' listeners per window; deliver synthetic trusted events. Everything else passes
// through to jsdom's real addEventListener.
function installBus(win) {
  win.__msgListeners = [];
  const origAdd = win.addEventListener.bind(win);
  const origRemove = win.removeEventListener.bind(win);
  win.addEventListener = function (type, cb, opts) {
    if (type === 'message' && typeof cb === 'function') { win.__msgListeners.push(cb); return; }
    return origAdd(type, cb, opts);
  };
  win.removeEventListener = function (type, cb, opts) {
    if (type === 'message') { win.__msgListeners = win.__msgListeners.filter((f) => f !== cb); return; }
    return origRemove(type, cb, opts);
  };
}
function deliver(targetWin, data, origin, sourceWin) {
  try { if (data && data.event && data.event !== 'meow' && data.event !== 'food') { log('MSG event=' + data.event + (data.reason ? ' reason=' + data.reason : '') + (data.code ? ' code=' + data.code : '')); amark('msg:' + data.event, { reason: data.reason, code: data.code }); } } catch {}
  if (process.env.CF_MSGDBG && data && data.event && data.event !== 'meow' && data.event !== 'food') { try { log('MSGFULL ' + data.event + ' :: ' + JSON.stringify(data, (k, v) => (typeof v === 'function' ? '[fn]' : (typeof v === 'string' && v.length > 100 ? v.slice(0, 100) + '…(' + v.length + ')' : v))).slice(0, 700)); fs.writeFileSync('spike/_msg_' + data.event + '.json', JSON.stringify(data, (k, v) => (typeof v === 'function' ? '[fn]' : v), 1)); } catch {} }
  // CF_FASTPARAMS: api.js measures jsdom-slow init/render/load durations and ships them to the VM in extraParams;
  // a real browser's are ~100ms. If the VM gates on a too-slow environment, clamp them to fast plausible values.
  if (process.env.CF_FASTPARAMS && data && data.event === 'extraParams') { try { for (const kk of Object.keys(data)) if (/^time.*Ms$/.test(kk) && typeof data[kk] === 'number' && data[kk] > 130) data[kk] = 35 + Math.floor(Math.random() * 70); } catch {} }
  if (process.env.CF_INITDBG && data && data.event === 'init') { try { log('INIT data.source=' + data.source + ' keys=' + Object.keys(data).join(',') + ' stack=' + (new Error().stack || '').split('\n').slice(1, 4).join(' | ')); } catch {} }
  const ev = { data, origin, source: sourceWin, isTrusted: true, ports: [], type: 'message',
    stopPropagation() {}, stopImmediatePropagation() {}, preventDefault() {} };
  for (const cb of (targetWin.__msgListeners || []).slice()) {
    try { cb.call(targetWin, ev); } catch (e) { logOnce('msg handler threw: ' + (e && (e.stack || e.message))); }
  }
  // also honour window.onmessage
  if (typeof targetWin.onmessage === 'function') { try { targetWin.onmessage({ data, origin, source: sourceWin, isTrusted: true }); } catch {} }
}

/* ------------------------------------ window factory ------------------------------- */
const _seen = new Set();
function logOnce(msg) { if (_seen.has(msg) || _seen.size > 80) return; _seen.add(msg); log(msg); }

function makeWindow(html, url, label = '?') {
  const vc = new VirtualConsole();
  vc.on('jsdomError', (e) => logOnce(`[${label}] jsdomError: ` + (e && ((e.detail && (e.detail.stack || e.detail)) || e.message || e))));
  vc.on('error', (...a) => logOnce(`[${label}] console.error: ` + a.join(' ').slice(0, 200)));
  vc.on('warn', (...a) => logOnce(`[${label}] console.warn: ` + a.join(' ').slice(0, 200)));
  // resources NOT loaded automatically (no auto api.js/iframe/img fetch) — we drive every fetch.
  const dom = new JSDOM(html, {
    url, runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: vc,
    userAgent: (config && config.fingerprint && config.fingerprint.user_agent) || undefined,
    resources: new (class extends ResourceLoader { fetch() { return null; } })(),
  });
  setupWindow(dom.window, label);
  return { dom, win: dom.window };
}

// Emulate a CROSS-ORIGIN Window for the challenge iframe's view of its parent/top. A real Turnstile iframe is
// cross-origin to the host page, so the framed VM can only touch the spec's cross-origin-exposed members and
// reading `parent.location`/`parent.document`/`parent.frameElement`/etc. throws SecurityError. Our jsdom iframe is
// SAME-origin (hosted in the parent realm), so without this the VM reads its parent freely — a synthetic-embed
// tell (the PoW serializes a frameElement/window probe right before the flow decision). Gated by CF_XORIGIN.
function makeCrossOriginWindow(realWin) {
  let xhits = 0;
  const xerr = (p) => { xhits++; if (xhits <= 8) { try { log('[xorigin] VM blocked-read parent.' + String(p) + ' (#' + xhits + ')'); } catch {} } const e = new Error('Blocked a frame with origin "https://challenges.cloudflare.com" from accessing a cross-origin frame.'); e.name = 'SecurityError'; throw e; };
  let proxy;
  const loc = new Proxy(Object.create(null), {  // cross-origin Location: only replace/assign are callable; href is set-only, all reads throw
    get(t, p) { if (p === 'replace' || p === 'assign') return function () {}; if (typeof p === 'symbol') return undefined; return xerr('location.' + String(p)); },
    set() { return true; }, has() { return true; }, ownKeys() { return []; }, getPrototypeOf() { return null; },
  });
  const CROSS = ['window', 'self', 'location', 'close', 'closed', 'focus', 'blur', 'frames', 'length', 'top', 'opener', 'parent', 'postMessage'];
  const val = (p) => {
    switch (p) {
      case 'postMessage': return function postMessage() { return realWin.postMessage.apply(realWin, arguments); };
      case 'window': case 'self': case 'frames': case 'top': case 'parent': return proxy;
      case 'location': return loc;
      case 'closed': return false; case 'length': return 0; case 'opener': return null;
      case 'close': return function close() {}; case 'focus': return function focus() {}; case 'blur': return function blur() {};
    }
  };
  proxy = new Proxy(Object.create(null), {
    get(t, p) { if (typeof p === 'symbol' || p === 'then') return undefined; if (CROSS.indexOf(p) >= 0) return val(p); return xerr(p); },
    set() { return xerr('<set>'); },
    has(t, p) { return CROSS.indexOf(p) >= 0; },
    ownKeys() { return []; },
    getOwnPropertyDescriptor(t, p) { if (CROSS.indexOf(p) >= 0) return { configurable: true, enumerable: false, value: val(p), writable: false }; return undefined; },
    getPrototypeOf() { return null; }, setPrototypeOf() { return false; },
    defineProperty() { return xerr(); }, deleteProperty() { return xerr(); },
    isExtensible() { return true; }, preventExtensions() { return false; },
  });
  return proxy;
}

// The Turnstile PoW probes WebRTC: it builds `{iceCandidatePoolSize:1, iceServers:[{urls:'stun:stun.cloudflare
// .com:3478'}]}` and `new RTCPeerConnection(...)`, then waits for ICE candidates (VMTRACE: the bytecode
// materialises "stun:stun.cloudflare.com:3478" in the h() entry, before the flow decision). jsdom has no
// WebRTC and matchWindowSurface only adds a no-op stub constructor, so gathering never fires — a hard
// not-a-real-browser tell. Minimal functional stub: emit a plausible mDNS host candidate + gathering-complete.
// EXPERIMENTAL: no real STUN round-trip (no srflx/public-IP candidate); if CF server-correlates the reflexive
// IP this won't pass, but it tests whether WebRTC *presence/gathering* is a local gate.
function installWebRTC(win) {
  try {
    const rid = (n) => Array.from({ length: n }, () => 'abcdefghijklmnopqrstuvwxyz0123456789'[Math.floor(Math.random() * 36)]).join('');
    const mdns = () => ((win.crypto && win.crypto.randomUUID && win.crypto.randomUUID()) || rid(8) + '-0000-0000-0000-000000000000') + '.local';
    function RTCSessionDescription(init) { this.type = (init && init.type) || 'offer'; this.sdp = (init && init.sdp) || ''; }
    function RTCIceCandidate(init) { Object.assign(this, init || {}); }
    function RTCPeerConnection(config) {
      try { log('[webrtc] new RTCPeerConnection ' + JSON.stringify(config).slice(0, 120)); } catch {}
      this._config = config || {}; this.localDescription = null; this.remoteDescription = null;
      this.iceGatheringState = 'new'; this.iceConnectionState = 'new'; this.connectionState = 'new'; this.signalingState = 'stable';
      this.onicecandidate = null; this.onicegatheringstatechange = null; this.oniceconnectionstatechange = null; this.ondatachannel = null; this.onnegotiationneeded = null;
      this.canTrickleIceCandidates = null; this._evts = {};
    }
    const sdp = () => ['v=0', 'o=- 4611731400430051336 2 IN IP4 127.0.0.1', 's=-', 't=0 0', 'a=group:BUNDLE 0',
      'a=extmap-allow-mixed', 'a=msid-semantic: WMS', 'm=application 9 UDP/DTLS/SCTP webrtc-datachannel',
      'c=IN IP4 0.0.0.0', 'a=ice-ufrag:' + rid(4), 'a=ice-pwd:' + rid(22), 'a=ice-options:trickle',
      'a=fingerprint:sha-256 ' + Array.from({ length: 32 }, () => ('0' + Math.floor(Math.random() * 256).toString(16)).slice(-2).toUpperCase()).join(':'),
      'a=setup:actpass', 'a=mid:0', 'a=sctp-port:5000', 'a=max-message-size:262144', ''].join('\r\n');
    const P = win.Promise || Promise, ST = win.setTimeout ? win.setTimeout.bind(win) : setTimeout;
    const proto = RTCPeerConnection.prototype;
    proto.createDataChannel = function (label) { return { label: label || '', readyState: 'connecting', ordered: true, bufferedAmount: 0, send() {}, close() {}, addEventListener() {}, removeEventListener() {}, onopen: null, onmessage: null, onclose: null }; };
    proto.createOffer = function () { return P.resolve(new RTCSessionDescription({ type: 'offer', sdp: sdp() })); };
    proto.createAnswer = function () { return P.resolve(new RTCSessionDescription({ type: 'answer', sdp: sdp() })); };
    proto.setLocalDescription = function (desc) {
      this.localDescription = desc || new RTCSessionDescription({ type: 'offer', sdp: sdp() }); this.iceGatheringState = 'gathering';
      const fire = (c) => { const ev = { candidate: c }; try { this.onicecandidate && this.onicecandidate(ev); } catch {} (this._evts.icecandidate || []).forEach((f) => { try { f(ev); } catch {} }); };
      const host = mdns(), uf = rid(4);
      // The WHOLE point of giving a STUN server is the server-reflexive (srflx) candidate = public IP; a host-only
      // gather reads as "STUN unreachable". The decision is LOCAL (uc mints with zero flow POSTs) so the VM can't
      // server-verify the IP — a plausible reflexive IP suffices. CRITICAL (from the [webrtc] trace): the VM does
      // createOffer→setLocalDescription→close with NO wait, so async (setTimeout) candidates fire AFTER close and
      // the VM's icecandidate listener never collects them. Fire SYNCHRONOUSLY here so the listener gets them
      // before the VM reads/closes. Also munge the candidates into localDescription.sdp for SDP-parse probes.
      const srflxIp = (typeof process !== 'undefined' && process.env && process.env.CF_WEBRTC_SRFLX_IP) || '189.6.244.7';
      const cHost = new RTCIceCandidate({ candidate: 'candidate:1 1 udp 2122260223 ' + host + ' 54321 typ host generation 0 ufrag ' + uf + ' network-cost 999', sdpMid: '0', sdpMLineIndex: 0, foundation: '1', component: 1, protocol: 'udp', priority: 2122260223, address: host, port: 54321, type: 'host', relatedAddress: null, relatedPort: null, toJSON() { return this; } });
      const cSrflx = new RTCIceCandidate({ candidate: 'candidate:2 1 udp 1685921535 ' + srflxIp + ' 51234 typ srflx raddr 0.0.0.0 rport 0 generation 0 ufrag ' + uf + ' network-cost 999', sdpMid: '0', sdpMLineIndex: 0, foundation: '2', component: 1, protocol: 'udp', priority: 1685921535, address: srflxIp, port: 51234, type: 'srflx', relatedAddress: '0.0.0.0', relatedPort: 0, toJSON() { return this; } });
      try { this.localDescription.sdp = this.localDescription.sdp.replace('a=mid:0\r\n', 'a=mid:0\r\na=' + cHost.candidate + '\r\na=' + cSrflx.candidate + '\r\n'); } catch {}
      fire(cHost); fire(cSrflx);
      this.iceGatheringState = 'complete'; try { this.onicegatheringstatechange && this.onicegatheringstatechange({}); } catch {} (this._evts.icegatheringstatechange || []).forEach((f) => { try { f({}); } catch {} }); fire(null);
      return P.resolve();
    };
    proto.setRemoteDescription = function (d) { this.remoteDescription = d; return P.resolve(); };
    proto.addIceCandidate = function () { return P.resolve(); };
    proto.setConfiguration = function (c) { this._config = c; };
    proto.getConfiguration = function () { return this._config; };
    proto.getStats = function () { return P.resolve(new (win.Map || Map)()); };
    proto.getSenders = function () { return []; }; proto.getReceivers = function () { return []; }; proto.getTransceivers = function () { return []; };
    proto.close = function () { this.iceConnectionState = 'closed'; this.connectionState = 'closed'; this.signalingState = 'closed'; };
    proto.addEventListener = function (t, cb) { (this._evts[t] = this._evts[t] || []).push(cb); };
    proto.removeEventListener = function (t, cb) { if (this._evts[t]) this._evts[t] = this._evts[t].filter((f) => f !== cb); };
    proto.dispatchEvent = function () { return true; };
    // TRACE: log exactly how the VM drives the PC (just construct? createOffer+setLocalDescription+wait for a
    // connected state / datachannel?) — tells us what the probe actually requires. Gated by log()=CF_SIDECAR_DEBUG.
    ['createDataChannel', 'createOffer', 'createAnswer', 'setLocalDescription', 'setRemoteDescription', 'addIceCandidate', 'getStats', 'getSenders', 'getReceivers', 'getTransceivers', 'setConfiguration', 'getConfiguration', 'close'].forEach((m) => { const o = proto[m]; proto[m] = function (...a) { try { log('[webrtc] .' + m + '()'); } catch {} return o.apply(this, a); }; });
    { const oAdd = proto.addEventListener; proto.addEventListener = function (t, cb) { try { log('[webrtc] addEventListener ' + t); } catch {} return oAdd.call(this, t, cb); }; }
    win.RTCPeerConnection = RTCPeerConnection; win.webkitRTCPeerConnection = RTCPeerConnection;
    win.RTCSessionDescription = RTCSessionDescription; win.RTCIceCandidate = RTCIceCandidate;
    if (win.__markNative) { const M = win.__markNative; M(RTCPeerConnection, 'RTCPeerConnection'); M(RTCSessionDescription, 'RTCSessionDescription'); M(RTCIceCandidate, 'RTCIceCandidate');
      ['createDataChannel', 'createOffer', 'createAnswer', 'setLocalDescription', 'setRemoteDescription', 'addIceCandidate', 'close', 'addEventListener', 'removeEventListener', 'getStats', 'setConfiguration', 'getConfiguration', 'getSenders', 'getReceivers', 'getTransceivers', 'dispatchEvent'].forEach((m) => M(proto[m], m)); }
  } catch (e) { try { log('installWebRTC failed: ' + e); } catch {} }
}

// Install fingerprint + missing APIs + net shims + the message bus + debug hooks on a window. Used for
// both the parent jsdom AND the iframe's real contentWindow (a child jsdom window jsdom auto-creates).
function setupWindow(win, label) {
  applyFingerprint(win, config.fingerprint || {});
  augmentWindow(win);
  installNet(win);
  installBus(win);
  spoofNatives(win);
  matchWindowSurface(win);
  installWebRTC(win);  // functional RTCPeerConnection (the PoW probes stun:stun.cloudflare.com) — override the no-op surface stub
  // attribute async throws to their VM offsets: jsdom swallows setTimeout errors into jsdomError with
  // only its own frame, so wrap setTimeout to log the ORIGINAL stack (has the cf VM function+offset).
  const origST = win.setTimeout.bind(win);
  win.setTimeout = function (cb, ms, ...args) {
    if (typeof cb !== 'function') return origST(cb, ms, ...args);
    return origST(function () { try { return cb.apply(this, args); } catch (e) { logOnce(`[${label}] setTimeout threw: ` + (e && e.stack || e)); throw e; } }, ms);
  };
  // Turnstile builds its widget inside a shadow root, so the challenge iframe never appears in the light
  // DOM — capture every <iframe> at creation regardless of where it's appended.
  win.__createdIframes = [];
  const origCreate = win.document.createElement.bind(win.document);
  win.__rawCreate = origCreate;
  win.document.createElement = function (tag, ...rest) {
    const el = origCreate(tag, ...rest);
    try { if (String(tag).toLowerCase() === 'iframe') win.__createdIframes.push(el); } catch {}
    return el;
  };
  // These shims (+ installBus's add/removeEventListener) are installed around/after spoofNatives' sweep,
  // so re-mark them [native code] — else fn.toString() leaks our impl (the VM toStrings them to run).
  try {
    const M = win.__markNative;
    if (M) { M(win.setTimeout, 'setTimeout'); M(win.document.createElement, 'createElement'); M(win.addEventListener, 'addEventListener'); M(win.removeEventListener, 'removeEventListener'); }
  } catch {}
  // The challenge's proof-of-work evals code in a throwaway child iframe's contentWindow; jsdom returns
  // null for iframes that aren't connected to the light DOM, so back any null contentWindow with a real
  // hidden light-DOM frame window (correct realm, with eval) lazily on access.
  try {
    const ifp = win.HTMLIFrameElement && win.HTMLIFrameElement.prototype;
    const orig = ifp && Object.getOwnPropertyDescriptor(ifp, 'contentWindow');
    if (orig && orig.get && !ifp.__cwBacked) {
      ifp.__cwBacked = true;
      Object.defineProperty(ifp, 'contentWindow', {
        configurable: true,
        get() {
          const real = orig.get.call(this);
          if (real) return real;
          if (this.__backingWin) return this.__backingWin;
          try {
            const b = win.__rawCreate('iframe'); b.setAttribute('aria-hidden', 'true'); b.style.display = 'none';
            win.document.body.appendChild(b);
            const bw = orig.get.call(b);
            if (bw) { setupWindow(bw, label + '/sub');
              if (process.env.CF_REALMDBG) { try {
                log('[realm/' + label + '/sub] backed. eval.toString()=' + JSON.stringify(String(bw.eval).slice(0, 40)) +
                  ' | CROSS bw.FP.toString.call(MAIN.fetch)=' + JSON.stringify(String(bw.Function.prototype.toString.call(win.fetch)).slice(0, 40)) +
                  ' | CROSS bw.FP.toString.call(MAIN.eval)=' + JSON.stringify(String(bw.Function.prototype.toString.call(win.eval)).slice(0, 40)) +
                  ' | bw.FPtoString===MAIN? ' + (bw.Function.prototype.toString === win.Function.prototype.toString));
              } catch (e) { log('[realm dbg] err ' + e); } }
              this.__backingWin = bw; }
            return bw;
          } catch { return null; }
        },
      });
    }
  } catch {}
  // Phase-1 instrumentation: record EXACTLY what the PoW reads on EVERY realm it touches (parent, the
  // challenge iframe, and any pristine child iframe the PoW spins up). Gated by CF_ACCESS_LOG, idempotent.
  installAccessRecorder(win, label);
  return win;
}

function runScriptIn(win, code, name) {
  const s = win.document.createElement('script');
  s.textContent = name ? (code + '\n//# sourceURL=' + name) : code;
  (win.document.head || win.document.documentElement).appendChild(s);
}

/* --------------------------------------- driver ------------------------------------ */
let resolved = false;
function finishToken(token) {
  if (resolved) return; resolved = true;
  dumpAccessLog();
  send({ t: 'result', token: String(token) });
  setTimeout(() => process.exit(0), 10);
}

// Real Chrome sets Sec-Fetch-* + Accept per request CONTEXT. The challenge sub-resources have distinct
// contexts that Cloudflare validates; sending one generic set (or the bogus browserforge values) is a bot
// tell. Matched to the passing undetected-chromedriver capture (selenium_capture/out/network.json).
const NAV_ACCEPT = 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7';
const SCRIPT_HDRS = { Accept: '*/*', 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'no-cors', 'Sec-Fetch-Dest': 'script', priority: 'u=1' };
// An iframe load is a (non-user-activated) cross-site navigation: Mode=navigate, Dest=iframe, UIR=1, NO
// Origin, NO Sec-Fetch-User (not user-activated). `priority: u=0, i` like a real Chrome iframe nav.
const IFRAME_NAV_HDRS = { Accept: NAV_ACCEPT, 'Sec-Fetch-Site': 'cross-site', 'Sec-Fetch-Mode': 'navigate', 'Sec-Fetch-Dest': 'iframe', 'Upgrade-Insecure-Requests': '1', priority: 'u=0, i' };

// High-entropy User-Agent Client Hints. The challenge response sends `Critical-CH` demanding the FULL
// set (Arch/Bitness/Full-Version-List/Platform-Version/Model); a real Chrome resends the request WITH
// them, and Cloudflare's trust (easy path) keys on their presence + coherence with the UA and the JS
// `navigator.userAgentData`. curl_cffi already emits the 3 low-entropy hints (sec-ch-ua/mobile/platform),
// so we add only the missing high-entropy ones, derived from the fingerprint so they never drift.
function clientHints() {
  const fp = (config && config.fingerprint) || {};
  const ua = fp.user_agent || '';
  const major = (/Chrome\/(\d+)/.exec(ua) || [, '146'])[1];
  const full = major + '.0.0.0';
  const isWin = /Windows/i.test(ua), isMac = /Macintosh|Mac OS/i.test(ua);
  const platVer = isWin ? '10.0.0' : (isMac ? '15.0.0' : '6.8.0');
  return {
    'sec-ch-ua-arch': '"x86"',
    'sec-ch-ua-bitness': '"64"',
    'sec-ch-ua-full-version': `"${full}"`,
    'sec-ch-ua-full-version-list': `"Google Chrome";v="${full}", "Not.A/Brand";v="8.0.0.0", "Chromium";v="${full}"`,
    'sec-ch-ua-model': '""',
    'sec-ch-ua-platform-version': `"${platVer}"`,
  };
}

async function bridgeGet(url, referer, extra) {
  const h = Object.assign({}, referer ? { Referer: referer } : {}, extra || {});
  const r = await bridgeRequest('GET', url, Object.keys(h).length ? h : null, null);
  return b64ToStr(r.bodyB64);
}

async function main() {
  await configReady;
  const pageUrl = config.pageUrl;
  // Cross-origin Referer for sub-resources is ORIGIN-ONLY under Chrome's default referrer policy
  // (strict-origin-when-cross-origin) — the passing uc capture sends exactly `<page origin>/`.
  let pageOrigin = pageUrl;
  try { pageOrigin = new URL(pageUrl).origin + '/'; } catch {}
  const formHtml = b64ToStr(config.formHtmlB64);

  // 1) parent window from the page form
  const { win: pwin } = makeWindow(formHtml, pageUrl, 'PARENT');

  // token sink: api.js invokes the widget's data-callback with the token. Point the widget at ours and
  // also watch the hidden input api.js injects.
  pwin.__cfToken = (tok) => { if (tok) finishToken(tok); };
  const cfEl = pwin.document.querySelector('.cf-turnstile');
  if (cfEl) {
    cfEl.setAttribute('data-callback', '__cfToken');
    if (config.action) cfEl.setAttribute('data-action', config.action);
    // Suppress api.js's implicit auto-render so we render EXACTLY ONCE (explicitly, below). A double
    // render makes api.js track a different iframe than the one we boot -> it rejects the VM's messages
    // as "unexpected source" and never replies (extraParams/execute), so the VM stalls (overrunBegin).
    cfEl.classList.remove('cf-turnstile');
  }

  // 2) run api.js in the parent (auto-renders the widget, creates the challenge iframe). api.js is a
  // cross-site <script> load: Referer=page origin, Sec-Fetch-Dest=script/no-cors (matches uc).
  const apiJs = await bridgeGet(config.apiJsUrl, pageOrigin, SCRIPT_HDRS);
  log(`parent ready: readyState=${pwin.document.readyState} cfEl=${!!cfEl} apiJsBytes=${apiJs.length}`);
  try { runScriptIn(pwin, apiJs); } catch (e) { log('api.js threw: ' + (e && e.stack || e)); }
  await new Promise((r) => setTimeout(r, 150));
  log(`after api.js: turnstile=${typeof pwin.turnstile} iframes=${pwin.document.querySelectorAll('iframe').length} scripts=${pwin.document.querySelectorAll('script').length} msgL=${(pwin.__msgListeners || []).length}`);
  // force an explicit render if nothing auto-rendered the challenge iframe (jsdom has no layout, so
  // api.js's implicit/visibility-gated render may never fire) — mirrors engine.py step 3.
  if (pwin.turnstile && typeof pwin.turnstile.render === 'function' &&
      (pwin.__createdIframes || []).length === 0) {
    try {
      const opts = { sitekey: config.sitekey, callback: pwin.__cfToken };
      if (config.action) opts.action = config.action;
      if (config.cdata) opts.cData = config.cdata;
      const wid = pwin.turnstile.render(cfEl, opts);
      log('explicit render -> ' + wid);
    } catch (e) { log('explicit render threw: ' + (e && e.stack || e)); }
    await new Promise((r) => setTimeout(r, 150));
    const srcs = (pwin.__createdIframes || []).map((i) => i.src || i.getAttribute('src'));
    log('iframes after render: ' + JSON.stringify(srcs));
  }

  // 3) poll for the challenge iframe api.js created; boot its realm + VM once
  const deadline = Date.now() + (config.timeoutMs || 30000);
  const bootedIframes = new Set();
  while (!resolved && Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 60));
    // hidden-input fallback token
    const inp = pwin.document.querySelector('[name="cf-turnstile-response"]');
    if (inp && inp.value && inp.value.length > 20) { finishToken(inp.value); break; }
    if (bootedIframes.size === 0) {  // one widget is enough; double-render spawns extras we ignore
      for (const ifr of (pwin.__createdIframes || [])) {
        const src = ifr.src || ifr.getAttribute('src') || '';
        if (src.includes('challenge-platform') && src.includes('/turnstile/')) {
          bootedIframes.add(src);
          try { await bootIframe(pwin, ifr, src); } catch (e) { log('bootIframe failed: ' + (e && e.stack || e)); }
          break;
        }
      }
    }
  }
  if (!resolved) {
    dumpAccessLog();
    send({ t: 'timeout', detail: `no token within ${config.timeoutMs}ms; iframes booted=${bootedIframes.size}` });
    process.exit(0);
  }
}

async function bootIframe(pwin, iframeEl, url) {
  const u = new URL(url);
  // The challenge iframe is a cross-site NAVIGATION created by the page: Referer=page origin (origin-only),
  // Sec-Fetch Mode=navigate/Dest=iframe + Upgrade-Insecure-Requests, NO Origin — matches the passing uc GET.
  let pageOrigin = pwin.location.href;
  try { pageOrigin = new URL(pwin.location.href).origin + '/'; } catch {}
  const html = await bridgeGet(url, pageOrigin, Object.assign({}, IFRAME_NAV_HDRS, clientHints()));
  if (process.env.CF_IFRAME_DUMP) { try { fs.writeFileSync(process.env.CF_IFRAME_DUMP, html); log('iframe html dumped'); } catch {} }

  // Run the VM in the iframe element's REAL child-frame window (jsdom builds a correct frame tree:
  // top===parent===pwin, top!==self). The VM bails unless it is genuinely framed, and a standalone jsdom
  // can't fake that (window.top is a non-configurable self-reference). Ensure the iframe is connected so
  // jsdom materialises its contentWindow.
  // api.js renders the iframe INSIDE the widget's shadow root, and jsdom gives no browsing context
  // (contentWindow) to shadow-DOM iframes. So host the VM in a hidden LIGHT-DOM iframe (which DOES get a
  // contentWindow with a correct frame tree: top===parent===pwin, top!==self — the VM bails otherwise),
  // and PIN api.js's shadow iframe's contentWindow/contentDocument to it so api.js's `e.source ===
  // iframe.contentWindow` identity check still resolves to the window the VM actually runs in.
  let iwin = iframeEl.contentWindow;
  if (!iwin) {
    const host = pwin.document.createElement('iframe');
    host.setAttribute('aria-hidden', 'true'); host.style.display = 'none';
    pwin.document.body.appendChild(host);
    iwin = host.contentWindow;
    try { Object.defineProperty(iframeEl, 'contentWindow', { get: () => iwin, configurable: true }); } catch {}
    try { Object.defineProperty(iframeEl, 'contentDocument', { get: () => iwin.document, configurable: true }); } catch {}
  }
  if (!iwin) { log('iframe has no contentWindow — cannot boot'); return; }

  // load the challenge HTML into the frame doc (strip its <script>s; we inject the VM after wiring shims)
  const headBody = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
  try { iwin.document.open(); iwin.document.write(headBody); iwin.document.close(); } catch (e) { log('doc.write failed: ' + e); }
  log(`after write: iwin===contentWindow? ${iwin === iframeEl.contentWindow} top!==self? ${iwin.top !== iwin.self}`);
  setupWindow(iwin, 'IFRAME');
  // CF_FASTTIME: the PoW brackets operations with Date.now()/performance.now() (VMTRACE: t0@op218, t1@op257) — a
  // jsdom realm + JS-function native spoofs are structurally slower than native Blink, so if the VM gates on an
  // operation-timing budget we'd fail it inherently. Compress ELAPSED time (keep absolute ~correct at start so the
  // challenge-expiry window check still passes) to test whether timing is the discriminator. CF_FASTTIME=k (e.g. 0.02).
  if (process.env.CF_FASTTIME) {
    const k = Number(process.env.CF_FASTTIME) || 0.05, RN = Date.now, t0 = RN();
    const perf = iwin.performance, RP = perf && perf.now ? perf.now.bind(perf) : null, p0 = RP ? RP() : 0;
    try { iwin.Date.now = () => Math.floor(t0 + (RN() - t0) * k); iwin.__markNative && iwin.__markNative(iwin.Date.now, 'now'); } catch {}
    try { if (RP) { perf.now = () => (RP() - p0) * k; iwin.__markNative && iwin.__markNative(perf.now, 'now'); } } catch {}
    log('CF_FASTTIME: elapsed time compressed x' + (1 / k));
  }
  // Real Turnstile loads challenges.cloudflare.com CROSS-ORIGIN to the host page, so inside the challenge
  // iframe `window.frameElement` is null (a cross-origin container is invisible to the framed doc). jsdom
  // hosts our iframe SAME-ORIGIN, so it returns the host <iframe> element — a synthetic-embed tell the PoW
  // probes heavily (VMTRACE: the bytecode materialises "window.frameElement" repeatedly right before the
  // flow-vs-complete decision + fingerprint hash). Force it null to match a real cross-origin embed.
  try { const had = iwin.frameElement; Object.defineProperty(iwin, 'frameElement', { get: () => null, configurable: true }); log(`frameElement forced null (was ${had ? Object.prototype.toString.call(had) : String(had)})`); } catch (e) { log('frameElement pin failed: ' + e); }
  iwin.__refHref = url; iwin.__refOrigin = u.origin;  // real iframe URL for Referer/Origin on its requests

  // manual trusted-message bus across the real frame boundary (jsdom MessageEvent.isTrusted is false):
  //  VM -> parent : window.parent.postMessage -> pwin listeners; source = iwin (=== iframe.contentWindow,
  //                 so api.js's `e.source === iframe.contentWindow` check holds), origin = cloudflare
  //  parent -> VM : iframe.contentWindow.postMessage -> iwin listeners; source = pwin, origin = page
  pwin.postMessage = (data) => deliver(pwin, data, u.origin, iwin);
  let parentForVM = pwin;  // the `source` stamped on parent->VM messages + what `window.parent` resolves to in the VM
  // CF_XORIGIN: make the VM see its parent/top as a real CROSS-ORIGIN Window (parent.location etc. throw). The bus
  // still works: VM->parent `proxy.postMessage` forwards to `pwin.postMessage` (the shim below); parent->VM is
  // delivered with source = the proxy (not pwin) so the VM's `e.source === window.parent` identity check holds.
  if (process.env.CF_XORIGIN) {
    const xparent = makeCrossOriginWindow(pwin); parentForVM = xparent;
    try { Object.defineProperty(iwin, 'parent', { get: () => xparent, configurable: true }); } catch (e) { log('xorigin parent pin failed: ' + e); }
    try { Object.defineProperty(iwin, 'top', { get: () => xparent, configurable: true }); } catch (e) { log('xorigin top pin failed: ' + e); }
    log('CF_XORIGIN: parent/top are cross-origin proxies (parent.location/document reads throw SecurityError)');
  }
  iwin.postMessage = (data) => deliver(iwin, data, pwin.location.origin, parentForVM);
  // postMessage is a shim (leaks `deliver(iwin,…,pwin)`); the VM toStrings it — mark both [native code].
  try { pwin.__markNative && pwin.__markNative(pwin.postMessage, 'postMessage'); iwin.__markNative && iwin.__markNative(iwin.postMessage, 'postMessage'); } catch {}

  // (the access recorder is installed by setupWindow on every realm — parent, iframe, sub-frames.)
  // run the iframe's inline VM script(s) in the real frame window
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
  for (const m of scripts) {
    const attrs = m[1] || '';
    const srcM = /\bsrc\s*=\s*["']([^"']+)["']/i.exec(attrs);
    let code;
    if (srcM) {
      let su = srcM[1];
      if (su.startsWith('//')) su = 'https:' + su; else if (su.startsWith('/')) su = CF_ORIGIN + su;
      else if (!su.startsWith('http')) su = url.replace(/\/[^/]*$/, '/') + su;
      code = await bridgeGet(su, url);
    } else { code = m[2]; }
    if (code && code.trim() && !srcM && process.env.CF_SIDECAR_DUMP) { try { fs.writeFileSync(process.env.CF_SIDECAR_DUMP, code); } catch {} }
    // CF_VMTRACE: instrument SA's opcode dispatch (the bytecode interpreter). The flow-vs-complete decision
    // is a conditional jump in this loop. Wrap both dispatch sites — anchored on the literal raw text after
    // `…*46161,20303)&255[.68],` (the RC4 opcode decode) — to ring-buffer (PC,opcode). Dumped at @2s (after
    // flow-post). Build-specific: if the names rotate, the patch reports NOT FOUND (re-derive from vm.beau.js).
    if (process.env.CF_VMTRACE && code && code.trim()) {
      const RN = 32768, ring = new Int16Array(RN); let ri = 0;
      iwin.__vmtN = 0; iwin.__vmtForm = [0, 0, 0]; iwin.__vmtFlowN = null; globalThis.__VMTWIN = iwin;
      const slot = new Int32Array(RN);  // absolute op-index per ring slot, to slice around flowN
      const fns = new Array(RN);        // the HANDLER fn dispatched at each step — toString it to read semantics
      const rts = REAL_TOSTRING;        // module-load capture, immune to spoofNatives' FP.toString patch
      // Branch reader: per-op snapshot of the PC value (regs[stateJ]) + scalar registers. The flow-vs-complete
      // decision is a loop-exit conditional jump (PoW-result vs target); its operands are scalar regs that change
      // right before the PC breaks out of the hot loop. flowN sits ~200 ops before the tail end, so a rolling
      // window of SNAP steps covers flowN-55..flowN. Reads VM state via VM_STATE (bound handler -> its `this`).
      const SNAP = 700; const pcSnap = new Int32Array(SNAP).fill(-2); const scSnap = new Array(SNAP); const scAbs = new Int32Array(SNAP).fill(-1); let si = 0;
      const scalarsOf = (st) => { const out = {}; try { const g = st.g; if (!g) return out; const isArr = Array.isArray(g); const keys = isArr ? null : Object.keys(g); const n = isArr ? Math.min(g.length >>> 0, 900) : keys.length;
        for (let ii = 0; ii < n; ii++) { const k = isArr ? ii : keys[ii]; const v = g[k], t = typeof v; if (t === 'number' || t === 'boolean') out[k] = v; else if (t === 'string' && v.length < 80) out[k] = v; else if (t === 'bigint') out[k] = String(v) + 'n'; } } catch {} return out; };
      iwin.__vmt = function (op, form, fn) {
        ring[ri] = ((form & 3) << 9) | (op & 0x1ff); slot[ri] = iwin.__vmtN; fns[ri] = fn; ri = (ri + 1) % RN;
        try { const st = VM_STATE.get(fn); if (st && st.g) { pcSnap[si] = (st.g[st.j] | 0); scSnap[si] = scalarsOf(st); iwin.__vmtJ = st.j; iwin.__vmtI = st.i; } else { pcSnap[si] = -1; scSnap[si] = null; } scAbs[si] = iwin.__vmtN; si = (si + 1) % SNAP; } catch {}
        iwin.__vmtN++; iwin.__vmtForm[form]++;
      };
      iwin.__vmtBranch = function (flowN, N) {  // dump PC + changed scalar regs per op -> the compare operands / decision input
        try { const at2 = (a) => { for (let i = 0; i < SNAP; i++) if (scAbs[i] === a) return i; return -1; };
          const opAt = (a) => { for (let i = 0; i < RN; i++) if (slot[i] === a) return ring[i] & 0x1ff; return -1; };
          const SKIP = {}; SKIP[iwin.__vmtJ] = 1; SKIP[iwin.__vmtI] = 1;  // PC reg (shown as pc=) + opcode decode key — pure churn
          const win = (lo, hi) => { const W = [], JUMPS = []; let prev = {}, ppc = null;
            for (let a = lo; a <= hi; a++) { const i = at2(a); if (i < 0) continue; const pc = pcSnap[i], sc = scSnap[i] || {}, ch = [];
              for (const k in sc) { if (SKIP[k]) continue; if (prev[k] !== sc[k]) ch.push(k + '=' + (typeof sc[k] === 'string' ? JSON.stringify(sc[k]) : sc[k])); }
              const jmp = ppc != null && Math.abs(pc - ppc) > 24 ? ' <<JUMP ' + ppc + '->' + pc + '>>' : '';
              if (jmp) JUMPS.push(a + ': ' + ppc + ' -> ' + pc);
              if (ch.length || jmp || a <= lo + 4) W.push(a + ' op=' + opAt(a) + ' pc=' + pc + jmp + (ch.length ? '  Δ{' + ch.join(' ') + '}' : '')); prev = sc; ppc = pc; }
            return { W, JUMPS }; };
          const early = win(0, Math.min(N - 1, 90)), flow = win(Math.max(0, (flowN || N) - 40), flowN != null ? flowN + 3 : N);
          // FULL scalar snapshot per op across the whole collection->decision span (not just deltas) so a value
          // SET early (the trusted/untrusted verdict) and TESTED later is visible at both points. Filter the PC +
          // key churn + zero/tiny-int counters; keep strings, bools, floats, big ints (the probe results + verdict).
          const full = []; for (let a = 0; a <= (flowN != null ? flowN + 1 : N); a++) { const i = at2(a); if (i < 0) continue; const sc = scSnap[i] || {}, kv = [];
            for (const k in sc) { if (SKIP[k]) continue; const v = sc[k]; if (typeof v === 'boolean' || typeof v === 'string' || (typeof v === 'number' && (v !== (v | 0) || Math.abs(v) > 1023))) kv.push(k + '=' + (typeof v === 'string' ? JSON.stringify(v) : v)); }
            if (kv.length) full.push(a + ' pc=' + pcSnap[i] + ' {' + kv.join(' ') + '}'); }
          fs.writeFileSync('spike/_vmfull.txt', 'flowN=' + flowN + ' total=' + N + ' PCreg=' + iwin.__vmtJ + ' keyReg=' + iwin.__vmtI + '\n(full scalar snapshot/op: strings+bools+floats+bignums, PC/key/small-int filtered)\n' + full.join('\n'));
          fs.writeFileSync('spike/_vmbranch.txt', 'flowN=' + flowN + ' total=' + N + ' PCreg=' + iwin.__vmtJ + ' keyReg=' + iwin.__vmtI +
            '\n(Δ = scalar regs changed since prev op; <<JUMP>> = non-sequential PC = fn call/return/branch)\n' +
            '\n=== EARLY 0..90 (the h() entry + complete-vs-flow decision should be here) ===\n= jumps: ' + early.JUMPS.join('  ') + '\n' + early.W.join('\n') +
            '\n\n=== FLOW WINDOW (flowN-40..) ===\n=jumps: ' + flow.JUMPS.join('  ') + '\n' + flow.W.join('\n'));
        } catch (e) { try { fs.writeFileSync('spike/_vmbranch.txt', 'branch dump err: ' + (e && e.stack || e)); } catch {} }
      };
      iwin.__vmtDump = function (tail) {
        const N = iwin.__vmtN, flowN = iwin.__vmtFlowN, k = Math.min(tail || 600, N, RN), out = [];
        for (let i = 0; i < k; i++) { const j = ((ri - k + i) % RN + RN) % RN; const v = ring[j]; out.push(slot[j] + '|' + (v >> 9) + ':' + (v & 0x1ff)); }
        // Dump the handler SOURCE for opcodes around the flow branch (flowN-34 .. flowN+2) — the conditional
        // jump that chose flow lives here; its source shows the compared register/value.
        const unwrap = (fn) => { let t = fn, g = 0; while (BOUND_TARGETS.has(t) && g++ < 12) t = BOUND_TARGETS.get(t); return t; };
        const srcOf = (fn) => { try { return rts.call(unwrap(fn)).replace(/\s+/g, ' '); } catch (e) { return 'ERR'; } };
        const at = (a) => { for (let i = 0; i < k; i++) { const j = ((ri - k + i) % RN + RN) % RN; if (slot[j] === a) return j; } return -1; };
        const handlers = [];           // FULL source of the opcodes around the flow branch (flowN-70..flowN+3)
        if (flowN != null) for (let a = Math.max(0, flowN - 70); a <= flowN + 3; a++) { const j = at(a); if (j >= 0 && fns[j]) handlers.push(a + ' op=' + (ring[j] & 0x1ff) + ' :: ' + srcOf(fns[j])); }
        const dict = {};               // dedup opcode -> handler source (the build's instruction set)
        for (let i = 0; i < k; i++) { const j = ((ri - k + i) % RN + RN) % RN; const op = ring[j] & 0x1ff; if (fns[j] && !dict[op]) dict[op] = srcOf(fns[j]); }
        try { fs.writeFileSync('spike/_vmt_handlers.txt', 'flowN=' + flowN + ' total=' + N + '\n\n=== BRANCH WINDOW (full src) ===\n' + handlers.join('\n') + '\n\n=== OPCODE DICTIONARY ===\n' + Object.keys(dict).sort((x, y) => x - y).map((op) => op + ' :: ' + dict[op]).join('\n')); } catch {}
        return { total: N, flowN, byForm: iwin.__vmtForm, tailOps: out, branchHandlers: handlers.slice(-40).map((h) => h.slice(0, 120)) };
      };
      // Names/constants rotate every build, but the dispatch has a STRUCTURAL signature that survives:
      // `REGS[op ^ CONST](op)` — a computed member-call whose index xors the opcode with a constant and
      // whose call-arg IS that same opcode. (Exactly the 2 loop-body dispatch sites; verified unique.)
      // Both loop bodies dispatch `REGS[<index over op>](op)`; the index is either `op^CONST` (form 1) or a
      // helper `FN(op,CONST)` (form 2 = the hot try-body loop). Signature: the call-arg `op` also appears
      // INSIDE the preceding `[...]`. Wrap both; tag form so we can tell the hot loop.
      let nP = 0;
      const ID = '[A-Za-z_$][\\w$]*';
      const H = (m, arg) => m.slice(0, m.length - (arg.length + 2));  // strip trailing `(arg)` -> the handler `REGS[idx]`
      code = code.replace(new RegExp(`(${ID})\\[(${ID})\\^(${ID})\\]\\((${ID})\\)`, 'g'),
        (m, regs, a, b, arg) => ((arg === a || arg === b) ? (nP++, `(globalThis.__vmt&&globalThis.__vmt(${arg},1,${H(m, arg)}),${m})`) : m));
      code = code.replace(new RegExp(`(${ID})\\[(${ID})\\((${ID}),(${ID})\\)\\]\\((${ID})\\)`, 'g'),
        (m, regs, fn, a, b, arg) => ((arg === a || arg === b) ? (nP++, `(globalThis.__vmt&&globalThis.__vmt(${arg},2,${H(m, arg)}),${m})`) : m));
      log('VMTRACE dispatch patch: ' + (nP ? nP + ' site(s) wrapped' : 'PATTERN NOT FOUND (re-derive: grep dump for REGS[op^const](op))'));
    }
    if (code && code.trim()) runScriptIn(iwin, code, 'cf-vm-iframe.js');
  }
  log('iframe booted: ' + url);
  setTimeout(() => {
    const o = iwin._cf_chl_opt;
    logOnce(`iframe state @2s: _cf_chl_opt=${o ? Object.keys(o).length + 'keys' : 'NONE'}` +
      ` lHnIp5=${o ? typeof o.lHnIp5 : '-'} body=${!!iwin.document.body}` +
      ` bodyShadow=${iwin.document.body ? !!iwin.document.body.shadowRoot : '-'}` +
      ` msgL=${(iwin.__msgListeners || []).length}`);
    if (process.env.CF_CHLOPT_DUMP && o) {  // dump our iframe's _cf_chl_opt to diff vs uc's
      try { fs.writeFileSync(process.env.CF_CHLOPT_DUMP, JSON.stringify(o, (k, v) => (typeof v === 'function' ? '[fn]' : v), 1)); log('chlopt dumped'); } catch (e) { log('chlopt dump err: ' + e); }
    }
    if (process.env.CF_VMTRACE && iwin.__vmtDump) {  // dump the SA opcode tail (PC:opcode) ending at the flow decision
      try { const d = iwin.__vmtDump(800); const out = process.env.CF_VMTRACE_OUT || 'spike/_vmtrace.json'; fs.writeFileSync(out, JSON.stringify(d)); if (iwin.__vmtBranch) iwin.__vmtBranch(d.flowN, d.total); log(`VMTRACE: ${d.total} opcodes total; tail(${d.tailOps.length}) -> ${out}; branch -> spike/_vmbranch.txt`); } catch (e) { log('vmt dump err: ' + e); }
    }
    if (process.env.CF_ENV_DUMP) {  // dev self-diff: snapshot the iframe realm vs the real-Chrome baseline
      try {
        const r = iwin.eval(`(function(){var N=navigator,W=window,out={};function tos(f){try{return Function.prototype.toString.call(f)}catch(e){return 'ERR'}}
          out.nav={}; ['userAgent','platform','vendor','language','languages','hardwareConcurrency','deviceMemory','maxTouchPoints','webdriver','productSub','vendorSub','pdfViewerEnabled'].forEach(function(k){out.nav[k]=N[k]});
          out.navKeys=[]; for(var k in N){out.navKeys.push(k)}
          out.pluginsLen=N.plugins&&N.plugins.length; out.mimeLen=N.mimeTypes&&N.mimeTypes.length;
          out.uaData=N.userAgentData?{brands:N.userAgentData.brands,platform:N.userAgentData.platform,mobile:N.userAgentData.mobile}:null;
          try{var c=document.createElement('canvas');c.width=200;c.height=50;var x=c.getContext('2d');x.textBaseline='top';x.font="14px 'Arial'";x.fillStyle='#f60';x.fillRect(0,0,100,30);x.fillStyle='#069';x.fillText('Cloudflare ⚡ 1.0',2,15);out.canvasLen=c.toDataURL().length;out.canvasHead=c.toDataURL().slice(0,40)}catch(e){out.canvasErr=String(e)}
          try{var gc=document.createElement('canvas');var gl=gc.getContext('webgl');var dbg=gl.getExtension('WEBGL_debug_renderer_info');out.webgl={r:gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL),exts:(gl.getSupportedExtensions()||[]).length}}catch(e){out.webglErr=String(e)}
          out.natives={fetch:tos(W.fetch),Worker:tos(W.Worker),eval:tos(W.eval),winTag:Object.prototype.toString.call(W),docTag:Object.prototype.toString.call(document)};
          out.domNatives={appendChild:tos(W.Node.prototype.appendChild),createElement:tos(W.document.createElement),attachShadow:tos(W.Element.prototype.attachShadow),getRandomValues:tos(W.crypto.getRandomValues),querySelector:tos(W.document.querySelector),addEventListener:tos(W.EventTarget.prototype.addEventListener),setTimeout:tos(W.setTimeout),toString:tos(W.Function.prototype.toString),postMessage:tos(W.postMessage)};
          out.gpu_tostr=Object.prototype.toString.call(N.gpu); out.gpu_keys=N.gpu?Object.keys(N.gpu):null;
          try{out.gpu_chain=[];var g=N.gpu;for(var i=0;i<4&&g;i++){out.gpu_chain.push(Object.prototype.toString.call(g));g=Object.getPrototypeOf(g)}}catch(e){out.gpu_chain='E:'+e}
          try{out.perf_res=(W.performance.getEntriesByType('resource')||[]).length;out.perf_entries=(W.performance.getEntries()||[]).map(function(e){return e.entryType})}catch(e){out.perf_err=String(e)}
          out.winProps=Object.getOwnPropertyNames(W); out.winPropCount=out.winProps.length; out.hasChrome=typeof W.chrome; out.subtle=!!(W.crypto&&W.crypto.subtle);
          try{out.errStack=new Error('x').stack.split('\\n')[1]}catch(e){}
          out.docHidden=document.hidden; out.visState=document.visibilityState; out.cookieEnabled=N.cookieEnabled;
          return JSON.stringify(out)})()`);
        fs.writeFileSync(process.env.CF_ENV_DUMP, r);
        log('env dumped -> ' + process.env.CF_ENV_DUMP);
      } catch (e) { log('env dump err: ' + (e && e.message)); }
    }
  }, 2500);
}

main().catch((e) => { dumpAccessLog(); send({ t: 'error', msg: String((e && e.stack) || e) }); process.exit(1); });
