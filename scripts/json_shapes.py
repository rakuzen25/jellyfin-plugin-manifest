"""Runtime JSON container checks with explicit dynamic element types."""

from typing import Any, TypeGuard


def is_json_object(value: object) -> TypeGuard[dict[Any, Any]]:
    return isinstance(value, dict)


def is_json_array(value: object) -> TypeGuard[list[Any]]:
    return isinstance(value, list)
