from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import fmean
from typing import Any, Deque, Iterable

try:
    from pylsl import StreamInlet, resolve_byprop
except Exception:  # pragma: no cover - optional dependency
    StreamInlet = None
    resolve_byprop = None


CHANNELS = ("TP9", "AF7", "AF8", "TP10")

DEVICE_PROFILES = {
    "muse-1": {
        "label": "Muse 1",
        "headline": "Classic four-channel Muse headset",
        "notes": [
            "Streams core EEG channels TP9, AF7, AF8, and TP10.",
            "Battery telemetry depends on your MuseLSL source exposing a Telemetry stream.",
        ],
    },
    "muse-2": {
        "label": "Muse 2",
        "headline": "Muse 2 with EEG, motion, and richer telemetry support",
        "notes": [
            "Uses the same four EEG channels as Muse 1 for the live viewer.",
            "Muse 2 can expose more sensors, but this dashboard stays focused on EEG and battery state.",
        ],
    },
}

BAND_DEFS = (
    ("delta", 1, 4),
    ("theta", 4, 8),
    ("alpha", 8, 13),
    ("beta", 13, 30),
    ("gamma", 30, 45),
)


@dataclass
class BatteryState:
    percent: float = 92.0
    trend: str = "steady"
    updated_at: str = ""
    source: str = "demo"

    def to_dict(self) -> dict[str, Any]:
        percent = max(0.0, min(100.0, self.percent))
        if percent <= 15:
            level = "critical"
        elif percent <= 30:
            level = "low"
        elif percent <= 65:
            level = "moderate"
        else:
            level = "healthy"
        return {
            "percent": round(percent, 1),
            "level": level,
            "trend": self.trend,
            "updatedAt": self.updated_at or utc_now(),
            "source": self.source,
            "isLow": percent <= 30,
        }


