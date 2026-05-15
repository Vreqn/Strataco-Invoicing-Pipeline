"""Regression tests for step_1 conversation-link prior-message move handling.

Original bug (v0.15.0, logs/step_1_2026-05-14.log:92):

    ERROR | move-to-processed_emails failed: _post_json failed:
    404 Client Error: Not Found for url: .../messages/AAMk...44iJSAAA%3D/move

When a conversation-link reply pulls a PDF from a prior message in the same
thread, the reply branch moves *both* the reply and that prior out of the
Inbox. The prior message is still in the inbox list the outer loop iterates;
when the loop reached it, the `consumed_priors` skip branch tried to move it a
*second* time — Graph 404, the message is already gone.

Codex follow-up: the skip branch must act on the prior's *actual* move
disposition, not assume it was moved. `consumed_priors` is now a dict whose
value is `None` when the source was filed OK, or the target folder id when the
reply branch's source move failed (the skip branch retries once).

These tests drive `main()`'s outer loop with Graph and the prior-processing
internals mocked. Scenarios:
  1. source move succeeds  -> prior moved exactly once, no retry, no error.
  2. source move fails once -> skip branch retries, succeeds; failure surfaced.
  3. source move fails always -> skip branch retries once, both failures
     surfaced, prior NOT silently skipped.
"""

from __future__ import annotations

import logging
import os
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Stub env so importing tools._lib.config doesn't fail at module load.
os.environ.setdefault("STRATACO_ROOT", os.getcwd())
os.environ.setdefault("TENANT_ID", "x")
os.environ.setdefault("CLIENT_ID", "x")
os.environ.setdefault("CLIENT_SECRET", "x")
os.environ.setdefault("MAILBOX_UPN", "t@example.com")
os.environ.setdefault("NOTIFY_DEFAULT_EMAIL", "x@example.com")

import steps.step_1_intake as s1
from tools._lib import dup_ledger
from tools._lib.log import _Run
from tools._lib.xls import PlanRow


_REPLY_ID = "reply-msg-id"
_PRIOR_ID = "prior-msg-id"
_CONV_ID = "conversation-abc"
_PROCESSED_FOLDER = "processed-folder-id"
_DUP_FOLDER = "duplicate-folder-id"


def _row() -> PlanRow:
    return PlanRow(
        plan_norm="LMS1624",
        plan_raw="LMS 1624",
        strata_name="",
        address="",
        manager_name="Carey Grandy",
        manager_key="CAREY_GRANDY",
        manager_email="",
        ap_name="Alex AP",
        ap_key="ALEX_AP",
        ap_email="",
        status_active=True,
    )


def _reply_msg() -> dict:
    """Corrected reply: matches a plan, carries no attachment of its own."""
    return {
        "id": _REPLY_ID,
        "subject": "FW: Kimberly Court LMS 1624 - April 2026 Invoice",
        "bodyPreview": "",
        "hasAttachments": False,
        "conversationId": _CONV_ID,
        "receivedDateTime": "2026-05-13T10:00:00Z",
        "from": {"emailAddress": {"address": "frontdesk@stratacomgmt.com"}},
    }


def _prior_msg() -> dict:
    """The earlier message in the thread that actually carries the PDF."""
    return {
        "id": _PRIOR_ID,
        "subject": "Kimberly Court LMS 1624 - April 2026 Invoice",
        "bodyPreview": "",
        "hasAttachments": True,
        "conversationId": _CONV_ID,
        "receivedDateTime": "2026-05-13T09:00:00Z",
        "from": {"emailAddress": {"address": "vendor@example.com"}},
    }


@contextmanager
def _fake_daily_log(captured: dict):
    run = _Run("step_1", logging.getLogger("strataco.test_consumed_prior_move"))
    captured["run"] = run
    yield run
    run.status = "error" if run.errors else "ok"


def _find_child_folder_id(_parent: str, name: str) -> str | None:
    if name == "processed_emails":
        return _PROCESSED_FOLDER
    if name == "duplicate_emails":
        return _DUP_FOLDER
    return None  # Action_Required and others → sweep skipped in these tests


def _make_move(fail_first: dict[str, int] | None = None):
    """Graph `move_message_to_folder` simulator.

    `fail_first[msg_id]` = number of initial move attempts for that id that
    raise (a transient Graph failure); after that many failures the move
    succeeds. Once a message has been moved successfully it is no longer in the
    Inbox, so any further move of it raises a 404 — the real bug the original
    fix addressed.

    The returned callable exposes `.move_calls`: a list of (msg_id, folder_id).
    """
    fail_first = dict(fail_first or {})
    moved_ids: set[str] = set()
    call_counts: dict[str, int] = {}
    move_calls: list[tuple[str, str]] = []

    def _move(msg_id: str, folder_id: str) -> None:
        move_calls.append((msg_id, folder_id))
        call_counts[msg_id] = call_counts.get(msg_id, 0) + 1
        if msg_id in moved_ids:
            raise RuntimeError(
                f"_post_json failed: 404 Client Error: Not Found for url: "
                f".../messages/{msg_id}/move"
            )
        if call_counts[msg_id] <= fail_first.get(msg_id, 0):
            raise RuntimeError(
                f"_post_json failed: 503 Service Unavailable for url: "
                f".../messages/{msg_id}/move"
            )
        moved_ids.add(msg_id)

    _move.move_calls = move_calls  # type: ignore[attr-defined]
    return _move


