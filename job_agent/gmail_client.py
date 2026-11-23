"""Gmail API integration (OAuth 2.0, Desktop app flow). Sends only what the UI passes after explicit approval."""
from __future__ import annotations

import base64
import logging
import mimetypes
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


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


class GmailClient:
    def __init__(self, credentials_file: Path, token_file: Path):
        self.credentials_file = credentials_file
        self.token_file = token_file
        self._service = None
        self.account: str | None = None

    def _load_creds(self):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        if not self.token_file.exists():
            return None
        creds = Credentials.from_authorized_user_file(str(self.token_file), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_file.write_text(creds.to_json())
        return creds if creds and creds.valid else None

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
            if "gmail.send" in str(w):
                return GmailStatus(False, "error",
                                   "The 'Send email on your behalf' permission was not granted (its checkbox was "
                                   "left unticked). Run --gmail-auth again and tick that box.")
            return GmailStatus(False, "error", f"Login failed: {w}")
        if "https://www.googleapis.com/auth/gmail.send" not in (creds.scopes or SCOPES):
            return GmailStatus(False, "error", "Gmail send permission was not granted. Run --gmail-auth again.")
        self.token_file.write_text(creds.to_json())
        self.token_file.chmod(0o600)
        return self.check()

    def send(self, to: str, subject: str, body: str, attachment: Path, filename: str | None = None) -> str:
        if self._service is None:
            st = self.check()
            if not st.ready:
                raise RuntimeError(st.detail)
        msg = build_message(self.account, to, subject, body, attachment, filename)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        sent = self._service.users().messages().send(userId="me", body={"raw": raw}).execute()
        log.info("Sent email to %s, gmail id %s", to, sent.get("id"))
        return sent["id"]