class MuseSampleStore:
    def __init__(self, max_seconds: int = 8, sample_rate_hz: int = 64) -> None:
        self.max_seconds = max_seconds
        self.sample_rate_hz = sample_rate_hz
        self._max_samples = max_seconds * sample_rate_hz
        self._samples: Deque[dict[str, Any]] = deque(maxlen=self._max_samples)
        self._acc_samples: Deque[dict[str, Any]] = deque(maxlen=max_seconds * 52)
        self._gyro_samples: Deque[dict[str, Any]] = deque(maxlen=max_seconds * 52)
        self._battery = BatteryState(updated_at=utc_now())
        self._stream_name = "Demo Muse Feed"
        self._stream_source_id = ""
        self._stream_mode = "demo"
        self._connected = False
        self._telemetry_available = False
        self._motion_available = False
        self._last_error = "Waiting for a MuseLSL EEG stream; demo mode is active."
        self._battery_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._fit_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._motion_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._confidence_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._version_info = build_version_info("muse-2", "Demo Muse Feed", "", sample_rate_hz, False)
        self._lock = threading.Lock()

    def set_connection(
        self,
        *,
        connected: bool,
        stream_mode: str,
        stream_name: str,
        stream_source_id: str,
        telemetry_available: bool,
        motion_available: bool,
        last_error: str | None,
        profile_key: str,
        sample_rate_hz: int | None = None,
    ) -> None:
        with self._lock:
            self._connected = connected
            self._stream_mode = stream_mode
            self._stream_name = stream_name
            self._stream_source_id = stream_source_id
            self._telemetry_available = telemetry_available
            self._motion_available = motion_available
            self._last_error = last_error
            if sample_rate_hz and sample_rate_hz > 0 and sample_rate_hz != self.sample_rate_hz:
                self.sample_rate_hz = sample_rate_hz
                self._max_samples = self.max_seconds * sample_rate_hz
                self._samples = deque(list(self._samples)[-self._max_samples :], maxlen=self._max_samples)
            self._version_info = build_version_info(
                profile_key,
                stream_name,
                stream_source_id,
                self.sample_rate_hz,
                telemetry_available,
            )

    def add_samples(self, samples: Iterable[tuple[float, list[float]]]) -> None:
        with self._lock:
            for timestamp, values in samples:
                if len(values) < len(CHANNELS):
                    continue
                self._samples.append(
                    {
                        "timestamp": round(timestamp, 4),
                        "values": [round(float(value), 3) for value in values[: len(CHANNELS)]],
                    }
                )

    def add_motion_samples(self, motion_type: str, samples: Iterable[tuple[float, list[float]]]) -> None:
        target = self._acc_samples if motion_type == "acc" else self._gyro_samples
        with self._lock:
            for timestamp, values in samples:
                if len(values) < 3:
                    continue
                target.append(
                    {
                        "timestamp": round(timestamp, 4),
                        "values": [round(float(value), 4) for value in values[:3]],
                    }
                )

    def update_battery(self, percent: float, source: str) -> None:
        with self._lock:
            trend = "steady"
            if percent < self._battery.percent - 0.3:
                trend = "falling"
            elif percent > self._battery.percent + 0.3:
                trend = "charging"
            self._battery.percent = max(0.0, min(100.0, percent))
            self._battery.trend = trend
            self._battery.updated_at = utc_now()
            self._battery.source = source
            self._telemetry_available = True
            self._battery_history.append(
                {
                    "timestamp": self._battery.updated_at,
                    "percent": round(self._battery.percent, 1),
                }
            )

    def snapshot(self, profile_key: str) -> dict[str, Any]:
        with self._lock:
            profile = DEVICE_PROFILES.get(profile_key, DEVICE_PROFILES["muse-2"])
            samples = list(self._samples)
            latest_timestamp = samples[-1]["timestamp"] if samples else time.time()
            recent_samples = [
                {
                    "offsetMs": round((item["timestamp"] - latest_timestamp) * 1000.0, 1),
                    "values": item["values"],
                }
                for item in samples[-480:]
            ]
            fit_metrics = build_fit_metrics(samples, self.sample_rate_hz)
            motion_metrics = build_motion_metrics(list(self._acc_samples), list(self._gyro_samples))
            metrics = build_signal_metrics(samples, self.sample_rate_hz)
            calibration = build_calibration_guidance(
                fit_metrics=fit_metrics,
                motion_metrics=motion_metrics,
                signal_metrics=metrics,
                battery=self._battery,
                telemetry_available=self._telemetry_available,
            )
            self._fit_history.append(
                {
                    "timestamp": utc_now(),
                    "score": fit_metrics["overallScore"],
                }
            )
            self._motion_history.append(
                {
                    "timestamp": utc_now(),
                    "stability": motion_metrics["stabilityScore"],
                }
            )
            self._confidence_history.append(
                {
                    "timestamp": utc_now(),
                    "confidence": calibration["confidenceScore"],
                }
            )
            return {
                "generatedAt": utc_now(),
                "device": {
                    "selectedProfile": profile_key,
                    "label": profile["label"],
                    "headline": profile["headline"],
                    "notes": profile["notes"],
                    "channels": list(CHANNELS),
                    "supportedProfiles": [
                        {"id": key, "label": value["label"], "headline": value["headline"]}
                        for key, value in DEVICE_PROFILES.items()
                    ],
                    "version": self._version_info,
                },
                "connection": {
                    "connected": self._connected,
                    "mode": self._stream_mode,
                    "streamName": self._stream_name,
                    "streamSourceId": self._stream_source_id,
                    "telemetryAvailable": self._telemetry_available,
                    "motionAvailable": self._motion_available,
                    "statusLine": connection_status_copy(
                        connected=self._connected,
                        mode=self._stream_mode,
                        stream_name=self._stream_name,
                    ),
                    "lastError": self._last_error,
                },
                "battery": {
                    **self._battery.to_dict(),
                    "history": list(self._battery_history),
                },
                "sensorFit": {
                    **fit_metrics,
                    "history": list(self._fit_history),
                    "method": "Estimated from rolling EEG stability because this MuseLSL stream does not expose a dedicated fit metric.",
                },
                "motion": {
                    **motion_metrics,
                    "history": list(self._motion_history),
                },
                "calibration": {
                    **calibration,
                    "history": list(self._confidence_history),
                },
                "eeg": {
                    "sampleRateHz": self.sample_rate_hz,
                    "channels": list(CHANNELS),
                    "sampleCount": len(recent_samples),
                    "samples": recent_samples,
                    "metrics": metrics,
                },
            }


