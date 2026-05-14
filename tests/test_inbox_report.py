"""Unit tests for `tools._lib.inbox_report`.

Covers the helpers extracted from the deleted Step 8 (`steps/step_8_inbox_sweep.py`):

  - `sender_display` fallbacks: name+address, address-only, name-only, empty dict, None.
  - `render_messages`: empty list, single message, multiple messages, malformed
    `from`, optional `Msg id` line, `Attachments: yes/no`, `start_number` offset.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools._lib.inbox_report import render_messages, sender_display


def _msg(
    *,
    msg_id: str = "AAMkADk2NzQwYjFiLTUyNzAt",
    subject: str = "Q1 invoice batch",
    sender_name: str | None = "John Doe",
    sender_address: str | None = "john@abcplumbing.com",
    received: str = "2026-05-12T08:14:00Z",
    has_attachments: bool = True,
) -> dict:
    from_field: dict | None
    if sender_name is None and sender_address is None:
        from_field = None
    else:
        ea: dict = {}
        if sender_name is not None:
            ea["name"] = sender_name
        if sender_address is not None:
            ea["address"] = sender_address
        from_field = {"emailAddress": ea}
    msg: dict = {
        "id": msg_id,
        "subject": subject,
        "from": from_field,
        "receivedDateTime": received,
        "hasAttachments": has_attachments,
    }
    return msg


def test_render_empty() -> None:
    lines = render_messages([])
    assert lines == [], f"empty_list_returns_empty: got {lines!r}"


def test_render_single() -> None:
    lines = render_messages([_msg()])
    body = "\n".join(lines)
    assert "1. From:    John Doe <john@abcplumbing.com>" in body, "from_line"
    assert "Domain:  abcplumbing.com" in body, "domain_line"
    assert "Subject: Q1 invoice batch" in body, "subject_line"
    assert "Arrived: 2026-05-12T08:14:00Z" in body, "arrived_line"
    assert "Attachments: yes" in body, "attachments_yes"
    assert "Msg id:  AAMkADk2NzQwYjFiLTUyNzAt" in body, "msg_id_line"


def test_render_multiple() -> None:
    msgs = [
        _msg(),
        _msg(
            msg_id="BBB",
            subject="Quarterly statement",
            sender_name="Jane Roe",
            sender_address="ar@xyz-cleaning.com",
            received="2026-05-12T14:33:00Z",
            has_attachments=False,
        ),
        _msg(
            msg_id="CCC",
            subject="(no plan in subject)",
            sender_name=None,
            sender_address="noreply@hydroco.ca",
            received="2026-05-12T17:01:00Z",
            has_attachments=True,
        ),
    ]
    body = "\n".join(render_messages(msgs))
    assert "1. From:" in body, "numbered_1"
    assert "2. From:" in body, "numbered_2"
    assert "3. From:" in body, "numbered_3"
    assert "Attachments: no" in body, "second_msg_no_attachments"
    assert "From:    noreply@hydroco.ca" in body, "third_msg_address_only"


def test_render_start_number_offset() -> None:
    """When the caller wants the list numbered to continue an earlier sequence."""
    body = "\n".join(render_messages([_msg()], start_number=5))
    assert "5. From:" in body, "starts_at_five"
    assert "1. From:" not in body, "does_not_start_at_one"


def test_render_malformed_from() -> None:
    """`from` is None — Outlook produces this for drafts and some system mail."""
    msg = _msg(sender_name=None, sender_address=None)
    msg["from"] = None
    body = "\n".join(render_messages([msg]))
    assert "(unknown sender)" in body, "renders_unknown_sender"
    assert "Domain:" not in body, "no_domain_line_when_unknown"


def test_render_missing_subject_and_id() -> None:
    """Subject defaults to '(no subject)'; missing id suppresses the Msg id line."""
    msg = _msg(subject="")
    msg["id"] = ""
    body = "\n".join(render_messages([msg]))
    assert "Subject: (no subject)" in body, "subject_default"
    assert "Msg id:" not in body, "no_msg_id_line_when_blank"


def test_sender_display_fallbacks() -> None:
    d, a, dom = sender_display(
        {"emailAddress": {"name": "John", "address": "john@x.com"}},
    )
    assert d == "John <john@x.com>" and a == "john@x.com" and dom == "x.com", "display_name_address"

    d, a, dom = sender_display({"emailAddress": {"address": "x@y.org"}})
    assert d == "x@y.org" and dom == "y.org", "display_address_only"

    d, a, dom = sender_display({"emailAddress": {"name": "Anonymous"}})
    assert d == "Anonymous" and a == "" and dom == "", "display_name_only"

    d, a, dom = sender_display({})
    assert d == "(unknown sender)", "display_empty_dict"

    d, a, dom = sender_display(None)
    assert d == "(unknown sender)", "display_none"
