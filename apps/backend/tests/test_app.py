from __future__ import annotations

import json
import math
import threading
import time
from urllib.request import urlopen

import apps.backend.muse_lsl_bridge as muse_lsl_bridge
from apps.backend.app import build_server
from apps.backend.muse_lsl_bridge import (
    MuseLSLBridge,
    build_brain_state,
    build_baseline_metrics,
    build_fit_metrics,
    build_motion_metrics,
    build_signal_metrics,
    extract_battery_percent,
)


def test_extract_battery_percent_accepts_common_ranges() -> None:
    assert extract_battery_percent([0.72, 4.1, 512]) == 72.0
    assert extract_battery_percent([83, 4.1, 512]) == 83.0
    assert extract_battery_percent([200, 512]) is None


def test_bridge_snapshot_stays_empty_until_live_data_arrives() -> None:
    bridge = MuseLSLBridge(profile_key="muse-1", sample_rate_hz=32)
    snapshot = bridge.snapshot()

    assert snapshot["device"]["label"] == "Muse 1"
    assert snapshot["device"]["version"]["hardwareName"] == "Muse 1"
    assert snapshot["battery"]["percent"] is None
    assert snapshot["battery"]["history"] == []
    assert snapshot["telemetry"]["available"] is False
    assert snapshot["telemetry"]["history"] == []
    assert snapshot["eeg"]["sampleCount"] == 0
    assert snapshot["eeg"]["samples"] == []
    assert snapshot["connection"]["mode"] == "waiting"
    assert snapshot["connection"]["connected"] is False
    assert snapshot["connection"]["streams"][0]["status"] == "waiting"


def test_bridge_snapshot_contains_live_samples_and_telemetry() -> None:
    bridge = MuseLSLBridge(profile_key="muse-1", sample_rate_hz=32)
    now = time.time()
    samples = [
        (now + (index / 32.0), [12.0 + index, 10.0 + index, 8.0 + index, 6.0 + index])
        for index in range(32)
    ]
    acc_samples = [
        (now + (index * 0.02), [0.02 * index, 0.01 * index, 0.98])
        for index in range(12)
    ]
    gyro_samples = [
        (now + (index * 0.02), [0.2 * index, 0.1 * index, 0.05 * index])
        for index in range(12)
    ]
    bridge.store.add_samples(samples)
    bridge.store.add_motion_samples("acc", acc_samples)
    bridge.store.add_motion_samples("gyro", gyro_samples)
    bridge.store.update_telemetry([83.0, 0.79, 3.97, 30.8], source="lsl-telemetry")
    bridge.store.set_connection(
        connected=True,
        stream_mode="lsl",
        stream_name="Muse 1",
        stream_source_id="test-source",
        telemetry_available=True,
        motion_available=True,
        last_error=None,
        profile_key="muse-1",
        sample_rate_hz=32,
    )

    snapshot = bridge.snapshot()

    assert snapshot["battery"]["percent"] == 83.0
    assert snapshot["battery"]["history"]
    assert snapshot["eeg"]["sampleCount"] > 0
    assert len(snapshot["eeg"]["samples"][-1]["values"]) == 4
    assert snapshot["sensorFit"]["channels"][0]["channel"] == "TP9"
    assert snapshot["sensorFit"]["officialView"]["shape"] == "horseshoe"
    assert snapshot["sensorFit"]["officialView"]["sensors"][0]["status"] in {
        "excellent",
        "good",
        "fair",
        "poor",
        "waiting",
    }
    assert snapshot["sensorFit"]["channels"][0]["history"]
    assert "score" in snapshot["sensorFit"]["channels"][0]["history"][-1]
    assert "telemetry" in snapshot
    assert "temperatureC" in snapshot["telemetry"]
    assert snapshot["telemetry"]["available"] is True
    assert snapshot["connection"]["streams"][0]["id"] == "eeg"
    assert "confidenceScore" in snapshot["calibration"]
    assert "headPose" in snapshot["motion"]
    assert snapshot["motion"]["sensors"]["accelerometer"]["history"]
    assert snapshot["eeg"]["metrics"]["moments"][0]["channel"] == "TP9"
    assert snapshot["eeg"]["metrics"]["quality"]["label"] in {
        "excellent",
        "good",
        "fair",
        "poor",
        "waiting",
    }
    assert 0 <= snapshot["eeg"]["metrics"]["quality"]["artifactScore"] <= 100
    assert 0 <= snapshot["eeg"]["metrics"]["quality"]["lineNoiseScore"] <= 100
    assert 0 <= snapshot["eeg"]["metrics"]["quality"]["contactScore"] <= 100
    assert 0 <= snapshot["eeg"]["metrics"]["quality"]["stabilityScore"] <= 100
    assert 0 <= snapshot["eeg"]["metrics"]["quality"]["accuracyScore"] <= 100
    assert snapshot["eeg"]["metrics"]["quality"]["blockers"]
    assert set(snapshot["eeg"]["metrics"]["overallBands"]) == {
        "delta",
        "theta",
        "alpha",
        "beta",
        "gamma",
    }
    assert snapshot["eeg"]["metrics"]["deltaDominance"]["status"] in {
        "balanced",
        "elevated",
        "strong",
        "waiting",
    }
    assert snapshot["brainState"]["dominantBand"] in {
        "delta",
        "theta",
        "alpha",
        "beta",
        "gamma",
        "waiting",
    }
    assert 0 <= snapshot["brainState"]["plausibilityScore"] <= 100
    assert "baseline" in snapshot
    assert "vsNormal" in snapshot["baseline"]
    assert "vsFocused" in snapshot["baseline"]


