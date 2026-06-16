#!/usr/bin/env python3
"""Add a very light voice-correlated side layer to a rendered vocal group.

This is a second-stage tool, not a default widening pass. It expects a mono
post-source-EQ vocal and the current stereo vocal_group, then adds band-limited
pure-side energy so Mid stays unchanged and mono fold-down keeps the existing
center vocal.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from pathlib import Path
from typing import Any

import numpy as np

from analyze_reference import load_audio_as_float


LIGHT_PARAMS = {
    "side_gain_db": -20.0,
    "delay_ms": 8.0,
    "band_low_hz": 180.0,
    "band_high_hz": 6500.0,
    "peak_ceiling": 0.96,
}


def command_path(name: str) -> str:
    return shutil.which(name) or name


def audio_sample_rate(path: Path) -> int:
    proc = subprocess.run(
        [
            command_path("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-500:])
    return int(proc.stdout.strip())


def db2lin(value: float) -> float:
    return math.pow(10.0, value / 20.0)


def bandpass_fft(x: np.ndarray, sr: int, low_hz: float, high_hz: float) -> np.ndarray:
    if x.size < 16:
        return x.copy()
    spectrum = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    mask = (freqs >= low_hz) & (freqs <= min(high_hz, sr * 0.48))
    spectrum *= mask
    return np.fft.irfft(spectrum, n=x.size).astype(np.float64)


def delay_signal(x: np.ndarray, sr: int, delay_ms: float) -> np.ndarray:
    delay = max(0, int(round(delay_ms * 0.001 * sr)))
    if delay <= 0:
        return x.copy()
    out = np.zeros_like(x)
    if delay < x.size:
        out[delay:] = x[:-delay]
    return out


def write_float_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [
            command_path("ffmpeg"),
            "-y",
            "-hide_banner",
            "-nostats",
            "-f",
            "f32le",
            "-ar",
            str(sr),
            "-ac",
            "2",
            "-i",
            "-",
            "-c:a",
            "pcm_f32le",
            str(path),
        ],
        input=np.asarray(audio, dtype=np.float32).tobytes(),
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace")[-800:])


def apply_layer(vocal_mono_path: Path, vocal_group_path: Path, output_path: Path, params: dict[str, float]) -> dict[str, Any]:
    sr = audio_sample_rate(vocal_group_path)
    vocal, _ = load_audio_as_float(vocal_mono_path, target_sr=sr)
    group, group_sr = load_audio_as_float(vocal_group_path, target_sr=sr)
    if group_sr != sr:
        raise RuntimeError(f"sample-rate mismatch after decode: {sr} vs {group_sr}")

    source = vocal[:, 0] if vocal.ndim == 2 else vocal
    n = min(source.size, group.shape[0])
    source = source[:n]
    group = group[:n, :2].astype(np.float64)

    side = bandpass_fft(source, sr, params["band_low_hz"], params["band_high_hz"])
    side = delay_signal(side, sr, params["delay_ms"])
    side *= db2lin(params["side_gain_db"])

    out = group.copy()
    out[:, 0] += side
    out[:, 1] -= side

    peak_before = float(np.max(np.abs(out))) if out.size else 0.0
    trim_db = 0.0
    ceiling = float(params["peak_ceiling"])
    if peak_before > ceiling > 0.0:
        scale = ceiling / peak_before
        out *= scale
        trim_db = 20.0 * math.log10(scale)

    write_float_wav(output_path, out, sr)
    return {
        "enabled": True,
        "mode": "light",
        "input_vocal_mono": str(vocal_mono_path),
        "input_vocal_group": str(vocal_group_path),
        "output": str(output_path),
        "params": params,
        "peak_before_trim": round(peak_before, 6),
        "safety_trim_db": round(trim_db, 3),
        "policy": "pure_side_band_limited_voice_correlated_layer_mid_unchanged",
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply a light direct vocal side layer.")
    parser.add_argument("vocal_mono", type=Path)
    parser.add_argument("vocal_group", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mode", choices=("light",), default="light")
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--side-gain-db", type=float, default=LIGHT_PARAMS["side_gain_db"])
    parser.add_argument("--delay-ms", type=float, default=LIGHT_PARAMS["delay_ms"])
    parser.add_argument("--band-low-hz", type=float, default=LIGHT_PARAMS["band_low_hz"])
    parser.add_argument("--band-high-hz", type=float, default=LIGHT_PARAMS["band_high_hz"])
    args = parser.parse_args()

    params = dict(LIGHT_PARAMS)
    params.update({
        "side_gain_db": args.side_gain_db,
        "delay_ms": args.delay_ms,
        "band_low_hz": args.band_low_hz,
        "band_high_hz": args.band_high_hz,
    })
    report = apply_layer(args.vocal_mono, args.vocal_group, args.output, params)
    if args.metadata:
        write_json(args.metadata, report)


if __name__ == "__main__":
    main()
