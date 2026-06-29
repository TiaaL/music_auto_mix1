#!/usr/bin/env python3
"""只读生成 final fusion pass 决策。

核心原则：每首歌只对齐自己的原曲，不按歌名或风格标签硬套参数。
本脚本不写音频、不接渲染链，只把“参考目标 → 当前误差 → 建议修正”
整理成 JSON，供后续 apply_final_fusion_pass.py 或人工讨论使用。
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def rounded(value: Any, digits: int = 3) -> float | None:
    numeric = number(value)
    return round(numeric, digits) if numeric is not None else None


def median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def plan_prefix(plan_path: Path) -> str:
    stem = plan_path.stem
    suffix = "resolved_mix_plan"
    return stem[: -len(suffix)] if stem.endswith(suffix) else stem


def infer_report_path(render_dir: Path | None, plan_path: Path, suffix: str) -> Path | None:
    if render_dir is None:
        return None
    return render_dir / f"mix_{plan_prefix(plan_path)}.{suffix}.json"


def reference_targets(plan: dict[str, Any]) -> dict[str, Any]:
    """从对应原曲提取最终融合目标。

    这些目标来自 reference.features；没有参考时只记录缺失，不生成伪目标。
    """
    ref = (plan.get("reference") or {}).get("features") or {}
    balance = ref.get("vocal_accomp_balance") or {}
    spatial = ref.get("vocal_spatial_profile") or {}
    reverb = ref.get("reverb_proxy") or {}
    dynamics = ref.get("vocal_dynamics") or {}
    sources = ref.get("sources") or {}
    return {
        "available": bool(ref),
        "source_paths": {
            "full_mix": sources.get("full_mix"),
            "vocal": sources.get("vocal"),
            "accomp": sources.get("accomp") or sources.get("provided_accomp"),
        },
        "global_active_gap_db": rounded(
            balance.get("active_vocal_minus_accomp_db", balance.get("vocal_minus_accomp_db")),
            3,
        ),
        "active_vocal_rms_db": rounded(balance.get("active_vocal_rms_db"), 3),
        "active_accomp_rms_db": rounded(balance.get("active_accomp_rms_db"), 3),
        "vocal_width": {
            "active_side_minus_mid_db": rounded(spatial.get("active_side_minus_mid_db"), 3),
            "near_mono_center_led": bool(spatial.get("near_mono_center_led")),
            "lr_correlation_active": rounded(spatial.get("lr_correlation_active"), 5),
        },
        "vocal_reverb": {
            "tail_to_onset_ratio_db": rounded(reverb.get("tail_to_onset_ratio_db"), 3),
            "est_rt60_ms": rounded(reverb.get("est_rt60_ms"), 1),
            "confidence": rounded(reverb.get("confidence"), 3),
        },
        "vocal_dynamics": {
            "frame_range_p90_p10_db": rounded(dynamics.get("frame_range_p90_p10_db"), 3),
            "micro_range_p95_p50_db": rounded(dynamics.get("micro_range_p95_p50_db"), 3),
            "micro_range_p99_p50_db": rounded(dynamics.get("micro_range_p99_p50_db"), 3),
        },
        "policy": "每首只对齐自己的原曲 reference.features；profile 只用于解释，不参与核心误差计算。",
    }


def current_state(
    bus: dict[str, Any],
    duck: dict[str, Any],
    section: dict[str, Any],
    effect_audit: dict[str, Any],
) -> dict[str, Any]:
    events = section.get("events") or []
    deficits = [float(row["deficit_db"]) for row in events if isinstance(row.get("deficit_db"), (int, float))]
    accomp_moves = [float(row["accomp_gain_db"]) for row in events if isinstance(row.get("accomp_gain_db"), (int, float))]
    errors = effect_audit.get("errors") or {}
    return {
        "global_balance": {
            "measured_active_gap_db": rounded(bus.get("active_vocal_minus_accomp_db"), 3),
            "existing_vocal_bus_gain_db": rounded(bus.get("vocal_bus_gain_db"), 3),
            "existing_accomp_bus_gain_db": rounded(bus.get("accomp_bus_gain_db"), 3),
        },
        "duck": {
            "low_p50_db": rounded(duck.get("low_duck_db_active_p50"), 3),
            "low_p90_db": rounded(duck.get("low_duck_db_active_p90"), 3),
            "presence_p50_db": rounded(duck.get("presence_duck_db_active_p50"), 3),
            "presence_p90_db": rounded(duck.get("presence_duck_db_active_p90"), 3),
            "profile": duck.get("profile") or {},
        },
        "section": {
            "event_count": int(section.get("event_count") or 0),
            "peak_extra_vocal_gain_db": rounded(section.get("peak_extra_vocal_gain_db"), 3),
            "peak_extra_accomp_gain_db": rounded(section.get("peak_extra_accomp_gain_db"), 3),
            "median_deficit_db": median(deficits),
            "median_accomp_gain_db": median(accomp_moves),
        },
        "vocal_effect_errors": {
            "spatial": errors.get("spatial") or {},
            "reverb": errors.get("reverb") or {},
            "dynamics": errors.get("dynamics") or {},
            "tonal_balance": errors.get("tonal_balance") or {},
        },
    }


def build_errors(targets: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    target_gap = number(targets.get("global_active_gap_db"))
    current_gap = number((state.get("global_balance") or {}).get("measured_active_gap_db"))
    effect = state.get("vocal_effect_errors") or {}
    spatial = effect.get("spatial") or {}
    reverb = effect.get("reverb") or {}
    dynamics = effect.get("dynamics") or {}
    tonal = effect.get("tonal_balance") or {}
    high_errors = [
        number(tonal.get("upper_error")),
        number(tonal.get("harsh_error")),
        number(tonal.get("sib_error")),
        number(tonal.get("air_error")),
    ]
    high_errors = [value for value in high_errors if value is not None]
    return {
        "global_gap_error_db": (
            round(current_gap - target_gap, 3)
            if current_gap is not None and target_gap is not None
            else None
        ),
        "needed_global_gap_correction_db": (
            round(target_gap - current_gap, 3)
            if current_gap is not None and target_gap is not None
            else None
        ),
        "width_error_db": rounded(spatial.get("active_side_minus_mid_db_error_db"), 3),
        "reverb_rt60_error_ms": rounded(reverb.get("est_rt60_ms_error"), 1),
        "reverb_tail_error_db": rounded(reverb.get("tail_to_onset_ratio_db_error"), 3),
        "frame_range_error_db": rounded(dynamics.get("frame_range_p90_p10_db_error"), 3),
        "micro_p95_error_db": rounded(dynamics.get("micro_range_p95_p50_db_error"), 3),
        "max_effect_high_error_db": round(max(high_errors), 3) if high_errors else None,
    }


def split_global_correction(needed: float | None) -> dict[str, Any]:
    """把 active gap 修正拆成可执行的全局人声/伴奏 gain 建议。

    这里只生成决策，不实际应用。后续执行器可以再根据峰值/真峰值截顶。
    """
    if needed is None:
        return {"enabled": False, "reason": "missing target or current active gap"}
    if abs(needed) < 0.35:
        return {"enabled": False, "reason": "global gap already close to reference", "needed_db": round(needed, 3)}
    correction = clamp(needed, -8.0, 8.0)
    if correction > 0.0:
        # 人声比原曲更靠后：人声和伴奏一起移动，但不在这里决定峰值安全。
        vocal_gain = correction * 0.58
        accomp_gain = -correction * 0.42
    else:
        # 人声比原曲更靠前：优先收人声，避免继续挖伴奏。
        vocal_gain = correction * 0.78
        accomp_gain = -correction * 0.22
    return {
        "enabled": True,
        "needed_gap_correction_db": round(needed, 3),
        "applied_gap_correction_db": round(correction, 3),
        "vocal_gain_db": round(vocal_gain, 3),
        "accomp_gain_db": round(accomp_gain, 3),
        "policy": "以原曲 active gap 为目标；profile 不参与计算，后续执行器再做峰值安全截顶。",
    }


def section_policy(state: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    section = state.get("section") or {}
    event_count = int(section.get("event_count") or 0)
    frame_error = number(errors.get("frame_range_error_db")) or 0.0
    peak_accomp = number(section.get("peak_extra_accomp_gain_db")) or 0.0
    if event_count >= 50 or peak_accomp <= -1.4 or frame_error >= 2.2:
        return {
            "mode": "replan_from_reference_windows",
            "existing_section_is_risky": True,
            "reason": "现有 section 修正触发较多或动态更不稳定，最终融合阶段需要统一重算局部移动。",
            "max_extra_accomp_atten_db": 1.0,
            "max_extra_vocal_gain_db": 1.2,
        }
    return {
        "mode": "light_reference_window_guard",
        "existing_section_is_risky": False,
        "reason": "局部段落只做轻兜底，不替代整首混音。",
        "max_extra_accomp_atten_db": 0.8,
        "max_extra_vocal_gain_db": 1.0,
    }


def duck_budget(state: dict[str, Any], errors: dict[str, Any]) -> dict[str, Any]:
    duck = state.get("duck") or {}
    presence_p50 = number(duck.get("presence_p50_db")) or 0.0
    presence_p90 = number(duck.get("presence_p90_db")) or 0.0
    high_error = number(errors.get("max_effect_high_error_db")) or 0.0
    width_error = number(errors.get("width_error_db")) or 0.0
    should_reduce_presence = presence_p50 <= -1.3 or presence_p90 <= -1.9 or width_error >= 6.0
    return {
        "mode": "reference_masking_budget",
        "presence_budget_db": 0.9 if should_reduce_presence else 1.2,
        "body_budget_db": 0.7,
        "low_budget_db": 0.9,
        "air_budget_db": 0.35 if high_error >= 1.8 else 0.5,
        "current_presence_p50_db": round(presence_p50, 3),
        "current_presence_p90_db": round(presence_p90, 3),
        "reason": (
            "伴奏让位只作为最终融合预算的一部分；若当前 presence 洞或空间脱离明显，后续执行器应减少 presence/air duck。"
        ),
    }


def spatial_correction(errors: dict[str, Any], targets: dict[str, Any]) -> dict[str, Any]:
    width_error = number(errors.get("width_error_db")) or 0.0
    rt_error = number(errors.get("reverb_rt60_error_ms")) or 0.0
    tail_error = number(errors.get("reverb_tail_error_db")) or 0.0
    near_mono = bool(((targets.get("vocal_width") or {}).get("near_mono_center_led")))
    side_trim = 0.0
    wet_trim = 0.0
    if width_error > 2.0:
        side_trim = -clamp(width_error * 0.45, 0.5, 6.0)
    if near_mono and width_error > 5.0:
        side_trim = min(side_trim, -3.0)
    if rt_error > 500.0 or tail_error > 1.2:
        wet_trim = -clamp(max(rt_error / 1200.0, tail_error), 0.5, 3.0)
    return {
        "enabled": bool(side_trim or wet_trim),
        "side_trim_db": round(side_trim, 3),
        "wet_trim_db": round(wet_trim, 3),
        "reason": "按原曲人声 stem 的宽度/混响误差做最终轻收；不改变音色相似度目标。",
    }


def risk_flags(state: dict[str, Any], errors: dict[str, Any]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    section = state.get("section") or {}
    duck = state.get("duck") or {}
    global_balance = state.get("global_balance") or {}
    existing_move = abs(number(global_balance.get("existing_vocal_bus_gain_db")) or 0.0) + abs(
        number(global_balance.get("existing_accomp_bus_gain_db")) or 0.0
    )
    presence_p50 = number(duck.get("presence_p50_db")) or 0.0
    event_count = int(section.get("event_count") or 0)
    if existing_move >= 5.0 and presence_p50 <= -1.0 and event_count >= 30:
        flags.append({"id": "stacked_fusion_moves", "reason": "现有链路里 bus、duck、section 已同时改变融合。"})
    if (number(errors.get("width_error_db")) or 0.0) >= 5.0:
        flags.append({"id": "vocal_too_wide_vs_reference", "reason": "最终人声比原曲人声更宽。"})
    if event_count >= 50:
        flags.append({"id": "section_overmix_risk", "reason": "局部段落修正触发过多。"})
    return flags


def build_decision(
    plan_path: Path,
    bus_path: Path | None,
    duck_path: Path | None,
    section_path: Path | None,
    effect_audit_path: Path | None,
) -> dict[str, Any]:
    plan = load_json(plan_path)
    targets = reference_targets(plan)
    state = current_state(
        load_json(bus_path),
        load_json(duck_path),
        load_json(section_path),
        load_json(effect_audit_path),
    )
    errors = build_errors(targets, state)
    fusion_intent = plan.get("fusion_intent") or ((plan.get("reference") or {}).get("fusion_intent") or {})
    corrections = {
        "global_gain": split_global_correction(number(errors.get("needed_global_gap_correction_db"))),
        "section": section_policy(state, errors),
        "duck_budget": duck_budget(state, errors),
        "spatial": spatial_correction(errors, targets),
    }
    return {
        "schema": "final_fusion_decision.v1.read_only",
        "plan": str(plan_path),
        "reference_targets": targets,
        "current_state": state,
        "current_errors": errors,
        "corrections": corrections,
        "risk_flags": risk_flags(state, errors),
        "debug_profile": {
            "profile": fusion_intent.get("profile"),
            "reasons": fusion_intent.get("reasons") or [],
            "usage": "explain_only_not_decision_core",
        },
        "render_consumption": {
            "active": False,
            "policy": "本脚本只生成决策 JSON，不写音频、不改变渲染链。",
        },
    }


def write_markdown(path: Path, decisions: list[dict[str, Any]]) -> None:
    lines = [
        "# Final Fusion Pass Decisions",
        "",
        "| track | ref gap | current gap | needed | global gain | section | duck presence budget | spatial trim | flags |",
        "| --- | ---: | ---: | ---: | --- | --- | ---: | --- | --- |",
    ]
    for item in decisions:
        track = plan_prefix(Path(item["plan"]))
        targets = item["reference_targets"]
        state = item["current_state"]
        errors = item["current_errors"]
        corr = item["corrections"]
        gain = corr["global_gain"]
        spatial = corr["spatial"]
        flags = ", ".join(flag["id"] for flag in item.get("risk_flags") or []) or "none"
        lines.append(
            "| {track} | {ref_gap} | {cur_gap} | {needed} | v {vgain} / a {again} | {section} | {duck_budget} | side {side} / wet {wet} | {flags} |".format(
                track=track,
                ref_gap=targets.get("global_active_gap_db"),
                cur_gap=(state.get("global_balance") or {}).get("measured_active_gap_db"),
                needed=errors.get("needed_global_gap_correction_db"),
                vgain=gain.get("vocal_gain_db"),
                again=gain.get("accomp_gain_db"),
                section=(corr.get("section") or {}).get("mode"),
                duck_budget=(corr.get("duck_budget") or {}).get("presence_budget_db"),
                side=spatial.get("side_trim_db"),
                wet=spatial.get("wet_trim_db"),
                flags=flags,
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan a read-only final fusion decision from reference/current reports.")
    parser.add_argument("--plan", type=Path, help="Single resolved_mix_plan.json")
    parser.add_argument("--render-dir", type=Path, help="Directory containing rendered reports and resolved plans.")
    parser.add_argument("--bus-report", type=Path)
    parser.add_argument("--duck-report", type=Path)
    parser.add_argument("--section-report", type=Path)
    parser.add_argument("--effect-audit", type=Path)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path)
    args = parser.parse_args()

    if args.plan:
        plan_paths = [args.plan]
    elif args.render_dir:
        plan_paths = sorted(args.render_dir.glob("*resolved_mix_plan.json"))
    else:
        raise SystemExit("Provide --plan or --render-dir")
    if not plan_paths:
        raise SystemExit("No resolved mix plans found")

    decisions = []
    for plan_path in plan_paths:
        decisions.append(
            build_decision(
                plan_path,
                args.bus_report or infer_report_path(args.render_dir, plan_path, "bus_balance"),
                args.duck_report or infer_report_path(args.render_dir, plan_path, "accomp_duck"),
                args.section_report or infer_report_path(args.render_dir, plan_path, "section_balance_guard"),
                args.effect_audit or infer_report_path(args.render_dir, plan_path, "vocal_effect_audit"),
            )
        )

    payload: dict[str, Any]
    if len(decisions) == 1 and args.plan:
        payload = decisions[0]
    else:
        payload = {
            "schema": "final_fusion_decision_batch.v1.read_only",
            "render_dir": str(args.render_dir) if args.render_dir else None,
            "items": decisions,
        }
    write_json(args.out_json, payload)
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(args.out_md, decisions)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
