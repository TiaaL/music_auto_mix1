#!/usr/bin/env python3
"""Extract reference-track features used to drive per-song mix parameters.

Inputs:
  - reference full mix (used for LUFS, 8-band tonal balance, dynamics)
  - reference vocal stem
  - reference accompaniment (the same stem we mix with)

Output JSON keys:
  - loudness:         { lufs_i, true_peak_db, lra }
  - tonal_balance:    { sub..air dB per band, normalised so mid=0 }
  - dynamics:         { crest_db, dr_db }
  - vocal_accomp_balance: { vocal_lufs, accomp_lufs, vocal_minus_accomp_db }
  - reverb_proxy:     { tail_to_onset_ratio_db, est_rt60_ms, confidence, ... }
  - delay_proxy:      { peak_corr, peak_lag_ms, confidence }
  - vocal_stem_quality: active/inactive vocal-stem energy gap used as a leakage guard
  - sources:          paths actually used
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_ROOT = ROOT / "downloads" / "feishu_long_audio_screened"
REFERENCE_ROOT_CANDIDATES = (
    DOWNLOADS_ROOT,
    ROOT.parent / "feishu_long_audio_screened",
    ROOT.parent.parent / "feishu_long_audio_screened",
)

BANDS = [
    ("sub", 20.0, 80.0),
    ("low", 80.0, 180.0),
    ("lowmid", 180.0, 500.0),
    ("mid", 500.0, 1000.0),
    ("upper", 1000.0, 4000.0),
    ("harsh", 4000.0, 8000.0),
    ("sib", 8000.0, 12000.0),
    ("air", 12000.0, 20000.0),
]

SPECTRAL_ENVELOPE_BANDS = [
    # 比 8-band tonal_balance 更细的“音色轮廓”采样点。
    # 只用于人声活动区，并归一到中频人声主体，避免响度差被误当成音色差。
    ("env_120", 90.0, 160.0, 120.0),
    ("env_200", 160.0, 260.0, 200.0),
    ("env_320", 260.0, 420.0, 320.0),
    ("env_500", 420.0, 650.0, 500.0),
    ("env_750", 650.0, 950.0, 750.0),
    ("env_1100", 950.0, 1400.0, 1100.0),
    ("env_1600", 1400.0, 2100.0, 1600.0),
    ("env_2400", 2100.0, 3200.0, 2400.0),
    ("env_3600", 3200.0, 4800.0, 3600.0),
    ("env_5400", 4800.0, 7200.0, 5400.0),
    ("env_8000", 7200.0, 10500.0, 8000.0),
    ("env_12000", 10500.0, 15500.0, 12000.0),
]


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        ROOT / ".tools" / "msys64" / "ucrt64" / "bin" / f"{name}.exe",
        ROOT / ".tools" / "msys64" / "usr" / "bin" / f"{name}.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return name


FFMPEG = command_path("ffmpeg")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def db(value: float, floor: float = -120.0) -> float:
    if value <= 0 or not math.isfinite(value):
        return floor
    return 20.0 * math.log10(value)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_audio_as_float(path: Path, target_sr: int = 48000) -> tuple[np.ndarray, int]:
    """Decode any format through ffmpeg into a float32 numpy array at target_sr."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ar",
            str(target_sr),
            "-ac",
            "2",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg decode failed for {path}: {proc.stderr.decode('utf-8', errors='replace')[-500:]}"
        )
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    if raw.size == 0:
        raise RuntimeError(f"empty audio after decode: {path}")
    data = raw.reshape(-1, 2).astype(np.float64)
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    return data, target_sr


def to_mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        return data
    return data.mean(axis=1)


def measure_loudness(path: Path) -> dict[str, float]:
    """Run ffmpeg loudnorm in measurement mode to get LUFS-I, true peak, LRA."""
    proc = run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-23.0:TP=-2.0:LRA=11.0:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg loudnorm failed for {path}: {proc.stderr[-500:]}")
    match = re.search(r"\{[\s\S]*?\}", proc.stderr)
    if not match:
        raise RuntimeError(f"loudnorm JSON not found in output for {path}")
    raw = json.loads(match.group(0))
    return {
        "lufs_i": float(raw["input_i"]),
        "true_peak_db": float(raw["input_tp"]),
        "lra": float(raw["input_lra"]),
    }


def tonal_balance(data: np.ndarray, sr: int) -> dict[str, float]:
    """8-band energy profile in dB, normalised so the 'mid' band = 0 dB."""
    profile = band_profile(data, sr, normalize_mid=True)
    return {name: round(value, 3) for name, value in profile.items()}


