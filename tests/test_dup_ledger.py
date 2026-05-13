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

Standalone: no pytest dependency. Run with `python tests/test_dup_ledger.py`.
Exits 0 if every case passes, 1 otherwise.
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

from tools._lib.dup_ledger import (
    HEADER,
    FingerprintRow,
    Ledger,
    load,
    make_row,
)


def _fixed_dt(year=2026, month=5, day=11, hour=6, minute=0) -> _dt.datetime:
    return _dt.datetime(year, month, day, hour, minute, 0)


# Some realistic-looking hashes for the tests.
H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def test_missing_file_returns_empty() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "missing.csv"
        ledger = load(path)
        if ledger.all_rows():
            failures.append(f"[missing] expected empty, got {len(ledger.all_rows())} rows")
        if ledger.find_by_hash(H1) is not None:
            failures.append("[missing] find_by_hash should return None")
    return failures


def test_upsert_writes_header_and_row() -> list[str]:
    failures: list[str] = []
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

        if not path.exists():
            failures.append("[upsert] file should exist after upsert")
            return failures

        content = path.read_text(encoding="utf-8").splitlines()
        if len(content) != 2:
            failures.append(f"[upsert] expected header + 1 row, got {len(content)}: {content}")
            return failures
        if content[0] != ",".join(HEADER):
            failures.append(f"[upsert] header wrong: {content[0]!r}")
        if H1 not in content[1] or "BCS2707" not in content[1] or "INV-12345" not in content[1]:
            failures.append(f"[upsert] data row missing fields: {content[1]!r}")
        if "44600" not in content[1]:
            failures.append(f"[upsert] amount_cents 44600 missing: {content[1]!r}")
    return failures


def test_upsert_overwrites_same_sha() -> list[str]:
    failures: list[str] = []
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
        if len(all_rows) != 1:
            failures.append(f"[upsert overwrite] expected 1 row after re-upsert, got {len(all_rows)}")
            return failures
        if all_rows[0].current_stage != "archived":
            failures.append(f"[upsert overwrite] stage not updated: {all_rows[0].current_stage}")
        if all_rows[0].archive_path != "Strata_Plans/foo.pdf":
            failures.append(f"[upsert overwrite] archive_path not updated: {all_rows[0].archive_path}")
    return failures


def test_find_by_hash_and_semantic_key() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com", now=_fixed_dt()))
        ledger.upsert(make_row(sha256=H2, plan_norm="LMS4193",
                               invoice_number="INV-B", amount_cents=25000,
                               sender_domain="vendor.com", now=_fixed_dt()))
        # New fingerprint, same semantic key as H1 (regenerated PDF case)
        ledger.upsert(make_row(sha256=H3, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com", now=_fixed_dt()))

        if ledger.find_by_hash(H1) is None:
            failures.append("[find_by_hash] H1 should be found")
        if ledger.find_by_hash(H2) is None:
            failures.append("[find_by_hash] H2 should be found")
        if ledger.find_by_hash("z" * 64) is not None:
            failures.append("[find_by_hash] unknown hash should return None")

        # Layer B lookup
        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        if match is None or match.sha256 != H1:
            failures.append(f"[semantic_key] should return H1 (earliest), got {match!r}")

        # Case-insensitive plan
        match = ledger.find_by_semantic_key("bcs2707", "INV-A", 10000, "vendor.com")
        if match is None or match.sha256 != H1:
            failures.append("[semantic_key] plan match should be case-insensitive")

        # Case-insensitive sender_domain
        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "VENDOR.COM")
        if match is None or match.sha256 != H1:
            failures.append("[semantic_key] sender_domain match should be case-insensitive")

        # Blank fields return None
        if ledger.find_by_semantic_key("BCS2707", "", 10000, "vendor.com") is not None:
            failures.append("[semantic_key] blank invoice# must return None")
        if ledger.find_by_semantic_key("BCS2707", "INV-A", None, "vendor.com") is not None:
            failures.append("[semantic_key] blank amount must return None")
        if ledger.find_by_semantic_key("", "INV-A", 10000, "vendor.com") is not None:
            failures.append("[semantic_key] blank plan must return None")
        if ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "") is not None:
            failures.append("[semantic_key] blank sender_domain must return None")
    return failures


