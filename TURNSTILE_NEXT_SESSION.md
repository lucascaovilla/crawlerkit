# Turnstile solver — next-session brief (SESSION 8): copy the wire, keep reading the branch

> **You are a fresh Claude Code session.** Mission unchanged: make crawlerkit's **browserless**
> Node+jsdom+V8 Turnstile sidecar mint a **server-valid** `cf-turnstile-response` for Detran-PA and prove
> it through the in-repo crawler. Read this fully, then read `TURNSTILE_HANDOFF.md` **SESSION 7** (deep
> state + every dead end with line refs) and the two memory files (`detran-pa-turnstile-target`,
> `turnstile-engine-decision`). Use `~/Projects/crawlerkit/.venv/bin/python` (3.11, uv-managed). Baseline:
> `cd ~/Projects/crawlerkit && .venv/bin/python -m pytest -q` → 61. Don't commit unless asked.

## The reframe that drives this session (read twice)
**A real browser (undetected-chromedriver, Chrome 144) passes the EASY path from THIS exact machine +
THIS IP in ~3.5s, minting a 752-char token with ZERO `/flow/` POSTs** (`poc-infra-pa/tools/
selenium_capture/out/{messages,network}.json`). Our browserless engine, same machine/IP/session, gets the
HARD path (the `/flow/` POST, which the server tarpits → overrun → no token).

> **So this is NOT a ceiling and NOT IP/reputation. uc proves the target is solvable from here. If a real
> browser passes and we don't, we are impersonating WRONG somewhere. Find where, and copy it exactly.**

Do NOT propose a 3rd-party solver and do NOT propose a real browser at runtime — both are banned (see Hard
rules). The job is to make our independent, browserless client indistinguishable from the real Chrome that
already works on this box.

## Hard rules (non-negotiable)
- **Browserless + independent.** Runtime solver = Node + jsdom + V8 + curl_cffi ONLY. NO browser / CDP /
  Playwright / Selenium and NO third-party/paid solver service, ever, in the runtime path.
- `undetected-chromedriver`/selenium are **OFFLINE GROUND-TRUTH TOOLS ONLY** (`spike/uc_chlopt.py`,
  `poc-infra-pa/tools/selenium_capture/`) — used to capture what a real browser does, never shipped.
- Public API frozen (`TurnstileSolver.solve`, `turnstile_hint`, registry). Keep rotation-prone code in
  `crawlerkit/core/captcha/_turnstile/`.

## PROVEN this session — do NOT re-chase (SESSION 7 exhausted the CLIENT-JS surface)
Built a register/PC bytecode tracer and read what `h()` actually does. The flow-vs-complete decision is **a
value `h()` computes that NO client-side JS-env signal we control affects.** Mapped the full anti-automation
probe sweep (native-fn `toString` tamper [all `[native code]`], `Date.now()` timing bracket, WebRTC
`stun.cloudflare.com`, `getTestability`/`whenStable`, `frameElement`, **two nested `sandbox=allow-same-origin`
pristine iframe realms** with verified-clean cross-realm `toString`). Then tested **8 distinct client-side
manipulations — ALL ZERO EFFECT, flow fires byte-identically every run:** `frameElement=null`, XHR-proto
native-mark, WebRTC (host / +srflx / sync+SDP-munged), cross-origin parent proxy (`CF_XORIGIN` — proved the
VM never even reads a cross-origin parent prop), pristine-realm toString, `Date.now`/`perf.now` compression
(`CF_FASTTIME`), `extraParams` timing clamp (`CF_FASTPARAMS`). Also confirmed: we DO receive the EASY config
(`init.mode=non-interactive`, `extraParams.execution=render`, `appearance=always`), and `execute` (parent→VM)
is UNCONDITIONAL. **Conclusion: the discriminator is NOT in jsdom env fidelity.** By elimination it's set by
the **server-provided challenge at the iframe GET**, i.e. our **REQUEST WIRE IMAGE** differs from real
Chrome's — OR it's a branch input we never actually READ (we only ruled out inputs empirically; we never
deobfuscated and read the exact compare). Both tracks below.

## ⭐ PRIMARY TRACK — byte-exact wire impersonation (the iframe GET)
Real Chrome's request to `challenges.cloudflare.com` is more than JA4. SESSION 3/4 matched JA4 + the Akamai
HTTP/2 hash via `tls.peet.ws` and still got the hard path — but those are coarse HASHES; the **full** wire
image (TLS ClientHello extension ORDER + GREASE + ALPN + sigalgs + supported-groups + key-share; HTTP/2
SETTINGS values + frame order + WINDOW_UPDATE + PRIORITY; pseudo-header order; header order/CASE; HPACK;
cookie set) was never byte-diffed. curl_cffi's `impersonate` is an APPROXIMATION.

**Step A (cheap, do first): full fingerprint diff, curl_cffi vs real Chrome, on THIS box.**
- Hit `https://tls.peet.ws/api/all` with our `Transport` (`crawlerkit/core/transport.py` / the same client
  the crawler uses) → save JSON. The `spike/ja4probe.py` / `examples/fingerprint_demo.py` already do this.
- Hit the SAME URL with the offline uc (Chrome 144, `DISPLAY=:0`) → save JSON.
- **Diff the WHOLE JSON, not just `ja4`/`akamai`:** `ja3`, `ja4`, `ja4_r` (raw, un-hashed — shows extension
  order/GREASE), `peetprint`, `http2.akamai_fingerprint` AND `http2.sent_frames` (SETTINGS values, header
  frame pseudo-order), `http_headers` (exact order + casing). Every divergence is a lead.

