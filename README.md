# Muse Tracker

Muse Tracker is a local MuseLSL dashboard for Muse headsets with live EEG viewing, battery tracking, sensor-fit estimation, head-position tracking, and calibration guidance.

## What it does

- Streams and visualizes live EEG for the four Muse forehead/ear channels.
- Tracks live battery telemetry when available.
- Estimates electrode fit quality from the rolling EEG window.
- Tracks head tilt and motion stability from ACC/GYRO streams when present.
- Produces calibration confidence and a preparation guide for cleaner sessions.
- Falls back to a polished demo feed when live LSL data is not available.

## Repo layout

- `apps/backend/app.py` - local server, SSE stream, static serving.
- `apps/backend/muse_lsl_bridge.py` - MuseLSL bridge, fit/motion/calibration logic.
- `apps/frontend/index.html` - dashboard structure.
- `apps/frontend/app.js` - live rendering and chart drawing.
- `apps/frontend/styles.css` - dashboard visual design.
- `apps/backend/tests/test_app.py` - backend verification.
- `scripts/start_muse_stream.py` - optional direct Muse-to-LSL launcher with EEG, telemetry, ACC, and GYRO.

## Quick start

### 1) Create the Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install muselsl pylsl
```

### 2) Start the direct Muse stream launcher

If you want battery + motion-aware calibration in the dashboard, start the richer streamer first:

```bash
.venv/bin/python scripts/start_muse_stream.py --name Muse-8410
```

You can also pass `--address YOUR_DEVICE_ADDRESS`.

### 3) Start the dashboard

```bash
PYTHONPATH=. .venv/bin/python apps/backend/app.py --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

## Dashboard features

### Version insight

The dashboard surfaces the detected Muse family and the evidence used to infer it from the live stream identity.

### Sensor fit

The fit score is estimated from EEG spread, peak-to-peak range, and flatline behavior in the rolling signal window.

### Head position tracking

When `ACC` and `GYRO` streams are available, the app estimates pitch, roll, and motion stability. This is best treated as orientation guidance rather than precise 3D tracking.

### Calibration coach

Calibration confidence blends:

- sensor fit quality
- motion stability
- signal continuity
- battery state
- live telemetry availability

## Accuracy tips

- Move hair away from TP9/TP10 and keep forehead contact clean.
- Let the headset settle for about a minute before trusting the baseline.
- Stay still for 20-30 seconds during calibration.
- Calibrate away from strong Bluetooth clutter and charging cables when possible.
- Recharge the headset when battery gets low to avoid unstable sessions.

## Verification

```bash
python3 -m py_compile apps/backend/app.py apps/backend/muse_lsl_bridge.py apps/backend/tests/test_app.py scripts/start_muse_stream.py scripts/tools/rpg_builder.py scripts/validate_rpg.py
node --check apps/frontend/app.js
python3 -m pytest
python3 scripts/tools/rpg_builder.py --write --rpg-mode minimal --dep-depth 1 --include-tests
python3 scripts/validate_rpg.py
```

## Notes

- The fit metric is an estimate; MuseLSL does not provide a first-class "fit" stream in this setup.
- The app is designed to stay usable in demo mode when no live headset is available.
- For best results, use `scripts/start_muse_stream.py` instead of a plain EEG-only MuseLSL stream so telemetry and motion streams are present.

