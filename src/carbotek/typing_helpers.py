# pyright: strict
"""Narrowing helpers for untyped JSON data (``Any``) under pyright strict.

``isinstance(value, Mapping)`` narrows ``Any`` to ``Mapping[Unknown, Unknown]``; these helpers narrow
to ``Mapping[str, Any]`` / ``list[Any]`` instead, in both branches (``TypeIs``).
"""

from collections.abc import Mapping
from typing import Any, TypeIs


def is_mapping(value: object) -> TypeIs[Mapping[str, Any]]:
    return type(value) is dict or isinstance(value, Mapping)  # dict first: the ABC check is slow


def is_list(value: object) -> TypeIs[list[Any]]:
    return type(value) is list or isinstance(value, list)


def is_dict(value: object) -> TypeIs[dict[str, Any]]:
    return isinstance(value, dict)


def is_sequence(value: object) -> TypeIs[list[Any] | tuple[Any, ...]]:
    return isinstance(value, list | tuple)
