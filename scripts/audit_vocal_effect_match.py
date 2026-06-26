#!/usr/bin/env python3
"""对比最终人声效果和原曲人声 stem。

原则必须拆开：
- 人声音色相似度追「音色筛选片段」，用于干声/音色 EQ。
- 纵深、混响、动态、宽度、delay 追「原曲人声 stem」，用于最终人声贡献轨审计。
- 频段差异只保留诊断，不再生成 effect_brightness 建议；音色允许和原唱不同。
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


def intervals_from_reference_features(features: dict[str, Any] | None) -> list[tuple[float, float]]:
    rows = (((features or {}).get("active_vocal_regions") or {}).get("regions") or [])
    intervals: list[tuple[float, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        start = row.get("start")
        end = row.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and end > start:
            intervals.append((float(start), float(end)))
    return intervals


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
    # 混响不能再用一个 reverb_depth 粗暴概括：
    # tail 是“尾巴量/湿度”，RT60 是“持续时间/纵深”。两者方向相反时只做诊断，不自动加减混响。
    tail_too_much = tail_error > 1.2
    tail_too_little = tail_error < -1.6
    decay_too_long = rt_error > 220.0
    decay_too_short = rt_error < -180.0
    if tail_too_much and decay_too_long:
        actions.append({
            "area": "reverb_tail_amount",
            "action": "reduce_vocal_group_wet_or_return_level",
            "reason": "candidate_reverb_tail_amount_is_above_reference",
            "tail_to_onset_ratio_db_error": round(tail_error, 3),
        })
        actions.append({
            "area": "reverb_decay_time",
            "action": "shorten_bounded_vocal_group_decay",
            "reason": "candidate_reverb_decay_is_longer_than_reference",
            "est_rt60_ms_error": round(rt_error, 3),
        })
    elif tail_too_little and decay_too_short:
        actions.append({
            "area": "reverb_tail_amount",
            "action": "consider_bounded_wet_or_return_lift",
            "reason": "candidate_reverb_tail_amount_is_below_reference",
            "tail_to_onset_ratio_db_error": round(tail_error, 3),
        })
        actions.append({
            "area": "reverb_decay_time",
            "action": "consider_bounded_decay_lift",
            "reason": "candidate_reverb_decay_is_shorter_than_reference",
            "est_rt60_ms_error": round(rt_error, 3),
        })
    elif (tail_too_much or tail_too_little) and (decay_too_long or decay_too_short):
        actions.append({
            "area": "reverb_proxy_conflict",
            "action": "diagnostic_only_do_not_auto_change_wet",
            "reason": "tail_amount_and_decay_time_disagree",
            "tail_to_onset_ratio_db_error": round(tail_error, 3),
            "est_rt60_ms_error": round(rt_error, 3),
        })

    dynamics = errors.get("dynamics", {})
    frame_range_error = float(dynamics.get("frame_range_p90_p10_db_error", 0.0))
    micro_p95_error = float(dynamics.get("micro_range_p95_p50_db_error", 0.0))
    micro_p99_error = float(dynamics.get("micro_range_p99_p50_db_error", 0.0))
    crest_error = float(dynamics.get("crest_db_error", 0.0))
    if micro_p95_error < -1.4 or micro_p99_error < -2.2 or (frame_range_error < -1.2 and crest_error < -1.0):
        actions.append({
            "area": "dynamics_punch",
            "action": "increase_bounded_micro_dynamic_contrast_not_bus_loudness",
            "reason": "candidate_vocal_short_frame_dynamics_are_flatter_than_reference_vocal",
            "micro_range_p95_p50_db_error": round(micro_p95_error, 3),
            "micro_range_p99_p50_db_error": round(micro_p99_error, 3),
            "frame_range_p90_p10_db_error": round(frame_range_error, 3),
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

    # 频段差异不再当作效果错误：不同歌手/音色片段本来就可能和原唱不同。
    # 高频、齿音、刺耳问题应由干声瑕疵检测和音色筛选片段约束；本审计只把 tonal
    # 数据留在 errors 里辅助排查，不生成 effect_brightness 这类主流程建议。
    return actions


def reverb_axis_summary(
    reference: dict[str, Any],
    candidate: dict[str, Any],
    errors: dict[str, Any],
) -> dict[str, Any]:
    """把混响拆成尾巴量和持续时间两条轴，避免一个 depth 结论误导后续策略。"""
    reverb = errors.get("reverb", {})
    tail_error = float(reverb.get("tail_to_onset_ratio_db_error", 0.0))
    rt_error = float(reverb.get("est_rt60_ms_error", 0.0))
    tail_state = "match"
    if tail_error > 1.2:
        tail_state = "candidate_more_tail_amount"
    elif tail_error < -1.6:
        tail_state = "candidate_less_tail_amount"
    decay_state = "match"
    if rt_error > 220.0:
        decay_state = "candidate_longer_decay"
    elif rt_error < -180.0:
        decay_state = "candidate_shorter_decay"
    conflict = tail_state != "match" and decay_state != "match" and tail_error * rt_error < 0.0
    return {
        "tail_amount": {
            "reference_tail_to_onset_ratio_db": reference["reverb"].get("tail_to_onset_ratio_db"),
            "candidate_tail_to_onset_ratio_db": candidate["reverb"].get("tail_to_onset_ratio_db"),
            "error_db": round(tail_error, 3),
            "state": tail_state,
        },
        "decay_time": {
            "reference_est_rt60_ms": reference["reverb"].get("est_rt60_ms"),
            "candidate_est_rt60_ms": candidate["reverb"].get("est_rt60_ms"),
            "error_ms": round(rt_error, 3),
            "state": decay_state,
        },
        "proxy_conflict": bool(conflict),
        "policy": (
            "tail_amount 控制 wet/return，decay_time 控制 time；两轴方向相反时只报诊断，不自动加减混响。"
        ),
    }


def build_reference_metrics(
    reference_vocal: Path,
    intervals: list[tuple[float, float]],
    reference_features: dict[str, Any] | None,
) -> dict[str, Any]:
    # 性能：原曲人声动态/混响/包络在 analyze_reference 阶段已经计算过。
    # 审计优先复用 plan 里的 reference.features，只在缺字段时回退到重新分析音频。
    if reference_features:
        missing = [
            key
            for key in ("vocal_dynamics", "reverb_proxy", "delay_proxy", "vocal_tonal_balance", "vocal_spectral_envelope")
            if not reference_features.get(key)
        ]
        if not missing:
            return {
                "spatial": ms_metrics(reference_vocal, intervals),
                "dynamics": reference_features["vocal_dynamics"],
                "reverb": reference_features["reverb_proxy"],
                "delay": reference_features["delay_proxy"],
                "tonal_balance": reference_features["vocal_tonal_balance"],
                "spectral_envelope": reference_features["vocal_spectral_envelope"],
                "reused_reference_features": True,
            }

    ref_audio, ref_sr = load_audio_as_float(reference_vocal)
    return {
        "spatial": ms_metrics(reference_vocal, intervals),
        "dynamics": vocal_dynamic_profile(ref_audio, ref_sr, intervals),
        "reverb": reverb_proxy(ref_audio, ref_sr),
        "delay": delay_proxy(ref_audio, ref_sr),
        "tonal_balance": tonal_balance_for_intervals(ref_audio, ref_sr, intervals),
        "spectral_envelope": spectral_envelope_for_intervals(ref_audio, ref_sr, intervals),
        "reused_reference_features": False,
    }


def build_report(
    reference_vocal: Path,
    candidate_vocal_group: Path,
    reference_audio: Path | None = None,
    reference_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    intervals = intervals_from_reference_features(reference_features)
    if intervals:
        ref_sr = 48000
    else:
        ref_audio, ref_sr = load_audio_as_float(reference_vocal)
        intervals = active_intervals_from_vocal(ref_audio, ref_sr)
    cand_audio, cand_sr = load_audio_as_float(candidate_vocal_group)
    coverage_sec = sum(end - start for start, end in intervals)

    reference = build_reference_metrics(reference_vocal, intervals, reference_features)
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
            "effects": "人声靠前/靠后、动态、纵深、混响、宽度和 delay 追原曲人声 stem。",
            "tonal_balance": "频段差异仅作诊断，不作为效果建议；不同歌手音色不需要贴原曲人声频谱。",
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
        "reverb_match": reverb_axis_summary(reference, candidate, errors),
        "recommendations": build_recommendations(errors, spatial_rec),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit final vocal_group effects against the original vocal stem.")
    parser.add_argument("--summary-json", type=Path)
    parser.add_argument("--plan", type=Path, help="resolved_mix_plan.json；优先复用其中的 reference.features")
    parser.add_argument("--reference-vocal", type=Path)
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--candidate-vocal-group", type=Path)
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    reference_vocal = args.reference_vocal
    reference_audio = args.reference_audio
    candidate = args.candidate_vocal_group
    reference_features = None
    if args.summary_json:
        summary = load_summary(args.summary_json)
        reference_used = summary.get("reference_used") or {}
        reference_vocal = reference_vocal or (Path(reference_used["vocal"]) if reference_used.get("vocal") else None)
        reference_audio = reference_audio or (
            Path(reference_used["full_mix"]) if reference_used.get("full_mix") else None
        )
        candidate = candidate or (Path(summary["vocal_group_output"]) if summary.get("vocal_group_output") else None)
        if args.plan is None and summary.get("resolved_mix_plan"):
            args.plan = Path(summary["resolved_mix_plan"])
    if args.plan is not None and args.plan.exists():
        plan = load_summary(args.plan)
        reference_features = ((plan.get("reference") or {}).get("features") or None)

    if reference_vocal is None:
        raise SystemExit("Provide --reference-vocal or --summary-json with reference_used.vocal.")
    if candidate is None:
        raise SystemExit("Provide --candidate-vocal-group or --summary-json with vocal_group_output.")

    report = build_report(
        reference_vocal,
        candidate,
        reference_audio=reference_audio,
        reference_features=reference_features,
    )
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
