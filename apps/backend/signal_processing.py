"""Transparent window-level EEG QC and Welch spectra. Not a cognitive classifier."""
from __future__ import annotations

import numpy as np
from scipy.integrate import trapezoid
from scipy.signal import butter, filtfilt, iirnotch, sosfiltfilt, welch

CHANNELS = ("TP9", "AF7", "AF8", "TP10")
BANDS = (("delta", 1, 4), ("theta", 4, 8), ("alpha", 8, 13),
         ("beta", 13, 30), ("gamma", 30, 45))
VERSION = "welch-2.0"
WINDOW_SECONDS = 4


def settings(rate: float, mains: int = 50) -> dict:
    return {"version": VERSION, "method": "Welch", "windowSeconds": WINDOW_SECONDS,
            "segmentSeconds": 2, "overlap": 0.5, "window": "hann", "detrend": "linear",
            "highpassHz": 0.5, "notchHz": mains or None, "notchQ": 30,
            "sampleRateHz": rate, "psdUnit": "uV^2/Hz", "powerUnit": "uV^2",
            "relativeRangeHz": [1, 45], "bands": [list(b) for b in BANDS]}


def integrate_band(f: np.ndarray, p: np.ndarray, low: float, high: float) -> float | None:
    """Integrate a piecewise-linear PSD, sharing boundaries without double counting."""
    if len(f) < 2 or low < f[0] or high > f[-1] or low >= high:
        return None
    inside = (f > low) & (f < high)
    x = np.r_[low, f[inside], high]
    y = np.r_[np.interp(low, f, p), p[inside], np.interp(high, f, p)]
    return max(0.0, float(trapezoid(y, x)))


def empty_analysis(status: str = "waiting", reason: str = "Waiting for live EEG.") -> dict:
    return {"status": status, "available": False, "reason": reason, "channels": [],
            "sourceSensors": [], "sourceMode": "withheld", "relative": {}, "absolute": {},
            "reliabilityIndex": None, "coverage": 0, "timing": {},
            "notice": "Signal checks are heuristic, not accuracy, impedance or neurotransmitter measurements."}


def analyze(samples: list[dict], rate: float, mains: int = 50,
            motion: dict | None = None) -> dict:
    if not np.isfinite(rate) or rate < 8 or rate > 2048:
        return empty_analysis("invalid", "Unsupported sample rate.")
    n = int(round(rate * WINDOW_SECONDS))
    if len(samples) < n:
        return empty_analysis("warming", f"Collecting a continuous {WINDOW_SECONDS}-second window.")
    window = samples[-n:]
    try:
        data = np.asarray([s["values"] for s in window], dtype=float)
        stamps = np.asarray([s["timestamp"] for s in window], dtype=float)
    except (KeyError, TypeError, ValueError):
        return empty_analysis("invalid", "Malformed EEG samples.")
    if data.shape != (n, 4) or not np.isfinite(data).all() or not np.isfinite(stamps).all():
        return empty_analysis("invalid", "Non-finite or malformed EEG samples.")
    intervals = np.diff(stamps)
    expected = 1.0 / rate
    gaps = int(np.sum(intervals > expected * 1.5))
    backwards = int(np.sum(intervals <= 0))
    jitter = float(np.median(np.abs(intervals - expected)) / expected)
    timing_ok = gaps == 0 and backwards == 0 and jitter <= 0.1
    timing = {"gaps": gaps, "nonIncreasing": backwards,
              "medianJitterPercent": round(jitter * 100, 3), "valid": timing_ok}
    rows = []
    for index, channel in enumerate(CHANNELS):
        raw = data[:, index]
        rms = float(np.std(raw))
        p2p = float(np.ptp(raw))
        clipping = float(np.mean(np.abs(raw) >= 950))
        flat = float(np.mean(np.abs(np.diff(raw)) < 0.01))
        reasons = []
        if not timing_ok:
            reasons.append("Sample gaps or irregular timing")
        if clipping >= 0.01:
            reasons.append("ADC clipping / railing")
        if rms < 0.1 or flat > 0.95:
            reasons.append("Flat signal")
        if p2p > 500:
            reasons.append("Large amplitude artifact")
        if motion and motion.get("moving"):
            reasons.append("Head movement")
        segment = int(round(rate * 2))
        frequencies, raw_psd = welch(raw, fs=rate, window="hann", nperseg=segment,
                                     noverlap=segment // 2, detrend="linear", scaling="density")
        total = integrate_band(frequencies, raw_psd, 1, min(45, rate / 2)) or 0.0
        line_power = integrate_band(frequencies, raw_psd, mains - 1, mains + 1) if mains else None
        line_fraction = line_power / max(total + line_power, 1e-12) if line_power is not None else None
        if line_fraction is not None and line_fraction > 0.3:
            reasons.append("Strong mains contamination")
        filtered = sosfiltfilt(butter(2, 0.5, btype="highpass", fs=rate, output="sos"), raw)
        if mains and mains + 1 < rate / 2:
            b, a = iirnotch(mains, 30, fs=rate)
            filtered = filtfilt(b, a, filtered)
        frequencies, psd = welch(filtered, fs=rate, window="hann", nperseg=segment,
                                 noverlap=segment // 2, detrend="linear", scaling="density")
        absolute = {name: integrate_band(frequencies, psd, lo, hi) for name, lo, hi in BANDS}
        full_range = all(v is not None for v in absolute.values())
        denominator = sum(v for v in absolute.values() if v is not None)
        relative = {name: (100 * value / denominator if full_range and denominator > 1e-12 else None)
                    for name, value in absolute.items()}
        # A normalized diagnostic index, with no agreement or cognitive-state assumption.
        components = [100 * (1 - min(clipping / 0.01, 1)),
                      0 if rms < 0.1 or flat > 0.95 else 100,
                      100 if timing_ok else 0,
                      max(0, 100 * (1 - (line_fraction or 0)))]
        reliability = float(np.mean(components))
        if p2p > 500 or (motion and motion.get("moving")):
            reliability = min(reliability, 25)
        admitted = not reasons and full_range and denominator > 1e-12
        if not full_range:
            reasons.append("Sample rate does not cover the full 1–45 Hz range")
        display = frequencies <= min(65, rate / 2)
        rows.append({"channel": channel, "admitted": admitted, "reasons": reasons,
                     "rmsUv": rms, "peakToPeakUv": p2p, "clippingPercent": clipping * 100,
                     "lineNoisePercent": line_fraction * 100 if line_fraction is not None else None,
                     "reliabilityIndex": reliability, "absolute": absolute, "relative": relative,
                     "frequencies": frequencies[display].tolist(), "psd": psd[display].tolist()})
    accepted = [r for r in rows if r["admitted"]]
    fronts = [r for r in accepted if r["channel"] in ("AF7", "AF8")]
    selected = accepted if len(accepted) >= 3 else fronts if len(fronts) == 2 else []
    mode = "combined" if len(selected) >= 3 else "frontal-only" if selected else "withheld"
    result = empty_analysis("ready" if selected else "withheld",
                            "Equal-weight average of admitted sensors." if selected
                            else "Aggregate withheld. Inspect individual channel checks.")
    result.update({"available": bool(selected), "channels": rows, "timing": timing,
                   "sourceSensors": [r["channel"] for r in selected], "sourceMode": mode,
                   "coverage": len(accepted),
                   "reliabilityIndex": float(np.mean([r["reliabilityIndex"] for r in rows]))})
    if selected:
        for key in ("absolute", "relative"):
            result[key] = {name: float(np.mean([r[key][name] for r in selected])) for name, _, _ in BANDS}
    return result
