# Muse Tracker

Muse Tracker is a local live-only MuseLSL dashboard for Muse 1 and Muse 2 headsets. It focuses on real EEG streaming, telemetry, sensor-contact estimation, motion-aware calibration, and deeper signal-reliability scoring.

If no real MuseLSL stream is available, the dashboard stays in a waiting state instead of showing synthetic demo data.

## Features

- Live EEG viewer for the four Muse channels: `TP9`, `AF7`, `AF8`, `TP10`
- Muse family/version inference from stream identity and telemetry availability
- Battery + telemetry surfaces when the Muse stream exposes `Telemetry`
- Sensor-fit and skin-contact estimates derived from rolling EEG behavior
- Head tilt and motion tracking from `ACC` and `GYRO` streams
- Accuracy-oriented reliability stack with:
  - artifact control
  - channel agreement
  - drift suppression
  - line-noise rejection
  - contact confidence
  - split-window stability
  - motion stability
  - continuity-aware overall accuracy scoring
- Calibration guidance that reacts to contact quality, motion, continuity, delta dominance, battery state, and line noise
- Time graphs for battery/fit trends and band drift
- Data-source inventory for `EEG`, `Telemetry`, `ACC`, and `GYRO`

## Repository layout

- `apps/backend/app.py` - local HTTP server, SSE stream, and static asset serving
- `apps/backend/muse_lsl_bridge.py` - MuseLSL bridge, signal processing, telemetry handling, fit estimation, motion analysis, and calibration logic
- `apps/backend/tests/test_app.py` - backend regression tests and snapshot validation
- `apps/frontend/index.html` - dashboard layout
- `apps/frontend/app.js` - live rendering, chart drawing, and accuracy surfaces
- `apps/frontend/styles.css` - frontend styling
- `scripts/start_muse_stream.py` - direct Muse-to-LSL launcher with EEG, telemetry, accelerometer, and gyroscope streams
- `repo_plan/` - RPG planning artifacts and per-run logs

## Requirements

- Python 3.10+
- Node.js (used for frontend syntax checks)
- A Muse headset powered on and paired through your local MuseLSL workflow
- `muselsl` and `pylsl` in your Python environment

## Quick start

### 1) Create a Python environment

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install muselsl pylsl
```

### 2) Start the Muse stream launcher

For the richest dashboard data, start the direct launcher so `EEG`, `Telemetry`, `ACC`, and `GYRO` are all exposed:

```bash
.venv/bin/python scripts/start_muse_stream.py --name Muse-8410
```

You can also use:

```bash
.venv/bin/python scripts/start_muse_stream.py --address YOUR_DEVICE_ADDRESS
```

### 3) Start the dashboard server

```bash
PYTHONPATH=. .venv/bin/python apps/backend/app.py --host 127.0.0.1 --port 8000 --profile muse-2
```

Then open:

```text
http://127.0.0.1:8000
```

## Live data model

The dashboard uses whatever real LSL streams are available from the headset:

- `EEG` - required for the wave viewer and brain-state analysis
- `Telemetry` - battery percent, fuel gauge, ADC voltage, and temperature
- `ACC` - posture and motion stability
- `GYRO` - stillness and angular-velocity checks

If a stream is missing, that panel stays in a waiting state instead of inventing values.

## Accuracy model

The backend tries to make the rolling band view more trustworthy by combining multiple heuristics rather than trusting raw band power directly.

### Signal preprocessing

- outlier clipping before spectral analysis
- slow-drift suppression to reduce false delta inflation
- mains component removal for common 50/60 Hz contamination
- Hann windowing before per-band DFT accumulation

### Reliability scoring

Per-channel and aggregate trust are influenced by:

- drift leakage
- spike activity
- flatline behavior
- line-noise ratio
- split-half spectral stability
- estimated contact quality
- motion contamination from accelerometer/gyroscope stability
- stream continuity / timing jitter

### Why this helps

This does not make the app clinically validated, but it does make it much harder for obvious bad windows to look deceptively trustworthy.

## Calibration guidance

Calibration confidence blends:

- sensor fit quality
- motion stability
- continuity score
- delta-dominance warnings
- telemetry availability
- battery state
- line-noise rejection
- within-window stability

The goal is to flag windows that are internally inconsistent before you trust the overall brain-state surface.

## Verification

```bash
python3 -m py_compile apps/backend/app.py apps/backend/muse_lsl_bridge.py apps/backend/tests/test_app.py
node --check apps/frontend/app.js
python3 -m pytest -q
python3 scripts/tools/rpg_builder.py --write --rpg-mode minimal --dep-depth 1 --include-tests
python3 scripts/validate_rpg.py
```

## Development notes

- The dashboard is intentionally local-first and simple to run.
- The sensor-fit metric is estimated from EEG behavior; MuseLSL does not provide a dedicated official fit stream in this setup.
- Motion/orientation outputs are guidance signals, not precise 3D head-tracking.
- The accuracy stack is heuristic and intended for practical session quality control, not medical interpretation.

## Troubleshooting

- If the dashboard stays in a waiting state, confirm your MuseLSL workflow is actually publishing an `EEG` stream.
- If telemetry stays blank, your source likely is not publishing `Telemetry`.
- If the accuracy score is poor, first check contact, then stillness, then nearby power noise and Bluetooth congestion.
- If delta remains unusually strong, let the headset settle, reseat the sensors, and avoid motion before trusting the combined band view.