def _run_main(tmp_path: Path, monkeypatch, move_side_effect):
    """Drive `s1.main()` with Graph + prior-processing internals mocked.

    Inbox = [reply, prior] in that order, so the reply (Branch B) consumes the
    prior before the outer loop iterates to the prior itself. Returns the
    captured `_Run`.
    """
    monkeypatch.setenv("STRATACO_ROOT", str(tmp_path))
    captured: dict = {}
    empty_ledger = dup_ledger.Ledger([], tmp_path / "_state" / "dup_ledger.csv")

    with (
        patch.object(s1, "daily_log", lambda step: _fake_daily_log(captured)),
        patch.object(
            s1.strataplan_snapshot, "refresh_snapshot",
            return_value=types.SimpleNamespace(name="snap.xlsx"),
        ),
        patch.object(s1, "load_plans", return_value=[_row()]),
        patch.object(s1, "plan_to_manager", return_value={}),
        patch.object(
            s1.graph, "list_inbox_messages",
            return_value=[_reply_msg(), _prior_msg()],
        ),
        patch.object(s1.graph, "find_child_folder_id", side_effect=_find_child_folder_id),
        patch.object(s1.dup_ledger, "load", return_value=empty_ledger),
        patch.object(s1, "_pick_plan", return_value=(_row(), "subject")),
        patch.object(
            s1.graph, "list_conversation_messages",
            return_value=[_prior_msg(), _reply_msg()],
        ),
        patch.object(
            s1, "_process_prior_attachments",
            return_value=s1.PriorProcessingResult(outcomes=[s1.RouteOutcome.ROUTED]),
        ),
        patch.object(s1.graph, "move_message_to_folder", side_effect=move_side_effect),
        patch.object(s1.graph, "flag_message"),
    ):
        rc = s1.main()

    assert rc == 0, f"main() returned {rc}; expected 0"
    return captured["run"]


def test_consumed_prior_is_moved_exactly_once(tmp_path: Path, monkeypatch) -> None:
    """Happy path: the reply branch moves the consumed prior out of the Inbox
    successfully, so the outer-loop skip branch just logs and continues — it
    must NOT re-move (that would 404, the original bug)."""
    move = _make_move()
    run = _run_main(tmp_path, monkeypatch, move)

    prior_moves = [c for c in move.move_calls if c[0] == _PRIOR_ID]
    assert len(prior_moves) == 1, (
        f"prior should be moved exactly once (by the reply branch); "
        f"got {len(prior_moves)}: {prior_moves}"
    )
    assert not run.errors, f"expected no errors; got {run.errors}"


def test_consumed_prior_move_retried_when_reply_branch_move_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """The reply branch's move of the *source* fails once. The outer-loop skip
    branch must retry it (not silently skip), and the retry succeeds — the
    source ends up filed, and the original failure is surfaced in run.errors."""
    move = _make_move(fail_first={_PRIOR_ID: 1})
    run = _run_main(tmp_path, monkeypatch, move)

    prior_moves = [c for c in move.move_calls if c[0] == _PRIOR_ID]
    assert len(prior_moves) == 2, (
        f"prior should be moved twice (reply-branch attempt fails, skip-branch "
        f"retry succeeds); got {len(prior_moves)}: {prior_moves}"
    )
    assert len(run.errors) == 1, (
        f"expected exactly the one reply-branch failure surfaced; got {run.errors}"
    )
    assert "move source msg" in run.errors[0], (
        f"expected the reply-branch source-move failure; got {run.errors[0]!r}"
    )


def test_consumed_prior_left_in_inbox_when_retry_also_fails(
    tmp_path: Path, monkeypatch
) -> None:
    """The source move fails on every attempt. The skip branch must retry
    exactly once (bounded — no loop), surface *both* failures, and leave the
    prior in the Inbox — never silently skip it."""
    move = _make_move(fail_first={_PRIOR_ID: 99})
    run = _run_main(tmp_path, monkeypatch, move)

    prior_moves = [c for c in move.move_calls if c[0] == _PRIOR_ID]
    assert len(prior_moves) == 2, (
        f"prior should be moved twice — reply-branch attempt + one bounded "
        f"retry; got {len(prior_moves)}: {prior_moves}"
    )
    assert len(run.errors) == 2, (
        f"both the reply-branch failure and the retry failure must be "
        f"surfaced; got {run.errors}"
    )
    assert any("move source msg" in e for e in run.errors), (
        f"expected the reply-branch source-move failure; got {run.errors}"
    )
    assert any("retry move of stranded" in e for e in run.errors), (
        f"expected the skip-branch retry failure; got {run.errors}"
    )
