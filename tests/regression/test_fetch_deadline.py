"""Real sockets exercise request deadlines, including HTTP/1.0 and trickle responses."""

import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest

from carbotek.features import fetch


@pytest.mark.parametrize("slow_headers", [False, True], ids=["body", "headers"])
def test_download_deadline_stops_trickle_response(monkeypatch: pytest.MonkeyPatch, slow_headers: bool) -> None:
    stopped = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                if slow_headers:
                    header = b"HTTP/1.0 200 OK\r\nContent-Type: image/png\r\nContent-Length: 100\r\n\r\n"
                    for byte in header:
                        self.connection.sendall(bytes([byte]))
                        if stopped.wait(0.02):
                            return
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", "100")
                    self.end_headers()
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    if stopped.wait(0.02):
                        return
            except OSError:
                pass

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(fetch, "_resolve_public", lambda _host, _port: "127.0.0.1")
    try:
        start = time.monotonic()
        result = fetch.fetch_image(
            f"http://localhost:{server.server_port}/image.png",
            config=fetch.ImageSourceConfig(allowlist=("localhost",), fetch_timeout_ms=100),
        )
        elapsed = time.monotonic() - start
        assert result is None
        assert elapsed < 0.8
    finally:
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b::a00:1", "::127.0.0.1", "::10.0.0.1"])
def test_ipv4_translation_cannot_bypass_private_address_guard(address: str) -> None:
    assert not fetch._public_address(address)
