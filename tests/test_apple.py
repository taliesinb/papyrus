"""Tests for the Apple Foundation Models backend (papyrus.llm.apple + tools/afm-bridge).

Live tests are skipped unless the bridge binary is built
(`cd tools/afm-bridge && swift build -c release`, or $PAPYRUS_AFM_BIN) and the on-device model
reports itself ready.
"""

from __future__ import annotations

import pytest

from papyrus.llm.apple import (
    AppleFMBackend,
    AppleFMContextOverflow,
    _fill_missing,
    _with_order,
    estimate_tokens,
    find_binary,
    fit_prompt,
    truncate_to_tokens,
)
from papyrus.llm.base import Backend, BackendUnavailable

# ---------------------------------------------------------------- pure-Python helpers


def test_estimate_tokens_is_conservative_and_monotone():
    assert estimate_tokens("") == 0
    text = "Attention Is All You Need. Ashish Vaswani, Noam Shazeer."  # 18 real AFM tokens
    assert estimate_tokens(text) >= 18
    assert estimate_tokens(text * 2) >= estimate_tokens(text)
    assert estimate_tokens("日本語のテキスト") == 8  # one token per non-ASCII char


def test_truncate_to_tokens():
    text = "word " * 1000
    cut = truncate_to_tokens(text, 100)
    assert estimate_tokens(cut) <= 100
    assert len(cut) > 200
    assert text.startswith(cut)
    assert truncate_to_tokens("short", 100) == "short"
    assert truncate_to_tokens("anything", 0) == ""


def test_fit_prompt_budget():
    schema = {"type": "object", "properties": {"t": {"type": "string"}}}
    long = "x " * 50_000
    fitted = fit_prompt(instructions="Extract.", prompt=long, schema=schema, max_tokens=1000, context_tokens=4096)
    assert estimate_tokens(fitted) <= 4096 - 1000
    with pytest.raises(AppleFMContextOverflow):
        fit_prompt(instructions="x" * 20_000, prompt="p", schema=schema, max_tokens=1000, context_tokens=4096)


def test_with_order_and_fill_missing():
    schema = {
        "type": "object",
        "properties": {
            "b": {"type": "string"},
            "a": {"type": "array", "items": {"type": "object", "properties": {"z": {"type": "string"}, "y": {"type": "integer"}}}},
        },
    }
    ordered = _with_order(schema)
    assert ordered["x-order"] == ["b", "a"]
    assert ordered["properties"]["a"]["items"]["x-order"] == ["z", "y"]
    assert "x-order" not in schema  # input not mutated
    assert _fill_missing({"a": [{"z": "q"}]}, schema) == {"a": [{"z": "q", "y": None}], "b": None}


def test_missing_binary_raises_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPYRUS_AFM_BIN", str(tmp_path / "nope"))
    monkeypatch.setattr("papyrus.llm.apple.DEFAULT_BIN", tmp_path / "nope")  # hide any repo build
    b = AppleFMBackend()
    assert b.binary is None
    assert not b.available()
    with pytest.raises(BackendUnavailable):
        b.extract(instructions="", prompt="x", schema={"type": "object", "properties": {}})


# ---------------------------------------------------------------- live tests


@pytest.fixture(scope="module")
def backend():
    if find_binary() is None:
        pytest.skip("papyrus-afm not built (cd tools/afm-bridge && swift build -c release)")
    b = AppleFMBackend()
    a = b.availability(probe=True)
    if not a.get("available"):
        b.close()
        pytest.skip(f"Apple Foundation Model not available: {a.get('reason')}")
    yield b
    b.close()


ARXIV_PAGE = """Filename: 2501.01234v1.pdf
Found at: /Users/someone/Downloads/2501.01234v1.pdf
Downloaded from: https://arxiv.org/pdf/2501.01234v1
Pages: 12
PDF metadata: creator=LaTeX with hyperref; producer=pdfTeX-1.40.26

--- page 1 ---
Sparse Lattice Transformers for Protein Folding
Maria Gonzalez1, Wei Chen2, Aditya Raman1
1University of Edinburgh, 2Tsinghua University
arXiv:2501.01234v1 [q-bio.BM] 3 Jan 2025
Abstract
We introduce sparse lattice transformers, an attention architecture that restricts attention
to neighbours on a learned 3D lattice. On CASP15 targets our model matches dense attention
accuracy while using 6x less memory, enabling folding of proteins with up to 4,000 residues
on a single GPU.
1 Introduction
Predicting protein structure from sequence has been transformed by deep learning...
"""

