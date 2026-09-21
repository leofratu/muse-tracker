from __future__ import annotations

import io
import json
import math
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np
import pytest

import apps.backend.muse_lsl_bridge as bridge_module
from apps.backend.app import MuseDashboardServer
from apps.backend.muse_lsl_bridge import MuseLSLBridge, channel_layout, device_key
from apps.backend.recording import Recorder, csv_rows
from apps.backend.signal_processing import BANDS, analyze, integrate_band
from scripts.analyze_recording import export_analysis
from scripts.start_muse_stream import telemetry_payload


def samples(frequency=10.6, amplitude=20.0, rate=256, seconds=4, start=100.0):
    return [{"timestamp": start + i / rate,
             "values": [amplitude * math.sin(2 * math.pi * frequency * i / rate)] * 4}
            for i in range(int(rate * seconds))]


def feed(bridge, data=None):
    data = samples() if data is None else data
    bridge.ingest("eeg", [(s["timestamp"], s["values"]) for s in data])
    bridge.refresh()


@pytest.mark.parametrize("frequency,band", [(2.3,"delta"),(6.2,"theta"),(10.6,"alpha"),(18.7,"beta"),(37.3,"gamma")])
def test_off_integer_peaks_and_absolute_power(frequency, band):
    result = analyze(samples(frequency), 256)
    assert result["available"]
    assert result["relative"][band] > 98
    assert result["absolute"][band] == pytest.approx(200, rel=0.04)
    assert sum(result["relative"].values()) == pytest.approx(100)


def test_amplitude_scaling_is_quadratic_and_relative_power_invariant():
    a, b = analyze(samples(amplitude=10),256), analyze(samples(amplitude=20),256)
    assert b["absolute"]["alpha"] / a["absolute"]["alpha"] == pytest.approx(4)
    assert b["relative"]["alpha"] == pytest.approx(a["relative"]["alpha"])


def test_band_integrals_partition_boundaries_without_double_counting():
    f, p = np.arange(0,129,.5), np.ones(258)
    total = sum(integrate_band(f,p,lo,hi) for _,lo,hi in BANDS)
    assert total == pytest.approx(integrate_band(f,p,1,45)) == 44


def test_drift_suppression_preserves_alpha():
    data = samples()
    for i, sample in enumerate(data):
        sample["values"] = [v + 40*math.sin(2*math.pi*.25*i/256) for v in sample["values"]]
    result = analyze(data,256)
    assert result["relative"]["alpha"] > result["relative"]["delta"]


def test_line_noise_is_measured_before_notch_and_rejected():
    data = samples()
    for i, sample in enumerate(data):
        sample["values"] = [v + 35*math.sin(2*math.pi*50*i/256) for v in sample["values"]]
    result = analyze(data,256)
    assert not result["available"]
    assert result["channels"][0]["lineNoisePercent"] > 30
    assert any("mains" in s for s in result["channels"][0]["reasons"])


def test_clipped_rear_channels_fall_back_to_frontal_pair():
    data = samples()
    for i, sample in enumerate(data):
        sample["values"][0] = sample["values"][3] = 990 if i%2 else -990
    result = analyze(data,256)
    assert result["available"]
    assert result["sourceMode"] == "frontal-only"
    assert result["sourceSensors"] == ["AF7","AF8"]


def test_flat_channels_withhold_aggregate():
    result = analyze(samples(amplitude=0),256)
    assert not result["available"]
    assert all("Flat signal" in row["reasons"] for row in result["channels"])


@pytest.mark.parametrize("invalid", [float("nan"),float("inf"),-float("inf")])
def test_nonfinite_data_is_rejected(invalid):
    data = samples()
    data[50]["values"][0] = invalid
    assert analyze(data,256)["status"] == "invalid"


def test_gaps_are_not_treated_as_regular_samples():
    data = samples(seconds=5)
    del data[700]
    result = analyze(data,256)
    assert not result["available"]
    assert result["timing"]["gaps"] == 1


def test_short_window_and_unavailable_gamma_do_not_report_zero_as_measurement():
    assert analyze(samples(seconds=1),256)["status"] == "warming"
    result = analyze(samples(rate=64),64)
    assert not result["available"]
    assert result["channels"][0]["absolute"]["gamma"] is None
    assert all(v is None for v in result["channels"][0]["relative"].values())


