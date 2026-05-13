"""Render Outlook Inbox messages into report lines.

Extracted from the deleted Step 8 so Step 6's consolidated morning report can
reuse the same per-message formatting. Two pure functions:

  * `sender_display(msg_from)` — `(display, address, domain)` from a Graph
    `from` field. Falls back gracefully when the shape is missing or malformed.
  * `render_messages(messages, start_number=1)` — produces the body lines for
    a list of Graph message dicts. Caller owns the section header and any
    blank-line separators around the block.
"""

from __future__ import annotations

from tools._lib import dup_fingerprint


def sender_display(msg_from) -> tuple[str, str, str]:
    """Pull `(display_line, address, domain)` from a Graph `from` field.

    `msg_from` is the Graph shape `{"emailAddress": {"name": ..., "address": ...}}`.
    Returns `("(unknown sender)", "", "")` on missing or malformed input rather
    than raising — a stuck email with a malformed sender is still worth
    surfacing in the report.
    """
    if not isinstance(msg_from, dict):
        return ("(unknown sender)", "", "")
    ea = msg_from.get("emailAddress")
    if not isinstance(ea, dict):
        return ("(unknown sender)", "", "")
    name = (ea.get("name") or "").strip()
    address = (ea.get("address") or "").strip()
    domain = dup_fingerprint.extract_domain(msg_from)
    if name and address:
        display = f"{name} <{address}>"
    elif address:
        display = address
    elif name:
        display = name
    else:
        display = "(unknown sender)"
    return (display, address, domain)


def render_messages(messages: list[dict], start_number: int = 1) -> list[str]:
    """Render Graph message dicts as report rows.

    Returns a list of lines suitable for `"\\n".join(...)`. The caller is
    responsible for the surrounding section header / spacing.

    Each rendered row contains: numbered `From:` line, optional `Domain:` line,
    `Subject:`, `Arrived:`, `Attachments: yes/no`, and (when present)
    `Msg id:`. A blank line is appended after every row including the last,
    matching the legacy Step 8 output so existing assertions still pass.
    """
    lines: list[str] = []
    for offset, msg in enumerate(messages):
        i = start_number + offset
        display, _address, domain = sender_display(msg.get("from"))
        subject_line = (msg.get("subject") or "(no subject)").strip()
        received = (msg.get("receivedDateTime") or "").strip() or "(unknown)"
        has_att = bool(msg.get("hasAttachments"))
        msg_id = (msg.get("id") or "").strip()

        lines.append(f"{i}. From:    {display}")
        if domain:
            lines.append(f"   Domain:  {domain}")
        lines.append(f"   Subject: {subject_line}")
        lines.append(f"   Arrived: {received}")
        lines.append(f"   Attachments: {'yes' if has_att else 'no'}")
        if msg_id:
            lines.append(f"   Msg id:  {msg_id}")
        lines.append("")
    return lines
