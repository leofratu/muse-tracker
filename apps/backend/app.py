from __future__ import annotations

import argparse
import json
import mimetypes
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from apps.backend.muse_lsl_bridge import MuseLSLBridge


ROOT = Path(__file__).resolve().parents[2]
FRONTEND_ROOT = ROOT / "apps" / "frontend"


class MuseDashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server_address: tuple[str, int], bridge: MuseLSLBridge) -> None:
        super().__init__(server_address, MuseDashboardRequestHandler)
        self.bridge = bridge
        self.frontend_root = FRONTEND_ROOT


class MuseDashboardRequestHandler(BaseHTTPRequestHandler):
    server: MuseDashboardServer

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json({"ok": True, "service": "muse-dashboard"})
            return
        if self.path == "/api/status":
            self._send_json(self.server.bridge.snapshot())
            return
        if self.path == "/api/stream":
            self._stream_events()
            return
        self._serve_static()

    def _stream_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(self.server.bridge.snapshot()).encode("utf-8")
                self.wfile.write(b"event: snapshot\n")
                self.wfile.write(b"data: ")
                self.wfile.write(payload)
                self.wfile.write(b"\n\n")
                self.wfile.flush()
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_static(self) -> None:
        requested = self.path.split("?", 1)[0]
        relative = "index.html" if requested in {"/", ""} else requested.lstrip("/")
        candidate = (self.server.frontend_root / relative).resolve()
        if not str(candidate).startswith(str(self.server.frontend_root.resolve())) or not candidate.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        payload = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, payload: dict) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return



def build_server(host: str = "127.0.0.1", port: int = 8000, profile_key: str = "muse-2") -> MuseDashboardServer:
    bridge = MuseLSLBridge(profile_key=profile_key)
    bridge.start()
    return MuseDashboardServer((host, port), bridge)



def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a MuseLSL battery tracker and brain-wave viewer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--profile", choices=["muse-1", "muse-2"], default="muse-2")
    args = parser.parse_args()

    server = build_server(host=args.host, port=args.port, profile_key=args.profile)
    print(f"Muse dashboard running on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.bridge.stop()
        server.server_close()


if __name__ == "__main__":
    main()