def test_semantic_key_rejects_cross_vendor_match() -> list[str]:
    """The Q3 scenario: ABC Plumbing's INV-1023 must NOT match XYZ Cleaning's INV-1023.

    Same plan + invoice number + amount but different sender domains: the new
    sender_domain field is the 4th key element, so the two rows are distinct
    Layer B entries. Each vendor's own lookup hits its own row; a third
    arrival with a third domain finds neither.
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        # ABC Plumbing — INV-1023 for $850 on BCS2707
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1023", amount_cents=85000,
                               sender_domain="abcplumbing.com", now=_fixed_dt()))
        # XYZ Cleaning — same invoice number, same amount, same plan, different vendor
        ledger.upsert(make_row(sha256=H2, plan_norm="BCS2707",
                               invoice_number="INV-1023", amount_cents=85000,
                               sender_domain="xyzcleaning.com", now=_fixed_dt()))

        # Each vendor's own lookup finds its own row.
        abc = ledger.find_by_semantic_key("BCS2707", "INV-1023", 85000, "abcplumbing.com")
        if abc is None or abc.sha256 != H1:
            failures.append(f"[cross-vendor] ABC lookup should return H1, got {abc!r}")
        xyz = ledger.find_by_semantic_key("BCS2707", "INV-1023", 85000, "xyzcleaning.com")
        if xyz is None or xyz.sha256 != H2:
            failures.append(f"[cross-vendor] XYZ lookup should return H2, got {xyz!r}")

        # A third unrelated vendor sending the same invoice number is NOT a duplicate.
        other = ledger.find_by_semantic_key("BCS2707", "INV-1023", 85000, "other-vendor.com")
        if other is not None:
            failures.append(
                f"[cross-vendor] third-vendor lookup should return None, got {other!r}"
            )
    return failures


def test_blank_sender_domain_row_excluded_from_layer_b() -> list[str]:
    """A row inserted by a non-email path (sender_domain="") is NOT indexed in Layer B.

    Mirrors the Step 3/5/6 reality: file-only entry points have no email
    context, so they pass sender_domain="" to make_row. Layer A still catches
    verbatim resends of those rows; Layer B simply doesn't fire.
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="",  # explicit
                               now=_fixed_dt()))

        # Layer A still finds it.
        if ledger.find_by_hash(H1) is None:
            failures.append("[blank domain] Layer A should still find the row")

        # Layer B lookup with the same (plan, inv, amount) but specifying a
        # vendor domain must NOT return the blank-domain row.
        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        if match is not None:
            failures.append(
                f"[blank domain] Layer B should not match a blank-domain row, got {match!r}"
            )

        # Lookup with blank domain also returns None (the blank-domain rule on
        # the LOOKUP side, not just the row side).
        match = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "")
        if match is not None:
            failures.append(
                f"[blank domain] blank-domain lookup should return None, got {match!r}"
            )
    return failures


