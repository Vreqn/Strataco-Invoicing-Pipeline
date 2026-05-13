"""Operator CLI: mark a fingerprint as `overridden` so the next arrival of it
proceeds through the pipeline normally.

Use this when a vendor genuinely re-bills (e.g. corrected invoice, credit-and-
rebill) and the duplicate detection is over-applying. The override is one-shot:
the next time that fingerprint flows through Step 1 / 3 / 5, the override is
consumed (the row goes back to a normal lifecycle stage). To override again,
re-run this CLI.

Usage:
  python tools/dup_override.py <sha256> --reason "vendor re-billed for credit applied"

The reason is stored in the run logs, not in the ledger row itself (the ledger
stays Excel-friendly with its 10-column schema). The full reason is grep-able
in `logs/dup_override_<date>.log` if you ever need to audit.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import re

from tools._lib import dup_ledger

_MIN_PREFIX_HEX = 12
_HEX_RE = re.compile(r"^[0-9a-f]+$")


def _today_iso() -> str:
    try:
        from zoneinfo import ZoneInfo
        now = _dt.datetime.now(ZoneInfo("America/Vancouver"))
    except Exception:
        now = _dt.datetime.now()
    return now.isoformat(timespec="seconds")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Mark a duplicate-detected fingerprint as overridden for one re-arrival.",
    )
    parser.add_argument(
        "sha256",
        help=(
            "The sha256 hex string of the fingerprint to override. "
            "Either a full 64-char hash or at least 12 hex characters."
        ),
    )
    parser.add_argument(
        "--reason",
        required=True,
        help="Why this override is being applied (required for audit trail).",
    )
    args = parser.parse_args(argv)

    # Validate the prefix BEFORE loading the ledger so the operator gets a
    # clear error even if STRATACO_ROOT isn't configured.
    sha = args.sha256.strip().lower()
    if not _HEX_RE.match(sha):
        print(
            f"ERROR: sha256 must be hex characters only (0-9, a-f); got {args.sha256!r}",
            file=sys.stderr,
        )
        return 1
    if len(sha) < _MIN_PREFIX_HEX:
        print(
            f"ERROR: sha256 prefix must be at least {_MIN_PREFIX_HEX} hex chars to avoid "
            f"typo-induced wrong-row matches; got {len(sha)} chars",
            file=sys.stderr,
        )
        return 1
    if len(sha) > 64:
        print(
            f"ERROR: sha256 is at most 64 hex chars; got {len(sha)} chars",
            file=sys.stderr,
        )
        return 1

    try:
        ledger = dup_ledger.load()
    except ValueError as exc:
        print(f"ERROR: duplicate-detection ledger is corrupted: {exc}", file=sys.stderr)
        return 1

    # Allow prefix matching for operator convenience — they typically have a
    # short hash from a log line.
    matched: list[dup_ledger.FingerprintRow] = []
    for row in ledger.all_rows():
        if row.sha256.startswith(sha):
            matched.append(row)

    if not matched:
        print(f"ERROR: no fingerprint matches {sha!r}", file=sys.stderr)
        return 1
    if len(matched) > 1:
        print(
            f"ERROR: prefix {sha!r} matches {len(matched)} fingerprints — provide more characters:",
            file=sys.stderr,
        )
        for m in matched:
            print(f"  {m.sha256}  plan={m.plan_norm}  stage={m.current_stage}", file=sys.stderr)
        return 1

    target = matched[0]
    if target.current_stage == "overridden":
        print(f"NOTE: {target.sha256[:16]}... is already marked overridden; re-applying anyway.")

    try:
        updated = ledger.update_stage(target.sha256, "overridden")
    except Exception as exc:
        print(f"ERROR: ledger update failed: {exc}", file=sys.stderr)
        return 1

    # Append a line to a dedicated override log for audit.
    log_path = Path(__file__).resolve().parent.parent / "logs" / f"dup_override_{_dt.date.today().isoformat()}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(
            f"{_today_iso()}  override sha={target.sha256}  plan={target.plan_norm}  "
            f"prior_stage={target.current_stage}  reason={args.reason!r}\n"
        )

    print(
        f"OK: fingerprint {target.sha256[:16]}... marked overridden "
        f"(was {target.current_stage}). Next arrival will route normally. "
        f"Audit logged to {log_path}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
