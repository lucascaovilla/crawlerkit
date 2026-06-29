# Browserless Cloudflare Turnstile solver — clean-session handoff

> **You are a fresh Claude Code session.** This document is your complete brief: solve Cloudflare
> Turnstile **with no browser at runtime**, then prove it through the `poc-infra-pa` `DetranPA` crawler.
> A previous session built the whole engine and got the challenge VM to run end-to-end, but hit one
> wall (`unsupported_browser`). Your job is to break that wall and ship a production-robust solve.
> Read this together with `TURNSTILE_HANDOFF.md` (the deep technical state).

---

## 1. Goal & hard constraints

- **Goal:** `crawlerkit`'s `TurnstileSolver.solve()` returns a real, server-valid `cf-turnstile-response`
  token for the **managed / non-interactive (passive)** Turnstile on the Detran-PA infraction form, by
  running the challenge's own JS in an embedded JS engine — **no real browser in the solve path**.
- **Hard constraint (non-negotiable):** NO browser/CDP/Playwright/Selenium **in the runtime solver**.
  Selenium is allowed **only as an offline ground-truth/instrumentation tool** to observe a real
  browser solving the challenge and capture reference data — never shipped, never in `solve()`.
- **Success bar (what "done" means):** `poc-infra-pa` CLI consults without a captcha error:
  `python -m crawlers.detran_pa.crawler --data plate=SZT3I75 renavam=01433191358` → prints
  `consulted OK — status=200 …` (a valid token was minted, POSTed, infractions page returned).
- **Robustness bar:** production-robust — must survive Cloudflare's periodic VM rotations. Keep all
  rotation-prone logic isolated in `crawlerkit/core/captcha/_turnstile/`; the public surface
  (`TurnstileSolver.solve`, `turnstile_hint`, the registry entry) stays frozen.

## 2. Orient first (do this before anything else)

- Repos: `~/Projects/crawlerkit` (the library, package `crawlerkit.core`) and `~/Projects/poc-infra-pa`
  (the consumer/integration test; depends on crawlerkit-core editable).
- **Read `~/Projects/crawlerkit/TURNSTILE_HANDOFF.md` end-to-end** — it is the deep technical state
  (exact handshake, every gap already fixed, debug aids). Also read the two memory files referenced
  there (`detran-pa-turnstile-target`, `crawlerkit-sandbox-env`).
- Use the venv: **`~/Projects/crawlerkit/.venv/bin/python` (CPython 3.11.15)**. pythonmonkey segfaults
  on 3.14, so the venv is pinned to 3.11 via `uv` (`uv` lives at `~/.local/bin`). `requires-python` is
  capped `>=3.11,<3.14` in `pyproject.toml`.
- Sanity: `cd ~/Projects/crawlerkit && .venv/bin/python -m pytest` (expect **61 passed**); then
  `.venv/bin/python scripts/harvest_turnstile.py` to watch the live engine reach the reject.

## 3. What already exists (DO NOT rebuild — reuse/extend)

`crawlerkit/core/captcha/_turnstile/` (≈1100 lines, all working up to the reject):
- `engine.py` — boots pythonmonkey, installs env+bridge+frame, fetches+evals api.js, drives the iframe
  load, pumps the asyncio loop, returns `__token`. `run_challenge(page_url, fingerprint, widget,
  transport, timeout)`.
- `env.py` — faked browser globals (window/document/navigator/screen/crypto/canvas/WebGL/storage/…),
  a **permissive `trapVoid`** (misses → chainable stub; empty-iterable; tree-walk-terminating), a real
  bidirectional **postMessage bus** (`__installBus`/`__deliverMessage`, `isTrusted:true`), an id
  registry, TextEncoder/Blob/structuredClone/etc. polyfills, native-`toString` spoof. Debug ring
  buffer `__getLog` (gated by `globalThis.__rec`), `__undef` miss log.
- `bridge.py` — `fetch`/`XMLHttpRequest`/`sendBeacon` shims routed through the crawler `Transport`
  (curl_cffi) so challenge requests share JA3/HTTP2/proxy/cookies. Captures the request sequence.
