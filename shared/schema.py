"""A small JSON Schema checker for model output.

Covers the subset the extractors' schemas use: type (single or list, including "null"), properties,
required, additionalProperties: false, items, enum, minimum, maximum and pattern. It exists so both
Claude backends (the API and the local `claude` CLI) reject a malformed answer the same way, without
adding a dependency. Returns a list of problems; empty means valid.
"""

from __future__ import annotations

import re

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is(value, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def problems(schema: dict, value, path: str = "$") -> list[str]:
    out: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_is(value, t) for t in allowed):
            return [f"{path}: expected {'/'.join(allowed)}, got {type(value).__name__}"]
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        out.append(f"{path}: {value!r} not in enum")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} < minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} > maximum {schema['maximum']}")
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        out.append(f"{path}: {value!r} does not match pattern")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k in schema.get("required", []):
            if k not in value:
                out.append(f"{path}: missing required '{k}'")
        if schema.get("additionalProperties") is False:
            out += [f"{path}: unexpected '{k}'" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                out += problems(sub, value[k], f"{path}.{k}")
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            out += problems(schema["items"], item, f"{path}[{i}]")
    return out
