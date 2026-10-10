"""Remote image response handling."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from carbotek.features import fetch


def test_http10_response_with_closed_connection(monkeypatch) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            payload = b"image"
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(fetch, "_resolve_public", lambda _host, _port: "127.0.0.1")
    try:
        config = fetch.ImageSourceConfig(allowlist=("localhost",))
        assert fetch.fetch_image(f"http://localhost:{server.server_port}/image.png", config=config) == (
            b"image",
            "image/png",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_blocked_dns_does_not_outlive_the_deadline(monkeypatch) -> None:
    import threading
    import time

    release = threading.Event()

    def slow_resolver(_host: str, _port: int) -> str:
        release.wait(5)
        return "127.0.0.1"

    monkeypatch.setattr(fetch, "_resolve_public", slow_resolver)
    config = fetch.ImageSourceConfig(allowlist=("img.example.com",), fetch_timeout_ms=200)
    started = time.monotonic()
    try:
        assert fetch.fetch_image("https://img.example.com/a.png", config=config) is None
        assert time.monotonic() - started < 1.5
    finally:
        release.set()
