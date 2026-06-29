"""Coherent browser identity via browserforge + curl_cffi impersonate.

curl_cffi's `impersonate` target owns the TLS/JA3 + HTTP2 fingerprint and MUST stay coherent with
the User-Agent — a UA that disagrees with the JA3 is worse than no spoofing. browserforge supplies a
realistic header SET + ORDER + locale; we pick the nearest supported impersonate target and SNAP the
UA/sec-ch-ua Chrome version to it, so UA <-> JA3 never drift. Each `generate()` randomizes (rotation),
and profiles are rotated together with the proxy.
"""

import re
from dataclasses import dataclass, field

# curl_cffi impersonate targets we support, ascending by Chrome major. Recent majors (136–146, shipped by
# curl_cffi >= 0.15) matter: Turnstile downgrades trust when the JA3/UA look like an older Chrome than the
# live fleet — real passing browsers here are Chrome 144, so stay close to it.
_IMPERSONATE_BY_MAJOR: list[tuple[int, str]] = [
    (120, "chrome120"),
    (124, "chrome124"),
    (131, "chrome131"),
    (133, "chrome133a"),
    (136, "chrome136"),
    (142, "chrome142"),
    (145, "chrome145"),
    (146, "chrome146"),
]
DEFAULT_IMPERSONATE = "chrome142"


def _impersonate_for_major(major: int, targets: list[tuple[int, str]] | None = None) -> tuple[str, int]:
    """Nearest supported (target, target_major) with target_major <= major; else the lowest."""
    targets = targets or _IMPERSONATE_BY_MAJOR
    chosen_major, chosen_target = targets[0]
    for mj, target in targets:
        if mj <= major:
            chosen_major, chosen_target = mj, target
    return chosen_target, chosen_major


def available_chrome_targets() -> list[tuple[int, str]] | None:
    """Best-effort: every desktop `chromeNNN` impersonate target the installed curl_cffi ships,
    parsed from its `BrowserType` enum. Returns None if curl_cffi doesn't expose one (older/newer
    versions may differ) — callers should fall back to the curated default in that case.

    Not used as the default automatically: curl_cffi may list targets (e.g. very recent Chrome
    majors) before their fingerprint implementation is well-verified. Pass the result to
    `ProfileGenerator(impersonate_targets=...)` to opt in once you've verified a target works for
    your traffic."""
    try:
        from curl_cffi.requests import BrowserType
    except Exception:  # noqa: BLE001 — curl_cffi internals may differ across versions
        return None
    targets = []
    for b in BrowserType:
        name = b.value if hasattr(b, "value") else str(b)
        m = re.fullmatch(r"chrome(\d+)", name)
        if m:
            targets.append((int(m.group(1)), name))
    return sorted(targets) or None


def _snap_version(headers: dict, gen_major: int, target_major: int) -> None:
    """Rewrite the UA + sec-ch-ua Chrome version from gen_major to target_major (in place)."""
    if gen_major == target_major:
        return
    if ua := headers.get("User-Agent"):
        headers["User-Agent"] = re.sub(r"Chrome/\d+", f"Chrome/{target_major}", ua)
    if sch := headers.get("sec-ch-ua"):  # only the Chrome/Chromium brands carry the major
        headers["sec-ch-ua"] = sch.replace(f'v="{gen_major}"', f'v="{target_major}"')


@dataclass(frozen=True)
class Profile:
    impersonate: str
    _headers: dict = field(default_factory=dict)

    @property
    def user_agent(self) -> str:
        return self._headers.get("User-Agent", "")

    def headers(self) -> dict:
        return dict(self._headers)


def _fallback_profile() -> Profile:
    """Static coherent profile when browserforge is unavailable."""
    return Profile(
        impersonate=DEFAULT_IMPERSONATE,
        _headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/142.0.0.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate, br, zstd",  # curl_cffi decodes br/zstd natively
        },
    )


class ProfileGenerator:
    """Generate coherent Profiles via browserforge, snapped to a curl_cffi impersonate target."""

    def __init__(self, *, browser="chrome", os=("linux",), device="desktop", locale="pt-BR",
                 impersonate_targets: list[tuple[int, str]] | None = None):
        self._hg = None
        # ascending-by-major list of (major, impersonate_target); defaults to the curated built-in
        # set. Pass your own (or `available_chrome_targets()`) to add a target without forking.
        self._targets = impersonate_targets or _IMPERSONATE_BY_MAJOR
        try:
            from browserforge.headers import HeaderGenerator

            self._hg = HeaderGenerator(browser=browser, os=os, device=device, locale=locale)
        except Exception:  # noqa: BLE001 — browserforge optional; fall back to a static profile
            self._hg = None

    def generate(self) -> Profile:
        if self._hg is None:
            return _fallback_profile()
        try:
            h = dict(self._hg.generate())
        except Exception:  # noqa: BLE001
            return _fallback_profile()
        # browserforge's Bayesian generator emits INCOHERENT Sec-Fetch-* values (observed e.g.
        # `Sec-Fetch-Site: ?1`, `Sec-Fetch-User: document` — the four values rotated off their keys).
        # Those are request-context headers a real browser sets per fetch anyway; a wrong static value
        # is a glaring bot tell (Cloudflare validates them). Drop them here; the caller (e.g. the
        # Turnstile sidecar) sets correct per-request Sec-Fetch, and a bare doc GET simply omits them.
        for k in list(h):
            if k.lower().startswith("sec-fetch-"):
                del h[k]
        # browserforge emits a non-Chrome Accept-Language (e.g. `pt-BR;q=1.0` — Chrome omits q on the
        # primary tag and includes fallbacks). Normalize to a real pt-BR Chrome value.
        for k in list(h):
            if k.lower() == "accept-language":
                h[k] = "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7"
        m = re.search(r"Chrome/(\d+)", h.get("User-Agent", ""))
        gen_major = int(m.group(1)) if m else 131
        target, target_major = _impersonate_for_major(gen_major, self._targets)
        _snap_version(h, gen_major, target_major)
        return Profile(impersonate=target, _headers=h)


_DEFAULT_GEN: ProfileGenerator | None = None


def pick(pool=None, index: int = 0) -> Profile:
    """Return a freshly generated, coherent Profile (browserforge-randomized = rotation)."""
    global _DEFAULT_GEN
    if _DEFAULT_GEN is None:
        _DEFAULT_GEN = ProfileGenerator()
    return _DEFAULT_GEN.generate()
