import datetime as dt

import pytest

from papyrus import ids
from papyrus.ids import ArxivId, find_arxiv_ids, find_dois, find_isbns, normalize_isbn


@pytest.fixture(autouse=True)
def fixed_today(monkeypatch):
    monkeypatch.setattr(ids, "_today", lambda: dt.date(2026, 10, 26))


def one(s):
    found = find_arxiv_ids(s)
    assert len(found) == 1, (s, found)
    return found[0]


@pytest.mark.parametrize(
    "text,aid,version,explicit",
    [
        ("2412.06464", "2412.06464", None, False),
        ("arXiv:2412.06464v3", "2412.06464", "v3", True),
        ("arXiv: 2412.06464", "2412.06464", None, True),
        ("ARXIV:2412.06464V2", "2412.06464", "v2", True),
        ("https://arxiv.org/abs/2412.06464", "2412.06464", None, True),
        ("http://arxiv.org/abs/2412.06464v1", "2412.06464", "v1", True),
        ("https://arxiv.org/pdf/2412.06464v2.pdf", "2412.06464", "v2", True),
        ("https://arxiv.org/pdf/2412.06464", "2412.06464", None, True),
        ("https://arxiv.org/html/2412.06464v1", "2412.06464", "v1", True),
        ("2412.06464v3-2.pdf", "2412.06464", "v3", False),
        ("2412.06464.pdf", "2412.06464", None, False),
        ("/Users/x/Downloads/2405.21060v1 (1).pdf", "2405.21060", "v1", False),
        ("10.48550/arXiv.2412.06464", "2412.06464", None, True),
        ("https://doi.org/10.48550/arxiv.2405.21060", "2405.21060", None, True),
        ("(arXiv:1206.3620)", "1206.3620", None, True),
        ("0704.0001", "0704.0001", None, False),  # the very first new-style id
        ("1412.6980", "1412.6980", None, False),  # Adam, 4-digit era
        ("1501.00001", "1501.00001", None, False),  # 5-digit era starts 2015
        ("arxiv.org/abs/2412.06464%20", "2412.06464", None, True),
        # old style
        ("math/0601001", "math/0601001", None, False),
        ("hep-th/9901001v2", "hep-th/9901001", "v2", False),
        ("arXiv:hep-th/9901001", "hep-th/9901001", None, True),
        ("https://arxiv.org/abs/math/0601001v1", "math/0601001", "v1", True),
        ("https://arxiv.org/pdf/cond-mat/0410550v1", "cond-mat/0410550", "v1", True),
        ("quant-ph/0702001", "quant-ph/0702001", None, False),
        ("math0601001v2.pdf", "math/0601001", "v2", False),
        ("astro-ph/9912345", "astro-ph/9912345", None, False),
    ],
)
def test_arxiv_positive(text, aid, version, explicit):
    a = one(text)
    assert (a.id, a.version, a.explicit) == (aid, version, explicit)


@pytest.mark.parametrize(
    "text",
    [
        "1234.5678",  # month 34
        "1200.12345",  # month 00
        "0612.1234",  # 2006: before new-style ids
        "0703.1234",  # March 2007: still old-style era
        "1501.0001",  # 2015+ needs 5 digits
        "1412.12345",  # pre-2015 has 4 digits
        "2612.12345",  # future (today is 2026-10)
        "3001.12345",
        "tel 2012.03.15",
        "2012.03.15",
        "call 0161 1203.4567",
        "see p. 1203.4567",
        "pp 1203.4567",
        "costs $1203.4567",
        "€1203.45",
        "version 1.2412.06464",  # glued to a bigger number
        "x2412.06464",
        "2412.064641",  # too many digits
        "2412.06464.1",
        "10.1103/1203.4567",  # suffix of another DOI
        "cs.LG/0601001",  # subject-class form is not an id
        "math.AG/0601001",
        "arxiv.org/list/cs.LG/2412",
        "foo/0601001",  # not an archive
        "math/0613001",  # month 13
        "math/0801001",  # old-style ended March 2007
        "math/060100",
        "ISBN 978-0-13-461099-3",
        "Phone: +44 20 7946 0958",
        "",
    ],
)
def test_arxiv_negative(text):
    assert find_arxiv_ids(text) == []


