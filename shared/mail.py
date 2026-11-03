"""Reads a mailbox exported as .eml files. Stands in for an IMAP or Gmail adapter."""

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


def read_mailbox(folder: Path) -> list[Mail]:
    out = []
    for p in sorted(Path(folder).glob("*.eml")):
        msg = BytesParser(policy=policy.default).parsebytes(p.read_bytes())
        body = msg.get_body(preferencelist=("plain", "html"))
        atts = [
            Attachment(a.get_filename() or "attachment", a.get_content_type(), a.get_payload(decode=True))
            for a in msg.iter_attachments()
        ]
        out.append(
            Mail(p, str(msg["From"]), str(msg["Subject"]), str(msg["Date"]),
                 body.get_content() if body else "", atts)
        )
    return out


def build_eml(*, sender: str, to: str, subject: str, date: str, body: str,
              attachments: list[tuple[str, str, bytes]] = ()) -> bytes:
    """Used by the synthetic-data generators to write fixture mailboxes."""
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"] = sender, to, subject, date
    msg.set_content(body)
    for name, ctype, data in attachments:
        main, sub = ctype.split("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return bytes(msg)
