# Working In This Repo

## Run the Muse dashboard
- `python3 apps/backend/app.py`
- Open `http://127.0.0.1:8000`

## Recommended live stream launcher
- Create a virtualenv and install `muselsl` plus `pylsl`.
- Start the richer bridge first with:
  - `.venv/bin/python scripts/start_muse_stream.py --name Muse-8410`
- This gives the dashboard EEG, telemetry, accelerometer, and gyroscope streams so battery, head-position tracking, and calibration guidance all work together.

## MuseLSL live mode
- Install `pylsl` in your Python environment.
- Start your MuseLSL EEG stream first.
- Launch the dashboard server; it will automatically switch from the demo feed to the live LSL feed when available.

## Calibration and accuracy notes
- Move hair away from TP9 and TP10 before trusting fit scores.
- Give the headset about one minute to settle after you put it on.
- Keep your head still for 20-30 seconds when establishing a baseline.
- Use the dashboard's fit, motion, and continuity panels together; a clean reading usually needs all three to look healthy.
- If battery is low, expect more drift and reconnects during longer runs.

## Test
- `python3 -m pytest`