def test_arxiv_multiple_and_dedupe():
    text = (
        "We build on arXiv:2405.21060 and 2412.06464v2; see also https://arxiv.org/abs/2405.21060v3 and hep-th/9901001."
    )
    got = find_arxiv_ids(text)
    assert [a.id for a in got] == ["2405.21060", "2412.06464", "hep-th/9901001"]
    # duplicate merged: explicit + version from the later occurrence
    assert got[0] == ArxivId("2405.21060", "v3", True)


def test_arxiv_explicit_only():
    assert find_arxiv_ids("page 1205.1530 and arXiv:1206.3620", explicit_only=True) == [
        ArxivId("1206.3620", None, True)
    ]


def test_parse_and_props():
    a = ids.parse_arxiv_id("arXiv:2412.06464v3")
    assert a.versioned == "2412.06464v3"
    assert a.doi == "10.48550/arxiv.2412.06464"
    assert not a.is_old_style
    assert ids.parse_arxiv_id("math/0601001").is_old_style
    assert ids.parse_arxiv_id("2405.21060 and 2412.06464") is None
    assert ids.parse_arxiv_id("nothing") is None
    assert ids.normalize_arxiv_id("https://arxiv.org/abs/2412.06464v3") == "2412.06464"
    assert ids.arxiv_id_from_doi("https://doi.org/10.48550/arXiv.2412.06464").id == "2412.06464"
    assert ids.arxiv_id_from_doi("10.1007/s10801-013-0456-7") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://doi.org/10.1007/s10801-013-0456-7", ["10.1007/s10801-013-0456-7"]),
        ("doi:10.1145/3292500.3330701.", ["10.1145/3292500.3330701"]),
        ("DOI: 10.1103/PhysRevLett.116.061102,", ["10.1103/physrevlett.116.061102"]),
        ("(see 10.1016/j.cell.2020.01.001)", ["10.1016/j.cell.2020.01.001"]),
        ("10.1016/0004-3702(85)90019-0", ["10.1016/0004-3702(85)90019-0"]),  # balanced parens kept
        ("(10.1016/0004-3702(85)90019-0)", ["10.1016/0004-3702(85)90019-0"]),
        ("https://dl.acm.org/doi/pdf/10.1145/3292500.3330701", ["10.1145/3292500.3330701"]),
        ("https://onlinelibrary.wiley.com/doi/10.1002/andp.19053221004/abstract", ["10.1002/andp.19053221004"]),
        ("https://link.springer.com/content/pdf/10.1007/978-3-030-34372-9.pdf", ["10.1007/978-3-030-34372-9"]),
        ("https://doi.org/10.1007%2Fs10801-013-0456-7", ["10.1007/s10801-013-0456-7"]),
        ("10.48550/arXiv.2412.06464", ["10.48550/arxiv.2412.06464"]),
        ("a 10.1000/182 b 10.1000/182", ["10.1000/182"]),
        # unicode brackets / quotes / dashes after the DOI (PDF text)
        ("doi ⟨10.1177/23998083261460673⟩.", ["10.1177/23998083261460673"]),
        ("〈10.1177/23998083261460673〉", ["10.1177/23998083261460673"]),
        ("“10.1016/j.cell.2020.01.001”", ["10.1016/j.cell.2020.01.001"]),
        ("«10.1007/s10801-013-0456-7»", ["10.1007/s10801-013-0456-7"]),
        ("see 10.1145/3292500.3330701—and more", ["10.1145/3292500.3330701"]),
        ("10.1145/3292500.3330701’s", ["10.1145/3292500.3330701"]),
        # placeholders from journal templates
        ("10.1017/xxxxx", []),
        ("DOI 10.1000/XXXXXXX", []),
        ("10.1234/0000000", []),
        ("10.1093/mnras/xxx", []),
        ("10.1016/j.xxxx.2020.xxx", []),
        ("10.0000/abc", []),
        ("10.12/abc", []),  # registrant too short
        ("10.1234/", []),
        ("no doi here 2024", []),
    ],
)
def test_dois(text, expected):
    assert find_dois(text) == expected