class MuseLSLBridge:
    def __init__(self, profile_key: str = "muse-2", sample_rate_hz: int = 64, max_seconds: int = 8) -> None:
        self.profile_key = profile_key if profile_key in DEVICE_PROFILES else "muse-2"
        self.store = MuseSampleStore(max_seconds=max_seconds, sample_rate_hz=sample_rate_hz)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._demo_phase = 0.0
        self._battery_cursor = 94.0
        self._pylsl_ready = StreamInlet is not None and resolve_byprop is not None
        self._eeg_inlet: Any = None
        self._telemetry_inlet: Any = None
        self._acc_inlet: Any = None
        self._gyro_inlet: Any = None
        self._last_resolve_attempt = 0.0
        self._stream_name = "Demo Muse Feed"
        self._stream_source_id = ""

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_forever, name="muse-lsl-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        return self.store.snapshot(self.profile_key)

    def _run_forever(self) -> None:
        while not self._stop_event.is_set():
            if self._try_pull_lsl():
                time.sleep(0.03)
            else:
                self._generate_demo_samples()
                time.sleep(0.12)

    def _try_pull_lsl(self) -> bool:
        if not self._pylsl_ready:
            self.store.set_connection(
                connected=False,
                stream_mode="demo",
                stream_name="Demo Muse Feed",
                stream_source_id="",
                telemetry_available=False,
                motion_available=False,
                last_error="Install pylsl and start MuseLSL to replace the demo feed with live EEG.",
                profile_key=self.profile_key,
            )
            return False

        now = time.time()
        if self._eeg_inlet is None and now - self._last_resolve_attempt >= 3.0:
            self._last_resolve_attempt = now
            try:
                streams = resolve_byprop("type", "EEG", timeout=1.0)
                if streams:
                    eeg_stream = streams[0]
                    self._eeg_inlet = StreamInlet(eeg_stream, max_buflen=self.store.max_seconds)
                    self._stream_name = eeg_stream.name() or "Muse EEG"
                    self._stream_source_id = stream_source_id(eeg_stream)
                    sample_rate = int(eeg_stream.nominal_srate() or self.store.sample_rate_hz)
                    self.profile_key = detect_profile_key(self._stream_name, self._stream_source_id)
                    self.store.set_connection(
                        connected=True,
                        stream_mode="lsl",
                        stream_name=self._stream_name,
                        stream_source_id=self._stream_source_id,
                        telemetry_available=self._telemetry_inlet is not None,
                        motion_available=self._acc_inlet is not None or self._gyro_inlet is not None,
                        last_error=None,
                        profile_key=self.profile_key,
                        sample_rate_hz=sample_rate,
                    )
                telemetry_streams = resolve_byprop("type", "Telemetry", timeout=0.25)
                if telemetry_streams:
                    self._telemetry_inlet = StreamInlet(telemetry_streams[0], max_buflen=2)
                acc_streams = resolve_byprop("type", "ACC", timeout=0.25)
                if acc_streams:
                    self._acc_inlet = StreamInlet(acc_streams[0], max_buflen=self.store.max_seconds)
                gyro_streams = resolve_byprop("type", "GYRO", timeout=0.25)
                if gyro_streams:
                    self._gyro_inlet = StreamInlet(gyro_streams[0], max_buflen=self.store.max_seconds)
            except Exception as exc:  # pragma: no cover - depends on runtime environment
                self._eeg_inlet = None
                self._telemetry_inlet = None
                self._acc_inlet = None
                self._gyro_inlet = None
                self.store.set_connection(
                    connected=False,
                    stream_mode="demo",
                    stream_name="Demo Muse Feed",
                    stream_source_id="",
                    telemetry_available=False,
                    motion_available=False,
                    last_error=f"MuseLSL lookup failed: {exc}",
                    profile_key=self.profile_key,
                )
                return False

        if self._eeg_inlet is None:
            self.store.set_connection(
                connected=False,
                stream_mode="demo",
                stream_name="Demo Muse Feed",
                stream_source_id="",
                telemetry_available=False,
                motion_available=False,
                last_error="No active MuseLSL EEG stream detected yet; showing demo data while the app keeps retrying.",
                profile_key=self.profile_key,
            )
            return False

        try:
            chunk, timestamps = self._eeg_inlet.pull_chunk(timeout=0.0, max_samples=24)
            if chunk and timestamps:
                paired = [(stamp, list(sample)) for sample, stamp in zip(chunk, timestamps)]
                self.store.add_samples(paired)
            if self._telemetry_inlet is not None:
                telemetry_chunk, _ = self._telemetry_inlet.pull_chunk(timeout=0.0, max_samples=4)
                if telemetry_chunk:
                    battery = extract_battery_percent(telemetry_chunk[-1])
                    if battery is not None:
                        self.store.update_battery(battery, source="lsl-telemetry")
            if self._acc_inlet is not None:
                acc_chunk, acc_timestamps = self._acc_inlet.pull_chunk(timeout=0.0, max_samples=12)
                if acc_chunk and acc_timestamps:
                    acc_pairs = [(stamp, list(sample)) for sample, stamp in zip(acc_chunk, acc_timestamps)]
                    self.store.add_motion_samples("acc", acc_pairs)
            if self._gyro_inlet is not None:
                gyro_chunk, gyro_timestamps = self._gyro_inlet.pull_chunk(timeout=0.0, max_samples=12)
                if gyro_chunk and gyro_timestamps:
                    gyro_pairs = [(stamp, list(sample)) for sample, stamp in zip(gyro_chunk, gyro_timestamps)]
                    self.store.add_motion_samples("gyro", gyro_pairs)
            self.store.set_connection(
                connected=True,
                stream_mode="lsl",
                stream_name=self._stream_name,
                stream_source_id=self._stream_source_id,
                telemetry_available=self._telemetry_inlet is not None,
                motion_available=self._acc_inlet is not None or self._gyro_inlet is not None,
                last_error=None,
                profile_key=self.profile_key,
            )
            return True
        except Exception as exc:  # pragma: no cover - depends on runtime environment
            self._eeg_inlet = None
            self._telemetry_inlet = None
            self._acc_inlet = None
            self._gyro_inlet = None
            self.store.set_connection(
                connected=False,
                stream_mode="demo",
                stream_name="Demo Muse Feed",
                stream_source_id="",
                telemetry_available=False,
                motion_available=False,
                last_error=f"MuseLSL stream dropped: {exc}",
                profile_key=self.profile_key,
            )
            return False

    def _generate_demo_samples(self) -> None:
        sample_rate = self.store.sample_rate_hz
        step = 1.0 / float(sample_rate)
        batch: list[tuple[float, list[float]]] = []
        now = time.time()
        for index in range(8):
            timestamp = now + (index * step)
            t = self._demo_phase + (index * step)
            values = [
                35.0 * math.sin((2.0 * math.pi * 7.8 * t) + 0.1),
                24.0 * math.sin((2.0 * math.pi * 10.3 * t) + 0.7) + 7.0 * math.cos(2.0 * math.pi * 0.4 * t),
                20.0 * math.sin((2.0 * math.pi * 12.2 * t) + 1.1),
                28.0 * math.sin((2.0 * math.pi * 5.6 * t) + 2.1) + 4.0 * math.sin(2.0 * math.pi * 20.0 * t),
            ]
            batch.append((timestamp, values))
        self._demo_phase += len(batch) * step
        self._battery_cursor -= 0.01
        if self._battery_cursor < 23.0:
            self._battery_cursor = 94.0
        self.store.add_samples(batch)
        self.store.add_motion_samples("acc", generate_demo_motion(now, self._demo_phase, kind="acc"))
        self.store.add_motion_samples("gyro", generate_demo_motion(now, self._demo_phase, kind="gyro"))
        self.store.update_battery(self._battery_cursor, source="demo")
        self.store.set_connection(
            connected=False,
            stream_mode="demo",
            stream_name="Demo Muse Feed",
            stream_source_id="demo-feed",
            telemetry_available=False,
            motion_available=True,
            last_error="Demo data is active until a live MuseLSL EEG stream appears.",
            profile_key=self.profile_key,
        )


