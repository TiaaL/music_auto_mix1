#!/usr/bin/env python3
"""Measure loudness with libebur128 and optionally compare with FFmpeg loudnorm."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import math
import re
import shutil
import subprocess
import time
from pathlib import Path

import numpy as np
import soundfile as sf


EBUR128_SUCCESS = 0
EBUR128_MODE_M = 1 << 0
EBUR128_MODE_S = (1 << 1) | EBUR128_MODE_M
EBUR128_MODE_I = (1 << 2) | EBUR128_MODE_M
EBUR128_MODE_LRA = (1 << 3) | EBUR128_MODE_S
EBUR128_MODE_SAMPLE_PEAK = (1 << 4) | EBUR128_MODE_M
EBUR128_MODE_TRUE_PEAK = (1 << 5) | EBUR128_MODE_M | EBUR128_MODE_SAMPLE_PEAK
EBUR128_MODE_HISTOGRAM = 1 << 6
DEFAULT_MODE = EBUR128_MODE_I | EBUR128_MODE_LRA | EBUR128_MODE_TRUE_PEAK


def command_path(name: str) -> str:
    return shutil.which(name) or name


def load_libebur128() -> ctypes.CDLL:
    lib_path = ctypes.util.find_library("ebur128") or "/opt/homebrew/lib/libebur128.dylib"
    lib = ctypes.CDLL(lib_path)
    lib.ebur128_init.argtypes = [ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
    lib.ebur128_init.restype = ctypes.c_void_p
    lib.ebur128_destroy.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.ebur128_add_frames_float.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_size_t]
    lib.ebur128_loudness_global.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]
    lib.ebur128_loudness_range.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]
    lib.ebur128_relative_threshold.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_double)]
    lib.ebur128_true_peak.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_double)]
    return lib


def check(code: int, label: str) -> None:
    if code != EBUR128_SUCCESS:
        raise RuntimeError(f"{label} failed with libebur128 code {code}")


def amplitude_to_db(value: float) -> float:
    if value <= 0.0:
        return float("-inf")
    return 20.0 * math.log10(value)


def libebur128_measure(path: Path, use_histogram: bool = False, chunk_frames: int = 262_144) -> dict[str, float | str]:
    start = time.perf_counter()
    data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    read_sec = time.perf_counter() - start
    data = np.ascontiguousarray(data, dtype=np.float32)
    frames, channels = data.shape
    lib = load_libebur128()
    mode = DEFAULT_MODE | (EBUR128_MODE_HISTOGRAM if use_histogram else 0)
    state = ctypes.c_void_p(lib.ebur128_init(channels, sample_rate, mode))
    if not state:
        raise RuntimeError("libebur128 init failed")
    process_start = time.perf_counter()
    try:
        for offset in range(0, frames, chunk_frames):
            chunk = np.ascontiguousarray(data[offset : offset + chunk_frames], dtype=np.float32)
            ptr = chunk.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            check(lib.ebur128_add_frames_float(state, ptr, chunk.shape[0]), "add_frames")
        integrated = ctypes.c_double()
        loudness_range = ctypes.c_double()
        threshold = ctypes.c_double()
        check(lib.ebur128_loudness_global(state, ctypes.byref(integrated)), "loudness_global")
        check(lib.ebur128_loudness_range(state, ctypes.byref(loudness_range)), "loudness_range")
        check(lib.ebur128_relative_threshold(state, ctypes.byref(threshold)), "relative_threshold")
        true_peaks = []
        for channel in range(channels):
            peak = ctypes.c_double()
            check(lib.ebur128_true_peak(state, channel, ctypes.byref(peak)), "true_peak")
            true_peaks.append(peak.value)
    finally:
        lib.ebur128_destroy(ctypes.byref(state))
    process_sec = time.perf_counter() - process_start
    total_sec = time.perf_counter() - start
    max_true_peak = max(true_peaks) if true_peaks else 0.0
    return {
        "engine": "libebur128",
        "input_i": round(integrated.value, 3),
        "input_tp": round(amplitude_to_db(max_true_peak), 3),
        "input_lra": round(loudness_range.value, 3),
        "input_thresh": round(threshold.value, 3),
        "sample_rate": sample_rate,
        "channels": channels,
        "frames": frames,
        "read_sec": round(read_sec, 3),
        "process_sec": round(process_sec, 3),
        "elapsed_sec": round(total_sec, 3),
    }


def parse_loudnorm_json(text: str) -> dict[str, float | str]:
    match = re.search(r"\{[\s\S]*?\}", text)
    if not match:
        raise RuntimeError(f"Could not find loudnorm JSON in ffmpeg output:\n{text[-2000:]}")
    raw = json.loads(match.group(0))
    parsed: dict[str, float | str] = {}
    for key, value in raw.items():
        try:
            parsed[key] = float(value)
        except (TypeError, ValueError):
            parsed[key] = value
    return parsed


def ffmpeg_loudnorm_measure(path: Path, target_i: float, target_tp: float, target_lra: float) -> dict[str, float | str]:
    start = time.perf_counter()
    proc = subprocess.run(
        [
            command_path("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
            "-f",
            "null",
            "-",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    measured = parse_loudnorm_json(proc.stderr)
    measured["engine"] = "ffmpeg_loudnorm"
    measured["elapsed_sec"] = round(time.perf_counter() - start, 3)
    return measured


def compare(fast: dict[str, float | str], ffmpeg: dict[str, float | str]) -> dict[str, float]:
    return {
        "input_i": round(float(fast["input_i"]) - float(ffmpeg["input_i"]), 3),
        "input_tp": round(float(fast["input_tp"]) - float(ffmpeg["input_tp"]), 3),
        "input_lra": round(float(fast["input_lra"]) - float(ffmpeg["input_lra"]), 3),
        "input_thresh": round(float(fast["input_thresh"]) - float(ffmpeg["input_thresh"]), 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe libebur128 loudness against FFmpeg loudnorm.")
    parser.add_argument("audio", type=Path)
    parser.add_argument("--target-i", type=float, default=-12.5)
    parser.add_argument("--target-tp", type=float, default=-0.8)
    parser.add_argument("--target-lra", type=float, default=14.0)
    parser.add_argument("--histogram", action="store_true")
    parser.add_argument("--compare-ffmpeg", action="store_true")
    args = parser.parse_args()

    fast = libebur128_measure(args.audio, use_histogram=args.histogram)
    out: dict[str, object] = {"libebur128": fast}
    if args.compare_ffmpeg:
        ffmpeg = ffmpeg_loudnorm_measure(args.audio, args.target_i, args.target_tp, args.target_lra)
        out["ffmpeg_loudnorm"] = ffmpeg
        out["delta_fast_minus_ffmpeg"] = compare(fast, ffmpeg)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