def test_signal_metrics_reduce_slow_drift_bias() -> None:
    sample_rate = 64
    samples = []
    for index in range(sample_rate * 3):
        t = index / sample_rate
        alpha_wave = 18.0 * math.sin(2.0 * math.pi * 10.0 * t)
        slow_drift = 40.0 * math.sin(2.0 * math.pi * 0.28 * t)
        value = alpha_wave + slow_drift
        samples.append(
            {
                "timestamp": t,
                "values": [value, value * 0.96, value * 1.04, value * 0.92],
            }
        )

    metrics = build_signal_metrics(samples, sample_rate)

    assert metrics["overallBands"]["alpha"] > metrics["overallBands"]["delta"]
    assert metrics["quality"]["usableChannelCount"] >= 2
    assert metrics["deltaDominance"]["status"] in {"balanced", "elevated"}
    assert metrics["quality"]["driftScore"] >= 55
    assert metrics["quality"]["accuracyScore"] >= 50


def test_signal_metrics_penalize_line_noise_contamination() -> None:
    sample_rate = 256
    clean_samples = []
    noisy_samples = []
    for index in range(sample_rate * 2):
        t = index / sample_rate
        alpha_wave = 20.0 * math.sin(2.0 * math.pi * 10.0 * t)
        mains_noise = 14.0 * math.sin(2.0 * math.pi * 50.0 * t)
        clean_samples.append(
            {
                "timestamp": t,
                "values": [alpha_wave, alpha_wave * 0.98, alpha_wave * 1.02, alpha_wave * 0.95],
            }
        )
        contaminated = alpha_wave + mains_noise
        noisy_samples.append(
            {
                "timestamp": t,
                "values": [contaminated, contaminated * 0.98, contaminated * 1.02, contaminated * 0.95],
            }
        )

    clean_metrics = build_signal_metrics(clean_samples, sample_rate)
    noisy_metrics = build_signal_metrics(noisy_samples, sample_rate)

    assert noisy_metrics["quality"]["lineNoiseScore"] < clean_metrics["quality"]["lineNoiseScore"]
    assert noisy_metrics["quality"]["accuracyScore"] <= clean_metrics["quality"]["accuracyScore"]
    assert "line noise" in " ".join(noisy_metrics["quality"]["blockers"]).lower()


