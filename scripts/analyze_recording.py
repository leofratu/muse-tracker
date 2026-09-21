"""Reprocess non-overlapping 4-second windows from a v2 JSONL recording to CSV."""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apps.backend.signal_processing import BANDS, analyze


def export_analysis(recording: Path, output):
    writer = csv.writer(output)
    writer.writerow(["epoch", "window_start_lsl", "window_end_lsl", "channel", "admitted", "reasons",
                     *[f"{b[0]}_uV2" for b in BANDS], *[f"{b[0]}_percent" for b in BANDS]])
    source, epoch, mains, buffer = None, None, 50, []
    with recording.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Malformed JSON at line {number}; preserve the original before recovery.") from exc
            if event.get("type") == "header":
                if event.get("schemaVersion") != 2:
                    raise ValueError("Only schemaVersion 2 recordings are supported.")
                meta = event["metadata"]
                source, epoch = meta["source"], meta["epoch"]
                mains = meta["settings"].get("notchHz") or 0
            elif event.get("type") == "stream":
                source, epoch, buffer = event["source"], event["epoch"], []
            elif event.get("type") == "eeg":
                if event.get("source") and (source != event["source"] or epoch != event["epoch"]):
                    source, epoch, buffer = event["source"], event["epoch"], []
                if source is None or epoch != event["epoch"]:
                    raise ValueError("EEG epoch has no matching source metadata.")
                rate = source["sampleRateHz"]
                buffer.extend(event["samples"])
                count = int(round(rate * 4))
                while len(buffer) >= count:
                    window, buffer = buffer[:count], buffer[count:]
                    # Offline signal-only QC: do not pretend motion was reconstructed.
                    result = analyze(window, rate, mains)
                    for row in result["channels"]:
                        writer.writerow([epoch, window[0]["timestamp"], window[-1]["timestamp"], row["channel"],
                                         row["admitted"], "; ".join(row["reasons"]),
                                         *[row["absolute"][b[0]] for b in BANDS], *[row["relative"][b[0]] for b in BANDS]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        if args.output:
            # Exclusive creation protects the input and existing analysis files.
            with args.output.open("x", newline="", encoding="utf-8") as output:
                export_analysis(args.recording, output)
        else:
            export_analysis(args.recording, sys.stdout)
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"Analysis failed: {exc}\n")


if __name__ == "__main__":
    main()