- `frame.py` — fetches the cross-origin challenge iframe and runs its ~240KB VM with
  `window/self/parent/top/document/location/globalThis` shadowed to the iframe's window (the iframe
  element's `contentWindow`, so api.js's `e.source===iframe.contentWindow` check passes).
- `widget.py`, `fingerprint.py` — widget parse + deterministic profile-consistent fingerprint.
- Public surface: `turnstile.py` (`TurnstileSolver`, `turnstile_hint`) + registry in `base.py`.
- Tools: `scripts/harvest_turnstile.py` (live engine instrumentation), `scripts/capture_turnstile.py`
  (form capture). `poc-infra-pa/crawlers/detran_pa/crawler.py` already calls `solve_captcha(...,
  hint=turnstile_hint(...))` and POSTs `cf-turnstile-response`, and has the CLI entrypoint.

**Verified working:** prime → form → api.js → implicit render → **full api.js↔VM postMessage handshake**
(`init → requestExtraParams → extraParams → translationInit`, `meow`/`food` heartbeat) → **VM runs to
completion, zero crashes**.

## 4. The one blocker

The VM posts `{"event":"reject","reason":"unsupported_browser"}` every run, then never issues its
challenge XHR → no token → `CaptchaTimeoutError` (clean). **Ruled out** (none changed the verdict): UA
swap to Firefox, `navigator.userAgentData` brands + `getHighEntropyValues`, `window.chrome`, real
`canvas.getContext('webgl')`, native-`toString` spoof, `Symbol.toStringTag='Window'`. The verdict is
**computed from accumulated environment checks and posted from an async callback** — the access log and
the JS stack don't reach the deciding check. Pinpointing it requires **ground truth + deobfuscation**,
not more guessing. Likely culprits per the evidence: engine behavioural fingerprint (SpiderMonkey vs
V8), or un-faked capabilities (real canvas/WebGL pixel readback, timing, a required API).

---

## 5. Methodology to crack it (the core of this task)

### Phase A — Capture ground truth with a real browser (offline tool, not shipped)
Build `crawlerkit/tools/selenium_capture/` (dev-only; gitignore the artifacts):
1. `pip install selenium` (+ `undetected-chromedriver` if plain Selenium Chrome is itself flagged —
   the passive widget is lenient, but verify the real browser actually **passes**). Drive a real Chrome
   to the form (`https://sistemas-renavam.detran.pa.gov.br/sistransito/detran-web/servicos/infracao/indexConsultaInfracao.jsf`,
   after priming `https://www.detran.pa.gov.br/`), let Turnstile auto-solve, and confirm a real
   `cf-turnstile-response` lands in the hidden input.
2. Capture, into a fixtures dir, from the **passing** real browser:
   - **Full network trace** via CDP (`Network.*`) — every `…/cdn-cgi/challenge-platform/…` request +
     **response body**, headers, order, timing, and the final token. (The VM's challenge XHR/payload is
     the thing our env never reaches — this shows exactly what a pass looks like.)
   - **The exact VM source** the real browser executed (the iframe HTML + its inline ~240KB script).
   - **A full environment dump** via an injected JS probe run **in the real page + in the iframe**:
     `navigator` (all enumerable + UA-CH), `screen`, `window` dims, `canvas.toDataURL()` for a known
     draw, WebGL `getParameter` for the full constant set + `getSupportedExtensions`, `Intl`,
     `performance` shape, `Function.prototype.toString` of key natives, `Object.prototype.toString.call`
     of host objects, plugin/mimeType arrays, `crypto.subtle` presence, timing/`performance.now`
     resolution, error/stack formats. This is the **target the browserless env must match**.
3. Save everything under `tests/fixtures/turnstile/realbrowser/` (gitignored) as the diff baseline.

### Phase B — Deobfuscate the VM + find the `unsupported_browser` gate
- Deobfuscate the captured ~240KB inline VM (it's obfuscator.io-style: string-array + encrypted-opcode
  interpreter, per-load randomized names). Use AST tools/references (allowed): e.g. `webcrack`,
  `synchrony`/`deobfuscator.io`, `restringer`, Babel — plus public Turnstile reverse-engineering
  writeups. Goal: locate where `reason` is set to the `"unsupported_browser"` string-table entry and
  walk back the condition(s) that select it.
