# Working in this repo

## Run

Python 3.11+. Install `requirements-hardware.txt` in a virtual environment. Start the headset and dashboard in separate terminals:

```bash
python scripts/start_muse_stream.py --profile muse-1 --name YOUR_MUSE_NAME
python apps/backend/app.py --profile muse-1 --mains 50
```

Open `http://127.0.0.1:8000`. The production dashboard has no demo mode. Without valid live EEG, derived values remain unavailable. Hardware libraries are optional for development; install `requirements-dev.txt` to run the waiting-state dashboard and tests without a headset.

## Implementation rules

- Keep acquisition locks short. Do not perform DSP, disk writes or HTTP serialization while holding the acquisition lock.
- `snapshot()` is a cached, read-only operation. Analysis and bounded histories advance on the processing worker, not per browser request.
- Preserve accepted raw samples/timestamps in recording. Mark malformed or dropped data explicitly; do not replace it with invented samples.
- Keep stream identity, sampling rate and source epochs with data. Do not compare references across device restarts or changed sensor sets.
- Call reliability/contact outputs heuristics. Do not reintroduce medical accuracy percentages, focus scores or neurotransmitter estimates.
- Never commit recordings, browser scratch files, secrets or local installation artifacts. The historical `repo_plan/` logs describe the previous implementation, not current validation.

## Verify

```bash
python -m pytest -q
python -m compileall -q apps scripts
npm run check
npm test
```

For browser checks, install `requirements-browser.txt`, run `python -m playwright install chromium`, then `python scripts/smoke_browser.py`. The fixture is deliberately synthetic and clearly labelled. Native browser SSE is not tested by `--offline-browser`.

On actual Muse hardware, check contact, jaw/eye artifacts, a short raw recording, auxiliary telemetry, and disconnect/reconnect before using session comparisons. Signal checks are not electrode impedance measurements. Low battery may interrupt recording; this app does not infer a quantitative drift effect from battery percentage.