def test_legacy_csv_without_sender_domain_column_parses() -> list[str]:
    """A CSV written before the sender_domain column existed must still load.

    Uses the .get(field, "") pattern from aggregation_ledger so old rows
    deserialize with an empty sender_domain — they keep Layer A coverage
    and are excluded from Layer B (which is the right behaviour: we don't
    know which vendor they came from).
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        # Pre-sender_domain HEADER: 10 columns, no sender_domain.
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
        if len(rows) != 1:
            failures.append(f"[legacy csv] expected 1 row, got {len(rows)}")
            return failures
        row = rows[0]
        if row.sha256 != H1:
            failures.append(f"[legacy csv] sha mangled: {row.sha256!r}")
        if row.sender_domain != "":
            failures.append(
                f"[legacy csv] sender_domain should default to empty, got {row.sender_domain!r}"
            )
        if row.plan_norm != "BCS2707" or row.invoice_number != "INV-OLD":
            failures.append(f"[legacy csv] other fields scrambled: {row!r}")
        if row.amount_cents != 12345:
            failures.append(f"[legacy csv] amount_cents wrong: {row.amount_cents!r}")
        if row.current_stage != "archived":
            failures.append(f"[legacy csv] stage wrong: {row.current_stage!r}")

        # Layer A finds it.
        if ledger.find_by_hash(H1) is None:
            failures.append("[legacy csv] Layer A should still find legacy row")
        # Layer B does NOT (sender_domain is blank).
        match = ledger.find_by_semantic_key("BCS2707", "INV-OLD", 12345, "anyvendor.com")
        if match is not None:
            failures.append(
                f"[legacy csv] Layer B must not match a legacy blank-domain row, got {match!r}"
            )
    return failures


def test_increment_dup_count_roundtrips() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        # Initial row has no dup yet.
        initial = ledger.find_by_hash(H1)
        if initial is None or initial.last_dup_date != "":
            failures.append(f"[dup_count] fresh row should have empty last_dup_date, got {initial.last_dup_date if initial else None!r}")

        ledger.increment_dup_count(H1, now=_fixed_dt(day=15))
        ledger.increment_dup_count(H1, now=_fixed_dt(day=16))

        row = ledger.find_by_hash(H1)
        if row is None or row.dup_count != 2:
            failures.append(f"[dup_count] expected 2, got {row.dup_count if row else None}")
        if row and row.last_seen_date != "2026-05-16":
            failures.append(f"[dup_count] last_seen_date should be 2026-05-16, got {row.last_seen_date}")
        if row and row.last_dup_date != "2026-05-16":
            failures.append(f"[dup_count] last_dup_date should be 2026-05-16, got {row.last_dup_date}")
        if row and row.first_seen_date != "2026-05-11":
            failures.append(f"[dup_count] first_seen_date should be unchanged: {row.first_seen_date}")

        # Round-trip through disk
        ledger2 = load(path)
        row2 = ledger2.find_by_hash(H1)
        if row2 is None or row2.dup_count != 2 or row2.last_seen_date != "2026-05-16":
            failures.append(f"[dup_count round trip] lost across reload: {row2!r}")
        if row2 and row2.last_dup_date != "2026-05-16":
            failures.append(f"[dup_count round trip] last_dup_date lost: {row2.last_dup_date!r}")
    return failures


def test_update_stage_roundtrips() -> list[str]:
    failures: list[str] = []
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
        if row is None:
            failures.append("[update_stage] row vanished")
            return failures
        if row.current_stage != "archived":
            failures.append(f"[update_stage] stage {row.current_stage} != 'archived'")
        if "Strata_Plans" not in row.archive_path:
            failures.append(f"[update_stage] archive_path not set: {row.archive_path}")

        # Reload
        ledger2 = load(path)
        row2 = ledger2.find_by_hash(H1)
        if row2 is None or row2.current_stage != "archived" or "Strata_Plans" not in row2.archive_path:
            failures.append(f"[update_stage round trip] lost across reload: {row2!r}")

    return failures


def test_corrupted_row_raises() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        # amount_cents = "not_an_int" should trip parsing. Column order matches
        # HEADER: first_seen, sha, plan, inv, amount, sender_domain, archive,
        # stage, last_seen, dup_count.
        path.write_text(
            ",".join(HEADER) + "\n"
            "2026-05-11," + H1 + ",BCS2707,INV-1,not_an_int,vendor.com,,manager_queue,2026-05-11,0\n",
            encoding="utf-8",
        )
        try:
            load(path)
        except ValueError:
            return failures
        except Exception as exc:
            failures.append(f"[corrupted] expected ValueError, got {type(exc).__name__}: {exc}")
            return failures
        failures.append("[corrupted] expected ValueError, got no exception")
    return failures


def test_empty_sha_in_loaded_row_raises() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        path.write_text(
            ",".join(HEADER) + "\n"
            "2026-05-11,,BCS2707,INV-1,10000,vendor.com,,manager_queue,2026-05-11,0\n",
            encoding="utf-8",
        )
        try:
            load(path)
        except ValueError:
            return failures
        except Exception as exc:
            failures.append(f"[empty sha] expected ValueError, got {type(exc).__name__}: {exc}")
            return failures
        failures.append("[empty sha] expected ValueError, got no exception")
    return failures


def test_atomic_rewrite_keeps_prior_on_crash() -> list[str]:
    """If the atomic write raises mid-rewrite, the prior good file must survive.

    `atomic_write_bytes` writes to a tmp file then `os.replace`s. We simulate a
    crash by patching `os.replace` to raise before the replace happens — the
    target file should remain unchanged.
    """
    import os as _os

    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-1", amount_cents=10000,
                               now=_fixed_dt(day=11)))

        prior_bytes = path.read_bytes()

        # Now try a second upsert with the os.replace patched to raise.
        try:
            with patch("os.replace", side_effect=OSError("simulated crash")):
                ledger.upsert(make_row(sha256=H2, plan_norm="LMS4193",
                                       invoice_number="INV-2", amount_cents=99999,
                                       now=_fixed_dt(day=12)))
            failures.append("[atomic] expected OSError to propagate")
        except OSError:
            pass
        except Exception as exc:
            failures.append(f"[atomic] expected OSError, got {type(exc).__name__}: {exc}")

        # The prior good file must still be intact.
        after_bytes = path.read_bytes()
        if after_bytes != prior_bytes:
            failures.append(
                f"[atomic] prior good file was corrupted by failed write: "
                f"prior={len(prior_bytes)} bytes, after={len(after_bytes)} bytes"
            )

        # Make sure no orphan tmp file was left behind.
        tmp_count = sum(1 for p in path.parent.glob("invoice_fingerprints.csv.tmp.*"))
        if tmp_count != 0:
            failures.append(f"[atomic] expected 0 orphan tmp files, found {tmp_count}")
    return failures


def test_transactional_rmw_no_lost_updates() -> list[str]:
    """Two `Ledger` instances pointed at the same file must not lose updates.

    Simulates the race: Process A loads ledger (1 row), Process B independently
    upserts a different row. Process A then upserts a third row. Both rows
    from B and A must survive on disk because mutations re-read disk under
    the lock.
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"

        # Seed disk with one row.
        seed = load(path)
        seed.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                             invoice_number="INV-1", amount_cents=10000,
                             now=_fixed_dt(day=11)))

        # Both processes load — both have the same single-row in-memory view.
        process_a = load(path)
        process_b = load(path)

        # Process B inserts a row directly to disk via its own ledger.
        process_b.upsert(make_row(sha256=H2, plan_norm="LMS4193",
                                  invoice_number="INV-2", amount_cents=25000,
                                  now=_fixed_dt(day=11)))

        # Process A's in-memory view still shows only H1.
        if process_a.find_by_hash(H2) is not None:
            failures.append("[rmw] process_a should not see H2 in-memory yet (stale)")

        # Now Process A inserts H3. Transactional RMW should re-read disk and
        # find H2 already there, so the final disk state must contain H1, H2, AND H3.
        process_a.upsert(make_row(sha256=H3, plan_norm="VR9999",
                                  invoice_number="INV-3", amount_cents=99999,
                                  now=_fixed_dt(day=11)))

        # Reload from disk.
        fresh = load(path)
        if len(fresh.all_rows()) != 3:
            failures.append(f"[rmw] expected 3 rows on disk, got {len(fresh.all_rows())}")
            return failures
        seen = {r.sha256 for r in fresh.all_rows()}
        for h, label in [(H1, "H1"), (H2, "H2"), (H3, "H3")]:
            if h not in seen:
                failures.append(f"[rmw] expected {label} ({h[:12]}...) preserved on disk")
    return failures


