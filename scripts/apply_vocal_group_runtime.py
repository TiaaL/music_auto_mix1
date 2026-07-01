#!/usr/bin/env python3
"""Apply vocal_group_fx through the runtime-parameter dynamic library.

这个入口只负责空间效果器本身：输入/输出与发送路径固定为 0.1 rack，
每首歌只从 plan 读取白名单参数，不在这里做人声/伴奏融合。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_LIB = ROOT / "build" / "libvocal_group_fx_runtime.dylib"


class VocalGroupFxParams(ctypes.Structure):
    _fields_ = [
        ("rverb_send_pre_db", ctypes.c_float),
        ("rverb_time_s", ctypes.c_float),
        ("rverb_predelay_ms", ctypes.c_float),
        ("rverb_early_ref_db", ctypes.c_float),
        ("rverb_damp", ctypes.c_float),
        ("rverb_eq_hi_gain_db", ctypes.c_float),
        ("supertap_send_pre_db", ctypes.c_float),
        ("supertap_gain_db", ctypes.c_float),
        ("supertap_feedback", ctypes.c_float),
        ("supertap_width", ctypes.c_float),
        ("supertap_color_hz", ctypes.c_float),
        ("shimmer_send_pre_db", ctypes.c_float),
        ("shimmer_gain_db", ctypes.c_float),
    ]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def spatial_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return ((plan.get("reference") or {}).get("overrides") or {}).get("spatial_fx") or {}


def clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def params_from_spatial(spatial: dict[str, Any]) -> tuple[VocalGroupFxParams, dict[str, float]]:
    reverb = spatial.get("reverb") or {}
    delay = spatial.get("delay") or {}
    shimmer = spatial.get("shimmer") or {}
    params = {
        "rverb_send_pre_db": clamp(float(reverb.get("send_pre_db", -12.5)), -60.0, -12.5),
        "rverb_time_s": clamp(float(reverb.get("time_s", 1.75)), 0.30, 1.75),
        "rverb_predelay_ms": clamp(float(reverb.get("predelay_ms", 12.0)), 0.0, 12.0),
        "rverb_early_ref_db": clamp(float(reverb.get("early_ref_db", -2.0)), -24.0, -2.0),
        "rverb_damp": clamp(float(reverb.get("damp", 0.35)), 0.0, 1.0),
        "rverb_eq_hi_gain_db": clamp(float(reverb.get("eq_hi_gain_db", -4.0)), -24.0, -4.0),
        "supertap_send_pre_db": clamp(float(delay.get("send_pre_db", -27.0)), -80.0, -27.0),
        "supertap_gain_db": clamp(float(delay.get("gain_db", -18.5)), -80.0, -18.5),
        "supertap_feedback": clamp(float(delay.get("feedback", 0.10)), 0.0, 0.10),
        "supertap_width": clamp(float(delay.get("width", 0.45)), 0.0, 0.45),
        "supertap_color_hz": clamp(float(delay.get("color_hz", 2400.0)), 400.0, 2400.0),
        "shimmer_send_pre_db": clamp(float(shimmer.get("send_pre_db", -18.0)), -80.0, -18.0),
        "shimmer_gain_db": clamp(float(shimmer.get("gain_db", -18.0)), -80.0, -18.0),
    }
    return VocalGroupFxParams(**params), params


def ensure_library(path: Path) -> None:
    if path.exists():
        return
    cmd = [sys.executable, str(ROOT / "scripts" / "build_vocal_group_runtime.py"), "--output", str(path)]
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "failed to build vocal_group_fx runtime dylib\n"
            + proc.stdout
            + proc.stderr
        )


def load_library(path: Path) -> ctypes.CDLL:
    lib = ctypes.CDLL(str(path))
    lib.vocal_group_fx_runtime_version.restype = ctypes.c_char_p
    lib.vocal_group_fx_create.argtypes = [ctypes.c_int]
    lib.vocal_group_fx_create.restype = ctypes.c_void_p
    lib.vocal_group_fx_destroy.argtypes = [ctypes.c_void_p]
    lib.vocal_group_fx_set_params.argtypes = [ctypes.c_void_p, ctypes.POINTER(VocalGroupFxParams)]
    lib.vocal_group_fx_set_params.restype = ctypes.c_int
    lib.vocal_group_fx_process.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
    ]
    lib.vocal_group_fx_process.restype = ctypes.c_int
    return lib


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply runtime vocal_group_fx dylib.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--library", type=Path, default=DEFAULT_LIB)
    args = parser.parse_args()

    plan = load_json(args.plan)
    spatial = spatial_plan(plan)
    if not (spatial.get("enabled") and spatial.get("applied_to_render")):
        raise SystemExit(spatial.get("reason") or "spatial_fx_not_enabled")

    params_struct, params_dict = params_from_spatial(spatial)
    ensure_library(args.library)
    lib = load_library(args.library)

    audio, sr = sf.read(args.input_wav, dtype="float32", always_2d=True)
    if audio.shape[1] != 1:
        raise SystemExit(f"runtime vocal_group_fx expects mono input, got {audio.shape[1]} channels")
    mono = np.ascontiguousarray(audio[:, 0], dtype=np.float32)
    left = np.zeros_like(mono)
    right = np.zeros_like(mono)

    handle = lib.vocal_group_fx_create(int(sr))
    if not handle:
        raise SystemExit("failed to create vocal_group_fx runtime handle")
    try:
        if not lib.vocal_group_fx_set_params(handle, ctypes.byref(params_struct)):
            raise SystemExit("failed to set vocal_group_fx runtime params")
        ok = lib.vocal_group_fx_process(
            handle,
            mono.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            left.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            right.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            int(mono.shape[0]),
        )
        if not ok:
            raise SystemExit("vocal_group_fx runtime processing failed")
    finally:
        lib.vocal_group_fx_destroy(handle)

    out = np.column_stack([left, right]).astype(np.float32, copy=False)
    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.output_wav, out, int(sr), subtype="FLOAT")

    if args.metadata:
        write_json(args.metadata, {
            "enabled": True,
            "runtime": "dylib",
            "library": str(args.library),
            "version": lib.vocal_group_fx_runtime_version().decode("utf-8", errors="replace"),
            "params": params_dict,
            "spatial_fx": spatial,
            "plan": str(args.plan),
            "policy": (
                "0.1 mono-in/stereo-out and dry/early/reverb/shimmer/delay send path are locked; "
                "only whitelisted effect parameters are runtime-controlled."
            ),
        })
    print(f"[vocal-group-runtime] dylib={args.library} frames={mono.shape[0]} sr={sr}")


if __name__ == "__main__":
    main()
