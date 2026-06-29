#!/usr/bin/env python3
"""Build read-only fusion intent reports from an existing render batch.

This script does not render audio and does not mutate mix plans. It only
collects plan/report evidence so the vocal/accompaniment fusion target can be
discussed before wiring it into bus, duck, section, or spatial processors.
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


def round_float(value: Any, digits: int = 3) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), digits)
    return None


def median(values: list[float]) -> float | None:
    return round(float(statistics.median(values)), 3) if values else None


def strip_plan_stem(path: Path) -> str:
    suffix = "resolved_mix_plan"
    if path.stem.endswith(suffix):
        return path.stem[: -len(suffix)]
    return path.stem


def report_path(render_dir: Path, mix_stem: str, suffix: str) -> Path:
    return render_dir / f"{mix_stem}.{suffix}.json"


def reference_gap(plan: dict[str, Any]) -> float | None:
    balance = (((plan.get("reference") or {}).get("features") or {}).get("vocal_accomp_balance") or {})
    value = balance.get("active_vocal_minus_accomp_db")
    if value is None:
        value = balance.get("vocal_minus_accomp_db")
    return float(value) if isinstance(value, (int, float)) else None


def reference_spatial(plan: dict[str, Any]) -> dict[str, Any]:
    return (((plan.get("reference") or {}).get("features") or {}).get("vocal_spatial_profile") or {})


def dry_strategy(plan: dict[str, Any]) -> dict[str, Any]:
    source_cleanup = plan.get("source_cleanup") or {}
    if source_cleanup.get("dry_vocal_strategy"):
        return source_cleanup.get("dry_vocal_strategy") or {}
    return (((plan.get("reference") or {}).get("overrides") or {}).get("dry_vocal_strategy") or {})


def high_error(effect_audit: dict[str, Any]) -> float | None:
    tonal = ((effect_audit.get("errors") or {}).get("tonal_balance") or {})
    values = [
        tonal.get("upper_error"),
        tonal.get("harsh_error"),
        tonal.get("sib_error"),
        tonal.get("air_error"),
    ]
    numeric = [float(value) for value in values if isinstance(value, (int, float))]
    return round(max(numeric), 3) if numeric else None


def infer_profile(plan: dict[str, Any]) -> tuple[str, list[str]]:
    analysis = plan.get("analysis") or {}
    group = analysis.get("group_ratios") or {}
    ratios = analysis.get("ratios") or {}
    ref_gap = reference_gap(plan)
    spatial = reference_spatial(plan)
    selected_template = str(plan.get("selected_template") or "")
    body = float(group.get("body") or 0.0)
    presence = float(group.get("presence") or 0.0)
    lowmid = float(ratios.get("lowmid") or 0.0)
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    side_mid = float(spatial.get("active_side_minus_mid_db") or 0.0)
    near_mono = bool(spatial.get("near_mono_center_led"))

    reasons: list[str] = []
    if near_mono and side_mid <= -29.0 and ref_gap is not None and ref_gap >= -0.6:
        reasons.append("原曲人声接近持平且非常居中/窄，优先保持近、小、贴。")
        return "intimate_light", reasons
    if selected_template == "template_a" and ref_gap is not None and ref_gap >= -1.6:
        reasons.append("原曲 active 人声靠前，模板为强情绪/前置型。")
        if presence <= 0.03:
            reasons.append("干声 presence 极低，清晰度不能只靠伴奏大幅让位。")
        return "front_pop", reasons
    if body >= 0.93 and body_to_presence >= 20.0 and ref_gap is not None and ref_gap >= -1.9:
        reasons.append("人声主体厚、presence 少，但原曲不是深埋型，重点是包裹和灰度。")
        return "warm_mid_embedded", reasons
    if lowmid >= 0.70 and body >= 0.85:
        reasons.append("低中频/主体占比高，重点是男声厚度和伴奏低中频分工。")
        return "dense_male", reasons
    if body_to_presence >= 16.0:
        reasons.append("主体对 presence 比例很高，按温暖嵌入型处理。")
        return "warm_mid_embedded", reasons
    reasons.append("没有命中强特征，先按通用参考融合型处理。")
    return "reference_balanced", reasons


def profile_targets(profile: str) -> dict[str, Any]:
    table = {
        "front_pop": {
            "frontness": "人声可前置，但伴奏 presence 不能被挖空。",
            "duck": "只解决遮挡；presence duck 需要和 bus/section 共用预算。",
            "space": "中心稳定，宽度/湿度不能让人声浮到伴奏外。",
        },
        "warm_mid_embedded": {
            "frontness": "人声应贴在伴奏里，清晰度来自频段分工和空间，不来自硬推。",
            "duck": "少做高频洞，优先保护灰暗包裹感。",
            "space": "控制湿度和高频 return，避免变亮变宽。",
        },
        "dense_male": {
            "frontness": "男声可略靠后，厚度不能被 bus/duck 改成贴脸。",
            "duck": "重点低中频错位，避免全频压伴奏。",
            "space": "保持中心厚度，空间只做贴合。",
        },
        "intimate_light": {
            "frontness": "近、小、自然，不要做成大流行前景人声。",
            "duck": "极轻，只处理明显遮挡。",
            "space": "窄、干、居中，side/wet 是首要约束。",
        },
        "reference_balanced": {
            "frontness": "跟随参考 active gap，但必须避免多模块重复修正。",
            "duck": "按遮挡证据使用。",
            "space": "跟随原曲人声 stem 审计。",
        },
    }
    return table.get(profile, table["reference_balanced"])


def build_conflict_flags(
    bus: dict[str, Any],
    duck: dict[str, Any],
    section: dict[str, Any],
    effect_audit: dict[str, Any],
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    vocal_gain = float(bus.get("vocal_bus_gain_db") or 0.0)
    accomp_gain = float(bus.get("accomp_bus_gain_db") or 0.0)
    bus_move = vocal_gain + abs(accomp_gain)
    presence_p50 = float(duck.get("presence_duck_db_active_p50") or 0.0)
    presence_p90 = float(duck.get("presence_duck_db_active_p90") or 0.0)
    event_count = int(section.get("event_count") or 0)
    peak_accomp = float(section.get("peak_extra_accomp_gain_db") or 0.0)
    spatial = ((effect_audit.get("errors") or {}).get("spatial") or {})
    dynamics = ((effect_audit.get("errors") or {}).get("dynamics") or {})
    reverb = ((effect_audit.get("errors") or {}).get("reverb") or {})
    side_error = float(spatial.get("active_side_minus_mid_db_error_db") or 0.0)
    frame_error = float(dynamics.get("frame_range_p90_p10_db_error") or 0.0)
    rt_error = float(reverb.get("est_rt60_ms_error") or 0.0)
    max_high = high_error(effect_audit)

    if bus_move >= 5.0 and presence_p50 <= -1.0 and event_count >= 30:
        flags.append({
            "id": "stacked_fronting",
            "severity": "high",
            "reason": "bus、duck、section 同时把人声往前/把伴奏往后推。",
            "evidence": {
                "bus_total_move_db": round(bus_move, 3),
                "presence_duck_p50_db": round(presence_p50, 3),
                "section_event_count": event_count,
            },
        })
    if presence_p90 <= -1.8 or presence_p50 <= -1.4:
        flags.append({
            "id": "presence_hole_risk",
            "severity": "medium",
            "reason": "伴奏 presence 让位偏深，可能清楚但不包裹。",
            "evidence": {
                "presence_duck_p50_db": round(presence_p50, 3),
                "presence_duck_p90_db": round(presence_p90, 3),
            },
        })
    if event_count >= 50 and peak_accomp <= -1.4:
        flags.append({
            "id": "section_overmix_risk",
            "severity": "high",
            "reason": "section guard 触发太多，已接近第二套自动混音。",
            "evidence": {
                "event_count": event_count,
                "peak_extra_accomp_gain_db": round(peak_accomp, 3),
            },
        })
    if side_error >= 5.0:
        flags.append({
            "id": "space_detach_risk",
            "severity": "medium",
            "reason": "候选人声比原曲人声更宽，可能从伴奏里浮出来。",
            "evidence": {"active_side_minus_mid_error_db": round(side_error, 3)},
        })
    if max_high is not None and max_high >= 1.8:
        flags.append({
            "id": "effect_brightness_risk",
            "severity": "medium",
            "reason": "最终人声效果高频高于原曲人声 stem。",
            "evidence": {"max_high_error_db": max_high},
        })
    if frame_error >= 2.2:
        flags.append({
            "id": "dynamic_instability_risk",
            "severity": "medium",
            "reason": "短帧动态比原曲更不稳定，可能和 section/sidechain 叠加有关。",
            "evidence": {"frame_range_error_db": round(frame_error, 3)},
        })
    if rt_error >= 500.0:
        flags.append({
            "id": "wet_depth_mismatch",
            "severity": "medium",
            "reason": "候选人声混响/尾部深度与原曲 stem 有偏差。",
            "evidence": {"est_rt60_error_ms": round(rt_error, 3)},
        })
    return flags


def build_module_contracts(profile: str, flags: list[dict[str, Any]]) -> dict[str, Any]:
    flag_ids = {str(flag.get("id")) for flag in flags}
    return {
        "timbre": {
            "role": "只追音色筛选片段的人声音色目标。",
            "must_not": "不决定人声前后、不通过削 presence 间接改变融合。",
        },
        "bus_balance": {
            "role": "只决定全局前后关系。",
            "must_not": "不能独立把 active gap 补满；需要扣除 duck/section 预算。",
            "watch": "stacked_fronting" in flag_ids,
        },
        "accomp_duck": {
            "role": "只解决伴奏遮挡。",
            "must_not": "不能承担弱人声整体抬出任务。",
            "watch": "presence_hole_risk" in flag_ids or "stacked_fronting" in flag_ids,
        },
        "section_balance": {
            "role": "只修段落级明显偏差。",
            "must_not": "触发大量窗口时继续改变整首融合关系。",
            "watch": "section_overmix_risk" in flag_ids,
        },
        "spatial_fx": {
            "role": "匹配原曲人声 stem 的宽度、湿度、纵深。",
            "must_not": "不能为了质感把人声做宽/做湿到脱离伴奏。",
            "watch": profile == "intimate_light" or "space_detach_risk" in flag_ids,
        },
    }


def build_intent(render_dir: Path, plan_path: Path) -> dict[str, Any]:
    plan = load_json(plan_path)
    plan_stem = strip_plan_stem(plan_path)
    mix_stem = f"mix_{plan_stem}"
    bus = load_json(report_path(render_dir, mix_stem, "bus_balance"))
    duck = load_json(report_path(render_dir, mix_stem, "accomp_duck"))
    section = load_json(report_path(render_dir, mix_stem, "section_balance_guard"))
    timbre = load_json(report_path(render_dir, mix_stem, "timbre_chain_guard"))
    effect_audit = load_json(report_path(render_dir, mix_stem, "vocal_effect_audit"))
    spatial_audit = load_json(report_path(render_dir, mix_stem, "vocal_group_spatial_audit"))

    profile, profile_reasons = infer_profile(plan)
    flags = build_conflict_flags(bus, duck, section, effect_audit)
    ref_gap = reference_gap(plan)
    analysis = plan.get("analysis") or {}
    group = analysis.get("group_ratios") or {}
    ratios = analysis.get("ratios") or {}
    spatial = reference_spatial(plan)
    dry = dry_strategy(plan)
    events = section.get("events") or []
    event_deficits = [float(item.get("deficit_db")) for item in events if isinstance(item.get("deficit_db"), (int, float))]
    event_accomp = [float(item.get("accomp_gain_db")) for item in events if isinstance(item.get("accomp_gain_db"), (int, float))]
    effect_errors = effect_audit.get("errors") or {}

    evidence = {
        "plan": {
            "plan": str(plan_path),
            "mix_stem": mix_stem,
            "selected_template": plan.get("selected_template"),
            "classification_label": plan.get("classification_label"),
            "reference_active_vocal_minus_accomp_db": round_float(ref_gap, 2),
            "reference_near_mono_center_led": bool(spatial.get("near_mono_center_led")),
            "reference_active_side_minus_mid_db": round_float(spatial.get("active_side_minus_mid_db")),
            "dry_vocal_tags": dry.get("tags") or [],
            "dry_vocal_duck_profile": dry.get("duck_profile") or {},
            "body_ratio": round_float(group.get("body")),
            "presence_ratio": round_float(group.get("presence")),
            "lowmid_ratio": round_float(ratios.get("lowmid")),
            "body_to_presence": round_float(analysis.get("body_to_presence")),
        },
        "render": {
            "bus": {
                "render_gap_db": bus.get("active_vocal_minus_accomp_db"),
                "target_gap_db": bus.get("target_active_vocal_minus_accomp_db"),
                "vocal_gain_db": bus.get("vocal_bus_gain_db"),
                "accomp_gain_db": bus.get("accomp_bus_gain_db"),
                "total_abs_move_db": round(
                    float(bus.get("vocal_bus_gain_db") or 0.0) + abs(float(bus.get("accomp_bus_gain_db") or 0.0)),
                    3,
                ),
            },
            "duck": {
                "profile": duck.get("profile") or {},
                "presence_p50_db": duck.get("presence_duck_db_active_p50"),
                "presence_p90_db": duck.get("presence_duck_db_active_p90"),
                "low_p50_db": duck.get("low_duck_db_active_p50"),
                "low_p90_db": duck.get("low_duck_db_active_p90"),
            },
            "section": {
                "event_count": section.get("event_count"),
                "peak_extra_vocal_gain_db": section.get("peak_extra_vocal_gain_db"),
                "peak_extra_accomp_gain_db": section.get("peak_extra_accomp_gain_db"),
                "median_deficit_db": median(event_deficits),
                "median_accomp_gain_db": median(event_accomp),
            },
            "timbre": {
                "action_count": len(timbre.get("actions") or []),
                "actions": [
                    {
                        "band": item.get("band"),
                        "type": item.get("type"),
                        "gain_db": item.get("gain_db"),
                    }
                    for item in (timbre.get("actions") or [])[:10]
                ],
            },
            "effect_errors": {
                "spatial": effect_errors.get("spatial") or {},
                "dynamics": effect_errors.get("dynamics") or {},
                "reverb": effect_errors.get("reverb") or {},
                "max_high_error_db": high_error(effect_audit),
            },
            "spatial_audit_recommendation": ((spatial_audit.get("candidates") or [{}])[0].get("recommendation") or {}),
        },
    }

    return {
        "schema": "fusion_intent.v1.read_only",
        "track": plan_stem.rstrip("_"),
        "profile": profile,
        "profile_reasons": profile_reasons,
        "targets": profile_targets(profile),
        "conflict_flags": flags,
        "module_contracts": build_module_contracts(profile, flags),
        "evidence": evidence,
        "next_discussion": [
            "确认 profile 是否符合听感目标。",
            "确认哪些 flags 是真正问题，哪些是可接受的风格差异。",
            "确认后再把 fusion_intent 接入 bus/duck/section/spatial；本报告本身不改声音。",
        ],
    }


def write_markdown(path: Path, intents: list[dict[str, Any]]) -> None:
    lines = [
        "# Fusion Intent Diagnostics",
        "",
        "| track | profile | ref gap | bus move | duck presence p50/p90 | section events | key flags |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in intents:
        plan_ev = item["evidence"]["plan"]
        render_ev = item["evidence"]["render"]
        bus = render_ev["bus"]
        duck = render_ev["duck"]
        section = render_ev["section"]
        flags = ", ".join(flag["id"] for flag in item["conflict_flags"]) or "none"
        lines.append(
            "| {track} | {profile} | {ref_gap} | {bus_move} | {p50}/{p90} | {events} | {flags} |".format(
                track=item["track"],
                profile=item["profile"],
                ref_gap=plan_ev.get("reference_active_vocal_minus_accomp_db"),
                bus_move=bus.get("total_abs_move_db"),
                p50=duck.get("presence_p50_db"),
                p90=duck.get("presence_p90_db"),
                events=section.get("event_count"),
                flags=flags,
            )
        )
    lines.append("")
    for item in intents:
        lines.extend([
            f"## {item['track']}",
            "",
            f"- profile: `{item['profile']}`",
            f"- target: {item['targets']['frontness']}",
            f"- duck: {item['targets']['duck']}",
            f"- space: {item['targets']['space']}",
            "- reasons: " + "；".join(item["profile_reasons"]),
            "- flags: " + (", ".join(flag["id"] for flag in item["conflict_flags"]) or "none"),
            "",
        ])
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build read-only fusion intent diagnostics for a render directory.")
    parser.add_argument("--render-dir", type=Path, required=True)
    parser.add_argument("--out-json", type=Path)
    parser.add_argument("--out-md", type=Path)
    parser.add_argument("--write-per-track", action="store_true")
    args = parser.parse_args()

    plan_paths = sorted(args.render_dir.glob("*resolved_mix_plan.json"))
    if not plan_paths:
        raise SystemExit(f"No resolved mix plans found in {args.render_dir}")

    intents = [build_intent(args.render_dir, plan_path) for plan_path in plan_paths]
    payload = {
        "schema": "fusion_intent_batch.v1.read_only",
        "render_dir": str(args.render_dir),
        "items": intents,
    }

    if args.write_per_track:
        for item in intents:
            out = args.render_dir / f"mix_{item['track']}.fusion_intent.json"
            out.write_text(json.dumps(item, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(args.out_md, intents)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
