"""State restoration must retain independent phase changes and isolate contexts."""

from contextvars import Context, copy_context

import pytest

from carbotek.render_state import StateSlot


def test_reset_preserves_other_phase_changes() -> None:
    mode = StateSlot("test_mode", default=False)
    deferred = StateSlot("test_deferred", default=False)

    def render() -> None:
        token = mode.set(True)
        deferred.set(True)
        mode.reset(token)
        assert mode.get() is False
        assert deferred.get() is True
        with pytest.raises(ValueError, match="already used"):
            mode.reset(token)

    Context().run(render)
    assert deferred.get() is False


def test_nested_and_copied_contexts_are_isolated() -> None:
    slot = StateSlot("test_nested", default="default")

    def outer() -> None:
        first = slot.set("outer")
        second = slot.set("inner")
        slot.reset(second)
        assert slot.get() == "outer"
        copy_context().run(slot.set, "copy")
        assert slot.get() == "outer"
        assert Context().run(slot.get) == "default"
        slot.reset(first)
        assert slot.get() == "default"

    Context().run(outer)
