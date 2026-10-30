"""Turning an organiser's HTML blurb into plain text that keeps its shape.

get_text(" ") flattens a description into one run-on line, losing the
headings and paragraphs the organiser wrote. html_to_text keeps them as
plain text the event page can lay out again:

- blank lines between blocks (paragraphs, headings, list items' lists);
- a single newline for a <br> inside a block;
- "• " in front of each list item.
"""

from __future__ import annotations

import re
import unicodedata

from bs4 import BeautifulSoup, NavigableString, Tag

_BLOCKS = {
    "p", "div", "section", "article", "blockquote", "pre", "table", "tr",
    "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "hr", "figure",
    "header", "footer", "dl", "dt", "dd",
}
_SKIP = {"script", "style", "noscript", "template", "svg", "img", "iframe"}
# a sentinel for block edges, folded into blank lines at the end
_BREAK = "\x00"


def html_to_text(value: str | None) -> str | None:
    if not value:
        return None
    soup = BeautifulSoup(value, "html.parser")
    out: list[str] = []
    _walk(soup, out)
    text = "".join(out).replace("\xa0", " ")
    text = re.sub(r"• [\s\x00]*", "• ", text)
    blocks = []
    for block in text.split(_BREAK):
        lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in block.split("\n")]
        # a run of <br>s is a paragraph break
        para: list[str] = []
        for ln in lines + [""]:
            if ln:
                para.append(ln)
            elif para:
                blocks.append("\n".join(para))
                para = []
    return "\n\n".join(blocks) or None


def _walk(node: Tag, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if child.__class__.__name__ in ("Comment", "Doctype", "Declaration"):
                continue
            # source newlines are just HTML whitespace
            out.append(re.sub(r"\s+", " ", str(child)))
        elif isinstance(child, Tag):
            name = child.name.lower()
            if name in _SKIP:
                continue
            if name == "br":
                out.append("\n")
            elif name == "li":
                if _is_section(child):
                    # some organisers put whole sections in a list item:
                    # keep its paragraphs, a bullet on the first
                    out += [_BREAK, "• "]
                    _walk(child, out)
                    out.append(_BREAK)
                    continue
                out.append("\n" if _in_list_item_run(child) else _BREAK)
                item = re.sub(r"\s+", " ", child.get_text(" ")).strip()
                if item:
                    out.append(f"• {item}")
            elif name in ("ul", "ol"):
                out.append(_BREAK)
                _walk(child, out)
                out.append(_BREAK)
            elif name in _BLOCKS:
                out.append(_BREAK)
                _walk(child, out)
                out.append(_BREAK)
            else:
                _walk(child, out)


def _is_section(li: Tag) -> bool:
    return any(
        t.name in _BLOCKS or (t.name == "br" and t.find_next_sibling() is not None)
        for t in li.find_all(True)
    )


def _in_list_item_run(li: Tag) -> bool:
    """Items after the first stay in one block, a line each."""
    prev = li.find_previous_sibling()
    return prev is not None and prev.name == "li" and not _is_section(prev)


# ---------- titles ---------------------------------------------------------
# Organisers style titles for their own feeds: emoji, "𝘮𝘰𝘯𝘵𝘩𝘭𝘺" in maths
# italic, ALL CAPS. tidy_title evens that out, lightly — wording, order and
# length stay the organiser's.

# Emoji and pictographs, the dingbats/misc-symbol blocks (✨ ☀ ♡), and the
# joiners and variation selectors that glue emoji sequences together.
# ® and ™ stay: they're part of names like 5Rhythms®.
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿⌀-⏿"
    "︀-️‍⃣\U000E0020-\U000E007F]"
)
# 𝘮𝘰𝘯𝘵𝘩𝘭𝘺, 𝚜𝚙𝚎𝚌𝚒𝚊𝚕, ｆｕｌｌｗｉｄｔｈ: letters in a costume. NFKC maps
# them back, applied only here so ™, ½ and friends are left alone.
_STYLED = re.compile("[\U0001D400-\U0001D7FF！-～]+")
_SMALL = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "via", "vs", "with", "w/",
}
# kept in capitals when a shouted title is brought down
_ACRONYMS = {
    "AI", "ASMR", "BIPOC", "CI", "DJ", "DMT", "EFT", "LGBT", "LGBTQ", "LGBTQIA",
    "LSD", "MDMA", "NLP", "NYX", "QTIPOC", "TRE", "UK", "USA", "Q&A",
}
# a word that starts a token: "PM" in "3PM" isn't one
_WORD = re.compile(r"(?<![\w'’])[^\W\d_][\w'’&.+/-]*", re.UNICODE)