def band_profile(data: np.ndarray, sr: int, normalize_mid: bool) -> dict[str, float]:
    """8-band FFT magnitude profile. Optionally normalise so 'mid' = 0 dB."""
    x = to_mono(data)
    max_samples = min(x.size, sr * 90)
    x = x[:max_samples]
    if x.size < 16:
        return {name: 0.0 for name, _, _ in BANDS}
    x = x - float(np.mean(x))
    window = np.hanning(x.size)
    spectrum = np.abs(np.fft.rfft(x * window)) + 1e-12
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    profile: dict[str, float] = {}
    for name, low, high in BANDS:
        mask = (freqs >= low) & (freqs < min(high, sr / 2.0))
        profile[name] = db(float(np.mean(spectrum[mask]))) if np.any(mask) else -120.0
    if normalize_mid:
        mid_ref = profile.get("mid", 0.0)
        return {name: value - mid_ref for name, value in profile.items()}
    return profile


def spectral_envelope_profile(data: np.ndarray, sr: int) -> dict[str, Any]:
    """细分频谱包络；用于比 8-band 更可听的音色相似度优化。"""
    x = to_mono(data)
    max_samples = min(x.size, sr * 90)
    x = x[:max_samples]
    if x.size < 16:
        return {
            "version": 1,
            "basis": "active_vocal_regions_mid_normalized_fft_envelope",
            "bands": [],
        }
    x = x - float(np.mean(x))
    window = np.hanning(x.size)
    spectrum = np.abs(np.fft.rfft(x * window)) + 1e-12
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    raw: list[dict[str, float | str]] = []
    for name, low, high, center in SPECTRAL_ENVELOPE_BANDS:
        upper = min(high, sr / 2.0)
        if upper <= low:
            continue
        mask = (freqs >= low) & (freqs < upper)
        if not np.any(mask):
            continue
        raw.append({
            "id": name,
            "freq_hz": round(center, 1),
            "db_raw": db(float(np.mean(spectrum[mask]))),
        })
    if not raw:
        return {
            "version": 1,
            "basis": "active_vocal_regions_mid_normalized_fft_envelope",
            "bands": [],
        }
    mid_values = [
        float(item["db_raw"])
        for item in raw
        if 500.0 <= float(item["freq_hz"]) <= 2400.0
    ]
    ref = float(np.median(mid_values)) if mid_values else float(np.median([float(item["db_raw"]) for item in raw]))
    bands = [
        {
            "id": str(item["id"]),
            "freq_hz": float(item["freq_hz"]),
            "db": round(float(item["db_raw"]) - ref, 3),
        }
        for item in raw
    ]
    return {
        "version": 1,
        "basis": "active_vocal_regions_mid_normalized_fft_envelope",
        "bands": bands,
    }


def intervals_to_rows(intervals: list[tuple[float, float]], limit: int = 60) -> list[dict[str, float]]:
    return [
        {"start": round(start, 3), "end": round(end, 3), "duration": round(end - start, 3)}
        for start, end in intervals[:limit]
    ]


