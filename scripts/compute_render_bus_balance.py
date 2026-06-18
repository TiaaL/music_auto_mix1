#!/usr/bin/env python3
"""按 post-FX 人声/伴奏 active RMS 计算总线比例增益。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_reference import (  # noqa: E402
    active_intervals_from_vocal,
    load_audio_as_float,
    measure_loudness,
    rms_db_for_intervals,
    to_mono,
)

BUS_MAX_GAIN_DB = 12.0
BUS_MAX_ATTEN_DB = 12.0
BUS_DEAD_BAND_DB = 0.4
# 距离驱动的修正余量：渲染比例接近目标时保守，明显埋声时放开上限。
# 这样“听不清”的素材可以真正靠近目标，而不是被固定 3-4 dB 上限卡住。
BUS_RATIO_MIN_CORRECTION_DB = 6.0
BUS_RATIO_MAX_CORRECTION_DB = 10.0
# 当 |目标 - 渲染| 达到这个距离时，允许使用完整最大修正量。
BUS_RATIO_FULL_UNLOCK_GAP_DB = 12.0
# 修正分配到两个 bus：人声推 60%，伴奏压 40%，并随动态上限一起缩放。
BUS_RATIO_VOCAL_GAIN_FRACTION = 0.60
BUS_RATIO_ACCOMP_ATTEN_FRACTION = 0.40
GENERIC_ACTIVE_GAP_DB = -2.0
# 弱/闷/咬字区缺失的人声，追到参考比例后听感仍可能偏埋；
# 这里最多只把“目标比例”往人声侧挪一点，不直接改人声音色。
WEAK_VOCAL_TARGET_MAX_LIFT_DB = 2.0
TARGET_GAP_MIN_DB = -5.0
TARGET_GAP_MAX_DB = 0.0
SEVERE_ARTIFACT_PULLBACK_DB = 0.8


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def reference_levels(plan: dict | None) -> dict[str, Any]:
    if not plan:
        return {}
    return (
        (plan.get("reference") or {})
        .get("features", {})
        .get("vocal_accomp_balance", {})
    ) or {}


def vocal_artifact_repair(plan: dict | None) -> dict[str, Any]:
    """读取人声瑕疵修复档位，用来决定人声是否应该更往后。"""
    return ((plan or {}).get("source_cleanup") or {}).get("vocal_artifact_repair") or {}


def vocal_balance_compensation_db(plan: dict | None) -> tuple[float, list[str]]:
    """根据干声自身问题给比例目标加一点人声侧补偿，不改变音色。"""
    analysis = (plan or {}).get("analysis") or {}
    ratios = analysis.get("ratios") or {}
    group_ratios = analysis.get("group_ratios") or {}
    compensation = 0.0
    reasons: list[str] = []

    lowmid = float(ratios.get("lowmid") or 0.0)
    presence = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    upper_peak = float(analysis.get("peakiness_upper") or 0.0)
    harsh_peak = float(analysis.get("peakiness_harsh") or 0.0)
    sib_peak = float(analysis.get("peakiness_sib") or 0.0)

    # 闷、厚、低中频重的干声，即使数值比例追到原曲，听感仍会更埋。
    if lowmid >= 0.55:
        compensation += 0.7
        reasons.append(f"lowmid_ratio={lowmid:.3f}")
    if lowmid >= 0.70:
        compensation += 0.4
        reasons.append("extreme_lowmid")
    if body_to_presence >= 8.0:
        compensation += 0.7
        reasons.append(f"body_to_presence={body_to_presence:.1f}")
    if body_to_presence >= 12.0:
        compensation += 0.4
        reasons.append("extreme_body_to_presence")
    if presence <= 0.085 and (lowmid >= 0.45 or body_to_presence >= 6.0):
        compensation += 0.5
        reasons.append(f"presence_starved={presence:.3f}")
    if presence <= 0.03 and body_to_presence >= 16.0:
        compensation += 0.5
        reasons.append("extreme_presence_starvation")

    # 刺、毛、金属感会降低可懂度；这里只轻微前推。
    # 严重受损时推太前会把瑕疵暴露得更明显，所以这项保持很小。
    if max(upper_peak, harsh_peak, sib_peak) >= 8.5:
        compensation += 0.2
        reasons.append(
            f"peaky_hf={max(upper_peak, harsh_peak, sib_peak):.1f}dB"
        )

    repair = vocal_artifact_repair(plan)
    if repair.get("mode") == "split_high_repair":
        # 耀武类严重受损人声：推前会把瑕疵放大，不能再按“弱人声”大幅补偿。
        compensation = 0.0
        reasons.append("严重瑕疵档:不做人声前推补偿")

    return round(clamp(compensation, 0.0, WEAK_VOCAL_TARGET_MAX_LIFT_DB), 2), reasons


def reference_gap_value(ref_balance: dict[str, Any]) -> float | None:
    """优先取 active 人声/伴奏比例，旧字段只做兼容 fallback。"""
    value = ref_balance.get("active_vocal_minus_accomp_db")
    if value is None:
        value = ref_balance.get("vocal_minus_accomp_db")
    return float(value) if value is not None else None


def gated_vocal_balance_compensation_db(
    plan: dict | None,
    ref_gap_value: float | None,
) -> tuple[float, list[str]]:
    """把弱人声补偿统一套上参考曲 dry-deficit 限制。

    有参考曲时，只有输入干声本身已经明显比参考比例更埋，才额外前推。
    没有参考曲时走通用补偿，因为没有 dry/reference deficit 可判断。
    """
    compensation, reasons = vocal_balance_compensation_db(plan)
    if ref_gap_value is None:
        return compensation, reasons

    bus = (
        ((plan or {}).get("reference") or {})
        .get("overrides", {})
        .get("bus_balance", {})
    )
    dry_gap = bus.get("dry_input_vocal_minus_accomp_db")
    if dry_gap is None:
        return compensation, reasons

    # 健康干声通常只是渲染后需要常规 bus balance；不要额外推前。
    # 只有输入素材本身已经明显比参考曲更埋，才启用“问题干声可懂度补偿”。
    dry_deficit = float(ref_gap_value) - float(dry_gap)
    if dry_deficit < 3.5:
        return 0.0, [*reasons, f"补偿跳过:dry_deficit={dry_deficit:.1f}dB"]

    max_by_deficit = clamp(0.8 + (dry_deficit - 3.5) * 1.1, 0.8, WEAK_VOCAL_TARGET_MAX_LIFT_DB)
    if compensation > max_by_deficit:
        compensation = round(max_by_deficit, 2)
        reasons = [*reasons, f"按dry_deficit限制={dry_deficit:.1f}dB"]
    return compensation, reasons


def target_gap_from_plan(
    plan: dict | None,
    ref_balance: dict[str, Any],
) -> tuple[float, float | None, float, str, list[str]]:
    """返回最终 active vocal-accomp 目标；无参考曲时用通用目标。"""
    ref_gap_value = reference_gap_value(ref_balance)
    if ref_gap_value is None:
        base_gap = GENERIC_ACTIVE_GAP_DB
        source = "generic"
    else:
        base_gap = ref_gap_value
        source = "reference"

    compensation, reasons = gated_vocal_balance_compensation_db(plan, ref_gap_value)
    artifact_pullback = 0.0
    if vocal_artifact_repair(plan).get("mode") == "split_high_repair":
        artifact_pullback = SEVERE_ARTIFACT_PULLBACK_DB
        reasons = [*reasons, f"严重瑕疵档:目标后退{artifact_pullback:.1f}dB"]

    target_gap = clamp(base_gap + compensation - artifact_pullback, TARGET_GAP_MIN_DB, TARGET_GAP_MAX_DB)
    return round(target_gap, 2), ref_gap_value, compensation, source, reasons


def measure_render_balance(
    vocal_group: Path,
    accomp_bus: Path,
    include_loudness: bool = True,
) -> dict[str, float | int | None]:
    vocal_audio, sr = load_audio_as_float(vocal_group)
    accomp_audio, _ = load_audio_as_float(accomp_bus)
    n = min(vocal_audio.shape[0], accomp_audio.shape[0])
    vocal_audio = vocal_audio[:n]
    accomp_audio = accomp_audio[:n]
    active_regions = active_intervals_from_vocal(vocal_audio, sr)
    vocal_active_rms = rms_db_for_intervals(to_mono(vocal_audio), sr, active_regions)
    accomp_active_rms = rms_db_for_intervals(to_mono(accomp_audio), sr, active_regions)
    out: dict[str, float | int | None] = {
        "active_vocal_rms_db": round(vocal_active_rms, 3),
        "active_accomp_rms_db": round(accomp_active_rms, 3),
        "active_vocal_minus_accomp_db": round(vocal_active_rms - accomp_active_rms, 2),
        "active_region_count": len(active_regions),
    }
    if include_loudness:
        vocal_lufs = measure_loudness(vocal_group)["lufs_i"]
        accomp_lufs = measure_loudness(accomp_bus)["lufs_i"]
        out.update(
            {
                "vocal_lufs_i": round(vocal_lufs, 2),
                "accomp_lufs_i": round(accomp_lufs, 2),
                "vocal_minus_accomp_lufs_db": round(vocal_lufs - accomp_lufs, 2),
                "loudness_measurement": "enabled",
            }
        )
    else:
        out.update(
            {
                "vocal_lufs_i": None,
                "accomp_lufs_i": None,
                "vocal_minus_accomp_lufs_db": None,
                "loudness_measurement": "skipped_not_used_for_bus_gain",
            }
        )
    return out


def apply_dead_band(gain_db: float) -> float:
    if abs(gain_db) < BUS_DEAD_BAND_DB:
        return 0.0
    return round(gain_db, 2)


def compute_bus_gains(
    ref_balance: dict[str, Any],
    measured: dict[str, float | int | None],
    plan: dict | None = None,
) -> dict[str, Any]:
    vocal_gain = 0.0
    accomp_gain = 0.0
    target_gap, ref_gap_value, compensation, target_source, compensation_reasons = target_gap_from_plan(
        plan,
        ref_balance,
    )
    render_gap = float(measured["active_vocal_minus_accomp_db"])

    raw_correction = target_gap - render_gap
    # 按距离动态放开修正上限：越埋越允许多修，接近目标时保持保守。
    gap_distance = abs(raw_correction)
    unlock = clamp(gap_distance / BUS_RATIO_FULL_UNLOCK_GAP_DB, 0.0, 1.0)
    dyn_cap = BUS_RATIO_MIN_CORRECTION_DB + unlock * (
        BUS_RATIO_MAX_CORRECTION_DB - BUS_RATIO_MIN_CORRECTION_DB
    )
    correction = clamp(raw_correction, -dyn_cap, dyn_cap)
    vocal_cap = dyn_cap * BUS_RATIO_VOCAL_GAIN_FRACTION
    accomp_cap = dyn_cap * BUS_RATIO_ACCOMP_ATTEN_FRACTION
    if correction > 0.0:
        vocal_gain = apply_dead_band(clamp(correction * BUS_RATIO_VOCAL_GAIN_FRACTION, 0.0, vocal_cap))
        accomp_gain = apply_dead_band(clamp(-correction * BUS_RATIO_ACCOMP_ATTEN_FRACTION, -accomp_cap, 0.0))
    else:
        vocal_gain = apply_dead_band(clamp(correction * BUS_RATIO_VOCAL_GAIN_FRACTION, -BUS_MAX_ATTEN_DB, 0.0))
        accomp_gain = 0.0
    predicted_gap = round(render_gap + vocal_gain - accomp_gain, 2)
    reason = (
        f"match {target_source} active vocal/accomp target with weak-vocal compensation: "
        f"render gap {render_gap:+.1f} dB -> predicted {predicted_gap:+.1f} dB; "
        f"target {target_gap:+.1f} dB"
        + (f" (reference {float(ref_gap_value):+.1f} dB" if ref_gap_value is not None else " (generic fallback")
        + f", compensation +{compensation:.1f} dB); "
        f"cap {dyn_cap:.1f} dB (distance {gap_distance:.1f} dB); "
        f"correction applied {correction:+.1f} dB."
    )

    return {
        "vocal_bus_gain_db": vocal_gain,
        "accomp_bus_gain_db": accomp_gain,
        "reference_vocal_lufs_i": ref_balance.get("vocal_lufs"),
        "reference_accomp_lufs_i": ref_balance.get("accomp_lufs"),
        "reference_vocal_minus_accomp_lufs_db": ref_balance.get("vocal_minus_accomp_db"),
        "reference_active_vocal_minus_accomp_db": ref_gap_value,
        "target_active_vocal_minus_accomp_db": target_gap,
        "target_source": target_source,
        "weak_vocal_compensation_db": compensation,
        "weak_vocal_compensation_reasons": compensation_reasons,
        "policy": "match_active_vocal_accomp_target_with_generic_fallback_and_weak_vocal_compensation",
        "measurement_basis": "post_fx_vocal_group_and_accomp_bus_active_vocal_regions",
        "reason": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute bus balance from post-FX render buses.")
    parser.add_argument("vocal_group", type=Path, help="Post-FX vocal group WAV.")
    parser.add_argument("accomp_bus", type=Path, help="Post-FX accompaniment bus WAV.")
    parser.add_argument("--plan", type=Path, default=None, help="Resolved mix plan with reference features.")
    parser.add_argument("--metadata", type=Path, default=None, help="Optional JSON report path.")
    parser.add_argument(
        "--skip-loudness",
        action="store_true",
        help="Skip integrated LUFS metadata scans; bus gains use active RMS only.",
    )
    args = parser.parse_args()

    plan = load_json(args.plan) if args.plan and args.plan.exists() else None
    measured = measure_render_balance(args.vocal_group, args.accomp_bus, include_loudness=not args.skip_loudness)
    ref_balance = reference_levels(plan)
    bus = compute_bus_gains(ref_balance, measured, plan)

    metadata = {**measured, **bus, "reference_balance": ref_balance}
    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[bus-balance] vocal {bus['vocal_bus_gain_db']:+.2f} dB, "
        f"accomp {bus['accomp_bus_gain_db']:+.2f} dB",
        file=sys.stderr,
    )
    print(f"[bus-balance] {bus['reason']}", file=sys.stderr)
    print(f"{bus['vocal_bus_gain_db']:.3f} {bus['accomp_bus_gain_db']:.3f}")


if __name__ == "__main__":
    main()
