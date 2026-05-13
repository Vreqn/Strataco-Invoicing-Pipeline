"""Unit tests for `tools._lib.inbox_report`.

Covers the helpers extracted from the deleted Step 8 (`steps/step_8_inbox_sweep.py`):

  - `sender_display` fallbacks: name+address, address-only, name-only, empty dict, None.
  - `render_messages`: empty list, single message, multiple messages, malformed
    `from`, optional `Msg id` line, `Attachments: yes/no`, `start_number` offset.

Standalone: no pytest dependency. Run with `python tests/test_inbox_report.py`.
Exits 0 if every case passes, 1 otherwise.
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


FAILED: list[str] = []


def _check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}{(': ' + detail) if detail else ''}")
        FAILED.append(name)


def test_render_empty() -> None:
    print("test_render_empty")
    lines = render_messages([])
    _check("empty_list_returns_empty", lines == [], f"got: {lines!r}")


def test_render_single() -> None:
    print("test_render_single")
    lines = render_messages([_msg()])
    body = "\n".join(lines)
    _check("from_line", "1. From:    John Doe <john@abcplumbing.com>" in body)
    _check("domain_line", "Domain:  abcplumbing.com" in body)
    _check("subject_line", "Subject: Q1 invoice batch" in body)
    _check("arrived_line", "Arrived: 2026-05-12T08:14:00Z" in body)
    _check("attachments_yes", "Attachments: yes" in body)
    _check("msg_id_line", "Msg id:  AAMkADk2NzQwYjFiLTUyNzAt" in body)


def test_render_multiple() -> None:
    print("test_render_multiple")
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
    _check("numbered_1", "1. From:" in body)
    _check("numbered_2", "2. From:" in body)
    _check("numbered_3", "3. From:" in body)
    _check("second_msg_no_attachments", "Attachments: no" in body)
    _check("third_msg_address_only", "From:    noreply@hydroco.ca" in body)


def test_render_start_number_offset() -> None:
    """When the caller wants the list numbered to continue an earlier sequence."""
    print("test_render_start_number_offset")
    body = "\n".join(render_messages([_msg()], start_number=5))
    _check("starts_at_five", "5. From:" in body)
    _check("does_not_start_at_one", "1. From:" not in body)


def test_render_malformed_from() -> None:
    """`from` is None — Outlook produces this for drafts and some system mail."""
    print("test_render_malformed_from")
    msg = _msg(sender_name=None, sender_address=None)
    msg["from"] = None
    body = "\n".join(render_messages([msg]))
    _check("renders_unknown_sender", "(unknown sender)" in body)
    _check("no_domain_line_when_unknown", "Domain:" not in body)


def test_render_missing_subject_and_id() -> None:
    """Subject defaults to '(no subject)'; missing id suppresses the Msg id line."""
    print("test_render_missing_subject_and_id")
    msg = _msg(subject="")
    msg["id"] = ""
    body = "\n".join(render_messages([msg]))
    _check("subject_default", "Subject: (no subject)" in body)
    _check("no_msg_id_line_when_blank", "Msg id:" not in body)


def test_sender_display_fallbacks() -> None:
    print("test_sender_display_fallbacks")
    # Name + address
    d, a, dom = sender_display(
        {"emailAddress": {"name": "John", "address": "john@x.com"}},
    )
    _check("display_name_address", d == "John <john@x.com>" and a == "john@x.com" and dom == "x.com")
    # Address only
    d, a, dom = sender_display(
        {"emailAddress": {"address": "x@y.org"}},
    )
    _check("display_address_only", d == "x@y.org" and dom == "y.org")
    # Name only (no address)
    d, a, dom = sender_display(
        {"emailAddress": {"name": "Anonymous"}},
    )
    _check("display_name_only", d == "Anonymous" and a == "" and dom == "")
    # Empty dict
    d, a, dom = sender_display({})
    _check("display_empty_dict", d == "(unknown sender)")
    # None
    d, a, dom = sender_display(None)
    _check("display_none", d == "(unknown sender)")


def main() -> int:
    test_render_empty()
    test_render_single()
    test_render_multiple()
    test_render_start_number_offset()
    test_render_malformed_from()
    test_render_missing_subject_and_id()
    test_sender_display_fallbacks()
    if FAILED:
        print(f"\nFAILED ({len(FAILED)}):")
        for n in FAILED:
            print(f"  - {n}")
        return 1
    print("\nall ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
