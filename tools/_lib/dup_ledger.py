"""Duplicate-detection ledger — one row per unique invoice fingerprint, forever.

Mirrors the storage pattern of `aggregation_ledger.py` (portalocker + Excel-
friendly CSV under `_state/`) but with **upsert-in-place** semantics rather
than append-only. One row per unique fingerprint identified by its sha256;
state transitions (manager_queue -> ap_queue -> archived) update the row's
`current_stage`, and duplicate detections increment `dup_count`.

Lean schema so the file stays Excel-openable at high volume:

  first_seen_date, sha256, plan_norm, invoice_number, amount_cents,
  sender_domain, archive_path, current_stage, last_seen_date, dup_count,
  last_dup_date

`last_dup_date` is the most recent date `dup_count` was incremented (empty
string when no duplicate has ever been detected for this fingerprint).
Step 6's daily summary email filters rows by `last_dup_date == today`.

`sender_domain` is the lowercased domain of the email's `From:` address.
Rows inserted by non-email entry points (Steps 3/5/6: manual drops, retries)
carry an empty `sender_domain` and are excluded from the Layer B semantic
index — they still get Layer A (sha256) coverage. Legacy rows in CSVs
written before this column existed parse as `sender_domain=""` and behave
the same way.

Sizing budget at the operator's 45–60 invoices/day volume:
  Year 1:   ~15K rows × ~150 bytes  =  ~2 MB
  Year 5:   ~75K rows                ~11 MB
  Year 10: ~150K rows                ~22 MB

Audit details (subject lines, message IDs, per-decision logs) live in the
per-step daily logs which already rotate. The fingerprint file is purely the
dedup key + pointer to the original.

## Concurrency model (transactional read-modify-write)

Mutations are **transactional**: every mutating method acquires the
portalocker lock, **re-reads the current state from disk**, applies the
mutation to that fresh state, writes the whole CSV atomically, then
releases the lock. Only after a successful write are the in-memory
caches updated.

This eliminates the lost-update race where two Step processes load the
ledger at 06:00 and 06:20, both mutate independently, and the second
flush overwrites the first's appends. With transactional RMW, even if
Process A has a stale in-memory snapshot, Process A's mutation re-reads
the disk-of-record at the moment of the mutation and applies the change
on top of whatever Process B wrote.

Read operations (`find_by_hash`, `find_by_semantic_key`) consult the
in-memory cache and may return slightly stale data. That is OK because:
  - The caller's pattern is "check, then mutate". The mutate step
    re-reads under the lock, so a stale "I don't see this" check is
    corrected at mutation time.
  - The same-process serial mutations always commit fresh state back
    to memory, so subsequent reads within the same run reflect both
    that run's writes and any cross-process writes seen at the most
    recent mutation.

Corrupted-row handling matches `aggregation_ledger.load()`: raise
ValueError on the first malformed row and halt the step. A corrupted
ledger MUST NOT be silently treated as empty — that would let every
already-archived invoice re-enter the pipeline.
"""

from __future__ import annotations

import csv
import datetime as _dt
import io
from dataclasses import dataclass, replace
from pathlib import Path

import portalocker

from tools._lib import paths, safe_io

HEADER: list[str] = [
    "first_seen_date",
    "sha256",
    "plan_norm",
    "invoice_number",
    "amount_cents",
    "sender_domain",
    "archive_path",
    "archive_sha256",
    "current_stage",
    "last_seen_date",
    "dup_count",
    "last_dup_date",
]

# Stage values the rest of the pipeline writes. `quarantined` and `overridden`
# are reserved for the operator-tools side (dup_override.py). `superseded`
# marks an overridden row whose Layer B semantic key was matched by a new
# arrival with different bytes — see `consume_override_and_insert()`.
STAGES: frozenset[str] = frozenset({
    "intake",
    "manager_queue",
    "ap_queue",
    "archived",
    "quarantined",
    "overridden",
    "superseded",
})


