#!/usr/bin/env python3
"""Light final safety guard for short high-frequency burst artifacts."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy import signal


def db(values: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(values, 1e-12))


def lin(gain_db: np.ndarray) -> np.ndarray:
    return np.power(10.0, gain_db / 20.0)


def frame_rms(samples: np.ndarray, sr: int, frame_ms: float, hop_ms: float) -> tuple[np.ndarray, np.ndarray]:
    frame = max(128, int(round(sr * frame_ms * 0.001)))
    hop = max(32, int(round(sr * hop_ms * 0.001)))
    if samples.shape[0] < frame:
        rms = np.sqrt(np.mean(np.square(samples), axis=0, keepdims=True).mean(axis=1) + 1e-12)
        return np.array([0.0]), rms
    starts = np.arange(0, samples.shape[0] - frame + 1, hop)
    window = np.hanning(frame)[:, None]
    norm = max(float(np.mean(np.square(window))), 1e-12)
    values = np.empty(starts.size, dtype=np.float64)
    for idx, start in enumerate(starts):
        chunk = samples[start : start + frame] * window
        values[idx] = np.sqrt(float(np.mean(np.square(chunk))) / norm + 1e-12)
    times = (starts + frame * 0.5) / sr
    return times, values


def smooth_gain(gain_db: np.ndarray, sr: int, attack_ms: float = 2.0, release_ms: float = 75.0) -> np.ndarray:
    attack = np.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    release = np.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    out = np.empty_like(gain_db)
    prev = float(gain_db[0])
    for idx, target in enumerate(gain_db):
        coeff = attack if target < prev else release
        prev = coeff * prev + (1.0 - coeff) * float(target)
        out[idx] = prev
    return out


def process(
    audio: np.ndarray,
    sr: int,
    cutoff_hz: float,
    max_atten_db: float,
    excess_threshold_db: float,
    min_high_db: float,
    min_high_to_full_db: float,
    max_event_ms: float,
) -> tuple[np.ndarray, dict[str, object]]:
    if audio.shape[0] < sr // 2:
        return audio, {"enabled": True, "triggered": False, "reason": "too_short"}

    nyq = sr * 0.5
    sos = signal.butter(4, min(cutoff_hz, nyq * 0.9) / nyq, btype="highpass", output="sos")
    high = signal.sosfiltfilt(sos, audio, axis=0)
    body = audio - high

    times, high_rms = frame_rms(high, sr, frame_ms=18.0, hop_ms=5.0)
    _, full_rms = frame_rms(audio, sr, frame_ms=18.0, hop_ms=5.0)
    high_db = db(high_rms)
    full_db = db(full_rms)

    median_frames = max(5, int(round(1.2 / 0.005)))
    if median_frames % 2 == 0:
        median_frames += 1
    local_floor = signal.medfilt(high_db, kernel_size=median_frames)
    excess = high_db - local_floor
    high_to_full = high_db - full_db

    raw_trigger = (
        (excess >= excess_threshold_db)
        & (high_db >= min_high_db)
        & (high_to_full >= min_high_to_full_db)
    )

    groups_raw: list[tuple[int, int]] = []
    if np.any(raw_trigger):
        idx = np.flatnonzero(raw_trigger)
        group_start = int(idx[0])
        group_end = int(idx[0])
        for cur_idx in idx[1:]:
            cur = int(cur_idx)
            if cur - group_end > 2:
                groups_raw.append((group_start, group_end))
                group_start = cur
            group_end = cur
        groups_raw.append((group_start, group_end))

    trigger = raw_trigger.copy()
    suppressed_long_groups = 0
    if max_event_ms > 0.0 and groups_raw:
        trigger = np.zeros_like(raw_trigger)
        max_event_sec = max_event_ms * 0.001
        hop_sec = 0.005
        for start, end in groups_raw:
            duration = float(times[end] - times[start]) + hop_sec
            if duration <= max_event_sec:
                trigger[start : end + 1] = raw_trigger[start : end + 1]
            else:
                suppressed_long_groups += 1
    atten = np.zeros_like(high_db)
    atten[trigger] = -np.clip(
        (excess[trigger] - excess_threshold_db) / 8.0 * max_atten_db,
        0.0,
        max_atten_db,
    )

    sample_times = np.arange(audio.shape[0], dtype=np.float64) / sr
    gain_db = np.interp(sample_times, times, atten, left=0.0, right=0.0)
    gain_db = smooth_gain(gain_db, sr)
    out = body + high * lin(gain_db)[:, None]
    out = np.clip(out, -0.999, 0.999)

    groups: list[dict[str, float]] = []
    if np.any(trigger):
        idx = np.flatnonzero(trigger)
        starts = [int(idx[0])]
        ends: list[int] = []
        for prev, cur in zip(idx[:-1], idx[1:]):
            if cur - prev > 2:
                ends.append(int(prev))
                starts.append(int(cur))
        ends.append(int(idx[-1]))
        ranked = sorted(
            (
                (
                    float(np.min(atten[start : end + 1])),
                    float(times[start]),
                    float(times[end]),
                    float(np.max(excess[start : end + 1])),
                    float(np.max(high_db[start : end + 1])),
                )
                for start, end in zip(starts, ends)
            ),
            key=lambda row: row[0],
        )
        for gain, start_sec, end_sec, max_excess, max_high_db in ranked[:40]:
            groups.append({
                "start_sec": round(start_sec, 3),
                "end_sec": round(end_sec, 3),
                "atten_db": round(gain, 3),
                "max_excess_db": round(max_excess, 3),
                "max_high_db": round(max_high_db, 3),
            })

    report = {
        "enabled": True,
        "triggered": bool(np.any(trigger)),
        "cutoff_hz": cutoff_hz,
        "max_atten_db": max_atten_db,
        "excess_threshold_db": excess_threshold_db,
        "min_high_db": min_high_db,
        "min_high_to_full_db": min_high_to_full_db,
        "max_event_ms": max_event_ms,
        "raw_triggered_frames": int(np.sum(raw_trigger)),
        "triggered_frames": int(np.sum(trigger)),
        "raw_triggered_groups": len(groups_raw),
        "suppressed_long_groups": suppressed_long_groups,
        "peak_atten_db": round(float(np.min(gain_db)), 3),
        "events": groups,
        "policy": "attenuate only short high-frequency excess after loudness compensation; leave body/mid/low untouched",
    }
    return out, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a light final high-frequency transient guard.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--cutoff-hz", type=float, default=5000.0)
    parser.add_argument("--max-atten-db", type=float, default=2.0)
    parser.add_argument("--excess-threshold-db", type=float, default=13.0)
    parser.add_argument("--min-high-db", type=float, default=-39.0)
    parser.add_argument("--min-high-to-full-db", type=float, default=-21.0)
    parser.add_argument("--max-event-ms", type=float, default=160.0)
    parser.add_argument("--output-subtype", choices=("FLOAT", "PCM_16", "PCM_24"), default="PCM_16")
    args = parser.parse_args()

    audio, sr = sf.read(args.input_wav, always_2d=True, dtype="float64")
    out, report = process(
        audio,
        int(sr),
        cutoff_hz=args.cutoff_hz,
        max_atten_db=max(0.0, args.max_atten_db),
        excess_threshold_db=args.excess_threshold_db,
        min_high_db=args.min_high_db,
        min_high_to_full_db=args.min_high_to_full_db,
        max_event_ms=args.max_event_ms,
    )
    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if report.get("triggered"):
        sf.write(args.output_wav, out, sr, subtype=args.output_subtype)
    else:
        shutil.copyfile(args.input_wav, args.output_wav)
    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "[final-transient-guard] "
        f"triggered={report.get('triggered')} frames={report.get('triggered_frames')} "
        f"peak_atten={report.get('peak_atten_db')} dB"
    )


if __name__ == "__main__":
    main()
