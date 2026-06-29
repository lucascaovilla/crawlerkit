# Turnstile solver — session handoff

> **➡️ If you are a fresh session picking this up: read `TURNSTILE_SOLVE_PLAN.md` first** — it is the
> current end-to-end brief (the `unsupported_browser` wall + the selenium-ground-truth / deobfuscate /
> V8-switch methodology to crack it, plus poc-infra-pa testing). This file below is the deep technical
> state it refers to.

Resume point for the browserless Cloudflare Turnstile solver in **crawlerkit**. Read this, then
continue at **"Next task"**. Full design lives in
`~/.claude/plans/browserless-cloudflare-turnstile-purrfect-ocean.md`; durable facts are in
`~/.claude/projects/-home-caovilla-Projects-crawlerkit/memory/` (`detran-pa-turnstile-target.md`,
`crawlerkit-sandbox-env.md`).

Branch: `feat/turnstile-solver`. **Nothing is committed** — commit only when asked.

## Goal

`TurnstileSolver.solve(challenge, transport)` returns a real `cf-turnstile-response` token for
**managed/non-interactive** Turnstile by running the challenge JS in an embedded JS engine with a
faked browser env derived from the active `Profile`, routing the challenge's network calls back
through the same `transport`. Interactive escalation is NOT faked — raise `InteractiveChallengeError`.
No real browser. No token caching.

## Decisions (locked)

- **Engine = `pythonmonkey`** (SpiderMonkey), a normal pip dependency of `crawlerkit-core`. No Rust,
  no maturin, no cibuildwheel, **nothing new published to PyPI, no new repo**. Chosen for: real
  async event loop + native Python↔JS interop (clean fetch/cookie bridge) + simplest packaging.
  Kept behind `_turnstile/engine.py` so it's swappable (deno_core/V8 later) without touching the
  public API. (This SUPERSEDES an earlier deno_core/maturin decision — ignore the "Native build
  story" / cibuildwheel sections in the plan.)
- Turnstile is a **standalone** core solver, like `McaptchaPowSolver` — no dependency on `govbr`.
- Contract: `turnstile_hint(page_url, html, *, sitekey, action, cdata, pagedata)` carries full
  context; `solve(challenge, transport)` signature stays frozen.

## ⭐⭐⭐⭐⭐⭐ SESSION 7 (2026-06-28): READ the bytecode branch — it's an ANTI-AUTOMATION FINGERPRINT (WebRTC/frameElement/tamper), not a PoW compare

Built a runtime **register/PC tracer** on top of Session 6's opcode tracer and READ what `h()` actually does in
the decision window. **The "branch" is not a crypto-PoW threshold compare — `h()` collects an anti-automation
fingerprint and the env fails specific probes the prior (getter-based) access-log never saw.** Found + fixed 2
real env bugs and identified WebRTC as the standout gap, but **the flow POST still fires unconditionally** after
`execute` — so the true gate is either an unmatched probe (likely WebRTC server-IP correlation) or, more likely,
**upstream of `h()` entirely** (see the `execute`-event reframe below). Nothing committed; 61 tests green.

### New durable tooling: the register/PC branch tracer (`sidecar.mjs`, gated `CF_VMTRACE`)
- The bind-wrapper now also records `boundHandler -> its bound this` (the VM state) in `VM_STATE`; `__vmt` snapshots
  **PC value (`state.g[state.j]`) + all scalar registers** per opcode into rolling rings; `__vmtBranch` dumps
  **`spike/_vmbranch.txt`** = an EARLY window `0..90` (h() entry) + a FLOW window (`flowN-40..`) + a JUMPS list
  (non-sequential PC = fn calls/returns), with the PC + decode-key registers auto-filtered as noise.
- Run (loop until the rotation-inconsistent dispatch patch matches → `total>400`, prints `PATTERN NOT FOUND` when
  it misses): `for i in $(seq 8); do CF_VMTRACE=1 CF_SIDECAR_DUMP=spike/_vm_now.js CF_TIMEOUT=12 PYTHONPATH=$PWD
  .venv/bin/python spike/ntest_turnstile.py >/dev/null 2>&1; n=$(.venv/bin/python -c "import json;print(json.load(open('spike/_vmtrace.json')).get('total',0))"); echo "try $i total=$n"; [ "$n" -gt 400 ] && break; done`

### What `h()` actually reads at the decision (from `spike/_vmbranch.txt`, build-agnostic values)
The bytecode VM `SA(SE(bytecodeA))` running `h()` materialises these STRING CONSTANTS at its entry (ops 0–90),
then collects/encrypts them and POSTs `/flow/`. **These are anti-automation probes the Session-4 access-recorder
missed** (it wrapped navigator/screen/perf/document GETTERS; these are a constructor + bytecode-internal strings):
- **`window.frameElement`** — probed REPEATEDLY (most-referenced string). A real CF iframe is **cross-origin** →
  `frameElement === null`. Ours was **`[object HTMLIFrameElement]`** (jsdom hosts the iframe same-origin). **FIXED.**
- **WebRTC**: builds `{iceCandidatePoolSize:1, iceServers:[{urls:"stun:stun.cloudflare.com:3478"}]}` →
  `new RTCPeerConnection(...)` + ICE gathering. jsdom has NONE; `matchWindowSurface` only adds a no-op stub
  constructor → zero candidates. **Added an experimental functional stub (unproven).**
- **Native-fn toString tamper checks** (ops 24–78 build `"function rtKJo1"`-style expected strings → booleans).
  Found a real gap: `spoofNatives` marked the XHR **constructor** native but **not its prototype methods**
  (`open`/`send`/`setRequestHeader`/`addEventListener`) — and the VM uses XHR for the flow POST, so a
  `XMLHttpRequest.prototype.open.toString()` check saw our shim source. **FIXED** (mark all XHR proto methods).
- The 16-hex `a12f…` value in the flow URL (`a12fc8addc5211fe` / `a12fd129dbaa9…`) is the **challenge/session id**
  (stable `a12f` prefix + per-run nonce), NOT a fingerprint hash — don't chase it.

### Fixes applied this session (sidecar.mjs; KEEP unless a reason emerges)
1. **`frameElement` forced `null`** on the challenge realm (cross-origin fidelity) — `bootIframe` after `setupWindow`.
2. **XHR prototype methods native-marked** in `spoofNatives` (tamper-toString hygiene; the VM uses XHR).
3. **Experimental `installWebRTC(win)`** (after `matchWindowSurface`) — functional `RTCPeerConnection` emitting a
   plausible **mDNS host candidate + gathering-complete**, no srflx/public-IP candidate, **no real STUN round-trip**.
   Net-more-faithful than a no-op stub but UNPROVEN; revisit when testing the IP-correlation hypothesis.

### GROUND TRUTH that reframes everything: the token is minted LOCALLY (→ definitely browserless-crackable)
`poc-infra-pa/tools/selenium_capture/out/{messages,network}.json` (uc easy path) shows **exactly 2
challenge-platform requests (api.js + the iframe GET), ZERO `/flow/` POSTs**, and the 752-char token arrives in
the **`complete`** message. **So uc's VM `h()` mints the token LOCALLY with no server round-trip.** Ours runs the
same `h()` but decides "untrusted" → POSTs `/flow/` to prove itself → server tarpits → overrun. Therefore:
- The complete-vs-flow decision is **purely LOCAL inside `h()`** (no server input on the easy path) and the token
  is **locally mintable** ⇒ **browserless self-mint IS achievable** if the local probes pass. (uc's recorder only
  logs VM→parent msgs, so its missing `execute`/`extraParams` are capture artifacts — the real signal is uc's VM
  posts `complete`, ours posts the flow + `overrunBegin`. The "decision is upstream in api.js/`execute`" idea is
  thus WEAKENED — it's the VM's own local branch.)
- **WebRTC server-IP correlation is RULED OUT** as the gate (no server in the local decision). What the VM checks
  is whether its **local** anti-automation probes pass.

### The decisive negative result (5 fixes tested, none flip it) — WebRTC RULED OUT
**CONFIRMED the VM constructs `new RTCPeerConnection({iceCandidatePoolSize:1,iceServers:[{urls:"stun:stun.
cloudflare.com:3478"}]})` in the headless path** and **traced its exact usage** (debug logs in every stub method):
`new RTCPeerConnection → createDataChannel → addEventListener('icecandidate') → createOffer → setLocalDescription
→ close` — the classic WebRTC local-IP-discovery snippet, **closes immediately, does NOT await a connection**.
Tested, NONE flip the flow: (1) `frameElement=null`, (2) XHR-proto native-mark, (3) WebRTC host candidate (async),
(4) WebRTC host+**srflx** (`CF_WEBRTC_SRFLX_IP`), (5) WebRTC **synchronous** host+srflx candidates fired inside
`setLocalDescription` + munged into `localDescription.sdp` (so a non-waiting/SDP-parse reader sees them). **So
WebRTC is fully satisfied by the stub yet it still flows → WebRTC is NOT the discriminator.** (frameElement and
XHR-tamper likewise ruled out by direct test.)

### What the full-register trace shows at the fork (`spike/_vmfull.txt`, new this session)
`__vmtBranch` now ALSO writes **`spike/_vmfull.txt`** = the FULL scalar-register snapshot per op across the whole
collect→decide span (strings/bools/floats/bignums; PC/key/small-int filtered). Reading it (flowN=523 run): the
~60 ops right before the flow (`pc→972`) are a **serialization LOOP dominated by `frameElement`/`window.frameElement`**
(regs hold `"frameElement"`, `"window.frameElement"`, a derived bool flips false→true per iteration). The probe
booleans (regs ~40/42/43/46) flip true/false during collection (ops 24–140: tamper + WebRTC) — these are temp
regs, reused per item, NOT a persistent verdict. **The decision is UPSTREAM of this serialization loop** (the loop
just encrypts the already-collected fingerprint into the flow body); flow is unconditional once chosen.

### Cross-origin FRAME ISOLATION — IMPLEMENTED + RULED OUT (this session)
Built `makeCrossOriginWindow(realWin)` (a cross-origin `Window` Proxy: allows only the spec cross-origin members,
throws `SecurityError` on all else, `location` read-throws/set-allows) and wired it gated behind **`CF_XORIGIN=1`**
in `bootIframe`: `iwin.parent`/`iwin.top` → the proxy, and parent→VM delivery `sourceWin` switched `pwin`→proxy so
the VM's `e.source === window.parent` still holds. **Verified the bus is intact** (full handshake runs under
`CF_XORIGIN=1`). Added a one-shot `[xorigin]` log in the proxy's SecurityError path: **the VM NEVER reads a
cross-origin parent prop** (`parent.location`/`document`/etc.) — the proxy is inert, the flow fires identically.
**So the VM does NOT probe cross-origin frame isolation; hypothesis ruled out.** The fork's `window.frameElement`
churn is just serializing the (null) value into the body, not a relationship check. (Proxy kept, gated, documented.)