@dataclass
class FingerprintRow:
    first_seen_date: str
    sha256: str
    plan_norm: str
    invoice_number: str
    amount_cents: int | None
    archive_path: str
    current_stage: str
    last_seen_date: str
    dup_count: int
    last_dup_date: str = ""
    sender_domain: str = ""
    # SHA-256 of the on-disk archive bytes (post-flatten). The `sha256`
    # field above is the chain SHA carried through intake -> AP -> archive
    # so cross-stage rows still join; `archive_sha256` is what
    # tools/dup_reconcile.py needs to recognise a flattened archive on
    # disk, since the flatten changes the byte hash. Empty for rows
    # written by pre-0.12.1 code or by stages that don't produce a
    # flattened archive.
    archive_sha256: str = ""

    def to_csv_row(self) -> list:
        return [
            self.first_seen_date,
            self.sha256,
            self.plan_norm,
            self.invoice_number,
            "" if self.amount_cents is None else str(self.amount_cents),
            self.sender_domain,
            self.archive_path,
            self.archive_sha256,
            self.current_stage,
            self.last_seen_date,
            str(self.dup_count),
            self.last_dup_date,
        ]


def _today_str(now: _dt.datetime | None = None) -> str:
    if now is None:
        try:
            from zoneinfo import ZoneInfo
            now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
        except Exception:
            now = _dt.datetime.now()
    return now.date().isoformat()


def make_row(
    *,
    sha256: str,
    plan_norm: str,
    invoice_number: str = "",
    amount_cents: int | None = None,
    sender_domain: str = "",
    current_stage: str = "manager_queue",
    archive_path: str = "",
    archive_sha256: str = "",
    now: _dt.datetime | None = None,
) -> FingerprintRow:
    """Build a fresh `FingerprintRow` for a never-before-seen invoice.

    `sender_domain` defaults to "" so non-email entry points (Steps 3/5/6)
    don't need to thread anything new; their rows simply skip Layer B and
    rely on Layer A. Email-driven Step 1 passes the real domain.
    """
    today = _today_str(now)
    return FingerprintRow(
        first_seen_date=today,
        sha256=sha256,
        plan_norm=plan_norm,
        invoice_number=invoice_number,
        amount_cents=amount_cents,
        sender_domain=sender_domain,
        archive_path=archive_path,
        current_stage=current_stage,
        last_seen_date=today,
        dup_count=0,
        last_dup_date="",
        archive_sha256=archive_sha256,
    )


def _build_indexes(rows: list[FingerprintRow]) -> tuple[dict[str, int], dict[tuple[str, str, int, str], int]]:
    """Build (by_hash, by_semantic) indexes from a row list.

    Layer B (by_semantic) prefers the earliest first-seen row when multiple
    rows share the same (plan, invoice_number, amount_cents, sender_domain)
    tuple. Rows are excluded from the semantic index when:
      - Any of plan_norm / invoice_number / amount_cents / sender_domain
        is blank (legacy rows, non-email entry points, malformed extracts),
      - Or `current_stage` is `overridden` / `superseded` (consumed rows).
    Excluded rows still appear in `by_hash` and so still get Layer A coverage.
    """
    by_hash: dict[str, int] = {}
    by_semantic: dict[tuple[str, str, int, str], int] = {}
    for i, r in enumerate(rows):
        by_hash[r.sha256] = i
        if (
            r.invoice_number
            and r.amount_cents is not None
            and r.plan_norm
            and r.sender_domain
            and r.current_stage not in ("overridden", "superseded")
        ):
            key = (r.plan_norm.upper(), r.invoice_number, r.amount_cents, r.sender_domain.lower())
            if key not in by_semantic:
                by_semantic[key] = i
    return by_hash, by_semantic


