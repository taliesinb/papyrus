import re

import pytest

from papyrus import pagetext
from papyrus.pagetext import cut_tokens, estimate_tokens, normalize, select_pages, unsplit_small_caps

WORDS = frozenset("gated delta network improve mamba abstract rule with act scan book the he roof proof embed".split())


def _pages(selection_body: str) -> list[int]:
    out = []
    for a, b in re.findall(r"^--- page (\d+)(?:-(\d+))?(?: \(no text\))? ---$", selection_body, re.M):
        out += range(int(a), int(b or a) + 1)
    return out


def test_estimate_and_cut():
    assert estimate_tokens("") == 0
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("internationalization") == 3  # 20 letters: 1 + 19 // 8
    assert estimate_tokens("2024") == 4  # digits one by one, as Qwen splits them
    assert estimate_tokens("a, b.\n") == 5
    text = " ".join(f"word{i}" for i in range(200))
    head = cut_tokens(text, 50)
    assert estimate_tokens(head) <= 50 and text.startswith(head) and not head.endswith(" ")
    assert cut_tokens("short", 50) == "short"


def test_normalize_squashes_fillers_and_duplicate_lines():
    assert normalize("2.1 Types . . . . . . . . 9") == "2.1 Types . . . 9"
    assert normalize("Intro ........... 3") == "Intro ... 3"
    assert normalize("a == b, x --- y, ...") == "a == b, x --- y, ..."  # runs of three stay
    assert normalize("\u2014\u2014\u2014\u2014\u2014 \u00b7 \u00b7 \u00b7 \u00b7") == "\u2014\u2014\u2014 \u00b7 \u00b7 \u00b7"
    assert normalize("head\nhead\n\n\n\nfoo\nfoo \nbar\nhead") == "head\n\nfoo\nbar\nhead"


def test_normalize_drops_invisible_characters():
    # poppler puts U+FFFC where a page has an image; alternating with blank lines, they hid from
    # the duplicate-line rule
    assert normalize("19\n\n\ufffc\n\n\ufffc\n\n\ufffc\nnext") == "19\n\nnext"
    assert normalize("mail \uf0e0 me\u2060x") == "mail mex"


def test_small_caps_rejoined_only_when_it_makes_a_word():
    u = lambda s: unsplit_small_caps(s, WORDS)  # noqa: E731
    assert u("G ATED D ELTA N ETWORKS :") == "GATED DELTA NETWORKS :"
    assert u("I MPROVING M AMBA 2 WITH D ELTA RULE") == "IMPROVING MAMBA 2 WITH DELTA RULE"
    assert u("E MBEDDING") == "EMBEDDING"  # doubled consonant
    assert u("A BOOK") == "A BOOK"  # the tail is a word
    assert u("T HE P ROOF") == "T HE P ROOF"  # so is this one: left alone
    assert u("A GPU") == "A GPU"  # nor is the whole
    assert unsplit_small_caps("G ATED", frozenset()) == "G ATED"  # no word list: unchanged


def test_all_pages_when_they_fit():
    pages = {1: "Title page", 2: "", 3: "", 4: "Body text", 5: ""}
    s = select_pages(5, pages.get, 1000)
    assert s.complete and s.body == "--- page 1 ---\nTitle page\n--- page 2-3 (no text) ---\n--- page 4 ---\nBody text\n" \
                                     "--- page 5 (no text) ---"
    assert "..." not in s.body


def _book(n: int, words: int = 300) -> dict[int, str]:
    return {p: " ".join(f"p{p}w{i}" for i in range(words)) for p in range(1, n + 1)}


@pytest.mark.parametrize("budget", [120, 400, 1000, 3000])
@pytest.mark.parametrize("tail", [0.0, 0.2, 0.5])
def test_front_and_tail_within_budget_without_repeats(budget, tail):
    book = _book(40)
    s = select_pages(40, book.get, budget, tail)
    shown = _pages(s.body)
    assert shown == sorted(set(shown)) and shown[0] == 1  # in order, no page twice
    assert estimate_tokens(s.body) <= budget + 5
    if tail and budget >= 400:  # below that the tail's share can't hold a page header and some text
        assert shown[-1] == 40 and s.tail and s.tail[-1] == 40
    blocks = re.split(r"(?m)^(?=--- page)", s.body)[1:]
    for a, b in zip(blocks, blocks[1:]):  # every gap is marked with '...' on its own line
        if _pages(b)[0] != _pages(a)[-1] + 1:
            assert a.rstrip().endswith("\n...")
    if not tail:
        assert s.body.endswith("\n...")


def test_blank_pages_cost_one_line_and_long_books_read_few_pages():
    read = []

    def text(p):
        read.append(p)
        return "Title" if p == 1 else "" if p <= 300 else " ".join(f"w{i}" for i in range(400))

    s = select_pages(2000, text, 1000, 0.2)
    assert "--- page 1 ---\nTitle\n--- page 2-300 (no text) ---\n--- page 301 ---\n" in s.body
    assert s.tail == [2000] and len(set(read)) < 320  # not the whole book


def test_words_list_missing_is_harmless(monkeypatch):
    monkeypatch.setattr(pagetext, "words", lambda: frozenset())
    assert normalize("G ATED") == "G ATED"
