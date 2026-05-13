"""Microsoft Graph helpers for the Strataco mailbox.

MSAL client-credentials flow (app-only). Same retry/backoff pattern as the
sibling Ati_Netwks project's graph_auth.py — copied and adapted, NOT shared.

Provides only the calls Step 1 and the notification steps actually need:
- list_inbox_messages
- list_conversation_messages
- list_attachments
- download_attachment
- find_child_folder_id (Inbox subfolder by displayName)
- move_message_to_folder
- flag_message (Outlook 'Flag as to-do' — red flag in the UI)
- send_mail
"""

from __future__ import annotations

import functools
import logging
import time
import urllib.parse

import msal
import requests

from tools._lib import config

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


class AuthenticationError(Exception):
    pass


class GraphAPIError(Exception):
    def __init__(self, message: str, status_code: int | None = None, body: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


_msal_app: msal.ConfidentialClientApplication | None = None


def _app() -> msal.ConfidentialClientApplication:
    global _msal_app
    if _msal_app is None:
        _msal_app = msal.ConfidentialClientApplication(
            client_id=config.client_id(),
            client_credential=config.client_secret(),
            authority=f"https://login.microsoftonline.com/{config.tenant_id()}",
        )
    return _msal_app


def get_access_token() -> str:
    result = _app().acquire_token_for_client(
        scopes=["https://graph.microsoft.com/.default"]
    )
    if "access_token" not in result:
        err = result.get("error_description", result.get("error", "unknown error"))
        raise AuthenticationError(f"Failed to acquire token: {err}")
    return result["access_token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_access_token()}"}


_session = requests.Session()


def _retry(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        max_attempts = config.retry_max_attempts()
        base_delay = config.retry_base_delay_seconds()
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return func(*args, **kwargs)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status in (429, 500, 502, 503, 504) if status else True
                if not retryable or attempt == max_attempts:
                    body = None
                    if exc.response is not None:
                        body = exc.response.text[:500]
                    raise GraphAPIError(
                        f"{func.__name__} failed: {exc}", status_code=status, body=body
                    ) from exc
                delay = base_delay * (2 ** (attempt - 1))
                if status == 429 and exc.response is not None:
                    ra = exc.response.headers.get("Retry-After")
                    if ra:
                        try:
                            delay = max(delay, int(ra))
                        except ValueError:
                            pass
                logger.warning(
                    "%s attempt %d/%d failed (HTTP %s). Retrying in %ds...",
                    func.__name__, attempt, max_attempts, status, delay,
                )
                time.sleep(delay)
        raise GraphAPIError(f"{func.__name__} failed after {max_attempts} attempts") from last_exc

    return wrapper


@_retry
def _get_json(url: str, params: dict | None = None) -> dict:
    resp = _session.get(url, headers=_auth_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


@_retry
def _get_bytes(url: str, params: dict | None = None) -> bytes:
    resp = _session.get(url, headers=_auth_headers(), params=params, timeout=60)
    resp.raise_for_status()
    return resp.content


@_retry
def _post_json(url: str, body: dict) -> dict:
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    resp = _session.post(url, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    if resp.content:
        try:
            return resp.json()
        except ValueError:
            return {}
    return {}


@_retry
def _patch_json(url: str, body: dict) -> dict:
    headers = {**_auth_headers(), "Content-Type": "application/json"}
    resp = _session.patch(url, headers=headers, json=body, timeout=30)
    resp.raise_for_status()
    if resp.content:
        try:
            return resp.json()
        except ValueError:
            return {}
    return {}


def _q(value: str) -> str:
    """URL-encode a single path segment.

    Mailbox UPNs contain `@`; message and attachment IDs from Graph contain
    `+`, `/`, and `=` (they're base64url-style). None of those are safe to
    interpolate raw into an f-string URL, so route every interpolation
    through this helper.
    """
    return urllib.parse.quote(str(value), safe="")


def _odata_str(value: str) -> str:
    """Escape a literal string for use inside an OData `$filter` expression.

    Single quotes inside the literal are doubled per OData v4 syntax.
    """
    return value.replace("'", "''")


def _get_paged_value(url: str, params: dict | None = None) -> list[dict]:
    """Fetch a Graph collection endpoint, following `@odata.nextLink` to exhaustion.

    Returns the concatenated `value` arrays. The first request uses `params`;
    subsequent requests use the URL Graph hands us in `@odata.nextLink` and
    drop the original params (the nextLink already encodes them).
    """
    items: list[dict] = []
    first_params = params
    while url:
        data = _get_json(url, params=first_params)
        page = data.get("value") or []
        items.extend(page)
        url = data.get("@odata.nextLink") or ""
        first_params = None  # nextLink already carries the query
    return items


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

def list_inbox_messages(top: int = 500) -> list[dict]:
    """Return up to `top` messages from the mailbox's Inbox.

    Follows `@odata.nextLink` so we don't silently drop everything past the
    first page when the mailbox has more than `top` matches.

    `conversationId` and `bodyPreview` are included so Step 1 can run the
    body-text matcher and resolve reply-to-self chains back to the original
    PDF-bearing message in the same thread.
    """
    upn = _q(config.mailbox_upn())
    url = f"{GRAPH_BASE_URL}/users/{upn}/mailFolders/Inbox/messages"
    params = {
        "$top": str(top),
        "$select": (
            "id,subject,from,receivedDateTime,hasAttachments,"
            "conversationId,bodyPreview"
        ),
    }
    return _get_paged_value(url, params=params)


def list_conversation_messages(conversation_id: str) -> list[dict]:
    """Return every message in `conversation_id` across the mailbox.

    Used by Step 1 to find the original PDF-bearing message when a
    reply-to-self with the corrected subject arrives without re-attaching
    the PDF.
    """
    upn = _q(config.mailbox_upn())
    url = f"{GRAPH_BASE_URL}/users/{upn}/messages"
    params = {
        "$filter": f"conversationId eq '{_odata_str(conversation_id)}'",
        "$select": (
            "id,subject,from,receivedDateTime,hasAttachments,"
            "conversationId,parentFolderId"
        ),
    }
    return _get_paged_value(url, params=params)


def list_attachments(message_id: str) -> list[dict]:
    upn = _q(config.mailbox_upn())
    msg_id = _q(message_id)
    url = f"{GRAPH_BASE_URL}/users/{upn}/messages/{msg_id}/attachments"
    params = {"$select": "id,name,contentType,size,isInline"}
    return _get_paged_value(url, params=params)


def download_attachment(message_id: str, attachment_id: str) -> bytes:
    upn = _q(config.mailbox_upn())
    msg_id = _q(message_id)
    att_id = _q(attachment_id)
    url = (
        f"{GRAPH_BASE_URL}/users/{upn}/messages/{msg_id}"
        f"/attachments/{att_id}/$value"
    )
    return _get_bytes(url)


def find_child_folder_id(parent_folder: str, display_name: str) -> str | None:
    """Return the id of `display_name` directly under `parent_folder` (e.g. 'Inbox').

    Returns None if the folder does not exist.
    """
    upn = _q(config.mailbox_upn())
    parent = _q(parent_folder)
    url = (
        f"{GRAPH_BASE_URL}/users/{upn}/mailFolders/{parent}/childFolders"
    )
    params = {
        "$filter": f"displayName eq '{_odata_str(display_name)}'",
        "$select": "id,displayName",
    }
    data = _get_json(url, params=params)
    items = data.get("value") or []
    if not items:
        return None
    return items[0].get("id")


def move_message_to_folder(message_id: str, destination_folder_id: str) -> None:
    upn = _q(config.mailbox_upn())
    msg_id = _q(message_id)
    url = f"{GRAPH_BASE_URL}/users/{upn}/messages/{msg_id}/move"
    _post_json(url, {"destinationId": destination_folder_id})


def flag_message(message_id: str) -> None:
    """Set Outlook's 'Flag as to-do' on a message.

    Shows up as the standard red flag in Outlook; when the operator marks it
    complete (in Outlook), it becomes a green checkmark. Uses Graph's
    `followupFlag` resource via PATCH on the message.
    """
    upn = _q(config.mailbox_upn())
    msg_id = _q(message_id)
    url = f"{GRAPH_BASE_URL}/users/{upn}/messages/{msg_id}"
    _patch_json(url, {"flag": {"flagStatus": "flagged"}})


def send_mail(to: str, subject: str, body: str) -> None:
    """Send a plain-text email from the configured mailbox."""
    upn = _q(config.mailbox_upn())
    url = f"{GRAPH_BASE_URL}/users/{upn}/sendMail"
    # Graph accepts ; or , separated; keep ; to mirror N8n's normalisation.
    recipients = [
        {"emailAddress": {"address": addr.strip()}}
        for addr in (to or "").replace(",", ";").split(";")
        if addr.strip()
    ]
    if not recipients:
        raise ValueError("send_mail: no recipients")
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": recipients,
        },
        "saveToSentItems": True,
    }
    _post_json(url, payload)


def resolve_recipient(real_to: str) -> str:
    """Apply the NOTIFY_OVERRIDE_EMAIL during the shadow phase."""
    override = config.notify_override_email()
    return override if override else real_to
