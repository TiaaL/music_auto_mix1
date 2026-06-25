#!/usr/bin/env python3
"""对比最终人声效果和原曲人声 stem。

原则必须拆开：
- 人声音色相似度追「音色筛选片段」，用于干声/音色 EQ。
- 纵深、混响、动态、宽度和效果高频追「原曲人声 stem」，用于最终人声贡献轨审计。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from analyze_reference import (
    active_intervals_from_vocal,
    delay_proxy,
    load_audio_as_float,
    reverb_proxy,
    spectral_envelope_for_intervals,
    tonal_balance_for_intervals,
    vocal_dynamic_profile,
)
from audit_active_spatial_lift import compare_to_reference, ms_metrics, recommendation


def load_summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def scalar_delta(candidate: dict[str, Any], reference: dict[str, Any], keys: tuple[str, ...]) -> dict[str, float]:
    deltas: dict[str, float] = {}
    for key in keys:
        if key in candidate and key in reference:
            deltas[f"{key}_error"] = round(float(candidate[key]) - float(reference[key]), 3)
    return deltas


def envelope_map(envelope: dict[str, Any]) -> dict[str, float]:
    return {
        str(band.get("id")): float(band.get("db", 0.0))
        for band in envelope.get("bands", [])
        if band.get("id") is not None
    }


def envelope_delta(candidate: dict[str, Any], reference: dict[str, Any]) -> list[dict[str, float | str]]:
    cand = envelope_map(candidate)
    ref = envelope_map(reference)
    rows: list[dict[str, float | str]] = []
    for band_id in sorted(set(cand) & set(ref), key=lambda value: float(value.replace("env_", ""))):
        rows.append({
            "id": band_id,
            "candidate_db": round(cand[band_id], 3),
            "reference_db": round(ref[band_id], 3),
            "error_db": round(cand[band_id] - ref[band_id], 3),
        })
    return rows


def build_recommendations(errors: dict[str, Any], spatial_rec: dict[str, Any]) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    action = spatial_rec.get("action")
    if action and action != "keep_current_vocal_group":
        actions.append({
            "area": "depth_width",
            "action": action,
            "reason": spatial_rec.get("reason"),
        })

    reverb = errors.get("reverb", {})
    tail_error = float(reverb.get("tail_to_onset_ratio_db_error", 0.0))
    rt_error = float(reverb.get("est_rt60_ms_error", 0.0))
    if tail_error > 1.8 or rt_error > 220.0:
        actions.append({
            "area": "reverb_depth",
            "action": "reduce_vocal_group_wet_time_or_high_return",
            "reason": "candidate_vocal_group_is_wetter_or_deeper_than_reference_vocal",
            "tail_to_onset_ratio_db_error": round(tail_error, 3),
            "est_rt60_ms_error": round(rt_error, 3),
        })
    elif tail_error < -2.5 and rt_error < -180.0:
        actions.append({
            "area": "reverb_depth",
            "action": "consider_bounded_wet_or_time_lift",
            "reason": "candidate_vocal_group_is_drier_than_reference_vocal",
            "tail_to_onset_ratio_db_error": round(tail_error, 3),
            "est_rt60_ms_error": round(rt_error, 3),
        })

    dynamics = errors.get("dynamics", {})
    active_rms_error = float(dynamics.get("active_rms_db_error", 0.0))
    peak_error = float(dynamics.get("peak_db_error", 0.0))
    frame_range_error = float(dynamics.get("frame_range_p90_p10_db_error", 0.0))
    if active_rms_error < -2.0 and peak_error < -1.5:
        actions.append({
            "area": "dynamics_punch",
            "action": "increase_bounded_micro_dynamic_presence_not_bus_loudness",
            "reason": "candidate_vocal_active_level_and_peak_are_below_reference_vocal",
            "active_rms_db_error": round(active_rms_error, 3),
            "peak_db_error": round(peak_error, 3),
        })
    if frame_range_error < -1.6:
        actions.append({
            "area": "dynamics_shape",
            "action": "restore_bounded_short_frame_contrast",
            "reason": "candidate_vocal_frame_contrast_is_flatter_than_reference_vocal",
            "frame_range_p90_p10_db_error": round(frame_range_error, 3),
        })
    if frame_range_error > 2.2:
        actions.append({
            "area": "dynamics_stability",
            "action": "smooth_section_or_timeline_vocal_gain",
            "reason": "candidate_vocal_frame_contrast_is_more_unstable_than_reference_vocal",
            "frame_range_p90_p10_db_error": round(frame_range_error, 3),
        })

    tonal = errors.get("tonal_balance", {})
    high_error = max(
        float(tonal.get("upper_error", 0.0)),
        float(tonal.get("harsh_error", 0.0)),
        float(tonal.get("sib_error", 0.0)),
        float(tonal.get("air_error", 0.0)),
    )
    if high_error > 1.8:
        actions.append({
            "area": "effect_brightness",
            "action": "reduce_post_fx_presence_or_reverb_high_return",
            "reason": "candidate_vocal_effect_highs_are_above_reference_vocal",
            "max_high_error_db": round(high_error, 3),
        })
    return actions


def build_report(reference_vocal: Path, candidate_vocal_group: Path, reference_audio: Path | None = None) -> dict[str, Any]:
    ref_audio, ref_sr = load_audio_as_float(reference_vocal)
    cand_audio, cand_sr = load_audio_as_float(candidate_vocal_group)
    intervals = active_intervals_from_vocal(ref_audio, ref_sr)
    coverage_sec = sum(end - start for start, end in intervals)

    reference = {
        "spatial": ms_metrics(reference_vocal, intervals),
        "dynamics": vocal_dynamic_profile(ref_audio, ref_sr, intervals),
        "reverb": reverb_proxy(ref_audio, ref_sr),
        "delay": delay_proxy(ref_audio, ref_sr),
        "tonal_balance": tonal_balance_for_intervals(ref_audio, ref_sr, intervals),
        "spectral_envelope": spectral_envelope_for_intervals(ref_audio, ref_sr, intervals),
    }
    candidate = {
        "spatial": ms_metrics(candidate_vocal_group, intervals),
        "dynamics": vocal_dynamic_profile(cand_audio, cand_sr, intervals),
        "reverb": reverb_proxy(cand_audio, cand_sr),
        "delay": delay_proxy(cand_audio, cand_sr),
        "tonal_balance": tonal_balance_for_intervals(cand_audio, cand_sr, intervals),
        "spectral_envelope": spectral_envelope_for_intervals(cand_audio, cand_sr, intervals),
    }

    spatial_rec = recommendation(candidate["spatial"], reference["spatial"])
    errors = {
        "spatial": compare_to_reference(candidate["spatial"], reference["spatial"]),
        "dynamics": scalar_delta(
            candidate["dynamics"],
            reference["dynamics"],
            (
                "active_rms_db",
                "peak_db",
                "crest_db",
                "frame_range_p90_p10_db",
                "micro_range_p95_p50_db",
                "micro_range_p99_p50_db",
            ),
        ),
        "reverb": scalar_delta(
            candidate["reverb"],
            reference["reverb"],
            ("tail_to_onset_ratio_db", "est_rt60_ms", "confidence"),
        ),
        "delay": scalar_delta(candidate["delay"], reference["delay"], ("peak_corr", "peak_lag_ms", "confidence")),
        "tonal_balance": scalar_delta(
            candidate["tonal_balance"],
            reference["tonal_balance"],
            ("sub", "low", "lowmid", "mid", "upper", "harsh", "sib", "air"),
        ),
        "spectral_envelope": envelope_delta(candidate["spectral_envelope"], reference["spectral_envelope"]),
    }
    return {
        "reference_vocal": str(reference_vocal),
        "candidate_vocal_group": str(candidate_vocal_group),
        "reference_audio": str(reference_audio) if reference_audio else None,
        "target_policy": {
            "timbre": "音色相似度只追音色筛选片段，不使用本报告裁判。",
            "effects": "动态、纵深、混响、宽度和效果高频追原曲人声 stem。",
            "candidate_stage": "候选轨应为最终入 stereo sum 的人声贡献，包含 bus/section 动态后的人声效果。",
        },
        "active_regions": {
            "count": len(intervals),
            "coverage_sec": round(coverage_sec, 3),
            "basis": "reference_vocal_active_regions",
        },
        "reference": reference,
        "candidate": candidate,
        "errors": errors,
        "recommendations": build_recommendations(errors, spatial_rec),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit final vocal_group effects against the original vocal stem.")
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--reference-vocal", type=Path)
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--candidate-vocal-group", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    reference_vocal = args.reference_vocal
    reference_audio = args.reference_audio
    candidate = args.candidate_vocal_group
    if args.summary_json:
        summary = load_summary(args.summary_json)
        reference_used = summary.get("reference_used") or {}
        reference_vocal = reference_vocal or (Path(reference_used["vocal"]) if reference_used.get("vocal") else None)
        reference_audio = reference_audio or (
            Path(reference_used["full_mix"]) if reference_used.get("full_mix") else None
        )
        candidate = candidate or (Path(summary["vocal_group_output"]) if summary.get("vocal_group_output") else None)

    if reference_vocal is None:
        raise SystemExit("Provide --reference-vocal or --summary-json with reference_used.vocal.")
    if candidate is None:
        raise SystemExit("Provide --candidate-vocal-group or --summary-json with vocal_group_output.")

    report = build_report(reference_vocal, candidate, reference_audio=reference_audio)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
