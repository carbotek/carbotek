"""Backend switches for the native-extension tests."""

import pytest

from carbotek import _native_adapter


@pytest.fixture
def python_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render on the Python path only, as CARBOTEK_NATIVE=0 does for a whole process."""
    monkeypatch.setattr(_native_adapter, "_native", None)