def test_build_baseline_metrics_compares_current_state_to_normal_and_focused_windows() -> None:
    history = []
    for index in range(60):
        timestamp = 100.0 + index
        beta = 18.0 + (index % 4)
        alpha = 24.0 + ((index + 1) % 3)
        delta = 18.0 - min(index * 0.1, 4.0)
        theta = 20.0 - min(index * 0.05, 2.0)
        gamma = max(0.0, 100.0 - (beta + alpha + delta + theta))
        history.append(
            {
                "timestamp": timestamp,
                "capturedAt": f"2026-04-21T00:00:{index:02d}+00:00",
                "bands": {
                    "delta": round(delta, 1),
                    "theta": round(theta, 1),
                    "alpha": round(alpha, 1),
                    "beta": round(beta, 1),
                    "gamma": round(gamma, 1),
                },
                "dominantBand": "alpha",
                "accuracyScore": 68.0 + (index % 5),
                "fitScore": 60.0 + (index % 4),
                "motionScore": 78.0,
                "continuityScore": 93.0,
                "qualityAnchor": 72.0 + (index % 6),
                "focusIndex": 55.0 + (index * 0.35),
                "calmIndex": 58.0,
                "acceptedForBaseline": True,
                "eligibleNormal": True,
            }
        )

    current = {
        "timestamp": 161.0,
        "capturedAt": "2026-04-21T00:02:41+00:00",
        "bands": {
            "delta": 11.0,
            "theta": 18.0,
            "alpha": 26.0,
            "beta": 31.0,
            "gamma": 14.0,
        },
        "dominantBand": "beta",
        "accuracyScore": 81.0,
        "fitScore": 67.0,
        "motionScore": 84.0,
        "continuityScore": 97.0,
        "qualityAnchor": 83.0,
        "focusIndex": 74.0,
        "calmIndex": 52.0,
        "acceptedForBaseline": True,
        "eligibleNormal": True,
    }

    baseline = build_baseline_metrics(history=history, current_point=current)

    assert baseline["available"] is True
    assert baseline["windowCount"] >= 30
    assert baseline["durationSeconds"] >= 29.0
    assert baseline["normal"]["dominantBand"] in {"alpha", "beta"}
    assert baseline["focused"]["focusIndex"] >= baseline["normal"]["focusIndex"]
    assert baseline["vsNormal"]["status"] in {"more focused", "close", "less focused"}
    assert baseline["vsFocused"]["summary"]
    assert baseline["history"]


def test_brain_state_falls_back_to_frontal_pair_when_rear_channels_are_rail_contaminated() -> None:
    sample_rate = 256
    samples = []
    for index in range(sample_rate * 4):
        t = index / sample_rate
        frontal = (18.0 * math.sin(2.0 * math.pi * 10.0 * t)) + (7.0 * math.sin(2.0 * math.pi * 18.0 * t))
        rear_noise = 980.0 if index % 40 == 0 else ((index % 2) * 180.0 - 90.0)
        samples.append(
            {
                "timestamp": t,
                "values": [rear_noise, frontal * 0.98, frontal * 1.02, -rear_noise],
            }
        )

    fit_metrics = build_fit_metrics(samples, sample_rate)
    motion_metrics = build_motion_metrics([], [])
    metrics = build_signal_metrics(samples, sample_rate, fit_metrics=fit_metrics, motion_metrics=motion_metrics)
    brain_state = build_brain_state(metrics, fit_metrics, motion_metrics)

    assert brain_state["withheld"] is False
    assert brain_state["sourceMode"] == "frontal-only"
    assert brain_state["sourceSensors"] == ["AF7", "AF8"]
    assert brain_state["overallBands"]["alpha"] > brain_state["overallBands"]["delta"]
    rejected = {channel["channel"]: channel["rejectReasons"] for channel in metrics["bands"] if not channel["admitted"]}
    assert "TP9" in rejected
    assert any("clipping" in reason.lower() or "railing" in reason.lower() for reason in rejected["TP9"])


def test_brain_state_is_withheld_when_clean_sensor_coverage_is_too_low() -> None:
    sample_rate = 256
    samples = []
    for index in range(sample_rate * 4):
        t = index / sample_rate
        clean = 16.0 * math.sin(2.0 * math.pi * 10.0 * t)
        clipped = 999.51 if index % 18 == 0 else -999.51 if index % 19 == 0 else 0.0
        flat = 0.0
        samples.append(
            {
                "timestamp": t,
                "values": [clipped, clean, clipped, flat],
            }
        )

    fit_metrics = build_fit_metrics(samples, sample_rate)
    motion_metrics = build_motion_metrics([], [])
    metrics = build_signal_metrics(samples, sample_rate, fit_metrics=fit_metrics, motion_metrics=motion_metrics)
    brain_state = build_brain_state(metrics, fit_metrics, motion_metrics)

    assert brain_state["withheld"] is True
    assert brain_state["sourceMode"] == "withheld"
    assert any("low clean-window coverage" in reason.lower() for reason in brain_state["withheldReasons"])