def tidy_title(title: str | None) -> str:
    if not title:
        return title or ""
    styled_start = bool(_STYLED.match(_EMOJI.sub("", title).lstrip()))
    t = _STYLED.sub(lambda m: unicodedata.normalize("NFKC", m.group()), title)
    # an emoji between two phrases was a separator: "Yourself ✨ A Winter…"
    t = _SEPARATOR.sub(" · ", t.strip())
    t = _EMOJI.sub(" ", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"([(\[])\s+", r"\1", re.sub(r"\s+([)\]])", r"\1", t)).strip(" -–—|•·~*_")
    letters = [c for c in t if c.isalpha()]
    upper = sum(c.isupper() for c in letters)
    if len(letters) >= 12 and upper / len(letters) > 0.8:
        t = _title_case(t)  # the whole title is shouting
    else:
        # a shouted run inside a normal title: "CONTACT IMPROVISATION JAM with…"
        t = re.sub(r"\b[A-Z][A-Z'’&]*(?:(?:\s[:\-–—|]\s|[ -])+[A-Z][A-Z'’&]*)+\b", _shouted_run, t)
    # "𝘮𝘰𝘯𝘵𝘩𝘭𝘺 | Sounding Circle": the costume was the capital. Only then —
    # tldraw and freeCodeCamp are lowercase on purpose.
    if styled_start and t[:1].islower():
        t = t[0].upper() + t[1:]
    return t or title


_EMOJI_RUN = f"(?:{_EMOJI.pattern}|\\s)*{_EMOJI.pattern}(?:{_EMOJI.pattern}|\\s)*"
# emoji after a word, before a capitalised phrase of two words or more
_SEPARATOR = re.compile(rf"(?<=[\w!?)])\s*{_EMOJI_RUN}\s(?=[A-Z0-9][\w'’]*[ ,:]+\S)")


def _shouted_run(m: re.Match) -> str:
    """Bring a run of capitalised words down if it's prose, not initials:
    two or more words of four letters or more."""
    words = re.findall(r"[A-Z'’&]+", m.group())
    if sum(len(w) >= 4 and w not in _ACRONYMS for w in words) < 2:
        return m.group()
    return _title_case(m.group(), first=m.start() == 0)


def _initials(word: str) -> bool:
    """LFG, ABRC, BST: no vowel, or three consonants running — not a word."""
    w = word.upper()
    if not w.isalpha() or len(w) > 4:
        return False
    return not re.search("[AEIOU]", w) or bool(re.search("[^AEIOUY]{3}", w))


def _title_case(text: str, first: bool = True) -> str:
    out = []
    start = first

    def fix(m: re.Match) -> str:
        nonlocal start
        w = m.group()
        was_start, start = start, False
        if w.upper() in _ACRONYMS or (len(w) <= 2 and "." in w) or _initials(w):
            return w.upper()
        if not was_start and w.lower() in _SMALL:
            return w.lower()
        return w[0].upper() + w[1:].lower()

    # restart the capital after a colon, dash or bar: "Day - A Powerful…"
    for i, part in enumerate(re.split(r"(\s[:\-–—|]\s|:\s)", text)):
        if i % 2:
            out.append(part)
            start = True
            continue
        out.append(_WORD.sub(fix, part))
    return "".join(out)
