"""Regression tests for Step 1 forward-to-manager logic (0.15.3).

Covers:
  - _decide_email_action: multi-PDF AGREE+NO_PLAN → ROUTE_AS_SUBJECT (not FLAG)
  - _decide_email_action: lone NO_PLAN → ROUTE_AS_SUBJECT (unchanged)
  - _decide_email_action: AMBIGUOUS → FLAG_AND_HOLD (unchanged)
  - _decide_email_action: AGREE-only → ROUTE_AS_SUBJECT (unchanged)
  - _process_self_attachments: forward_message called when non-PDF extras exist
  - _process_self_attachments: forward_message called when NO_PLAN PDF sibling exists
  - _process_self_attachments: forward_message NOT called for single-PDF invoice
  - _process_self_attachments: forward_message NOT called when routing fails
  - _process_self_attachments: empty-manager-email → FORWARD_SKIPPED (no crash)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch, call

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from steps.step_1_intake import (
    EmailAction,
    EmailActionKind,
    PdfClassification,
    PdfOutcome,
    _decide_email_action,
)
from tools._lib.xls import PlanRow


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _plan_row(
    plan_norm: str = "BCS2707",
    manager_email: str = "mgr@example.com",
    strata_name: str = "Strata BCS 2707",
) -> PlanRow:
    return PlanRow(
        plan_norm=plan_norm,
        plan_raw=f"BCS {plan_norm[3:]}",
        strata_name=strata_name,
        address="123 Main St",
        manager_name="Test Manager",
        manager_key="Test_Manager",
        manager_email=manager_email,
        ap_name="AP Name",
        ap_key="AP_Key",
        ap_email="ap@example.com",
        status_active=True,
    )


def _cls(
    outcome: PdfOutcome,
    name: str = "invoice.pdf",
    plan_norm: str = "",
    plan_row: PlanRow | None = None,
) -> PdfClassification:
    return PdfClassification(
        outcome=outcome,
        base_name=name,
        blob=b"%PDF-1.4 fake",
        pdf_plan_norm=plan_norm,
        pdf_plan_row=plan_row,
    )


# ---------------------------------------------------------------------------
# _decide_email_action — pure function tests
# ---------------------------------------------------------------------------

class TestDecideEmailAction:
    def test_agree_only_routes_as_subject(self) -> None:
        row = _plan_row()
        clsfs = [_cls(PdfOutcome.AGREE, plan_norm="BCS2707", plan_row=row)]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT

    def test_lone_no_plan_routes_as_subject(self) -> None:
        clsfs = [_cls(PdfOutcome.NO_PLAN)]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT

    def test_multi_pdf_agree_plus_no_plan_routes_not_flags(self) -> None:
        """Regression: was FLAG_AND_HOLD before 0.15.3."""
        row = _plan_row()
        clsfs = [
            _cls(PdfOutcome.AGREE, "invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.NO_PLAN, "boilerplate.pdf"),
        ]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT, (
            "multi-PDF AGREE+NO_PLAN should now be ROUTE_AS_SUBJECT, not FLAG"
        )

    def test_multi_pdf_only_no_plan_routes_as_subject(self) -> None:
        clsfs = [
            _cls(PdfOutcome.NO_PLAN, "doc1.pdf"),
            _cls(PdfOutcome.NO_PLAN, "doc2.pdf"),
        ]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT

    def test_agree_plus_empty_routes_as_subject(self) -> None:
        row = _plan_row()
        clsfs = [
            _cls(PdfOutcome.AGREE, "invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.EMPTY, "scanned.pdf"),
        ]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT

    def test_ambiguous_flags(self) -> None:
        clsfs = [_cls(PdfOutcome.AMBIGUOUS, "unclear.pdf")]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.FLAG_AND_HOLD

    def test_no_classifications_routes_as_subject(self) -> None:
        action = _decide_email_action("BCS2707", [])
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT

    def test_agree_plus_no_plan_plus_empty_routes_as_subject(self) -> None:
        row = _plan_row()
        clsfs = [
            _cls(PdfOutcome.AGREE, "invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.EMPTY, "scanned.pdf"),
            _cls(PdfOutcome.NO_PLAN, "fsc_surcharge.pdf"),
        ]
        action = _decide_email_action("BCS2707", clsfs)
        assert action.kind == EmailActionKind.ROUTE_AS_SUBJECT


# ---------------------------------------------------------------------------
# _process_self_attachments — forward behavior via mocks
#
# We patch `steps.step_1_intake.graph` to intercept all Graph API calls,
# `steps.step_1_intake._classify_pdf_against_subject` to inject classifications,
# `steps.step_1_intake._route_pdf` to return ROUTED without touching disk,
# and `steps.step_1_intake._is_real_pdf` to accept any bytes.
# ---------------------------------------------------------------------------

FAKE_PDF = b"%PDF-1.4 fake content"


def _fake_attachment(name: str, att_id: str = "att1", content_type: str = "application/pdf") -> dict:
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": name,
        "id": att_id,
        "contentType": content_type,
        "size": len(FAKE_PDF),
    }


class _FakeRun:
    def __init__(self) -> None:
        self.processed = 0
        self.need_review: list[str] = []
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, msg: str) -> None:
        self.infos.append(msg)

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def review(self, msg: str) -> None:
        self.need_review.append(msg)

    def error(self, msg: str) -> None:
        self.errors.append(msg)


def _run_process_self(
    attachments: list[dict],
    classifications: list[PdfClassification],
    plan_row: PlanRow,
    graph_mock: MagicMock,
    route_outcome: str = "ROUTED",
) -> _FakeRun:
    """Wire up mocks and call _process_self_attachments."""
    from steps.step_1_intake import RouteOutcome, _process_self_attachments

    run = _FakeRun()
    graph_mock.list_attachments.return_value = attachments
    graph_mock.download_attachment.return_value = FAKE_PDF

    cls_iter = iter(classifications)

    with (
        patch("steps.step_1_intake._is_real_pdf", return_value=True),
        patch("steps.step_1_intake._classify_pdf_against_subject", side_effect=lambda *a, **kw: next(cls_iter)),
        patch("steps.step_1_intake._route_pdf", return_value=RouteOutcome.ROUTED),
        patch("steps.step_1_intake.dup_ledger") as _dup,
    ):
        _process_self_attachments(
            msg_id="msg123",
            subject="RE: BCS 2707 - Test Invoice",
            plan_row=plan_row,
            match_source="subject",
            received_str="2026-05-14T08:00:00",
            sender_domain="vendor.com",
            rows=[plan_row],
            ledger=_dup.load.return_value,
            run=run,
            processed_folder_id="folder_processed",
            duplicate_folder_id="folder_dup",
        )
    return run


class TestProcessSelfAttachmentsForward:
    """Forward-to-manager behavior in _process_self_attachments."""

    def _make_graph(self) -> MagicMock:
        """Return a fresh mock for the graph module."""
        g = MagicMock()
        g.resolve_recipient.side_effect = lambda email: email
        return g

    def test_single_invoice_pdf_no_forward(self) -> None:
        """Single invoice PDF with no extras → forward_message never called."""
        row = _plan_row()
        att = _fake_attachment("BCS2707 - Invoice.pdf")
        cls = _cls(PdfOutcome.AGREE, "BCS2707 - Invoice.pdf", "BCS2707", row)

        g = self._make_graph()
        with patch("steps.step_1_intake.graph", g):
            _run_process_self([att], [cls], row, g)

        g.forward_message.assert_not_called()

    def test_invoice_plus_no_plan_pdf_forwards(self) -> None:
        """Invoice PDF + NO_PLAN sibling → forward_message called with manager email."""
        row = _plan_row(manager_email="mgr@example.com")
        atts = [
            _fake_attachment("BCS2707 - Invoice.pdf", "att1"),
            _fake_attachment("FSC_Fuel_Surcharge_.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "BCS2707 - Invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.NO_PLAN, "FSC_Fuel_Surcharge_.pdf"),
        ]

        g = self._make_graph()
        with patch("steps.step_1_intake.graph", g):
            _run_process_self(atts, clsfs, row, g)

        g.forward_message.assert_called_once()
        args, kwargs = g.forward_message.call_args
        assert args[0] == "msg123"
        assert "mgr@example.com" in args[1]

    def test_invoice_plus_docx_extra_forwards(self) -> None:
        """Invoice PDF + non-PDF attachment → forward_message called."""
        row = _plan_row(manager_email="mgr@example.com")
        atts = [
            _fake_attachment("BCS2707 - Invoice.pdf", "att1"),
            _fake_attachment("Contract.docx", "att2", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ]
        # Only one PDF classification (the .docx is discarded in Pass 1)
        clsfs = [
            _cls(PdfOutcome.AGREE, "BCS2707 - Invoice.pdf", "BCS2707", row),
        ]

        g = self._make_graph()
        with patch("steps.step_1_intake.graph", g):
            _run_process_self(atts, clsfs, row, g)

        g.forward_message.assert_called_once()

    def test_two_agree_pdfs_no_forward(self) -> None:
        """Two invoice PDFs for the same plan, no extras → no forward."""
        row = _plan_row()
        atts = [
            _fake_attachment("Invoice_A.pdf", "att1"),
            _fake_attachment("Invoice_B.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "Invoice_A.pdf", "BCS2707", row),
            _cls(PdfOutcome.AGREE, "Invoice_B.pdf", "BCS2707", row),
        ]

        g = self._make_graph()
        with patch("steps.step_1_intake.graph", g):
            _run_process_self(atts, clsfs, row, g)

        g.forward_message.assert_not_called()

    def test_no_forward_when_routing_fails(self) -> None:
        """If _route_pdf returns FAILED, no forward (partial-commit flag path)."""
        from steps.step_1_intake import RouteOutcome
        row = _plan_row(manager_email="mgr@example.com")
        atts = [
            _fake_attachment("Invoice.pdf", "att1"),
            _fake_attachment("Boilerplate.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "Invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.NO_PLAN, "Boilerplate.pdf"),
        ]

        g = self._make_graph()
        with (
            patch("steps.step_1_intake.graph", g),
            patch("steps.step_1_intake._is_real_pdf", return_value=True),
            patch(
                "steps.step_1_intake._classify_pdf_against_subject",
                side_effect=iter(clsfs),
            ),
            patch("steps.step_1_intake._route_pdf", return_value=RouteOutcome.FAILED),
            patch("steps.step_1_intake.dup_ledger") as _dup,
        ):
            g.list_attachments.return_value = atts
            g.download_attachment.return_value = FAKE_PDF
            from steps.step_1_intake import _process_self_attachments
            run = _FakeRun()
            _process_self_attachments(
                msg_id="msg123",
                subject="RE: BCS 2707 - Test Invoice",
                plan_row=row,
                match_source="subject",
                received_str="2026-05-14T08:00:00",
                sender_domain="vendor.com",
                rows=[row],
                ledger=_dup.load.return_value,
                run=run,
                processed_folder_id="folder_processed",
                duplicate_folder_id="folder_dup",
            )

        g.forward_message.assert_not_called()

    def test_missing_manager_email_flags_and_stays_in_inbox(self) -> None:
        """No manager email → FORWARD_SKIPPED warn + red flag + email stays in inbox."""
        row = _plan_row(manager_email="")
        atts = [
            _fake_attachment("Invoice.pdf", "att1"),
            _fake_attachment("Boilerplate.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "Invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.NO_PLAN, "Boilerplate.pdf"),
        ]

        g = self._make_graph()
        with patch("steps.step_1_intake.graph", g):
            run = _run_process_self(atts, clsfs, row, g)

        g.forward_message.assert_not_called()
        assert any("FORWARD_SKIPPED" in w for w in run.warns), (
            "expected FORWARD_SKIPPED warning when manager email is empty"
        )
        g.flag_message.assert_called_once_with("msg123"), (
            "expected flag_message when forward was blocked by missing email"
        )
        g.move_message_to_folder.assert_not_called(), (
            "email must stay in inbox when forward was blocked"
        )

    def test_forward_failure_flags_email_and_stays_in_inbox(self) -> None:
        """Graph forward error → flag set + email stays in inbox (not moved to processed)."""
        row = _plan_row(manager_email="mgr@example.com")
        atts = [
            _fake_attachment("Invoice.pdf", "att1"),
            _fake_attachment("Boilerplate.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "Invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.NO_PLAN, "Boilerplate.pdf"),
        ]

        g = self._make_graph()
        g.forward_message.side_effect = RuntimeError("Graph 503 Service Unavailable")
        with patch("steps.step_1_intake.graph", g):
            run = _run_process_self(atts, clsfs, row, g)

        g.forward_message.assert_called_once()
        assert any("forward_to_manager failed" in e for e in run.errors), (
            "expected error log when forward raises"
        )
        g.flag_message.assert_called_once_with("msg123"), (
            "expected flag_message when forward failed"
        )
        g.move_message_to_folder.assert_not_called(), (
            "email must stay in inbox when forward failed"
        )

    def test_forward_email_moves_to_processed(self) -> None:
        """When forwarded, email is moved to processed_emails regardless of outcomes."""
        row = _plan_row(manager_email="mgr@example.com")
        atts = [
            _fake_attachment("Invoice.pdf", "att1"),
            _fake_attachment("Boilerplate.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "Invoice.pdf", "BCS2707", row),
            _cls(PdfOutcome.NO_PLAN, "Boilerplate.pdf"),
        ]

        g = self._make_graph()
        with patch("steps.step_1_intake.graph", g):
            _run_process_self(atts, clsfs, row, g)

        g.move_message_to_folder.assert_called_once_with("msg123", "folder_processed")


# ---------------------------------------------------------------------------
# _sweep_inbox_to_action_required
# ---------------------------------------------------------------------------

class TestSweepInboxToActionRequired:
    """Tests for _sweep_inbox_to_action_required (added 0.15.x)."""

    def test_happy_path_moves_all_messages(self) -> None:
        """Every originally-fetched inbox message is moved to the given folder_id."""
        from steps.step_1_intake import _sweep_inbox_to_action_required

        msgs = [
            {"id": "msg1", "subject": "Invoice A"},
            {"id": "msg2", "subject": "Invoice B"},
            {"id": "msg3", "subject": "General query"},
        ]
        run = _FakeRun()
        move_calls: list[tuple[str, str]] = []

        with patch("steps.step_1_intake.graph") as mock_graph:
            mock_graph.list_inbox_messages.return_value = msgs
            mock_graph.move_message_to_folder.side_effect = (
                lambda mid, fid: move_calls.append((mid, fid))
            )
            result = _sweep_inbox_to_action_required(
                "action-folder-id", run, {"msg1", "msg2", "msg3"}
            )

        assert result == 3
        assert move_calls == [
            ("msg1", "action-folder-id"),
            ("msg2", "action-folder-id"),
            ("msg3", "action-folder-id"),
        ]
        assert any("3" in m for m in run.infos), "expected info log with moved count"
        assert not run.errors

    def test_graceful_degradation_when_list_fails(self) -> None:
        """If list_inbox_messages raises, sweep logs an error and returns 0."""
        from steps.step_1_intake import _sweep_inbox_to_action_required

        run = _FakeRun()
        with patch("steps.step_1_intake.graph") as mock_graph:
            mock_graph.list_inbox_messages.side_effect = RuntimeError("Graph 503")
            result = _sweep_inbox_to_action_required("action-folder-id", run, {"msg1"})

        assert result == 0
        mock_graph.move_message_to_folder.assert_not_called()
        assert any("sweep" in e for e in run.errors), "expected error log on list failure"

    def test_partial_move_failure_continues_and_logs(self) -> None:
        """A move failure for one message doesn't stop the sweep; others still move."""
        from steps.step_1_intake import _sweep_inbox_to_action_required

        msgs = [
            {"id": "msg1", "subject": "Good"},
            {"id": "msg2", "subject": "Fails"},
            {"id": "msg3", "subject": "Also good"},
        ]
        run = _FakeRun()
        move_calls: list[str] = []

        def _move(mid: str, fid: str) -> None:
            if mid == "msg2":
                raise RuntimeError("404 Not Found")
            move_calls.append(mid)

        with patch("steps.step_1_intake.graph") as mock_graph:
            mock_graph.list_inbox_messages.return_value = msgs
            mock_graph.move_message_to_folder.side_effect = _move
            result = _sweep_inbox_to_action_required(
                "action-folder-id", run, {"msg1", "msg2", "msg3"}
            )

        assert result == 2
        assert move_calls == ["msg1", "msg3"]
        assert any("sweep" in e for e in run.errors), "expected error log for failed move"

    def test_new_arrival_not_in_initial_ids_is_skipped(self) -> None:
        """A message that arrived after the initial fetch (not in initial_ids) is
        left in the Inbox and NOT swept to Action_Required."""
        from steps.step_1_intake import _sweep_inbox_to_action_required

        initial_msg = {"id": "msg-original", "subject": "Original invoice"}
        new_arrival = {"id": "msg-new-arrival", "subject": "Just arrived"}
        run = _FakeRun()
        move_calls: list[str] = []

        with patch("steps.step_1_intake.graph") as mock_graph:
            # Re-fetch returns both the original and the new arrival
            mock_graph.list_inbox_messages.return_value = [initial_msg, new_arrival]
            mock_graph.move_message_to_folder.side_effect = (
                lambda mid, fid: move_calls.append(mid)
            )
            result = _sweep_inbox_to_action_required(
                "action-folder-id", run, {"msg-original"}
            )

        assert result == 1, "only the original message should be swept"
        assert move_calls == ["msg-original"], "new arrival must NOT be moved"
        assert not run.errors