def generate_demo_motion(now: float, phase: float, kind: str) -> list[tuple[float, list[float]]]:
    samples = []
    for index in range(6):
        timestamp = now + (index * 0.06)
        t = phase + index * 0.06
        if kind == "acc":
            values = [
                0.06 * math.sin(t * 0.8),
                0.08 * math.cos(t * 0.5),
                0.98 + (0.04 * math.sin(t * 0.35)),
            ]
        else:
            values = [
                4.5 * math.sin(t * 0.7),
                3.2 * math.cos(t * 0.45),
                2.1 * math.sin(t * 0.25),
            ]
        samples.append((timestamp, values))
    return samples


def detect_profile_key(stream_name: str, stream_source_id_value: str = "") -> str:
    lowered = f"{stream_name} {stream_source_id_value}".lower()
    if "muse s" in lowered or "muse 2" in lowered or "muse-2" in lowered:
        return "muse-2"
    if "muse 1" in lowered or "2014" in lowered or "classic" in lowered:
        return "muse-1"
    return "muse-2"


def stream_source_id(stream: Any) -> str:
    try:
        value = stream.source_id()
        return value or ""
    except Exception:
        return ""


def build_version_info(
    profile_key: str,
    stream_name: str,
    stream_source_id_value: str,
    sample_rate_hz: int,
    telemetry_available: bool,
) -> dict[str, Any]:
    lowered = f"{stream_name} {stream_source_id_value}".lower()
    confidence = "medium"
    evidence = []
    hardware_name = DEVICE_PROFILES.get(profile_key, DEVICE_PROFILES["muse-2"])["label"]

    if "muse s" in lowered:
        hardware_name = "Muse S"
        confidence = "high"
        evidence.append("BLE stream name advertises Muse S.")
    elif "muse 2" in lowered or "muse-2" in lowered:
        hardware_name = "Muse 2"
        confidence = "high"
        evidence.append("BLE stream name advertises Muse 2.")
    elif "muse 1" in lowered or "2014" in lowered or "classic" in lowered:
        hardware_name = "Muse 1 / Classic"
        confidence = "high"
        evidence.append("BLE stream name suggests the classic headset.")
    else:
        evidence.append("No explicit hardware string was present in the LSL stream identity.")

    if stream_source_id_value:
        evidence.append(f"LSL source id: {stream_source_id_value}")
    if telemetry_available:
        evidence.append("Live telemetry is available.")
    if sample_rate_hz:
        evidence.append(f"EEG sample rate reported as {sample_rate_hz} Hz.")

    return {
        "hardwareName": hardware_name,
        "profileLabel": DEVICE_PROFILES.get(profile_key, DEVICE_PROFILES["muse-2"])["label"],
        "confidence": confidence,
        "streamIdentity": stream_name,
        "sourceId": stream_source_id_value,
        "evidence": evidence,
    }


