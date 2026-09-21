"""Acquisition and analysis run independently; HTTP reads only cached snapshots."""
from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque
from pathlib import Path

from apps.backend.recording import Recorder, utc_now
from apps.backend.signal_processing import CHANNELS, analyze, empty_analysis, settings

try:
    from pylsl import StreamInlet, local_clock, resolve_byprop
    LSL_ERROR = None
except Exception as exc:  # liblsl may be missing even when the Python package is installed.
    StreamInlet = resolve_byprop = None
    local_clock = time.monotonic
    LSL_ERROR = str(exc)

MAX_AGES = {"eeg": 2.0, "acc": 3.0, "gyro": 3.0, "telemetry": 30.0}
PROFILES = {"auto": "Unknown Muse", "muse-1": "Muse 1", "muse-2": "Muse 2"}


def stream_value(stream, name: str, default=""):
    try:
        return getattr(stream, name)() or default
    except Exception:
        return default


def device_key(stream) -> str:
    try:
        explicit = stream.desc().child_value("device_id")
        if explicit:
            return explicit.lower()
    except Exception:
        pass
    identity = str(stream_value(stream, "source_id")).lower()
    for prefix in ("musetelemetry", "musegyro", "museacc", "muse"):
        if identity.startswith(prefix):
            return identity[len(prefix):]
    return identity


def channel_layout(stream) -> tuple[list[int], list[float]]:
    """Honor channel labels and units; absent metadata is a documented Muse fallback."""
    labels, units = [], []
    try:
        child = stream.desc().child("channels").child("channel")
        for _ in range(int(stream_value(stream, "channel_count", 4))):
            labels.append(child.child_value("label"))
            units.append(child.child_value("unit"))
            child = child.next_sibling()
    except Exception:
        labels, units = [], []
    if any(labels):
        if not all(c in labels for c in CHANNELS):
            raise ValueError("EEG metadata does not contain all four Muse channels.")
        indices = [labels.index(c) for c in CHANNELS]
    else:
        indices, units = list(range(4)), [""] * 4
    scales = {"": 1.0, "uv": 1.0, "µv": 1.0, "μv": 1.0, "microvolts": 1.0,
              "mv": 1000.0, "millivolts": 1000.0, "v": 1e6, "volts": 1e6}
    try:
        return indices, [scales[units[i].lower()] for i in indices]
    except (IndexError, KeyError) as exc:
        raise ValueError("Unsupported EEG units.") from exc


