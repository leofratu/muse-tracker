"""Loopback-only HTTP/SSE server. Recordings are never exposed as static files."""
from __future__ import annotations

import argparse
import json
import mimetypes
import secrets
import sys
import tempfile
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parents[2]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from apps.backend.muse_lsl_bridge import MuseLSLBridge
from apps.backend.recording import csv_rows

FRONTEND_ROOT = ROOT / "apps" / "frontend"


class MuseDashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, bridge: MuseLSLBridge):
        if address[0] not in ("127.0.0.1", "localhost"):
            raise ValueError("The workstation only supports loopback binding.")
        super().__init__(address, MuseDashboardRequestHandler)
        self.bridge = bridge
        self.frontend_root = FRONTEND_ROOT
        self.control_token = secrets.token_urlsafe(32)
        self.stopping = threading.Event()
        self.stream_slots = threading.BoundedSemaphore(8)

    def server_close(self):
        self.stopping.set()
        self.bridge.stop()
        super().server_close()


class MuseDashboardRequestHandler(BaseHTTPRequestHandler):
    server: MuseDashboardServer

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        super().end_headers()

    def _valid_host(self) -> bool:
        port = self.server.server_address[1]
        hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if port == 80:
            hosts.update({"localhost", "127.0.0.1"})
        return self.headers.get("Host", "") in hosts

    def _json(self, value: dict, status=HTTPStatus.OK):
        payload = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _snapshot(self):
        return {**self.server.bridge.snapshot(), "controlToken": self.server.control_token}

    def do_GET(self):  # noqa: N802
        if not self._valid_host():
            self.send_error(403, "Invalid Host header")
            return
        route = urlsplit(self.path)
        path = unquote(route.path)
        if path == "/healthz":
            self._json({"ok": True, "service": "muse-workstation", "schemaVersion": 2})
        elif path == "/api/status":
            self._json(self._snapshot())
        elif path == "/api/stream":
            self._stream()
        elif path == "/api/sessions":
            self._json({"sessions": self.server.bridge.recorder.sessions()})
        elif path.startswith("/api/sessions/") and path.endswith("/export"):
            self._export(path, parse_qs(route.query).get("format", ["jsonl"])[0])
        else:
            self._static(path)

    def do_POST(self):  # noqa: N802
        if not self._valid_host():
            self.send_error(403, "Invalid Host header")
            return
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            self.send_error(403, "Cross-origin control is not allowed")
            return
        if not secrets.compare_digest(self.headers.get("X-Muse-Token", ""), self.server.control_token):
            self.send_error(403, "Missing control token")
            return
        if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            self.send_error(415, "Expected application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                self.send_error(413, "Request body must be between 1 and 8192 bytes")
                return
            self.connection.settimeout(5)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            path = urlsplit(self.path).path
            if path not in ("/api/record/start", "/api/record/stop", "/api/marker", "/api/reference"):
                self._json({"error": "Unknown control endpoint."}, 404)
                return
            if path == "/api/record/stop":
                result = self.server.bridge.recorder.stop()
            else:
                label = body.get("label")
                if not isinstance(label, str) or not 1 <= len(label.strip()) <= 120:
                    raise ValueError("Label must contain 1–120 characters.")
                label = label.strip()
                action = {"/api/record/start": self.server.bridge.start_recording,
                          "/api/marker": self.server.bridge.mark,
                          "/api/reference": self.server.bridge.capture_reference}[path]
                result = action(label)
            self._json(result)
        except (ValueError, UnicodeError) as exc:
            self._json({"error": str(exc)}, 400)
        except OSError as exc:
            self._json({"error": f"Local I/O failed: {exc}"}, 503)

    def _stream(self):
        if not self.server.stream_slots.acquire(blocking=False):
            self.send_error(429, "Too many live clients")
            return
        try:
            self.connection.settimeout(5)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            while not self.server.stopping.is_set():
                payload = json.dumps(self._snapshot(), allow_nan=False)
                self.wfile.write(f"event: snapshot\ndata: {payload}\n\n".encode())
                self.wfile.flush()
                self.server.stopping.wait(0.4)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.stream_slots.release()

    def _static(self, path):
        relative = "index.html" if path == "/" else path.lstrip("/")
        candidate = (self.server.frontend_root / relative).resolve()
        allowed = {"index.html", "app.js", "transport.js", "styles.css"}
        if relative not in allowed or not candidate.is_relative_to(self.server.frontend_root.resolve()) or not candidate.is_file():
            self.send_error(404, "File not found")
            return
        payload = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(candidate.name)[0] or "application/octet-stream") + "; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _export(self, path, file_format):
        prepared = None
        try:
            parts = path.split("/")
            if len(parts) != 5 or file_format not in ("jsonl", "csv"):
                raise ValueError("Invalid export request.")
            identifier = parts[3]
            candidate = self.server.bridge.recorder.path_for(identifier)
            state = self.server.bridge.recorder.status()
            if state["id"] == identifier and (state["active"] or state["finishing"]):
                self._json({"error": "Stop recording before exporting."}, 409)
                return
            if file_format == "csv":
                # Validate before sending headers. Large exports spill to a private temp file.
                prepared = tempfile.SpooledTemporaryFile(max_size=1024 * 1024)
                for row in csv_rows(candidate):
                    prepared.write(row)
                prepared.seek(0)
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8" if file_format == "csv" else "application/x-ndjson")
            self.send_header("Content-Disposition", f'attachment; filename="{identifier}.{file_format}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if file_format == "csv":
                while chunk := prepared.read(65536):
                    self.wfile.write(chunk)
            else:
                with candidate.open("rb") as handle:
                    while chunk := handle.read(65536):
                        self.wfile.write(chunk)
        except (json.JSONDecodeError, KeyError, TypeError):
            self._json({"error": "Recording is malformed. Export the original JSONL for recovery."}, 400)
        except (ValueError, FileNotFoundError):
            self.send_error(404, "Recording not found or invalid export")
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            if prepared is not None:
                prepared.close()

    def log_message(self, format, *args):
        pass


def build_server(host="127.0.0.1", port=8000, profile_key="auto", **kwargs):
    bridge = MuseLSLBridge(profile_key=profile_key, **kwargs)
    server = MuseDashboardServer((host, port), bridge)
    bridge.start()  # Bind first, so a failed bind cannot leak acquisition threads.
    return server


def main():
    parser = argparse.ArgumentParser(description="Local Muse EEG workstation")
    parser.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--profile", choices=["auto", "muse-1", "muse-2"], default="auto")
    parser.add_argument("--source-id", help="Select a specific LSL EEG source_id")
    parser.add_argument("--mains", type=int, choices=[0, 50, 60], default=50)
    parser.add_argument("--record-dir", type=Path, default=ROOT / "recordings")
    args = parser.parse_args()
    server = build_server(host=args.host, port=args.port, profile_key=args.profile,
                          source_id=args.source_id, mains=args.mains, record_dir=args.record_dir)
    print(f"Muse workstation: http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
