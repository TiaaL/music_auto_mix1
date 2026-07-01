#!/usr/bin/env python3
"""模板链后的音色保持 guard。

前置音色 EQ 会经过模板 EQ、压缩、brighter 后被改写；这个脚本在模板链跑完后，
重新测当前人声和“音色筛选片段”的宽带差异，只做小幅补偿，让后续清理建立在相似度基础上。
同一逻辑也可以放在 vocal_group_fx 之后再轻校一次，因为空间/组总线也会重新染色。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from analyze_reference import (
    active_intervals_from_vocal,
    load_audio_as_float,
    spectral_envelope_for_intervals,
    tonal_balance_for_intervals,
)


BANDS = {
    "low": {"freq_hz": 130.0, "q": 0.8, "actions": ("cut",)},
    "lowmid": {"freq_hz": 320.0, "q": 0.9, "actions": ("cut", "boost")},
    "mid": {"freq_hz": 800.0, "q": 0.8, "actions": ()},
    "upper": {"freq_hz": 2800.0, "q": 0.9, "actions": ("cut", "boost")},
    "harsh": {"freq_hz": 6200.0, "q": 1.2, "actions": ("cut", "boost")},
    "sib": {"freq_hz": 9500.0, "q": 1.4, "actions": ("cut", "boost")},
    "air": {"freq_hz": 14000.0, "q": 0.7, "actions": ("cut",)},
}

DEAD_BAND_DB = 1.2
CUT_FRACTION = 0.45
BOOST_FRACTION = 0.35
MAX_ACTIONS = 5
MAX_CUT_DB = {
    "low": 1.6,
    "lowmid": 1.8,
    "upper": 2.2,
    "harsh": 1.5,
    "sib": 1.2,
    "air": 1.0,
}
MAX_BOOST_DB = {
    "lowmid": 0.8,
    "upper": 1.2,
    "harsh": 0.7,
    "sib": 0.6,
}

POST_GROUP_MAX_CUT_DB = {
    "low": 1.2,
    "lowmid": 1.2,
    # post-group 是最终入总线的人声贡献轨；偏亮/偏冲会直接变成靠前感。
    # 这里适度放宽 cut 上限，但仍保持低幅度、宽峰、只按音色筛选片段差异触发。
    "upper": 1.35,
    "harsh": 1.05,
    "sib": 0.85,
    "air": 0.8,
}
POST_GROUP_MAX_BOOST_DB = {
    "lowmid": 0.6,
    "upper": 0.8,
    "harsh": 0.45,
    "sib": 0.35,
}
ENVELOPE_DEAD_BAND_DB = 1.1
ENVELOPE_GAIN_FRACTION = 0.22
ENVELOPE_MAX_ACTIONS = 2
ENVELOPE_Q = 1.1
ENVELOPE_CUT_CAP_DB = {
    "low": 0.45,
    "lowmid": 0.60,
    "mid": 0.50,
    "upper": 0.70,
    "harsh": 0.55,
    "sib": 0.40,
    "air": 0.30,
}
ENVELOPE_BOOST_CAP_DB = {
    "lowmid": 0.45,
    "mid": 0.40,
    "upper": 0.60,
    "harsh": 0.30,
    "sib": 0.25,
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def clamp(value: float, lo: float, hi: float) -> float:
    return min(hi, max(lo, value))


def timbre_target(plan: dict[str, Any]) -> dict[str, float]:
    # timbre-only 流程不一定有完整 reference 块，所以优先读顶层 timbre_features；
    # 旧 plan 仍兼容 reference.timbre_features。
    ref = plan.get("timbre_features") or (plan.get("reference") or {}).get("timbre_features") or {}
    target = ref.get("vocal_tonal_balance") or {}
    return {str(k): float(v) for k, v in target.items() if isinstance(v, (int, float))}


def timbre_target_envelope(plan: dict[str, Any]) -> dict[str, dict[str, float]]:
    ref = plan.get("timbre_features") or (plan.get("reference") or {}).get("timbre_features") or {}
    out: dict[str, dict[str, float]] = {}
    for item in (ref.get("vocal_spectral_envelope") or {}).get("bands") or []:
        if not isinstance(item, dict):
            continue
        band_id = str(item.get("id") or "")
        freq = item.get("freq_hz")
        value = item.get("db")
        if band_id and isinstance(freq, (int, float)) and isinstance(value, (int, float)):
            out[band_id] = {"freq_hz": float(freq), "db": float(value)}
    return out


def analyse_current(path: Path) -> dict[str, float]:
    audio, sr = load_audio_as_float(path)
    regions = active_intervals_from_vocal(audio, sr)
    return tonal_balance_for_intervals(audio, sr, regions)


def analyse_current_envelope(path: Path) -> dict[str, dict[str, float]]:
    audio, sr = load_audio_as_float(path)
    regions = active_intervals_from_vocal(audio, sr)
    envelope = spectral_envelope_for_intervals(audio, sr, regions)
    out: dict[str, dict[str, float]] = {}
    for item in envelope.get("bands") or []:
        if not isinstance(item, dict):
            continue
        band_id = str(item.get("id") or "")
        freq = item.get("freq_hz")
        value = item.get("db")
        if band_id and isinstance(freq, (int, float)) and isinstance(value, (int, float)):
            out[band_id] = {"freq_hz": float(freq), "db": float(value)}
    return out


def reference_vocal_balance_db(plan: dict[str, Any]) -> float | None:
    """读取原曲 active 人声-伴奏比例；越接近 0，说明人声越不能被音色回正压暗。"""
    bus = (((plan.get("reference") or {}).get("overrides") or {}).get("bus_balance") or {})
    value = bus.get("reference_vocal_minus_accomp_db")
    return float(value) if isinstance(value, (int, float)) else None


def reference_presence_policy(ref_balance_db: float | None) -> dict[str, Any]:
    """根据原曲人声位置限制音色回正强度。

    音色筛选片段只决定“往哪里靠”；原曲人声/伴奏比例决定“能靠多远”。
    人声接近持平或靠前时，upper/harsh/sib 这些穿透力频段只能轻微削，
    避免为了音色相似度把主唱压进伴奏里。
    """
    if ref_balance_db is not None and ref_balance_db >= -1.2:
        return {
            "mode": "reference_vocal_forward_or_even",
            "presence_cut_scale": 0.42,
            "presence_cut_caps": {"upper": 0.9, "harsh": 0.75, "sib": 0.65},
        }
    if ref_balance_db is not None and ref_balance_db >= -2.5:
        return {
            "mode": "reference_vocal_slightly_back",
            "presence_cut_scale": 0.65,
            "presence_cut_caps": {"upper": 1.4, "harsh": 1.0, "sib": 0.85},
        }
    return {
        "mode": "reference_vocal_back_or_unknown",
        "presence_cut_scale": 1.0,
        "presence_cut_caps": {},
    }


def unified_presence_policy(plan: dict[str, Any], ref_balance_db: float | None) -> dict[str, Any]:
    """优先读取 plan 里的统一决策层；旧 plan 才用本脚本的兜底判断。"""
    context = plan.get("vocal_processing_context") or {}
    policy = context.get("presence_band_policy") or {}
    if policy:
        return {
            "mode": policy.get("mode") or "reference_vocal_back_or_unknown",
            "presence_cut_scale": float(policy.get("post_timbre_cut_scale") or 1.0),
            "presence_cut_caps": policy.get("post_timbre_cut_caps_db") or {},
            "processing_context_version": context.get("version"),
        }
    return reference_presence_policy(ref_balance_db)


def planned_cut_db(actions: list[dict[str, Any]] | None, band: str) -> float:
    total = 0.0
    for action in actions or []:
        if action.get("band") != band:
            continue
        gain = action.get("gain_db")
        if isinstance(gain, (int, float)) and float(gain) < 0.0:
            total += abs(float(gain))
    return total


def planned_reserved_cut_db(plan: dict[str, Any], band: str) -> float:
    """统计链后 guard 之外已经计划的同频段 cut，用统一预算防止叠刀。"""
    source_cleanup = plan.get("source_cleanup") or {}
    source_eq = source_cleanup.get("source_eq") or {}
    timbre_eq = source_eq.get("timbre_vocal_eq") or {}
    vocal_eq = source_eq.get("vocal_eq") or {}
    hf_guard = source_cleanup.get("vocal_hf_guard") or {}
    residual = plan.get("residual_vocal_eq") or {}
    return sum(
        planned_cut_db((block or {}).get("actions") or [], band)
        for block in (timbre_eq, vocal_eq, hf_guard, residual)
    )


def band_cut_budget(plan: dict[str, Any], band: str) -> float | None:
    context = plan.get("vocal_processing_context") or {}
    budget = (context.get("band_budget") or {}).get("max_total_cut_db") or {}
    value = budget.get(band)
    return float(value) if isinstance(value, (int, float)) else None


def envelope_budget_band(freq_hz: float) -> str:
    if freq_hz < 180.0:
        return "low"
    if freq_hz < 500.0:
        return "lowmid"
    if freq_hz < 1000.0:
        return "mid"
    if freq_hz < 4000.0:
        return "upper"
    if freq_hz < 8000.0:
        return "harsh"
    if freq_hz < 12000.0:
        return "sib"
    return "air"


def envelope_guard_actions(
    target_env: dict[str, dict[str, float]],
    current_env: dict[str, dict[str, float]],
    presence_policy: dict[str, Any],
    clarity_guard: dict[str, Any],
    stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """模板链后用细分包络再轻校一次，避免相似度只停留在粗 band。"""
    if not target_env or not current_env:
        return [], [{"reason": "missing spectral envelope target/current for timbre guard"}]
    clarity_protected = set(clarity_guard.get("protected_bands") or [])
    ranked: list[tuple[float, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for band_id, target_item in target_env.items():
        current_item = current_env.get(band_id)
        if not current_item:
            continue
        freq = float(target_item["freq_hz"])
        delta = float(target_item["db"]) - float(current_item["db"])
        if abs(delta) < ENVELOPE_DEAD_BAND_DB:
            continue
        budget_band = envelope_budget_band(freq)
        if delta < 0.0:
            if budget_band in clarity_protected:
                skipped.append({
                    "envelope_band": band_id,
                    "budget_band": budget_band,
                    "delta_db": round(delta, 2),
                    "reason": (
                        "原曲人声 stem 显示该清晰度频段已经比当前更亮；"
                        "跳过链后高频 cut，避免把好干声做闷"
                    ),
                    "reference_clarity_guard": (clarity_guard.get("by_band") or {}).get(budget_band),
                })
                continue
            amount = abs(delta) * ENVELOPE_GAIN_FRACTION
            cap = ENVELOPE_CUT_CAP_DB.get(budget_band, 0.45)
            if budget_band in {"upper", "harsh", "sib"}:
                amount *= float(presence_policy["presence_cut_scale"])
                cap = min(cap, float((presence_policy["presence_cut_caps"] or {}).get(budget_band, cap)))
            gain = -round(clamp(amount, 0.12, cap), 2)
            action_type = "cut"
        else:
            cap = ENVELOPE_BOOST_CAP_DB.get(budget_band)
            if cap is None:
                skipped.append({
                    "envelope_band": band_id,
                    "budget_band": budget_band,
                    "delta_db": round(delta, 2),
                    "reason": "post-chain envelope boost disabled for this region",
                })
                continue
            gain = round(clamp(delta * ENVELOPE_GAIN_FRACTION, 0.12, cap), 2)
            action_type = "boost"
        ranked.append((
            abs(delta),
            {
                "band": budget_band,
                "envelope_band": band_id,
                "type": action_type,
                "freq_hz": round(freq, 1),
                "q": ENVELOPE_Q,
                "gain_db": gain,
                "source": "post_template_timbre_envelope_guard",
                "reason": (
                    f"{stage} 阶段细分包络 {band_id} 与音色筛选片段相差 {delta:+.1f} dB；"
                    "小幅修正模板链洗掉的音色差异"
                ),
                "evidence": {
                    "current_envelope_db": round(float(current_item["db"]), 2),
                    "target_envelope_db": round(float(target_item["db"]), 2),
                    "delta_db": round(delta, 2),
                    "stage": stage,
                },
            },
        ))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    actions: list[dict[str, Any]] = []
    used: set[str] = set()
    for _, action in ranked:
        band = str(action.get("band") or "")
        if band in used:
            continue
        actions.append(action)
        used.add(band)
        if len(actions) >= ENVELOPE_MAX_ACTIONS:
            break
    return actions, skipped


def build_actions(
    plan: dict[str, Any],
    target: dict[str, float],
    current: dict[str, float],
    target_env: dict[str, dict[str, float]],
    current_env: dict[str, dict[str, float]],
    ref_balance_db: float | None,
    presence_policy: dict[str, Any],
    stage: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    ranked: list[tuple[float, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    clarity_guard = (plan.get("vocal_processing_context") or {}).get("reference_clarity_guard") or {}
    clarity_protected = set(clarity_guard.get("protected_bands") or [])
    for band, rule in BANDS.items():
        if band not in target or band not in current:
            continue
        delta = float(target[band]) - float(current[band])
        if abs(delta) < DEAD_BAND_DB:
            continue
        if delta < 0.0:
            if "cut" not in rule["actions"]:
                continue
            if band in clarity_protected:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": (
                        "原曲人声 stem 显示该清晰度频段已经比当前更亮；"
                        "跳过链后高频 cut，避免把好干声做闷"
                    ),
                    "reference_clarity_guard": (clarity_guard.get("by_band") or {}).get(band),
                })
                continue
            cut_fraction = CUT_FRACTION
            cap = (POST_GROUP_MAX_CUT_DB if stage == "post_group" else MAX_CUT_DB).get(band, 1.0)
            if band in {"upper", "harsh", "sib"}:
                cut_fraction *= float(presence_policy["presence_cut_scale"])
                cap = min(cap, float((presence_policy["presence_cut_caps"] or {}).get(band, cap)))
            budget = None if stage == "post_group" else band_cut_budget(plan, band)
            if budget is not None:
                reserved = planned_reserved_cut_db(plan, band)
                remaining = budget - reserved
                if remaining <= 0.05:
                    skipped.append({
                        "band": band,
                        "delta_db": round(delta, 2),
                        "reason": (
                            f"统一频段预算已被前置/后置清理占满 "
                            f"({reserved:.2f}/{budget:.2f} dB)，跳过链后音色 cut"
                        ),
                    })
                    continue
                cap = min(cap, remaining)
            if cap < 0.15:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": "链后音色 cut 剩余预算不足 0.15 dB",
                })
                continue
            amount = clamp(abs(delta) * cut_fraction, 0.15, cap)
            action_type = "cut"
            gain_db = -round(amount, 2)
        else:
            boost_caps = POST_GROUP_MAX_BOOST_DB if stage == "post_group" else MAX_BOOST_DB
            if "boost" not in rule["actions"] or band not in boost_caps:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": "模板链后不安全或无必要的 boost 已跳过",
                })
                continue
            amount = clamp(delta * BOOST_FRACTION, 0.20, boost_caps[band])
            action_type = "boost"
            gain_db = round(amount, 2)
        ranked.append((
            abs(delta),
            {
                "band": band,
                "type": action_type,
                "freq_hz": rule["freq_hz"],
                "q": rule["q"],
                "gain_db": gain_db,
                "source": "post_template_timbre_guard",
                "reason": (
                    f"{stage} 阶段 {band} 与音色筛选片段相差 {delta:+.1f} dB；"
                    "只做小幅回正，避免模板链或 vocal_group 继续洗掉相似度"
                ),
                "evidence": {
                    "current_db": round(float(current[band]), 2),
                    "target_db": round(float(target[band]), 2),
                    "delta_db": round(delta, 2),
                    "reference_vocal_minus_accomp_db": (
                        round(ref_balance_db, 2) if ref_balance_db is not None else None
                    ),
                    "reference_presence_policy": presence_policy["mode"],
                    "stage": stage,
                },
            },
        ))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    broad_actions = [action for _, action in ranked[:MAX_ACTIONS]]
    envelope_actions, envelope_skipped = envelope_guard_actions(
        target_env,
        current_env,
        presence_policy,
        clarity_guard,
        stage,
    )
    actions = [*broad_actions, *envelope_actions][: MAX_ACTIONS + ENVELOPE_MAX_ACTIONS]
    return actions, [*skipped, *envelope_skipped], presence_policy


def eq_filter(action: dict[str, Any]) -> str | None:
    try:
        freq = float(action["freq_hz"])
        q = float(action["q"])
        gain = float(action["gain_db"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(gain) < 0.05 or freq <= 0.0 or q <= 0.0:
        return None
    return f"equalizer=f={freq:.3f}:width_type=q:width={q:.3f}:g={gain:.3f}"


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply post-template timbre preservation EQ.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument(
        "--stage",
        choices=("post_template", "post_group"),
        default="post_template",
        help="post_template=模板插入链后；post_group=vocal_group_fx 后的最终可听音色轻校。",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan)
    target = timbre_target(plan)
    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if not target:
        shutil.copyfile(args.input_wav, args.output_wav)
        report = {"enabled": False, "reason": "missing timbre target in plan", "actions": []}
        if args.metadata:
            write_json(args.metadata, report)
        print("[timbre-chain-guard] skipped")
        return

    current = analyse_current(args.input_wav)
    target_env = timbre_target_envelope(plan)
    current_env = analyse_current_envelope(args.input_wav)
    ref_balance_db = reference_vocal_balance_db(plan)
    presence_policy = unified_presence_policy(plan, ref_balance_db)
    actions, skipped, presence_policy = build_actions(
        plan,
        target,
        current,
        target_env,
        current_env,
        ref_balance_db,
        presence_policy,
        args.stage,
    )
    filters = [value for action in actions if (value := eq_filter(action))]
    report = {
        "enabled": bool(filters),
        "stage": args.stage,
        "target": target,
        "current_after_template": current,
        "target_spectral_envelope": target_env,
        "current_spectral_envelope": current_env,
        "actions": actions,
        "skipped": skipped,
        "reference_vocal_minus_accomp_db": ref_balance_db,
        "reference_presence_policy": presence_policy,
        "policy": (
            "按音色筛选片段回正宽带差异，并用细分频谱包络补足粗 band 听感不明显的问题；"
            "回正强度受原曲人声/伴奏比例约束，不为了相似度牺牲人声站位。"
        ),
    }
    if not filters:
        shutil.copyfile(args.input_wav, args.output_wav)
        if args.metadata:
            write_json(args.metadata, report)
        print("[timbre-chain-guard] no actions")
        return

    print("[timbre-chain-guard] applying:")
    for action in actions:
        print(
            "  - "
            f"{action.get('band')} {action.get('type')} "
            f"{action.get('gain_db')} dB @ {action.get('freq_hz')} Hz"
        )
    cmd = [
        args.ffmpeg,
        "-y",
        "-hide_banner",
        "-i",
        str(args.input_wav),
        "-af",
        ",".join(filters),
        "-c:a",
        "pcm_f32le",
        str(args.output_wav),
    ]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            "Timbre chain guard failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    if args.metadata:
        write_json(args.metadata, report)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