def test_doi_helpers():
    assert ids.normalize_doi(" https://doi.org/10.1007/S10801-013-0456-7 ") == "10.1007/s10801-013-0456-7"
    assert ids.normalize_doi("nope") is None
    assert ids.is_arxiv_doi("10.48550/arxiv.2412.06464")
    assert not ids.is_arxiv_doi("10.1007/x")
    assert not ids.is_arxiv_doi(None)


@pytest.mark.parametrize(
    "s,expected",
    [
        ("978-0-13-461099-3", "9780134610993"),
        ("9780134610993", "9780134610993"),
        ("0-13-461099-7", "9780134610993"),  # ISBN-10 converted
        ("0134610997", "9780134610993"),
        ("ISBN-13: 978-1-292-40113-3", "9781292401133"),
        ("ISBN 0-8044-2957-X", "9780804429573"),  # X check digit
        ("080442957x", "9780804429573"),
        ("978-0-13-461099-4", None),  # bad checksum
        ("0-13-461099-8", None),
        ("123456789", None),
        ("9770134610993", None),  # not 978/979
    ],
)
def test_normalize_isbn(s, expected):
    assert normalize_isbn(s) == expected


def test_isbn_conversions():
    assert ids.isbn13_to_10("9780134610993") == "0134610997"
    assert ids.isbn13_to_10("9780804429573") == "080442957X"
    assert ids.isbn13_to_10("9791234567896") is None
    assert ids.isbn10_to_13("0134610997") == "9780134610993"


def test_find_isbns():
    text = (
        "Artificial Intelligence: A Modern Approach, 4th ed. ISBN 978-0-13-461099-3. "
        "Global edition 9781292401133. ISBN-10: 0-13-461099-7. "
        "Call 0134610997 or 0207-946-0958; bad 9781292401134; 1-2-3-4."
    )
    assert find_isbns(text) == ["9780134610993", "9781292401133"]
    assert find_isbns("ISBN 0-8044-2957-X") == ["9780804429573"]
    assert find_isbns("hyphenated 0-8044-2957-X alone") == ["9780804429573"]
    assert find_isbns("order no. 1234567890") == []


def test_detect():
    text = "arXiv:2412.06464v3 [cs.LG] doi:10.48550/arXiv.2412.06464 ISBN 978-0-13-461099-3"
    d = ids.detect(text)
    assert d.best_arxiv == ArxivId("2412.06464", "v3", True)
    assert d.dois == ["10.48550/arxiv.2412.06464"]
    assert d.isbns == ["9780134610993"]
    assert ids.detect("page 1205.1530, arXiv:1206.3620").best_arxiv.id == "1206.3620"
    assert ids.detect("").best_arxiv is None


def test_text_helpers():
    assert (
        ids.normalize_title("Transformers are SSMs: Generalized  Models!") == "transformers are ssms generalized models"
    )
    assert ids.normalize_title("The $\\mathcal{O}(n)$ Café & <i>Bar</i>") == "the o n cafe and bar"
    assert ids.split_name("Tri Dao") == ("Tri", "Dao")
    assert ids.split_name("Dao, Tri") == ("Tri", "Dao")
    assert ids.split_name("Ludwig van Beethoven") == ("Ludwig", "van Beethoven")
    assert ids.split_name("Martin Luther King Jr.") == ("Martin Luther", "King")
    assert ids.split_name("Plato") == (None, "Plato")
    assert ids.surname_key("Johannes van der Waals") == "waals"
    assert ids.surname_key("Paul Erdős") == "erdos"
