"""mCaptcha proof-of-work solver (compute backend, no third-party service).

Ported from the Detran POC. Byte layout confirmed empirically against a captured
(salt, string, nonce) -> result oracle (guarded by self_test). No sitekey secret needed.
"""

import hashlib
import re
import struct
import time

from .base import CaptchaServiceError, CaptchaTimeoutError, CaptchaUnsolvedError, Challenge, Solved

U128_MAX = (1 << 128) - 1

# Captured oracle (a real, self-consistent challenge) — proves the byte layout.
_ORACLE_SALT = "8c7b6a5d4e3f2a1b0c9d8e7f6a5b4c3d2e1f0a9b8c7d6e5f4a3b2c1d0e9f8a7b"
_ORACLE_STRING = "7KoiRWIqZk3qFy7C8Jt96E9KUSGfdbVL"
_ORACLE_DIFFICULTY = 4_000_000
_ORACLE_NONCE = 3_539_967
_ORACLE_RESULT = 340282365527686933810834880601832247926

# matches the mCaptcha widget iframe URL embedded in a page
_WIDGET_RE = re.compile(r"https?://([\w.-]+)/widget\?sitekey=([\w-]+)")


def mcaptcha_hint(host: str, sitekey: str) -> Challenge:
    """Build a Challenge from a known sitekey when the widget isn't inline in the GET HTML."""
    return Challenge(
        kind="mcaptcha",
        params={"host": host, "sitekey": sitekey, "api_base": f"https://{host}/api/v1/pow"},
    )


def _headers(challenge: Challenge) -> dict:
    host = challenge.params["host"]
    sitekey = challenge.params["sitekey"]
    return {
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Origin": f"https://{host}",
        "Referer": f"https://{host}/widget?sitekey={sitekey}",
    }


class McaptchaPowSolver:
    kind = "mcaptcha"

    # ---- detection ----
    @classmethod
    def detect(cls, text: str):
        m = _WIDGET_RE.search(text or "")
        if not m:
            return None
        return mcaptcha_hint(host=m.group(1), sitekey=m.group(2))

    # ---- proof-of-work math ----
    @staticmethod
    def _prefix(salt: str, string: str):
        h = hashlib.sha256()
        h.update(salt.encode())
        sb = string.encode()
        h.update(struct.pack("<Q", len(sb)))  # bincode fixint LE u64 length prefix
        h.update(sb)
        return h

    @classmethod
    def compute_result(cls, salt: str, string: str, nonce: int) -> int:
        h = cls._prefix(salt, string)
        h.update(str(nonce).encode())  # nonce as decimal ASCII
        return int.from_bytes(h.digest()[:16], "big")  # first 16 bytes, big-endian

    @staticmethod
    def threshold(difficulty: int) -> int:
        return U128_MAX - U128_MAX // difficulty

    @classmethod
    def solve_pow(cls, salt, string, difficulty, max_iters=2_000_000_000, max_seconds=120):
        thr = cls.threshold(difficulty)
        base = cls._prefix(salt, string)
        start = time.perf_counter()
        nonce = 0
        while nonce < max_iters:
            nonce += 1
            h = base.copy()
            h.update(str(nonce).encode())
            if int.from_bytes(h.digest()[:16], "big") >= thr:
                return nonce, str(cls.compute_result(salt, string, nonce)), int(
                    (time.perf_counter() - start) * 1000
                )
            if (nonce & 0x3FFFF) == 0 and (time.perf_counter() - start) > max_seconds:
                raise CaptchaTimeoutError(f"PoW unsolved in {max_seconds}s (difficulty {difficulty})")
        raise CaptchaUnsolvedError("PoW unsolved within iteration cap")

    @classmethod
    def self_test(cls) -> None:
        """Oracle gate: assert the layout reproduces the captured result. Instant."""
        got = cls.compute_result(_ORACLE_SALT, _ORACLE_STRING, _ORACLE_NONCE)
        if got != _ORACLE_RESULT:
            raise AssertionError(f"mCaptcha layout self-test FAILED: {got} != {_ORACLE_RESULT}")

    # ---- solve (config -> pow -> verify -> token) ----
    def solve(self, challenge: Challenge, transport) -> Solved:
        # mCaptcha's actor backend is intermittently flaky ("Actor mailbox error") on both
        # /config and /verify — retry the whole config -> pow -> verify a few times.
        last: Exception | None = None
        for attempt in range(3):
            try:
                return self._solve_once(challenge, transport)
            except CaptchaServiceError as e:
                last = e
                time.sleep(1.0 * (attempt + 1))
        raise last  # type: ignore[misc]

    def _solve_once(self, challenge: Challenge, transport) -> Solved:
        api = challenge.params["api_base"]
        key = challenge.params["sitekey"]
        headers = _headers(challenge)

        cfg = transport.post(f"{api}/config", json={"key": key}, headers=headers).json()
        if not (isinstance(cfg, dict) and "salt" in cfg):
            raise CaptchaServiceError(f"mcaptcha /config returned no challenge: {cfg!r}")

        nonce, result, elapsed_ms = self.solve_pow(
            cfg["salt"], cfg["string"], cfg["difficulty_factor"]
        )
        verify = transport.post(
            f"{api}/verify",
            json={
                "key": key,
                "nonce": nonce,
                "result": result,
                "string": cfg["string"],
                "time": elapsed_ms,
                "worker_type": "wasm",
            },
            headers=headers,
        ).json()
        token = verify.get("token") if isinstance(verify, dict) else None
        if not token:
            raise CaptchaServiceError(f"mcaptcha /verify returned no token: {verify!r}")
        return Solved(token=token, expires_at=None)