def test_motion_gates_and_normalized_reliability():
    result = analyze(samples(),256)
    assert result["reliabilityIndex"] <= 100
    assert "accuracyScore" not in result
    moving = analyze(samples(),256,motion={"moving":True})
    assert not moving["available"] and moving["reliabilityIndex"] <= 25


def test_explicit_profile_survives_generic_identity_and_auto_stays_unknown(tmp_path):
    bridge = MuseLSLBridge(profile_key="muse-1",record_dir=tmp_path)
    bridge.configure_source("Muse","Museabc",256)
    bridge.refresh()
    assert bridge.snapshot()["device"]["label"] == "Muse 1"
    automatic = MuseLSLBridge(record_dir=tmp_path)
    automatic.configure_source("Muse","Museabc",256)
    automatic.refresh()
    assert automatic.snapshot()["device"]["profile"] == "auto"


def test_store_preserves_precision_rejects_invalid_and_does_not_shift_channels(tmp_path):
    bridge = MuseLSLBridge(record_dir=tmp_path)
    bridge.ingest("eeg", [(1.123456789,[1.23456789,2,3,4]), (2,[1,float("nan"),3,4]), (1,[4,3,2,1])])
    bridge.refresh()
    snapshot = bridge.snapshot()
    assert snapshot["eeg"]["samples"] == [{"timestamp":1.123456789,"values":[1.23456789,2.,3.,4.]}]
    assert snapshot["connection"]["invalidSamples"] == 2
    json.dumps(snapshot,allow_nan=False)


def test_snapshots_are_read_only_and_cannot_mutate_cached_analysis(tmp_path):
    bridge = MuseLSLBridge(record_dir=tmp_path)
    feed(bridge)
    one = bridge.snapshot()
    two = bridge.snapshot()
    assert one["history"] == two["history"]
    assert one["generatedAt"] == two["generatedAt"]
    one["analysis"]["channels"].clear()
    assert len(bridge.snapshot()["analysis"]["channels"]) == 4


def test_slow_analysis_does_not_hold_acquisition_lock(tmp_path,monkeypatch):
    bridge = MuseLSLBridge(record_dir=tmp_path)
    data = samples()
    bridge.ingest("eeg",[(s["timestamp"],s["values"]) for s in data])
    entered, release, ingested = threading.Event(), threading.Event(), threading.Event()
    original = bridge_module.analyze
    def slow(*args,**kwargs):
        entered.set()
        assert release.wait(2)
        return original(*args,**kwargs)
    monkeypatch.setattr(bridge_module,"analyze",slow)
    worker = threading.Thread(target=bridge.refresh)
    worker.start()
    try:
        assert entered.wait(1)
        def ingest():
            bridge.ingest("eeg",[(105.,[1,2,3,4])])
            ingested.set()
        acquisition = threading.Thread(target=ingest)
        acquisition.start()
        assert ingested.wait(.5), "DSP held the ingestion lock"
        acquisition.join(1)
    finally:
        release.set()
        worker.join(2)


def test_stale_streams_hide_analysis_motion_and_battery(tmp_path):
    bridge = MuseLSLBridge(record_dir=tmp_path)
    feed(bridge)
    bridge.ingest("telemetry",[(100,[83,1234,3400,30])])
    bridge.ingest("acc",[(100,[0,0,1])])
    bridge.refresh()
    assert bridge.snapshot()["telemetry"]["batteryPercent"] == 83
    with bridge._lock:
        for kind in bridge._received:
            bridge._received[kind] -= 60
    bridge.refresh()
    snapshot = bridge.snapshot()
    assert snapshot["connection"]["status"] == "stale"
    assert not snapshot["analysis"]["available"]
    assert snapshot["telemetry"]["batteryPercent"] is None
    assert snapshot["motion"]["accelerometer"] is None


def test_reference_requires_live_admitted_data_and_resets_on_reconnect(tmp_path):
    bridge = MuseLSLBridge(record_dir=tmp_path)
    with pytest.raises(ValueError):
        bridge.capture_reference("rest")
    bridge.configure_source("Muse","Musea",256)
    feed(bridge)
    bridge.capture_reference("eyes open")
    bridge.refresh()
    assert bridge.snapshot()["reference"]["available"]
    assert bridge.snapshot()["reference"]["relativeShift"]["alpha"] == 0
    bridge.configure_source("Muse","Musea",256)
    feed(bridge,samples(start=200))
    assert not bridge.snapshot()["reference"]["available"]


