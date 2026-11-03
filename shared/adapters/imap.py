"""IMAP mailbox adapter (Gmail or any IMAP4rev1 server), stdlib imaplib only.

Opt-in: AFO_MAILBOX=imap. Implements shared.protocols.Mailbox and returns the same Mail objects
as the .eml reader, so agents cannot tell the difference. It never changes the mailbox: the folder
is selected read-only and messages are fetched with BODY.PEEK[], which does not set \\Seen. Message
UIDs are stable per folder (with UIDVALIDITY), so each Mail.path is imap/<folder>/<uidvalidity>-<uid>.eml
and a re-run can skip what it has already proposed.

References:
- imaplib: https://docs.python.org/3/library/imaplib.html
- IMAP4rev1 UID FETCH, BODY.PEEK and EXAMINE (read-only select): RFC 3501, https://www.rfc-editor.org/rfc/rfc3501
- Gmail IMAP server imap.gmail.com:993 over SSL: https://developers.google.com/workspace/gmail/imap/imap-smtp
- Gmail XOAUTH2 ("user=" {user} "^Aauth=Bearer " {token} "^A^A"):
  https://developers.google.com/workspace/gmail/imap/xoauth2-protocol

Environment:
  AFO_IMAP_USER                      required
  AFO_IMAP_PASSWORD or AFO_IMAP_OAUTH_TOKEN   one required (a Gmail app password, or an OAuth2
                                     access token with the https://mail.google.com/ scope)
  AFO_IMAP_HOST    default imap.gmail.com
  AFO_IMAP_PORT    default 993
  AFO_IMAP_FOLDER  default INBOX (Gmail labels are folders, e.g. "finance/bills")
  AFO_IMAP_SEARCH  default ALL; e.g. 'UNSEEN' or 'SINCE 01-Oct-2026'
  AFO_IMAP_LIMIT   default 200: newest N matches, so a large mailbox is not pulled whole
"""

from __future__ import annotations

import imaplib
import os
import re
from pathlib import Path
from typing import Callable

from shared.adapters.http import AdapterConfigError
from shared.mail import Mail, parse_eml


class ImapMailbox:
    name = "imap"

    def __init__(self, *, user: str, password: str | None = None, oauth_token: str | None = None,
                 host: str = "imap.gmail.com", port: int = 993, folder: str = "INBOX", search: str = "ALL",
                 limit: int = 200, connect: Callable[[str, int], imaplib.IMAP4] | None = None) -> None:
        if not (password or oauth_token):
            raise AdapterConfigError("set AFO_IMAP_PASSWORD or AFO_IMAP_OAUTH_TOKEN")
        self.user, self._password, self._token = user, password, oauth_token
        self.host, self.port, self.folder, self.search, self.limit = host, port, folder, search, limit
        self._connect = connect or (lambda h, p: imaplib.IMAP4_SSL(h, p))

    def __repr__(self) -> str:
        return f"ImapMailbox(user={self.user!r}, host={self.host!r}, folder={self.folder!r})"

    @classmethod
    def from_env(cls, **kw) -> "ImapMailbox":
        user = os.environ.get("AFO_IMAP_USER")
        if not user:
            raise AdapterConfigError("missing environment variable(s): AFO_IMAP_USER")
        return cls(user=user, password=os.environ.get("AFO_IMAP_PASSWORD"),
                   oauth_token=os.environ.get("AFO_IMAP_OAUTH_TOKEN"),
                   host=os.environ.get("AFO_IMAP_HOST", "imap.gmail.com"),
                   port=int(os.environ.get("AFO_IMAP_PORT", "993")),
                   folder=os.environ.get("AFO_IMAP_FOLDER", "INBOX"),
                   search=os.environ.get("AFO_IMAP_SEARCH", "ALL"),
                   limit=int(os.environ.get("AFO_IMAP_LIMIT", "200")), **kw)

    def _login(self, conn) -> None:
        if self._token:
            auth = f"user={self.user}\x01auth=Bearer {self._token}\x01\x01".encode()
            conn.authenticate("XOAUTH2", lambda _challenge: auth)
        else:
            conn.login(self.user, self._password)

    def fetch(self) -> list[Mail]:
        conn = self._connect(self.host, self.port)
        try:
            self._login(conn)
            typ, data = conn.select(_quote(self.folder), readonly=True)
            if typ != "OK":
                raise RuntimeError(f"cannot open IMAP folder {self.folder!r}: {data!r}")
            validity = _uidvalidity(conn)
            typ, data = conn.uid("SEARCH", None, self.search)
            if typ != "OK":
                raise RuntimeError(f"IMAP search {self.search!r} failed: {data!r}")
            uids = (data[0] or b"").split()[-self.limit:] if self.limit else (data[0] or b"").split()
            out = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None)
                if typ != "OK" or raw is None:
                    continue
                path = Path("imap") / self.folder / f"{validity}-{uid.decode()}.eml"
                out.append(parse_eml(raw, path))
            return out
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def _quote(folder: str) -> str:
    return folder if re.fullmatch(r"[A-Za-z0-9_./-]+", folder) else '"' + folder.replace('"', '\\"') + '"'


def _uidvalidity(conn) -> str:
    try:
        typ, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            return data[0].decode() if isinstance(data[0], bytes) else str(data[0])
    except Exception:
        pass
    return "0"
