from __future__ import annotations

import json
import threading
import time
from urllib.request import urlopen

from apps.backend.app import build_server
from apps.backend.muse_lsl_bridge import MuseLSLBridge, extract_battery_percent



def test_extract_battery_percent_accepts_common_ranges() -> None:
    assert extract_battery_percent([0.72, 4.1, 512]) == 72.0
    assert extract_battery_percent([83, 4.1, 512]) == 83.0
    assert extract_battery_percent([200, 512]) is None



def test_bridge_snapshot_contains_samples_and_battery() -> None:
    bridge = MuseLSLBridge(profile_key="muse-1", sample_rate_hz=32)
    bridge.start()
    try:
        time.sleep(0.35)
        snapshot = bridge.snapshot()
    finally:
        bridge.stop()

    assert snapshot["device"]["label"] == "Muse 1"
    assert snapshot["device"]["version"]["hardwareName"] == "Muse 1"
    assert snapshot["battery"]["percent"] > 0
    assert snapshot["battery"]["history"]
    assert snapshot["eeg"]["sampleCount"] > 0
    assert len(snapshot["eeg"]["samples"][-1]["values"]) == 4
    assert snapshot["sensorFit"]["channels"][0]["channel"] == "TP9"
    assert "confidenceScore" in snapshot["calibration"]
    assert "headPose" in snapshot["motion"]
    assert snapshot["eeg"]["metrics"]["moments"][0]["channel"] == "TP9"



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
    assert snapshot["connection"]["mode"] in {"demo", "lsl"}
    assert snapshot["eeg"]["sampleRateHz"] > 0
    assert "hardwareName" in snapshot["device"]["version"]
    assert "sensorFit" in snapshot
    assert "calibration" in snapshot
    assert "motion" in snapshot