### The meta-result: iframe-realm env probes do NOT gate the flow — look at the HANDSHAKE / h() internals
**5 distinct fixes now ruled out by direct test, and NONE changes the outcome at all** (frameElement-value,
XHR-tamper, WebRTC, cross-origin). Captured the full parent↔VM handshake (`CF_MSGDBG=1`, dumps `MSGFULL <event> ::
<json>` in `deliver`): `init{mode:"non-interactive", nextRcV}` → `requestExtraParams` → `extraParams{appearance:
"always", execution:"render", ch:"<12hex>", timeInitMs/timeRenderMs/…, wPr:{…}, url}` → `translationInit` →
**`execute{}` (UNCONDITIONAL — no mode/hard flag)** → flow. **We DO receive the easy/managed config** (mode
non-interactive, execution=render, appearance=always — matches Session-5's `_cf_chl_opt`), and `execute` fires
regardless, so api.js always runs h(). The flow-vs-complete fork is a value h() COMPUTES that none of the visible
iframe-realm env signals affect. **NEXT, two concrete leads:** (1) dump the FULL `extraParams` (esp. the truncated
`wPr` object + the `time*Ms` values — if h() checks timing/`wPr`, our jsdom values may differ); raise the
`MSGFULL` slice or write each payload to a file. (2) The disciplined tracer path is still open: extend
`__vmtBranch` to capture the verdict register at the pre-serialization branch (between probe collection ~op140 and
the serialization loop ~op440) and trace it to its source — it may be a CONSTANT from `bytecodeA`/the challenge,
not an env read. Diff our `extraParams`/`nextRcV` vs a uc iframe-realm capture (selenium OOPIF) to see if the
parent-computed inputs to h() already differ.

### State of the experimental WebRTC stub (KEEP, gated by nothing — unconditional)
`installWebRTC(win)` is now confirmed-exercised + more faithful than the no-op surface stub, but UNPROVEN and not
the gate. Method-call debug logs (`[webrtc] …`, gated `CF_SIDECAR_DEBUG`) are kept. Revisit/trim when the real
discriminator is found; a half-faked WebRTC could itself be a minor tell.

### ⛔ FINAL CONCLUSION (this session): client-side env does NOT gate the flow — 8 levers, ZERO effect
Mapped the **entire** h() anti-automation probe sweep from the register trace (`_vmfull.txt`): native-fn tamper
(eval/toString → all `[native code]`), a `Date.now()` timing bracket (op218→257), WebRTC (`stun.cloudflare.com`),
`getTestability`/`whenStable` (Angular), `frameElement`, and **two nested `sandbox="allow-same-origin"` pristine
iframe realms** (cross-realm `toString` verified clean — `bw.FP.toString.call(MAIN.fetch)` → `[native code]`).
Then tested **8 distinct client-side manipulations** — frameElement=null, XHR-proto native-mark, WebRTC (host /
+srflx / sync+SDP-munged), cross-origin parent proxy (`CF_XORIGIN`), pristine-realm toString, `Date.now`/`perf.now`
compression (`CF_FASTTIME`), and clamped `extraParams` timings (`CF_FASTPARAMS`). **NOT ONE changes the outcome —
the flow POST fires byte-identically every run.** The `CF_XORIGIN` probe even proved the VM never reads a
cross-origin parent prop. **So the flow-vs-complete decision is NOT influenced by any client-side signal a
browserless engine can control.** By elimination, the path is **baked into the server-provided challenge at the
iframe GET** (decided from the request gestalt) — which **re-supports SESSION 4's network-level ceiling and
contradicts the SESSION 5/6 "client-side, crackable" premise** (that was inferred from `_cf_chl_opt`'s config
flags matching uc's, but identical config flags ≠ identical bytecode decision). uc gets a "mint-locally" challenge;
a browserless curl_cffi+jsdom client gets a "prove-via-flow" challenge, and no JS-env fidelity flips it.

**Honest status: browserless self-mint is NOT achievable for this deployment via client-env work** — proven by
direct bytecode-VM instrumentation, not inference. Remaining routes (all break "browserless-pure" or need external
help, decision is the user's): (a) a real-Blink network stack for ONLY the 2 easy-path GETs via CDP/Playwright —
**forbidden by the hard rule**; (b) a byte-exact Chrome HTTP/2/TLS stack beyond curl_cffi at the iframe GET (heavy,
uncertain — S4 matched JA4 exactly and still got the hard challenge); (c) a 3rd-party Turnstile solver
(CapSolver/2captcha — keeps the crawler process browserless, needs an API key + cost). To DISPROVE the server-side
conclusion next session, the only lever left is the **iframe GET request** itself (not client JS): diff our iframe
GET request vs uc's at the wire and vary impersonation/cookies/`cf_clearance`; if the returned challenge's flow
behavior changes, it's request-gated (and possibly reachable); if not, the ceiling stands. All env fixes + the
gated diagnostics (`CF_XORIGIN`/`CF_MSGDBG`/`CF_REALMDBG`/`CF_FASTTIME`/`CF_FASTPARAMS`) are kept; 61 tests green.



Improved the deobfuscator and traced the complete-vs-flowPOST decision end-to-end through readable JS.
It bottoms out in the bytecode VM (as Session 5 said), but the JS scaffolding around it is now fully mapped.

### Deobf toolchain upgraded (durable, `spike/re/resolve.js`)
Rewrote `resolve.js` Babel-**binding/scope-aware**: resolves decoder aliases bound by PARAM-reassignment
(`function(d,W4,kq,k){ W4={d:1368}, kq=ds, … kq(W4.d) }`) and numeric maps built via assignment/param-
reassign (not just `var X={…}`). **Result: 5168 decoder calls resolved (was ~115); 264 aliases, 272 maps.**
`resolved.js` is now readable — property names, URLs, event strings are literals. Rotation const **C=2085**,
arr len **2119** (unchanged this build). Re-run: `cd spike/re && node resolve.js`. The `N["IfMXQ8"]('b64==$b64')`
calls littering everything are obfuscator NO-OPS (comma-sequence, result discarded) — ignore them.

### The branch, fully mapped (readable-JS scaffolding around the bytecode)
- Iframe VM message handler is `vH` (resolved.js ~5449). On parent msg `execute` → `vF` (5486) →
  **`fL()`** (the flow-POST function, def @4823).
- `fL` runs the gates, ALL of which PASS for our real sitekey, so fL ALWAYS reaches the flow branch:
  `if (fT()) postMessage(unsupported_browser)` — `fT`=PP capability check (@10318/10358), we pass → false.
  then `!fy(43200,…) ? return : ( fv(), setTimeout(qtiBE4,1000), !fI() ? return : <FLOW> )`.
  - **`fy`** (@4300) = "challenge not expired" — true when `|now − _cf_chl_opt.yJYp6| < 12h` (fresh) → true.
  - **`fI`** (@10731) is NOT the easy/hard decider — it's the **test-sitekey shortcut + final render**:
    reads `_cf_chl_opt.wsCy5` (= the **sitekey**) and special-cases CF's documented TEST keys
    (`1x…AA`/`1x…BB`→post `{event:"complete",token}`, `2x…AB`/`2x…BB`→fail 600010, `3x…FF`→interactive).
    For a REAL key none match → default branch returns **true** → fL proceeds to flow. (This is also the
    ONLY readable `event:"complete"` site, lines 10891/10920 — real-key completes come from the flow path.)
- FLOW branch (resolved.js line 5000, one giant line): `v1(); G = "/cdn-cgi/challenge-platform/h/"+uizGB0+
  "/flow/ov"+…;  h = runProgram('<base64 bytecodeA>', N);  h();  v0(onload@6041)`.
  `runProgram(d)` (@179) = `SA(new SE(d),0,210,[])`. **`SA`=the bytecode interpreter (@6813), `SE`=cursor
  (@6386).** SA is a register-machine VM: RC4-style opcode decode, dispatch `vn[vC ^ opcode](opcode)` to
  handler fns stored IN the register array. **`h()` runs the PoW and DECIDES: post `complete` locally
  (easy) vs trigger the flow XHR (hard). That decision is in bytecodeA (data) — not readable JS.**

### Runtime proof (this session, fresh live challenge, `spike/ntest_turnstile.py`)
Trace: `init,init,requestExtraParams,extraParams,translationInit, execute → NET POST /flow/ov1 (+196ms,
bodyLen 4183) → overrunBegin → timeout`. uc (easy) has **no `execute`-driven flow** — completes right
after translationInit. **Access log (`spike/_acc.log`) — the ONLY env reads between `execute` and the
flow POST:** `crypto` ×3 (PoW), `navigator.gpu` ×2, `sub/sub document.readyState`. Earlier in load:
parent `performance.getEntriesByType("resource") = Array(0)` and sub `getEntries() = Array(1)` — i.e.
**almost no resource-timing entries** though api.js+iframe loaded (a real page has several). `gpu` already
has a realistic adapter in sidecar (`requestAdapter`/`wgslLanguageFeatures`, @sidecar.mjs:417). gpu/
readyState/perf-navigation were tried before; **resource-timing entries look genuinely un-faked** and are
the top untested candidate, but NOT yet proven to be the discriminator.

### uc IFRAME-realm CDP probe now WORKS (Phase 2 infra, durable in `spike/uc_chlopt.py`)
`switch_to.frame` CANNOT reach the challenge iframe (CLOSED shadow root — brief was right). Solved via
`Page.addScriptToEvaluateOnNewDocument` injecting `INJECT_PROBE`: runs in EVERY new doc, re-checks
`location.href` INSIDE a delayed `cap()` (the iframe is `about:blank` at doc-creation — gating up front
silently skips it), and postMessages env snapshots (t=600/1500/2500/3300ms, tagged by frame depth) to
`top`, captured by the REC listener → `window.__m` → saved to `spike/_uc_ifenv.json`. **Requires
`--disable-site-isolation-trials`** (the cf iframe is an OOPIF the page session otherwise can't inject) —
BUT that flag makes uc get DETECTED → `init→requestExtraParams→translationInit→fail` (no token). So the
captured env is from a *detected* run; its **shape/native values are still genuine Chrome-144** (UA, gpu,
native-fn sources, win surface) and valid for diffing, but we did NOT get a depth=2 pristine-child snapshot
(the failed run made none). `spike/_our_ifenv.json` = our matching snapshot via `CF_ENV_DUMP` (probe
extended with gpu/perf fields). Diff helper: just `json.load` both and compare.

### What the ground-truth diff FOUND+FIXED this session (2 real bugs; neither is the discriminator)
- **gpu was own-enumerable** → `Object.keys(navigator.gpu)` returned `[requestAdapter,…]`; real Chrome's is
  `[]` (members live on `GPU.prototype`, non-enumerable; proto chain toStrings `[GPU,GPU,Object]`). Rebuilt
  the webgpu stub prototype-backed (sidecar.mjs ~420) — **ours now matches uc EXACTLY** (`gpu_keys=[]`,
  chain `[GPU,GPU,Object]`, `[object GPU]`). Path UNCHANGED → **gpu fully ruled out** (toStringTag + shape).
- **`navigator.userAgent` leaked `…jsdom/26.1.0` in the iframe realm!** jsdom's auto-created child-frame
  windows don't inherit the top JSDOM's configured UA, and `applyFingerprint` set platform/vendor/UAData but
  **never `userAgent`/`appVersion`**. Fixed: forces `fp.user_agent` every realm (sidecar.mjs:381). Path
  unchanged → not the client decider, but a real fix (kept; was poisoning the flow body).
- **BIG native-source gap (now fixed, still not the discriminator).** The uc diff (`domNatives` field) showed
  EVERY DOM method ours leaked its impl: jsdom builtins reveal `esValue/implSymbol/globalObject`, and OUR
  shims leak internals (`createElement`→`__createdIframes`, `setTimeout`→`logOnce`, `postMessage`→`deliver`).
  uc: uniform `[native code]`. `spoofNatives` only marked ~15 fns. Rewrote it: (1) `NATIVE_MARKED` WeakMap is
  now **module-shared across realms** (cross-realm `Fn.prototype.toString.call(mainFn)` from a pristine child
  no longer unmasks us); (2) a **realm sweep** marks every built-in whose source isn't already `[native code]`;
  (3) `mark()` no longer installs a per-fn own `toString` (that shim was itself leaky — real natives have no
  own toString); (4) late shims (setTimeout/createElement/postMessage/add+removeEventListener) marked at their
  install sites. **Verified: production toString-leak trace (`CF_VMTRACE=1`, routes leaks to stderr) is now
  EMPTY** — and crucially that means the VM toStrings NOTHING in our run, so **toString anti-tamper is NOT the
  decision mechanism either.** All kept (correct fidelity). Diff field added to `CF_ENV_DUMP` (`domNatives`).
- **"uc has no `execute`" = capture artifact** (`execute` is parent→VM; a received-msg log won't show it).
  uc DOES run `fL`+`h()`; it just `complete`s locally. Divergence is genuinely inside `h()`'s bytecode.
- Other remaining (small) env diffs ours→uc, none in the decision window: `winPropCount` 1204 vs **1234**
  (~30 missing globals); `perf_res` 0 vs 1 + uc has a `visibility-state` performance entry (sidecar.mjs:506
  returns only the nav entry); resource-timing reads happen pre-`execute` (body fingerprint), not at decision.

### Next (Phase 1 finish → Phase 2) — decision is in `h()`=`SA(new SE(bytecodeA),0,210,[])`
Env-shape signals are EXHAUSTED (gpu, UA, natives, toString, surface all match real Chrome, none flip it).
The discriminator is a **COMPUTED value** in the bytecode (PoW or the pristine child realm), not a property
read. **The opcode tracer is now BUILT and WORKING** (Phase-1 deliverable done):
- **CF_VMTRACE opcode tracer (BUILT, `sidecar.mjs bootIframe`).** Run `CF_VMTRACE=1 … spike/ntest_turnstile.py`.
  It **rotation-robustly** auto-finds SA's two dispatch sites by their STRUCTURAL signature `REGS[op^CONST](op)`
  and `REGS[FN(op,CONST)](op)` (the call-arg `op` also appears in the index) — survives the per-build name/
  string-array/RC4-constant rotation (vm.beau.js/resolved.js go stale every challenge; do NOT rely on literal
  names). Wraps each with `globalThis.__vmt(op,form)` → 32k ring buffer; `bridgeStream` stamps `__vmtFlowN` =
  opcode index when the `…/flow/…` POST fires; dumps `{total,flowN,byForm,tailOps:"absidx|form:op"}` to
  `spike/_vmtrace.json` at @2s. **Verified: a run gives ~595 opcodes total, the whole hot loop is form-1
  (`[op^const](op)`), and `flowN≈387` — so the flow-vs-complete branch is the handful of opcodes just BEFORE
  index ~387.** Helper `spike/_win.py` slices the decision window from an access log.
  **BREAKTHROUGH — the VM's bound-handler anti-RE is DEFEATED; the whole instruction set is now readable.**
  The handlers are bound (`fn.bind(state)` → `toString` = `[native code]`, blocking inspection). Beat it: the
  tracer (gated `CF_VMTRACE`) patches `Function.prototype.bind` to record `boundFn → target` in a WeakMap,
  follows the chain at dump time, and toStrings the UNBOUND target (with a module-load-captured clean
  toString, immune to our own spoof). **Result: every dispatched handler's real source is recovered.** Each
  run writes **`spike/_vmt_handlers.txt`**: (a) the full source of every opcode in the branch window
  `flowN-70..flowN+3`, and (b) a dedup'd **OPCODE DICTIONARY** (op-int → handler fn, ~32 instructions, e.g.
  `op 193→ty {return rG^rw}`=XOR, `op 5→tY`=add-builder, `op 226→tJ`=XOR, `op 146→tn`, `op 246→tB`). Caveat:
  the structural patch is rotation-inconsistent (`total` is 609/595/14/0 per run — loop runs until `total>400`
  for the hot loop). The handlers still use per-fn obfuscated string-decode (`f5(gA.f)`) — resolvable with the
  current build's decoder.
  **REMAINING (focused analysis, artifacts in hand): from `spike/_vmt_handlers.txt`, identify the CALL opcode
  (invokes a register-held host fn — that's what fires the flow XHR at flowN) and the conditional-JUMP opcode
  (reads a register, compares, sets the PC register) in the window just before `flowN`. Read what it compares
  and trace that register back to its env input. Then Phase 2: make that input faithful browserlessly.** (To
  fully de-obfuscate the handlers, recompute the rotation `C` for THIS build — resolve.js hardcodes 2085,
  STALE — by re-running the rotation IIFE in Node like the old `mktable.js`; do dump+trace+resolve in ONE run
  since the VM rotates per challenge.)
- **(b) Capture uc's PRISTINE child realm + a PASSING-uc iframe env.** The `_uc_ifenv.json` we have is from a
  *detected* uc run (the isolation flag trips detection) and has no depth=2 snapshot. To get a passing-uc
  iframe env WITHOUT the detection-causing `--disable-site-isolation-trials`, attach to the cf iframe's OWN
  OOPIF target: CDP `Target.setAutoAttach{autoAttach:true,flatten:true}` then run the probe via the child
  session's `Runtime.evaluate` / `Page.addScriptToEvaluateOnNewDocument`. Compare the pristine child realm's
  native-fn sources + readyState/timing vs ours — that realm is the top remaining suspect.
Concrete cheap fidelity wins still open (none proven to flip, do alongside): add the ~30 missing window
globals (1204→1234), a `visibility-state` + resource perf entry, align UA major to the impersonate version.
`spike/re/resolved.js` is the readable map (grep `fL`/`fy`/`fI`/`SA`/`SE`/`runProgram`). Diff inputs:
`spike/_uc_ifenv.json` (uc) vs `spike/_our_ifenv.json` (ours). 61 tests green; nothing committed.

## ⭐⭐⭐⭐ SESSION 5 (2026-06-28): IT'S CLIENT-SIDE — SESSION 4's "HTTP ceiling" verdict was WRONG

Ran the real browser myself (`undetected-chromedriver`, the sanctioned ground-truth tool; Chrome 144 at
`/opt/google/chrome`, `DISPLAY=:0`) and diffed it against our sidecar at the `_cf_chl_opt` level. This
**reverses Session 4's conclusion**: the wall is NOT the HTTP/TLS client gestalt — it is **client-side, in
how our jsdom VM runs the challenge**, and is in-principle fixable browserless.

### The proof (decisive)
- **uc easy path, fresh + reproducible** (`spike/uc_chlopt.py`): events `init → requestExtraParams →
  translationInit → complete`, token 752 chars, **only the iframe GET, NO flow POST**.
- **Our `_cf_chl_opt` (dumped via `CF_CHLOPT_DUMP`, `spike/_our_chlopt.json`) is the SAME non-interactive
  EASY config the server would give uc**: `Bvgjd1="non-interactive"`, `bgGRn3="chl_api_ni"`,
  `yFSEk9(execution)="render"`, `sMoC3(appearance)="always"`. **So the server did NOT hand us a harder
  challenge** — the easy/hard fork is NOT server-side / not the request fingerprint (Session 4 chased the
  wrong thing; TLS/headers/JA4 are all red herrings for this).
- Our VM instead goes `execute → runProgram → flow POST (+200ms, immediate, deterministic) → overrunBegin`.
  With a 60s window: `overrunBegin → forceFail → interactiveEnd → overrunEnd`. So **our jsdom VM takes the
  "prove-yourself" branch (flow POST) where a real browser mints the token locally and posts `complete`.**
  The server tarpits OUR flow POST because that path is the bot path.

### Where the branch lives (the real remaining target)
`runProgram` is a **bytecode interpreter**; the complete-vs-flowPOST decision is in the **bytecode program
(data)**, not readable JS. Deobf is re-established: `spike/re/` has `vm.beau.js` (js-beautify),
`resolve.js` (Babel resolver; rotation const **C=2085**, array len 2119) → partial `resolved.js`. The
access recorder shows the ONLY env signals runProgram reads before the flow POST: `crypto` (PoW),
`navigator.gpu`, `document.readyState`, `performance.getEntries()`. Tried, NONE flipped the branch:
realistic `navigator.gpu` adapter, a real `PerformanceNavigationTiming` entry, non-zero
`getBoundingClientRect`. (Double-`init` is a logging artifact — init#1 = VM→parent, init#2 = api.js `qt`→VM;
uc has both, its top-only recorder showed one.)

### Honest next step (heavy but bounded)
The trigger is either a COMPUTED value in the PoW (a hash/measurement) or an env signal read via the
bytecode that needs faithful emulation. To pinpoint it, **disassemble the runProgram bytecode** (opcode
switch; the handoff toolchain + `spike/re/resolve.js` is the start) to find the branch condition, OR
instrument the interpreter's opcode loop at runtime to log the comparison that selects flow-POST. This is
the genuine remaining work; it is browserless-tractable (no real browser needed at runtime). Tooling added
this session is in `spike/` (uc_chlopt, ntest_turnstile, tls_test, re/). `tls-client` was added to the venv
for the (negative) TLS-engine test — dev-only, not a crawlerkit dep. 61 tests still green; nothing committed.

## ⭐⭐⭐ SESSION 4 (2026-06-27): [SUPERSEDED conclusion] instrumented PoW; thought wall = HTTP gestalt

Built the brief's Phase-1 keystone (access-recorder) and it **inverted the diagnosis**. Key artifacts this
session live in the session scratchpad: `sidecar.mjs` access-recorder (`CF_ACCESS_LOG=<file>`),
`analyze.py`, `hdrprobe.py`/`orderprobe.py` (outgoing-header capture).

### What the access-recorder proved (the keystone finding)
Wrapped navigator/screen/performance/document/window/canvas/WebGL + getBoundingClientRect in logging
getters (getter-wrapping IN PLACE, on instance **and** prototype — defeats the `getOwnPropertyDescriptor(
proto,k).get.call(obj)` bypass a Proxy misses) on **every realm** (parent, challenge iframe, and the
pristine child iframe the PoW spins up — labelled PARENT / PARENT/sub / PARENT/sub/sub). Captured EXACTLY
what the PoW reads between `execute` and the flow POST. Result, stable across runs:
- The VM reads **almost nothing**: `window.crypto` ×~12 (PoW hashing), `performance.now/getEntries`,
  `document.readyState/compatMode`, `window.localStorage/speechSynthesis`, `navigator.sendBeacon`,
  `navigator.gpu` (→ undefined), `navigator.userAgentData`. **No `userAgent`, no `platform`, no
  `languages`, no `screen.*`, no canvas, no WebGL.** The flow body is ~3756 bytes but is NOT built from
  JS env property reads.
- Deobf cross-check agrees: `resolved_live.js` has **zero** `toDataURL/getContext/getImageData`, **zero**
  `WebAssembly`, **zero** extra `Worker`. The 4 `getBoundingClientRect` sites are all in click/touch
  handlers that never fire headless. So **canvas/WebGL/layout/navigator fidelity is IRRELEVANT to this
  challenge** — the brief's Phase-3 (real GL backend, real layout) would not have moved the needle, and per
  the brief's own gate ("add GL only if the access log shows it's needed") it is NOT needed.

### Where the wall actually is (network/transport trust at the iframe GET)
Network diff vs the passing uc capture (`selenium_capture/out/network.json`): the **EASY path = exactly 2
GETs** (api.js + the challenge iframe), token delivered in the `complete` postMessage, **no flow POST, no
extraParams POST**. The **HARD path adds the `…/flow/ov1/…` POST**, which the server **tarpits** (accepts
the connection, sends nothing — verified again this session: no `[flow-stream]` chunk ever arrives). Easy
vs hard is decided **server-side when it generates the iframe response**, i.e. from the **iframe GET
request fingerprint** — there is no JS self-check in between (the VM's reads happen *after* `execute`).

### Real bugs found + fixed in our request fingerprint (all kept; 61 tests still green)
Captured our actual outgoing headers (`hdrprobe.py`) and found genuine defects, all now fixed:
1. **`identity.py` emitted a Windows profile at random** (`os=("windows","linux")`) — UA/sec-ch-ua-platform
   said Windows while the JS env + uc ground truth are Linux. **Pinned `os=("linux",)`.**
2. **browserforge emitted INCOHERENT static `Sec-Fetch-*`** (`Sec-Fetch-Site: ?1`, `Sec-Fetch-User:
   document` — the four values rotated off their keys), baked into every request via
   `session.headers.update`. **Stripped them in `identity.py`**; curl_cffi's impersonate now supplies
   coherent per-context Sec-Fetch (`Site:none/Mode:navigate/...`).
3. **Sidecar sent ONE generic Sec-Fetch set for all sub-resources.** Now sends request-context-correct
   headers: api.js = `Dest:script/Mode:no-cors`, iframe = `Dest:iframe/Mode:navigate/UIR:1`, VM
   fetch/XHR = `Dest:empty/Mode:cors`.
4. **Cross-origin Referer was the full page URL**; real Chrome (strict-origin-when-cross-origin) sends
   **origin-only** — now matched to uc.
5. **High-entropy UA client hints** (`sec-ch-ua-arch/bitness/full-version-list/platform-version/model`)
   were absent; the iframe response demands them via **`Critical-CH`**. Now added (`clientHints()` in
   sidecar, coherent with UA), on the iframe + VM requests.
6. **Header ORDER**: curl_cffi appends custom headers after its template. Tried `default_headers=False`
   for exact order — **REVERTED**: it makes curl_cffi leak `sec-ch-ua: "HeadlessChrome"` on the wire (its
   engine is built from headless chromium; default mode lets our session "Google Chrome" override it). The
   minor order/`Sec-Fetch-User` imperfection is far less bad than a literal "Headless" leak.
7. Also added (fidelity, kept): non-zero `getBoundingClientRect` (~300×65 widget) and `navigator.gpu`.

### Verdict (evidence-backed, exhaustive) — it's REAL-BROWSER-ONLY, not the HTTP client
**None of the above flipped us off the hard path**, on a FRESH challenge window (cooldown reset the id, so
it is NOT reputation burn). Two decisive proofs that the wall is NOT at the HTTP-client level:
- **curl_cffi matched uc's JA4 + Akamai EXACTLY** (`t13d1516h2_8daaf6152771_d8a2da3f94cd` /
  `52d84b11737d980aef856699f885ca86`) and still got the hard path. A *perfect* fingerprint match failing ⇒
  the fingerprint is not the discriminator.
- Swapped the whole transport to **bogdanfinn `tls-client` (non-headless, real-Chrome BoringSSL,
  `chrome_138`)** — `spike/tls_test.py`, akamai also matched — and it **also got the hard path**. So a
  second, higher-fidelity, non-headless engine fails identically.
Therefore no HTTP-client impersonation (curl_cffi, tls-client, or any peer — you cannot beat "exact match")
earns the easy path here. The discriminator is something Cloudflare derives from a **real Blink browser**
that no scripted HTTP client reproduces (holistic page-load/connection gestalt or an undocumented server
heuristic). The hard path yields NO token: with a 60s window the VM goes `overrunBegin → forceFail →
interactiveEnd → overrunEnd` (force-fail + interactive escalation), matching the Session-3 ground truth
that even a detected real Chrome fails the hard path.

**Conclusion: browserless SELF-minting of a server-valid token is not achievable for this deployment.** The
only runtime-browserless route left is a **3rd-party Turnstile solver** (e.g. CapSolver/2captcha — keeps
crawlerkit's process browserless; needs an API key + has cost and possible token↔IP-binding risk). The
other options break the hard rule (a real Blink stack via CDP/Playwright for just the 2 easy-path GETs) or
accept the ceiling. `tls-client` was added to the venv (dev only, used solely by `spike/tls_test.py`; not a
crawlerkit dependency). Genuine request-fingerprint fixes (Linux pin, Sec-Fetch sanitize, Accept-Language,
client hints) are kept in the tree (61 tests green); nothing committed.

This is the brief's anticipated "honest ceiling" — but the **root cause is the network/transport trust
layer, not jsdom's DOM/GL/layout implementation depth** (instrumentation disproved the latter). Realistic
paths from here, none browserless-pure: (a) a real-Blink network stack for the 2 GETs only (CDP/Playwright
— forbidden by the hard rule); (b) byte-exact Chrome HTTP/2 beyond curl_cffi (heavy, uncertain); (c) a
3rd-party Turnstile solver for this target. Nothing committed.

## ⭐⭐ SESSION 3 (2026-06-27): GROUND TRUTH — target IS solvable; our sidecar is handed the HARD challenge

Built the offline selenium capture (`poc-infra-pa/tools/selenium_capture/`, dev-only, gitignored;
uc/selenium NEVER in crawlerkit runtime — hard rule). Decisive findings:

- **The Detran-PA passive Turnstile mints a real token (752 chars) in ~3.5s** via **undetected-chromedriver**
  — flow `init → requestExtraParams → translationInit → complete` (token is in the `complete` message,
  resolved_live.js:9098 `event:"complete",token:H`). **NO flow POST on this path.** Egress/IP is CLEAN.
- **Plain Selenium is DETECTED and FAILED** (`fail 300010`/`600010`; `600010` = a challenge request got
  HTTP 400, resolved_live.js:10095) even with real WebGL + `webdriver=false`. So Cloudflare gates on the
  CDP/automation tells, not the IP. **Our sidecar is NOT a CDP browser**, so it isn't caught by *that* — but
  it still doesn't look trusted enough.
- **Two paths exist:** trusted client → EASY path (immediate `complete`, ~3.4s, no PoW POST); untrusted →
  HARD path (the PoW + `flow/ov1` POST → which then **overruns→fail** even in a real-but-detected Chrome).
  **Our sidecar gets the HARD path** (that's why it reaches `execute`+POST then `overrunBegin`). The goal
  shifts: look trusted enough to be handed the EASY path. `overrunBegin` is NOT mainly our slowness — a
  real detected Chrome overruns on the hard path too.
- **Env baseline captured** (`tools/selenium_capture/out/`): UA `Chrome/144.0.0.0 (X11; Linux x86_64)`,
  platform `Linux x86_64`, hwConc 4, devMem 8; WebGL vendor `Google Inc. (Intel)` / renderer
  `ANGLE (Intel, Mesa Intel(R) HD Graphics 620 (KBL GT2), OpenGL ES 3.2)`, 36 exts; a real 3202-char
  `canvas.toDataURL`; natives `[native code]`; `[object Window]`/`[object HTMLDocument]`; subtle present;
  20 resource-timing entries. Plus `messages.json` (the ~3.4s passing timing) and `token.txt`.
- **Implication for the sidecar:** the stubbed canvas/WebGL (placeholder pixels/params) are the prime
  suspects for being classed untrusted → hard challenge. **Part C (fidelity) is now central** and likely
  needs a REAL backend (`node-canvas` for 2D, `headless-gl`/swiftshader for WebGL) — a static replay of the
  captured canvas won't match because the VM draws dynamic content. Re-run `capture.py` (uc) to refresh; the
  iframe-realm probe needs site-isolation-off (may re-trip detection) so the PAGE env is the usable baseline.

### Session 3 cont. — C.1 fidelity + impersonation done; blocker is now the streaming flow endpoint
- **Applied (committed, 61 tests green):** faithful env in `sidecar.mjs` — exact captured WebGL renderer +
  36 extensions + GL params, navigator `plugins(5)`/`mimeTypes(2)`/`userAgentData`/`maxTouchPoints`, a
  **native-`toString` spoof** (`spoofNatives`), and a **binary request-body fix** (`bodyToB64` — the PoW
  POSTs an ArrayBuffer; `String(body)` was corrupting it). `fingerprint.py` linux WebGL renderer + availH.
  `identity.py`: added curl_cffi `chrome136/142/145/146`, `DEFAULT_IMPERSONATE=chrome142`; `pick()` now
  yields **chrome146** with a consistent UA↔JA3.
- **Neither flipped us to the EASY path** — still issued the HARD challenge (`execute→PoW→flow POST→
  overrunBegin`). So trust is NOT gated on the JS-env values or the curl_cffi version we can control.
- **New concrete blocker:** the hard-path `POST …/flow/ov1/…` (the PoW submission) **never resolves** — no
  2xx, no error, no 20s timeout. That endpoint is a **streaming / long-poll** channel; our **synchronous**
  bridge (`node_engine._do_request` = blocking curl_cffi) waits for a full body that never ends → blocks
  the loop → the VM overruns. A real browser streams it. **This is architectural, not trust** (the hard
  path is the "prove-yourself" path — completing it correctly may well mint a token even without easy-path
  trust).
- **Built (committed, 61 green): the streaming/concurrent bridge** — `node_engine` now threads each request
  and streams `net-head→net-chunk*→net-end`; the sidecar XHR fires incremental `readyState 2/3/4`. Plus
  `bodyToB64` binary handling and browser-auto headers (Referer/Origin/Sec-Fetch-*) on the iframe's
  requests. All correct improvements.
- **But the flow POST is HELD SILENTLY by the server.** With `stream=True`, `curl_cffi` blocks on
  `session.request()` itself — **no response headers, no body, no error** for `…/flow/…` (the GET requests
  stream fine). The server accepts the TCP connection and sends NOTHING. Adding Referer/Origin/Sec-Fetch
  did not change it. So it is NOT a streaming-delivery or request-format problem — the server is
  **stalling our submission**. Combined with: faithful JS env (C.1) and recent matching impersonation
  (chrome146) both failed to flip us to the EASY path → **the wall is server-side client trust at the
  TLS/HTTP/session layer**, below what a curl_cffi+jsdom browserless client controls. uc passes because it
  is real Blink + real Chrome TLS; a real browser is forbidden at runtime. **Concrete levers exhausted.**
  Remaining directions are heavy + uncertain: byte-exact Chrome-144 TLS/HTTP2 (beyond curl_cffi), session
  warming / cf_clearance, or behavioral telemetry — none guaranteed. Honest read: this deployment grants a
  silent token only to a real-browser gestalt; the browserless engine is built + correct but capped here.
- **TLS RULED OUT (measured).** `tls.peet.ws/api/all` via our transport (curl_cffi chrome146) vs the
  PASSING uc Chrome: **JA4 identical** (`t13d1516h2_8daaf6152771_d8a2da3f94cd`), **HTTP/2 Akamai hash
  identical** (`52d84b11737d980aef856699f885ca86`). JA3 differs but Chrome randomizes JA3 per-connection
  (JA4 is the stable one). So our TLS/HTTP2 is a perfect Chrome match — NOT the differentiator.
- **Real canvas added (node-canvas / Cairo)** — `getContext('2d')`/`toDataURL`/`getImageData` are now real
  rendered pixels (sidecar `package.json` dep `canvas`; optional, falls back to stub). Did NOT flip the path.
- **Self-diff (sidecar iframe env vs real-Chrome baseline; via `CF_ENV_DUMP`).** Fixed the clear tells it
  found: proper `Function.prototype.toString` spoof (the robust `Function.prototype.toString.call(fetch)`
  now returns `[native code]`), added `window.chrome`, and `[object HTMLDocument]` tag. Verified fixed —
  **still did NOT flip the path.** Remaining diff is STRUCTURAL: **`Object.getOwnPropertyNames(window)` =
  511 for us vs ~900 in real Chrome** — jsdom implements ~half of Blink's window API surface, and the PoW
  fingerprint (sent in the `flow` body, which the server then tarpits) detects the missing surface +
  behaviours. This is the jsdom-vs-Blink **engine gestalt** — the fundamental limit of browserless-via-jsdom.
- **Window SURFACE now matched (committed).** Captured real Chrome's 1204 window own-prop names
  (`sidecar/chrome_window_props.json`, refreshable). `matchWindowSurface()` adds the ~700 missing globals as
  native-looking stubs AND overrides `Object.{getOwnPropertyNames,keys}(window)` to return exactly the real
  list — which HID the damning extras the diff exposed: **jsdom internals (`_globalProxy`/`_globalObject`/
  `_eventHandlers`/`_customElementRegistry`/…) and our own plumbing (`__createdIframes`/`__markNative`/…)
  were leaking on `window`** (instant jsdom+tamper tells). Verified: winPropCount 1204, zero leaking
  internals, VM doesn't break. **Still did NOT flip the path.**
- **CONCLUSION (evidence-backed, exhaustive):** every MEASURABLE SURFACE signal now matches real Chrome —
  TLS/JA4, navigator/screen values, real canvas pixels, native-`toString`, `window.chrome`, doc tag, and the
  full window property surface with jsdom internals hidden — and the server STILL tarpits the flow POST. So
  the residual differentiator is **BEHAVIORAL / implementation-depth**: jsdom's API *implementations* differ
  from Blink's in ways the PoW exercises (layout: `getBoundingClientRect` is all-zero; WebGL `readPixels`
  returns zeros not real pixels; the ~700 stub constructors have empty prototypes; navigator `for-in` enum
  ~10 vs ~60; event/timing behaviours). Fixing these blind is low-yield — the ONLY high-confidence path is to
  **instrument what the PoW actually reads** (an access-recording Proxy on the iframe realm) OR disassemble
  the `runProgram` bytecode, to learn the EXACT checks, then satisfy precisely those. Deep RE; needs fresh
  focused context. Short of that (or a real Blink engine, forbidden at runtime), this deployment's silent
  token is beyond the browserless solver — but the env is now extremely faithful, so the remaining gap is
  narrow + specific.

## ⭐ SESSION 2 (2026-06-26): Node+jsdom+V8 sidecar — the PoW now SUBMITS

After cracking `unsupported_browser` (the `PP()` capability checks) and `runProgram` in the pythonmonkey
engine, the next blocker was the proof-of-work **bytecode VM** doing shadow-DOM/fingerprint introspection
that hand-stubs couldn't satisfy. **Decision (Lucas): build the Node+jsdom+V8 sidecar** (memory
[[turnstile-engine-decision]]). It now runs the WHOLE challenge in real V8 over a real DOM and **the PoW
computes + POSTs its challenge submission** (`POST /cdn-cgi/challenge-platform/h/b/flow/ov1/…`) — something
the pythonmonkey engine never reached (`requests=[]`). No token yet, but this is the core anti-bot running.

**Files (new):** `_turnstile/sidecar/sidecar.mjs` (Node engine) + `sidecar/package.json` (dep: jsdom 26;
`npm install` done, `node_modules` present) + `_turnstile/node_engine.py` (Python driver: spawns node,
proxies `fetch`/`XHR`/`sendBeacon` to curl_cffi `Transport` over newline-JSON stdio, base64 bodies — same
JA3/cookies). NOT yet wired into `TurnstileSolver.solve` (engine.py still calls pythonmonkey). Run it with
`scripts`-style harness `scratchpad/ntest.py` (primes detran → `node_engine.run_challenge`). Debug env vars:
`CF_SIDECAR_DEBUG=1` (verbose logs: NET/MSG/threw), `CF_SIDECAR_DUMP=<path>` (dump the exact iframe VM this
run), `CF_TIMEOUT=<s>`.

**Architecture (mirrors env.py/frame.py on real jsdom windows):** parent jsdom of the form → run api.js →
**explicit** `turnstile.render` (jsdom has no layout so implicit/visibility render never fires; we also
`classList.remove('cf-turnstile')` first so there's exactly ONE render) → api.js builds the widget in a
shadow root and creates the challenge iframe. The VM runs in the iframe's **real child-frame window** (a
hidden light-DOM `<iframe>` whose `contentWindow` we PIN onto api.js's shadow iframe) — REQUIRED because
(a) jsdom gives shadow-DOM iframes no `contentWindow`, and (b) the VM bails unless `top!==self` (jsdom's
`window.top` is a non-configurable self-reference, so a standalone jsdom can't fake being framed). Manual
**postMessage bus** (`deliver()`/`installBus`) delivers `{isTrusted:true, source, origin}` events (jsdom's
MessageEvent.isTrusted is false, which api.js rejects); source=iwin so api.js's `e.source ===
iframe.contentWindow` holds.

**jsdom gaps filled in `augmentWindow` (the recurring theme — jsdom is ~95% complete, patch the last 5%):**
`performance.getEntries/getEntriesByType/mark/measure/timing`; `innerText` (alias→textContent);
`matchMedia`/`IntersectionObserver`/`ResizeObserver`/`PerformanceObserver`/`requestIdleCallback`; canvas
2d+WebGL stubs; **the PP gate trio Worker / URL.createObjectURL / ReadableStream.pipeTo**; `crypto.subtle`
(wired to Node WebCrypto). And: the PoW evals in a throwaway child iframe → jsdom returns null
`contentWindow` for unconnected iframes, so `setupWindow` lazily backs any null `contentWindow` with a
hidden light-DOM frame window. Each was found by mapping the V8 error offset (sourceURL `cf-vm-iframe.js`,
or the iframe URL) into the `CF_SIDECAR_DUMP`'d VM, then decoding via the deobf toolchain.

**Verified flow:** `init → requestExtraParams → extraParams → translationInit → execute → (PoW) → POST flow`.
**CURRENT BLOCKER:** after `execute` the VM posts `overrunBegin` (a watchdog/telemetry marker — NOT fatal;
in one run the flow POST fired *after* it) then stalls before a token. Open work: (1) make the PoW complete
+ submit **consistently** (timing-variant right now — sometimes the watchdog fires before the POST; the env
is slower than a real browser, and the backed-subframe eval may be racy); (2) process the flow-POST
response → the widget callback (`__cfToken`) / hidden `cf-turnstile-response`; (3) residual fidelity risk —
canvas/WebGL are stubs (no real pixels), the eval'd child realm is a jsdom window — the server may reject a
token built from those even once minting works. Deobf of the live VM: `scratchpad/re/resolved_live.js`
(toolchain is per-build — re-extract decoder offset + rotation target; see `live_mktable.js`).

## DONE + verified

- **Offline foundations** (pure-Python, all green): `base.py` typed errors
  (`InteractiveChallengeError`, `ChallengeEngineError`) + `CaptchaRegistry._resolve` (detect+hint
  merge); `base_crawler.py` `solve_captcha(source, *, hint=None)`; `turnstile.py` (`turnstile_hint`
  + real `solve()` orchestration); `_turnstile/{widget,fingerprint,engine}.py`; tests; docs split
  (`docs/cracking-turnstile.md` + `docs/cracking-govbr.md`).
- **Live target confirmed + captured.** Fixture: `tests/fixtures/turnstile/detran_pa_form.{html,cookies.json}`.
- **poc-infra-pa wired as the live "turnstile test"** — runs end-to-end and fails cleanly at the
  engine boundary (everything except the engine is proven).
- **Local venv** `crawlerkit/.venv` is now **Python 3.11.15** (rebuilt via `uv` — pythonmonkey
  segfaults on 3.14; see step 1) with all deps + `pythonmonkey` + `py-mini-racer`; **`pytest` = 61 passed**.
- **Engine BUILT + runs the live Cloudflare api.js to widget render** (`_turnstile/{env,bridge}.py`
  written, `engine.py` rewritten for pythonmonkey). Remaining: emulate the challenge iframe (step 5).

## Key facts (the live target)

- Form (still a JSF form POST): `https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/servicos/infracao/indexConsultaInfracao.jsf`
- **Cookie prime (required):** GET `https://www.detran.pa.gov.br/` first (sets `veio_da_home`),
  THEN the form GET returns the real ~7KB form. Without it → 302 to the portal. Session cookies:
  `veio_da_home`, `INGRESSCOOKIE`, `JSESSIONID`.
- **Sitekey `0x4AAAAAADpvM_lNoEdBJ3cR`, passive/managed** (not an interactive interstitial).
  Page loads `https://challenges.cloudflare.com/turnstile/v0/api.js` (67KB; uses a cross-origin
  **iframe + challenge-platform** + postMessage — the hard part).
- POST fields: `indexForm, placa, renavam, confirma, j_idt23, javax.faces.ViewState` +
  `cf-turnstile-response=<token>`.
- Egress: direct from this machine (no proxy locally). Use the venv: **`crawlerkit/.venv/bin/python`**.

## Next task — build the pythonmonkey engine (the gate)

1. ~~install pythonmonkey + confirm it runs JS~~ **DONE.** `pythonmonkey==1.3.2` installed. Its async
   event-loop binding **segfaults on CPython 3.14** (`'_asyncio.Task' has no attribute 'call_later'`
   from inside `pythonmonkey.so`; `await pm.wait()` crashes). **Pivoted the venv to Python 3.11.15**
   via `uv` (`uv python install 3.11`; uv at `~/.local/bin`). `.venv` rebuilt with
   `uv pip install -e ".[dev]" pythonmonkey py-mini-racer` → **61 passed**. On 3.11 the gate PASSES:
   setTimeout pump + Promise→global + **JS→Python sync callback** (the fetch-bridge path) all proven.
   JS `await` of a Python *coroutine* returns `null` — irrelevant, curl_cffi transport is sync.
   `engine.py`/`__init__.py` docstrings still describe the dead native-V8/deno_core/maturin plan —
   rewrite `engine.py` to boot pythonmonkey (no `_load_ext`, no `crawlerkit._turnstile_engine`).
2. ~~Build `_turnstile/env.py`~~ **DONE.** Faked browser env seeded from `fingerprint.Fingerprint`:
   window/document/location/navigator/screen/crypto, canvas+WebGL stubs, `document.cookie` ↔
   `transport._session.cookies.jar`, an id registry (`getElementById`), the `cf-turnstile` element
   with `data-callback="__turnstileToken"`, DOM-interface constructors with a custom
   `Symbol.hasInstance` (so api.js's `instanceof HTMLScriptElement` etc. pass), `attachShadow`,
   `contains`, observers. A miss-logging Proxy records every unstubbed access to `globalThis.__undef`.
3. ~~Build `_turnstile/bridge.py`~~ **DONE.** `fetch`/`XMLHttpRequest`/`sendBeacon` shims that
   OVERRIDE pythonmonkey's own (aiohttp-backed, fingerprint-leaking) network with synchronous calls
   into `transport` (curl_cffi is sync). Text + base64 bodies; captures the request sequence in
   `_RequestBridge.requests`. Smoke-tested: fetch routes to transport, cookie bridge works both ways.
4. ~~Rewrite `engine.py` for pythonmonkey~~ **DONE.** `run_challenge` spins its own asyncio loop,
   installs bridge+env, fetches+evals the live api.js, lets the widget render IMPLICITLY (detran is
   implicit: api.js auto-renders `.cf-turnstile`, token via `data-callback`), and pumps in 0.1s
   `asyncio.sleep` slices (NOT `await pm.wait()` — that blocks until the loop is fully idle and hangs
   on a live challenge) until `__token` is a string or `timeout`. `pm.stop()` cleans up. NOTE: JS
   `null`/`undefined` → truthy pythonmonkey sentinels, so always coerce via a JS `typeof==='string'`
   guard before trusting a value (bit us as a token false-positive).
   **Verified live: api.js now runs clean through widget render** (`__undef=[]`, no JS exceptions).
   Iterate with **`scripts/harvest_turnstile.py`** (primes detran → runs the engine → on no-token
   dumps `requests` + `renderError` + `__undef`). The evidence ladder already climbed:
   `HTMLScriptElement` undefined → `Symbol.hasInstance` script-tag find → `attachShadow` →
   `element.contains` → `featurePolicy`/`isConnected`. Each fix in `env.py`, filled from observation.

5. **Iframe emulation — BUILT (`_turnstile/frame.py`), handshake fully working, VM not yet completing.**
   - `env.py` intercepts the IFRAME `src` set (`__iframeSrc` → `__onIframeSrc` hook) and exposes a real
     bidirectional **postMessage bus** (`__installBus`/`__deliverMessage`) with `isTrusted:true`.
   - `frame.py` `load_frame()` fetches the challenge iframe through the transport and runs its ONE
     ~240KB inline VM with `window/self/parent/top/document/location/globalThis` shadowed to the
     iframe's window (= the iframe element's `contentWindow`, so api.js's `e.source ===
     iframe.contentWindow` check passes). The VM uses `window.`/`parent.` only (0 `globalThis`), so one
     SpiderMonkey realm is enough.
   - **Working live:** the full api.js↔VM handshake runs — `init` → `requestExtraParams` → api.js
     ships `extraParams` → VM `translationInit`, plus the `meow`/`food` heartbeat. Getting here needed:
     iframe found via `widget.shadow.querySelector("#"+id)` (env `querySelector("#id")`→`byId`); deliver
     VM→parent with `origin = iframe origin` (not `'*'`); deliver parent→VM with `source = window.parent`
     handle; a real listener store (don't `|| {}` through the permissive proxy); page+frame DOM made
     **permissive** (`trapVoid` returns a chainable stub, terminates tree-walks, empty-iterable) so
     api.js's `requestExtraParams` DOM fingerprint (`bn`/`ba`/`createNodeIterator`/`styleSheets`/
     `localStorage`/`NodeFilter`) doesn't throw; standard engine globals copied onto the iframe window
     (`URL/Blob/BigInt/Worker/PerformanceObserver/...`).
   - **The VM now RUNS TO COMPLETION (no crashes).** Cleared every env gap: stub-on-miss DOM queries
     (`ifdoc.querySelector/getElementById` return a permissive stub via `__voidStub`, never null, so
     `q(x).innerText` doesn't throw), `TextEncoder`/`TextDecoder`/`Blob`/`structuredClone`/
     `queueMicrotask`/`ReadableStream` polyfills (SpiderMonkey lacks them), all standard globals copied
     onto the iframe window, `voidStub` made empty-iterable + tree-walk-terminating, page+frame DOM
     permissive (`trapVoid`). Frame `__undef` is clean. Verified full sequence:
     `init → requestExtraParams → extraParams → translationInit → reject`.
   - **`unsupported_browser` gate — CRACKED (2026-06-26, session 2).** Deobfuscated the VM (toolchain
     below) and found the reject is posted by function `Pn` **iff `PP()` returns true**. `PP()` is NOT a
     behavioural/engine fingerprint — it is six concrete **capability checks** (`Y` = iframe window):
     (1) `new Worker(URL.createObjectURL(new Blob([...],{type:'text/javascript'})))` in a try/catch —
     throw ⇒ unsupported; (2) `ReadableStream.prototype.pipeTo === undefined`; (3) `!BigInt`;
     (4) `!crypto || !crypto.getRandomValues`; (5) `typeof performance.getEntries !== 'function'`;
     (6) `typeof PerformanceObserver !== 'function'`. Our env failed **(1)** (SpiderMonkey's native `URL`
     has no `createObjectURL` → threw) and **(2)** (env.py's `ReadableStream` stub had no `pipeTo`).
     **Fix (env.py):** added `URL.createObjectURL`/`revokeObjectURL` (return a `blob:` string) and
     `ReadableStream.prototype.{pipeTo,pipeThrough,tee,cancel}`. Verified: reject is GONE, full handshake
     now runs `init → requestExtraParams → extraParams → execute → translationInit` + heartbeat.
   - **`runProgram is not defined` gate — CRACKED.** Past PP, the `execute` handler calls bare
     `runProgram(...)`, which the VM defines as `window.runProgram = fn`. In a real page `window ===
     globalThis`, so that creates a bare global; our `cw` is a plain object param, so the bare call was a
     ReferenceError. **Fix (frame.py):** `cw` is now built by `__makeFrameWindow` (a Proxy that, on
     `window.X = fn` for **function** values, also mirrors `globalThis.X = fn`) so the VM's bare calls
     resolve. Kept the VM in **strict mode + window/self/... params** (do NOT switch to `with`/sloppy: it
     makes a plain call's `this` the real engine global, so the VM's `Y = this || self` stops being `cw`
     and everything reads off the wrong window — confirmed-and-reverted dead end).
   - **CURRENT BLOCKER — `runProgram` bytecode + DOM-builder env completeness.** `runProgram` is a
     **bytecode interpreter** (opcode `switch` at fn `dt`; errors are posted as a `feedbackOrigin/rayId`
     telemetry message by `aYotS`). It now runs and crashes inside fingerprint/DOM collection, e.g.
     `TypeError: can't access property "lHnIp5", N[P] is undefined`. `_cf_chl_opt.lHnIp5` is the
     challenge's **shadow-root container** (`document.body.attachShadow(...)`, resolved.js:11034) on which
     it does `querySelector/appendChild/getElementById`. These are STRUCTURAL gaps (our env returns
     undefined/stub where the VM needs a real object) — readable straight from the deobfuscated source,
     and the next ones to fix. Likely a multi-gate grind; Phase A ground truth (real-Chrome env dump) is
     the systematic way to get the remaining VALUES right once the structure holds.
   - **DEOBFUSCATION TOOLCHAIN (reproducible, in session scratchpad `…/scratchpad/re/`).** The VM is
     obfuscator.io: one `;`-split string array decoded by `F(n)=array[n-450]` after a rotation IIFE
     (checksum target `492667` over `P()`). Steps: `npx webcrack vm.raw.js` (beautify; does NOT inline the
     array) → `mktable.js` (re-runs P+F+rotator in Node, dumps `table.json` = idx→string) → `transform.js`
     (Babel: folds the per-function numeric-map wrappers `obj.key→N`, then resolves every decoder-alias
     call `alias(N)→string`, scope-correct via bindings) → **`resolved.js`** (fully readable VM; grep it
     to read any check by name). Re-run on each rotation: refetch the iframe HTML, extract the inline
     `<script>` → `vm.raw.js`, repeat. The string-array NAMES (`rOjl5`, `lHnIp5`, …) are stable across the
     current deployment, so stack-trace function names map 1:1 into `resolved.js`.
   - **Debug aids in place (guarded, low-overhead):** set `globalThis.__rec=true` → every trap/trapVoid
     property READ is ring-buffered in `__getLog`; `__busErrs` records swallowed message-handler throws
     (with name+message+stack); the timeout detail dumps `iframes`/`renderError`/`frameError`/`__undef`.
     `scripts/harvest_turnstile.py` runs the engine path; **`scratchpad/probe.py`** (boot env+bridge+frame,
     RECORD every `__deliverMessage` payload so you SEE the reject/handshake, eval api.js, drive
     `load_frame`, dump events/`__busErrs`/`__frameErr`/requests/`__undef`) is the tightest debug loop —
     far better than harvest for seeing what the VM posts. `_read_token` trusts ONLY `__token` (api.js's
     validated extraction); the `__frameToken` heuristic false-matched the `nextRcV` nonce — do NOT trust it.

Keep the rotation-prone surface isolated in `_turnstile/`; keep `TurnstileSolver.solve` + the
registry entry frozen. `spike/turnstile_spike.py` (py_mini_racer) is throwaway — the real
instrumentation tool is `scripts/harvest_turnstile.py` (pythonmonkey, full path).

**Deps + POC (done):** `pyproject.toml` now hard-deps `pythonmonkey>=1.3` and caps
`requires-python = ">=3.11,<3.14"`. The POC `crawlers/detran_pa/crawler.py` has a CLI:
`python -m crawlers.detran_pa.crawler --data plate=SZT3I75 renavam=01433191358` (maps `plate`→`placa`,
direct egress, exits 1 + `CAPTCHA FAIL: …` on any `CaptchaError`, else prints `consulted OK …`). Run it
on a 3.11 venv that has crawlerkit-core editable-installed (pythonmonkey comes transitively), or via
`PYTHONPATH=…/poc-infra-pa …/crawlerkit/.venv/bin/python -m crawlers.detran_pa.crawler …`. Today it
fails cleanly at the captcha; it will consult the moment the engine mints a valid token.

## How to test

All from `/home/caovilla/Projects/crawlerkit` using `.venv/bin/python`.

- **Unit/offline suite:** `.venv/bin/python -m pytest -q`  (expect 61+ passed).
- **Re-capture the live form** (refresh fixture / inspect): `.venv/bin/python scripts/capture_turnstile.py`
  → prints sitekey + saves `tests/fixtures/turnstile/detran_pa_form.{html,cookies.json}`.
- **Live end-to-end through the real crawler** (the "turnstile test"):
  ```
  PYTHONPATH=/home/caovilla/Projects/poc-infra-pa .venv/bin/python -c "
  from crawlers.detran_pa.crawler import DetranPA
  c = DetranPA()
  print(c.run({'placa':'ABC1D23','renavam':'12345678901'}).status)
  c.close()"
  ```
  Today this raises `ChallengeEngineError` (engine not built). When the engine works it should mint a
  token, POST, and return the infractions page. **Get a valid-format `placa`/`renavam` from the user
  for a non-empty result** (random ones are fine to exercise the path).
- **Engine smoke (once built):** run the captured challenge through `engine.run_challenge` against
  the fixture and assert a token comes out; assert interactive escalation → `InteractiveChallengeError`.

## File map

- Solver public: `crawlerkit/core/captcha/turnstile.py`
- Engine guts (isolated): `crawlerkit/core/captcha/_turnstile/{engine,env,bridge,fingerprint,widget}.py`
  (all exist; `engine.py`+`env.py` are where the iframe-emulation work lands)
- Registry/errors: `crawlerkit/core/captcha/base.py`; crawler hook: `crawlerkit/core/base_crawler.py`
- Capture: `scripts/capture_turnstile.py`; **live engine instrumentation: `scripts/harvest_turnstile.py`**;
  fixtures: `tests/fixtures/turnstile/`. (Cached api.js for offline inspection lives in the session
  scratchpad, not committed.)
- Throwaway spike: `spike/turnstile_spike.py`
- poc-infra-pa: `~/Projects/poc-infra-pa/crawlers/detran_pa/{crawler,worker_main}.py`, `requirements.txt`