def test_bridge_falls_back_to_pull_sample_when_chunks_are_empty(monkeypatch) -> None:
    class FakeStream:
        def __init__(self, stream_type: str, name: str, source_id: str, rate: float, channels: int) -> None:
            self._type = stream_type
            self._name = name
            self._source_id = source_id
            self._rate = rate
            self._channels = channels

        def type(self) -> str:
            return self._type

        def name(self) -> str:
            return self._name

        def source_id(self) -> str:
            return self._source_id

        def nominal_srate(self) -> float:
            return self._rate

        def channel_count(self) -> int:
            return self._channels

    class FakeInlet:
        def __init__(self, chunk=None, timestamps=None, samples=None) -> None:
            self._chunk = chunk or []
            self._timestamps = timestamps or []
            self._samples = list(samples or [])
            self.opened = False

        def open_stream(self, timeout: float | None = None) -> None:
            self.opened = True

        def pull_chunk(self, timeout: float = 0.0, max_samples: int = 1):
            chunk, timestamps = self._chunk, self._timestamps
            self._chunk, self._timestamps = [], []
            return chunk, timestamps

        def pull_sample(self, timeout: float = 0.0):
            if self._samples:
                return self._samples.pop(0)
            return None, None

    streams_by_type = {
        "EEG": [FakeStream("EEG", "Muse", "Muse123", 256.0, 5)],
        "ACC": [FakeStream("ACC", "Muse ACC", "Muse123", 52.0, 3)],
        "GYRO": [FakeStream("GYRO", "Muse GYRO", "Muse123", 52.0, 3)],
    }
    inlets = {
        "EEG": FakeInlet(samples=[([1.0, 2.0, 3.0, 4.0, 9.0], 100.0)]),
        "ACC": FakeInlet(chunk=[[0.1, 0.2, 0.98]], timestamps=[100.01]),
        "GYRO": FakeInlet(chunk=[[0.3, 0.2, 0.1]], timestamps=[100.02]),
    }

    def fake_resolve_byprop(prop: str, value: str, timeout: float = 0.0):
        assert prop == "type"
        return streams_by_type.get(value, [])

    def fake_stream_inlet(stream, max_buflen=None):
        return inlets[stream.type()]

    monkeypatch.setattr(muse_lsl_bridge, "resolve_byprop", fake_resolve_byprop)
    monkeypatch.setattr(muse_lsl_bridge, "StreamInlet", fake_stream_inlet)

    bridge = MuseLSLBridge(profile_key="muse-2", sample_rate_hz=256)
    assert bridge._try_pull_lsl() is True

    snapshot = bridge.snapshot()

    assert snapshot["connection"]["connected"] is True
    assert snapshot["eeg"]["sampleCount"] == 1
    assert snapshot["eeg"]["samples"][-1]["values"] == [1.0, 2.0, 3.0, 4.0]
    assert snapshot["motion"]["available"] is True
    assert snapshot["motion"]["sensors"]["accelerometer"]["history"]
    assert snapshot["motion"]["sensors"]["gyroscope"]["history"]
    assert inlets["EEG"].opened is True


def test_http_server_exposes_health_and_status() -> None:
    server = build_server(host="127.0.0.1", port=0, profile_key="muse-2")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        time.sleep(0.25)
        host, port = server.server_address
        with urlopen(f"http://{host}:{port}/") as response:
            shell = response.read().decode("utf-8")
        with urlopen(f"http://{host}:{port}/healthz") as response:
            health = json.loads(response.read().decode("utf-8"))
        with urlopen(f"http://{host}:{port}/api/status") as response:
            snapshot = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.bridge.stop()
        server.server_close()
        thread.join(timeout=1.0)

    assert "MuseLSL Studio" in shell
    assert health["ok"] is True
    assert snapshot["device"]["label"] == "Muse 2"
    assert snapshot["connection"]["mode"] in {"waiting", "lsl"}
    assert snapshot["eeg"]["sampleRateHz"] > 0
    assert "hardwareName" in snapshot["device"]["version"]
    assert "sensorFit" in snapshot
    assert "calibration" in snapshot
    assert "motion" in snapshot
    assert "brainState" in snapshot
    assert "officialView" in snapshot["sensorFit"]
    assert "streams" in snapshot["connection"]
    assert "telemetry" in snapshot
