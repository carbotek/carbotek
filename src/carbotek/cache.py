# pyright: strict
"""Explicit process-cache reset for cold benchmark runs."""

import sys


def clear_module_caches() -> None:
    """Clear every loaded Carbotek functools cache, including the shared formatter registry."""
    cleared: set[int] = set()
    for name, module in tuple(sys.modules.items()):
        if module is None or not name.startswith("carbotek."):  # pyright: ignore[reportUnnecessaryComparison]
            continue
        for value in tuple(vars(module).values()):
            clear = getattr(value, "cache_clear", None)
            if callable(clear) and id(value) not in cleared:
                clear()
                cleared.add(id(value))
