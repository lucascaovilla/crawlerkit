"""Cookie persistence: round-trips secure/expires, and failures are logged, not silent."""

import json

from structlog.testing import capture_logs

from crawlerkit.core import cookies
from crawlerkit.core.identity import pick
from crawlerkit.core.proxy import NullProxyProvider
from crawlerkit.core.transport import Transport


def _transport(enable_logs: bool = False) -> Transport:
    return Transport(pick(), NullProxyProvider().lease(), enable_logs=enable_logs)


def test_round_trip_preserves_secure_and_expires(tmp_path) -> None:
    t1 = _transport()
    t1._session.cookies.set("sid", "abc123", domain="example.com", path="/", secure=True)
    for c in t1._session.cookies.jar:
        c.expires = 9999999999

    path = str(tmp_path / "cookies.json")
    assert cookies.save_cookies(t1, path) == 1

    t2 = _transport()
    assert cookies.load_cookies(t2, path) == 1
    [loaded] = list(t2._session.cookies.jar)
    assert (loaded.name, loaded.value, loaded.domain, loaded.secure, loaded.expires) == (
        "sid", "abc123", "example.com", True, 9999999999,
    )


def test_missing_file_returns_zero_silently() -> None:
    assert cookies.load_cookies(_transport(), "/nonexistent/path.json") == 0


def test_corrupt_file_returns_zero_and_logs_when_enabled(tmp_path) -> None:
    path = tmp_path / "corrupt.json"
    path.write_text("not json{{{")
    t = _transport(enable_logs=True)

    with capture_logs() as logs:
        assert cookies.load_cookies(t, str(path)) == 0
    assert any(e["event"] == "cookies_load_corrupt" for e in logs)


def test_malformed_entry_is_skipped_not_fatal(tmp_path) -> None:
    path = tmp_path / "partial.json"
    path.write_text(json.dumps([{"name": "good", "value": "v", "domain": "x.com"}, {"oops": True}]))
    t = _transport(enable_logs=True)

    with capture_logs() as logs:
        assert cookies.load_cookies(t, str(path)) == 1
    assert any(e["event"] == "cookie_load_skipped" for e in logs)