EXTRACT_SCHEMA_TEST = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["paper", "book", "slides", "personal", "other"]},
        "is_library_material": {"type": "boolean"},
        "title": {"type": "string"},
        "authors": {"type": "array", "items": {"type": "string"}},
        "year": {"type": "integer"},
        "abstract": {"type": "string"},
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "field": {"type": "string"}},
                "required": ["name", "field"],
            },
        },
    },
    "required": ["kind", "is_library_material", "title", "authors", "year", "abstract", "topics"],
}


def test_protocol_and_info(backend):
    assert isinstance(backend, Backend)
    info = backend.info()
    assert info.backend == "apple"
    assert info.tag.startswith("apple:")
    assert info.context_tokens and info.context_tokens >= 4096


def test_count_tokens_and_estimate_is_conservative(backend):
    n = backend.count_tokens(ARXIV_PAGE)
    assert 50 < n < len(ARXIV_PAGE)
    assert estimate_tokens(ARXIV_PAGE) >= n


def test_live_arxiv_extraction(backend):
    r = backend.extract(
        instructions="You catalogue documents for a research library. Extract bibliographic metadata "
        "using only information in the input. Copy the abstract verbatim.",
        prompt=ARXIV_PAGE,
        schema=EXTRACT_SCHEMA_TEST,
        max_tokens=800,
    )
    assert r["kind"] == "paper"
    assert r["is_library_material"] is True
    assert "sparse lattice transformer" in r["title"].lower()
    assert any("Gonzalez" in a for a in r["authors"])
    assert len(r["authors"]) == 3
    assert r["year"] == 2025
    assert "CASP15" in r["abstract"]
    assert r["topics"] and all(set(t) >= {"name", "field"} for t in r["topics"])
    meta = backend.last_response
    assert meta["ok"] and meta["mode"] in ("guided", "unguided_fallback")


def test_live_long_prompt_is_truncated(backend):
    prompt = ARXIV_PAGE + ("\nFiller paragraph about lattice models of proteins and attention. " * 3000)
    r = backend.extract(
        instructions="Extract the title.",
        prompt=prompt,
        schema={"type": "object", "properties": {"title": {"type": "string"}}, "required": ["title"]},
        max_tokens=100,
    )
    assert "lattice" in r["title"].lower()
    assert backend.last_response["python_truncated"] or backend.last_response.get("truncated")


def test_live_schema_types(backend):
    schema = {
        "type": "object",
        "properties": {
            "colour": {"type": "string", "enum": ["red", "green", "blue"]},
            "count": {"type": "integer", "minimum": 0, "maximum": 10},
            "ratio": {"type": "number"},
            "flag": {"type": "boolean"},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        },
        "required": ["colour", "count", "ratio", "flag", "tags"],
    }
    r = backend.extract(
        instructions="Fill the fields from the text.",
        prompt="Three green apples, half of them ripe, are on the table; tag them as fruit, food.",
        schema=schema,
        max_tokens=200,
    )
    assert r["colour"] in {"red", "green", "blue"}
    assert isinstance(r["count"], int) and 0 <= r["count"] <= 10
    assert isinstance(r["ratio"], (int, float))
    assert isinstance(r["flag"], bool)
    assert isinstance(r["tags"], list) and len(r["tags"]) <= 3


def test_raw_errors_are_json(backend):
    r = backend.request({"op": "nonsense"})
    assert r["ok"] is False and r["kind"] == "other"
    r = backend.request({"op": "extract", "prompt": "x", "schema": {"type": "object", "properties": {"a": {"type": "date"}}}})
    assert r["ok"] is False and "unsupported type" in r["error"]
