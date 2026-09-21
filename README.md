# Muse Classic

A local EEG workstation for a Muse headset and MuseLSL. Four-channel waveforms, Welch spectra, transparent signal checks, labelled references and raw recordings. Nothing is simulated in the production app: without a headset stream, it waits.

**This is exploratory software, not a medical device or a dopamine, acetylcholine, intelligence or focus meter.** The reliability index is an unvalidated diagnostic heuristic. Passing its checks does not prove that a signal is free of eye or muscle activity.

## Start with Muse 1

Use Python 3.11 or newer. The hardware launcher also needs Bluetooth permissions and a working native `liblsl` installation for your operating system.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-hardware.txt

# Terminal 1: replace the name with your headset's advertised name.
python scripts/start_muse_stream.py --profile muse-1 --name YOUR_MUSE_NAME

# Terminal 2, using the same environment:
python apps/backend/app.py --profile muse-1 --mains 50
```

Open `http://127.0.0.1:8000`. Use `--address YOUR_DEVICE_ADDRESS` instead of `--name` when appropriate. Close other apps using the headset. Actual Bluetooth compatibility depends on the headset generation, firmware and MuseLSL backend; choosing a profile does not change the hardware protocol.

Use `--profile muse-2` for Muse 2. The dashboard also supports `--profile auto`, which leaves unidentified streams as **Unknown Muse** rather than guessing Muse 2. Explicit profile choices are preserved. `--source-id EXACT_LSL_SOURCE_ID` selects one particular EEG source when multiple headsets are present.

The included launcher publishes EEG, accelerometer, gyroscope and telemetry streams with a shared device identity. The dashboard can use other MuseLSL sources, but absent auxiliary streams remain unavailable. Hardware telemetry other than battery is displayed as decoded **raw values**, not guessed percentages, volts or degrees Celsius.

## The workspace

The live workspace contains a four-second, four-channel waveform with a selectable vertical scale, a PSD plot, per-channel band powers, signal checks and a compact connection/recording strip. **Freeze charts** freezes only the display; acquisition and recording continue. Waiting, warming-up, stale, rejected and backend-offline states are separate.

Spectra use a four-second analysis window, a 0.5 Hz high-pass filter, optional 50/60 Hz notch, and Welch PSD with two-second Hann segments, 50% overlap and linear detrending. All available frequency bins are integrated with interpolated band boundaries. Absolute power is in µV²; relative power is normalized over 1–45 Hz. Missing frequency coverage is unavailable, not zero. Use `--mains 60` or `--mains 0` to change or disable the notch.

The aggregate is an equal-weight mean of at least three admitted sensors, or the AF7/AF8 pair when only the frontal pair qualifies. Individual rejected spectra remain visible for diagnosis. No cross-channel similarity or preferred band pattern is treated as proof of accuracy.

**Capture reference** stores the current admitted four-second spectrum with a label. Comparisons are descriptive percentage-point changes, not statistical significance or a learned focused state. Stream restarts clear the reference; different sensor sets cannot be compared. Task markers label activities performed separately. Selecting PVT or Stroop does not launch a cognitive test.

## Record and export

Press **Start recording**, optionally add markers, then **Stop & save**. The default directory is `recordings/`; change it with `--record-dir PATH`. Recording starts with new arriving samples, not the preceding chart buffer.

Each schema-v2 JSONL recording contains its metadata, raw four-channel EEG, available motion/telemetry, source epochs, task/reference markers, rejected-sample counts and a completion footer. Original accepted numeric samples and LSL timestamps are not rounded, clipped or filtered. Explicitly labelled volts/millivolts are converted to µV; the AUX channel is not recorded. Non-finite, malformed and non-increasing EEG samples are rejected and counted.

The archive offers **JSONL** and **CSV** exports after stopping. CSV contains raw EEG only; retain JSONL for markers and metadata. An interrupted/corrupt JSONL remains downloadable for recovery, while malformed CSV conversions are rejected before download begins. A finalized file means the writer finished successfully, not that every EEG window was clean or that Bluetooth lost no data. Queue overflow and write failures stop recording and are displayed explicitly.

Files stay on this computer and are ignored by Git. They are **not encrypted**. New recording files use restrictive local file permissions where supported. The server accepts loopback connections only and requires a per-process token for write actions; it is not intended for hosting on the internet or a shared LAN.

### Offline spectral analysis

```bash
python scripts/analyze_recording.py recordings/RECORDING_ID.jsonl --output analysis.csv
```

This reprocesses complete, non-overlapping four-second windows using the current algorithm and recorded mains setting. It is **signal-only QC**: live motion gating is not reconstructed, so admission need not match the live display. A trailing partial window is omitted. Existing output files are never overwritten. For reproducible historical results, retain the app commit and pinned numerical environment as well as the recording.

LSL timestamps are not Unix timestamps. Markers are timed at server receipt, with the latest EEG timestamp also stored. These markers are not suitable for laboratory-precision stimulus-onset or ERP experiments.

## Development and verification

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m compileall -q apps scripts
npm run check
npm test

# Browser integration and screenshots; requires downloading Chromium once.
python -m pip install -r requirements-browser.txt
python -m playwright install chromium
python scripts/smoke_browser.py
```

The tests use explicitly synthetic fixtures, never a production demo stream. CI checks Python 3.11/3.13, numerical and HTTP behavior, Node transport tests and native browser HTTP/SSE integration. The optional `--offline-browser` harness exercises UI controls through a Python HTTP bridge when browser networking is administratively unavailable; it does not validate native browser SSE. `CHROMIUM_EXECUTABLE` can select a locally installed browser.

Numerical and direct hardware dependencies are pinned separately. Hardware packages remain optional for tests and the waiting-state dashboard. This is not a complete cross-platform transitive lock; Bluetooth/native library installation must be checked on the target computer.

## Project layout

| File | Responsibility |
| --- | --- |
| `apps/backend/muse_lsl_bridge.py` | LSL selection, acquisition, freshness, independent cached analysis |
| `apps/backend/signal_processing.py` | Welch PSD, integrated bands and explicit QC heuristics |
| `apps/backend/recording.py` | Bounded asynchronous raw recording and exports |
| `apps/backend/app.py` | Loopback HTTP/SSE, control validation and static files |
| `apps/frontend/` | Responsive workstation and SSE-first transport |
| `scripts/start_muse_stream.py` | Headset-to-LSL launcher |
| `scripts/analyze_recording.py` | Offline recording-to-band-power CSV |

This upgrade deliberately introduces **API schema v2** and removes the previous accuracy, plausibility, calm and focus scores. Old scripts consuming the v1 snapshot need updating. See [architecture and limitations](docs/WORKSTATION_UPGRADE.md) and [working guide](docs/guides/WORKING_IN_THIS_REPO.md).

## Method references

- [SciPy Welch PSD documentation](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.welch.html)
- [MuseLSL project and supported hardware](https://github.com/alexandrebarachant/muse-lsl)
- [MuseLSL telemetry decoder](https://github.com/alexandrebarachant/muse-lsl/blob/master/muselsl/muse.py)
- [pylsl installation and native library requirements](https://pypi.org/project/pylsl/)

Real Muse 1 Bluetooth, telemetry scaling, lengthy recording and physical disconnect/reconnect checks remain necessary on the target headset. Automated fixtures do not establish clinical validity, electrode impedance accuracy or sensitivity to any particular substance.