def build_fit_metrics(samples: list[dict[str, Any]], sample_rate_hz: int) -> dict[str, Any]:
    if not samples:
        empty_channels = [build_empty_fit_channel(channel) for channel in CHANNELS]
        return {
            "overallScore": 0,
            "overallLabel": "waiting",
            "channels": empty_channels,
        }

    window_size = min(len(samples), max(sample_rate_hz * 2, 48))
    window = samples[-window_size:]
    channels = []
    for channel_index, channel_name in enumerate(CHANNELS):
        values = [sample["values"][channel_index] for sample in window]
        channels.append(estimate_channel_fit(channel_name, values))

    overall = round(fmean(channel["score"] for channel in channels), 1)
    return {
        "overallScore": overall,
        "overallLabel": fit_label(overall),
        "channels": channels,
    }


def build_empty_fit_channel(channel_name: str) -> dict[str, Any]:
    return {
        "channel": channel_name,
        "score": 0.0,
        "label": "waiting",
        "contactState": "waiting for signal",
    }


def estimate_channel_fit(channel_name: str, values: list[float]) -> dict[str, Any]:
    if len(values) < 8:
        return build_empty_fit_channel(channel_name)

    mean_value = fmean(values)
    centered = [value - mean_value for value in values]
    variance = sum(value * value for value in centered) / len(centered)
    stddev = math.sqrt(variance)
    amplitude = max(values) - min(values)
    deltas = [abs(values[index] - values[index - 1]) for index in range(1, len(values))]
    avg_step = fmean(deltas) if deltas else 0.0
    flat_ratio = (
        sum(1 for delta in deltas if delta < 0.18) / len(deltas)
        if deltas
        else 1.0
    )

    std_component = banded_score(stddev, ideal=(6.0, 36.0), acceptable=(3.0, 70.0), maximum=40.0)
    amplitude_component = banded_score(amplitude, ideal=(18.0, 140.0), acceptable=(8.0, 210.0), maximum=35.0)
    step_component = banded_score(avg_step, ideal=(2.0, 24.0), acceptable=(0.6, 40.0), maximum=25.0)
    flat_penalty = flat_ratio * 38.0
    score = clamp(std_component + amplitude_component + step_component - flat_penalty, 0.0, 100.0)

    label = fit_label(score)
    if score >= 78:
        contact_state = "stable contact"
    elif score >= 58:
        contact_state = "good contact"
    elif score >= 38:
        contact_state = "adjusting"
    else:
        contact_state = "weak contact"

    return {
        "channel": channel_name,
        "score": round(score, 1),
        "label": label,
        "contactState": contact_state,
        "signalSpreadUv": round(stddev, 2),
        "peakToPeakUv": round(amplitude, 2),
        "movementIndex": round(avg_step, 2),
    }