class MuseLSLBridge:
    def __init__(self, profile_key: str = "auto", sample_rate_hz: float = 256,
                 max_seconds: int = 8, record_dir: Path | None = None,
                 source_id: str | None = None, mains: int = 50) -> None:
        if profile_key not in PROFILES or mains not in (0, 50, 60):
            raise ValueError("Invalid profile or mains frequency.")
        self.profile_key, self.mains = profile_key, mains
        self.requested_source = source_id
        self.max_seconds = max(4, max_seconds)
        self._lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._inlets = {}
        self._opened = {}
        self._last_resolve = -100.0
        self._indices, self._scales = list(range(4)), [1.0] * 4
        self._selected_key = ""
        self._source = {"name": "Waiting for MuseLSL", "id": "", "sampleRateHz": float(sample_rate_hz),
                        "profile": profile_key, "label": PROFILES[profile_key]}
        self._epoch = 0
        self._sequence = 0
        self._invalid = 0
        self._buffers = {kind: deque(maxlen=int(self.max_seconds * (sample_rate_hz if kind == "eeg" else 52)))
                         for kind in MAX_AGES}
        self._received = {kind: 0.0 for kind in MAX_AGES}
        self._last_error = "Waiting for a live MuseLSL EEG stream."
        self._reference = None
        self._last_analysis_key = None
        self._analysis = empty_analysis()
        self._history = deque(maxlen=180)
        self._last_history_key = None
        self.recorder = Recorder(record_dir or Path(__file__).resolve().parents[2] / "recordings")
        self._cache = {}
        self.refresh()

    def start(self) -> None:
        if any(t.is_alive() for t in self._threads):
            return
        self._stop.clear()
        self._threads = [threading.Thread(target=self._acquire, name="muse-acquisition", daemon=True),
                         threading.Thread(target=self._process, name="muse-analysis", daemon=True)]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=3)
        self._close_inlets()
        self.recorder.stop()

    def configure_source(self, name: str, identifier: str, rate: float) -> None:
        if not math.isfinite(rate) or not 8 <= rate <= 2048:
            raise ValueError("Invalid EEG sample rate.")
        detected = "auto"
        lowered = f"{name} {identifier}".lower()
        if any(s in lowered for s in ("muse 1", "muse-1", "classic")):
            detected = "muse-1"
        elif any(s in lowered for s in ("muse 2", "muse-2")):
            detected = "muse-2"
        profile = self.profile_key if self.profile_key != "auto" else detected
        with self._lock:
            self._source = {"name": name, "id": identifier, "sampleRateHz": rate,
                            "profile": profile, "label": PROFILES[profile]}
            self._epoch += 1
            self._sequence += 1
            for kind in self._buffers:
                self._buffers[kind] = deque(maxlen=int(self.max_seconds * (rate if kind == "eeg" else 52)))
                self._received[kind] = 0.0
            self._reference = None
            source, epoch = dict(self._source), self._epoch
        self.recorder.enqueue({"type": "stream", "source": source, "epoch": epoch, "receivedAt": utc_now()})

    def ingest(self, kind: str, pairs) -> None:
        if kind not in self._buffers:
            raise ValueError("Unknown stream type.")
        count = 4 if kind in ("eeg", "telemetry") else 3
        accepted = []
        invalid = 0
        with self._lock:
            target = self._buffers[kind]
            for timestamp, values in pairs:
                try:
                    timestamp = float(timestamp)
                    values = [float(v) for v in values[:count]]
                    if len(values) != count or not all(math.isfinite(v) for v in [timestamp, *values]):
                        raise ValueError
                    if kind == "eeg" and target and timestamp <= target[-1]["timestamp"]:
                        raise ValueError
                except (TypeError, ValueError, OverflowError):
                    invalid += 1
                    continue
                item = {"timestamp": timestamp, "values": values}
                target.append(item)
                accepted.append(item)
            self._invalid += invalid
            if accepted:
                self._received[kind] = time.monotonic()
                if kind == "eeg":
                    self._sequence += 1
                    self._last_error = None
            epoch, source = self._epoch, dict(self._source)
        # No disk I/O and no waiting on a full queue on the acquisition thread.
        if accepted:
            self.recorder.enqueue({"type": kind, "samples": accepted, "receivedAt": utc_now(), "epoch": epoch,
                                   **({"source": source} if kind == "eeg" else {})})
        if invalid:
            self.recorder.enqueue({"type": "rejected", "stream": kind, "count": invalid, "receivedAt": utc_now()})

    def snapshot(self) -> dict:
        with self._cache_lock:
            result = copy.deepcopy(self._cache)
        result["recording"] = self.recorder.status()
        return result

    def refresh(self) -> None:
        with self._lock:
            buffers = {kind: list(values) for kind, values in self._buffers.items()}
            received, source = dict(self._received), dict(self._source)
            sequence, epoch, error, invalid = self._sequence, self._epoch, self._last_error, self._invalid
            reference = copy.deepcopy(self._reference)
        now = time.monotonic()
        streams = {kind: {"live": bool(received[kind] and now - received[kind] <= MAX_AGES[kind]),
                          "ageSeconds": now - received[kind] if received[kind] else None}
                   for kind in MAX_AGES}
        live = streams["eeg"]["live"]
        acc = buffers["acc"][-1]["values"] if streams["acc"]["live"] and buffers["acc"] else None
        gyro = buffers["gyro"][-1]["values"] if streams["gyro"]["live"] and buffers["gyro"] else None
        gyro_magnitude = math.sqrt(sum(v * v for v in gyro)) if gyro else None
        accel_magnitude = math.sqrt(sum(v * v for v in acc)) if acc else None
        motion = {"accelerometer": acc, "gyroscope": gyro, "gyroDps": gyro_magnitude,
                  "accelG": accel_magnitude, "available": bool(acc or gyro),
                  "moving": bool((gyro_magnitude is not None and gyro_magnitude > 15)
                                 or (accel_magnitude is not None and abs(accel_magnitude - 1) > 0.18))}
        status = "live" if live else "stale" if buffers["eeg"] else "waiting"
        key = (epoch, sequence, live, motion["moving"])
        if key != self._last_analysis_key:
            self._analysis = analyze(buffers["eeg"], source["sampleRateHz"], self.mains, motion) if live else empty_analysis(
                status, "EEG is stale. Derived values are withheld." if buffers["eeg"] else "Waiting for live EEG.")
            self._last_analysis_key = key
        analysis = copy.deepcopy(self._analysis)
        if self._last_history_key and self._last_history_key[0] != epoch:
            self._history.clear()
        history_key = (epoch, sequence)
        if analysis["available"] and history_key != self._last_history_key:
            self._history.append({"timestamp": buffers["eeg"][-1]["timestamp"],
                                  "relative": analysis["relative"], "sensors": analysis["sourceSensors"]})
            self._last_history_key = history_key
        comparison = {"available": False, "reason": "Capture a labelled reference window first."}
        if reference:
            compatible = (reference["epoch"] == epoch and reference["sensors"] == analysis["sourceSensors"]
                          and analysis["available"])
            comparison = {"available": bool(compatible), "label": reference["label"],
                          "capturedAt": reference["capturedAt"],
                          "reason": "Descriptive change only; one four-second reference is not a statistical baseline."
                          if compatible else "Reference and live data must use the same stream, rate and sensors."}
            if compatible:
                comparison["relativeShift"] = {name: analysis["relative"][name] - value
                                               for name, value in reference["relative"].items()}
        telemetry = buffers["telemetry"][-1]["values"] if streams["telemetry"]["live"] and buffers["telemetry"] else None
        battery = telemetry[0] if telemetry and 0 <= telemetry[0] <= 100 else None
        snapshot = {"schemaVersion": 2, "generatedAt": utc_now(), "sequence": sequence, "epoch": epoch,
                    "device": source, "connection": {"connected": live, "status": status, "streams": streams,
                    "lastError": error, "invalidSamples": invalid}, "analysis": analysis,
                    "settings": settings(source["sampleRateHz"], self.mains),
                    "eeg": {"channels": list(CHANNELS), "samples": buffers["eeg"][-int(source["sampleRateHz"] * 4):]},
                    "motion": motion, "telemetry": {"available": telemetry is not None, "batteryPercent": battery,
                    "fuelGaugeRaw": telemetry[1] if telemetry else None, "adcRaw": telemetry[2] if telemetry else None,
                    "temperatureRaw": telemetry[3] if telemetry else None},
                    "reference": comparison, "history": list(self._history)}
        with self._cache_lock:
            self._cache = snapshot

    def capture_reference(self, label: str) -> dict:
        current = self.snapshot()
        if not current["connection"]["connected"] or not current["analysis"]["available"]:
            raise ValueError("A live, admitted four-second EEG window is required.")
        reference = {"label": label, "capturedAt": utc_now(), "epoch": current["epoch"],
                     "sensors": current["analysis"]["sourceSensors"], "relative": current["analysis"]["relative"],
                     "absolute": current["analysis"]["absolute"], "settings": current["settings"]}
        with self._lock:
            if current["epoch"] != self._epoch or time.monotonic() - self._received["eeg"] > MAX_AGES["eeg"]:
                raise ValueError("Stream changed; capture the reference again.")
            self._reference = reference
        self.recorder.enqueue({"type": "reference", **reference})
        return reference

    def start_recording(self, label: str) -> dict:
        with self._lock:
            if not self._received["eeg"] or time.monotonic() - self._received["eeg"] > MAX_AGES["eeg"]:
                raise ValueError("Connect a live EEG stream before recording.")
            source, epoch, reference = dict(self._source), self._epoch, copy.deepcopy(self._reference)
        return self.recorder.start(label, {"source": source, "epoch": epoch, "channels": list(CHANNELS),
                    "settings": settings(source["sampleRateHz"], self.mains), "reference": reference,
                    "timestampClock": "LSL clock, not Unix time", "lslClockAtStart": local_clock(),
                    "utcAtStart": utc_now(), "unit": "microvolts"})

    def mark(self, label: str) -> dict:
        if not self.recorder.status()["active"]:
            raise ValueError("Start a recording before adding a marker.")
        with self._lock:
            latest = self._buffers["eeg"][-1]["timestamp"] if self._buffers["eeg"] else None
        marker = {"type": "marker", "label": label, "receivedAt": utc_now(),
                  "lslTimestamp": local_clock(), "latestEegTimestamp": latest,
                  "timingNote": "Server receipt time, not laboratory stimulus-onset timing."}
        self.recorder.enqueue(marker)
        return marker

    def _process(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:
                with self._cache_lock:
                    self._cache["analysis"] = empty_analysis("error", f"Analysis failed: {exc}")
            self._stop.wait(0.4)

    def _close_inlets(self) -> None:
        for inlet in self._inlets.values():
            try:
                inlet.close_stream()
            except Exception:
                pass
        self._inlets.clear()
        self._opened.clear()

    def _open(self, kind, stream) -> None:
        inlet = StreamInlet(stream, max_buflen=self.max_seconds)
        self._inlets[kind] = inlet
        self._opened[kind] = time.monotonic()
        try:
            inlet.open_stream(timeout=0.2)
        except Exception:
            pass

    def _discover(self) -> None:
        if "eeg" not in self._inlets:
            candidates = resolve_byprop("type", "EEG", timeout=0.2)
            candidates = [s for s in candidates if int(stream_value(s, "channel_count", 0)) >= 4
                          and ((stream_value(s, "source_id") == self.requested_source) if self.requested_source
                               else "muse" in str(stream_value(s, "name")).lower())]
            if not candidates:
                return
            selected = sorted(candidates, key=lambda s: str(stream_value(s, "source_id")))[0]
            self._indices, self._scales = channel_layout(selected)
            self._selected_key = device_key(selected)
            self.configure_source(str(stream_value(selected, "name")), str(stream_value(selected, "source_id")),
                                  float(stream_value(selected, "nominal_srate", 0)))
            self._open("eeg", selected)
        # Revisit missing auxiliary streams, including streams published after EEG started.
        for kind, stream_type in (("telemetry", "Telemetry"), ("acc", "ACC"), ("gyro", "GYRO")):
            if kind not in self._inlets and self._selected_key:
                matches = [s for s in resolve_byprop("type", stream_type, timeout=0.1)
                           if device_key(s) == self._selected_key]
                if matches:
                    self._open(kind, matches[0])

    def _try_pull_lsl(self) -> bool:
        now = time.monotonic()
        if now - self._last_resolve >= 3:
            self._last_resolve = now
            self._discover()
        for kind, inlet in list(self._inlets.items()):
            chunk, timestamps = inlet.pull_chunk(timeout=0, max_samples=2048)
            if len(chunk) == 0:
                sample, stamp = inlet.pull_sample(timeout=0)
                chunk, timestamps = ([sample], [stamp]) if sample is not None and stamp is not None else ([], [])
            if len(chunk):
                if kind == "eeg":
                    mapped = []
                    for row in chunk:
                        try:
                            mapped.append([float(row[i]) * scale for i, scale in zip(self._indices, self._scales)])
                        except (IndexError, TypeError, ValueError, OverflowError):
                            mapped.append([])  # Count malformed rows rather than resetting all inlets.
                    chunk = mapped
                self.ingest(kind, zip(timestamps, chunk))
            with self._lock:
                last_received = self._received[kind]
            age = now - (last_received or self._opened[kind])
            if age > (6 if kind == "eeg" else MAX_AGES[kind] * 2):
                if kind == "eeg":
                    self._close_inlets()
                    with self._lock:
                        self._last_error = "EEG stream stopped; reconnecting."
                    break
                try:
                    inlet.close_stream()
                finally:
                    self._inlets.pop(kind, None)
                    self._opened.pop(kind, None)
        with self._lock:
            return bool(self._received["eeg"] and now - self._received["eeg"] <= 2)

    def _acquire(self) -> None:
        if StreamInlet is None:
            with self._lock:
                self._last_error = f"Install pylsl and liblsl to connect a headset. {LSL_ERROR or ''}"
            return
        try:
            while not self._stop.is_set():
                try:
                    self._try_pull_lsl()
                except Exception as exc:
                    self._close_inlets()
                    with self._lock:
                        self._last_error = f"LSL connection error: {exc}"
                self._stop.wait(0.02)
        finally:
            self._close_inlets()
