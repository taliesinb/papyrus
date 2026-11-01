"""Tiny JSON-Schema-subset validator/coercer + robust JSON-object extraction.

Local servers do not always enforce `response_format` (e.g. LM Studio with
reasoning models routes the answer into `reasoning_content` and may ignore the
schema), so every backend result goes through `coerce()`.
"""

from __future__ import annotations

import json
import re
from typing import Any


_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class SchemaError(ValueError):
    pass


def extract_json_object(text: str) -> dict[str, Any]:
    """Find the first top-level JSON object in `text` (handles ```json fences, prose around it)."""
    if not text:
        raise SchemaError("empty model output")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(obj, dict):
                        return obj
                    break
        start = text.find("{", start + 1)
    raise SchemaError(f"no JSON object found in model output: {text[:200]!r}")


def coerce(value: Any, schema: dict[str, Any], path: str = "$") -> Any:
    """Validate `value` against the subset schema, coercing harmless mismatches.

    Unknown object keys are dropped; missing required keys raise SchemaError;
    enum strings are matched case-insensitively; numbers-as-strings are parsed;
    'null'/'' for optional scalars become None; null items of arrays are dropped.
    """
    t = schema.get("type")
    if isinstance(t, list):  # e.g. ["string", "null"]
        if value is None and "null" in t:
            return None
        t = next((x for x in t if x != "null"), None)
    if t == "object":
        if not isinstance(value, dict):
            raise SchemaError(f"{path}: expected object")
        props = schema.get("properties", {})
        out = {}
        for k, sub in props.items():
            if k in value:
                out[k] = coerce(value[k], sub, f"{path}.{k}")
        missing = [k for k in schema.get("required", []) if k not in out]
        if missing:
            raise SchemaError(f"{path}: missing required {missing}")
        return out
    if t == "array":
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        items = schema.get("items", {})
        # a null item ([null], ["", "Jane Doe"]) is never meant: small models write it for "none"
        out = [coerce(v, items, f"{path}[{i}]") for i, v in enumerate(value) if v is not None]
        return [v for v in out if v is not None]
    if value is None or (isinstance(value, str) and value.strip().lower() in {"", "null", "none", "n/a", "unknown"}):
        if t == "string" and "enum" not in schema:
            return None
        if t in {"integer", "number", "boolean"}:
            return None
    if t == "string":
        if not isinstance(value, str):
            value = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        value = _CONTROL.sub("", value)
        if "enum" in schema:
            for e in schema["enum"]:
                if value.strip().lower() == str(e).lower():
                    return e
            raise SchemaError(f"{path}: {value!r} not in {schema['enum']}")
        return value.strip()
    if t == "integer":
        if isinstance(value, bool):
            raise SchemaError(f"{path}: expected integer")
        if isinstance(value, (int, float)):
            return int(value)
        m = re.search(r"-?\d+", str(value))
        if not m:
            raise SchemaError(f"{path}: expected integer, got {value!r}")
        return int(m.group())
    if t == "number":
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        try:
            return float(str(value).strip())
        except ValueError:
            raise SchemaError(f"{path}: expected number, got {value!r}") from None
    if t == "boolean":
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in {"true", "yes", "1"}:
            return True
        if s in {"false", "no", "0"}:
            return False
        raise SchemaError(f"{path}: expected boolean, got {value!r}")
    return value
