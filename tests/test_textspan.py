from papyrus.textspan import heuristic_abstract, locate

PAGE = """Gated Delta Networks: Improving Mamba2 with Delta Rule
Songlin Yang, Jan Kautz, Ali Hatamizadeh
Abstract
Linear Transformers have gained attention as efﬁcient alternatives to standard Trans-
formers, but their performance in retrieval and long-context tasks has been limited.
To address this limitation, recent work has explored two distinct mech\x02anisms: gating
for adaptive memory control and the delta update rule for precise memory modifications.
We observe that these mechanisms are complementary and introduce the gated delta rule.
1 Introduction
The Transformer architecture has significantly advanced..."""


def test_locate_exact_and_robust():
    got = locate(PAGE, "Linear Transformers have gained attention as efficient alternatives",
                 "are complementary and introduce the gated delta rule")
    assert got.startswith("Linear Transformers have gained attention as efﬁcient") or \
        got.startswith("Linear Transformers have gained attention as efficient")
    assert got.endswith("introduce the gated delta rule.")
    assert "Trans- formers" in got or "Transformers" in got
    assert "mechanisms" in got  # \x02 soft hyphen removed


def test_locate_tolerates_garbled_anchor_edges():
    # model changed the first word and the last word
    got = locate(PAGE, "Linear-time Transformers have gained attention as efficient",
                 "complementary and introduce the gated delta rules")
    assert got and got.startswith("Linear Transformers") and got.endswith("gated delta rule.")


def test_locate_none_when_absent():
    assert locate(PAGE, "completely different words that never appear", "neither do these ones here") is None
    assert locate(PAGE, "", "x") is None


def test_end_must_follow_start():
    assert locate(PAGE, "We observe that these mechanisms are complementary", "Linear Transformers have gained attention") is None


def test_heuristic_abstract():
    h = heuristic_abstract(PAGE)
    assert h.startswith("Linear Transformers") and h.endswith("gated delta rule.")
    assert heuristic_abstract("Payslip\nNet pay 1234") is None