def active_intervals_from_vocal(
    vocal_data: np.ndarray,
    sr: int,
    frame_sec: float = 0.050,
    hop_sec: float = 0.025,
    threshold_below_peak_db: float = 34.0,
    noise_floor_db: float = -58.0,
    merge_gap_sec: float = 0.18,
    min_active_sec: float = 0.08,
    pad_sec: float = 0.04,
) -> list[tuple[float, float]]:
    """Find sung/voiced regions from a vocal stem for reference-aware measurements."""
    x = to_mono(vocal_data)
    if x.size == 0:
        return []
    frame = max(128, int(frame_sec * sr))
    hop = max(64, int(hop_sec * sr))
    if x.size < frame:
        return [(0.0, x.size / sr)]

    starts = np.arange(0, x.size - frame + 1, hop)
    rms = np.array([np.sqrt(np.mean(np.square(x[start : start + frame]))) for start in starts])
    rms_db = np.array([db(float(value)) for value in rms])
    peak_ref = float(np.percentile(rms_db, 95)) if rms_db.size else noise_floor_db
    threshold = max(noise_floor_db, peak_ref - threshold_below_peak_db)
    flags = rms_db >= threshold
    if not np.any(flags):
        return [(0.0, x.size / sr)]

    raw: list[tuple[float, float]] = []
    active_start: int | None = None
    for index, flag in enumerate(flags):
        if flag and active_start is None:
            active_start = index
        elif not flag and active_start is not None:
            start_sec = starts[active_start] / sr
            end_sec = (starts[index - 1] + frame) / sr
            raw.append((start_sec, end_sec))
            active_start = None
    if active_start is not None:
        raw.append((starts[active_start] / sr, (starts[-1] + frame) / sr))

    duration = x.size / sr
    padded = [
        (max(0.0, start - pad_sec), min(duration, end + pad_sec))
        for start, end in raw
        if end - start >= min_active_sec
    ]
    if not padded:
        return [(0.0, duration)]

    merged: list[list[float]] = [[padded[0][0], padded[0][1]]]
    for start, end in padded[1:]:
        previous = merged[-1]
        if start - previous[1] <= merge_gap_sec:
            previous[1] = max(previous[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged if end - start >= min_active_sec]


def collect_interval_audio(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
    max_seconds: float = 90.0,
) -> np.ndarray:
    if not intervals:
        return data[: int(max_seconds * sr)]
    chunks: list[np.ndarray] = []
    total = 0
    max_samples = int(max_seconds * sr)
    for start, end in intervals:
        s0 = max(0, int(start * sr))
        s1 = min(data.shape[0], int(end * sr))
        if s1 <= s0:
            continue
        chunk = data[s0:s1]
        remaining = max_samples - total
        if remaining <= 0:
            break
        if chunk.shape[0] > remaining:
            chunk = chunk[:remaining]
        chunks.append(chunk)
        total += chunk.shape[0]
    if not chunks:
        return data[: int(max_seconds * sr)]
    return np.concatenate(chunks, axis=0)


def rms_db_for_intervals(data: np.ndarray, sr: int, intervals: list[tuple[float, float]]) -> float:
    active = collect_interval_audio(data, sr, intervals, max_seconds=180.0)
    x = to_mono(active)
    if x.size == 0:
        return -120.0
    return round(db(float(np.sqrt(np.mean(np.square(x))))), 3)


def tonal_balance_for_intervals(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
) -> dict[str, float]:
    active = collect_interval_audio(data, sr, intervals)
    return tonal_balance(active, sr)


def spectral_envelope_for_intervals(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
) -> dict[str, Any]:
    active = collect_interval_audio(data, sr, intervals)
    return spectral_envelope_profile(active, sr)


def band_levels_for_intervals(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
) -> dict[str, float]:
    active = collect_interval_audio(data, sr, intervals)
    return {name: round(value, 3) for name, value in band_profile(active, sr, normalize_mid=False).items()}


def dynamics(data: np.ndarray) -> dict[str, float]:
    x = to_mono(data)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0
    crest = db(peak) - db(rms)
    frame_size = 4096
    hop = 2048
    if x.size >= frame_size:
        starts = np.arange(0, x.size - frame_size + 1, hop)
        frames = np.array([np.sqrt(np.mean(np.square(x[s : s + frame_size]))) for s in starts])
        active = frames[frames > 1e-6]
        if active.size:
            dr = db(float(np.percentile(active, 95))) - db(float(np.percentile(active, 10)))
        else:
            dr = 0.0
    else:
        dr = 0.0
    return {"crest_db": round(crest, 3), "dr_db": round(dr, 3)}


def vocal_dynamic_profile(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
    frame_sec: float = 0.050,
    hop_sec: float = 0.025,
) -> dict[str, float | int]:
    """Short-frame vocal dynamics inside active singing regions."""
    active = collect_interval_audio(data, sr, intervals, max_seconds=180.0)
    x = to_mono(active)
    if x.size == 0:
        return {
            "active_frame_count": 0,
            "active_rms_db": -120.0,
            "peak_db": -120.0,
            "crest_db": 0.0,
            "frame_p10_db": -120.0,
            "frame_p50_db": -120.0,
            "frame_p90_db": -120.0,
            "frame_range_p90_p10_db": 0.0,
            "micro_range_p95_p50_db": 0.0,
            "micro_range_p99_p50_db": 0.0,
        }
    rms = float(np.sqrt(np.mean(np.square(x))))
    peak = float(np.max(np.abs(x)))
    frame = max(128, int(frame_sec * sr))
    hop = max(64, int(hop_sec * sr))
    if x.size < frame:
        frame_db = np.array([db(rms)])
    else:
        starts = np.arange(0, x.size - frame + 1, hop)
        frame_db = np.array([
            db(float(np.sqrt(np.mean(np.square(x[start : start + frame])))))
            for start in starts
        ])
    floor = max(-60.0, float(np.percentile(frame_db, 95)) - 34.0)
    active_frames = frame_db[frame_db >= floor]
    if active_frames.size == 0:
        active_frames = frame_db
    return {
        "active_frame_count": int(active_frames.size),
        "active_rms_db": round(db(rms), 3),
        "peak_db": round(db(peak), 3),
        "crest_db": round(db(peak) - db(rms), 3),
        "frame_p10_db": round(float(np.percentile(active_frames, 10)), 3),
        "frame_p50_db": round(float(np.percentile(active_frames, 50)), 3),
        "frame_p90_db": round(float(np.percentile(active_frames, 90)), 3),
        "frame_range_p90_p10_db": round(float(np.percentile(active_frames, 90) - np.percentile(active_frames, 10)), 3),
        "micro_range_p95_p50_db": round(float(np.percentile(active_frames, 95) - np.percentile(active_frames, 50)), 3),
        "micro_range_p99_p50_db": round(float(np.percentile(active_frames, 99) - np.percentile(active_frames, 50)), 3),
    }


def reverb_proxy(data: np.ndarray, sr: int) -> dict[str, float]:
    """Crude wet/dry proxy: energy 150-400 ms after each transient onset vs onset peak."""
    x = to_mono(data)
    if x.size < sr:
        return {"tail_to_onset_ratio_db": -60.0, "est_rt60_ms": 0.0, "confidence": 0.0}
    frame = 1024
    hop = 512
    starts = np.arange(0, x.size - frame + 1, hop)
    env = np.array([np.sqrt(np.mean(np.square(x[s : s + frame]))) for s in starts])
    if env.size < 8:
        return {"tail_to_onset_ratio_db": -60.0, "est_rt60_ms": 0.0, "confidence": 0.0}
    diff = np.diff(env)
    threshold = float(np.percentile(diff, 95))
    onset_frames = np.where(diff > max(threshold, 1e-5))[0]
    if onset_frames.size == 0:
        return {
            "tail_to_onset_ratio_db": -60.0,
            "est_rt60_ms": 0.0,
            "onset_count": 0,
            "valid_tail_count": 0,
            "tail_iqr_db": 0.0,
            "confidence": 0.0,
        }
    frames_per_ms = sr / 1000.0 / hop
    tail_start = int(150 * frames_per_ms)
    tail_end = int(400 * frames_per_ms)
    ratios = []
    decays = []
    for onset in onset_frames:
        peak_idx = onset + 1
        if peak_idx >= env.size:
            continue
        peak_val = env[peak_idx]
        if peak_val < 1e-4:
            continue
        tail_slice = env[peak_idx + tail_start : peak_idx + tail_end]
        if tail_slice.size == 0:
            continue
        tail_val = float(np.mean(tail_slice))
        ratios.append(db(tail_val) - db(float(peak_val)))
        post = env[peak_idx : peak_idx + int(2000 * frames_per_ms)]
        if post.size > 4:
            post_db = np.array([db(v) for v in post])
            try:
                slope, _ = np.polyfit(np.arange(post.size), post_db, 1)
                if slope < -1e-6:
                    decays.append(-60.0 / slope / frames_per_ms)
            except (np.linalg.LinAlgError, ValueError):
                pass
    if not ratios:
        return {
            "tail_to_onset_ratio_db": -60.0,
            "est_rt60_ms": 0.0,
            "onset_count": int(onset_frames.size),
            "valid_tail_count": 0,
            "tail_iqr_db": 0.0,
            "confidence": 0.0,
        }
    ratio_med = float(np.median(ratios))
    rt60_med = float(np.median(decays)) if decays else 0.0
    ratio_iqr = float(np.percentile(ratios, 75) - np.percentile(ratios, 25)) if len(ratios) > 1 else 0.0
    count_score = clamp((len(ratios) - 4.0) / 16.0, 0.0, 1.0)
    stability_score = 1.0 - clamp(ratio_iqr / 8.0, 0.0, 1.0)
    wet_score = clamp((ratio_med + 12.0) / 12.0, 0.0, 1.0)
    confidence = clamp(0.15 + 0.45 * count_score + 0.25 * stability_score + 0.15 * wet_score, 0.0, 1.0)
    return {
        "tail_to_onset_ratio_db": round(ratio_med, 2),
        "est_rt60_ms": round(rt60_med, 1),
        "onset_count": int(onset_frames.size),
        "valid_tail_count": int(len(ratios)),
        "tail_iqr_db": round(ratio_iqr, 2),
        "confidence": round(confidence, 3),
    }


def delay_proxy(data: np.ndarray, sr: int) -> dict[str, float]:
    """Envelope autocorrelation proxy for audible 80-800 ms repeat structure."""
    x = to_mono(data)
    max_samples = min(x.size, int(sr * 180.0))
    x = x[:max_samples]
    if x.size < sr:
        return {"peak_corr": 0.0, "peak_lag_ms": 0.0, "confidence": 0.0}
    frame = max(128, int(sr * 0.020))
    hop = max(64, int(sr * 0.010))
    starts = np.arange(0, x.size - frame + 1, hop)
    if starts.size < 100:
        return {"peak_corr": 0.0, "peak_lag_ms": 0.0, "confidence": 0.0}
    env = np.array([np.sqrt(np.mean(np.square(x[start : start + frame]))) for start in starts])
    env = env - float(np.mean(env))
    energy = float(np.dot(env, env))
    if energy <= 1e-12:
        return {"peak_corr": 0.0, "peak_lag_ms": 0.0, "confidence": 0.0}
    n = 1 << int(np.ceil(np.log2(env.size * 2 - 1)))
    spectrum = np.fft.rfft(env, n=n)
    corr = np.fft.irfft(spectrum * np.conj(spectrum), n=n)[: env.size]
    corr = corr / max(corr[0], 1e-12)
    min_lag = max(1, int(0.080 / 0.010))
    max_lag = min(corr.size - 1, int(0.800 / 0.010))
    if max_lag <= min_lag:
        return {"peak_corr": 0.0, "peak_lag_ms": 0.0, "confidence": 0.0}
    segment = corr[min_lag : max_lag + 1]
    peak_offset = int(np.argmax(segment))
    peak_corr = float(segment[peak_offset])
    peak_lag_ms = float((min_lag + peak_offset) * hop / sr * 1000.0)
    confidence = clamp((peak_corr - 0.10) / 0.25, 0.0, 1.0)
    boundary_lag_guard = peak_lag_ms <= 90.0 or peak_lag_ms >= 790.0
    if boundary_lag_guard:
        confidence = min(confidence, 0.45)
    return {
        "peak_corr": round(peak_corr, 3),
        "peak_lag_ms": round(peak_lag_ms, 1),
        "confidence": round(confidence, 3),
        "boundary_lag_guard": bool(boundary_lag_guard),
    }


def interval_mask(length: int, sr: int, intervals: list[tuple[float, float]]) -> np.ndarray:
    mask = np.zeros(length, dtype=bool)
    for start, end in intervals:
        s0 = max(0, min(length, int(start * sr)))
        s1 = max(0, min(length, int(end * sr)))
        if s1 > s0:
            mask[s0:s1] = True
    return mask


def vocal_stem_quality(data: np.ndarray, sr: int, intervals: list[tuple[float, float]]) -> dict[str, float | bool]:
    """Estimate whether the reference vocal stem has too much non-vocal residual."""
    x = to_mono(data)
    if x.size == 0:
        return {
            "active_rms_db": -120.0,
            "inactive_rms_db": -120.0,
            "active_minus_inactive_db": 0.0,
            "active_coverage": 0.0,
            "severe_leakage": True,
        }
    mask = interval_mask(x.size, sr, intervals)
    active = x[mask]
    inactive = x[~mask]
    active_rms = float(np.sqrt(np.mean(np.square(active)))) if active.size else 0.0
    inactive_rms = float(np.sqrt(np.mean(np.square(inactive)))) if inactive.size else 0.0
    gap = db(active_rms) - db(inactive_rms)
    coverage = float(np.mean(mask)) if mask.size else 0.0
    severe = inactive.size > sr and gap < 6.0
    return {
        "active_rms_db": round(db(active_rms), 3),
        "inactive_rms_db": round(db(inactive_rms), 3),
        "active_minus_inactive_db": round(gap, 3),
        "active_coverage": round(coverage, 4),
        "severe_leakage": bool(severe),
    }


def lr_corr(left: np.ndarray, right: np.ndarray) -> float:
    if left.size < 2 or right.size < 2:
        return 1.0
    left_centered = left - float(np.mean(left))
    right_centered = right - float(np.mean(right))
    denom = float(np.sqrt(np.dot(left_centered, left_centered) * np.dot(right_centered, right_centered)))
    if denom <= 1e-12:
        return 1.0
    return float(np.dot(left_centered, right_centered) / denom)


def mono_fold_down_loss_db(left: np.ndarray, right: np.ndarray) -> float:
    if left.size == 0 or right.size == 0:
        return 0.0
    stereo_rms = float(np.sqrt(np.mean((np.square(left) + np.square(right)) * 0.5)))
    mono_rms = float(np.sqrt(np.mean(np.square((left + right) * 0.5))))
    return db(mono_rms) - db(stereo_rms)


def vocal_spatial_profile(data: np.ndarray, sr: int, intervals: list[tuple[float, float]]) -> dict[str, float | bool]:
    """Reference vocal-stem Mid/Side profile for deciding whether space should stay center-led."""
    if data.ndim == 1:
        stereo = np.column_stack([data, data])
    elif data.shape[1] == 1:
        stereo = np.repeat(data, 2, axis=1)
    else:
        stereo = data[:, :2]
    mask = interval_mask(stereo.shape[0], sr, intervals)
    left = stereo[:, 0]
    right = stereo[:, 1]
    mid = (left + right) / math.sqrt(2.0)
    side = (left - right) / math.sqrt(2.0)
    active_mid = mid[mask]
    active_side = side[mask]
    inactive_mid = mid[~mask]
    inactive_side = side[~mask]
    mid_active_db = db(float(np.sqrt(np.mean(np.square(active_mid)))) if active_mid.size else 0.0)
    side_active_db = db(float(np.sqrt(np.mean(np.square(active_side)))) if active_side.size else 0.0)
    mid_inactive_db = db(float(np.sqrt(np.mean(np.square(inactive_mid)))) if inactive_mid.size else 0.0)
    side_inactive_db = db(float(np.sqrt(np.mean(np.square(inactive_side)))) if inactive_side.size else 0.0)
    active_side_minus_mid = side_active_db - mid_active_db
    inactive_side_minus_mid = side_inactive_db - mid_inactive_db
    corr = lr_corr(left[mask], right[mask])
    mono_loss = mono_fold_down_loss_db(left[mask], right[mask])
    return {
        "mid_active_db": round(mid_active_db, 3),
        "side_active_db": round(side_active_db, 3),
        "active_side_minus_mid_db": round(active_side_minus_mid, 3),
        "inactive_side_minus_mid_db": round(inactive_side_minus_mid, 3),
        "lr_correlation_active": round(corr, 5),
        "mono_fold_down_loss_active_db": round(mono_loss, 3),
        "near_mono_center_led": bool(active_side_minus_mid <= -24.0 and corr >= 0.99 and mono_loss >= -0.25),
    }


def lufs_only(path: Path) -> float:
    return measure_loudness(path)["lufs_i"]


def normalize_song_token(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", "").replace("　", "")
    text = text.lower()
    return text


def extract_song_name(vocal_path: Path) -> str:
    """`hjf中文歌曲-黄昏_干声.wav` -> `黄昏`."""
    stem = vocal_path.stem
    for suffix in ("_干声", "_vocal", "-干声", "-vocal"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if "-" in stem:
        stem = stem.rsplit("-", 1)[-1]
    return stem.strip()


def fuzzy_find(folder: Path, song: str, extensions: tuple[str, ...]) -> Path | None:
    if not folder.exists():
        return None
    needle = normalize_song_token(song)
    if not needle:
        return None
    candidates: list[Path] = []
    for ext in extensions:
        candidates.extend(folder.glob(f"*{ext}"))
    for path in candidates:
        if needle in normalize_song_token(path.stem):
            return path
    return None


def resolve_downloads_root(downloads_root: Path | None = None) -> Path:
    if downloads_root is not None:
        return downloads_root
    for candidate in REFERENCE_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate
    return DOWNLOADS_ROOT


def resolve_accomp_file(
    folder: Path,
    song: str,
    extensions: tuple[str, ...],
    accomp_input: Path | None = None,
) -> Path | None:
    if accomp_input is not None and accomp_input.exists():
        try:
            accomp_input.resolve().relative_to(folder.resolve())
            return accomp_input
        except ValueError:
            same_name = folder / accomp_input.name
            if same_name.exists():
                return same_name
    return fuzzy_find(folder, song, extensions)


def resolve_reference_files(
    vocal_input: Path,
    downloads_root: Path | None = None,
    accomp_input: Path | None = None,
) -> dict[str, Path | None]:
    downloads_root = resolve_downloads_root(downloads_root)
    song = extract_song_name(vocal_input)
    return {
        "song": song,
        "full_mix": fuzzy_find(downloads_root / "原曲", song, (".mp3", ".wav", ".flac", ".m4a")),
        "vocal": fuzzy_find(downloads_root / "原曲人声", song, (".wav", ".mp3", ".flac")),
        "accomp": resolve_accomp_file(
            downloads_root / "伴奏",
            song,
            (".wav", ".mp3", ".flac"),
            accomp_input=accomp_input,
        ),
    }


def find_exact_timbre_reference(
    vocal_input: Path,
    timbre_folder: Path,
    extensions: tuple[str, ...],
) -> Path | None:
    """按干声文件名精确匹配同一行音色筛选片段，避免同歌名不同歌手串用。"""
    stem = vocal_input.stem
    candidate_stems: list[str] = []

    if "_干声" in stem:
        # 常规命名：歌手歌曲_干声.wav -> 歌手歌曲_音色筛选片段.wav。
        candidate_stems.append(stem.replace("_干声", "_音色筛选片段", 1))
        # row 后缀不一定在两边都存在，先保留 row，再补一个去 row 的同名候选。
        no_row_stem = re.sub(r"_干声(?:_row\d+)?$", "_音色筛选片段", stem)
        candidate_stems.append(no_row_stem)
    elif stem.endswith("干声"):
        candidate_stems.append(f"{stem[:-2]}音色筛选片段")

    # 有些临时输入不带“干声”后缀，仍允许显式同 basename 的筛选片段。
    candidate_stems.append(f"{stem}_音色筛选片段")

    seen_stems: set[str] = set()
    seen_exts: set[str] = set()
    ordered_exts = (vocal_input.suffix.lower(),) + extensions
    for candidate_stem in candidate_stems:
        if not candidate_stem or candidate_stem in seen_stems:
            continue
        seen_stems.add(candidate_stem)
        for ext in ordered_exts:
            ext = ext.lower()
            if not ext or ext in seen_exts:
                continue
            seen_exts.add(ext)
            candidate = timbre_folder / f"{candidate_stem}{ext}"
            if candidate.exists():
                return candidate
        seen_exts.clear()
    return None


def resolve_timbre_reference_file(
    vocal_input: Path,
    downloads_root: Path | None = None,
) -> Path | None:
    """解析“音色筛选片段”参考素材：先精确同 basename，再旧逻辑兜底。"""
    downloads_root = resolve_downloads_root(downloads_root)
    timbre_folder = downloads_root / "音色筛选片段"
    extensions = (".wav", ".mp3", ".flac", ".m4a")
    exact_match = find_exact_timbre_reference(vocal_input, timbre_folder, extensions)
    if exact_match is not None:
        return exact_match

    # 兜底才按歌名 fuzzy，防止黄昏/勇气这类同歌名素材误优先到别的歌手。
    song = extract_song_name(vocal_input)
    return fuzzy_find(timbre_folder, song, extensions)


def analyse(full_mix: Path, vocal: Path, accomp: Path) -> dict[str, Any]:
    full_audio, full_sr = load_audio_as_float(full_mix)
    vocal_audio, _ = load_audio_as_float(vocal)
    accomp_audio, _ = load_audio_as_float(accomp)
    active_regions = active_intervals_from_vocal(vocal_audio, full_sr)

    loudness = measure_loudness(full_mix)
    vocal_lufs = lufs_only(vocal)
    accomp_lufs = lufs_only(accomp)
    vocal_active_rms = rms_db_for_intervals(vocal_audio, full_sr, active_regions)
    accomp_active_rms = rms_db_for_intervals(accomp_audio, full_sr, active_regions)

    return {
        "sources": {
            "full_mix": str(full_mix),
            "vocal": str(vocal),
            "accomp": str(accomp),
        },
        "loudness": loudness,
        "tonal_balance": tonal_balance(full_audio, full_sr),
        "vocal_tonal_balance": tonal_balance_for_intervals(vocal_audio, full_sr, active_regions),
        "vocal_spectral_envelope": spectral_envelope_for_intervals(vocal_audio, full_sr, active_regions),
        "accomp_tonal_balance": tonal_balance_for_intervals(accomp_audio, full_sr, active_regions),
        "vocal_dynamics": vocal_dynamic_profile(vocal_audio, full_sr, active_regions),
        "active_band_levels": {
            "vocal": band_levels_for_intervals(vocal_audio, full_sr, active_regions),
            "accomp": band_levels_for_intervals(accomp_audio, full_sr, active_regions),
        },
        "dynamics": dynamics(full_audio),
        "vocal_accomp_balance": {
            "vocal_lufs": round(vocal_lufs, 2),
            "accomp_lufs": round(accomp_lufs, 2),
            "vocal_minus_accomp_db": round(vocal_lufs - accomp_lufs, 2),
            "active_vocal_rms_db": vocal_active_rms,
            "active_accomp_rms_db": accomp_active_rms,
            "active_vocal_minus_accomp_db": round(vocal_active_rms - accomp_active_rms, 2),
            "basis": "reference_vocal_active_regions",
        },
        "active_vocal_regions": {
            "count": len(active_regions),
            "coverage_sec": round(sum(end - start for start, end in active_regions), 3),
            "regions": intervals_to_rows(active_regions),
        },
        "vocal_stem_quality": vocal_stem_quality(vocal_audio, full_sr, active_regions),
        "vocal_spatial_profile": vocal_spatial_profile(vocal_audio, full_sr, active_regions),
        "reverb_proxy": reverb_proxy(vocal_audio, full_sr),
        "delay_proxy": delay_proxy(vocal_audio, full_sr),
    }


def analyse_timbre_reference(vocal: Path) -> dict[str, Any]:
    """只提取音色筛选片段的人声活动区特征。

    这里故意不测 loudness / 伴奏比例 / 空间参数，避免把“音色相似度”
    和后续混音阶段的响度、空间、总线平衡耦合在一起。
    """
    vocal_audio, sr = load_audio_as_float(vocal)
    active_regions = active_intervals_from_vocal(vocal_audio, sr)
    return {
        "sources": {
            "timbre_vocal": str(vocal),
        },
        "vocal_tonal_balance": tonal_balance_for_intervals(vocal_audio, sr, active_regions),
        "vocal_spectral_envelope": spectral_envelope_for_intervals(vocal_audio, sr, active_regions),
        "active_band_levels": {
            "vocal": band_levels_for_intervals(vocal_audio, sr, active_regions),
        },
        "vocal_dynamics": vocal_dynamic_profile(vocal_audio, sr, active_regions),
        "active_vocal_regions": {
            "count": len(active_regions),
            "coverage_sec": round(sum(end - start for start, end in active_regions), 3),
            "regions": intervals_to_rows(active_regions),
        },
    }


def analyse_input_pair(vocal: Path, accomp: Path) -> dict[str, Any]:
    """Predict the input-mix tonal/dynamics by summing pre-render vocal + accomp."""
    vocal_audio, sr = load_audio_as_float(vocal)
    accomp_audio, _ = load_audio_as_float(accomp)
    active_regions = active_intervals_from_vocal(vocal_audio, sr)
    n = min(vocal_audio.shape[0], accomp_audio.shape[0])
    summed = vocal_audio[:n] + accomp_audio[:n]
    vocal_lufs = lufs_only(vocal)
    accomp_lufs = lufs_only(accomp)
    vocal_active_rms = rms_db_for_intervals(vocal_audio, sr, active_regions)
    accomp_active_rms = rms_db_for_intervals(accomp_audio, sr, active_regions)
    return {
        "sources": {"vocal": str(vocal), "accomp": str(accomp)},
        "tonal_balance": tonal_balance(summed, sr),
        "vocal_tonal_balance": tonal_balance_for_intervals(vocal_audio, sr, active_regions),
        "vocal_spectral_envelope": spectral_envelope_for_intervals(vocal_audio, sr, active_regions),
        "accomp_tonal_balance": tonal_balance_for_intervals(accomp_audio, sr, active_regions),
        "vocal_dynamics": vocal_dynamic_profile(vocal_audio, sr, active_regions),
        "active_band_levels": {
            "vocal": band_levels_for_intervals(vocal_audio, sr, active_regions),
            "accomp": band_levels_for_intervals(accomp_audio, sr, active_regions),
        },
        "dynamics": dynamics(summed),
        "vocal_accomp_balance": {
            "vocal_lufs": round(vocal_lufs, 2),
            "accomp_lufs": round(accomp_lufs, 2),
            "vocal_minus_accomp_db": round(vocal_lufs - accomp_lufs, 2),
            "active_vocal_rms_db": vocal_active_rms,
            "active_accomp_rms_db": accomp_active_rms,
            "active_vocal_minus_accomp_db": round(vocal_active_rms - accomp_active_rms, 2),
            "basis": "input_vocal_active_regions",
        },
        "active_vocal_regions": {
            "count": len(active_regions),
            "coverage_sec": round(sum(end - start for start, end in active_regions), 3),
            "regions": intervals_to_rows(active_regions),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract reference-track features for the mix planner.")
    parser.add_argument("--vocal-input", type=Path, default=None,
                        help="Vocal-to-be-mixed; used to auto-resolve reference files by song name.")
    parser.add_argument("--full-mix", type=Path, default=None)
    parser.add_argument("--ref-vocal", type=Path, default=None)
    parser.add_argument("--ref-accomp", type=Path, default=None)
    parser.add_argument("--downloads-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if args.vocal_input is not None and (args.full_mix is None or args.ref_vocal is None or args.ref_accomp is None):
        resolved = resolve_reference_files(args.vocal_input.resolve(), args.downloads_root)
        args.full_mix = args.full_mix or resolved["full_mix"]
        args.ref_vocal = args.ref_vocal or resolved["vocal"]
        args.ref_accomp = args.ref_accomp or resolved["accomp"]

    missing = [name for name, value in (("full-mix", args.full_mix), ("ref-vocal", args.ref_vocal), ("ref-accomp", args.ref_accomp)) if value is None]
    if missing:
        raise SystemExit(f"Could not resolve reference inputs: {', '.join(missing)}")

    features = analyse(Path(args.full_mix), Path(args.ref_vocal), Path(args.ref_accomp))
    out_text = json.dumps(features, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out_text, encoding="utf-8")
    print(out_text)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
