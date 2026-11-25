"""Проверка ответа модели по JSON-схеме запроса, без внешних зависимостей.

``claude -p --json-schema`` уже проверяет ответ на своей стороне, но записанные ответы (кеш, replay,
фикстуры) и будущий SDK-бэкенд идут мимо неё, поэтому клиент проверяет ``structured`` сам.
Поддержано подмножество JSON Schema, которым пользуются схемы шагов: ``type`` (строка или список),
``properties``, ``required``, ``additionalProperties: false``, ``items``, ``enum``, ``const``,
``minItems``, ``maxItems``, ``minLength``, ``maxLength``, ``anyOf``. Остальные ключевые слова не проверяются.
Схемы шагов с ``$ref`` и прочим лучше держать плоскими, чтобы проверка их видела.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_ERRORS = 5

_TYPE_CHECKS: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "null": (type(None),),
}


def _type_ok(value: Any, name: str) -> bool:
    expected = _TYPE_CHECKS.get(name)
    if expected is None:
        return True
    if isinstance(value, bool) and name in ("number", "integer"):
        return False
    if name == "integer" and isinstance(value, float):
        return value.is_integer()
    return isinstance(value, expected)


def _check_type(value: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    declared = schema.get("type")
    if declared is None:
        return []
    names = declared if isinstance(declared, list) else [declared]
    if any(_type_ok(value, str(n)) for n in names):
        return []
    return [f"{path}: ожидался тип {'|'.join(map(str, names))}, получен {type(value).__name__}"]


def _check_object(value: dict[str, Any], schema: Mapping[str, Any], path: str) -> list[str]:
    errors = [f"{path}: нет обязательного поля «{k}»" for k in schema.get("required", []) if k not in value]
    props = schema.get("properties")
    props = props if isinstance(props, Mapping) else {}
    if schema.get("additionalProperties") is False:
        errors += [f"{path}: лишнее поле «{k}»" for k in value if k not in props]
    for key, sub in props.items():
        if key in value and isinstance(sub, Mapping):
            errors += _validate(value[key], sub, f"{path}.{key}")
    return errors


def _check_array(value: list[Any], schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(schema.get("minItems"), int) and len(value) < schema["minItems"]:
        errors.append(f"{path}: элементов {len(value)}, нужно не меньше {schema['minItems']}")
    if isinstance(schema.get("maxItems"), int) and len(value) > schema["maxItems"]:
        errors.append(f"{path}: элементов {len(value)}, нужно не больше {schema['maxItems']}")
    items = schema.get("items")
    if isinstance(items, Mapping):
        for i, item in enumerate(value):
            errors += _validate(item, items, f"{path}[{i}]")
            if len(errors) >= MAX_ERRORS:
                break
    return errors


def _check_string(value: str, schema: Mapping[str, Any], path: str) -> list[str]:
    errors: list[str] = []
    if isinstance(schema.get("minLength"), int) and len(value) < schema["minLength"]:
        errors.append(f"{path}: строка короче {schema['minLength']} символов")
    if isinstance(schema.get("maxLength"), int) and len(value) > schema["maxLength"]:
        errors.append(f"{path}: строка длиннее {schema['maxLength']} символов")
    return errors


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> list[str]:
    any_of = schema.get("anyOf")
    variants = [s for s in any_of if isinstance(s, Mapping)] if isinstance(any_of, list) else []
    if variants and all(_validate(value, s, path) for s in variants):
        return [f"{path}: не подходит ни под один вариант anyOf"]
    errors = _check_type(value, schema, path)
    if errors:
        return errors
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: значение {value!r} не из списка {schema['enum']}"]
    if "const" in schema and value != schema["const"]:
        return [f"{path}: ожидалось {schema['const']!r}"]
    if isinstance(value, dict):
        return _check_object(value, schema, path)
    if isinstance(value, list):
        return _check_array(value, schema, path)
    if isinstance(value, str):
        return _check_string(value, schema, path)
    return []


def schema_errors(value: Any, schema: Mapping[str, Any]) -> list[str]:
    """Нарушения схемы (не больше ``MAX_ERRORS``); пустой список, если ответ подходит."""
    return _validate(value, schema, "$")[:MAX_ERRORS]


__all__ = ["schema_errors"]
