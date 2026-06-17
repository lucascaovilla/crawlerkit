"""Optional cookie-jar persistence across crawls.

curl_cffi keeps cookies within a Session automatically (a GET that sets JSESSIONID is reused by the
following POST — no action needed). These helpers add OPTIONAL cross-run warming: dump the jar to
disk after a crawl and reload it before the next. Best-effort; never raises on a malformed file.
"""

import json
from http.cookiejar import Cookie


def save_cookies(transport, path: str) -> int:
    """Dump the transport's current cookies to `path` (JSON). Returns the count saved."""
    data = []
    try:
        for c in transport._session.cookies.jar:
            data.append({
                "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                "secure": c.secure, "expires": c.expires,
            })
    except Exception as e:  # noqa: BLE001 — cookie internals vary across curl_cffi versions; best-effort
        transport._log.warning("cookies_save_failed", error=str(e))
        return 0
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    return len(data)


def load_cookies(transport, path: str) -> int:
    """Load cookies from `path` into the transport's session. Returns the count loaded (0 if absent)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return 0
    except ValueError as e:
        transport._log.warning("cookies_load_corrupt", path=path, error=str(e))
        return 0
    n = 0
    for c in data:
        try:
            # curl_cffi's own Cookies.set() doesn't accept `expires` — build the underlying
            # http.cookiejar.Cookie directly (same recipe curl_cffi uses internally) so a
            # reloaded cookie's secure/expiry aren't silently dropped.
            domain = c.get("domain") or ""
            cpath = c.get("path") or "/"
            cookie = Cookie(
                version=0, name=c["name"], value=c["value"], port=None, port_specified=False,
                domain=domain, domain_specified=bool(domain), domain_initial_dot=domain.startswith("."),
                path=cpath, path_specified=bool(cpath), secure=c.get("secure", False),
                expires=c.get("expires"), discard=c.get("expires") is None,
                comment=None, comment_url=None, rest={"HttpOnly": None}, rfc2109=False,
            )
            transport._session.cookies.jar.set_cookie(cookie)
            n += 1
        except Exception as e:  # noqa: BLE001 — skip one malformed entry, keep loading the rest
            transport._log.warning("cookie_load_skipped", name=c.get("name"), error=str(e))
            continue
    return n
