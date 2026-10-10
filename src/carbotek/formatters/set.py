# pyright: strict, reportPrivateUsage=false
"""Stateful ``:set`` formatters, isolated to an explicit marker context."""

from copy import deepcopy
from typing import Any

from carbotek.typing_helpers import is_dict, is_list

from .registry import FormatterMetadata, FormatterRegistry, MarkerContext


def register(registry: FormatterRegistry) -> None:
    metadata = FormatterMetadata(uses_marker_context=True, mutates_input=True)
    registry.register("set", set_value, metadata)
    registry.register("_setInit", set_init, metadata)
    registry.register("_setObj", set_object, metadata)
    registry.register("_setArr", set_array, metadata)
    registry.register("_setVal", set_child_value, metadata)
    registry.register("_setIfEM", set_if_empty, metadata)
    registry.register("_setValInArr", set_value_in_array, metadata)
    registry.register("_setObjInArr", set_object_in_array, metadata)


def set_value(value: Any, path: Any, *, marker_context: MarkerContext | None = None) -> str:
    context = _context(marker_context)
    target = str(path)
    if target.startswith("c."):
        context.complement[target.removeprefix("c.")] = deepcopy(value)
    else:
        context.data[target.removeprefix("d.")] = deepcopy(value)
    return ""


def set_init(value: Any, parent: Any, *, marker_context: MarkerContext | None = None) -> Any:
    _context(marker_context).parent = parent
    return value


def set_object(value: Any, name: Any, *, marker_context: MarkerContext | None = None) -> Any:
    context = _context(marker_context)
    if is_list(context.parent):
        raise ValueError(f'Cannot add an attribute "{name}" to an array with :set() formatter')
    if not is_dict(context.parent):
        context.parent = {}
    if not (is_dict(context.parent.get(str(name))) or is_list(context.parent.get(str(name)))):
        context.parent[str(name)] = {}
    context.parent = context.parent[str(name)]
    return value


def set_array(value: Any, name: Any, *, marker_context: MarkerContext | None = None) -> Any:
    context = _context(marker_context)
    if not is_dict(context.parent):
        context.parent = {}
    if context.parent.get(str(name), _MISSING) is _MISSING:
        context.parent[str(name)] = []
        context.set_database[id(context.parent[str(name)])] = {}
    context.parent = context.parent[str(name)]
    return value


def set_child_value(value: Any, name: Any, *, marker_context: MarkerContext | None = None) -> str:
    context = _context(marker_context)
    if context.parent is None:
        return ""
    if is_list(context.parent):
        raise ValueError(f'Cannot add an attribute "{name}" to an array with :set() formatter')
    context.parent[str(name)] = deepcopy(value)
    return ""


def set_if_empty(value: Any, *, marker_context: MarkerContext | None = None) -> str:
    del value, marker_context
    return ""


def set_object_in_array(
    value: Any, key: Any = None, key_value: Any = None, *, marker_context: MarkerContext | None = None
) -> Any:
    context = _context(marker_context)
    array, index = _array_index(context, key, key_value)
    if key is None:
        item: dict[str, Any] = {}
        array.append(item)
    else:
        item = index.setdefault(key_value, {str(key): key_value})
        if item not in array:
            array.append(item)
    context.parent = item
    return value


def set_value_in_array(
    value: Any, key: Any = None, key_value: Any = None, *, marker_context: MarkerContext | None = None
) -> str:
    context = _context(marker_context)
    array, index = _array_index(context, key, key_value)
    copied = deepcopy(value)
    if key is None:
        array.append(copied)
        context.parent = copied
        return ""
    item = index.setdefault(key_value, copied)
    if item not in array:
        if is_dict(item) and isinstance(key, str):
            item[key] = key_value
        array.append(item)
    if is_dict(item) and is_dict(copied):
        item.update(copied)
    context.parent = item
    return ""


_MISSING = object()


def _array_index(context: MarkerContext, key: Any, key_value: Any) -> tuple[list[Any], dict[Any, Any]]:
    if id(context.parent) not in context.set_database:
        raise ValueError(
            f'The array cannot be updated with the condition "{key}={key_value}" by the formatter ":set". '
            "Was this table created by :set() formatter?"
        )
    parent: list[Any] = context.parent
    return parent, context.set_database[id(parent)]


def _context(context: MarkerContext | None) -> MarkerContext:
    if context is None:
        raise ValueError("The :set formatter requires a render marker context")
    return context
