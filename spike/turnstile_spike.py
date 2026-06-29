#!/usr/bin/env python3
"""THROWAWAY phase-0 spike — the go/no-go gate for the Turnstile solver.

This is NOT shipped code and NOT imported by the package. Its only job is to answer the one
question that decides whether the whole effort is viable, independent of the Rust wrapper:

    can we feed the real challenge JS into a V8 engine with a hand-stubbed env and get it to
    emit a `cf-turnstile-response` token?

Engine: py_mini_racer (V8) — same engine family as the production deno_core module, so there is
no spike->prod engine seam. Run it in an environment that HAS the package deps + py_mini_racer +
(ideally) the residential proxy identity crawlerkit uses, and a real captured challenge.

    pip install py_mini_racer
    python spike/turnstile_spike.py --page-url https://<target> --html captured_page.html

What it does:
  1. derive the profile-consistent fingerprint (reuses the real production deriver).
  2. install a MINIMAL hand-stubbed browser env (window/document/navigator/screen) seeded from it.
  3. instrument every undefined property access and log it (this list drives the real env module).
  4. run api.js + the challenge bundle; route fetch/XHR to a Python callback (here: stdlib urllib,
     in prod: the crawlerkit transport) so you can watch the request sequence.
  5. capture the token handed to the Turnstile callback, or report why it didn't appear.

Keep the LEARNINGS (undefined-access list, request sequence, what env the challenge needs),
throw the CODE away. The production env/bridge are filled from what this prints, never guessed.
"""

import argparse
import json
import sys

# reuse the real deriver so the spike env == the env production will build
from crawlerkit.core.captcha._turnstile.fingerprint import derive
from crawlerkit.core.identity import pick

API_JS = "https://challenges.cloudflare.com/turnstile/v0/api.js"

# Minimal env bootstrap. Proxies log every undefined access so we learn what to stub next.
# This is deliberately incomplete — fill ONLY what the run reports as touched.
_ENV_BOOTSTRAP = r"""
globalThis.__undef = [];
function trap(name, base) {
  return new Proxy(base || {}, {
    get(t, p) {
      if (p in t) return t[p];
      if (typeof p === 'symbol') return undefined;
      globalThis.__undef.push(name + '.' + String(p));
      return undefined;
    }
  });
}
globalThis.navigator = trap('navigator', {
  userAgent: __FP.user_agent, platform: __FP.platform, language: __FP.language,
  languages: __FP.languages, hardwareConcurrency: __FP.hardware_concurrency,
  deviceMemory: __FP.device_memory, webdriver: false,
});
globalThis.screen = trap('screen', {
  width: __FP.screen_width, height: __FP.screen_height,
  availWidth: __FP.avail_width, availHeight: __FP.avail_height,
  colorDepth: __FP.color_depth, pixelDepth: __FP.color_depth,
});
globalThis.location = trap('location', { href: __PAGE_URL, origin: __ORIGIN, protocol: 'https:' });
globalThis.document = trap('document', {
  cookie: '', createElement: () => trap('element', {}), getElementsByTagName: () => [],
  querySelector: () => null, addEventListener: () => {},
});
globalThis.window = globalThis;
globalThis.__token = null;
globalThis.turnstileCallback = function(t) { globalThis.__token = t; };
"""


def run(page_url: str, html: str) -> int:
    try:
        from py_mini_racer import MiniRacer
    except ImportError:
        print("FATAL: pip install py_mini_racer (V8) to run the spike", file=sys.stderr)
        return 2

    fp = derive(pick())
    origin = "/".join(page_url.split("/")[:3])
    fp_json = json.dumps({
        "user_agent": fp.user_agent, "platform": fp.platform, "language": fp.language,
        "languages": fp.languages, "hardware_concurrency": fp.hardware_concurrency,
        "device_memory": fp.device_memory, "screen_width": fp.screen_width,
        "screen_height": fp.screen_height, "avail_width": fp.avail_width,
        "avail_height": fp.avail_height, "color_depth": fp.color_depth,
    })

    ctx = MiniRacer()
    ctx.eval(f"globalThis.__FP = {fp_json};")
    ctx.eval(f"globalThis.__PAGE_URL = {json.dumps(page_url)};")
    ctx.eval(f"globalThis.__ORIGIN = {json.dumps(origin)};")
    ctx.eval(_ENV_BOOTSTRAP)

    print(f"[spike] fingerprint: {fp.platform} {fp.language} {fp.screen_width}x{fp.screen_height}")
    print(f"[spike] page_url={page_url} origin={origin}")
    print("[spike] TODO wire fetch/XHR -> transport, eval api.js + challenge bundle, pump timers.")
    print("[spike] HTML captured:", len(html), "bytes")

    # After eval-ing the real challenge JS here, harvest:
    undef = ctx.eval("JSON.stringify(globalThis.__undef)")
    token = ctx.eval("globalThis.__token")
    print("[spike] undefined accesses (fill these in the env module next):")
    print("       ", undef)
    if token:
        print(f"[spike] GATE PASS — token captured: {token[:24]}...")
        return 0
    print("[spike] no token yet — wire the challenge JS run, then re-check. (gate not yet proven)")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Throwaway Turnstile phase-0 spike (gate).")
    ap.add_argument("--page-url", required=True, help="URL of the page embedding the widget")
    ap.add_argument("--html", required=True, help="path to captured page HTML")
    args = ap.parse_args()
    with open(args.html, encoding="utf-8", errors="replace") as f:
        html = f.read()
    return run(args.page_url, html)


if __name__ == "__main__":
    raise SystemExit(main())
