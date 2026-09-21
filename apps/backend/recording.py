"""Bounded, asynchronous, local-only raw recording with explicit failure reporting."""
from __future__ import annotations

import csv
import io
import json
import os
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    def __init__(self, root: Path, queue_size: int = 2048) -> None:
        self.root = Path(root)
        self.queue_size = queue_size
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = {"active": False, "finishing": False, "id": None, "label": "",
                       "samplesWritten": 0, "droppedSamples": 0, "error": None}
        self._started = 0.0
        self._duration = 0.0

    def start(self, label: str, metadata: dict) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("A recording is already active or finishing.")
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            identifier = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
            path = self.root / f"{identifier}.jsonl"
            handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8")
            try:
                handle.write(json.dumps({"type": "header", "schemaVersion": 2, "id": identifier,
                                         "label": label, "startedAt": utc_now(), "metadata": metadata},
                                        allow_nan=False) + "\n")
                handle.flush()
            except Exception:
                handle.close()
                raise
            self._queue = queue.Queue(maxsize=self.queue_size)
            self._stop.clear()
            self._started = time.monotonic()
            self._duration = 0.0
            self._state = {"active": True, "finishing": False, "id": identifier, "label": label,
                           "samplesWritten": 0, "droppedSamples": 0, "error": None}
            self._thread = threading.Thread(target=self._write, args=(handle,), daemon=True,
                                            name="muse-recording")
            self._thread.start()
        return self.status()

    def enqueue(self, event: dict) -> None:
        with self._lock:
            if not self._state["active"]:
                return
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._state["droppedSamples"] += len(event.get("samples", []))
                self._state["error"] = "Recording queue overflow. File is incomplete; recording stopped."
                self._state["active"] = False
                self._state["finishing"] = True
                self._stop.set()

    def _write(self, handle) -> None:
        last_flush = time.monotonic()
        footer_written = False
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    event = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                handle.write(json.dumps(event, allow_nan=False) + "\n")
                with self._lock:
                    if event.get("type") == "eeg":
                        self._state["samplesWritten"] += len(event.get("samples", []))
                if time.monotonic() - last_flush >= 1:
                    handle.flush()
                    last_flush = time.monotonic()
            with self._lock:
                footer = {"type": "footer", "endedAt": utc_now(), "complete": not self._state["error"],
                          "samplesWritten": self._state["samplesWritten"],
                          "droppedSamples": self._state["droppedSamples"], "error": self._state["error"]}
            handle.write(json.dumps(footer, allow_nan=False) + "\n")
            footer_written = True
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, ValueError, TypeError) as exc:
            with self._lock:
                self._state["error"] = f"Recording write failed: {exc}"
            if footer_written:
                try:
                    handle.write(json.dumps({"type": "footer", "complete": False, "error": str(exc)}) + "\n")
                    handle.flush()
                except OSError:
                    pass
        finally:
            try:
                handle.close()
            except OSError:
                pass
            with self._lock:
                self._state["active"] = False
                self._state["finishing"] = False
                self._duration = time.monotonic() - self._started

    def stop(self) -> dict:
        with self._lock:
            self._state["active"] = False
            self._state["finishing"] = bool(self._thread and self._thread.is_alive())
            self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        return self.status()

    def status(self) -> dict:
        with self._lock:
            running = self._state["active"] or self._state["finishing"]
            return {**self._state, "durationSeconds": time.monotonic() - self._started if running else self._duration,
                    "pendingChunks": self._queue.qsize()}

    def path_for(self, identifier: str) -> Path:
        if not ID_PATTERN.fullmatch(identifier):
            raise ValueError("Invalid recording identifier.")
        candidate = self.root / f"{identifier}.jsonl"
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root.resolve()) or not candidate.is_file():
            raise FileNotFoundError("Recording not found.")
        return candidate

    def sessions(self) -> list[dict]:
        items = []
        for path in sorted(self.root.glob("*.jsonl"), reverse=True)[:100]:
            try:
                self.path_for(path.stem)
                with path.open("rb") as handle:
                    header = json.loads(handle.readline(32768))
                    handle.seek(max(0, path.stat().st_size - 8192))
                    tail = handle.read().splitlines()
                try:
                    footer = json.loads(tail[-1]) if tail else {}
                except json.JSONDecodeError:
                    footer = {}
                complete = footer.get("type") == "footer" and footer.get("complete") is True
                items.append({"id": path.stem, "label": header.get("label", path.stem),
                              "startedAt": header.get("startedAt"), "complete": complete,
                              "bytes": path.stat().st_size, "samplesWritten": footer.get("samplesWritten"),
                              "active": self.status().get("id") == path.stem and self.status()["active"]})
            except (OSError, ValueError, KeyError):
                continue
        return items


def csv_rows(path: Path):
    """Yield raw EEG rows without loading a session into memory. Markers remain in JSONL."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["lsl_timestamp", "TP9_uV", "AF7_uV", "AF8_uV", "TP10_uV", "received_utc", "epoch"])
    yield buffer.getvalue().encode()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            event = json.loads(line)
            if event.get("type") != "eeg":
                continue
            for sample in event["samples"]:
                buffer.seek(0)
                buffer.truncate(0)
                writer.writerow([sample["timestamp"], *sample["values"], event["receivedAt"], event["epoch"]])
                yield buffer.getvalue().encode()
