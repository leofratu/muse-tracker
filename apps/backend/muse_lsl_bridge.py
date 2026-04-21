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
FRONT_CHANNELS = ("AF7", "AF8")
REAR_CHANNELS = ("TP9", "TP10")
MIN_COMBINED_CLEAN_CHANNELS = 3
MIN_BASELINE_WINDOWS = 18
MIN_BASELINE_DURATION_SECONDS = 45.0

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
    percent: float | None = None
    trend: str = "waiting"
    updated_at: str = ""
    source: str = "waiting"

    def to_dict(self) -> dict[str, Any]:
        if self.percent is None:
            percent = None
            level = "waiting"
        else:
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
            "percent": round(percent, 1) if percent is not None else None,
            "level": level,
            "trend": self.trend,
            "updatedAt": self.updated_at or utc_now(),
            "source": self.source,
            "isLow": bool(percent is not None and percent <= 30),
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
        self._stream_name = "Waiting for MuseLSL"
        self._stream_source_id = ""
        self._stream_mode = "waiting"
        self._connected = False
        self._telemetry_available = False
        self._motion_available = False
        self._last_error = "Waiting for a live MuseLSL EEG stream."
        self._battery_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._telemetry_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._fit_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._fit_channel_history: dict[str, Deque[dict[str, Any]]] = {
            channel: deque(maxlen=180) for channel in CHANNELS
        }
        self._motion_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._confidence_history: Deque[dict[str, Any]] = deque(maxlen=180)
        self._baseline_history: Deque[dict[str, Any]] = deque(maxlen=240)
        self._last_baseline_timestamp = 0.0
        self._telemetry = {
            "batteryPercent": None,
            "fuelGaugePercent": None,
            "adcVolt": None,
            "temperatureC": None,
            "updatedAt": utc_now(),
            "source": "waiting",
        }
        self._version_info = build_version_info("muse-2", "Waiting for MuseLSL", "", sample_rate_hz, False)
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

    def update_battery(self, percent: float, source: str, *, mark_available: bool | None = None) -> None:
        with self._lock:
            trend = "steady"
            if self._battery.percent is not None:
                if percent < self._battery.percent - 0.3:
                    trend = "falling"
                elif percent > self._battery.percent + 0.3:
                    trend = "charging"
            self._battery.percent = max(0.0, min(100.0, percent))
            self._battery.trend = trend
            self._battery.updated_at = utc_now()
            self._battery.source = source
            if mark_available is None:
                mark_available = source.startswith("lsl")
            if mark_available:
                self._telemetry_available = True
            self._battery_history.append(
                {
                    "timestamp": self._battery.updated_at,
                    "percent": round(self._battery.percent, 1),
                }
            )

    def update_telemetry(self, sample: Iterable[Any], source: str) -> None:
        metrics = extract_telemetry_metrics(sample)
        is_live = source.startswith("lsl")
        if metrics["batteryPercent"] is not None:
            self.update_battery(metrics["batteryPercent"], source, mark_available=is_live)
        with self._lock:
            self._telemetry = {
                **metrics,
                "updatedAt": utc_now(),
                "source": source,
            }
            self._telemetry_history.append(
                {
                    "timestamp": self._telemetry["updatedAt"],
                    "batteryPercent": metrics["batteryPercent"],
                    "fuelGaugePercent": metrics["fuelGaugePercent"],
                    "adcVolt": metrics["adcVolt"],
                    "temperatureC": metrics["temperatureC"],
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
            metrics = build_signal_metrics(
                samples,
                self.sample_rate_hz,
                fit_metrics=fit_metrics,
                motion_metrics=motion_metrics,
            )
            brain_state = build_brain_state(metrics, fit_metrics, motion_metrics)
            baseline_point = build_baseline_point(
                signal_metrics=metrics,
                fit_metrics=fit_metrics,
                motion_metrics=motion_metrics,
                latest_timestamp=latest_timestamp,
            )
            if baseline_point and (
                self._last_baseline_timestamp <= 0.0
                or latest_timestamp - self._last_baseline_timestamp >= 1.0
            ):
                self._baseline_history.append(baseline_point)
                self._last_baseline_timestamp = latest_timestamp
            baseline_metrics = build_baseline_metrics(
                history=list(self._baseline_history),
                current_point=baseline_point,
            )
            calibration = build_calibration_guidance(
                fit_metrics=fit_metrics,
                motion_metrics=motion_metrics,
                signal_metrics=metrics,
                battery=self._battery,
                telemetry_available=self._telemetry_available,
                baseline_metrics=baseline_metrics,
            )
            if samples:
                self._fit_history.append(
                    {
                        "timestamp": utc_now(),
                        "score": fit_metrics["overallScore"],
                    }
                )
                for channel in fit_metrics["channels"]:
                    self._fit_channel_history[channel["channel"]].append(
                        {
                            "timestamp": utc_now(),
                            "score": round(channel["score"], 1),
                        }
                    )
                self._confidence_history.append(
                    {
                        "timestamp": utc_now(),
                        "confidence": calibration["confidenceScore"],
                    }
                )
            if motion_metrics["available"]:
                self._motion_history.append(
                    {
                        "timestamp": utc_now(),
                        "stability": motion_metrics["stabilityScore"],
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
                    "streams": build_connection_streams(
                        sample_rate_hz=self.sample_rate_hz,
                        mode=self._stream_mode,
                        telemetry_live=self._telemetry_available,
                        telemetry=self._telemetry,
                        acc_samples=list(self._acc_samples),
                        gyro_samples=list(self._gyro_samples),
                    ),
                    "lastError": self._last_error,
                },
                "battery": {
                    **self._battery.to_dict(),
                    "history": list(self._battery_history),
                },
                "telemetry": {
                    **self._telemetry,
                    "available": bool(self._telemetry_history) and self._telemetry_available,
                    "live": self._telemetry_available,
                    "history": list(self._telemetry_history),
                },
                "sensorFit": {
                    **attach_fit_channel_history(fit_metrics, self._fit_channel_history),
                    "history": list(self._fit_history),
                    "method": "EEG-derived estimated contact because this MuseLSL stream does not expose a dedicated impedance/contact metric.",
                },
                "motion": {
                    **motion_metrics,
                    "history": list(self._motion_history),
                },
                "baseline": baseline_metrics,
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
                "brainState": brain_state,
            }


class MuseLSLBridge:
    def __init__(self, profile_key: str = "muse-2", sample_rate_hz: int = 64, max_seconds: int = 8) -> None:
        self.profile_key = profile_key if profile_key in DEVICE_PROFILES else "muse-2"
        self.store = MuseSampleStore(max_seconds=max_seconds, sample_rate_hz=sample_rate_hz)
        self.store.set_connection(
            connected=False,
            stream_mode="waiting",
            stream_name="Waiting for MuseLSL",
            stream_source_id="",
            telemetry_available=False,
            motion_available=False,
            last_error="Waiting for a live MuseLSL EEG stream.",
            profile_key=self.profile_key,
            sample_rate_hz=sample_rate_hz,
        )
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pylsl_ready = StreamInlet is not None and resolve_byprop is not None
        self._eeg_inlet: Any = None
        self._telemetry_inlet: Any = None
        self._acc_inlet: Any = None
        self._gyro_inlet: Any = None
        self._last_resolve_attempt = 0.0
        self._stream_name = "Waiting for MuseLSL"
        self._stream_source_id = ""
        self._last_eeg_sample_at = 0.0
        self._last_telemetry_sample_at = 0.0
        self._last_acc_sample_at = 0.0
        self._last_gyro_sample_at = 0.0

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
            self._try_pull_lsl()
            time.sleep(0.03)

    def _build_inlet(self, stream: Any, max_buflen: int) -> Any:
        inlet = StreamInlet(stream, max_buflen=max_buflen)
        open_stream = getattr(inlet, "open_stream", None)
        if callable(open_stream):
            try:
                open_stream(timeout=0.25)
            except Exception:
                # Some pylsl builds do not require or like an explicit open.
                pass
        return inlet

    def _stream_is_live(self, last_sample_at: float, *, max_age_seconds: float = 3.0) -> bool:
        return last_sample_at > 0.0 and (time.monotonic() - last_sample_at) <= max_age_seconds

    def _pick_preferred_stream(self, streams: list[Any]) -> Any:
        if len(streams) <= 1:
            return streams[0]
        current_identity = f"{self._stream_name} {self._stream_source_id}".lower()
        preferred = sorted(
            streams,
            key=lambda stream: (
                0 if "muse" in f"{safe_stream_name(stream)} {stream_source_id(stream)}".lower() else 1,
                0
                if current_identity
                and current_identity
                in f"{safe_stream_name(stream)} {stream_source_id(stream)}".lower()
                else 1,
                -int(getattr(stream, "channel_count", lambda: 0)() or 0),
            ),
        )
        return preferred[0]

    def _pull_pairs(self, inlet: Any, *, max_samples: int, fallback_timeout: float) -> list[tuple[float, list[float]]]:
        chunk, timestamps = inlet.pull_chunk(timeout=0.0, max_samples=max_samples)
        if chunk and timestamps:
            return [(stamp, list(sample)) for sample, stamp in zip(chunk, timestamps)]

        sample, timestamp = inlet.pull_sample(timeout=fallback_timeout)
        if sample is None or timestamp is None:
            return []

        pairs = [(timestamp, list(sample))]
        for _ in range(max_samples - 1):
            sample, timestamp = inlet.pull_sample(timeout=0.0)
            if sample is None or timestamp is None:
                break
            pairs.append((timestamp, list(sample)))
        return pairs

    def _reset_inlets(self) -> None:
        self._eeg_inlet = None
        self._telemetry_inlet = None
        self._acc_inlet = None
        self._gyro_inlet = None

    def _try_pull_lsl(self) -> bool:
        if not self._pylsl_ready:
            self.store.set_connection(
                connected=False,
                stream_mode="waiting",
                stream_name="Waiting for MuseLSL",
                stream_source_id="",
                telemetry_available=False,
                motion_available=False,
                last_error="Install pylsl and start MuseLSL to stream live EEG.",
                profile_key=self.profile_key,
            )
            return False

        now = time.time()
        if self._eeg_inlet is None and now - self._last_resolve_attempt >= 3.0:
            self._last_resolve_attempt = now
            try:
                streams = resolve_byprop("type", "EEG", timeout=1.0)
                if streams:
                    eeg_stream = self._pick_preferred_stream(streams)
                    self._eeg_inlet = self._build_inlet(eeg_stream, max_buflen=self.store.max_seconds)
                    self._stream_name = safe_stream_name(eeg_stream)
                    self._stream_source_id = stream_source_id(eeg_stream)
                    sample_rate = int(eeg_stream.nominal_srate() or self.store.sample_rate_hz)
                    self.profile_key = detect_profile_key(self._stream_name, self._stream_source_id)
                    self.store.set_connection(
                        connected=True,
                        stream_mode="lsl",
                        stream_name=self._stream_name,
                        stream_source_id=self._stream_source_id,
                        telemetry_available=self._stream_is_live(self._last_telemetry_sample_at),
                        motion_available=self._stream_is_live(self._last_acc_sample_at) or self._stream_is_live(self._last_gyro_sample_at),
                        last_error=None,
                        profile_key=self.profile_key,
                        sample_rate_hz=sample_rate,
                    )
                telemetry_streams = resolve_byprop("type", "Telemetry", timeout=0.25)
                if telemetry_streams:
                    self._telemetry_inlet = self._build_inlet(
                        self._pick_preferred_stream(telemetry_streams),
                        max_buflen=2,
                    )
                acc_streams = resolve_byprop("type", "ACC", timeout=0.25)
                if acc_streams:
                    self._acc_inlet = self._build_inlet(
                        self._pick_preferred_stream(acc_streams),
                        max_buflen=self.store.max_seconds,
                    )
                gyro_streams = resolve_byprop("type", "GYRO", timeout=0.25)
                if gyro_streams:
                    self._gyro_inlet = self._build_inlet(
                        self._pick_preferred_stream(gyro_streams),
                        max_buflen=self.store.max_seconds,
                    )
            except Exception as exc:  # pragma: no cover - depends on runtime environment
                self._reset_inlets()
                self.store.set_connection(
                    connected=False,
                    stream_mode="waiting",
                    stream_name="Waiting for MuseLSL",
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
                stream_mode="waiting",
                stream_name="Waiting for MuseLSL",
                stream_source_id="",
                telemetry_available=False,
                motion_available=False,
                last_error="No active MuseLSL EEG stream detected yet; the app is waiting for your headset.",
                profile_key=self.profile_key,
            )
            return False

        try:
            eeg_pairs = self._pull_pairs(self._eeg_inlet, max_samples=24, fallback_timeout=0.03)
            if eeg_pairs:
                self.store.add_samples(eeg_pairs)
                self._last_eeg_sample_at = time.monotonic()
            if self._telemetry_inlet is not None:
                telemetry_pairs = self._pull_pairs(self._telemetry_inlet, max_samples=4, fallback_timeout=0.0)
                if telemetry_pairs:
                    self.store.update_telemetry(telemetry_pairs[-1][1], source="lsl-telemetry")
                    self._last_telemetry_sample_at = time.monotonic()
            if self._acc_inlet is not None:
                acc_pairs = self._pull_pairs(self._acc_inlet, max_samples=12, fallback_timeout=0.0)
                if acc_pairs:
                    self.store.add_motion_samples("acc", acc_pairs)
                    self._last_acc_sample_at = time.monotonic()
            if self._gyro_inlet is not None:
                gyro_pairs = self._pull_pairs(self._gyro_inlet, max_samples=12, fallback_timeout=0.0)
                if gyro_pairs:
                    self.store.add_motion_samples("gyro", gyro_pairs)
                    self._last_gyro_sample_at = time.monotonic()

            eeg_live = self._stream_is_live(self._last_eeg_sample_at, max_age_seconds=2.0)
            telemetry_live = self._stream_is_live(self._last_telemetry_sample_at)
            motion_live = self._stream_is_live(self._last_acc_sample_at) or self._stream_is_live(self._last_gyro_sample_at)

            last_error = None
            if not eeg_live:
                last_error = "Connected to MuseLSL, but waiting for live EEG samples to arrive."
                if self._last_eeg_sample_at and (time.monotonic() - self._last_eeg_sample_at) > 6.0:
                    self._reset_inlets()
                    return False
            self.store.set_connection(
                connected=True,
                stream_mode="lsl",
                stream_name=self._stream_name,
                stream_source_id=self._stream_source_id,
                telemetry_available=telemetry_live,
                motion_available=motion_live,
                last_error=last_error,
                profile_key=self.profile_key,
            )
            return eeg_live
        except Exception as exc:  # pragma: no cover - depends on runtime environment
            self._reset_inlets()
            self.store.set_connection(
                connected=False,
                stream_mode="waiting",
                stream_name="Waiting for MuseLSL",
                stream_source_id="",
                telemetry_available=False,
                motion_available=False,
                last_error=f"MuseLSL stream dropped: {exc}",
                profile_key=self.profile_key,
            )
            return False


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


def safe_stream_name(stream: Any) -> str:
    try:
        return stream.name() or "Muse EEG"
    except Exception:
        return "Muse EEG"


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
            "contactModel": "eeg-derived estimate",
            "telemetryScope": "Headset-level telemetry only. No per-sensor voltage or wattage is available from this stream.",
            "asymmetry": {
                "status": "waiting",
                "frontAverage": 0.0,
                "rearAverage": 0.0,
                "difference": 0.0,
                "summary": "Waiting for enough signal to compare front and rear contact stability.",
            },
            "channels": empty_channels,
            "officialView": {
                "shape": "horseshoe",
                "sensors": empty_channels,
            },
        }

    window_size = min(len(samples), max(sample_rate_hz * 2, 48))
    window = samples[-window_size:]
    channels = []
    for channel_index, channel_name in enumerate(CHANNELS):
        values = [sample["values"][channel_index] for sample in window]
        channels.append(estimate_channel_fit(channel_name, values))

    overall = round(fmean(channel["score"] for channel in channels), 1)
    asymmetry = summarize_contact_asymmetry(channels)
    return {
        "overallScore": overall,
        "overallLabel": fit_label(overall),
        "contactModel": "eeg-derived estimate",
        "telemetryScope": "Headset-level telemetry only. No per-sensor voltage or wattage is available from this stream.",
        "asymmetry": asymmetry,
        "channels": channels,
        "officialView": {
            "shape": "horseshoe",
            "sensors": [
                {
                    "channel": channel["channel"],
                    "score": channel["score"],
                    "status": sensor_status(channel["score"]),
                    "contactState": channel["contactState"],
                }
                for channel in channels
            ],
        },
    }


def attach_fit_channel_history(
    fit_metrics: dict[str, Any],
    fit_history: dict[str, Deque[dict[str, Any]]],
) -> dict[str, Any]:
    channels = []
    for channel in fit_metrics["channels"]:
        channels.append(
            {
                **channel,
                "history": list(fit_history.get(channel["channel"], [])),
            }
        )
    return {
        **fit_metrics,
        "channels": channels,
    }


def build_empty_fit_channel(channel_name: str) -> dict[str, Any]:
    return {
        "channel": channel_name,
        "score": 0.0,
        "label": "waiting",
        "status": "waiting",
        "contactState": "waiting for signal",
        "basis": "eeg-derived estimate",
        "clipRatio": 0.0,
        "railRatio": 0.0,
        "driftRatio": 0.0,
        "flatRatio": 0.0,
        "artifactPenalty": 0.0,
        "notes": ["Waiting for live EEG samples."],
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
    clip_ratio = estimate_clip_ratio(values)
    rail_ratio = estimate_rail_ratio(values)
    drift_ratio = estimate_drift_ratio(values)
    spike_ratio = estimate_spike_ratio(values)

    std_component = banded_score(stddev, ideal=(6.0, 36.0), acceptable=(3.0, 70.0), maximum=40.0)
    amplitude_component = banded_score(amplitude, ideal=(18.0, 140.0), acceptable=(8.0, 210.0), maximum=35.0)
    step_component = banded_score(avg_step, ideal=(2.0, 24.0), acceptable=(0.6, 40.0), maximum=25.0)
    flat_penalty = flat_ratio * 38.0
    artifact_penalty = (clip_ratio * 2500.0) + (rail_ratio * 1100.0) + (drift_ratio * 44.0) + (spike_ratio * 55.0)
    score = clamp(
        std_component + amplitude_component + step_component - flat_penalty - artifact_penalty,
        0.0,
        100.0,
    )

    label = fit_label(score)
    notes = []
    if clip_ratio >= 0.01:
        notes.append("This channel is railing near the ADC limits, so the contact estimate is unreliable.")
    if drift_ratio >= 0.22:
        notes.append("Slow baseline drift is leaking through this channel.")
    if flat_ratio >= 0.18:
        notes.append("This channel is flattening out too often for a stable contact estimate.")
    if not notes:
        notes.append("EEG-derived contact estimate looks stable enough for this channel.")

    if clip_ratio >= 0.01 or rail_ratio >= 0.02:
        contact_state = "railed / unstable"
    elif drift_ratio >= 0.24:
        contact_state = "drifty contact"
    elif score >= 78:
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
        "status": sensor_status(score),
        "contactState": contact_state,
        "basis": "eeg-derived estimate",
        "signalSpreadUv": round(stddev, 2),
        "peakToPeakUv": round(amplitude, 2),
        "movementIndex": round(avg_step, 2),
        "clipRatio": round(clip_ratio, 3),
        "railRatio": round(rail_ratio, 3),
        "driftRatio": round(drift_ratio, 3),
        "flatRatio": round(flat_ratio, 3),
        "artifactPenalty": round(artifact_penalty, 1),
        "notes": notes,
    }


def summarize_contact_asymmetry(channels: list[dict[str, Any]]) -> dict[str, Any]:
    if not channels:
        return {
            "status": "waiting",
            "frontAverage": 0.0,
            "rearAverage": 0.0,
            "difference": 0.0,
            "summary": "Waiting for enough signal to compare front and rear contact stability.",
        }

    front_scores = [channel.get("score", 0.0) for channel in channels if channel["channel"] in FRONT_CHANNELS]
    rear_scores = [channel.get("score", 0.0) for channel in channels if channel["channel"] in REAR_CHANNELS]
    if not front_scores or not rear_scores:
        return {
            "status": "partial",
            "frontAverage": round(fmean(front_scores), 1) if front_scores else 0.0,
            "rearAverage": round(fmean(rear_scores), 1) if rear_scores else 0.0,
            "difference": 0.0,
            "summary": "Need both frontal and rear sensors before comparing asymmetry.",
        }

    front_average = round(fmean(front_scores), 1)
    rear_average = round(fmean(rear_scores), 1)
    difference = round(front_average - rear_average, 1)
    if difference >= 12.0:
        status = "rear-weaker"
        summary = (
            "Rear contact looks weaker or noisier than the frontal sensors. "
            "This is an EEG-derived estimate, not a measured rear-sensor voltage or wattage reading."
        )
    elif difference <= -12.0:
        status = "front-weaker"
        summary = (
            "Frontal contact looks weaker or noisier than the rear sensors. "
            "This is inferred from EEG behavior, not from per-sensor hardware telemetry."
        )
    else:
        status = "balanced"
        summary = (
            "Front and rear contact estimates are reasonably balanced. "
            "These values are inferred from EEG stability rather than direct impedance telemetry."
        )
    return {
        "status": status,
        "frontAverage": front_average,
        "rearAverage": rear_average,
        "difference": difference,
        "summary": summary,
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


def sensor_status(score: float) -> str:
    if score >= 80:
        return "excellent"
    if score >= 60:
        return "good"
    if score >= 40:
        return "fair"
    if score > 0:
        return "poor"
    return "waiting"


def build_signal_metrics(
    samples: list[dict[str, Any]],
    sample_rate_hz: int,
    *,
    fit_metrics: dict[str, Any] | None = None,
    motion_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not samples:
        return {
            "bands": [],
            "moments": [],
            "overallBands": {name: 0.0 for name, _, _ in BAND_DEFS},
            "dominantBand": "waiting",
            "quality": {
                "artifactScore": 0.0,
                "agreementScore": 0.0,
                "driftScore": 0.0,
                "lineNoiseScore": 0.0,
                "contactScore": 0.0,
                "stabilityScore": 0.0,
                "motionScore": 0.0,
                "accuracyScore": 0.0,
                "usableChannelCount": 0,
                "label": "waiting",
                "summary": "Waiting for enough signal to score artifacts and channel agreement.",
                "blockers": ["Waiting for live EEG samples."],
            },
            "deltaDominance": {
                "score": 0.0,
                "status": "waiting",
                "warning": "Waiting for enough signal to judge the overall band balance.",
            },
            "continuity": {
                "score": 0.0,
                "label": "waiting",
            },
            "aggregate": {
                "withheld": True,
                "sourceMode": "withheld",
                "selectedChannels": [],
                "sourceSensors": [],
                "cleanSensorCount": 0,
                "reasons": ["Waiting for live EEG samples."],
                "summary": "Overall brain-state output is waiting for live EEG samples.",
            },
        }

    bands = compute_band_mix(
        samples,
        sample_rate_hz,
        fit_metrics=fit_metrics,
        motion_metrics=motion_metrics,
    )
    moments = compute_waveform_moments(samples)
    continuity = compute_continuity(samples, sample_rate_hz)
    quality = compute_signal_quality(
        bands,
        fit_metrics=fit_metrics,
        motion_metrics=motion_metrics,
        continuity=continuity,
    )
    aggregate = select_aggregate_channels(bands)
    overall_bands = compute_overall_band_mix(aggregate["selectedChannels"])
    dominant_band = max(overall_bands, key=overall_bands.get) if aggregate["selectedChannels"] else "waiting"
    delta_dominance = assess_delta_dominance(overall_bands, quality, aggregate)
    return {
        "bands": bands,
        "moments": moments,
        "overallBands": overall_bands,
        "dominantBand": dominant_band,
        "quality": quality,
        "deltaDominance": delta_dominance,
        "continuity": continuity,
        "aggregate": aggregate,
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
            "sensors": build_motion_sensor_views([], []),
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
        "sensors": build_motion_sensor_views(recent_acc, recent_gyro),
    }


def average_motion_variance(samples: list[dict[str, Any]]) -> float:
    if len(samples) < 3:
        return 0.0
    magnitudes = [math.sqrt(sum(component * component for component in sample["values"])) for sample in samples]
    mean_value = fmean(magnitudes)
    return sum((value - mean_value) ** 2 for value in magnitudes) / len(magnitudes)


def build_motion_sensor_views(
    acc_samples: list[dict[str, Any]],
    gyro_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "accelerometer": build_vector_sensor_view(acc_samples, label="Accelerometer", unit="g"),
        "gyroscope": build_vector_sensor_view(gyro_samples, label="Gyroscope", unit="dps"),
    }


def build_vector_sensor_view(samples: list[dict[str, Any]], *, label: str, unit: str) -> dict[str, Any]:
    if not samples:
        return {
            "label": label,
            "unit": unit,
            "available": False,
            "sampleRateHz": 0.0,
            "latest": {"x": 0.0, "y": 0.0, "z": 0.0},
            "history": [],
        }

    recent = samples[-min(len(samples), 90) :]
    latest_timestamp = recent[-1]["timestamp"]
    latest = recent[-1]["values"]
    return {
        "label": label,
        "unit": unit,
        "available": True,
        "sampleRateHz": round(estimate_sample_rate(recent), 1),
        "latest": {
            "x": round(latest[0], 4),
            "y": round(latest[1], 4),
            "z": round(latest[2], 4),
        },
        "history": [
            {
                "offsetMs": round((sample["timestamp"] - latest_timestamp) * 1000.0, 1),
                "values": sample["values"],
            }
            for sample in recent
        ],
    }


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


def build_baseline_point(
    *,
    signal_metrics: dict[str, Any],
    fit_metrics: dict[str, Any],
    motion_metrics: dict[str, Any],
    latest_timestamp: float,
) -> dict[str, Any] | None:
    overall_bands = signal_metrics.get("overallBands") or {}
    quality = signal_metrics.get("quality") or {}
    continuity = signal_metrics.get("continuity") or {}
    if not overall_bands:
        return None

    aggregate = signal_metrics.get("aggregate") or {}
    delta = float(overall_bands.get("delta", 0.0))
    theta = float(overall_bands.get("theta", 0.0))
    alpha = float(overall_bands.get("alpha", 0.0))
    beta = float(overall_bands.get("beta", 0.0))
    gamma = float(overall_bands.get("gamma", 0.0))
    accuracy = float(quality.get("accuracyScore", 0.0))
    fit_score = float(fit_metrics.get("overallScore", 0.0))
    motion_score = float(motion_metrics.get("stabilityScore", 65.0 if not motion_metrics.get("available") else 0.0))
    continuity_score = float(continuity.get("score", 0.0))
    calm_index = clamp(55.0 + (alpha * 0.55) + (theta * 0.18) - (beta * 0.18) - (delta * 0.48), 0.0, 100.0)
    focus_index = clamp(50.0 + (beta * 0.9) + (alpha * 0.38) + (gamma * 0.12) - (delta * 0.78) - (theta * 0.25), 0.0, 100.0)
    quality_anchor = clamp(
        (accuracy * 0.52) + (fit_score * 0.2) + (motion_score * 0.18) + (continuity_score * 0.1),
        0.0,
        100.0,
    )
    accepted_for_baseline = (
        not aggregate.get("withheld", False)
        and accuracy >= 64.0
        and fit_score >= 40.0
        and motion_score >= 55.0
        and continuity_score >= 74.0
        and signal_metrics.get("deltaDominance", {}).get("status") != "strong"
    )
    eligible_normal = accepted_for_baseline and accuracy >= 68.0 and continuity_score >= 80.0

    return {
        "timestamp": round(latest_timestamp, 4),
        "capturedAt": utc_now(),
        "bands": {name: round(float(overall_bands.get(name, 0.0)), 1) for name, _, _ in BAND_DEFS},
        "dominantBand": signal_metrics.get("dominantBand", "waiting"),
        "accuracyScore": round(accuracy, 1),
        "fitScore": round(fit_score, 1),
        "motionScore": round(motion_score, 1),
        "continuityScore": round(continuity_score, 1),
        "qualityAnchor": round(quality_anchor, 1),
        "focusIndex": round(focus_index, 1),
        "calmIndex": round(calm_index, 1),
        "acceptedForBaseline": accepted_for_baseline,
        "eligibleNormal": eligible_normal,
        "sourceMode": aggregate.get("sourceMode", "withheld"),
        "cleanSensorCount": int(aggregate.get("cleanSensorCount", 0)),
    }


def build_baseline_metrics(
    *,
    history: list[dict[str, Any]],
    current_point: dict[str, Any] | None,
) -> dict[str, Any]:
    if not history and not current_point:
        return empty_baseline_metrics()

    points = list(history)
    if current_point and (not points or points[-1]["timestamp"] != current_point["timestamp"]):
        points.append(current_point)

    if not points:
        return empty_baseline_metrics()

    current = current_point or points[-1]
    accepted_points = [point for point in points if point.get("acceptedForBaseline", True)]
    pool = accepted_points or []
    duration_seconds = max(0.0, pool[-1]["timestamp"] - pool[0]["timestamp"]) if len(pool) > 1 else 0.0
    normal_points = [point for point in pool if point.get("eligibleNormal")]
    baseline_pool = normal_points or pool

    focused_points = []
    if baseline_pool:
        sorted_by_focus = sorted(baseline_pool, key=lambda point: point.get("focusIndex", 0.0), reverse=True)
        focused_take = max(1, min(len(sorted_by_focus), max(3, len(sorted_by_focus) // 3)))
        focused_points = sorted_by_focus[:focused_take]

    normal_summary = summarize_baseline_cluster(baseline_pool)
    focused_summary = summarize_baseline_cluster(focused_points or baseline_pool)
    confidence = clamp(
        min(duration_seconds / 90.0, 1.0) * 32.0
        + min(len(baseline_pool) / 36.0, 1.0) * 24.0
        + normal_summary["accuracyScore"] * 0.24
        + normal_summary["fitScore"] * 0.12
        + normal_summary["motionScore"] * 0.08,
        0.0,
        100.0,
    )
    trend_history = [
        {
            "timestamp": point["capturedAt"],
            "focusIndex": point["focusIndex"],
            "qualityAnchor": point["qualityAnchor"],
        }
        for point in pool[-90:]
    ]

    if len(pool) < MIN_BASELINE_WINDOWS or duration_seconds < MIN_BASELINE_DURATION_SECONDS:
        return {
            **empty_baseline_metrics(),
            "available": False,
            "windowCount": len(pool),
            "durationSeconds": round(duration_seconds, 1),
            "confidenceScore": round(confidence, 1),
            "confidenceLabel": fit_label(confidence),
            "current": current,
            "history": trend_history,
            "note": (
                "Baseline learning is only using accepted clean windows. "
                "Keep the signal stable for about 45-60 seconds so normal and focused baselines mature."
            ),
        }

    return {
        "available": True,
        "windowCount": len(pool),
        "durationSeconds": round(duration_seconds, 1),
        "confidenceScore": round(confidence, 1),
        "confidenceLabel": fit_label(confidence),
        "current": current,
        "normal": normal_summary,
        "focused": focused_summary,
        "vsNormal": compare_to_baseline(current, normal_summary, baseline_name="normal"),
        "vsFocused": compare_to_baseline(current, focused_summary, baseline_name="focused"),
        "history": trend_history,
        "note": (
            f"Normal baseline uses {len(baseline_pool)} accepted clean windows; focused baseline keeps the most "
            f"alpha/beta-leaning {len(focused_points or baseline_pool)} clean windows from the last {round(duration_seconds)} seconds."
        ),
    }


def empty_baseline_metrics() -> dict[str, Any]:
    empty_state = {
        "dominantBand": "waiting",
        "bands": {name: 0.0 for name, _, _ in BAND_DEFS},
        "accuracyScore": 0.0,
        "fitScore": 0.0,
        "motionScore": 0.0,
        "continuityScore": 0.0,
        "qualityAnchor": 0.0,
        "focusIndex": 0.0,
        "calmIndex": 0.0,
    }
    return {
        "available": False,
        "windowCount": 0,
        "durationSeconds": 0.0,
        "confidenceScore": 0.0,
        "confidenceLabel": "waiting",
        "current": empty_state,
        "normal": empty_state,
        "focused": empty_state,
        "vsNormal": empty_baseline_comparison("normal"),
        "vsFocused": empty_baseline_comparison("focused"),
        "history": [],
        "note": "Waiting for enough stable EEG to build rolling baselines.",
    }


def summarize_baseline_cluster(points: list[dict[str, Any]]) -> dict[str, Any]:
    if not points:
        return empty_baseline_metrics()["normal"]

    bands = {
        name: round(fmean(point["bands"].get(name, 0.0) for point in points), 1)
        for name, _, _ in BAND_DEFS
    }
    dominant_band = max(bands, key=bands.get) if bands else "waiting"
    return {
        "dominantBand": dominant_band,
        "bands": bands,
        "accuracyScore": round(fmean(point.get("accuracyScore", 0.0) for point in points), 1),
        "fitScore": round(fmean(point.get("fitScore", 0.0) for point in points), 1),
        "motionScore": round(fmean(point.get("motionScore", 0.0) for point in points), 1),
        "continuityScore": round(fmean(point.get("continuityScore", 0.0) for point in points), 1),
        "qualityAnchor": round(fmean(point.get("qualityAnchor", 0.0) for point in points), 1),
        "focusIndex": round(fmean(point.get("focusIndex", 0.0) for point in points), 1),
        "calmIndex": round(fmean(point.get("calmIndex", 0.0) for point in points), 1),
    }


def compare_to_baseline(current: dict[str, Any], baseline: dict[str, Any], *, baseline_name: str) -> dict[str, Any]:
    if not current or not baseline or baseline.get("dominantBand") == "waiting":
        return empty_baseline_comparison(baseline_name)

    delta_shift = round(current["bands"].get("delta", 0.0) - baseline["bands"].get("delta", 0.0), 1)
    alpha_shift = round(current["bands"].get("alpha", 0.0) - baseline["bands"].get("alpha", 0.0), 1)
    beta_shift = round(current["bands"].get("beta", 0.0) - baseline["bands"].get("beta", 0.0), 1)
    focus_shift = round(current.get("focusIndex", 0.0) - baseline.get("focusIndex", 0.0), 1)

    if focus_shift >= 8.0:
        status = "more focused"
    elif focus_shift <= -8.0:
        status = "less focused"
    else:
        status = "close"

    if baseline_name == "focused":
        summary = (
            f"Current focus index is {abs(focus_shift):.0f} points {'above' if focus_shift >= 0 else 'below'} "
            f"your focused baseline, with delta {abs(delta_shift):.0f} points {'lower' if delta_shift <= 0 else 'higher'}."
        )
    else:
        summary = (
            f"Current activity is {status} than your normal baseline, with alpha {abs(alpha_shift):.0f} points "
            f"{'higher' if alpha_shift >= 0 else 'lower'} and beta {abs(beta_shift):.0f} points "
            f"{'higher' if beta_shift >= 0 else 'lower'}."
        )

    return {
        "status": status,
        "summary": summary,
        "focusShift": focus_shift,
        "alphaShift": alpha_shift,
        "betaShift": beta_shift,
        "deltaShift": delta_shift,
    }


def empty_baseline_comparison(baseline_name: str) -> dict[str, Any]:
    return {
        "status": "waiting",
        "summary": f"Waiting for a {baseline_name} baseline to compare against.",
        "focusShift": 0.0,
        "alphaShift": 0.0,
        "betaShift": 0.0,
        "deltaShift": 0.0,
    }


def build_calibration_guidance(
    *,
    fit_metrics: dict[str, Any],
    motion_metrics: dict[str, Any],
    signal_metrics: dict[str, Any],
    battery: BatteryState,
    telemetry_available: bool,
    baseline_metrics: dict[str, Any],
) -> dict[str, Any]:
    guidance = []
    score = 100.0

    if fit_metrics["overallScore"] < 60:
        score -= 28.0
        guidance.append("Re-seat the headset so TP9 and TP10 sit flat against the skin and move hair away from the sensors.")
    else:
        guidance.append("Estimated contact looks usable; keep the band pressure consistent while you record.")

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

    delta_status = signal_metrics["deltaDominance"]["status"]
    if delta_status in {"elevated", "strong"}:
        score -= 16.0 if delta_status == "elevated" else 24.0
        guidance.append(signal_metrics["deltaDominance"]["warning"])
    else:
        if signal_metrics.get("aggregate", {}).get("withheld"):
            guidance.append("The combined brain-state view is withheld until more clean sensors stay stable.")
        else:
            guidance.append("Overall band balance does not look artificially delta-heavy right now.")

    quality_metrics = signal_metrics["quality"]
    if quality_metrics["artifactScore"] < 60 or quality_metrics["agreementScore"] < 58:
        score -= 18.0
        guidance.append(quality_metrics["summary"])
    else:
        guidance.append("Cross-channel agreement looks stable enough for a more trustworthy combined band estimate.")

    if quality_metrics.get("lineNoiseScore", 100.0) < 60:
        score -= 10.0
        guidance.append("Line-noise rejection is weak right now; move farther from chargers, bright power bricks, and USB hubs.")

    if quality_metrics.get("stabilityScore", 100.0) < 60:
        score -= 10.0
        guidance.append("The EEG window is shifting within a second or two; hold still and give the headset another calm baseline window.")

    if baseline_metrics.get("available"):
        guidance.append(baseline_metrics["vsFocused"]["summary"])
    else:
        score -= 6.0
        guidance.append("Keep recording clean windows for another 45-60 seconds so the dashboard can build your normal and focused baselines.")

    if not telemetry_available:
        score -= 8.0
        guidance.append("Battery telemetry is not live yet, so charge status and temperature will stay blank until the Muse telemetry stream appears.")

    if battery.percent is not None and battery.percent <= 25:
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
        "deltaDominanceStatus": delta_status,
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


def compute_overall_band_mix(channels: list[dict[str, Any]]) -> dict[str, float]:
    admitted = [channel for channel in channels if channel.get("admitted", channel.get("usable", False))]
    if not admitted:
        return {name: 0.0 for name, _, _ in BAND_DEFS}
    total_weight = sum(max(channel.get("qualityWeight", 0.0), 0.0) for channel in admitted)
    if total_weight <= 0:
        return {name: 0.0 for name, _, _ in BAND_DEFS}
    return {
        name: round(
            sum(
                channel["mix"].get(name, 0.0)
                * max(channel.get("qualityWeight", 0.0), 0.0)
                for channel in admitted
            )
            / total_weight,
            1,
        )
        for name, _, _ in BAND_DEFS
    }


def compute_signal_quality(
    channels: list[dict[str, Any]],
    *,
    fit_metrics: dict[str, Any] | None,
    motion_metrics: dict[str, Any] | None,
    continuity: dict[str, Any],
) -> dict[str, Any]:
    if not channels:
        return {
            "artifactScore": 0.0,
            "agreementScore": 0.0,
            "driftScore": 0.0,
            "lineNoiseScore": 0.0,
            "contactScore": 0.0,
            "stabilityScore": 0.0,
            "motionScore": 0.0,
            "accuracyScore": 0.0,
            "usableChannelCount": 0,
            "label": "waiting",
            "summary": "Waiting for enough signal to score artifacts and channel agreement.",
            "blockers": ["Waiting for live EEG samples."],
        }

    artifact_score = round(fmean(channel.get("qualityWeight", 0.0) for channel in channels), 1)
    drift_score = round(
        fmean(max(0.0, 100.0 - (channel.get("driftRatio", 1.0) * 100.0)) for channel in channels),
        1,
    )
    line_noise_score = round(fmean(channel.get("lineNoiseScore", 0.0) for channel in channels), 1)
    contact_score = round(fmean(channel.get("fitScore", 0.0) for channel in channels), 1)
    stability_score = round(fmean(channel.get("splitHalfScore", 0.0) for channel in channels), 1)
    usable_channels = sum(1 for channel in channels if channel.get("usable", False))
    admitted_channels = sum(1 for channel in channels if channel.get("admitted", False))
    clean_window_coverage = round((admitted_channels / max(len(channels), 1)) * 100.0, 1)

    overall = compute_overall_band_mix([channel for channel in channels if channel.get("admitted", False)])
    divergences = []
    for channel in channels:
        divergence = sum(abs(channel["mix"].get(name, 0.0) - overall.get(name, 0.0)) for name, _, _ in BAND_DEFS)
        divergences.append(divergence)
    agreement_score = round(clamp(100.0 - (fmean(divergences) * 0.7), 0.0, 100.0), 1)
    motion_score = round(
        motion_metrics["stabilityScore"] if motion_metrics and motion_metrics.get("available") else 65.0,
        1,
    )
    continuity_score = continuity.get("score", 0.0)

    composite = (
        (artifact_score * 0.22)
        + (agreement_score * 0.17)
        + (drift_score * 0.12)
        + (line_noise_score * 0.14)
        + (contact_score * 0.17)
        + (stability_score * 0.12)
        + (motion_score * 0.1)
        + (continuity_score * 0.06)
    )
    blockers = summarize_quality_blockers(
        artifact_score=artifact_score,
        agreement_score=agreement_score,
        drift_score=drift_score,
        line_noise_score=line_noise_score,
        contact_score=contact_score,
        stability_score=stability_score,
        motion_score=motion_score,
        continuity_score=continuity_score,
        fit_metrics=fit_metrics,
        motion_metrics=motion_metrics,
    )
    summary = (
        f"{admitted_channels} of {len(channels)} channels are admitted into the aggregate view and {usable_channels} remain usable overall. Accuracy {composite:.0f}% with "
        f"artifact {artifact_score:.0f}%, agreement {agreement_score:.0f}%, line-noise rejection {line_noise_score:.0f}%, "
        f"and contact confidence {contact_score:.0f}%."
    )
    return {
        "artifactScore": artifact_score,
        "agreementScore": agreement_score,
        "driftScore": drift_score,
        "lineNoiseScore": line_noise_score,
        "contactScore": contact_score,
        "stabilityScore": stability_score,
        "motionScore": motion_score,
        "accuracyScore": round(clamp(composite, 0.0, 100.0), 1),
        "usableChannelCount": usable_channels,
        "admittedChannelCount": admitted_channels,
        "cleanWindowCoverage": clean_window_coverage,
        "minimumCombinedChannels": MIN_COMBINED_CLEAN_CHANNELS,
        "label": fit_label(composite),
        "summary": summary,
        "blockers": blockers,
    }


def assess_delta_dominance(
    overall_bands: dict[str, float],
    quality_metrics: dict[str, Any],
    aggregate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delta = overall_bands.get("delta", 0.0)
    alpha = overall_bands.get("alpha", 0.0)
    beta = overall_bands.get("beta", 0.0)
    theta = overall_bands.get("theta", 0.0)
    other_total = max(alpha + beta + theta + overall_bands.get("gamma", 0.0), 0.1)
    ratio = round(delta / other_total, 2)
    low_quality = (
        quality_metrics.get("artifactScore", 100.0) < 60
        or quality_metrics.get("agreementScore", 100.0) < 55
        or quality_metrics.get("accuracyScore", 100.0) < 58
        or quality_metrics.get("contactScore", 100.0) < 55
    )
    aggregate = aggregate or {}
    if aggregate.get("withheld"):
        return {
            "score": round(delta, 1),
            "ratio": ratio,
            "status": "waiting",
            "warning": "Delta judgment is withheld until enough clean sensors stay stable.",
            "alphaBetaSupport": round(alpha + beta, 1),
        }
    if aggregate.get("sourceMode") == "frontal-only":
        low_quality = low_quality or quality_metrics.get("admittedChannelCount", 0) < MIN_COMBINED_CLEAN_CHANNELS

    if delta >= 48 or ratio >= 0.72:
        status = "strong"
        if low_quality:
            warning = "Delta looks elevated, but the surrounding signal quality is too weak to trust it as a real awake-state conclusion."
        else:
            warning = "Delta is dominating the combined brain-wave view; this often means weak contact, motion, or a very drowsy baseline."
    elif delta >= 38 or ratio >= 0.5:
        status = "elevated"
        if low_quality:
            warning = "Delta stays elevated after drift suppression, but channel agreement is still weak; re-seat sensors and settle before trusting the reading."
        else:
            warning = "Delta is elevated versus the rest of the channels; re-check contact, posture, and stillness before trusting the reading."
    else:
        status = "balanced"
        warning = "Overall brain-wave balance looks more plausible across all sensors."

    return {
        "score": round(delta, 1),
        "ratio": ratio,
        "status": status,
        "warning": warning,
        "alphaBetaSupport": round(alpha + beta, 1),
    }


def build_brain_state(
    signal_metrics: dict[str, Any],
    fit_metrics: dict[str, Any],
    motion_metrics: dict[str, Any],
) -> dict[str, Any]:
    overall_bands = signal_metrics["overallBands"]
    dominant_band = signal_metrics["dominantBand"]
    delta_dominance = signal_metrics["deltaDominance"]
    quality_metrics = signal_metrics["quality"]
    aggregate = signal_metrics.get("aggregate") or select_aggregate_channels(signal_metrics.get("bands", []))
    plausibility = clamp(
        100.0
        - max(0.0, delta_dominance["score"] - 34.0) * 1.1
        - max(0.0, 60.0 - fit_metrics["overallScore"]) * 0.45
        - max(0.0, 65.0 - motion_metrics["stabilityScore"]) * 0.25,
        0.0,
        100.0,
    )
    plausibility = clamp(
        plausibility
        - max(0.0, 58.0 - quality_metrics["artifactScore"]) * 0.45
        - max(0.0, 55.0 - quality_metrics["agreementScore"]) * 0.35,
        0.0,
        100.0,
    )
    plausibility = clamp(
        plausibility
        - max(0.0, 60.0 - quality_metrics.get("lineNoiseScore", 100.0)) * 0.22
        - max(0.0, 60.0 - quality_metrics.get("contactScore", 100.0)) * 0.2
        - max(0.0, 60.0 - quality_metrics.get("stabilityScore", 100.0)) * 0.18,
        0.0,
        100.0,
    )
    if aggregate.get("sourceMode") == "frontal-only":
        plausibility = clamp(plausibility - 8.0, 0.0, 100.0)
    if aggregate.get("withheld"):
        plausibility = 0.0
    return {
        "overallBands": overall_bands,
        "dominantBand": dominant_band,
        "deltaDominance": delta_dominance,
        "quality": quality_metrics,
        "plausibilityScore": round(plausibility, 1),
        "plausibilityLabel": fit_label(plausibility),
        "withheld": bool(aggregate.get("withheld")),
        "withheldReasons": list(aggregate.get("reasons", [])),
        "sourceMode": aggregate.get("sourceMode", "withheld"),
        "sourceSensors": list(aggregate.get("sourceSensors", [])),
        "summary": aggregate.get("summary", "Waiting for enough clean signal."),
    }


def compute_band_mix(
    samples: list[dict[str, Any]],
    sample_rate_hz: int,
    *,
    fit_metrics: dict[str, Any] | None,
    motion_metrics: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    window = samples[-min(len(samples), max(sample_rate_hz * 2, 192)) :]
    fit_scores = {
        channel["channel"]: float(channel.get("score", 0.0))
        for channel in (fit_metrics or {}).get("channels", [])
    }
    has_fit_scores = bool(fit_scores)
    motion_penalty = 0.0
    if motion_metrics and motion_metrics.get("available"):
        motion_penalty = max(0.0, 100.0 - float(motion_metrics.get("stabilityScore", 100.0)))
    results = []
    for channel_index, channel_name in enumerate(CHANNELS):
        raw_series = [sample["values"][channel_index] for sample in window]
        sanitized = clip_outliers(raw_series)
        series = preprocess_band_series(sanitized, sample_rate_hz)
        raw_drift_ratio = estimate_drift_ratio(raw_series)
        drift_ratio = max(estimate_drift_ratio(series), raw_drift_ratio * 0.25)
        spike_ratio = estimate_spike_ratio(raw_series)
        flat_ratio = estimate_flat_ratio(raw_series)
        clip_ratio = estimate_clip_ratio(raw_series)
        rail_ratio = estimate_rail_ratio(raw_series)
        mix, grand_total = compute_band_distribution(series, sample_rate_hz)
        line_noise_ratio = estimate_line_noise_ratio(sanitized, sample_rate_hz)
        split_half_score = estimate_split_half_score(sanitized, sample_rate_hz)
        fit_score = round(fit_scores.get(channel_name, 50.0 if not has_fit_scores else 0.0), 1)
        line_noise_score = round(clamp(100.0 - (math.log10(1.0 + line_noise_ratio) * 34.0), 0.0, 100.0), 1)
        line_noise_penalty = max(0.0, 72.0 - line_noise_score) * 0.65
        delta_suspect = mix.get("delta", 0.0) >= 42.0 and (
            raw_drift_ratio >= 0.22 or clip_ratio >= 0.008 or rail_ratio >= 0.015
        )
        hard_reject_reasons = build_channel_reject_reasons(
            channel_name=channel_name,
            clip_ratio=clip_ratio,
            rail_ratio=rail_ratio,
            drift_ratio=raw_drift_ratio,
            flat_ratio=flat_ratio,
            split_half_score=split_half_score,
            fit_score=fit_score,
        )
        quality_weight = round(
            clamp(
                100.0
                - (drift_ratio * 34.0)
                - (spike_ratio * 118.0)
                - (flat_ratio * 55.0)
                - (clip_ratio * 2600.0)
                - (rail_ratio * 1200.0)
                - line_noise_penalty
                - max(0.0, 58.0 - fit_score) * 0.32
                - max(0.0, 62.0 - split_half_score) * 0.28
                - (16.0 if delta_suspect else 0.0)
                - (motion_penalty * 0.14),
                0.0,
                100.0,
            ),
            1,
        )
        admitted = not hard_reject_reasons and quality_weight >= 58.0
        results.append(
            {
                "channel": channel_name,
                "mix": mix,
                "bandPower": round(grand_total, 3),
                "qualityWeight": quality_weight,
                "driftRatio": round(drift_ratio, 3),
                "rawDriftRatio": round(raw_drift_ratio, 3),
                "spikeRatio": round(spike_ratio, 3),
                "flatRatio": round(flat_ratio, 3),
                "clipRatio": round(clip_ratio, 3),
                "railRatio": round(rail_ratio, 3),
                "lineNoiseRatio": round(line_noise_ratio, 3),
                "lineNoiseScore": line_noise_score,
                "splitHalfScore": round(split_half_score, 1),
                "fitScore": fit_score,
                "deltaSuspect": delta_suspect,
                "rejectReasons": hard_reject_reasons,
                "admitted": admitted,
                "usable": admitted and quality_weight >= 62.0,
            }
        )
    return results


def build_channel_reject_reasons(
    *,
    channel_name: str,
    clip_ratio: float,
    rail_ratio: float,
    drift_ratio: float,
    flat_ratio: float,
    split_half_score: float,
    fit_score: float,
) -> list[str]:
    reasons = []
    if clip_ratio >= 0.01 or rail_ratio >= 0.02:
        reasons.append(f"{channel_name} is clipping or railing too often.")
    if drift_ratio >= 0.75:
        reasons.append(f"{channel_name} has too much low-frequency drift.")
    if flat_ratio >= 0.22:
        reasons.append(f"{channel_name} is flattening too often to trust.")
    if split_half_score < 45.0:
        reasons.append(f"{channel_name} is not stable across the window.")
    if fit_score < 30.0:
        reasons.append(f"{channel_name} has very weak estimated contact.")
    return reasons


def select_aggregate_channels(channels: list[dict[str, Any]]) -> dict[str, Any]:
    admitted = [channel for channel in channels if channel.get("admitted", False)]
    front = [channel for channel in admitted if channel["channel"] in FRONT_CHANNELS]
    rear = [channel for channel in admitted if channel["channel"] in REAR_CHANNELS]
    rejected = [channel for channel in channels if not channel.get("admitted", False)]
    reasons = []

    clipping_rejected = [
        channel["channel"]
        for channel in rejected
        if channel.get("clipRatio", 0.0) >= 0.01 or channel.get("railRatio", 0.0) >= 0.02
    ]
    if clipping_rejected:
        reasons.append(f"Withheld due to clipping on {', '.join(clipping_rejected)}.")

    rear_rejected = [channel["channel"] for channel in rejected if channel["channel"] in REAR_CHANNELS]
    if len(rear_rejected) == len(REAR_CHANNELS):
        reasons.append("Withheld due to rear contact instability.")

    if len(admitted) >= MIN_COMBINED_CLEAN_CHANNELS:
        return {
            "withheld": False,
            "sourceMode": "combined",
            "selectedChannels": admitted,
            "sourceSensors": [channel["channel"] for channel in admitted],
            "cleanSensorCount": len(admitted),
            "reasons": reasons,
            "summary": f"Combined view uses {len(admitted)} admitted sensors.",
        }

    if len(front) == len(FRONT_CHANNELS) and len(rear) < len(REAR_CHANNELS):
        fallback_reasons = list(reasons)
        fallback_reasons.append("Rear contact instability triggered a frontal-only fallback.")
        return {
            "withheld": False,
            "sourceMode": "frontal-only",
            "selectedChannels": front,
            "sourceSensors": [channel["channel"] for channel in front],
            "cleanSensorCount": len(front),
            "reasons": fallback_reasons,
            "summary": "Frontal-only fallback is active because the rear sensors are not clean enough.",
        }

    reasons.append("Withheld due to low clean-window coverage.")
    return {
        "withheld": True,
        "sourceMode": "withheld",
        "selectedChannels": [],
        "sourceSensors": [],
        "cleanSensorCount": len(admitted),
        "reasons": reasons[:3],
        "summary": "Overall brain-state output is withheld until more clean sensors remain stable.",
    }


def summarize_quality_blockers(
    *,
    artifact_score: float,
    agreement_score: float,
    drift_score: float,
    line_noise_score: float,
    contact_score: float,
    stability_score: float,
    motion_score: float,
    continuity_score: float,
    fit_metrics: dict[str, Any] | None,
    motion_metrics: dict[str, Any] | None,
) -> list[str]:
    blockers = []
    if artifact_score < 60:
        blockers.append("Artifacts are still elevated after preprocessing.")
    if fit_metrics and fit_metrics.get("asymmetry", {}).get("status") == "rear-weaker":
        blockers.append("Rear sensors look weaker or noisier than the frontal pair.")
    if agreement_score < 58:
        blockers.append("Channels still disagree on the band balance.")
    if drift_score < 62:
        blockers.append("Slow baseline drift is still leaking into the EEG window.")
    if line_noise_score < 95:
        blockers.append("Line noise is leaking into the spectrum; move away from chargers and noisy cables.")
    if contact_score < 60:
        blockers.append("Sensor contact is too uneven to fully trust the combined brain-state view.")
    if stability_score < 60:
        blockers.append("The first and second halves of the EEG window do not agree yet.")
    if motion_metrics and motion_metrics.get("available") and motion_score < 60:
        blockers.append("Head motion is contaminating the reliability score.")
    if continuity_score < 65:
        blockers.append("Bluetooth timing jitter is reducing confidence in the window.")
    if not blockers and fit_metrics:
        blockers.append("The current window looks coherent across contact, timing, and spectrum checks.")
    return blockers[:4]


def clip_outliers(series: list[float]) -> list[float]:
    if len(series) < 6:
        return list(series)
    median = sorted(series)[len(series) // 2]
    deviations = sorted(abs(value - median) for value in series)
    mad = deviations[len(deviations) // 2]
    if mad <= 1e-6:
        return list(series)
    threshold = mad * 6.0
    return [clamp(value, median - threshold, median + threshold) for value in series]


def compute_band_distribution(series: list[float], sample_rate_hz: int) -> tuple[dict[str, float], float]:
    totals: dict[str, float] = {}
    grand_total = 0.0
    nyquist = max(int(sample_rate_hz // 2), 0)
    for name, start_hz, end_hz in BAND_DEFS:
        total = 0.0
        for frequency in range(start_hz, min(end_hz, nyquist + 1)):
            total += dft_power(series, sample_rate_hz, frequency)
        totals[name] = total
        grand_total += total
    mix = {
        name: round((totals[name] / grand_total) * 100.0, 1) if grand_total else 0.0
        for name, _, _ in BAND_DEFS
    }
    return mix, grand_total


def preprocess_band_series(series: list[float], sample_rate_hz: int) -> list[float]:
    if len(series) < 4:
        return list(series)
    return apply_hann_window(preprocess_band_series_without_window(series, sample_rate_hz))


def apply_hann_window(series: list[float]) -> list[float]:
    if len(series) < 2:
        return list(series)
    length = len(series)
    return [
        value * (0.5 - (0.5 * math.cos((2.0 * math.pi * index) / max(1, length - 1))))
        for index, value in enumerate(series)
    ]


def remove_line_components(series: list[float], sample_rate_hz: int) -> list[float]:
    if len(series) < 8 or sample_rate_hz <= 0:
        return list(series)
    cleaned = list(series)
    nyquist = sample_rate_hz / 2.0
    for frequency in (50.0, 60.0):
        if frequency >= nyquist - 0.5:
            continue
        sin_basis = []
        cos_basis = []
        for index in range(len(cleaned)):
            angle = (2.0 * math.pi * frequency * index) / sample_rate_hz
            sin_basis.append(math.sin(angle))
            cos_basis.append(math.cos(angle))
        sin_norm = max(sum(value * value for value in sin_basis), 1e-6)
        cos_norm = max(sum(value * value for value in cos_basis), 1e-6)
        sin_coeff = sum(value * basis for value, basis in zip(cleaned, sin_basis)) / sin_norm
        cos_coeff = sum(value * basis for value, basis in zip(cleaned, cos_basis)) / cos_norm
        cleaned = [
            value - (sin_coeff * sin_basis[index]) - (cos_coeff * cos_basis[index])
            for index, value in enumerate(cleaned)
        ]
    return cleaned


def estimate_line_noise_ratio(series: list[float], sample_rate_hz: int) -> float:
    if len(series) < 8 or sample_rate_hz <= 0:
        return 0.0
    filtered = preprocess_base_series(series, sample_rate_hz)
    mains_power = 0.0
    nyquist = sample_rate_hz / 2.0
    for frequency in (50, 60):
        if frequency >= nyquist - 0.5:
            continue
        mains_power += dft_power(filtered, sample_rate_hz, frequency)
    signal_power = 0.0
    max_frequency = min(int(nyquist), 45)
    for frequency in range(1, max_frequency + 1):
        signal_power += dft_power(filtered, sample_rate_hz, frequency)
    if signal_power <= 0:
        return 0.0
    return mains_power / signal_power


def preprocess_band_series_without_window(series: list[float], sample_rate_hz: int) -> list[float]:
    if len(series) < 4:
        return list(series)
    return remove_line_components(preprocess_base_series(series, sample_rate_hz), sample_rate_hz)


def preprocess_base_series(series: list[float], sample_rate_hz: int) -> list[float]:
    if len(series) < 4:
        return list(series)
    mean_value = fmean(series)
    centered = [value - mean_value for value in series]
    length = len(centered)
    start = centered[0]
    end = centered[-1]
    detrended = [
        value - (start + ((end - start) * (index / max(1, length - 1))))
        for index, value in enumerate(centered)
    ]
    dt = 1.0 / max(sample_rate_hz, 1)
    cutoff_hz = 0.9
    rc = 1.0 / max(2.0 * math.pi * cutoff_hz, 1e-6)
    alpha = clamp(rc / (rc + dt), 0.65, 0.995)
    filtered: list[float] = [detrended[0]]
    previous_input = detrended[0]
    previous_output = detrended[0]
    for value in detrended[1:]:
        output = alpha * (previous_output + value - previous_input)
        filtered.append(output)
        previous_input = value
        previous_output = output
    baseline_alpha = clamp(dt / (0.22 + dt), 0.02, 0.18)
    baseline = filtered[0]
    cleaned: list[float] = []
    for value in filtered:
        baseline += baseline_alpha * (value - baseline)
        cleaned.append(value - baseline)
    return cleaned


def estimate_split_half_score(series: list[float], sample_rate_hz: int) -> float:
    if len(series) < 12:
        return 0.0
    midpoint = len(series) // 2
    early = apply_hann_window(preprocess_band_series_without_window(series[:midpoint], sample_rate_hz))
    late = apply_hann_window(preprocess_band_series_without_window(series[midpoint:], sample_rate_hz))
    early_mix, _ = compute_band_distribution(early, sample_rate_hz)
    late_mix, _ = compute_band_distribution(late, sample_rate_hz)
    divergence = sum(abs(early_mix.get(name, 0.0) - late_mix.get(name, 0.0)) for name, _, _ in BAND_DEFS)
    return round(clamp(100.0 - (divergence * 0.9), 0.0, 100.0), 1)


def estimate_drift_ratio(series: list[float]) -> float:
    if len(series) < 8:
        return 1.0
    amplitude = max(max(series) - min(series), 1.0)
    quarter = max(len(series) // 4, 1)
    start_mean = fmean(series[:quarter])
    end_mean = fmean(series[-quarter:])
    return clamp(abs(end_mean - start_mean) / amplitude, 0.0, 2.0)


def estimate_clip_ratio(series: list[float]) -> float:
    if len(series) < 8:
        return 1.0
    return sum(1 for value in series if abs(value) >= 950.0) / len(series)


def estimate_rail_ratio(series: list[float]) -> float:
    if len(series) < 8:
        return 1.0
    return sum(1 for value in series if abs(value) >= 900.0) / len(series)


def estimate_spike_ratio(series: list[float]) -> float:
    if len(series) < 8:
        return 1.0
    deltas = [abs(series[index] - series[index - 1]) for index in range(1, len(series))]
    if not deltas:
        return 1.0
    mean_delta = max(fmean(deltas), 0.001)
    spikes = sum(1 for delta in deltas if delta > mean_delta * 3.2)
    return spikes / len(deltas)


def estimate_flat_ratio(series: list[float]) -> float:
    if len(series) < 8:
        return 1.0
    deltas = [abs(series[index] - series[index - 1]) for index in range(1, len(series))]
    if not deltas:
        return 1.0
    return sum(1 for delta in deltas if delta < 0.18) / len(deltas)


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


def build_connection_streams(
    *,
    sample_rate_hz: int,
    mode: str,
    telemetry_live: bool,
    telemetry: dict[str, Any],
    acc_samples: list[dict[str, Any]],
    gyro_samples: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    acc_rate = round(estimate_sample_rate(acc_samples), 1)
    gyro_rate = round(estimate_sample_rate(gyro_samples), 1)
    return [
        {
            "id": "eeg",
            "label": "EEG",
            "status": source_status(mode == "lsl", mode),
            "summary": (
                f"{len(CHANNELS)} channels at {sample_rate_hz} Hz"
                if mode == "lsl"
                else "Waiting for EEG samples from MuseLSL"
            ),
            "detail": "Primary brain-wave stream feeding the live viewer.",
        },
        {
            "id": "telemetry",
            "label": "Telemetry",
            "status": source_status(telemetry_live, mode),
            "summary": (
                f"Battery {format_optional(telemetry.get('batteryPercent'), '%')}, temp {format_optional(telemetry.get('temperatureC'), ' C')}"
                if telemetry_live
                else "Waiting for battery, temperature, and voltage metrics"
            ),
            "detail": "Fuel gauge, ADC voltage, and temperature from the Muse telemetry stream.",
        },
        {
            "id": "accelerometer",
            "label": "Accelerometer",
            "status": source_status(bool(acc_samples), mode),
            "summary": f"3 axes at {acc_rate:.1f} Hz" if acc_samples else "Waiting for accelerometer samples",
            "detail": "Raw X/Y/Z g-force data used for posture and motion stability.",
        },
        {
            "id": "gyroscope",
            "label": "Gyroscope",
            "status": source_status(bool(gyro_samples), mode),
            "summary": f"3 axes at {gyro_rate:.1f} Hz" if gyro_samples else "Waiting for gyroscope samples",
            "detail": "Raw X/Y/Z angular velocity data used for stillness and motion checks.",
        },
    ]


def source_status(is_available: bool, mode: str) -> str:
    if is_available and mode == "lsl":
        return "live"
    return "waiting"


def estimate_sample_rate(samples: list[dict[str, Any]]) -> float:
    if len(samples) < 2:
        return 0.0
    deltas = [samples[index]["timestamp"] - samples[index - 1]["timestamp"] for index in range(1, len(samples))]
    positive_deltas = [delta for delta in deltas if delta > 0]
    if not positive_deltas:
        return 0.0
    mean_delta = fmean(positive_deltas)
    if mean_delta <= 0:
        return 0.0
    return 1.0 / mean_delta


def format_optional(value: Any, suffix: str) -> str:
    if value is None:
        return "--"
    return f"{float(value):.1f}{suffix}"


def extract_telemetry_metrics(sample: Iterable[Any]) -> dict[str, float | None]:
    numeric_values = []
    for value in sample:
        try:
            numeric_values.append(float(value))
        except (TypeError, ValueError):
            continue

    battery_percent = extract_battery_percent(numeric_values)
    fuel_gauge = None
    if len(numeric_values) > 1:
        candidate = numeric_values[1]
        fuel_gauge = candidate * 100.0 if 0.0 < candidate <= 1.0 else candidate
        fuel_gauge = clamp(fuel_gauge, 0.0, 100.0)

    return {
        "batteryPercent": round(battery_percent, 1) if battery_percent is not None else None,
        "fuelGaugePercent": round(fuel_gauge, 1) if fuel_gauge is not None else None,
        "adcVolt": round(numeric_values[2], 3) if len(numeric_values) > 2 else None,
        "temperatureC": round(numeric_values[3], 2) if len(numeric_values) > 3 else None,
    }


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
    return "Waiting for a live MuseLSL stream."


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
