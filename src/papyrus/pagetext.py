"""The pages of a document as the models see them: cleaned up, and chosen within a token budget.

Triage's state (`triage.build_state`), which the screen, the classifier and the extraction model
all read, shows a document's first pages and, for a longer one, its last ones:

1. If every page fits in the budget, all of them, in order.
2. Otherwise the tail first, with `tail_fraction` of the budget: from the last page backwards,
   whole pages while they fit; the page that doesn't is cut (its start kept) and the walk stops.
3. Then the front, from page 1 up to the tail, with what is left: whole pages, then the page that
   doesn't fit, cut. If the front reaches the tail, its leftover goes to the tail's cut page.

No page appears twice; `...` on a line of its own marks every place text was left out; runs of
pages without text are one line, `--- page 2-5 (no text) ---`. Pages are read on demand, so a
2,000-page book costs a few pages' extraction.

Page text is normalised first (`normalize`): invisible characters dropped, words that small caps
split rejoined, runs of filler characters squashed, consecutive duplicate lines dropped.

Tokens are estimated (`estimate_tokens`), not counted with a model's tokenizer: on 1,200 pages
of the libraries' PDFs the estimate was within -5%/+12% of Qwen3.5's count for 80% of pages and
3% high overall.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources

ELLIPSIS = "..."
MIN_CUT = 12  # tokens: a cut page shows at least this much text, or isn't shown

# --- tokens -------------------------------------------------------------------------------------

_UNIT = re.compile(r"[A-Za-z]+|\d|\n|[^\w\s]|[^\x00-\x7f]")


def _unit_cost(s: str) -> int:
    return 1 + (len(s) - 1) // 8 if s[0].isascii() and s[0].isalpha() else 1


def estimate_tokens(text: str) -> int:
    """About as many tokens as a BPE tokenizer (Qwen3.5's) makes of `text`: a word is one token
    plus one per further 8 letters; a digit, a newline, a punctuation mark or a non-ASCII
    character is one."""
    return sum(_unit_cost(m.group()) for m in _UNIT.finditer(text))


def cut_tokens(text: str, n: int) -> str:
    """The longest start of `text` within about `n` tokens, ending at a word boundary when one is
    near."""
    used = 0
    for m in _UNIT.finditer(text):
        used += _unit_cost(m.group())
        if used > n:
            head = text[: m.start()]
            ws = max(head.rfind(" "), head.rfind("\n"))
            if ws > 0 and ws >= len(head) - 40:
                head = head[:ws]
            return head.rstrip()
    return text


# --- normalising page text ----------------------------------------------------------------------

# Nothing to read: U+FFFC (poppler's placeholder for an embedded image), U+2060 (word joiner),
# the private-use area (icon fonts: U+F0E0 is Font Awesome's envelope).
_INVISIBLE = re.compile("[\ufffc\u2060\ue000-\uf8ff]")
FILLERS = [".", "\u00b7", "\u22c5", "\u2014", "_", "-", "+", "=", "*"]  # . · ⋅ — _ - + = *
_RUN = re.compile("|".join(f"{re.escape(c)}{{4,}}" for c in FILLERS))  # XXXX… -> XXX
_SPACED = re.compile("|".join(f"{re.escape(c)}(?: {re.escape(c)}){{3,}}" for c in FILLERS))  # X X X X… -> X X X
_SPLIT_CAPS = re.compile(r"\b([A-Z]) ([A-Z]{2,})\b")
_SUFFIXES = [("s", ""), ("es", ""), ("ies", "y"), ("ied", "y"), ("ier", "y"), ("iest", "y"), ("ed", ""), ("ed", "e"),
             ("ing", ""), ("ing", "e")]


@lru_cache(maxsize=1)
def words() -> frozenset[str]:
    """data/words.txt: common English words (12dicts' 2of12), written at build time by
    tools/words/fetch.py. Empty if it hasn't been fetched: small caps are then left as they are."""
    try:
        return frozenset(resources.files("papyrus").joinpath("data", "words.txt").read_text().split())
    except (FileNotFoundError, OSError):
        return frozenset()


def is_word(w: str, vocabulary: frozenset[str] | None = None) -> bool:
    """A common English word, or one with a plural, verb or comparative ending (the list has
    none: 'network', not 'networks'). Stems have at least 3 letters, so 'ated' isn't 'ate' + 'd'."""
    vocab = words() if vocabulary is None else vocabulary
    w = w.lower()
    if w in vocab:
        return True
    for suf, add in _SUFFIXES:
        if w.endswith(suf) and len(w) > len(suf) + 2 and w[: -len(suf)] + add in vocab:
            return True
    for suf in ("ing", "ed", "er", "est"):  # a doubled final consonant: embedding, mapped, bigger
        stem = w[: -len(suf)]
        if w.endswith(suf) and len(stem) > 3 and stem[-1] == stem[-2] and stem[-1] not in "aeiou" \
                and stem[:-1] in vocab:
            return True
    return False


def unsplit_small_caps(text: str, vocabulary: frozenset[str] | None = None) -> str:
    """Rejoin words that small caps split after their first capital (poppler: 'G ATED D ELTA'):
    'X YYY' becomes 'XYYY' when YYY isn't a word and XYYY is."""
    vocab = words() if vocabulary is None else vocabulary
    if not vocab:
        return text

    def fix(m: re.Match) -> str:
        a, b = m.group(1), m.group(2)
        return a + b if not is_word(b, vocab) and is_word(a + b, vocab) else m.group(0)

    return _SPLIT_CAPS.sub(fix, text)


def normalize(text: str) -> str:
    """Page text as the models see it: invisible characters dropped, small caps rejoined, runs of
    a filler character squashed to three ('.......' -> '...', '. . . . .' -> '. . .'), then
    consecutive duplicate lines (blank ones included) dropped."""
    text = re.sub(" {2,}", " ", _INVISIBLE.sub("", text))
    text = unsplit_small_caps(text)
    text = _RUN.sub(lambda m: m.group(0)[0] * 3, text)
    text = _SPACED.sub(lambda m: " ".join(m.group(0)[0] * 3), text)
    out: list[str] = []
    for line in text.split("\n"):
        if out and line.strip() == out[-1].strip():
            continue
        out.append(line)
    return "\n".join(out).strip()


# --- choosing pages -----------------------------------------------------------------------------


@dataclass
class Block:
    """One page with text, or a run of pages without any."""

    first: int
    last: int
    text: str = ""
    cut: bool = False  # text was left out after this block

    @property
    def blank(self) -> bool:
        return not self.text

    @property
    def pages(self) -> list[int]:
        return list(range(self.first, self.last + 1))

    def render(self) -> str:
        if self.blank:
            span = str(self.first) if self.first == self.last else f"{self.first}-{self.last}"
            s = f"--- page {span} (no text) ---"
        else:
            s = f"--- page {self.first} ---\n{self.text}"
        return s + ("\n" + ELLIPSIS if self.cut else "")


def _cost(b: Block) -> int:
    return estimate_tokens(b.render()) + 1  # +1: the newline joining blocks


def _fill(order: list[int], text: Callable[[int], str], budget: int) -> tuple[list[Block], int, bool]:
    """Walk `order` (ascending or descending): whole pages while they fit, pages without text
    merged into runs, then the first page with text that doesn't fit, cut. Returns the blocks in
    walk order, the tokens used, and whether every page was walked."""
    blocks: list[Block] = []
    used = 0
    forward = len(order) < 2 or order[1] > order[0]
    for p in order:
        t = text(p)
        if not t:
            prev = blocks[-1] if blocks else None
            if prev and prev.blank and (prev.last + 1 == p if forward else prev.first - 1 == p):
                grown = Block(min(prev.first, p), max(prev.last, p))
                delta = _cost(grown) - _cost(prev)
                if used + delta > budget:
                    return blocks, used, False
                blocks[-1], used = grown, used + delta
            else:
                c = _cost(Block(p, p))
                if used + c > budget:
                    return blocks, used, False
                blocks.append(Block(p, p))
                used += c
            continue
        c = _cost(Block(p, p, t))
        if used + c <= budget:
            blocks.append(Block(p, p, t))
            used += c
            continue
        room = budget - used - (_cost(Block(p, p, "x", cut=True)) - 1)
        if room >= MIN_CUT:
            b = Block(p, p, cut_tokens(t, room), cut=True)
            blocks.append(b)
            used += _cost(b)
        return blocks, used, False
    return blocks, used, True


def _merge(blocks: list[Block]) -> list[Block]:
    out: list[Block] = []
    for b in blocks:
        if out and out[-1].blank and b.blank and not out[-1].cut and out[-1].last + 1 == b.first:
            out[-1] = Block(out[-1].first, b.last, cut=b.cut)
        else:
            out.append(b)
    return out


@dataclass
class Selection:
    body: str
    front: list[int] = field(default_factory=list)  # pages shown from the front
    tail: list[int] = field(default_factory=list)  # pages shown from the back
    complete: bool = False  # every page shown in full


def select_pages(n_pages: int, text: Callable[[int], str], budget: int, tail_fraction: float = 0.2) -> Selection:
    """The page section of a state within about `budget` tokens. `text(p)` is page p's
    normalised text (1-based; '' for a page without text)."""
    if n_pages <= 0:
        return Selection("", complete=True)
    blocks, _, complete = _fill(list(range(1, n_pages + 1)), text, budget)
    if complete:
        return Selection("\n".join(b.render() for b in blocks), [p for b in blocks for p in b.pages], [], True)
    tail_budget = round(budget * tail_fraction)
    tail: list[Block] = []
    tail_used = 0
    if tail_budget > 0:
        tail, tail_used, _ = _fill(list(range(n_pages, 1, -1)), text, tail_budget)
        tail.reverse()
    first_tail = tail[0].first if tail else n_pages + 1
    front, front_used, reached = _fill(list(range(1, first_tail)), text, budget - tail_used)
    if not reached and front:
        front[-1].cut = True  # a gap follows
    if reached and tail and tail[0].cut:  # the front ran into the tail: its leftover goes to the tail
        leftover = budget - tail_used - front_used
        tail, _, _ = _fill(list(range(n_pages, first_tail - 1, -1)), text, tail_budget + leftover)
        tail.reverse()
    return Selection("\n".join(b.render() for b in _merge(front + tail)),
                     [p for b in front for p in b.pages], [p for b in tail for p in b.pages])
