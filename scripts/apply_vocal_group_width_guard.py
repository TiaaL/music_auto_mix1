#!/usr/bin/env python3
"""按原曲人声空间特征收窄 vocal_group 侧向信息。

这一步不改混响 rack，也不重新塑造音色；它只在原曲人声明显 center-led、
而当前 vocal_group 的 side/mid 比原曲宽很多时，衰减 Side 通道。
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

from audit_active_spatial_lift import ms_metrics


DEFAULT_ALLOWED_EXCESS_DB = 2.0
DEFAULT_MAX_TRIM_DB = 8.0
NEAR_CENTER_SIDE_MID_DB = -24.0


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def lin(db_value: float) -> float:
    return math.pow(10.0, db_value / 20.0)


def reference_intervals(plan: dict[str, Any]) -> list[tuple[float, float]]:
    rows = (
        (((plan.get("reference") or {}).get("features") or {}).get("active_vocal_regions") or {}).get("regions")
        or []
    )
    intervals: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = row.get("start")
        end = row.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            intervals.append((float(start), float(end)))
    return intervals


def reference_spatial(plan: dict[str, Any]) -> dict[str, Any]:
    features = ((plan.get("reference") or {}).get("features") or {})
    return features.get("vocal_spatial_profile") or {}


def planned_max_trim_db(plan: dict[str, Any]) -> float:
    # 统一效果目标里已有 side_trim 建议；这里复用它当硬上限，避免再引入一套宽度策略。
    effect = (
        (((plan.get("vocal_processing_context") or {}).get("vocal_effect_target") or {}).get("spatial") or {})
    )
    value = effect.get("side_trim_db")
    if isinstance(value, (int, float)) and value < 0.0:
        return min(DEFAULT_MAX_TRIM_DB, abs(float(value)))
    return DEFAULT_MAX_TRIM_DB


def apply_side_trim(audio: np.ndarray, trim_db: float) -> tuple[np.ndarray, float]:
    if audio.ndim == 1:
        audio = np.column_stack([audio, audio])
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    stereo = audio[:, :2].astype(np.float64, copy=False)
    left = stereo[:, 0]
    right = stereo[:, 1]
    mid = (left + right) * 0.5
    side = (left - right) * 0.5 * lin(trim_db)
    out = np.column_stack([mid + side, mid - side])
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    safety_trim_db = 0.0
    if peak > 0.98:
        scale = 0.98 / peak
        out *= scale
        safety_trim_db = 20.0 * math.log10(scale)
    return np.clip(out, -1.0, 1.0), safety_trim_db


def process(input_wav: Path, output_wav: Path, plan: dict[str, Any], metadata: Path | None) -> None:
    ref_spatial = reference_spatial(plan)
    intervals = reference_intervals(plan)
    ref_side_mid = ref_spatial.get("active_side_minus_mid_db")
    center_led = bool(ref_spatial.get("near_mono_center_led"))
    output_wav.parent.mkdir(parents=True, exist_ok=True)

    if not isinstance(ref_side_mid, (int, float)) or not intervals:
        shutil.copyfile(input_wav, output_wav)
        report = {
            "enabled": False,
            "skipped": True,
            "reason": "missing_reference_spatial_or_active_regions",
            "policy": "只在有原曲人声空间画像和 active 区间时执行，避免无参考乱收窄。",
        }
        if metadata:
            write_json(metadata, report)
        print("[vocal-group-width-guard] skipped")
        return

    current = ms_metrics(input_wav, intervals)
    current_side_mid = float(current["active_side_minus_mid_db"])
    target_limit = float(ref_side_mid) + DEFAULT_ALLOWED_EXCESS_DB
    side_excess = current_side_mid - target_limit
    should_trim = center_led or float(ref_side_mid) <= NEAR_CENTER_SIDE_MID_DB

    if not should_trim or side_excess <= 0.35:
        shutil.copyfile(input_wav, output_wav)
        report = {
            "enabled": True,
            "skipped": True,
            "reason": "current_width_within_reference_limit",
            "reference_active_side_minus_mid_db": round(float(ref_side_mid), 3),
            "target_limit_db": round(target_limit, 3),
            "current": current,
            "side_excess_db": round(side_excess, 3),
            "policy": "center-led 原曲才收 Side；当前已在参考宽度余量内则不处理。",
        }
        if metadata:
            write_json(metadata, report)
        print("[vocal-group-width-guard] skipped")
        return

    trim_db = -min(planned_max_trim_db(plan), max(0.0, side_excess))
    audio, sr = sf.read(input_wav, always_2d=True, dtype="float64")
    out, safety_trim_db = apply_side_trim(audio, trim_db)
    sf.write(output_wav, out, sr, subtype="FLOAT")
    after = ms_metrics(output_wav, intervals)
    report = {
        "enabled": True,
        "skipped": False,
        "trim_db": round(trim_db, 3),
        "safety_trim_db": round(safety_trim_db, 3),
        "reference_active_side_minus_mid_db": round(float(ref_side_mid), 3),
        "target_limit_db": round(target_limit, 3),
        "side_excess_db": round(side_excess, 3),
        "max_trim_db": round(planned_max_trim_db(plan), 3),
        "before": current,
        "after": after,
        "policy": (
            "保留 0.1 之前 vocal_group 混响 rack，只对超出原曲 center-led 宽度余量的 Side 做硬上限衰减。"
        ),
    }
    if metadata:
        write_json(metadata, report)
    print(f"[vocal-group-width-guard] trim={trim_db:.2f} dB")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply reference-led vocal group width guard.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()

    plan = load_json(args.plan)
    process(args.input_wav, args.output_wav, plan, args.metadata)


if __name__ == "__main__":
    main()
