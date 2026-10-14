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
