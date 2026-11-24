"""Gmail API integration (OAuth 2.0, Desktop app flow).

Sends to recruiters/companies only what the UI passes after explicit approval. The daily run never sends to them:
it only creates drafts in the user's own Gmail Drafts folder, and the only email it sends is the summary addressed
to the user's own account.
"""
from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)

SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"      # create drafts (daily run)
METADATA_SCOPE = "https://www.googleapis.com/auth/gmail.metadata"    # was a draft sent or deleted? (labels only)
SCOPES = [
    SEND_SCOPE,
    COMPOSE_SCOPE,
    METADATA_SCOPE,
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]
SCOPE_LABELS = {SEND_SCOPE: "Send email on your behalf", COMPOSE_SCOPE: "Manage drafts and send emails",
                METADATA_SCOPE: "View your email message metadata"}


@dataclass
class GmailStatus:
    ready: bool
    state: str          # "ready" | "no_credentials" | "needs_login" | "error"
    detail: str
    account: str | None = None


def build_message(sender: str | None, to: str, subject: str, body: str, attachment: Path,
                  filename: str | None = None) -> EmailMessage:
    if not attachment.exists():
        raise FileNotFoundError(f"Attachment not found: {attachment}")
    msg = EmailMessage()
    msg["To"] = to
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    ctype, _ = mimetypes.guess_type(attachment.name)
    maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
    msg.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=filename or attachment.name)
    return msg


def build_summary_message(to: str, subject: str, text: str, html: str) -> EmailMessage:
    """The daily summary (plain text + HTML), addressed to the user themself."""
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _http_status(e: Exception) -> int | None:
    resp = getattr(e, "resp", None)
    try:
        return int(getattr(resp, "status", None))
    except (TypeError, ValueError):
        return None


class GmailClient:
    def __init__(self, credentials_file: Path, token_file: Path):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None
        self.account: str | None = None
        self.granted: set[str] = set()

    def _load_creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self.token_file.exists():
            return None
        # Use the scopes saved with the token (not SCOPES): refreshing an older send-only token with extra scopes
        # would fail until the user logs in again.
        creds = Credentials.from_authorized_user_file(str(self.token_file))
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            try:
                self.token_file.write_text(creds.to_json())
            except OSError:
                pass                        # read-only token file (e.g. CI secret): the refreshed token is in memory
        if creds and creds.valid:
            self.granted = set(creds.scopes or [])
            return creds
        return None

    def _account_email(self, creds) -> str | None:
        from googleapiclient.discovery import build
        try:
            info = build("oauth2", "v2", credentials=creds, cache_discovery=False).userinfo().get().execute()
            return info.get("email")
        except Exception as e:
            log.warning("Could not read Gmail account email: %s", e)
            return None

    def check(self) -> GmailStatus:
        if not self.credentials_file.exists():
            return GmailStatus(False, "no_credentials",
                               f"OAuth client file missing: {self.credentials_file}. Emails can be drafted but not sent.")
        try:
            creds = self._load_creds()
        except Exception as e:
            return GmailStatus(False, "error", f"Saved Gmail token is invalid: {e}. Run: python run.py --gmail-auth")
        if not creds:
            return GmailStatus(False, "needs_login", "One-time login required. Run: python run.py --gmail-auth")
        try:
            from googleapiclient.discovery import build
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
            self.account = self._account_email(creds)
            return GmailStatus(True, "ready", "Authenticated", self.account)
        except Exception as e:
            return GmailStatus(False, "error", f"Gmail API init failed: {e}")

    def authenticate(self) -> GmailStatus:
        """Interactive one-time browser login; stores a refreshable token locally."""
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.credentials_file.exists():
            return self.check()
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), SCOPES)
        try:
            creds = flow.run_local_server(port=0, open_browser=True)
        except Warning as w:  # oauthlib raises a Warning when the user unticks a requested permission
            log.warning("Gmail login scope warning: %s", w)
            return GmailStatus(False, "error",
                               "Not every Gmail permission was granted (a checkbox was left unticked). Run "
                               "--gmail-auth again and tick all boxes: " + ", ".join(SCOPE_LABELS.values()))
        missing = [label for scope, label in SCOPE_LABELS.items() if scope not in (creds.scopes or SCOPES)]
        if missing:
            return GmailStatus(False, "error", "Gmail permission not granted: " + ", ".join(missing)
                               + ". Run --gmail-auth again and tick all boxes.")
        self.token_file.write_text(creds.to_json())
        self.token_file.chmod(0o600)
        return self.check()

    def _ready(self) -> None:
        if self._service is None:
            st = self.check()
            if not st.ready:
                raise RuntimeError(st.detail)

    def missing_draft_scopes(self) -> list[str]:
        """Permissions the daily run needs that the saved token does not have (empty = OK)."""
        return [SCOPE_LABELS[s] for s in (SEND_SCOPE, COMPOSE_SCOPE, METADATA_SCOPE) if s not in self.granted]

    def send(self, to: str, subject: str, body: str, attachment: Path, filename: str | None = None) -> str:
        self._ready()
        msg = build_message(self.account, to, subject, body, attachment, filename)
        sent = self._service.users().messages().send(userId="me", body={"raw": _raw(msg)}).execute()
        log.info("Sent email to %s, gmail id %s", to, sent.get("id"))
        return sent["id"]

    # ------------------------------------------------------------ daily run (drafts + summary to self)
    def create_draft(self, to: str, subject: str, body: str, attachment: Path,
                     filename: str | None = None) -> tuple[str, str, str]:
        """Put the email in the user's Gmail Drafts folder (NOT sent). Returns (draft_id, message_id, thread_id)."""
        self._ready()
        msg = build_message(self.account, to, subject, body, attachment, filename)
        d = self._service.users().drafts().create(userId="me", body={"message": {"raw": _raw(msg)}}).execute()
        m = d.get("message") or {}
        log.info("Created Gmail draft %s to %s", d.get("id"), to)
        return d["id"], m.get("id", ""), m.get("threadId", "")

    def draft_exists(self, draft_id: str) -> bool | None:
        """True = still in Drafts, False = gone (sent or deleted), None = could not check."""
        self._ready()
        try:
            self._service.users().drafts().get(userId="me", id=draft_id, format="minimal").execute()
            return True
        except Exception as e:
            if _http_status(e) == 404:
                return False
            log.warning("Could not check Gmail draft %s: %s", draft_id, e)
            return None

    def thread_sent_times(self, thread_id: str) -> list[int] | None:
        """internalDate (ms) of messages in the thread that carry the SENT label; [] if none or the thread is gone;
        None if it could not be checked. Uses metadata only (no message bodies are read)."""
        self._ready()
        try:
            t = self._service.users().threads().get(userId="me", id=thread_id, format="metadata").execute()
        except Exception as e:
            if _http_status(e) == 404:
                return []
            log.warning("Could not check Gmail thread %s: %s", thread_id, e)
            return None
        return [int(m.get("internalDate") or 0) for m in t.get("messages", []) if "SENT" in (m.get("labelIds") or [])]

    def send_summary_to_self(self, subject: str, text: str, html: str) -> str:
        """The only automatic send: the daily summary, addressed to the authenticated account itself."""
        self._ready()
        if not self.account:
            raise RuntimeError("Could not determine your Gmail address; summary not sent.")
        msg = build_summary_message(self.account, subject, text, html)
        sent = self._service.users().messages().send(userId="me", body={"raw": _raw(msg)}).execute()
        return sent["id"]