def test_overridden_row_excluded_from_semantic_index() -> list[str]:
    """A row at stage=overridden must not block new semantic-key matches via find_by_semantic_key."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com",
                               now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "overridden")

        # Regular find_by_semantic_key must NOT return the overridden row.
        if ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com") is not None:
            failures.append("[overridden] semantic lookup should skip overridden rows")

        # find_overridden_by_semantic_key DOES surface it.
        row = ledger.find_overridden_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        if row is None or row.sha256 != H1:
            failures.append(f"[overridden] find_overridden_by_semantic_key should return H1, got {row!r}")
    return failures


def test_consume_override_and_insert() -> list[str]:
    """Override + Layer B regen: old row -> superseded, new row inserted."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)

        # Original PDF, marked overridden.
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               sender_domain="vendor.com",
                               now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "overridden")

        # Regenerated PDF arrives — same semantic key, different sha.
        new_row = make_row(sha256=H2, plan_norm="BCS2707",
                           invoice_number="INV-A", amount_cents=10000,
                           sender_domain="vendor.com",
                           now=_fixed_dt(day=12))
        ledger.consume_override_and_insert(old_sha256=H1, new_row=new_row)

        # Disk state: H1 is superseded; H2 is the new active row.
        h1 = ledger.find_by_hash(H1)
        h2 = ledger.find_by_hash(H2)
        if h1 is None or h1.current_stage != "superseded":
            failures.append(f"[consume_override] H1 should be superseded, got {h1!r}")
        if h2 is None or h2.current_stage != "manager_queue":
            failures.append(f"[consume_override] H2 should be active manager_queue, got {h2!r}")

        # Semantic index must now point at H2 (active), not H1 (superseded).
        sem = ledger.find_by_semantic_key("BCS2707", "INV-A", 10000, "vendor.com")
        if sem is None or sem.sha256 != H2:
            failures.append(f"[consume_override] semantic key should point at H2, got {sem!r}")

        # Old sha is no longer surfaced as an override.
        if ledger.find_overridden_by_hash(H1) is not None:
            failures.append("[consume_override] H1 should no longer be 'overridden'")

        # Round-trip through disk.
        reloaded = load(path)
        if reloaded.find_by_hash(H1).current_stage != "superseded":
            failures.append("[consume_override round-trip] H1 superseded lost across reload")
        if reloaded.find_by_hash(H2).current_stage != "manager_queue":
            failures.append("[consume_override round-trip] H2 lost across reload")
    return failures


