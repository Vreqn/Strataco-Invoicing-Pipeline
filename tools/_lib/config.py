"""Loads .env and exposes typed accessors for every config value used in the project.

Only `_lib/config.py` reads environment variables. Everything else imports
from here so we have a single place to validate, default, and document config.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


def project_root() -> Path:
    return _PROJECT_ROOT


def _required(name: str) -> str:
    val = os.getenv(name, "").strip()
    if not val:
        raise EnvironmentError(
            f"Missing required environment variable: {name}. "
            f"Copy .env.example to .env and fill in the value."
        )
    return val


def strataco_root() -> Path:
    """Root of the Strataco files tree on this machine."""
    return Path(_required("STRATACO_ROOT"))


def tenant_id() -> str:
    return _required("TENANT_ID")


def client_id() -> str:
    return _required("CLIENT_ID")


def client_secret() -> str:
    return _required("CLIENT_SECRET")


def mailbox_upn() -> str:
    return _required("MAILBOX_UPN")


def notify_override_email() -> str | None:
    """Reroute target for all manager/AP notifications during shadow phase.

    Empty string means "send to real recipients".
    """
    val = os.getenv("NOTIFY_OVERRIDE_EMAIL", "").strip()
    return val or None


def notify_default_email() -> str:
    """Default recipient for system-wide notifications (Step 6 summaries).

    During shadow phase this is the address that owns the migration; once
    `NOTIFY_OVERRIDE_EMAIL` is empty (cutover), this stays as the fallback
    summary recipient. Required — there is no built-in default so a missing
    value fails loudly instead of silently emailing the wrong person.
    """
    return _required("NOTIFY_DEFAULT_EMAIL")


def notify_email() -> str:
    """Resolve the actual recipient for system notifications.

    Returns `NOTIFY_OVERRIDE_EMAIL` when set (shadow phase), otherwise
    `NOTIFY_DEFAULT_EMAIL`. Used by Step 6's processed/unmatched summary
    emails so the old hardcoded address never gets reintroduced.
    """
    return notify_override_email() or notify_default_email()


def log_dir() -> Path:
    """Directory for daily_summary.csv, per-step logs, and lockfiles."""
    override = os.getenv("LOG_DIR", "").strip()
    if override:
        return Path(override)
    return strataco_root() / "logs"


def retry_max_attempts() -> int:
    return int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))


def retry_base_delay_seconds() -> int:
    return int(os.getenv("RETRY_BASE_DELAY_SECONDS", "2"))


# Step 2 zip safety knobs. Defaults sized for typical strata invoice ZIPs
# (a handful of small PDFs); operators can override via .env if a legitimate
# bulk-attachment workflow needs more headroom.
def zip_max_entries() -> int:
    return int(os.getenv("ZIP_MAX_ENTRIES", "200"))


def zip_max_uncompressed_bytes() -> int:
    return int(os.getenv("ZIP_MAX_UNCOMPRESSED_BYTES", str(100 * 1024 * 1024)))  # 100 MB


def zip_max_total_bytes() -> int:
    return int(os.getenv("ZIP_MAX_TOTAL_BYTES", str(500 * 1024 * 1024)))  # 500 MB


def zip_max_ratio() -> int:
    return int(os.getenv("ZIP_MAX_RATIO", "100"))


if __name__ == "__main__":
    # Smoke test: print resolved values without secrets.
    print(f"project_root: {project_root()}")
    try:
        print(f"strataco_root: {strataco_root()}")
    except EnvironmentError as e:
        print(f"strataco_root: NOT SET ({e})")
    print(f"log_dir: {log_dir()}")
    print(f"notify_override_email: {notify_override_email()}")
    try:
        print(f"notify_default_email: {notify_default_email()}")
    except EnvironmentError as e:
        print(f"notify_default_email: NOT SET ({e})")
