"""Derive a stable, profile-consistent browser fingerprint from a `Profile`.

`Profile` (crawlerkit/core/identity.py) is deliberately thin: it carries only the curl_cffi
`impersonate` target and the browserforge header set (UA, Accept-Language, sec-ch-ua...). It does
NOT carry the screen, platform, hardwareConcurrency, timezone, canvas, or WebGL values the
challenge JS probes. Those have to exist, and they have to AGREE with the UA/JA3 already on the
wire — a navigator/UA or fingerprint/TLS contradiction is exactly what managed Turnstile catches.

So we derive them here, deterministically: the seed is the UA + impersonate target, so the same
Profile always yields the same fingerprint (a real browser instance is stable across a session),
and every value is chosen to be consistent with the UA's platform. This module is the single
source the env emulation reads from — it must never invent a value that contradicts the Profile,
and it must never roll a fresh random value per call.
"""

import hashlib
import re
from dataclasses import dataclass, field

# Realistic option pools, indexed deterministically by the per-profile seed.
_SCREENS = [(1920, 1080), (1536, 864), (1366, 768), (2560, 1440), (1440, 900)]
_HARDWARE_CONCURRENCY = [4, 8, 12, 16]
_DEVICE_MEMORY = [4, 8]  # Chrome clamps navigator.deviceMemory to 8

# WebGL vendor/renderer strings keyed by OS family — must match navigator.platform.
_WEBGL_BY_OS = {
    "windows": ("Google Inc. (NVIDIA)",
                "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    "macos": ("Google Inc. (Apple)", "ANGLE (Apple, ANGLE Metal Renderer: Apple M2, Unspecified)"),
    # captured ground truth on the dev/egress box (poc-infra-pa/tools/selenium_capture) — refresh per host
    "linux": ("Google Inc. (Intel)", "ANGLE (Intel, Mesa Intel(R) HD Graphics 620 (KBL GT2), OpenGL ES 3.2)"),
}
# IANA timezone by primary language subtag (best-effort; crawlerkit's locale default is pt-BR).
_TZ_BY_LANG = {"pt": "America/Sao_Paulo", "en": "America/New_York", "es": "Europe/Madrid"}


@dataclass(frozen=True)
class Fingerprint:
    user_agent: str
    platform: str            # navigator.platform, e.g. "Win32" / "Linux x86_64" / "MacIntel"
    os_family: str           # "windows" / "macos" / "linux"
    languages: list[str]     # navigator.languages, e.g. ["pt-BR", "pt", "en"]
    hardware_concurrency: int
    device_memory: int
    screen_width: int
    screen_height: int
    color_depth: int = 24
    pixel_ratio: float = 1.0
    timezone: str = "America/Sao_Paulo"
    webgl_vendor: str = ""
    webgl_renderer: str = ""
    canvas_hash: str = ""    # stable fake toDataURL() hash (hex) for this profile
    headers: dict = field(default_factory=dict)

    @property
    def language(self) -> str:
        return self.languages[0] if self.languages else "en-US"

    @property
    def avail_width(self) -> int:
        return self.screen_width

    @property
    def avail_height(self) -> int:
        # taskbar/dock reserved height by OS (a real, checked signal). Captured linux egress shows
        # availHeight == height (no reserved area), macOS reserves the menu bar, Windows the taskbar.
        return self.screen_height - {"macos": 25, "windows": 40}.get(self.os_family, 0)


def derive(profile) -> Fingerprint:
    """Build the deterministic, profile-consistent fingerprint for ``profile``."""
    headers = profile.headers()
    ua = profile.user_agent or headers.get("User-Agent", "")
    seed = hashlib.sha256(f"{ua}|{profile.impersonate}".encode()).digest()

    os_family = _os_family(ua, headers.get("sec-ch-ua-platform"))
    platform = {"windows": "Win32", "macos": "MacIntel", "linux": "Linux x86_64"}[os_family]
    languages = _languages(headers.get("Accept-Language", ""))
    width, height = _pick(_SCREENS, seed, 0)
    vendor, renderer = _WEBGL_BY_OS[os_family]

    return Fingerprint(
        user_agent=ua,
        platform=platform,
        os_family=os_family,
        languages=languages,
        hardware_concurrency=_pick(_HARDWARE_CONCURRENCY, seed, 8),
        device_memory=_pick(_DEVICE_MEMORY, seed, 9),
        screen_width=width,
        screen_height=height,
        timezone=_TZ_BY_LANG.get(languages[0].split("-")[0] if languages else "en", "America/Sao_Paulo"),
        webgl_vendor=vendor,
        webgl_renderer=renderer,
        canvas_hash=hashlib.sha256(seed + b"canvas").hexdigest(),
        headers=headers,
    )


def _os_family(ua: str, ch_platform: str | None) -> str:
    if ch_platform:
        p = ch_platform.strip('"').lower()
        if "win" in p:
            return "windows"
        if "mac" in p:
            return "macos"
        if "linux" in p:
            return "linux"
    if "Windows" in ua:
        return "windows"
    if "Macintosh" in ua or "Mac OS X" in ua:
        return "macos"
    return "linux"


def _languages(accept_language: str) -> list[str]:
    """"pt-BR,pt;q=0.9,en;q=0.8" -> ["pt-BR", "pt", "en"] (q-stripped, order preserved, deduped)."""
    out: list[str] = []
    for part in accept_language.split(","):
        tag = part.split(";")[0].strip()
        if tag and tag not in out:
            out.append(tag)
    return out or ["en-US"]


def _pick(pool: list, seed: bytes, offset: int):
    """Deterministically choose from ``pool`` using one byte of the seed at ``offset``."""
    idx = seed[offset % len(seed)] % len(pool)
    return pool[idx]
