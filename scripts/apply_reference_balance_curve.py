#!/usr/bin/env python3
"""Apply an original-song vocal/accompaniment balance curve to an AI vocal.

The curve is derived from original dry vocal vs accompaniment short-window
LUFS-like energy. It only writes a processed AI vocal; the normal template
renderer still handles effects, DelayVerb, bus processing, and final loudness.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parent.parent


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


def decode_audio(path: Path, sr: int, channels: int) -> np.ndarray:
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
            str(sr),
            "-ac",
            str(channels),
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-1000:])
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    if raw.size == 0:
        raise RuntimeError(f"Decoded empty audio: {path}")
    return raw.reshape(-1, channels).astype(np.float64)


def short_lufs_like(frame: np.ndarray) -> float:
    """Relative LUFS-like level from mean square energy.

    For this automation task, the important value is the dB difference between
    aligned stems. The BS.1770 absolute offset cancels in the balance curve.
    """
    if frame.ndim == 1:
        energy = float(np.mean(np.square(frame)))
    else:
        energy = float(np.mean(np.sum(np.square(frame), axis=1) / frame.shape[1]))
    if energy <= 1e-12 or not math.isfinite(energy):
        return -120.0
    return -0.691 + 10.0 * math.log10(energy)


def active_global_lufs_like(audio: np.ndarray, sr: int, window_s: float, hop_s: float, gate_lufs: float) -> float:
    win = max(1, int(round(window_s * sr)))
    hop = max(1, int(round(hop_s * sr)))
    values = []
    for start in range(0, max(1, audio.shape[0] - win + 1), hop):
        value = short_lufs_like(audio[start : start + win])
        if value > gate_lufs:
            values.append(value)
    if not values:
        return short_lufs_like(audio)
    energies = [10.0 ** ((value + 0.691) / 10.0) for value in values]
    return -0.691 + 10.0 * math.log10(float(np.mean(energies)))


def smooth_gain(gain_db: np.ndarray, sr: int, transition_ms: float) -> np.ndarray:
    tau = max(0.05, transition_ms / 1000.0)
    alpha = 1.0 - math.exp(-1.0 / (tau * sr))
    out = np.empty_like(gain_db)
    prev = float(gain_db[0]) if gain_db.size else 0.0
    for idx, value in enumerate(gain_db):
        prev = prev + (float(value) - prev) * alpha
        out[idx] = prev
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply original-song balance curve to AI vocal.")
    parser.add_argument("--original-dry-vocal", type=Path, required=True)
    parser.add_argument("--accompaniment", type=Path, required=True)
    parser.add_argument("--ai-vocal", type=Path, required=True)
    parser.add_argument("--output-vocal", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--window-sec", type=float, default=3.0)
    parser.add_argument("--hop-sec", type=float, default=1.5)
    parser.add_argument("--gate-lufs", type=float, default=-50.0)
    parser.add_argument("--min-gain-db", type=float, default=-8.0)
    parser.add_argument(
        "--max-gain-db",
        type=float,
        default=0.0,
        help="Maximum gain applied to the AI vocal. Defaults to 0 dB so this stage never boosts vocal loudness.",
    )
    parser.add_argument("--transition-ms", type=float, default=250.0)
    args = parser.parse_args()

    sr = args.sample_rate
    original = decode_audio(args.original_dry_vocal, sr, 1)
    ai = decode_audio(args.ai_vocal, sr, 1)
    accomp = decode_audio(args.accompaniment, sr, 2)
    length = min(original.shape[0], ai.shape[0], accomp.shape[0])
    original = original[:length]
    ai = ai[:length]
    accomp = accomp[:length]

    win = max(1, int(round(args.window_sec * sr)))
    hop = max(1, int(round(args.hop_sec * sr)))
    correction = active_global_lufs_like(ai, sr, args.window_sec, args.hop_sec, args.gate_lufs) - active_global_lufs_like(
        original, sr, args.window_sec, args.hop_sec, args.gate_lufs
    )

    centers: list[int] = []
    gains: list[float] = []
    frames: list[dict[str, float | int | bool]] = []
    prev_gain = 0.0
    for start in range(0, max(1, length - win + 1), hop):
        end = min(length, start + win)
        center = start + (end - start) // 2
        original_lufs = short_lufs_like(original[start:end])
        accomp_lufs = short_lufs_like(accomp[start:end])
        ai_lufs = short_lufs_like(ai[start:end])
        valid = original_lufs > args.gate_lufs and ai_lufs > args.gate_lufs and accomp_lufs > args.gate_lufs
        if valid:
            balance = original_lufs - accomp_lufs
            target = accomp_lufs + balance - correction
            raw_gain = target - ai_lufs
            gain = max(args.min_gain_db, min(args.max_gain_db, raw_gain))
            prev_gain = gain
        else:
            balance = original_lufs - accomp_lufs
            target = ai_lufs + prev_gain
            raw_gain = prev_gain
            gain = prev_gain
        centers.append(center)
        gains.append(gain)
        frames.append(
            {
                "time_s": round(center / sr, 3),
                "valid": valid,
                "original_lufs": round(original_lufs, 3),
                "accomp_lufs": round(accomp_lufs, 3),
                "ai_lufs": round(ai_lufs, 3),
                "balance_curve_db": round(balance, 3),
                "target_ai_lufs": round(target, 3),
                "raw_gain_db": round(raw_gain, 3),
                "applied_gain_db": round(gain, 3),
            }
        )

    if not centers:
        centers = [0, length - 1]
        gains = [0.0, 0.0]
    elif centers[0] > 0:
        centers.insert(0, 0)
        gains.insert(0, gains[0])
    if centers[-1] < length - 1:
        centers.append(length - 1)
        gains.append(gains[-1])

    sample_positions = np.arange(length)
    gain_curve = np.interp(sample_positions, np.array(centers), np.array(gains))
    gain_curve = smooth_gain(gain_curve.astype(np.float64), sr, args.transition_ms)
    processed = ai[:, 0] * np.power(10.0, gain_curve / 20.0)
    peak = float(np.max(np.abs(processed))) if processed.size else 0.0
    if peak > 0.985:
        processed *= 0.985 / peak

    args.output_vocal.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output_vocal, processed.astype(np.float32), sr, subtype="PCM_24")

    report = {
        "inputs": {
            "original_dry_vocal": str(args.original_dry_vocal),
            "accompaniment": str(args.accompaniment),
            "ai_vocal": str(args.ai_vocal),
            "output_vocal": str(args.output_vocal),
        },
        "sample_rate": sr,
        "window_sec": args.window_sec,
        "hop_sec": args.hop_sec,
        "gate_lufs": args.gate_lufs,
        "correction_db": round(correction, 3),
        "gain_limits_db": {"min": args.min_gain_db, "max": args.max_gain_db},
        "transition_ms": args.transition_ms,
        "gain_summary_db": {
            "min": round(float(np.min(gain_curve)), 3),
            "median": round(float(np.median(gain_curve)), 3),
            "max": round(float(np.max(gain_curve)), 3),
        },
        "output_peak": round(float(np.max(np.abs(processed))) if processed.size else 0.0, 6),
        "frames": frames,
    }
    report_path = args.report or args.output_vocal.with_suffix(".balance_curve.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "frames"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