class FakeStream:
    def __init__(self,kind,identity="Musea"):
        self.kind,self.identity=kind,identity
    def type(self): return self.kind
    def name(self): return "Muse"
    def source_id(self): return self.identity
    def nominal_srate(self): return 256
    def channel_count(self): return 5 if self.kind=="EEG" else 4 if self.kind=="Telemetry" else 3


def test_auxiliary_device_matching_and_late_discovery(tmp_path,monkeypatch):
    available={"EEG":[FakeStream("EEG")],"ACC":[],"GYRO":[],"Telemetry":[]}
    opened=[]
    class Inlet:
        def __init__(self,stream,**kwargs): self.stream=stream;self.sent=False;opened.append(stream)
        def open_stream(self,**kwargs): pass
        def close_stream(self): pass
        def pull_chunk(self,**kwargs): return [],[]
        def pull_sample(self,**kwargs):
            if self.sent:return None,None
            self.sent=True
            return ([1,2,3,4,5] if self.stream.kind=="EEG" else [0,0,1]),100.
    monkeypatch.setattr(bridge_module,"StreamInlet",Inlet)
    monkeypatch.setattr(bridge_module,"resolve_byprop",lambda prop,value,**kw:available[value])
    bridge=MuseLSLBridge(profile_key="muse-1",record_dir=tmp_path)
    assert bridge._try_pull_lsl()
    assert len(opened)==1
    available["ACC"]=[FakeStream("ACC","MuseACCwrong"),FakeStream("ACC","MuseACCa")]
    bridge._last_resolve=-100
    bridge._try_pull_lsl()
    assert opened[-1].identity=="MuseACCa"
    bridge.refresh()
    assert bridge.snapshot()["motion"]["accelerometer"]==[0.,0.,1.]
    assert bridge.snapshot()["device"]["label"]=="Muse 1"
    bridge.stop()


def test_metadata_fallback_and_device_key():
    assert device_key(FakeStream("ACC","MuseACC123"))=="123"
    assert channel_layout(FakeStream("EEG"))==([0,1,2,3],[1.,1.,1.,1.])


def test_telemetry_preserves_percent_and_raw_fields():
    assert telemetry_payload(83,1122,3400,30)==[83.,1122.,3400.,30.]
    assert telemetry_payload(1,1122,3400,30)[0]==1
    with pytest.raises(ValueError): telemetry_payload(8300,0,0,0)