def test_consume_override_rejects_non_overridden_row() -> list[str]:
    """consume_override_and_insert must verify the old row's stage is still
    `overridden` under the lock. Two concurrent consumers of the same
    override row: only the first should succeed; the second must raise.
    """
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        ledger.update_stage(H1, "overridden")

        # First consume — succeeds.
        new1 = make_row(sha256=H2, plan_norm="BCS2707",
                        invoice_number="INV-A", amount_cents=10000,
                        now=_fixed_dt(day=12))
        ledger.consume_override_and_insert(old_sha256=H1, new_row=new1)

        # Second consume of the SAME old sha — must raise ValueError because
        # H1 is now `superseded`, not `overridden`.
        new2 = make_row(sha256=H3, plan_norm="BCS2707",
                        invoice_number="INV-A", amount_cents=10000,
                        now=_fixed_dt(day=12))
        try:
            ledger.consume_override_and_insert(old_sha256=H1, new_row=new2)
        except ValueError:
            pass
        except Exception as exc:
            failures.append(
                f"[double consume] expected ValueError, got {type(exc).__name__}: {exc}"
            )
        else:
            failures.append(
                "[double consume] expected ValueError on second consume of same override"
            )
    return failures


def test_consume_override_missing_old_sha_raises() -> list[str]:
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        try:
            ledger.consume_override_and_insert(
                old_sha256="z" * 64,
                new_row=make_row(sha256=H2, plan_norm="BCS2707",
                                 invoice_number="INV-A", amount_cents=10000,
                                 now=_fixed_dt(day=12)),
            )
        except KeyError:
            return failures
        except Exception as exc:
            failures.append(f"[consume missing] expected KeyError, got {type(exc).__name__}: {exc}")
            return failures
        failures.append("[consume missing] expected KeyError, got no exception")
    return failures


