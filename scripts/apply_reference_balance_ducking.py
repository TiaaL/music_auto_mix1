#!/usr/bin/env python3
"""Apply reference-driven accompaniment ducking before mix summing."""

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


def decode_audio(path: Path, sample_rate: int, channels: int) -> np.ndarray:
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
            str(sample_rate),
            "-ac",
            str(channels),
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-1200:])
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    if raw.size == 0:
        raise RuntimeError(f"Decoded empty audio: {path}")
    return raw.reshape(-1, channels).astype(np.float64)


def lufs_like(audio: np.ndarray) -> float:
    if audio.ndim == 1:
        energy = float(np.mean(np.square(audio)))
    else:
        energy = float(np.mean(np.sum(np.square(audio), axis=1) / audio.shape[1]))
    if energy <= 1e-12 or not math.isfinite(energy):
        return -120.0
    return -0.691 + 10.0 * math.log10(energy)


def smooth_gain(gain_db: np.ndarray, sample_rate: int, transition_ms: float) -> np.ndarray:
    tau = max(0.05, transition_ms / 1000.0)
    alpha = 1.0 - math.exp(-1.0 / (tau * sample_rate))
    out = np.empty_like(gain_db)
    previous = float(gain_db[0]) if gain_db.size else 0.0
    for idx, value in enumerate(gain_db):
        previous += (float(value) - previous) * alpha
        out[idx] = previous
    return out


def db_to_gain(db_value: float | np.ndarray) -> float | np.ndarray:
    return np.power(10.0, np.asarray(db_value) / 20.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Duck accompaniment to follow reference vocal/accomp balance.")
    parser.add_argument("--reference-vocal", type=Path, required=True)
    parser.add_argument("--reference-accomp", type=Path, required=True)
    parser.add_argument("--current-vocal", type=Path, required=True)
    parser.add_argument("--current-accomp", type=Path, required=True)
    parser.add_argument("--output-accomp", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--sample-rate", type=int, default=44100)
    parser.add_argument("--window-sec", type=float, default=2.4)
    parser.add_argument("--hop-sec", type=float, default=0.6)
    parser.add_argument("--transition-ms", type=float, default=220.0)
    parser.add_argument("--gate-lufs", type=float, default=-48.0)
    parser.add_argument("--deadband-db", type=float, default=0.55)
    parser.add_argument("--follow-strength", type=float, default=0.8)
    parser.add_argument("--max-duck-db", type=float, default=4.0)
    parser.add_argument("--vocal-bus-gain-db", type=float, default=0.0)
    parser.add_argument("--accomp-bus-gain-db", type=float, default=0.0)
    args = parser.parse_args()

    sr = args.sample_rate
    ref_vocal = decode_audio(args.reference_vocal, sr, 1)
    ref_accomp = decode_audio(args.reference_accomp, sr, 2)
    cur_vocal = decode_audio(args.current_vocal, sr, 2) * float(db_to_gain(args.vocal_bus_gain_db))
    cur_accomp_raw = decode_audio(args.current_accomp, sr, 2)
    cur_accomp = cur_accomp_raw * float(db_to_gain(args.accomp_bus_gain_db))

    length = min(ref_vocal.shape[0], ref_accomp.shape[0], cur_vocal.shape[0], cur_accomp.shape[0])
    ref_vocal = ref_vocal[:length]
    ref_accomp = ref_accomp[:length]
    cur_vocal = cur_vocal[:length]
    cur_accomp = cur_accomp[:length]

    win = max(1, int(round(args.window_sec * sr)))
    hop = max(1, int(round(args.hop_sec * sr)))
    centers: list[int] = []
    ducks: list[float] = []
    frames: list[dict[str, float | bool]] = []
    previous_duck = 0.0

    for start in range(0, max(1, length - win + 1), hop):
        end = min(length, start + win)
        center = start + (end - start) // 2
        rv = lufs_like(ref_vocal[start:end])
        ra = lufs_like(ref_accomp[start:end])
        cv = lufs_like(cur_vocal[start:end])
        ca = lufs_like(cur_accomp[start:end])
        valid = rv > args.gate_lufs and ra > args.gate_lufs and cv > args.gate_lufs and ca > args.gate_lufs
        ref_balance = rv - ra
        current_balance = cv - ca
        needed = ref_balance - current_balance
        if valid and needed > args.deadband_db:
            duck = min(args.max_duck_db, max(0.0, (needed - args.deadband_db) * args.follow_strength))
            previous_duck = duck
        elif valid:
            duck = 0.0
            previous_duck = duck
        else:
            duck = previous_duck
        centers.append(center)
        ducks.append(-duck)
        frames.append(
            {
                "time_s": round(center / sr, 3),
                "valid": valid,
                "reference_vocal_minus_accomp_db": round(ref_balance, 3),
                "current_vocal_minus_accomp_db": round(current_balance, 3),
                "needed_lift_db": round(needed, 3),
                "applied_accomp_gain_db": round(-duck, 3),
            }
        )

    if not centers:
        centers = [0, length - 1]
        ducks = [0.0, 0.0]
    elif centers[0] > 0:
        centers.insert(0, 0)
        ducks.insert(0, ducks[0])
    if centers[-1] < length - 1:
        centers.append(length - 1)
        ducks.append(ducks[-1])

    sample_positions = np.arange(length)
    dynamic_gain_db = np.interp(sample_positions, np.array(centers), np.array(ducks))
    dynamic_gain_db = smooth_gain(dynamic_gain_db.astype(np.float64), sr, args.transition_ms)
    total_gain_db = dynamic_gain_db + args.accomp_bus_gain_db
    processed = cur_accomp_raw[:length] * db_to_gain(total_gain_db)[:, None]
    peak = float(np.max(np.abs(processed))) if processed.size else 0.0
    if peak > 0.985:
        processed *= 0.985 / peak

    args.output_accomp.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output_accomp, processed.astype(np.float32), sr, subtype="FLOAT")

    report = {
        "inputs": {
            "reference_vocal": str(args.reference_vocal),
            "reference_accomp": str(args.reference_accomp),
            "current_vocal": str(args.current_vocal),
            "current_accomp": str(args.current_accomp),
            "output_accomp": str(args.output_accomp),
        },
        "sample_rate": sr,
        "window_sec": args.window_sec,
        "hop_sec": args.hop_sec,
        "transition_ms": args.transition_ms,
        "gate_lufs": args.gate_lufs,
        "deadband_db": args.deadband_db,
        "follow_strength": args.follow_strength,
        "max_duck_db": args.max_duck_db,
        "bus_gain_db": {
            "vocal": args.vocal_bus_gain_db,
            "accomp": args.accomp_bus_gain_db,
        },
        "dynamic_accomp_gain_summary_db": {
            "min": round(float(np.min(dynamic_gain_db)), 3),
            "median": round(float(np.median(dynamic_gain_db)), 3),
            "p10": round(float(np.percentile(dynamic_gain_db, 10)), 3),
            "p90": round(float(np.percentile(dynamic_gain_db, 90)), 3),
            "max": round(float(np.max(dynamic_gain_db)), 3),
        },
        "total_accomp_gain_summary_db": {
            "min": round(float(np.min(total_gain_db)), 3),
            "median": round(float(np.median(total_gain_db)), 3),
            "max": round(float(np.max(total_gain_db)), 3),
        },
        "output_peak": round(float(np.max(np.abs(processed))) if processed.size else 0.0, 6),
        "frames": frames,
    }
    report_path = args.report or args.output_accomp.with_suffix(".reference_balance_ducking.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "frames"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
