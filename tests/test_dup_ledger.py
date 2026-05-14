"""Unit tests for tools/_lib/dup_ledger — upsert-in-place fingerprint ledger.

Covers:
  - Missing file -> empty ledger.
  - Insert (new fingerprint) writes header + row.
  - Upsert in place: same sha256, second upsert overwrites.
  - find_by_hash / find_by_semantic_key.
  - increment_dup_count / update_stage round-trip through disk.
  - Corrupted row raises ValueError.
  - Empty sha256 in stored row raises on load.
  - Layer B lookup ignores blank fields.
  - Atomic full-file rewrite: previous good copy survives an interrupted write.
"""

from __future__ import annotations

import datetime as _dt
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from tools._lib.dup_ledger import (
    HEADER,
    FingerprintRow,
    Ledger,
    load,
    make_row,
)


def _fixed_dt(year=2026, month=5, day=11, hour=6, minute=0) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, minute, 0)


H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def test_missing_file_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "missing.csv"
        ledger = load(path)
        assert not ledger.all_rows(), f"[missing] expected empty, got {len(ledger.all_rows())} rows"
        assert ledger.find_by_hash(H1) is None, "[missing] find_by_hash should return None"


def test_upsert_writes_header_and_row() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        row = make_row(
            sha256=H1,
            plan_norm="BCS2707",
            invoice_number="INV-12345",
            amount_cents=44600,
            current_stage="manager_queue",
            now=_fixed_dt(),
        )
        ledger.upsert(row)

        assert path.exists(), "[upsert] file should exist after upsert"

        content = path.read_text(encoding="utf-8").splitlines()
        assert len(content) == 2, (
            f"[upsert] expected header + 1 row, got {len(content)}: {content}"
        )
        assert content[0] == ",".join(HEADER), f"[upsert] header wrong: {content[0]!r}"
        assert H1 in content[1] and "BCS2707" in content[1] and "INV-12345" in content[1], (
            f"[upsert] data row missing fields: {content[1]!r}"
        )
        assert "44600" in content[1], f"[upsert] amount_cents 44600 missing: {content[1]!r}"


def test_upsert_overwrites_same_sha() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               current_stage="manager_queue", now=_fixed_dt()))
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               current_stage="archived", archive_path="Strata_Plans/foo.pdf",
                               now=_fixed_dt()))
        all_rows = ledger.all_rows()
        assert len(all_rows) == 1, (
            f"[upsert overwrite] expected 1 row after re-upsert, got {len(all_rows)}"
        )
        assert all_rows[0].current_stage == "archived", (
            f"[upsert overwrite] stage not updated: {all_rows[0].current_stage}"
        )
        assert all_rows[0].archive_path == "Strata_Plans/foo.pdf", (
            f"[upsert overwrite] archive_path not updated: {all_rows[0].archive_path}"
        )


def test_find_by_hash_and_semantic_key() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com", now=_fixed_dt()))
        ledger.upsert(make_row(sha256=H2, plan_norm="LMS4193",
                               invoice_number="INV-B", amount_cents=25000,
                               sender_domain="vendor.com", now=_fixed_dt()))
        ledger.upsert(make_row(sha256=H3, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com", now=_fixed_dt()))

        assert ledger.find_by_hash(H1) is not None, "[find_by_hash] H1 should be found"
        assert ledger.find_by_hash(H2) is not None, "[find_by_hash] H2 should be found"
        assert ledger.find_by_hash("z" * 64) is None, "[find_by_hash] unknown hash should return None"

        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        assert match is not None and match.sha256 == H1, (
            f"[semantic_key] should return H1 (earliest), got {match!r}"
        )

        match = ledger.find_by_semantic_key("bcs2707", "INV-A", 10000, "vendor.com")
        assert match is not None and match.sha256 == H1, (
            "[semantic_key] plan match should be case-insensitive"
        )

        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "VENDOR.COM")
        assert match is not None and match.sha256 == H1, (
            "[semantic_key] sender_domain match should be case-insensitive"
        )

        assert ledger.find_by_semantic_key("BCS2707", "", 10000, "vendor.com") is None, (
            "[semantic_key] blank invoice# must return None"
        )
        assert ledger.find_by_semantic_key("BCS2707", "INV-A", None, "vendor.com") is None, (
            "[semantic_key] blank amount must return None"
        )
        assert ledger.find_by_semantic_key("", "INV-A", 10000, "vendor.com") is None, (
            "[semantic_key] blank plan must return None"
        )
        assert ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "") is None, (
            "[semantic_key] blank sender_domain must return None"
        )


