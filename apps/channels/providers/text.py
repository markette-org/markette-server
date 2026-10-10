"""Length counting the way each network counts."""

import re
import unicodedata

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
HASHTAG_RE = re.compile(r"(?<![\w#])#([\w]+)", re.UNICODE)
MENTION_RE = re.compile(r"(?<![\w@])@([a-zA-Z0-9_.-]+(?:\.[a-zA-Z0-9_-]+)+|[a-zA-Z0-9_]+)")


def graphemes(text):
    """Approximate extended grapheme clusters without third-party deps."""
    count = 0
    prev_joiner = False
    for ch in text:
        cat = unicodedata.category(ch)
        is_extend = cat in ("Mn", "Me", "Mc") or ch in ("‍", "️", "︎") or 0x1F3FB <= ord(ch) <= 0x1F3FF
        if is_extend or prev_joiner:
            prev_joiner = ch == "‍"
            continue
        prev_joiner = False
        count += 1
    return count


def x_weighted_length(text):
    """X's weighted count: URLs weigh 23, CJK and emoji weigh 2, others 1."""
    length = 0
    last = 0
    for m in URL_RE.finditer(text):
        length += _x_weight(text[last : m.start()]) + 23
        last = m.end()
    length += _x_weight(text[last:])
    return length


def _x_weight(chunk):
    w = 0
    for ch in chunk:
        cp = ord(ch)
        light = cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F or 0x2032 <= cp <= 0x2037
        w += 1 if light else 2
    return w


def measure(text, mode="chars"):
    if mode == "graphemes":
        return graphemes(text)
    if mode == "x":
        return x_weighted_length(text)
    if mode == "mastodon":
        # Mastodon counts every URL as 23 characters.
        return len(URL_RE.sub("x" * 23, text))
    return len(text)