# ---------------------------------------------------------------------------
# _process_prior_attachments — Changes E and F (v0.16.1)
#
# Same mock strategy as TestProcessSelfAttachmentsForward.
# We patch `steps.step_1_intake.graph`, `_classify_pdf_against_subject`,
# `_route_pdf`, `_is_real_pdf`, and `dup_ledger`.
# ---------------------------------------------------------------------------

class TestProcessPriorAttachments:
    """Tests for Change E (NO_PLAN skip + forward) and Change F (per_pdf_plan for
    PDF_OVERRIDE) in _process_prior_attachments."""

    def _make_graph(self) -> MagicMock:
        g = MagicMock()
        g.resolve_recipient.side_effect = lambda email: email
        return g

    def test_prior_no_plan_skip_routes_invoice_not_boilerplate(self) -> None:
        """Change E: Prior has AGREE invoice + NO_PLAN boilerplate.

        The invoice must be routed; the boilerplate must be skipped (NOT passed
        to _route_pdf); and graph.forward_message must be called with prior_msg_id
        so the manager can see the full original email.
        """
        from steps.step_1_intake import RouteOutcome, _process_prior_attachments

        row = _plan_row("BCS3396", "mgr@example.com", "Strata BCS 3396")
        atts = [
            _fake_attachment("BCS3396_invoice.pdf", "att1"),
            _fake_attachment("FSC_Fuel_Surcharge_.pdf", "att2"),
        ]
        clsfs = [
            _cls(PdfOutcome.AGREE, "BCS3396_invoice.pdf", "BCS3396", row),
            _cls(PdfOutcome.NO_PLAN, "FSC_Fuel_Surcharge_.pdf"),
        ]

        run = _FakeRun()
        route_pdf_mock = MagicMock(return_value=RouteOutcome.ROUTED)
        g = self._make_graph()
        g.list_attachments.return_value = atts
        g.download_attachment.return_value = FAKE_PDF

        cls_iter = iter(clsfs)
        with (
            patch("steps.step_1_intake.graph", g),
            patch("steps.step_1_intake._is_real_pdf", return_value=True),
            patch(
                "steps.step_1_intake._classify_pdf_against_subject",
                side_effect=lambda *a, **kw: next(cls_iter),
            ),
            patch("steps.step_1_intake._route_pdf", route_pdf_mock),
            patch("steps.step_1_intake.dup_ledger") as _dup,
        ):
            _process_prior_attachments(
                prior_msg_id="prior_msg123",
                reply_subject="RE: BCS 3396 Invoice",
                plan_row=row,
                match_source="subject",
                received_str="2026-05-14T08:00:00",
                prior_sender_domain="vendor.com",
                rows=[row],
                ledger=_dup.load.return_value,
                run=run,
            )

        # Invoice routed, boilerplate skipped
        assert route_pdf_mock.call_count == 1, (
            f"[prior NO_PLAN skip] expected _route_pdf called once (invoice only), "
            f"got {route_pdf_mock.call_count}"
        )
        routed_name = route_pdf_mock.call_args[0][1]
        assert routed_name == "BCS3396_invoice.pdf", (
            f"[prior NO_PLAN skip] expected invoice routed, got {routed_name!r}"
        )

        # Prior email forwarded to manager
        g.forward_message.assert_called_once()
        fwd_args = g.forward_message.call_args[0]
        assert fwd_args[0] == "prior_msg123", (
            f"[prior NO_PLAN skip] forward must use prior_msg_id, got {fwd_args[0]!r}"
        )
        assert fwd_args[1] == "mgr@example.com", (
            f"[prior NO_PLAN skip] forward must target manager email, got {fwd_args[1]!r}"
        )

    def test_prior_pdf_override_routes_to_pdf_plan(self) -> None:
        """Change F: Prior PDF confidently identifies Plan B; reply subject names Plan A.

        _route_pdf must receive Plan B's PlanRow as target_row — the prior path
        must honour per_pdf_plan for ROUTE_AS_SUBJECT actions, just like
        _process_self_attachments does.
        """
        from steps.step_1_intake import RouteOutcome, _process_prior_attachments

        row_a = _plan_row("BCS2707", "mgr_a@example.com", "Strata BCS 2707")
        row_b = _plan_row("BCS2800", "mgr_b@example.com", "Strata BCS 2800")
        atts = [_fake_attachment("BCS2800_invoice.pdf", "att1")]
        # PDF_OVERRIDE: PDF says BCS2800 while reply subject names BCS2707
        clsfs = [_cls(PdfOutcome.PDF_OVERRIDE, "BCS2800_invoice.pdf", "BCS2800", row_b)]

        run = _FakeRun()
        route_pdf_mock = MagicMock(return_value=RouteOutcome.ROUTED)
        g = self._make_graph()
        g.list_attachments.return_value = atts
        g.download_attachment.return_value = FAKE_PDF

        cls_iter = iter(clsfs)
        with (
            patch("steps.step_1_intake.graph", g),
            patch("steps.step_1_intake._is_real_pdf", return_value=True),
            patch(
                "steps.step_1_intake._classify_pdf_against_subject",
                side_effect=lambda *a, **kw: next(cls_iter),
            ),
            patch("steps.step_1_intake._route_pdf", route_pdf_mock),
            patch("steps.step_1_intake.dup_ledger") as _dup,
        ):
            _process_prior_attachments(
                prior_msg_id="prior_msg456",
                reply_subject="RE: BCS 2707 Invoice",
                plan_row=row_a,  # reply's plan — must NOT be used for routing
                match_source="subject",
                received_str="2026-05-14T08:00:00",
                prior_sender_domain="vendor.com",
                rows=[row_a, row_b],
                ledger=_dup.load.return_value,
                run=run,
            )

        assert route_pdf_mock.call_count == 1, (
            f"[prior PDF_OVERRIDE] expected _route_pdf called once, "
            f"got {route_pdf_mock.call_count}"
        )
        # _route_pdf signature: (blob, base_name, target_row, received_str, sender_domain, ledger, run)
        target_row = route_pdf_mock.call_args[0][2]
        assert target_row.plan_norm == "BCS2800", (
            f"[prior PDF_OVERRIDE] expected target_row=BCS2800 (PDF's plan), "
            f"got {target_row.plan_norm!r} — prior path ignored per_pdf_plan"
        )
        assert target_row.manager_email == "mgr_b@example.com", (
            f"[prior PDF_OVERRIDE] expected manager_email=mgr_b@example.com, "
            f"got {target_row.manager_email!r}"
        )
