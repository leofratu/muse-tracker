from __future__ import annotations

import argparse
import logging
from time import sleep

from pylsl import StreamInfo, StreamOutlet, local_clock

from muselsl.constants import (
    LSL_ACC_CHUNK,
    LSL_EEG_CHUNK,
    LSL_GYRO_CHUNK,
    MUSE_NB_ACC_CHANNELS,
    MUSE_NB_EEG_CHANNELS,
    MUSE_NB_GYRO_CHANNELS,
    MUSE_SAMPLING_ACC_RATE,
    MUSE_SAMPLING_EEG_RATE,
    MUSE_SAMPLING_GYRO_RATE,
)
from muselsl.muse import Muse
from muselsl.stream import find_muse


def build_outlet(name: str, stream_type: str, channel_count: int, sample_rate: int, source_id: str, labels: list[str], unit: str, kind: str, chunk_size: int | None = None) -> StreamOutlet:
    info = StreamInfo(name, stream_type, channel_count, sample_rate, "float32", source_id)
    info.desc().append_child_value("manufacturer", "Muse")
    channels = info.desc().append_child("channels")
    for label in labels:
        channels.append_child("channel").append_child_value("label", label).append_child_value("unit", unit).append_child_value("type", kind)
    if chunk_size is None:
        return StreamOutlet(info)
    return StreamOutlet(info, chunk_size)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start a richer Muse-to-LSL bridge with EEG, telemetry, ACC, and GYRO.")
    parser.add_argument("--name", default=None, help="Muse device name, for example Muse-8410")
    parser.add_argument("--address", default=None, help="Muse BLE address")
    parser.add_argument("--backend", default="bleak", choices=["auto", "bleak", "gatt", "bgapi", "bluemuse"])
    parser.add_argument("--retries", type=int, default=2)
    args = parser.parse_args()

    target = {"name": args.name, "address": args.address}
    if not target["address"]:
        found = find_muse(args.name, args.backend)
        if not found:
            raise SystemExit("Could not find a Muse headset. Make sure it is turned on and nearby.")
        target = found

    address = target["address"]
    name = target["name"]

    eeg_outlet = build_outlet(
        name="Muse",
        stream_type="EEG",
        channel_count=MUSE_NB_EEG_CHANNELS,
        sample_rate=MUSE_SAMPLING_EEG_RATE,
        source_id=f"Muse{address}",
        labels=["TP9", "AF7", "AF8", "TP10", "Right AUX"],
        unit="microvolts",
        kind="EEG",
        chunk_size=LSL_EEG_CHUNK,
    )
    telemetry_outlet = build_outlet(
        name="MuseTelemetry",
        stream_type="Telemetry",
        channel_count=4,
        sample_rate=0,
        source_id=f"MuseTelemetry{address}",
        labels=["battery_percent", "fuel_gauge", "adc_volt", "temperature"],
        unit="n/a",
        kind="telemetry",
    )
    acc_outlet = build_outlet(
        name="MuseACC",
        stream_type="ACC",
        channel_count=MUSE_NB_ACC_CHANNELS,
        sample_rate=MUSE_SAMPLING_ACC_RATE,
        source_id=f"MuseACC{address}",
        labels=["X", "Y", "Z"],
        unit="g",
        kind="accelerometer",
        chunk_size=LSL_ACC_CHUNK,
    )
    gyro_outlet = build_outlet(
        name="MuseGYRO",
        stream_type="GYRO",
        channel_count=MUSE_NB_GYRO_CHANNELS,
        sample_rate=MUSE_SAMPLING_GYRO_RATE,
        source_id=f"MuseGYRO{address}",
        labels=["X", "Y", "Z"],
        unit="dps",
        kind="gyroscope",
        chunk_size=LSL_GYRO_CHUNK,
    )

    def push_eeg(data, timestamps):
        for index in range(data.shape[1]):
            eeg_outlet.push_sample(data[:, index], timestamps[index])

    def push_telemetry(timestamp, battery, fuel_gauge, adc_volt, temperature):
        telemetry_outlet.push_sample([battery * 100.0, fuel_gauge, adc_volt, temperature], timestamp)

    def push_acc(data, timestamps):
        for index in range(data.shape[1]):
            acc_outlet.push_sample(data[:, index], timestamps[index])

    def push_gyro(data, timestamps):
        for index in range(data.shape[1]):
            gyro_outlet.push_sample(data[:, index], timestamps[index])

    muse = Muse(
        address=address,
        name=name,
        callback_eeg=push_eeg,
        callback_telemetry=push_telemetry,
        callback_acc=push_acc,
        callback_gyro=push_gyro,
        backend=args.backend,
        time_func=local_clock,
        log_level=logging.ERROR,
    )

    if not muse.connect(retries=args.retries):
        raise SystemExit(f"Failed to connect to Muse headset at {address}.")

    print(f"Connected to {name} ({address})")
    muse.start()
    print("Streaming EEG + Telemetry + ACC + GYRO...")
    try:
        while local_clock() - muse.last_timestamp < 3600:
            sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        muse.stop()
        muse.disconnect()
        print("Disconnected.")


if __name__ == "__main__":
    main()
