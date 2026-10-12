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


SEPARATOR_RE = re.compile(r"^[ \t]*---+[ \t]*$", re.MULTILINE)
SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")


def split_thread(text, limit, mode="chars", numbering=True):
    """Splits ``text`` into posts of at most ``limit`` (counted the network's way).

    Lines made of ``---`` mark where posts break. Otherwise the text is packed paragraph by
    paragraph, then sentence by sentence, then word by word. With ``numbering`` each post
    ends with "i/n" on its own line (when there is more than one).
    """
    reserve = len("\n\n99/99") if numbering else 0
    budget = max(limit - reserve, 20)

    def fits(s):
        return measure(s, mode) <= budget

    def pack(pieces, joiner):
        out, cur = [], ""
        for piece in pieces:
            cand = f"{cur}{joiner}{piece}" if cur else piece
            if fits(cand):
                cur = cand
                continue
            if cur:
                out.append(cur)
            cur = ""
            if fits(piece):
                cur = piece
            else:
                out.extend(chunks(piece))
        if cur:
            out.append(cur)
        return out

    def chunks(block):
        sentences = [s for s in SENTENCE_RE.split(block) if s]
        if len(sentences) > 1:
            return pack(sentences, " ")
        words = block.split(" ")
        if len(words) > 1:
            return pack(words, " ")
        # One unbreakable word (a very long URL): cut it.
        cut, rest = [], block
        while rest:
            n = len(rest)
            while n > 1 and not fits(rest[:n]):
                n -= 1
            cut.append(rest[:n])
            rest = rest[n:]
        return cut

    if SEPARATOR_RE.search(text):
        posts = []
        for block in SEPARATOR_RE.split(text):
            block = block.strip()
            if block:
                posts.extend([block] if fits(block) else pack(re.split(r"\n\s*\n", block), "\n\n"))
    elif measure(text, mode) <= limit:
        return [text]
    else:
        posts = pack([p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()], "\n\n")
    if numbering and len(posts) > 1:
        posts = [f"{p}\n\n{i}/{len(posts)}" for i, p in enumerate(posts, 1)]
    return posts