def _parse_csv_rows(path: Path) -> list[FingerprintRow]:
    """Read the ledger CSV from `path` and return parsed rows.

    Returns `[]` for missing or zero-byte files. Raises `ValueError` on a
    malformed row or empty sha256.
    """
    rows: list[FingerprintRow] = []
    if not path.exists() or path.stat().st_size == 0:
        return rows
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            try:
                amount_raw = (raw.get("amount_cents") or "").strip()
                amount_cents = int(amount_raw) if amount_raw else None
                dup_raw = (raw.get("dup_count") or "0").strip()
                rows.append(FingerprintRow(
                    first_seen_date=(raw.get("first_seen_date") or "").strip(),
                    sha256=(raw.get("sha256") or "").strip(),
                    plan_norm=(raw.get("plan_norm") or "").strip(),
                    invoice_number=(raw.get("invoice_number") or "").strip(),
                    amount_cents=amount_cents,
                    sender_domain=(raw.get("sender_domain") or "").strip().lower(),
                    archive_path=(raw.get("archive_path") or "").strip(),
                    current_stage=(raw.get("current_stage") or "").strip(),
                    last_seen_date=(raw.get("last_seen_date") or "").strip(),
                    dup_count=int(dup_raw) if dup_raw else 0,
                    last_dup_date=(raw.get("last_dup_date") or "").strip(),
                    archive_sha256=(raw.get("archive_sha256") or "").strip(),
                ))
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"invoice_fingerprints CSV {path} has a malformed row {raw!r}: {exc}"
                ) from exc
    for r in rows:
        if not r.sha256:
            raise ValueError(
                f"invoice_fingerprints CSV {path} has a row with an empty sha256"
            )
    return rows


