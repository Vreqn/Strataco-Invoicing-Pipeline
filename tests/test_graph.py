"""Unit tests for the 0.3.0 graph.py changes.

Pure URL/encoding tests — no HTTP calls. Mocks the `_get_json` shim so
`_get_paged_value` can be exercised without touching the network.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "test+user@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

from tools._lib import graph as graph_mod


def test_url_quoting() -> None:
    cases = [
        ("test+user@example.com", "test%2Buser%40example.com"),
        ("AAMkAGE2/abc=", "AAMkAGE2%2Fabc%3D"),
        ("hello world", "hello%20world"),
        ("Inbox/Sub-folder", "Inbox%2FSub-folder"),
    ]
    for raw, expected in cases:
        got = graph_mod._q(raw)
        assert got == expected, f"[quote {raw!r}] expected {expected!r}, got {got!r}"


def test_odata_str_escape() -> None:
    cases = [
        ("processed_emails", "processed_emails"),
        ("O'Brien", "O''Brien"),
        ("can't", "can''t"),
    ]
    for raw, expected in cases:
        got = graph_mod._odata_str(raw)
        assert got == expected, f"[odata {raw!r}] expected {expected!r}, got {got!r}"


def test_paged_value_follows_nextlink() -> None:
    """Mock _get_json and prove `_get_paged_value` chains through nextLink."""
    calls: list[tuple[str, dict | None]] = []
    responses = [
        {"value": [{"id": "a"}, {"id": "b"}],
         "@odata.nextLink": "https://example.com/page2"},
        {"value": [{"id": "c"}],
         "@odata.nextLink": "https://example.com/page3"},
        {"value": [{"id": "d"}, {"id": "e"}]},
    ]
    original = graph_mod._get_json

    def fake_get_json(url, params=None):
        calls.append((url, params))
        return responses[len(calls) - 1]

    graph_mod._get_json = fake_get_json  # type: ignore[assignment]
    try:
        items = graph_mod._get_paged_value(
            "https://example.com/start", params={"$top": "2"}
        )
    finally:
        graph_mod._get_json = original  # type: ignore[assignment]

    ids = [it["id"] for it in items]
    assert ids == ["a", "b", "c", "d", "e"], (
        f"[paged value] expected ['a','b','c','d','e'], got {ids}"
    )
    assert len(calls) == 3, f"[paged value] expected 3 HTTP calls, got {len(calls)}"
    assert calls[0][1] == {"$top": "2"}, f"[paged value] first call params {calls[0][1]!r}"
    assert all(c[1] is None for c in calls[1:]), (
        f"[paged value] follow-up calls should drop params, got {calls[1:]!r}"
    )


def test_flag_message_url_and_body() -> None:
    """flag_message should PATCH the message URL with `{flag: {flagStatus: 'flagged'}}`."""
    calls: list[tuple[str, dict]] = []
    original = graph_mod._patch_json

    def fake_patch_json(url, body):
        calls.append((url, body))
        return {}

    graph_mod._patch_json = fake_patch_json  # type: ignore[assignment]
    try:
        graph_mod.flag_message("AAMkAGE2/abc=")
    finally:
        graph_mod._patch_json = original  # type: ignore[assignment]

    assert len(calls) == 1, f"[flag_message] expected 1 PATCH call, got {len(calls)}"

    url, body = calls[0]
    expected_url = (
        f"{graph_mod.GRAPH_BASE_URL}/users/test%2Buser%40example.com"
        f"/messages/AAMkAGE2%2Fabc%3D"
    )
    assert url == expected_url, f"[flag_message URL] expected {expected_url!r}, got {url!r}"
    assert body == {"flag": {"flagStatus": "flagged"}}, (
        f"[flag_message body] expected flagged payload, got {body!r}"
    )