def test_semantic_key_rejects_cross_vendor_match() -> None:
    """The Q3 scenario: ABC Plumbing's INV-1023 must NOT match XYZ Cleaning's INV-1023.

    Same plan + invoice number + amount but different sender domains: the new
    sender_domain field is the 4th key element, so the two rows are distinct
    Layer B entries. Each vendor's own lookup hits its own row; a third
    arrival with a third domain finds neither.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1023", amount_cents=85000,
                               sender_domain="abcplumbing.com", now=_fixed_dt()))
        ledger.upsert(make_row(sha256=H2, plan_norm="BCS2707",
                               invoice_number="INV-1023", amount_cents=85000,
                               sender_domain="xyzcleaning.com", now=_fixed_dt()))

        abc = ledger.find_by_semantic_key("BCS2707", "INV-1023", 85000, "abcplumbing.com")
        assert abc is not None and abc.sha256 == H1, (
            f"[cross-vendor] ABC lookup should return H1, got {abc!r}"
        )
        xyz = ledger.find_by_semantic_key("BCS2707", "INV-1023", 85000, "xyzcleaning.com")
        assert xyz is not None and xyz.sha256 == H2, (
            f"[cross-vendor] XYZ lookup should return H2, got {xyz!r}"
        )

        other = ledger.find_by_semantic_key("BCS2707", "INV-1023", 85000, "other-vendor.com")
        assert other is None, (
            f"[cross-vendor] third-vendor lookup should return None, got {other!r}"
        )


def test_blank_sender_domain_row_excluded_from_layer_b() -> None:
    """A row inserted by a non-email path (sender_domain="") is NOT indexed in Layer B."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="",
                               now=_fixed_dt()))

        assert ledger.find_by_hash(H1) is not None, "[blank domain] Layer A should still find the row"

        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        assert match is None, (
            f"[blank domain] Layer B should not match a blank-domain row, got {match!r}"
        )

        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "")
        assert match is None, (
            f"[blank domain] blank-domain lookup should return None, got {match!r}"
        )


