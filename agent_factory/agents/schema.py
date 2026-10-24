"""Minimal JSON-Schema subset used to validate model output.

Supports ``type`` (object, array, string, boolean, integer, number),
``properties``, ``required``, ``additionalProperties`` (``false``, or a
schema), ``propertyNames`` (``maxLength``/``pattern``), ``items``, ``enum``,
``minLength``/``maxLength``, ``pattern``, ``minItems``/``maxItems``,
``maxProperties``, ``minimum``/``maximum``. Schemas are plain dicts so real
providers can pass them to APIs that accept JSON Schema.

A model's answer is untrusted: an unexpected key is an error, not something
to ignore, and every string and list is bounded.
"""

from __future__ import annotations

import re
from typing import Any

_TYPES: dict[str, type | tuple[type, ...]] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
}


def validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    expected = schema.get("type")
    if expected:
        py_type = _TYPES[expected]
        is_bool = isinstance(instance, bool)
        if not isinstance(instance, py_type) or (is_bool and expected in ("integer", "number")):
            return [f"{path}: expected {expected}, got {type(instance).__name__}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} not one of {schema['enum']}")
    if isinstance(instance, str):
        if len(instance) < schema.get("minLength", 0):
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: does not match {schema['pattern']!r}")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: below {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: above {schema['maximum']}")
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: missing required key {key!r}")
        if "maxProperties" in schema and len(instance) > schema["maxProperties"]:
            errors.append(f"{path}: more than {schema['maxProperties']} properties")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties")
        names = schema.get("propertyNames")
        for key, value in instance.items():
            if isinstance(names, dict):
                errors.extend(validate(key, {"type": "string", **names}, f"{path}<key {key[:40]!r}>"))
            if key in props:
                errors.extend(validate(value, props[key], f"{path}.{key}"))
            elif extra is False:
                errors.append(f"{path}: unexpected key {key[:40]!r}")
            elif isinstance(extra, dict):
                errors.extend(validate(value, extra, f"{path}[{key[:40]!r}]"))
    if isinstance(instance, list):
        if len(instance) < schema.get("minItems", 0):
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(instance):
                errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors[:20]
