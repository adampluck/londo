from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# iOS:     [11/06/2026, 14:03:22] Alice: check this out https://...
# Android: 11/06/2026, 14:03 - Alice: check this out https://...
MESSAGE_PREFIX_RE = re.compile(
    r"^‎?\[?\d{1,2}[./]\d{1,2}[./]\d{2,4},? \d{1,2}:\d{2}(?::\d{2})?\]?\s*[-–]?\s*"
)

# Full header with capture groups: timestamp, then sender up to the first
# ": " (system lines like "Alice joined" have no sender colon and are dropped).
MESSAGE_HEADER_RE = re.compile(
    r"^\[?(?P<date>\d{1,2}[./]\d{1,2}[./]\d{2,4}),? "
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\]?\s*[-–]?\s*"
    r"(?P<sender>[^:\n]{1,80}?):\s?(?P<body>.*)$"
)

URL_RE = re.compile(r"https?://[^\s<>\"')\]}]+", re.I)

# People also paste bare sites ("healingartsmassages.com", "www.x.org/book").
# Only well-known endings, not preceded by "@" (emails) or "/" (already
# part of a full URL above).
BARE_URL_RE = re.compile(
    r"(?<![@/\w.-])((?:www\.)?(?:[a-z0-9-]+\.)+"
    r"(?:com|co\.uk|org\.uk|org|uk|net|io|app|life|events|london|me|info|eu|co|live|space|studio|yoga|love|community)"
    r"(?:/[^\s<>\"')\]}]*)?)(?![\w.-]*@)",
    re.I,
)

TRACKING_PARAMS = re.compile(r"[?&](utm_[a-z]+|fbclid|gclid|mc_[a-z]+)=[^&#]*")

# "<attached: 00013439-PHOTO-2026-09-12-12-42-52.jpg>" — iOS exports put
# this at the end of the caption (or alone, for a caption-less photo).
ATTACHMENT_RE = re.compile(r"<attached:\s*([^>]+?)\s*>")
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

# Left-to-right marks and friends that iOS sprinkles around names/markers.
_INVISIBLE = "‎‏‪‬⁨⁩"

# A caption-less photo sent this close to a text message from the same
# sender is that message's flyer.
POST_GAP = timedelta(minutes=5)


@dataclass
class Message:
    sent_at: datetime
    sender: str
    text: str
    attachments: list[str] = field(default_factory=list)


@dataclass
class Post:
    """One shared thing: a message plus any bare photo messages the same
    sender dropped right before or after it."""

    sent_at: datetime
    sender: str
    text: str
    photos: list[str] = field(default_factory=list)  # attachment filenames

    @property
    def urls(self) -> list[str]:
        return extract_urls(self.text)


def extract_urls(export_text: str) -> list[str]:
    """Pull unique, cleaned URLs out of a WhatsApp chat export (txt)."""
    urls: list[str] = []
    seen: set[str] = set()
    for line in export_text.splitlines():
        # strip the timestamp/author prefix when present; URLs can also be
        # in continuation lines of multi-line messages
        line = MESSAGE_PREFIX_RE.sub("", line)
        found = URL_RE.findall(line)
        found += [
            "https://" + m.group(1)
            for m in BARE_URL_RE.finditer(URL_RE.sub(" ", line))
        ]
        for raw in found:
            url = _clean_url(raw)
            key = url.lower().rstrip("/")
            if key not in seen:
                seen.add(key)
                urls.append(url)
    return urls


def _clean_url(url: str) -> str:
    url = url.rstrip(".,;:!?…")
    # balance trailing parens: "(see https://x.com/a)" captures "a)"
    while url.endswith(")") and url.count("(") < url.count(")"):
        url = url[:-1]
    url = TRACKING_PARAMS.sub(lambda m: "?" if m.group(0).startswith("?") else "", url)
    return url.rstrip("?&")


def locate_export(path: str | Path) -> tuple[Path, Path]:
    """(chat txt, media dir) for either the .txt itself or its folder."""
    p = Path(path)
    if p.is_dir():
        txts = sorted(p.glob("*.txt"))
        preferred = [t for t in txts if t.name == "_chat.txt"] or txts
        if not preferred:
            raise FileNotFoundError(f"No chat .txt found in {p}")
        return preferred[0], p
    return p, p.parent


def parse_messages(export_text: str) -> list[Message]:
    """Split an export into messages: a header line starts one, and any
    following lines without a header continue it."""
    messages: list[Message] = []
    for raw in export_text.splitlines():
        line = raw.rstrip("\r").translate({ord(c): None for c in _INVISIBLE})
        m = MESSAGE_HEADER_RE.match(line)
        if m:
            sent_at = _parse_timestamp(m.group("date"), m.group("time"))
            if sent_at is not None:
                messages.append(
                    Message(sent_at=sent_at, sender=m.group("sender").strip(), text=m.group("body"))
                )
                continue
        elif MESSAGE_PREFIX_RE.match(line):
            continue  # system line ("X joined") — no sender, not a message
        if messages:
            messages[-1].text += "\n" + line

    for msg in messages:
        names = ATTACHMENT_RE.findall(msg.text)
        msg.attachments = [n for n in names if Path(n).suffix.lower() in IMAGE_SUFFIXES]
        msg.text = ATTACHMENT_RE.sub("", msg.text).strip()
    return messages


def _parse_timestamp(date_s: str, time_s: str) -> datetime | None:
    date_s = date_s.replace(".", "/")
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%y %H:%M:%S", "%d/%m/%y %H:%M"):
        try:
            return datetime.strptime(f"{date_s} {time_s}", fmt)
        except ValueError:
            continue
    return None


def group_posts(messages: list[Message]) -> list[Post]:
    """Fold caption-less photo messages into the neighbouring text message
    from the same sender, so a flyer sent just before or after its blurb
    counts as one post."""
    posts: list[Post] = []
    pending_photos: list[tuple[datetime, str, list[str]]] = []  # photos awaiting text

    def flush_pending() -> None:
        for sent_at, sender, photos in pending_photos:
            posts.append(Post(sent_at=sent_at, sender=sender, text="", photos=photos))
        pending_photos.clear()

    for msg in messages:
        if not msg.text and not msg.attachments:
            continue
        if not msg.text:  # bare photo(s)
            last = posts[-1] if posts else None
            if (
                last is not None
                and last.sender == msg.sender
                and msg.sent_at - last.sent_at <= POST_GAP
            ):
                last.photos.extend(msg.attachments)
            else:
                flush_pending()
                pending_photos.append((msg.sent_at, msg.sender, list(msg.attachments)))
            continue

        post = Post(sent_at=msg.sent_at, sender=msg.sender, text=msg.text, photos=list(msg.attachments))
        # photos that arrived just before this text, from the same sender
        keep: list[tuple[datetime, str, list[str]]] = []
        for sent_at, sender, photos in pending_photos:
            if sender == msg.sender and msg.sent_at - sent_at <= POST_GAP:
                post.photos = photos + post.photos
            else:
                keep.append((sent_at, sender, photos))
        pending_photos[:] = keep
        flush_pending()
        posts.append(post)

    flush_pending()
    return posts