- In parallel, **run the same env-probe (Phase A) inside our browserless engine** and **diff** against
  the real-browser dump. Every divergence is a candidate gate. Cross-reference the diff with the
  deobfuscated checks to identify the exact failing one(s).
- Use the existing debug aids: set `globalThis.__rec=true` (records every trap read into `__getLog`),
  `__busErrs` (swallowed handler throws with name+message+stack), and the `probe3.py`-style loop
  documented in `TURNSTILE_HANDOFF.md` (boot env+bridge+frame, eval api.js, drive `frame.load_frame`,
  dump events/deliveries/getLog/crash-offsets). Wrap the VM's `setTimeout` callbacks to attribute async
  throws to their VM offsets, and read the captured VM at those offsets.

### Phase C — Engine decision (evidence-driven; V8 switch is approved)
- If the diff/deobfuscation shows the gate is **engine-behavioral** (V8-specific output that SpiderMonkey
  can't match — `Function.prototype.toString` of natives, `Error.stack` format, number/Intl/sort quirks,
  `%`-style internals), **switch to a V8 engine** — still browserless. Concrete options, in order of
  preference for a **production-robust** build:
  1. **Node.js sidecar (recommended):** a Node subprocess (real V8 + real event loop) runs api.js + the
     VM in a `vm` context over a **jsdom** DOM (battle-tested, browserless), with `fetch`/`XHR` proxied
     back to Python's curl_cffi `Transport` over stdio/socket so JA3/cookies still match. Far more
     faithful + maintainable than hand-stubbing; jsdom + V8 closes most engine/DOM tells at once.
  2. STPyV8 (V8 with in-process Python interop) or a `deno_core`/Rust module — only if avoiding a Node
     dependency matters more than jsdom's completeness.
- If the gate is **not** engine-behavioral (just missing/incorrect env values), fix it in place in the
  current pythonmonkey env using the real-browser values from Phase A — cheaper, keeps the working code.
- Decide from evidence; **document the finding** in `TURNSTILE_HANDOFF.md` either way. Keep the engine
  behind `engine.py`/`frame.py` so the choice is swappable and the public API is untouched.

### Phase D — Make the VM accept, then mint the token
- Apply the fixes (faithful env values and/or new engine) until the VM stops rejecting, **issues its
  challenge XHR** (you'll see the `…/cdn-cgi/challenge-platform/…` POST in `bridge.requests` / the
  transport log), and posts the completion message api.js extracts → widget callback
  (`data-callback="__turnstileToken"`) → `globalThis.__token`. `_read_token` already trusts ONLY
  `__token` (the `__frameToken` heuristic false-matches the `nextRcV` nonce — never trust it).
- Validate the token is **server-valid** by POSTing it through the real crawler (§7), not just by shape.

### Phase E — Harden for rotation (production-robust)
- Isolate every value that came from ground truth (fingerprint constants, any per-version env data) so a
  VM rotation is a small, documented patch. Re-runnable: keep the Phase-A selenium capture + the Phase-B
  diff as repeatable tooling so refreshing against a new VM is mechanical.
- Add retry/rotation hooks: map Cloudflare blocks → `BlockedError`/`TransientError`, interactive
  escalation → `InteractiveChallengeError`, timeout → `CaptchaTimeoutError` (mostly wired already).
- Add regression tests (offline, deterministic) that lock the env-probe output to the real-browser
  baseline so future env edits can't silently regress a passing check.

## 6. poc-infra-pa integration (already wired — just keep it working)

- `poc-infra-pa/crawlers/detran_pa/crawler.py`: `DetranPA.flow()` does prime → form GET →
  `solve_captcha(page.text, hint=turnstile_hint(page_url=FORM_URL, html=page.text))` → JSF POST with
  `cf-turnstile-response`. A CLI `main()` is already added (`--data k=v…`, maps `plate`→`placa`, exits 1
  + `CAPTCHA FAIL` on any `CaptchaError`, else `consulted OK`).
