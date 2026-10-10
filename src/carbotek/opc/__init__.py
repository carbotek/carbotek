# pyright: strict, reportPrivateUsage=false
"""Safe ZIP/OPC persistence adapter."""

from .cache import CachedOpcTemplateRepository
from .repository import OpcTemplateRepository

__all__ = ["CachedOpcTemplateRepository", "OpcTemplateRepository"]
