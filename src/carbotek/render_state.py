# pyright: strict
"""A single context-local state for all rendering phases."""

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast


@dataclass(frozen=True, slots=True)
class RenderState:
    values: Mapping[str, Any]


_STATE: ContextVar[RenderState | None] = ContextVar("CARBOTEK_RENDER_STATE", default=None)


def _values() -> Mapping[str, Any]:
    state = _STATE.get()
    return state.values if state is not None else {}


@dataclass(slots=True)
class StateToken[T]:
    slot: StateSlot[T]
    previous: T
    present: bool
    used: bool = False


class StateSlot[T]:
    """Typed access to one field; resetting it preserves changes to other fields."""

    def __init__(self, name: str, *, default: T) -> None:
        self.name = name
        self.default = default

    def get(self) -> T:
        state = _STATE.get()
        return cast(T, state.values.get(self.name, self.default)) if state is not None else self.default

    def set(self, value: T) -> StateToken[T]:
        values = dict(_values())
        token = StateToken(self, self.get(), self.name in values)
        values[self.name] = value
        _STATE.set(RenderState(MappingProxyType(values)))
        return token

    def reset(self, token: StateToken[T]) -> None:
        if token.slot is not self or token.used:
            raise ValueError("Invalid or already used render-state token")
        values = dict(_values())
        if token.present:
            values[self.name] = token.previous
        else:
            values.pop(self.name, None)
        _STATE.set(RenderState(MappingProxyType(values)))
        token.used = True
