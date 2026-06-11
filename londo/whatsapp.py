from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# iOS:     [11/06/2026, 14:03:22] Alice: check this out https://...
# Android: 11/06/2026, 14:03 - Alice: check this out https://...
MESSAGE_PREFIX_RE = re.compile(
    r"^‎?\[?\d{1,2}[./]\d{1,2}[./]\d{2,4},? \d{1,2}:\d{2}(?::\d{2})?\]?\s*[-–]?\s*"
)

URL_RE = re.compile(r"https?://[^\s<>\"')\]}]+", re.I)

TRACKING_PARAMS = re.compile(r"[?&](utm_[a-z]+|fbclid|gclid|mc_[a-z]+)=[^&#]*")


def extract_urls(export_text: str) -> list[str]:
    """Pull unique, cleaned URLs out of a WhatsApp chat export (txt)."""
    urls: list[str] = []
    seen: set[str] = set()
    for line in export_text.splitlines():
        # strip the timestamp/author prefix when present; URLs can also be
        # in continuation lines of multi-line messages
        line = MESSAGE_PREFIX_RE.sub("", line)
        for raw in URL_RE.findall(line):
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