def test_legacy_csv_without_sender_domain_column_parses() -> None:
    """A CSV written before the sender_domain column existed must still load."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        legacy_header = (
            "first_seen_date,sha256,plan_norm,invoice_number,amount_cents,"
            "archive_path,current_stage,last_seen_date,dup_count,last_dup_date"
        )
        path.write_text(
            legacy_header + "\n"
            "2026-05-01," + H1 + ",BCS2707,INV-OLD,12345,Strata_Plans/foo.pdf,"
            "archived,2026-05-01,0,\n",
            encoding="utf-8",
        )

        ledger = load(path)
        rows = ledger.all_rows()
        assert len(rows) == 1, f"[legacy csv] expected 1 row, got {len(rows)}"
        row = rows[0]
        assert row.sha256 == H1, f"[legacy csv] sha mangled: {row.sha256!r}"
        assert row.sender_domain == "", (
            f"[legacy csv] sender_domain should default to empty, got {row.sender_domain!r}"
        )
        assert row.plan_norm == "BCS2707" and row.invoice_number == "INV-OLD", (
            f"[legacy csv] other fields scrambled: {row!r}"
        )
        assert row.amount_cents == 12345, f"[legacy csv] amount_cents wrong: {row.amount_cents!r}"
        assert row.current_stage == "archived", f"[legacy csv] stage wrong: {row.current_stage!r}"

        assert ledger.find_by_hash(H1) is not None, "[legacy csv] Layer A should still find legacy row"
        match = ledger.find_by_semantic_key("BCS2707", "INV-OLD", 12345, "anyvendor.com")
        assert match is None, (
            f"[legacy csv] Layer B must not match a legacy blank-domain row, got {match!r}"
        )


def test_increment_dup_count_roundtrips() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        initial = ledger.find_by_hash(H1)
        assert initial is not None and initial.last_dup_date == "", (
            f"[dup_count] fresh row should have empty last_dup_date, "
            f"got {initial.last_dup_date if initial else None!r}"
        )

        ledger.increment_dup_count(H1, now=_fixed_dt(day=15))
        ledger.increment_dup_count(H1, now=_fixed_dt(day=16))

        row = ledger.find_by_hash(H1)
        assert row is not None and row.dup_count == 2, (
            f"[dup_count] expected 2, got {row.dup_count if row else None}"
        )
        assert row.last_seen_date == "2026-05-16", (
            f"[dup_count] last_seen_date should be 2026-05-16, got {row.last_seen_date}"
        )
        assert row.last_dup_date == "2026-05-16", (
            f"[dup_count] last_dup_date should be 2026-05-16, got {row.last_dup_date}"
        )
        assert row.first_seen_date == "2026-05-11", (
            f"[dup_count] first_seen_date should be unchanged: {row.first_seen_date}"
        )

        ledger2 = load(path)
        row2 = ledger2.find_by_hash(H1)
        assert row2 is not None and row2.dup_count == 2 and row2.last_seen_date == "2026-05-16", (
            f"[dup_count round trip] lost across reload: {row2!r}"
        )
        assert row2.last_dup_date == "2026-05-16", (
            f"[dup_count round trip] last_dup_date lost: {row2.last_dup_date!r}"
        )


def test_update_stage_roundtrips() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               current_stage="manager_queue", now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "ap_queue", now=_fixed_dt(day=11, hour=6, minute=40))
        ledger.update_stage(H1, "archived",
                            archive_path="Strata_Plans/BCS 2707/12345 - 03 - BCS2707 March 2026 inv.pdf",
                            now=_fixed_dt(day=13))

        row = ledger.find_by_hash(H1)
        assert row is not None, "[update_stage] row vanished"
        assert row.current_stage == "archived", f"[update_stage] stage {row.current_stage} != 'archived'"
        assert "Strata_Plans" in row.archive_path, f"[update_stage] archive_path not set: {row.archive_path}"

        ledger2 = load(path)
        row2 = ledger2.find_by_hash(H1)
        assert row2 is not None and row2.current_stage == "archived" and "Strata_Plans" in row2.archive_path, (
            f"[update_stage round trip] lost across reload: {row2!r}"
        )


def test_corrupted_row_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        path.write_text(
            ",".join(HEADER) + "\n"
            "2026-05-11," + H1 + ",BCS2707,INV-1,not_an_int,vendor.com,,manager_queue,2026-05-11,0\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load(path)


def test_empty_sha_in_loaded_row_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        path.write_text(
            ",".join(HEADER) + "\n"
            "2026-05-11,,BCS2707,INV-1,10000,vendor.com,,manager_queue,2026-05-11,0\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError):
            load(path)


def test_atomic_rewrite_keeps_prior_on_crash() -> None:
    """If the atomic write raises mid-rewrite, the prior good file must survive."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               now=_fixed_dt(day=11)))

        prior_bytes = path.read_bytes()

        with patch("os.replace", side_effect=OSError("simulated crash")):
            with pytest.raises(OSError):
                ledger.upsert(make_row(sha256=H2, plan_norm="LMS4193",
                                       invoice_number="INV-2", amount_cents=99999,
                                       now=_fixed_dt(day=12)))

        after_bytes = path.read_bytes()
        assert after_bytes == prior_bytes, (
            f"[atomic] prior good file was corrupted by failed write: "
            f"prior={len(prior_bytes)} bytes, after={len(after_bytes)} bytes"
        )

        tmp_count = sum(1 for p in path.parent.glob("invoice_fingerprints.csv.tmp.*"))
        assert tmp_count == 0, f"[atomic] expected 0 orphan tmp files, found {tmp_count}"


def test_transactional_rmw_no_lost_updates() -> None:
    """Two `Ledger` instances pointed at the same file must not lose updates."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"

        seed = load(path)
        seed.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                             invoice_number="INV-1", amount_cents=10000,
                             now=_fixed_dt(day=11)))

        process_a = load(path)
        process_b = load(path)

        process_b.upsert(make_row(sha256=H2, plan_norm="LMS4193",
                                  invoice_number="INV-2", amount_cents=25000,
                                  now=_fixed_dt(day=11)))

        assert process_a.find_by_hash(H2) is None, (
            "[rmw] process_a should not see H2 in-memory yet (stale)"
        )

        process_a.upsert(make_row(sha256=H3, plan_norm="VR9999",
                                  invoice_number="INV-3", amount_cents=99999,
                                  now=_fixed_dt(day=11)))

        fresh = load(path)
        assert len(fresh.all_rows()) == 3, f"[rmw] expected 3 rows on disk, got {len(fresh.all_rows())}"
        seen = {r.sha256 for r in fresh.all_rows()}
        for h, label in [(H1, "H1"), (H2, "H2"), (H3, "H3")]:
            assert h in seen, f"[rmw] expected {label} ({h[:12]}...) preserved on disk"


def test_overridden_row_excluded_from_semantic_index() -> None:
    """A row at stage=overridden must not block new semantic-key matches via find_by_semantic_key."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com",
                               now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "overridden")

        assert ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com") is None, (
            "[overridden] semantic lookup should skip overridden rows"
        )

        row = ledger.find_overridden_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        assert row is not None and row.sha256 == H1, (
            f"[overridden] find_overridden_by_semantic_key should return H1, got {row!r}"
        )


