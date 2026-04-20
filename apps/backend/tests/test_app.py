from __future__ import annotations

import json
import math
import threading
import time
from urllib.request import urlopen

from apps.backend.app import build_server
from apps.backend.muse_lsl_bridge import MuseLSLBridge, build_signal_metrics, extract_battery_percent


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
