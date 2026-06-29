# Turnstile spike (throwaway — phase 0 gate)

Not shipped. Not imported by the package. This exists to prove **one** thing before any Rust
work: that the real Cloudflare challenge JS will run in a V8 engine with a hand-stubbed env and
emit a `cf-turnstile-response` token. If it can't, the whole browserless approach is reconsidered.

## Run it (needs the package deps + py_mini_racer + a real capture)

```bash
pip install py_mini_racer
python spike/turnstile_spike.py --page-url https://<target> --html captured_page.html
```

Run it in an environment that has the residential proxy identity crawlerkit uses, so the
challenge's network calls leave with the same fingerprint as the page fetch.

## What to keep

The **learnings**, not the code:

- the list of `__undef` (undefined property accesses) the run prints — this drives exactly what
  the production `_turnstile/env.py` must stub, filled from observation, never guessed.
- the request sequence the challenge makes (endpoints + order) — this drives `_turnstile/bridge.py`.
- whether a token actually comes out (the gate).

Once the gate is proven, port the proven env + bridge onto the deno_core/V8 module
(`crawlerkit._turnstile_engine`) and delete this directory.
```
