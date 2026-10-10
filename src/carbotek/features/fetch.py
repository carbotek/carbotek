# pyright: strict, reportPrivateUsage=false
"""Bounded image downloads with an allowlist and pinned DNS results."""

import http.client
import ipaddress
import os
import socket
import time
from contextlib import suppress
from dataclasses import dataclass
from threading import Thread, Timer
from urllib.parse import urljoin, urlsplit

from carbotek.errors import ValidationError
from carbotek.ooxml.image_types import MIME_EXTENSIONS, image_mime

_IMAGE_TYPES = frozenset(MIME_EXTENSIONS)


@dataclass(frozen=True, slots=True)
class ImageSourceConfig:
    allowlist: tuple[str, ...] = ()
    file_root: str = ""
    max_bytes: int = 10485760
    fetch_timeout_ms: int = 5000
    max_download_concurrency: int = 4
    total_timeout_ms: int = 10000

    def __post_init__(self) -> None:
        entries = self.allowlist
        if not isinstance(entries, (tuple, list)) or any(not isinstance(entry, str) for entry in entries):  # pyright: ignore[reportUnnecessaryIsInstance] - runtime configuration boundary
            raise ValidationError("image allowlist must be a list or tuple of strings")
        object.__setattr__(
            self, "allowlist", tuple(entry.strip().lower().rstrip(".") for entry in entries if entry.strip())
        )
        if type(self.file_root) is not str:
            raise ValidationError("image file_root must be a string")
        for name in ("max_bytes", "fetch_timeout_ms", "max_download_concurrency", "total_timeout_ms"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValidationError(f"image {name} must be a positive integer")

    @classmethod
    def from_environment(cls) -> ImageSourceConfig:
        """Snapshot legacy image settings once at the public render boundary."""
        try:
            return cls(
                allowlist=tuple(os.environ.get("CARBOTEK__IMAGE_URL_ALLOWLIST", "").split(",")),
                file_root=os.environ.get("CARBOTEK__IMAGE_FILE_ROOT", ""),
                max_bytes=int(os.environ.get("CARBOTEK__IMAGE_MAX_BYTES", "10485760")),
                fetch_timeout_ms=int(os.environ.get("CARBOTEK__IMAGE_FETCH_TIMEOUT_MS", "5000")),
                max_download_concurrency=int(os.environ.get("CARBOTEK__IMAGE_MAX_DOWNLOAD_CONCURRENCY", "4")),
                total_timeout_ms=int(os.environ.get("CARBOTEK__IMAGE_TOTAL_TIMEOUT_MS", "10000")),
            )
        except ValueError as error:
            raise ValidationError("Image source environment limits must be integers") from error


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        plain = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(plain, server_hostname=self.host)  # type: ignore[attr-defined]
        except BaseException:
            plain.close()
            raise


def _allowed_address(host: str, allowlist: tuple[str, ...]) -> bool:
    hostname = host.rstrip(".").lower()
    return any(hostname == entry or hostname.endswith(f".{entry}") for entry in allowlist)


def _public_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    if not address.is_global or address.is_multicast:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_global
        if address in ipaddress.IPv6Network("64:ff9b::/96") or address.packed[:12] == bytes(12):
            return ipaddress.IPv4Address(address.packed[-4:]).is_global
    return True


def _resolve_public(host: str, port: int) -> str | None:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        addresses = {str(answer[4][0]) for answer in answers}
        if not addresses or any(not _public_address(address) for address in addresses):
            return None
        return min(addresses)
    except OSError, ValueError:
        return None


def _resolve_within(host: str, port: int, timeout: float) -> str | None:
    """``_resolve_public`` bounded by ``timeout``: a blocked resolver must not outlive the deadline."""
    result: list[str | None] = [None]

    def resolve() -> None:
        result[0] = _resolve_public(host, port)

    # getaddrinfo cannot be interrupted; after a timeout the daemon thread finishes on its own.
    worker = Thread(target=resolve, name="carbotek-dns", daemon=True)
    worker.start()
    worker.join(max(timeout, 0.0))
    return None if worker.is_alive() else result[0]


def _expire_connection(connection: http.client.HTTPConnection, active_socket: list[socket.socket | None]) -> None:
    transport = active_socket[0] or connection.sock
    if transport is not None:
        with suppress(OSError):
            transport.shutdown(socket.SHUT_RDWR)


def fetch_image(  # noqa: C901 - each guard is a distinct network boundary
    url: str, *, overall_deadline: float | None = None, config: ImageSourceConfig | None = None
) -> tuple[bytes, str] | None:
    """Download one supported image, refusing private addresses and unlisted hosts."""
    entries = config.allowlist if config is not None else os.environ.get("CARBOTEK__IMAGE_URL_ALLOWLIST", "").split(",")
    allowlist = tuple(entry.strip().lower().rstrip(".") for entry in entries if entry.strip())
    if not allowlist:
        return None
    try:
        max_bytes = (
            config.max_bytes if config is not None else int(os.environ.get("CARBOTEK__IMAGE_MAX_BYTES", "10485760"))
        )
        timeout = (
            config.fetch_timeout_ms
            if config is not None
            else int(os.environ.get("CARBOTEK__IMAGE_FETCH_TIMEOUT_MS", "5000"))
        ) / 1000
        if max_bytes <= 0 or timeout <= 0:
            return None
    except ValueError:
        return None
    deadline = (
        min(time.monotonic() + timeout, overall_deadline)
        if overall_deadline is not None
        else time.monotonic() + timeout
    )
    current = url
    for hop in range(4):
        if time.monotonic() >= deadline:
            return None
        try:
            parsed = urlsplit(current)
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None
        host = parsed.hostname
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        if not _allowed_address(host, allowlist):
            return None
        address = _resolve_within(host, port, deadline - time.monotonic())
        if address is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        connection = (
            _PinnedHTTPSConnection(host, port, address, remaining)
            if parsed.scheme == "https"
            else _PinnedHTTPConnection(host, port, address, remaining)
        )
        active_socket: list[socket.socket | None] = [None]

        watchdog = Timer(remaining, _expire_connection, args=(connection, active_socket))
        watchdog.daemon = True
        watchdog.start()
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += f"?{parsed.query}"
            connection.request("GET", target, headers={"Accept": "image/*"})
            active_socket[0] = connection.sock
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if hop == 3 or not location:
                    return None
                following = urljoin(current, location)
                if parsed.scheme == "https" and urlsplit(following).scheme != "https":
                    return None
                current = following
                continue
            if response.status != 200:
                return None
            mime = image_mime(response.getheader("Content-Type", ""))
            if mime not in _IMAGE_TYPES:
                return None
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                return None
            chunks: list[bytes] = []
            size = 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if response.isclosed() or response.length == 0:
                    return (b"".join(chunks), mime) if size and response.length in {None, 0} else None
                if active_socket[0] is not None:
                    active_socket[0].settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - size))
                if not chunk:
                    return (
                        (b"".join(chunks), mime)
                        if size and time.monotonic() < deadline and response.length in {None, 0}
                        else None
                    )
                size += len(chunk)
                if size > max_bytes:
                    return None
                chunks.append(chunk)
        except OSError, ValueError, http.client.HTTPException:
            return None
        finally:
            watchdog.cancel()
            connection.close()
    return None