- Dependency: `requirements.txt` has `-e /home/caovilla/Projects/crawlerkit`; `pythonmonkey` is a hard
  dep of crawlerkit-core so it installs transitively. If you switch engines (Phase C), update
  crawlerkit-core deps accordingly (e.g. add a Node bootstrap step) and keep the POC install working.
- The POC must run on a **Python 3.11** environment that has crawlerkit-core installed (its own 3.11
  venv via `uv venv --python 3.11 && uv pip install -r requirements.txt`, or reuse crawlerkit's venv via
  `PYTHONPATH`). No RabbitMQ/Docker needed for the test.

## 7. Verification / testing instructions

1. **Offline suite (crawlerkit):** `cd ~/Projects/crawlerkit && .venv/bin/python -m pytest` → 61+ passed.
   Keep green after every change; add the Phase-E env-probe regression test.
2. **Engine iteration:** `.venv/bin/python scripts/harvest_turnstile.py` → must progress from
   `reject/unsupported_browser` to `*** TOKEN MINTED ***` with the challenge-platform XHR visible in the
   transport log and a token via `__token` (not `__frameToken`).
3. **Real-browser ground truth (offline tool):** the Phase-A selenium harness must show a real Chrome
   getting a valid token, and dump the network + env baseline used for the diff.
4. **Live end-to-end (the success bar):** from a 3.11 env with crawlerkit-core installed:
   ```
   cd ~/Projects/poc-infra-pa
   python -m crawlers.detran_pa.crawler --data plate=SZT3I75 renavam=01433191358
   ```
   **Pass:** `consulted OK — status=200 …` and **no** `CAPTCHA FAIL`. Cross-check the solver in isolation
   with the handoff's one-liner (`PYTHONPATH=…/poc-infra-pa …/crawlerkit/.venv/bin/python -m
   crawlers.detran_pa.crawler …`) to separate engine vs crawler issues.
5. Do **not** commit unless asked. Update `TURNSTILE_HANDOFF.md` + the memory files as you learn.

## 8. Key facts & references

- **Target:** form URL above; **prime** `https://www.detran.pa.gov.br/` first (sets `veio_da_home`,
  else the form 302s); sitekey **`0x4AAAAAADpvM_lNoEdBJ3cR`**, **passive/non-interactive** (a real
  browser solves it with **zero user interaction** — so a faithful browserless env *can* pass; that's
  the proof it's doable). Real plate for a non-empty consult: **placa `SZT3I75`, renavam `01433191358`**.
- **Iframe (where the token is minted):** `…/cdn-cgi/challenge-platform/h/b/turnstile/f/ov2/av0/rch<i>/
  <sitekey>/auto/fbE/new/normal?lang=auto` → ~260KB HTML, one ~240KB inline VM. Parent api.js makes NO
  XHR; the iframe VM does, then `postMessage`s the token to the parent (validated:
  `e.isTrusted && e.source===iframe.contentWindow && e.origin∈cloudflare && e.data.{source,event,widgetId}`).
- **postMessage protocol (already emulated):** VM→parent `init`/`requestExtraParams`/`food`; parent→VM
  `init`/`meow`/`extraParams`. The VM checks `e.source===window.parent` for parent messages.
- Allowed references: Turnstile/Cloudflare-challenge RE writeups, open-source **browserless** solver
  source, JS deobfuscators (webcrack/synchrony/restringer/Babel). Study for protocol + required env;
  never introduce a browser into the runtime.

## 9. Risks / honest notes

- Cracking `unsupported_browser` may reveal **further gates** (it's production anti-bot). Budget for
  multiple env-diff iterations and at least one VM-rotation refresh cycle.
- The Node+jsdom+V8 sidecar is the highest-probability path to a robust pass, but it's an architectural
  change — justify it with the engine-behavioral evidence from Phase B before committing.
- Selenium-driven Chrome may itself be Turnstile-flagged; if the real browser can't get a token, switch
  to `undetected-chromedriver` for the **capture tool only**.
