"""Publish one headset's EEG, telemetry and motion with shared device identity."""
from __future__ import annotations

import argparse
import logging
import math
import time


def telemetry_payload(battery, fuel_gauge, adc, temperature):
    # muse-lsl decodes battery as packet[1] / 512, already percentage points.
    # Preserve other decoded values as raw: they are not percentages or volts.
    values = [float(battery), float(fuel_gauge), float(adc), float(temperature)]
    if not all(math.isfinite(v) for v in values) or not 0 <= values[0] <= 100:
        raise ValueError("Invalid Muse telemetry values.")
    return values


def main():
    from pylsl import StreamInfo, StreamOutlet, local_clock
    from muselsl.constants import MUSE_SAMPLING_EEG_RATE, MUSE_SAMPLING_ACC_RATE, MUSE_SAMPLING_GYRO_RATE
    from muselsl.muse import Muse
    from muselsl.stream import find_muse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name")
    parser.add_argument("--address")
    parser.add_argument("--profile", choices=["muse-1", "muse-2"], default="muse-1")
    parser.add_argument("--backend", choices=["auto", "bleak", "gatt", "bgapi", "bluemuse"], default="bleak")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--idle-timeout", type=float, default=30)
    args = parser.parse_args()
    if args.idle_timeout <= 0 or args.retries < 0:
        parser.error("Timeout must be positive and retries must be nonnegative.")
    target = {"name": args.name or "Muse", "address": args.address}
    if not args.address:
        target = find_muse(args.name, args.backend)
        if not target:
            raise SystemExit("No Muse found. Power it on, close other Muse apps, and check Bluetooth permission.")
    address, name = target["address"], target["name"]
    model = "Muse 1" if args.profile == "muse-1" else "Muse 2"

    def outlet(kind, rate, labels, units):
        info = StreamInfo(f"{model} {kind}", kind, len(labels), rate, "float32", f"Muse{kind if kind != 'EEG' else ''}{address}")
        info.desc().append_child_value("manufacturer", "Muse")
        info.desc().append_child_value("device_id", address)
        channels = info.desc().append_child("channels")
        for label, unit in zip(labels, units):
            child = channels.append_child("channel")
            child.append_child_value("label", label)
            child.append_child_value("unit", unit)
            child.append_child_value("type", kind)
        return StreamOutlet(info)

    eeg = outlet("EEG", MUSE_SAMPLING_EEG_RATE, ["TP9", "AF7", "AF8", "TP10", "Right AUX"], ["microvolts"] * 5)
    telemetry = outlet("Telemetry", 0, ["battery_percent", "fuel_gauge_raw", "adc_raw", "temperature_raw"], ["percent", "raw", "raw", "raw"])
    acc = outlet("ACC", MUSE_SAMPLING_ACC_RATE, ["X", "Y", "Z"], ["g"] * 3)
    gyro = outlet("GYRO", MUSE_SAMPLING_GYRO_RATE, ["X", "Y", "Z"], ["dps"] * 3)
    last_eeg_received = time.monotonic()

    def push_eeg(data, timestamps):
        nonlocal last_eeg_received
        for index, timestamp in enumerate(timestamps):
            eeg.push_sample(data[:, index], float(timestamp))
        last_eeg_received = time.monotonic()

    def push_motion(destination):
        def callback(data, timestamps):
            for index, timestamp in enumerate(timestamps):
                destination.push_sample(data[:, index], float(timestamp))
        return callback

    def push_telemetry(timestamp, battery, fuel_gauge, adc_volt, temperature):
        try:
            telemetry.push_sample(telemetry_payload(battery, fuel_gauge, adc_volt, temperature), timestamp)
        except ValueError as exc:
            logging.warning("Telemetry rejected: %s", exc)

    muse = Muse(address=address, name=name, callback_eeg=push_eeg, callback_telemetry=push_telemetry,
                callback_acc=push_motion(acc), callback_gyro=push_motion(gyro), backend=args.backend,
                time_func=local_clock, log_level=logging.ERROR)
    connected = False
    try:
        connected = muse.connect(retries=args.retries)
        if not connected:
            raise SystemExit("Could not connect to the Muse headset.")
        last_eeg_received = time.monotonic()
        muse.start()
        print(f"Streaming {model}: EEG, telemetry, accelerometer and gyroscope. Ctrl+C to stop.")
        while time.monotonic() - last_eeg_received < args.idle_timeout:
            time.sleep(0.25)
        print("EEG idle timeout. Restart the launcher after checking the headset.")
    except KeyboardInterrupt:
        pass
    finally:
        if connected:
            try:
                muse.stop()
            finally:
                muse.disconnect()


if __name__ == "__main__":
    main()