def banded_score(value: float, ideal: tuple[float, float], acceptable: tuple[float, float], maximum: float) -> float:
    if ideal[0] <= value <= ideal[1]:
        return maximum
    if acceptable[0] <= value <= acceptable[1]:
        return maximum * 0.66
    return maximum * 0.28


def fit_label(score: float) -> str:
    if score >= 78:
        return "excellent"
    if score >= 58:
        return "good"
    if score >= 38:
        return "fair"
    if score > 0:
        return "poor"
    return "waiting"


def build_signal_metrics(samples: list[dict[str, Any]], sample_rate_hz: int) -> dict[str, Any]:
    if not samples:
        return {
            "bands": [],
            "moments": [],
            "continuity": {
                "score": 0.0,
                "label": "waiting",
            },
        }

    bands = compute_band_mix(samples, sample_rate_hz)
    moments = compute_waveform_moments(samples)
    continuity = compute_continuity(samples, sample_rate_hz)
    return {
        "bands": bands,
        "moments": moments,
        "continuity": continuity,
    }


def build_motion_metrics(
    acc_samples: list[dict[str, Any]],
    gyro_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    if not acc_samples and not gyro_samples:
        return {
            "available": False,
            "stabilityScore": 0.0,
            "stabilityLabel": "waiting",
            "headPose": {"pitchDeg": 0.0, "rollDeg": 0.0, "tiltLabel": "unknown"},
            "movement": {"gyroDps": 0.0, "accelG": 0.0, "label": "waiting"},
        }

    recent_acc = acc_samples[-min(len(acc_samples), 60) :]
    recent_gyro = gyro_samples[-min(len(gyro_samples), 60) :]
    last_acc = recent_acc[-1]["values"] if recent_acc else [0.0, 0.0, 1.0]
    last_gyro = recent_gyro[-1]["values"] if recent_gyro else [0.0, 0.0, 0.0]

    ax, ay, az = last_acc
    roll = math.degrees(math.atan2(ay, az if abs(az) > 0.001 else 0.001))
    pitch = math.degrees(math.atan2(-ax, math.sqrt((ay * ay) + (az * az)) or 0.001))
    accel_magnitude = math.sqrt(ax * ax + ay * ay + az * az)
    gyro_magnitude = math.sqrt(sum(value * value for value in last_gyro))

    acc_var = average_motion_variance(recent_acc)
    gyro_var = average_motion_variance(recent_gyro)
    motion_penalty = clamp((abs(accel_magnitude - 1.0) * 48.0) + (gyro_magnitude * 0.55) + (acc_var * 6.0) + (gyro_var * 0.12), 0.0, 100.0)
    stability = round(clamp(100.0 - motion_penalty, 0.0, 100.0), 1)

    return {
        "available": True,
        "stabilityScore": stability,
        "stabilityLabel": fit_label(stability),
        "headPose": {
            "pitchDeg": round(pitch, 1),
            "rollDeg": round(roll, 1),
            "tiltLabel": describe_tilt(pitch, roll),
        },
        "movement": {
            "gyroDps": round(gyro_magnitude, 2),
            "accelG": round(accel_magnitude, 3),
            "label": describe_motion(stability),
        },
    }


def average_motion_variance(samples: list[dict[str, Any]]) -> float:
    if len(samples) < 3:
        return 0.0
    magnitudes = [math.sqrt(sum(component * component for component in sample["values"])) for sample in samples]
    mean_value = fmean(magnitudes)
    return sum((value - mean_value) ** 2 for value in magnitudes) / len(magnitudes)


def describe_tilt(pitch: float, roll: float) -> str:
    if abs(pitch) < 8 and abs(roll) < 8:
        return "neutral"
    if pitch > 8:
        return "tilted forward"
    if pitch < -8:
        return "tilted back"
    if roll > 8:
        return "leaning right"
    return "leaning left"


def describe_motion(stability: float) -> str:
    if stability >= 82:
        return "very still"
    if stability >= 62:
        return "usable"
    if stability >= 40:
        return "moving"
    return "too much motion"


def build_calibration_guidance(
    *,
    fit_metrics: dict[str, Any],
    motion_metrics: dict[str, Any],
    signal_metrics: dict[str, Any],
    battery: BatteryState,
    telemetry_available: bool,
) -> dict[str, Any]:
    guidance = []
    score = 100.0

    if fit_metrics["overallScore"] < 60:
        score -= 28.0
        guidance.append("Re-seat the headset so TP9 and TP10 sit flat against the skin and move hair away from the sensors.")
    else:
        guidance.append("Sensor fit looks usable; keep the band pressure consistent while you record.")

    if motion_metrics["available"] and motion_metrics["stabilityScore"] < 65:
        score -= 24.0
        guidance.append("Reduce head movement for 20-30 seconds so the baseline can stabilize before trusting the bands.")
    else:
        guidance.append("Hold a neutral, comfortable head position during calibration for cleaner alpha and beta estimates.")

    continuity_score = signal_metrics["continuity"]["score"]
    if continuity_score < 72:
        score -= 18.0
        guidance.append("The EEG stream timing is a bit uneven; stay close to the computer and avoid Bluetooth congestion while calibrating.")
    else:
        guidance.append("Signal timing looks steady enough for baseline calibration.")

    if not telemetry_available:
        score -= 8.0
        guidance.append("Battery is estimated rather than measured live, so keep an eye on the headset and reconnect if the signal weakens.")

    if battery.percent <= 25:
        score -= 12.0
        guidance.append("Battery is getting low; charge the headset soon to avoid drift or disconnects in longer sessions.")

    guidance.extend(
        [
            "Clean the forehead and ear contact points and let the headset settle for about one minute before judging accuracy.",
            "Calibrate in a quiet spot away from chargers, fans, and Bluetooth clutter when possible.",
            "For the steadiest brain-wave baseline, relax your jaw, keep your eyes soft, and breathe evenly for 30 seconds.",
        ]
    )

    confidence = round(clamp(score, 0.0, 100.0), 1)
    return {
        "confidenceScore": confidence,
        "confidenceLabel": fit_label(confidence),
        "continuityScore": continuity_score,
        "preparationGuide": guidance[:6],
    }


def compute_continuity(samples: list[dict[str, Any]], sample_rate_hz: int) -> dict[str, Any]:
    if len(samples) < 4 or sample_rate_hz <= 0:
        return {"score": 0.0, "label": "waiting"}
    recent = samples[-min(len(samples), sample_rate_hz * 2) :]
    deltas = [recent[index]["timestamp"] - recent[index - 1]["timestamp"] for index in range(1, len(recent))]
    target = 1.0 / sample_rate_hz
    jitter = fmean(abs(delta - target) for delta in deltas)
    fill_ratio = len(recent) / max(sample_rate_hz * 2, 1)
    score = clamp(100.0 - ((jitter / target) * 180.0) - max(0.0, (1.0 - fill_ratio) * 35.0), 0.0, 100.0)
    return {
        "score": round(score, 1),
        "label": fit_label(score),
    }


def compute_band_mix(samples: list[dict[str, Any]], sample_rate_hz: int) -> list[dict[str, Any]]:
    window = samples[-min(len(samples), max(sample_rate_hz, 64)) :]
    results = []
    for channel_index, channel_name in enumerate(CHANNELS):
        series = [sample["values"][channel_index] for sample in window]
        totals: dict[str, float] = {}
        grand_total = 0.0
        for name, start_hz, end_hz in BAND_DEFS:
            total = 0.0
            for frequency in range(start_hz, end_hz):
                total += dft_power(series, sample_rate_hz, frequency)
            totals[name] = total
            grand_total += total
        mix = {
            name: round((totals[name] / grand_total) * 100.0, 1) if grand_total else 0.0
            for name, _, _ in BAND_DEFS
        }
        results.append({"channel": channel_name, "mix": mix})
    return results


def compute_waveform_moments(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    window = samples[-min(len(samples), 160) :]
    metrics = []
    for channel_index, channel_name in enumerate(CHANNELS):
        values = [sample["values"][channel_index] for sample in window]
        if not values:
            continue
        mean_abs = fmean(abs(value) for value in values)
        rms = math.sqrt(sum(value * value for value in values) / len(values))
        peak = max(abs(value) for value in values)
        metrics.append(
            {
                "channel": channel_name,
                "meanAbsUv": round(mean_abs, 2),
                "rmsUv": round(rms, 2),
                "peakUv": round(peak, 2),
            }
        )
    return metrics


def dft_power(series: list[float], sample_rate_hz: int, frequency: int) -> float:
    if not series:
        return 0.0
    length = len(series)
    real = 0.0
    imaginary = 0.0
    for index, value in enumerate(series):
        angle = (2.0 * math.pi * frequency * index) / sample_rate_hz
        real += value * math.cos(angle)
        imaginary -= value * math.sin(angle)
    return real * real + imaginary * imaginary


def extract_battery_percent(sample: Iterable[Any]) -> float | None:
    for value in sample:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 < numeric <= 1.0:
            return numeric * 100.0
        if 0.0 < numeric <= 100.0:
            return numeric
    return None


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def connection_status_copy(*, connected: bool, mode: str, stream_name: str) -> str:
    if connected and mode == "lsl":
        return f"Streaming live MuseLSL data from {stream_name}."
    if mode == "demo":
        return "Running the polished demo feed while the app waits for MuseLSL."
    return "Preparing the MuseLSL bridge."


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
