"""Reads a mailbox exported as .eml files.

`EmlMailbox` implements shared.protocols.Mailbox and is the default; the IMAP/Gmail adapter in
shared/adapters/imap.py implements the same Protocol and parses messages with `parse_eml` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path


@dataclass
class Attachment:
    filename: str
    content_type: str
    data: bytes


@dataclass
class Mail:
    path: Path
    sender: str
    subject: str
    date: str
    body: str
    attachments: list[Attachment] = field(default_factory=list)


def parse_eml(raw: bytes, path: Path) -> Mail:
    """Parse one RFC 5322 message. `path` names where it came from (a file, or imap/<folder>/<uid>.eml)."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = msg.get_body(preferencelist=("plain", "html"))
    atts = [
        Attachment(a.get_filename() or "attachment", a.get_content_type(), a.get_payload(decode=True))
        for a in msg.iter_attachments()
    ]
    return Mail(Path(path), str(msg["From"]), str(msg["Subject"]), str(msg["Date"]),
                body.get_content() if body else "", atts)


def read_mailbox(folder: Path) -> list[Mail]:
    return [parse_eml(p.read_bytes(), p) for p in sorted(Path(folder).glob("*.eml"))]


class EmlMailbox:
    """shared.protocols.Mailbox over a folder of .eml files: the default."""

    name = "eml"

    def __init__(self, folder: Path) -> None:
        self.folder = Path(folder)

    def fetch(self) -> list[Mail]:
        return read_mailbox(self.folder)


def build_eml(*, sender: str, to: str, subject: str, date: str, body: str,
              attachments: list[tuple[str, str, bytes]] = (), boundary: str | None = None) -> bytes:
    """Used by the synthetic-data generators to write fixture mailboxes.

    The stdlib picks a random MIME boundary for multipart messages; pass `boundary` to pin it
    so regenerated fixtures are byte-identical. Ignored when there are no attachments.
    """
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, to, subject, date
    msg.set_content(body)
    for name, ctype, data in attachments:
        main, sub = ctype.split("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    if boundary and msg.is_multipart():
        msg.set_boundary(boundary)
    return bytes(msg)