def test_increment_dup_count_missing_sha_raises() -> list[str]:
    """KeyError surfaces if the sha disappears between read and mutation."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        try:
            ledger.increment_dup_count("z" * 64)
        except KeyError:
            return failures
        except Exception as exc:
            failures.append(f"[inc missing] expected KeyError, got {type(exc).__name__}: {exc}")
            return failures
        failures.append("[inc missing] expected KeyError, got no exception")
    return failures


def test_flush_failure_leaves_memory_unchanged() -> list[str]:
    """If atomic_write_bytes raises mid-mutation, in-memory state must be untouched."""
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ledger.csv"
        ledger = load(path)
        ledger.upsert(make_row(sha256=H1, plan_norm="BCS2707",
                               invoice_number="INV-A", amount_cents=10000,
                               now=_fixed_dt(day=11)))
        prior_rows = ledger.all_rows()

        # Force the next write to raise.
        try:
            with patch("os.replace", side_effect=OSError("simulated crash")):
                ledger.upsert(make_row(sha256=H2, plan_norm="BCS2707",
                                       invoice_number="INV-B", amount_cents=20000,
                                       now=_fixed_dt(day=11)))
            failures.append("[flush fail] expected OSError to propagate")
        except OSError:
            pass
        except Exception as exc:
            failures.append(f"[flush fail] expected OSError, got {type(exc).__name__}: {exc}")

        # In-memory state must be exactly what it was before the failed mutation.
        after_rows = ledger.all_rows()
        if [(r.sha256, r.current_stage) for r in after_rows] != [(r.sha256, r.current_stage) for r in prior_rows]:
            failures.append(f"[flush fail] in-memory state diverged from prior: prior={prior_rows}, after={after_rows}")

        # find_by_hash for the failed-insert sha must return None — the row
        # was never committed.
        if ledger.find_by_hash(H2) is not None:
            failures.append("[flush fail] failed-write sha should not be in in-memory index")
    return failures


def main() -> int:
    all_failures: list[str] = []
    for label, fn in [
        ("missing file -> empty ledger", test_missing_file_returns_empty),
        ("upsert writes header + row", test_upsert_writes_header_and_row),
        ("upsert overwrites same sha", test_upsert_overwrites_same_sha),
        ("find_by_hash / semantic_key", test_find_by_hash_and_semantic_key),
        ("semantic_key rejects cross-vendor false-positive (Q3 fix)", test_semantic_key_rejects_cross_vendor_match),
        ("blank sender_domain row excluded from Layer B", test_blank_sender_domain_row_excluded_from_layer_b),
        ("legacy CSV without sender_domain column parses", test_legacy_csv_without_sender_domain_column_parses),
        ("increment_dup_count round-trip", test_increment_dup_count_roundtrips),
        ("update_stage round-trip", test_update_stage_roundtrips),
        ("corrupted row raises", test_corrupted_row_raises),
        ("empty sha raises on load", test_empty_sha_in_loaded_row_raises),
        ("atomic rewrite keeps prior on crash", test_atomic_rewrite_keeps_prior_on_crash),
        ("transactional RMW: no lost updates across instances", test_transactional_rmw_no_lost_updates),
        ("overridden row excluded from semantic index", test_overridden_row_excluded_from_semantic_index),
        ("consume_override_and_insert (Layer B regen)", test_consume_override_and_insert),
        ("consume_override rejects non-overridden row (one-shot enforcement)", test_consume_override_rejects_non_overridden_row),
        ("consume_override missing old sha raises", test_consume_override_missing_old_sha_raises),
        ("increment_dup_count missing sha raises", test_increment_dup_count_missing_sha_raises),
        ("flush failure leaves memory unchanged", test_flush_failure_leaves_memory_unchanged),
    ]:
        fails = fn()
        status = "OK  " if not fails else "FAIL"
        print(f"{status}[{label}] ({len(fails)} failure{'s' if len(fails) != 1 else ''})")
        all_failures.extend(fails)

    if all_failures:
        print("\nFAILURES:")
        for f in all_failures:
            print(f"  - {f}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