def _serialize_csv(rows: list[FingerprintRow]) -> bytes:
    """Return CSV bytes (header + every row)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for r in rows:
        w.writerow(r.to_csv_row())
    return buf.getvalue().encode("utf-8")


class Ledger:
    """Transactional, file-backed ledger of invoice fingerprints.

    Callers `load()` once at the start of a run, query via `find_by_hash` /
    `find_by_semantic_key` (in-memory, possibly stale), and call mutating
    methods (`upsert`, `increment_dup_count`, `update_stage`,
    `consume_override_and_insert`) for each invoice outcome.

    Mutations are transactional: each mutating method acquires the lock,
    re-reads disk, applies the change to the fresh state, writes back, and
    releases the lock — only then are the in-memory caches updated. If the
    write raises, in-memory state is unchanged so the caller can retry or
    fall through with a coherent view.
    """

    def __init__(self, rows: list[FingerprintRow], path: Path):
        self.path = path
        self._rows: list[FingerprintRow] = list(rows)
        self._by_hash, self._by_semantic = _build_indexes(self._rows)

    # --- Reads (in-memory, possibly stale by one mutation cycle) ---

    def find_by_hash(self, sha256: str) -> FingerprintRow | None:
        idx = self._by_hash.get(sha256)
        if idx is None:
            return None
        return self._rows[idx]

    def find_by_semantic_key(
        self,
        plan_norm: str,
        invoice_number: str,
        amount_cents: int | None,
        sender_domain: str,
    ) -> FingerprintRow | None:
        """Layer B lookup. Returns None unless ALL four fields are non-blank.

        `sender_domain` is the lowercased email domain captured at intake.
        A blank `sender_domain` (file-only entry point, malformed sender)
        causes Layer B to no-op, matching the existing treatment of blank
        plan_norm / invoice_number / amount_cents.

        Excludes rows whose `current_stage` is `overridden` or `superseded` —
        those rows are not active duplicate-blockers (the operator has
        explicitly let them through).
        """
        if not plan_norm or not invoice_number or amount_cents is None or not sender_domain:
            return None
        key = (plan_norm.upper(), invoice_number, amount_cents, sender_domain.lower())
        idx = self._by_semantic.get(key)
        if idx is None:
            return None
        return self._rows[idx]

    def find_overridden_by_hash(self, sha256: str) -> FingerprintRow | None:
        """Return the row only if it has stage=overridden. Used by callers
        that want to detect "Layer A hit on a one-shot override".
        """
        row = self.find_by_hash(sha256)
        if row is not None and row.current_stage == "overridden":
            return row
        return None

    def find_overridden_by_semantic_key(
        self,
        plan_norm: str,
        invoice_number: str,
        amount_cents: int | None,
        sender_domain: str,
    ) -> FingerprintRow | None:
        """Find an `overridden`-stage row matching the semantic key.

        The regular `find_by_semantic_key` excludes overridden rows from
        the active index; this method does a linear scan to surface them
        for the override-consume path. Linear scan is fine — overridden
        rows are rare (operator-initiated) and the row count is small.

        Like `find_by_semantic_key`, returns None when any field is blank.
        """
        if not plan_norm or not invoice_number or amount_cents is None or not sender_domain:
            return None
        key = (plan_norm.upper(), invoice_number, amount_cents, sender_domain.lower())
        for r in self._rows:
            if r.current_stage != "overridden":
                continue
            if (
                r.plan_norm.upper() == key[0]
                and r.invoice_number == key[1]
                and r.amount_cents == key[2]
                and r.sender_domain.lower() == key[3]
            ):
                return r
        return None

    def all_rows(self) -> list[FingerprintRow]:
        """Snapshot of every row, in insertion order."""
        return list(self._rows)

    # --- Mutations (transactional under the file lock) ---

    def upsert(self, row: FingerprintRow) -> FingerprintRow:
        """Insert if new sha256, otherwise replace the existing row in place.

        Transactional: re-reads disk under the lock and applies the upsert
        to the fresh state. Same-sha replace overwrites the prior row's
        fields with the new row's values.
        """
        def mutate(rows: list[FingerprintRow]) -> tuple[list[FingerprintRow], FingerprintRow]:
            for i, existing in enumerate(rows):
                if existing.sha256 == row.sha256:
                    rows[i] = row
                    return rows, row
            rows.append(row)
            return rows, row

        return self._transact(mutate)

    def increment_dup_count(
        self,
        sha256: str,
        now: _dt.datetime | None = None,
    ) -> FingerprintRow:
        """Bump `dup_count` + `last_seen_date` + `last_dup_date`. Raises
        KeyError if sha256 isn't in the ledger when the mutation actually
        runs (after re-reading disk under the lock).
        """
        today = _today_str(now)

        def mutate(rows: list[FingerprintRow]) -> tuple[list[FingerprintRow], FingerprintRow]:
            for i, existing in enumerate(rows):
                if existing.sha256 == sha256:
                    updated = replace(
                        existing,
                        dup_count=existing.dup_count + 1,
                        last_seen_date=today,
                        last_dup_date=today,
                    )
                    rows[i] = updated
                    return rows, updated
            raise KeyError(f"sha256 {sha256[:12]}... not in ledger")

        return self._transact(mutate)

    def update_stage(
        self,
        sha256: str,
        new_stage: str,
        *,
        archive_path: str | None = None,
        archive_sha256: str | None = None,
        now: _dt.datetime | None = None,
    ) -> FingerprintRow:
        """Move an existing row to a new lifecycle stage. Optional
        `archive_path` and `archive_sha256` updates (omit to leave existing
        values). Raises KeyError if sha256 isn't in the ledger when the
        mutation runs.
        """
        if new_stage not in STAGES:
            raise ValueError(f"unknown stage {new_stage!r}")
        today = _today_str(now)

        def mutate(rows: list[FingerprintRow]) -> tuple[list[FingerprintRow], FingerprintRow]:
            for i, existing in enumerate(rows):
                if existing.sha256 == sha256:
                    updated = replace(
                        existing,
                        current_stage=new_stage,
                        last_seen_date=today,
                        archive_path=(
                            archive_path if archive_path is not None else existing.archive_path
                        ),
                        archive_sha256=(
                            archive_sha256 if archive_sha256 is not None else existing.archive_sha256
                        ),
                    )
                    rows[i] = updated
                    return rows, updated
            raise KeyError(f"sha256 {sha256[:12]}... not in ledger")

        return self._transact(mutate)

    def consume_override_and_insert(
        self,
        old_sha256: str,
        new_row: FingerprintRow,
    ) -> FingerprintRow:
        """Atomically retire the old overridden row and insert a new row.

        Used when a Layer B match (different bytes, same semantic key) hits
        an `overridden` row. The override is one-shot, so the old row is
        marked `superseded` and the new row is inserted with whatever stage
        the caller picked. After this transaction, the semantic key is
        canonically held by `new_row` and the old sha is still in the
        ledger as an audit trail (`superseded` rows are excluded from the
        active semantic index).

        Raises:
          KeyError    — if `old_sha256` isn't found in the ledger at
                        mutation time.
          ValueError  — if the old row's `current_stage` is no longer
                        `overridden` at mutation time (override already
                        consumed, by this run or another process). The
                        caller should fall back to a plain `upsert`.

        If `new_row.sha256` is already in the ledger when the mutation
        runs, the new row replaces it (upsert semantics for the new sha).
        """
        def mutate(rows: list[FingerprintRow]) -> tuple[list[FingerprintRow], FingerprintRow]:
            old_idx = None
            for i, existing in enumerate(rows):
                if existing.sha256 == old_sha256:
                    old_idx = i
                    break
            if old_idx is None:
                raise KeyError(f"sha256 {old_sha256[:12]}... not in ledger")
            if rows[old_idx].current_stage != "overridden":
                raise ValueError(
                    f"sha256 {old_sha256[:12]}... is no longer overridden "
                    f"(current_stage={rows[old_idx].current_stage!r}); "
                    f"override was already consumed by another process or call"
                )
            rows[old_idx] = replace(rows[old_idx], current_stage="superseded")

            # Insert (or replace) the new_row by its own sha256.
            for i, existing in enumerate(rows):
                if existing.sha256 == new_row.sha256:
                    rows[i] = new_row
                    return rows, new_row
            rows.append(new_row)
            return rows, new_row

        return self._transact(mutate)

    # --- Transactional plumbing ---

    def _transact(
        self,
        mutate,  # callable: (rows) -> (rows, result)
    ) -> FingerprintRow:
        """Run a mutation under the file lock with read-modify-write semantics.

        1. Acquire the lock.
        2. Re-read the disk state.
        3. Apply `mutate` to the fresh state.
        4. Serialize and atomically write back to disk.
        5. Release the lock.
        6. Commit the new state to in-memory caches.

        If `mutate` raises (e.g. KeyError because the target sha isn't in
        the fresh disk state), the lock is released, no write happens, and
        the in-memory caches are unchanged. If the disk write raises, the
        in-memory caches are unchanged so the caller can retry coherently.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.path) + ".lock", timeout=30):
            fresh = _parse_csv_rows(self.path)
            new_rows, result = mutate(fresh)
            data = _serialize_csv(new_rows)
            safe_io.atomic_write_bytes(self.path, data)
            committed_rows = new_rows
        # Commit to memory only after the lock is released and the write
        # succeeded. Rebuild indexes from the fresh post-mutation state.
        self._rows = committed_rows
        self._by_hash, self._by_semantic = _build_indexes(self._rows)
        return result


def load(path: Path | None = None) -> Ledger:
    """Read the ledger from disk. Returns an empty ledger if the file is missing.

    Raises `ValueError` on a malformed row so corruption is loud and the
    operator triages — silent-treat-as-empty would re-enter every previously
    archived invoice into the pipeline.
    """
    if path is None:
        path = paths.invoice_fingerprints_csv()
    rows = _parse_csv_rows(path)
    return Ledger(rows, path)