def test_consume_override_and_insert() -> None:
    """Override + Layer B regen: old row -> superseded, new row inserted."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)

        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com",
                               now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "overridden")

        new_row = make_row(sha256=H2, plan_norm="BCS2707",
                           invoice_number="INV-A", amount_cents=10000,
                           sender_domain="vendor.com",
                           now=_fixed_dt(day=12))
        ledger.consume_override_and_insert(old_sha256=H1, new_row=new_row)

        h1 = ledger.find_by_hash(H1)
        h2 = ledger.find_by_hash(H2)
        assert h1 is not None and h1.current_stage == "superseded", (
            f"[consume_override] H1 should be superseded, got {h1!r}"
        )
        assert h2 is not None and h2.current_stage == "manager_queue", (
            f"[consume_override] H2 should be active manager_queue, got {h2!r}"
        )

        sem = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        assert sem is not None and sem.sha256 == H2, (
            f"[consume_override] semantic key should point at H2, got {sem!r}"
        )

        assert ledger.find_overridden_by_hash(H1) is None, (
            "[consume_override] H1 should no longer be 'overridden'"
        )

        reloaded = load(path)
        assert reloaded.find_by_hash(H1).current_stage == "superseded", (
            "[consume_override round-trip] H1 superseded lost across reload"
        )
        assert reloaded.find_by_hash(H2).current_stage == "manager_queue", (
            "[consume_override round-trip] H2 lost across reload"
        )


def test_consume_override_rejects_non_overridden_row() -> None:
    """consume_override_and_insert must verify the old row's stage is still
    `overridden` under the lock. Two concurrent consumers of the same
    override row: only the first should succeed; the second must raise.
    """
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "overridden")

        new1 = make_row(sha256=H2, plan_norm="BCS2707",
                        invoice_number="INV-A", amount_cents=10000,
                        now=_fixed_dt(day=12))
        ledger.consume_override_and_insert(old_sha256=H1, new_row=new1)

        new2 = make_row(sha256=H3, plan_norm="BCS2707",
                        invoice_number="INV-A", amount_cents=10000,
                        now=_fixed_dt(day=12))
        with pytest.raises(ValueError):
            ledger.consume_override_and_insert(old_sha256=H1, new_row=new2)


def test_consume_override_missing_old_sha_raises() -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        with pytest.raises(KeyError):
            ledger.consume_override_and_insert(
                old_sha256="z" * 64,
                new_row=make_row(sha256=H2, plan_norm="BCS2707",
                                 invoice_number="INV-A", amount_cents=10000,
                                 now=_fixed_dt(day=12)),
            )


def test_increment_dup_count_missing_sha_raises() -> None:
    """KeyError surfaces if the sha disappears between read and mutation."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        with pytest.raises(KeyError):
            ledger.increment_dup_count("z" * 64)


def test_flush_failure_leaves_memory_unchanged() -> None:
    """If atomic_write_bytes raises mid-mutation, in-memory state must be untouched."""
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        prior_rows = ledger.all_rows()

        with patch("os.replace", side_effect=OSError("simulated crash")):
            with pytest.raises(OSError):
                ledger.upsert(make_row(sha256=H2, plan_norm="BCS2707",
                                       invoice_number="INV-B", amount_cents=20000,
                                       now=_fixed_dt(day=11)))

        after_rows = ledger.all_rows()
        assert [(r.sha256, r.current_stage) for r in after_rows] == [(r.sha256, r.current_stage) for r in prior_rows], (
            f"[flush fail] in-memory state diverged from prior: prior={prior_rows}, after={after_rows}"
        )

        assert ledger.find_by_hash(H2) is None, (
            "[flush fail] failed-write sha should not be in in-memory index"
        )