def test_recording_roundtrip_markers_metadata_csv_and_offline_analysis(tmp_path):
    bridge=MuseLSLBridge(record_dir=tmp_path)
    bridge.configure_source("Muse","Musea",256)
    feed(bridge)
    state=bridge.start_recording("test session")
    bridge.mark("Eyes open rest")
    data=samples(start=104)
    feed(bridge,data)
    final=bridge.recorder.stop()
    assert not final["error"] and final["samplesWritten"]==1024
    path=bridge.recorder.path_for(state["id"])
    records=[json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["metadata"]["settings"]["method"]=="Welch"
    assert any(e["type"]=="marker" for e in records)
    assert records[-1]["complete"] is True
    raw=next(e for e in records if e["type"]=="eeg")["samples"]
    assert raw==data
    assert len(b"".join(csv_rows(path)).decode().splitlines())==1025
    output=io.StringIO()
    export_analysis(path,output)
    assert len(output.getvalue().splitlines())==5
    assert bridge.recorder.sessions()[0]["complete"] is True


def test_queue_overflow_is_explicit_and_footer_incomplete(tmp_path,monkeypatch):
    recorder=Recorder(tmp_path,queue_size=1)
    release=threading.Event()
    original=recorder._write
    def paused(handle):
        assert release.wait(2)
        original(handle)
    monkeypatch.setattr(recorder,"_write",paused)
    result=recorder.start("queue test",{})
    event={"type":"eeg","samples":[{"timestamp":1,"values":[1,2,3,4]}],"receivedAt":"test","epoch":0}
    try:
        recorder.enqueue(event)
        recorder.enqueue(event)
        assert not recorder.status()["active"]
        assert recorder.status()["droppedSamples"]==1
    finally:
        release.set()
        recorder.stop()
    footer=json.loads(recorder.path_for(result["id"]).read_text().splitlines()[-1])
    assert footer["complete"] is False


def test_recording_disk_sync_error_remains_visible(tmp_path,monkeypatch):
    recorder=Recorder(tmp_path)
    recorder.start("write failure",{})
    def fail(fd): raise OSError("disk sync failed")
    monkeypatch.setattr("apps.backend.recording.os.fsync",fail)
    assert "failed" in recorder.stop()["error"]
    assert recorder.sessions()[0]["complete"] is False


@pytest.mark.parametrize("identifier", ["../private","../../etc/passwd","anything","20260921T170000Z-12345678/../../x"])
def test_export_paths_are_restricted(tmp_path,identifier):
    with pytest.raises(ValueError): Recorder(tmp_path).path_for(identifier)


@pytest.fixture
def http_server(tmp_path):
    bridge=MuseLSLBridge(record_dir=tmp_path)
    server=MuseDashboardServer(("127.0.0.1",0),bridge)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    yield server,f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_http_health_static_and_read_only_status(http_server):
    server,url=http_server
    with urlopen(url+"/") as response:
        assert b"Muse Classic" in response.read()
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    with urlopen(url+"/healthz?check=1") as response:
        assert json.load(response)["ok"]
    with urlopen(url+"/api/status") as response:
        status=json.load(response)
        assert status["schemaVersion"]==2 and status["controlToken"]==server.control_token
        assert status["analysis"]["status"]=="waiting"


@pytest.mark.parametrize("path",["/../frontend-other/secret","/%2e%2e/README.md","/recordings/","/apps/","//etc/passwd"])
def test_static_traversal_and_directory_reads_are_rejected(http_server,path):
    _,url=http_server
    with pytest.raises(HTTPError) as error: urlopen(url+path)
    assert error.value.code==404


def test_local_controls_require_host_token_origin_and_json_object(http_server):
    server,url=http_server
    def post(payload,headers):
        return urlopen(Request(url+"/api/record/start",data=payload,headers=headers,method="POST"))
    with pytest.raises(HTTPError) as error:post(b'{}',{"Content-Type":"application/json"})
    assert error.value.code==403
    headers={"Content-Type":"application/json","X-Muse-Token":server.control_token,"Origin":"https://attacker.invalid"}
    with pytest.raises(HTTPError) as error:post(b'{}',headers)
    assert error.value.code==403
    headers.pop("Origin")
    with pytest.raises(HTTPError) as error:post(b'[]',headers)
    assert error.value.code==400
    with pytest.raises(HTTPError) as error:post(b'{"label":"rest"}',{**headers,"Host":"attacker.invalid"})
    assert error.value.code==403


def test_http_recording_control_and_export(http_server):
    server,url=http_server
    feed(server.bridge)
    headers={"Content-Type":"application/json","X-Muse-Token":server.control_token}
    request=Request(url+"/api/record/start",data=b'{"label":"http test"}',headers=headers)
    with urlopen(request) as response: state=json.load(response)
    export=url+f'/api/sessions/{state["id"]}/export'
    with pytest.raises(HTTPError) as error:urlopen(export)
    assert error.value.code==409
    feed(server.bridge,samples(start=104))
    with urlopen(Request(url+"/api/record/stop",data=b'{}',headers=headers)) as response:
        assert json.load(response)["active"] is False
    with urlopen(export) as response: assert b'"complete": true' in response.read()


def test_server_rejects_non_loopback_binding(tmp_path):
    with pytest.raises(ValueError):
        MuseDashboardServer(("0.0.0.0",0),MuseLSLBridge(record_dir=tmp_path))


def test_real_http_sse_delivers_cached_snapshot(http_server):
    _, url = http_server
    with urlopen(url + "/api/stream", timeout=3) as response:
        assert response.headers["Content-Type"] == "text/event-stream"
        assert response.readline() == b"event: snapshot\n"
        payload = json.loads(response.readline().decode().removeprefix("data: "))
        assert payload["schemaVersion"] == 2
        assert payload["analysis"]["status"] == "waiting"


def test_malformed_csv_export_is_rejected_before_headers(http_server):
    server, url = http_server
    feed(server.bridge)
    state = server.bridge.start_recording("interrupted file")
    server.bridge.recorder.stop()
    path = server.bridge.recorder.path_for(state["id"])
    with path.open("a") as handle:
        handle.write('{"partial":')
    export = url + f'/api/sessions/{state["id"]}/export'
    with pytest.raises(HTTPError) as error:
        urlopen(export + "?format=csv")
    assert error.value.code == 400
    with urlopen(export) as response:
        assert response.read().endswith(b'{"partial":')
