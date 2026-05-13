"""Operator CLI: cross-check on-disk PDFs against the duplicate-detection ledger.

Walks every folder the pipeline writes to, hashes each PDF, and reports:

  - **Orphans**: PDFs on disk with no ledger row. Means Step N ran into a
    ledger-write failure after the file write succeeded, or the operator
    manually placed files outside the pipeline.
  - **Stale rows**: ledger rows whose `archive_path` points at a file that no
    longer exists on disk. Usually means someone manually moved/deleted an
    archive.
  - **Multi-arrival ledger rows**: rows where `dup_count > 0` — informational
    summary so the operator can spot vendors who repeatedly resend.

Usage:
  python tools/dup_reconcile.py                # report to stdout
  python tools/dup_reconcile.py --tsv out.tsv  # also write detailed TSV report

This script is on-demand only. It does NOT acquire a step lockfile, does NOT
add a row to daily_summary.csv, and does NOT mutate the ledger.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools._lib import dup_fingerprint, dup_ledger, paths
from tools._lib.xls import load_plans, unique_aps, unique_managers


def _scan_folder(folder: Path) -> list[tuple[Path, str]]:
    """Hash every *.pdf in `folder` (non-recursive). Returns (path, sha256) pairs.

    Skips files whose name starts with `Processed -` or `Processed-` — those
    are the system's own done-markers and would double-count.
    """
    out: list[tuple[Path, str]] = []
    if not folder.exists():
        return out
    for p in sorted(folder.glob("*.pdf")):
        n = p.name.lower().lstrip()
        if n.startswith("processed -") or n.startswith("processed-"):
            continue
        try:
            sha = dup_fingerprint.sha256_of(p.read_bytes())
        except Exception as exc:
            print(f"WARN: could not hash {p}: {exc}", file=sys.stderr)
            continue
        out.append((p, sha))
    return out


def _find_orphan_tmps(folders: list[Path]) -> list[Path]:
    """Surface `*.tmp.<pid>*` files left behind by interrupted atomic writes.

    `tools/_lib/safe_io.atomic_write_bytes` writes to <path>.tmp.<pid> then
    `os.replace()`s into the final name. A crash between those two steps
    leaves the tmp file orphaned. Usually harmless, but worth flagging
    after a power loss or kill -9.
    """
    found: list[Path] = []
    for folder in folders:
        if not folder.exists():
            continue
        for p in folder.iterdir():
            if not p.is_file():
                continue
            # safe_io tmp suffix is ".tmp.<pid>" — match anything that
            # looks like atomic-write debris.
            if ".tmp." in p.name:
                found.append(p)
    return sorted(found)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-check on-disk PDFs against the duplicate-detection ledger.",
    )
    parser.add_argument(
        "--tsv",
        type=Path,
        help="Optional TSV path for the detailed report (orphans + stale rows).",
    )
    args = parser.parse_args(argv)

    try:
        ledger = dup_ledger.load()
    except ValueError as exc:
        print(f"ERROR: duplicate-detection ledger is corrupted: {exc}", file=sys.stderr)
        return 1

    # Build hash -> ledger row lookups for fast checking. `by_hash` keys on
    # the chain SHA (intake -> AP -> archive); `by_archive_hash` keys on the
    # post-flatten archive SHA introduced in 0.12.1. An on-disk PDF matches
    # the ledger if either lookup hits — archived files hash to the second.
    by_hash = {r.sha256: r for r in ledger.all_rows()}
    by_archive_hash = {
        r.archive_sha256: r for r in ledger.all_rows() if r.archive_sha256
    }

    # Discover every folder we should scan. Load the snapshot only for the
    # plan/manager/AP list — we don't refresh it here.
    try:
        snapshot = paths.strataplan_snapshot_xlsx()
        rows = load_plans(snapshot)
    except Exception as exc:
        print(f"ERROR: could not load snapshot {snapshot}: {exc}", file=sys.stderr)
        return 1

    scanned: list[tuple[Path, str]] = []
    folders_visited: list[Path] = []

    def _visit(folder: Path) -> None:
        if folder not in folders_visited:
            folders_visited.append(folder)

    # Manager folders
    for mgr in unique_managers(rows):
        f = paths.manager_to_approve(mgr.manager_name)
        scanned.extend(_scan_folder(f)); _visit(f)
        f = paths.manager_approved(mgr.manager_name)
        scanned.extend(_scan_folder(f)); _visit(f)
    # AP folders
    for ap in unique_aps(rows):
        f = paths.ap_approved_invoices(ap.ap_name)
        scanned.extend(_scan_folder(f)); _visit(f)
        f = paths.ap_paid_invoices(ap.ap_name)
        scanned.extend(_scan_folder(f)); _visit(f)
    # Plan archives — use plan_raw from each row
    seen_plan_dirs: set[str] = set()
    for row in rows:
        if row.plan_raw in seen_plan_dirs:
            continue
        seen_plan_dirs.add(row.plan_raw)
        try:
            f = paths.strata_plan_folder(row.plan_raw)
            scanned.extend(_scan_folder(f)); _visit(f)
        except Exception:
            pass
    # _Unmatched
    f = paths.unmatched_invoices()
    scanned.extend(_scan_folder(f)); _visit(f)
    # _state — for ledger tmps
    _visit(paths.invoice_fingerprints_csv().parent)

    # Orphans: hash present on disk but missing from BOTH ledger indexes.
    orphans = [
        (p, sha) for p, sha in scanned
        if sha not in by_hash and sha not in by_archive_hash
    ]

    # Stale rows: archive_path points somewhere that no longer exists.
    stale = [
        r for r in ledger.all_rows()
        if r.current_stage == "archived"
        and r.archive_path
        and not Path(r.archive_path).exists()
    ]

    # Multi-arrival vendors: dup_count >= 1, informational only.
    repeats = [r for r in ledger.all_rows() if r.dup_count > 0]

    # Orphan atomic-write tmp files (left after a crash mid-write).
    orphan_tmps = _find_orphan_tmps(folders_visited)

    print(f"Scanned {len(scanned)} on-disk PDFs across pipeline folders.")
    print(f"Ledger has {len(ledger.all_rows())} fingerprint rows.")
    print(f"  Orphans (on disk, not in ledger): {len(orphans)}")
    print(f"  Stale archived rows (ledger -> missing file): {len(stale)}")
    print(f"  Fingerprints with dup_count > 0: {len(repeats)}")
    print(f"  Orphan .tmp.<pid> files (atomic-write debris): {len(orphan_tmps)}")

    if orphans:
        print("\n-- Orphans --")
        for p, sha in orphans[:25]:
            print(f"  {sha[:16]}...  {p}")
        if len(orphans) > 25:
            print(f"  ... and {len(orphans) - 25} more (see --tsv for full list)")

    if stale:
        print("\n-- Stale archived rows --")
        for r in stale[:25]:
            print(f"  {r.sha256[:16]}...  plan={r.plan_norm}  missing: {r.archive_path}")
        if len(stale) > 25:
            print(f"  ... and {len(stale) - 25} more (see --tsv for full list)")

    if orphan_tmps:
        print("\n-- Orphan .tmp.<pid> files --")
        print("These are left over from interrupted atomic writes. Usually safe to delete.")
        for p in orphan_tmps[:25]:
            try:
                size = p.stat().st_size
            except Exception:
                size = -1
            print(f"  {size:>10} bytes  {p}")
        if len(orphan_tmps) > 25:
            print(f"  ... and {len(orphan_tmps) - 25} more (see --tsv for full list)")

    if args.tsv:
        try:
            args.tsv.parent.mkdir(parents=True, exist_ok=True)
            with open(args.tsv, "w", encoding="utf-8", newline="") as f:
                f.write("category\tsha256\tplan_norm\tdetail\n")
                for p, sha in orphans:
                    f.write(f"orphan\t{sha}\t\t{p}\n")
                for r in stale:
                    f.write(f"stale_archive\t{r.sha256}\t{r.plan_norm}\t{r.archive_path}\n")
                for r in repeats:
                    f.write(
                        f"repeat\t{r.sha256}\t{r.plan_norm}\t"
                        f"dup_count={r.dup_count} last_dup_date={r.last_dup_date} "
                        f"archive={r.archive_path}\n"
                    )
                for p in orphan_tmps:
                    f.write(f"orphan_tmp\t\t\t{p}\n")
            print(f"\nDetailed TSV written to {args.tsv}")
        except Exception as exc:
            print(f"ERROR: TSV write failed: {exc}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
