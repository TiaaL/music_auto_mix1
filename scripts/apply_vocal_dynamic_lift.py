#!/usr/bin/env python3
"""按 resolved mix plan 对人声做保守的微动态修正。

这个脚本只在人声明显比参考 stem 更“平”时启用：轻微拉开短帧动态，
让重音/弱音更接近参考表现。它不改变整体响度目标，也不负责音色匹配。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def vocal_dynamic_block(plan: dict[str, Any]) -> dict[str, Any]:
    """从 plan 里取出参考驱动的人声动态策略。"""
    overrides = ((plan.get("reference") or {}).get("overrides") or {})
    return overrides.get("vocal_dynamics") or {}


def db(value: float | np.ndarray) -> float | np.ndarray:
    return 20.0 * np.log10(np.maximum(value, 1e-8))


def lin(db_value: float | np.ndarray) -> float | np.ndarray:
    return np.power(10.0, np.asarray(db_value) / 20.0)


def mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1)


# 脚本侧硬上限：动态“没劲”只做微动态增强，不改变整体响度目标。
# 即使 plan 里参数异常，也不能突破这些值。
HARD_MAX_LIFT_DB = 1.6
HARD_MAX_CUT_DB = 0.7
HARD_MAX_CONTRAST_AMOUNT = 0.30


def rms_frames(samples: np.ndarray, sr: int, frame_ms: float, hop_ms: float) -> tuple[np.ndarray, np.ndarray]:
    """按短帧计算 RMS，用来估计人声的微动态起伏。"""
    frame = max(128, int(round(sr * frame_ms / 1000.0)))
    hop = max(64, int(round(sr * hop_ms / 1000.0)))
    if samples.size < frame:
        return np.array([0.0]), np.array([float(np.sqrt(np.mean(samples**2) + 1e-12))])
    starts = np.arange(0, samples.size - frame + 1, hop)
    values = np.empty(starts.size, dtype=np.float64)
    window = np.hanning(frame)
    norm = max(float(np.mean(window**2)), 1e-12)
    for idx, start in enumerate(starts):
        chunk = samples[start : start + frame] * window
        values[idx] = math.sqrt(float(np.mean(chunk**2)) / norm + 1e-12)
    times = (starts + frame * 0.5) / sr
    return times, values


def smooth_curve(curve: np.ndarray, sr: int, attack_ms: float, release_ms: float) -> np.ndarray:
    """平滑增益曲线，避免逐帧增益变化带来抽动或毛刺。"""
    attack = math.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    release = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    out = np.empty_like(curve)
    prev = float(curve[0])
    for idx, target_value in enumerate(curve):
        target = float(target_value)
        coeff = attack if abs(target) > abs(prev) else release
        prev = coeff * prev + (1.0 - coeff) * target
        out[idx] = prev
    return out


def build_gain_curve(audio: np.ndarray, sr: int, params: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """把短帧动态差异转成逐采样增益曲线。

    曲线以活动人声帧的中位数为中心，只做小幅度对比增强；
    非活动区保持 0 dB，避免把噪声底抬起来。
    """
    x = mono(audio)
    n = x.size
    frame_ms = float(params.get("frame_ms") or 50.0)
    hop_ms = float(params.get("hop_ms") or 25.0)
    times, rms = rms_frames(x, sr, frame_ms, hop_ms)
    frame_db = np.asarray(db(rms), dtype=np.float64)
    active_floor = max(-58.0, float(np.percentile(frame_db, 75)) - 18.0)
    active = frame_db >= active_floor
    if not np.any(active):
        return np.zeros(n, dtype=np.float64), {
            "active_frame_count": 0,
            "active_floor_db": round(active_floor, 3),
            "reason": "no active frames",
        }

    active_db = frame_db[active]
    center_db = float(np.percentile(active_db, 50))
    # 弱人声“没劲”在这里处理的是微动态，不是响度。
    # 即使 plan 写入异常参数，脚本侧仍按硬上限截断。
    contrast_amount = min(max(float(params.get("contrast_amount") or 0.0), 0.0), HARD_MAX_CONTRAST_AMOUNT)
    max_lift_db = min(max(float(params.get("max_lift_db") or 0.0), 0.0), HARD_MAX_LIFT_DB)
    max_cut_db = min(max(float(params.get("max_cut_db") or 0.0), 0.0), HARD_MAX_CUT_DB)

    frame_gain = (frame_db - center_db) * contrast_amount
    frame_gain = np.where(active, frame_gain, 0.0)
    active_gain = frame_gain[active]
    if active_gain.size:
        frame_gain[active] -= float(np.median(active_gain))
    frame_gain = np.clip(frame_gain, -max_cut_db, max_lift_db)
    sample_times = np.arange(n, dtype=np.float64) / sr
    gain_curve = np.interp(sample_times, times, frame_gain, left=0.0, right=0.0)
    gain_curve = smooth_curve(
        gain_curve,
        sr,
        float(params.get("attack_ms") or 35.0),
        float(params.get("release_ms") or 160.0),
    )
    active_curve = np.interp(sample_times, times, active.astype(np.float64), left=0.0, right=0.0) >= 0.5
    if np.any(active_curve):
        gain_curve -= float(np.median(gain_curve[active_curve]))
        gain_curve = np.clip(gain_curve, -max_cut_db, max_lift_db)
    stats = {
        "active_frame_count": int(active_db.size),
        "active_floor_db": round(active_floor, 3),
        "center_db": round(center_db, 3),
        "gain_db_p10": round(float(np.percentile(gain_curve, 10)), 3),
        "gain_db_p50": round(float(np.percentile(gain_curve, 50)), 3),
        "gain_db_p90": round(float(np.percentile(gain_curve, 90)), 3),
        "gain_db_min": round(float(np.min(gain_curve)), 3),
        "gain_db_max": round(float(np.max(gain_curve)), 3),
        "active_fraction": round(float(np.mean(active)), 4),
        "hard_caps": {
            "contrast_amount": HARD_MAX_CONTRAST_AMOUNT,
            "max_lift_db": HARD_MAX_LIFT_DB,
            "max_cut_db": HARD_MAX_CUT_DB,
        },
    }
    return gain_curve, stats


def process(audio: np.ndarray, sr: int, block: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    """应用微动态曲线，并在输出前做峰值保护。"""
    params = block.get("processing") or {}
    gain_curve, curve_stats = build_gain_curve(audio, sr, params)
    out = audio * lin(gain_curve)[:, None]
    peak_before_trim = float(np.max(np.abs(out))) if out.size else 0.0
    safety_trim_db = 0.0
    ceiling = float(params.get("peak_ceiling") or 0.97)
    if peak_before_trim > ceiling > 0.0:
        scale = ceiling / peak_before_trim
        out *= scale
        safety_trim_db = 20.0 * math.log10(scale)
    report = {
        "enabled": True,
        "mode": block.get("mode"),
        "params": params,
        "gap": block.get("gap"),
        "curve": curve_stats,
        "input_peak": round(float(np.max(np.abs(audio))) if audio.size else 0.0, 6),
        "peak_before_trim": round(peak_before_trim, 6),
        "safety_trim_db": round(safety_trim_db, 3),
        "policy": block.get("policy"),
    }
    return np.clip(out, -1.0, 1.0), report


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply conservative plan-driven vocal micro-dynamics.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, default=None)
    args = parser.parse_args()

    plan = load_json(args.plan)
    block = vocal_dynamic_block(plan)
    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "enabled": bool(block.get("enabled")),
        "mode": block.get("mode", "light_vocal_dynamic_lift"),
        "gap": block.get("gap"),
        "policy": block.get("policy"),
    }
    if not block.get("enabled"):
        # plan 没有明确要求时只透传，保证新增阶段不会无条件改声音。
        shutil.copyfile(args.input_wav, args.output_wav)
        metadata["skipped"] = True
        metadata["reason"] = block.get("reason", "vocal dynamics not enabled")
        if args.metadata:
            write_json(args.metadata, metadata)
        print("[vocal-dynamic-lift] skipped")
        return

    audio, sr = sf.read(args.input_wav, always_2d=True, dtype="float64")
    out, report = process(audio, int(sr), block)
    sf.write(args.output_wav, out, int(sr), subtype="FLOAT")
    report["skipped"] = False
    if args.metadata:
        write_json(args.metadata, report)
    curve = report.get("curve") or {}
    print(
        "[vocal-dynamic-lift] "
        f"gain {curve.get('gain_db_min')}..{curve.get('gain_db_max')} dB, "
        f"trim {report.get('safety_trim_db')} dB"
    )


if __name__ == "__main__":
    main()