**Step B (deep): capture the ACTUAL request to challenges.cloudflare.com (not a generic echo).**
- Raw TLS ClientHello (for JA3/JA4 + exact extensions): `tcpdump`/`tshark` on the iface, filter
  `host challenges.cloudflare.com`, extract the ClientHello — for BOTH uc and our client.
- Decrypted HTTP/2 + headers: run a local **mitmproxy** (its CA trusted by uc) so you see the iframe GET's
  decrypted frames/headers; point curl_cffi at it too (`Transport` honours proxy/env). Compare frame-for-
  frame and header-for-header. (mitmproxy is an OFFLINE diff tool, not a runtime dep.)

**Fix:** make `identity.py` / `transport.py` reproduce real-Chrome's image exactly. curl_cffi supports
CUSTOM fingerprints (`ja3=`, `akamai=`, `extra_fp=`) beyond the named `impersonate` presets — use them to
close any ClientHello/HTTP2/header-order gap the diff finds. Re-test after each change. **The bar: our
`tls.peet.ws` JSON is byte-identical to uc's.** Then re-run the live crawler.

## ⭐ SECONDARY TRACK — finally READ the branch (don't tunnel on the wire)
We ruled out inputs empirically but never deobfuscated the actual compare. Worth doing in parallel; if the
wire turns out clean, this is where the answer is.
- **Deobfuscate THIS build's VM and read the flow-vs-complete branch.** Toolchain: `spike/re/resolve.js`
  (Babel scope-aware) — but it hardcodes the stale rotation const `C=2085`. Recompute `C` for `spike/_vm_now.js`
  (run the rotation IIFE in Node, like the old `mktable.js`), re-resolve, find `SA`'s dispatch + the handler
  that writes the PC register from a value compare. The VM rotates per challenge, so dump+trace+resolve in
  ONE run (`CF_VMTRACE=1 CF_SIDECAR_DUMP=spike/_vm_now.js`). Cross-ref the register trace (`spike/_vmfull.txt`
  = full scalar regs/op; `spike/_vmbranch.txt` = jumps + windows) to pin the verdict register, then read what
  feeds it — it may be a CONSTANT from `bytecodeA`/the challenge data (server-set), confirming the wire track.
- **Last untested decision-window env read: `document.readyState`** (access log: the only decision-window
  reads are crypto-RNG, `navigator.gpu` [matched], `readyState`). Force the iframe + the `/sub`/`/sub/sub`
  realms' `readyState` through `loading`/`interactive`/`complete` and re-test. Cheap; close it out.
- **Diff the parent-computed inputs to `h()`** (`extraParams`/`wPr`/`nextRcV`, `CF_MSGDBG=1` dumps them to
  `spike/_msg_*.json`) against a uc iframe-realm capture (the OOPIF probe in `spike/uc_chlopt.py`).

## Tooling (durable, all in `spike/` + the sidecar; gated diagnostics added SESSION 7)
- VM tracer: `CF_VMTRACE=1` → `spike/_vmtrace.json` + `_vmt_handlers.txt` + **`_vmbranch.txt`** (PC + reg
  deltas, jumps) + **`_vmfull.txt`** (full scalar regs/op). Patch is rotation-inconsistent — loop until
  `total>400`. Harness: `spike/ntest_turnstile.py`.
- Handshake dump: `CF_MSGDBG=1` → `MSGFULL <event>` logs + `spike/_msg_<event>.json`.
- Pristine-realm check: `CF_REALMDBG=1`. Cross-origin proxy: `CF_XORIGIN=1`. Time compression:
  `CF_FASTTIME=k` / `CF_FASTPARAMS=1`. All gated, zero effect on the default path (kept as instruments).
- Ground truth: `poc-infra-pa/tools/selenium_capture/` (uc easy path: 2 GETs, 0 flow, token in `complete`,
  `out/{messages,network,env_page,token}.json`); `spike/uc_chlopt.py` (uc iframe-realm OOPIF probe); the
  captured uc iframe HTML `spike/_uc_iframe.html`.
- Sidecar engine: `crawlerkit/core/captcha/_turnstile/sidecar/sidecar.mjs` (+ `node_engine.py` driver,
  `engine.py` swap point). Kept correct fidelity fixes: `frameElement=null`, XHR-proto native-mark,
  `installWebRTC` (confirmed-exercised stub).

## The in-repo live test (brought over from poc-infra-pa this session)
`examples/detran_pa.py` — self-contained. Success bar:
```bash
cd ~/Projects/crawlerkit
CRAWLERKIT_TURNSTILE_ENGINE=node .venv/bin/python examples/detran_pa.py --data plate=SZT3I75 renavam=01433191358
# → "consulted OK — status=200 …"   (today: "CAPTCHA FAIL: …" until the engine mints a valid token)
```
(`poc-infra-pa/crawlers/detran_pa/crawler.py` still exists there too; this repo copy is the durable one. The
contracts-dependent parser was left behind — the consult itself is the success signal.)

## Definition of done
`examples/detran_pa.py` prints `consulted OK — status=200 …` (no `CAPTCHA FAIL`) via
`CRAWLERKIT_TURNSTILE_ENGINE=node`, browserless + independent, on a fresh challenge. 61 tests still green.
