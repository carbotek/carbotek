# pyright: strict, reportPrivateUsage=false
"""Thread-safe bounded cache for validated immutable OPC template packages."""

from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from threading import RLock

from carbotek.package import TemplatePackage

from .repository import OpcTemplateRepository


@dataclass(slots=True)
class CachedOpcTemplateRepository:
    """Decorate OPC loading with an LRU keyed by content, never by a caller object."""

    delegate: OpcTemplateRepository
    capacity: int = 64
    max_bytes: int = 256 * 1024 * 1024
    _entries: OrderedDict[str, TemplatePackage] = field(default_factory=OrderedDict[str, TemplatePackage], init=False)
    _sizes: dict[str, int] = field(default_factory=dict[str, int], init=False)
    _bytes: int = field(default=0, init=False)
    _lock: RLock = field(default_factory=RLock, init=False)
    _hits: int = field(default=0, init=False)
    _misses: int = field(default=0, init=False)

    def load(self, payload: bytes) -> TemplatePackage:
        key = sha256(payload).hexdigest()
        with self._lock:
            package = self._entries.get(key)
            if package is not None:
                self._hits += 1
                self._entries.move_to_end(key)
                return package
            self._misses += 1
        package = self.delegate.load(payload)
        package_bytes = sum(len(part.content) for part in package.parts.values())
        with self._lock:
            if self.capacity > 0 and package_bytes <= self.max_bytes:
                if key not in self._entries:
                    self._bytes += package_bytes
                    self._sizes[key] = package_bytes
                self._entries[key] = package
                self._entries.move_to_end(key)
                while len(self._entries) > self.capacity or self._bytes > self.max_bytes:
                    oldest, _ = self._entries.popitem(last=False)
                    self._bytes -= self._sizes.pop(oldest)
        return package

    def dump(self, template: TemplatePackage) -> bytes:
        return self.delegate.dump(template)

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def bytes_used(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def metrics(self) -> dict[str, int | float]:
        """Snapshot cache behavior without leaking mutable internals."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total else 0.0,
            }
