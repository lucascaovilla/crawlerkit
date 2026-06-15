"""Optional cookie-jar persistence across crawls.

curl_cffi keeps cookies within a Session automatically (a GET that sets JSESSIONID is reused by the
following POST — no action needed). These helpers add OPTIONAL cross-run warming: dump the jar to
disk after a crawl and reload it before the next. Best-effort; never raises on a malformed file.
"""

import json


def save_cookies(transport, path: str) -> int:
    """Dump the transport's current cookies to `path` (JSON). Returns the count saved."""
    data = []
    try:
        for c in transport._session.cookies.jar:
            data.append({"name": c.name, "value": c.value, "domain": c.domain, "path": c.path})
    except Exception:  # noqa: BLE001 — cookie internals vary; best-effort
        return 0
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return len(data)


def load_cookies(transport, path: str) -> int:
    """Load cookies from `path` into the transport's session. Returns the count loaded (0 if absent)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        return 0
    n = 0
    for c in data:
        try:
            transport._session.cookies.set(
                c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/")
            )
            n += 1
        except Exception:  # noqa: BLE001
            continue
    return n
