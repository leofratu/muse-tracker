# Classic workstation upgrade

## Architecture and migration

This is an intentional API-v2 replacement of the former monolithic heuristic dashboard. It keeps the local HTTP/MuseLSL architecture and four core channels while replacing the numerical, recording and presentation contracts. The old focus/calm/accuracy/plausibility and automatically selected focused-baseline fields are removed, not silently relabelled as validated results.

```text
Muse / MuseLSL
       |
       v
Acquisition worker -- finite samples --> bounded raw-recording queue --> JSONL writer
       |
       v
Short buffer lock / copy
       |
       v
Analysis worker (every ~0.4 s, independent of HTTP)
       |
       v
Cached snapshot --> SSE clients / fallback HTTP --> charts
```

Acquisition stores raw values, source metadata and LSL timestamps. Channel labels determine ordering when supplied; supported V/mV units are converted to µV. If labels and units are absent, the documented fallback assumes standard Muse order and microvolts. Select `--source-id` for ambiguous environments. Auxiliary streams must match the selected device identity; a single available but unrelated motion stream is not attached by guesswork.

Recording uses a bounded nonblocking queue. Queue overflow stops the recording and marks it incomplete rather than silently dropping data while retaining a successful indicator. Disk errors surface through status. Stopping waits up to five seconds for the writer; a still-finishing file cannot be exported. A process crash can leave a file without a footer. Preserve that JSONL for recovery.

Every EEG chunk also carries its source metadata and epoch so a stream transition coinciding with recording start remains identifiable. Stream changes clear buffers/references. Invalid samples are counted and logged; they are not interpolated for analysis. Raw LSL clock values and UTC receipt times are different clocks, deliberately labelled separately.

## Numerical choices

Four-second windows, two-second Hann segments and 50% overlap provide three Welch segments per full window at integer nominal rates. The PSD uses explicit density scaling and interpolated integration endpoints, avoiding the old integer-frequency-only calculation. Boundary areas partition the 1–45 Hz total without double counting. Absolute values refer to the filtered signal, not an unfiltered physiological total.

Filtering is a second-order 0.5 Hz high-pass and an optional mains notch (Q=30), applied to each analysis window. Welch applies linear detrending. Window-edge effects and low-frequency attenuation remain relevant. Longer offline windows and alternative preprocessing should be investigated against recorded data, not described as proven accuracy improvements.

The diagnostic index is the arithmetic mean of clipping, flatness, timing and mains components, with a motion/large-amplitude cap. It is normalized to 0–100, **not calibrated to probability or accuracy**. Channels are separately excluded by explicit rules. Signal amplitude, clipping and motion thresholds are engineering defaults, not universal biological cutoffs. Missing motion is unknown, not verified stillness. Blinks, eye movements and facial EMG can pass these rules.

A mean over admitted sensors changes its spatial sampling when sensors change. The UI exposes the source set, and reference comparisons are blocked on a mismatch. Four-second references are descriptive only: there are no p-values, effect sizes, automated optimal states or claims about cognitive performance.

## Local API

GET: `/healthz`, `/api/status`, `/api/stream`, `/api/sessions`, `/api/sessions/{id}/export?format=jsonl|csv`.

POST: `/api/record/start`, `/api/record/stop`, `/api/marker`, `/api/reference`. Writes require JSON, an object body and the current `X-Muse-Token` from the snapshot. Labelled actions require a nonempty label of at most 120 characters. Cross-origin control requests and invalid Host headers are rejected. There is no remote-user authentication system; loopback-only binding is mandatory.

The server exposes only four allowlisted frontend assets as static files. Export IDs are validated, symlinks/path traversal rejected, and active/finishing recordings blocked from export. CSV is validated into a bounded-memory temporary spool before headers are sent. JSONL is the primary audit format; CSV omits markers and source events.

## Verification and remaining checks

Automated checks cover analytic/off-integer spectral fixtures, power scaling, band partitioning, drift/mains/clipping/flatline/motion/gap handling, unavailable bands, stale states, profile selection, auxiliary identity, late discovery, raw precision, recording round trips and failures, HTTP/SSE/security, transport fallback and UI controls/layout.

The implementation was exercised locally with Python 3.13, NumPy 2.3.5, SciPy 1.17.0, pytest 9.0.2, Node 22 and Chromium. Browser networking was administratively blocked in that environment; the local browser harness therefore used a Python HTTP bridge. Actual HTTP/SSE framing was tested through Python, and transport behavior through Node mocks. Native browser HTTP/SSE and Python 3.11 are configured in CI and must be checked there before treating them as passed.

Before merging/relying on hardware sessions:

- [ ] Verify connection on the actual Muse 1 generation/firmware and target operating system.
- [ ] Check battery percentage against the headset and inspect raw telemetry; do not assume ADC/temperature units.
- [ ] Record a sustained session, export it, and verify raw counts and any gap/rejection logs.
- [ ] Physically disconnect/reconnect EEG and auxiliary streams; verify stale warnings and reference reset.
- [ ] Review CI results and native browser controls/SSE on the target browser.

Deferred extensions: EDF/BIDS export, a recording replay player, longitudinal session statistics, longer/task-specific references, exact stimulus synchronization, physiologically validated artifact classifiers and external ECG/HRV imports. None are implied by this PR. These do not prevent local exploratory recording, but the app should not be represented as a validated nootropic-effect detector.
