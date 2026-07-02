#!/usr/bin/env python3
"""按阶段应用 plan 里的人声 EQ。

音色筛选片段 EQ 可以单独放在模板链前面；自清理和高频保护留在模板链后面兜底。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


LEGACY_TEXTURE_HIGH_CUT_CAP_DB = {
    # v0.1 质感模式只允许高频轻修，避免把毛边、气口和齿音颗粒当瑕疵削掉。
    "upper": 0.8,
    "harsh": 0.75,
    "sib": 0.65,
    "air": 0.0,
}

VOCAL_CORE_PROTECT_LOW_HZ = 1000.0
VOCAL_CORE_PROTECT_HIGH_HZ = 1600.0
VOCAL_CORE_PROTECTED_SOURCES = {
    "covered_strong_hit",
    "uncovered_feature_hit",
    "spectral_deviation",
    "ratio_excess",
    "reference_vocal_tonal_delta",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def eq_filter(action: dict[str, Any]) -> str | None:
    # 低通动作没有增益，只负责移除指定频率以上的内容；
    # 人声高频保护用它削掉 Nyquist 墙以上的重采样颗粒。
    if action.get("type") == "lowpass":
        try:
            freq = float(action["freq_hz"])
            q = float(action.get("q", 0.707))
        except (TypeError, ValueError):
            return None
        if freq <= 0.0 or q <= 0.0:
            return None
        return f"lowpass=f={freq:.3f}:width_type=q:width={q:.3f}"
    try:
        freq = float(action["freq_hz"])
        q = float(action["q"])
        gain = float(action["gain_db"])
    except (KeyError, TypeError, ValueError):
        return None
    if abs(gain) < 0.05 or freq <= 0.0 or q <= 0.0:
        return None
    return f"equalizer=f={freq:.3f}:width_type=q:width={q:.3f}:g={gain:.3f}"


def cap_cumulative_cuts(
    actions: list[dict[str, Any]],
    prior_actions: list[dict[str, Any]],
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按统一决策层限制同一频段累计 cut，避免多个 guard 叠刀。"""
    context = plan.get("vocal_processing_context") or {}
    budget = (context.get("band_budget") or {}).get("max_total_cut_db") or {}
    if not budget:
        return actions, []

    used: dict[str, float] = {}
    for action in prior_actions:
        band = str(action.get("band") or "")
        gain = action.get("gain_db")
        if band and isinstance(gain, (int, float)) and float(gain) < 0.0:
            used[band] = used.get(band, 0.0) + abs(float(gain))

    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in actions:
        band = str(action.get("band") or "")
        gain = action.get("gain_db")
        if not band or not isinstance(gain, (int, float)) or float(gain) >= 0.0:
            kept.append(action)
            continue
        if not isinstance(budget.get(band), (int, float)):
            kept.append(action)
            continue
        remaining = float(budget[band]) - used.get(band, 0.0)
        if remaining <= 0.05:
            skipped.append({
                **action,
                "reason": f"{action.get('reason', '')}; unified band budget exhausted",
            })
            continue
        amount = abs(float(gain))
        adjusted = dict(action)
        if amount > remaining:
            adjusted["gain_db"] = -round(remaining, 2)
            adjusted["reason"] = (
                f"{adjusted.get('reason', '')}; capped by unified band budget "
                f"({used.get(band, 0.0):.2f}+{remaining:.2f}/{float(budget[band]):.2f} dB)"
            )
            amount = remaining
        used[band] = used.get(band, 0.0) + amount
        kept.append(adjusted)
    return kept, skipped


def cap_legacy_texture_high_cuts(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """v0.1 质感白名单：低/低中/中频照常修，高频只轻削或跳过。"""
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in actions:
        band = str(action.get("band") or "")
        gain = action.get("gain_db")
        if band not in LEGACY_TEXTURE_HIGH_CUT_CAP_DB or not isinstance(gain, (int, float)) or float(gain) >= 0.0:
            kept.append(action)
            continue
        cap = float(LEGACY_TEXTURE_HIGH_CUT_CAP_DB[band])
        if cap <= 0.0:
            skipped.append({
                **action,
                "reason": f"{action.get('reason', '')}; skipped by v0.1 texture high-band whitelist",
            })
            continue
        amount = abs(float(gain))
        if amount <= cap:
            kept.append(action)
            continue
        kept.append({
            **action,
            "gain_db": -round(cap, 2),
            "reason": (
                f"{action.get('reason', '')}; capped by v0.1 texture high-band whitelist "
                f"({amount:.2f}->{cap:.2f} dB)"
            ),
        })
    return kept, skipped


def protect_vocal_core_cuts(actions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """保护 1k-1.6k 人声核心：residual/legacy 阶段不能轻易削掉“芯”。

    这里不阻止 boost，也不阻止低中频清理；只拦截 residual/legacy 的核心频段 cut。
    这类 cut 很容易把 lead 的密度、字头实体感和贴脸中心感削空。
    """
    kept: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for action in actions:
        action_type = str(action.get("type") or "")
        source = str(action.get("source") or "")
        try:
            freq_hz = float(action.get("freq_hz") or 0.0)
            gain_db = float(action.get("gain_db") or 0.0)
        except (TypeError, ValueError):
            kept.append(action)
            continue
        if (
            action_type == "cut"
            and gain_db < 0.0
            and VOCAL_CORE_PROTECT_LOW_HZ <= freq_hz <= VOCAL_CORE_PROTECT_HIGH_HZ
            and (source in VOCAL_CORE_PROTECTED_SOURCES or str(action.get("band") or "") == "mid")
        ):
            skipped.append({
                **action,
                "reason": (
                    f"{action.get('reason', '')}; skipped by vocal core protection "
                    f"({VOCAL_CORE_PROTECT_LOW_HZ:.0f}-{VOCAL_CORE_PROTECT_HIGH_HZ:.0f} Hz)"
                ),
            })
            continue
        kept.append(action)
    return kept, skipped


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply plan-driven vocal EQ actions.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--eq-stage",
        choices=("all", "cleanup", "timbre", "post_timbre", "legacy_v0_1"),
        default="all",
        help=(
            "all=兼容当前流程；cleanup=音色前源人声清理；timbre=只做音色参考；"
            "post_timbre=音色之后的清理/保护；legacy_v0_1=只用 v0.1 residual+reference vocal EQ。"
        ),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan)
    residual = plan.get("residual_vocal_eq", {})
    residual_actions = residual.get("actions", []) if residual.get("enabled") else []

    source_cleanup = plan.get("source_cleanup") or {}
    # 优先读取新的自驱动清理块；reference fallback 只用于兼容更早保存的旧 plan。
    reference_overrides = (plan.get("reference") or {}).get("overrides") or {}
    overrides = source_cleanup or reference_overrides
    source_eq = overrides.get("source_eq") or {}
    vocal_eq = source_eq.get("vocal_eq") or {}
    source_actions = vocal_eq.get("actions", []) if vocal_eq.get("enabled") else []
    legacy_source_eq = (reference_overrides.get("source_eq") or {}).get("vocal_eq") or {}
    legacy_source_actions = legacy_source_eq.get("actions", []) if legacy_source_eq.get("enabled") else []
    timbre_eq = source_eq.get("timbre_vocal_eq") or {}
    timbre_actions = timbre_eq.get("actions", []) if timbre_eq.get("enabled") else []

    hf_guard = overrides.get("vocal_hf_guard") or {}
    hf_actions = hf_guard.get("actions", []) if hf_guard.get("enabled") else []

    skipped_by_budget: list[dict[str, Any]] = []
    if args.eq_stage == "legacy_v0_1":
        # v0.1 的人声修复只叠 residual_vocal_eq 和 reference.overrides.source_eq.vocal_eq。
        # 不读取当前分支新增的 source_cleanup、timbre EQ、HF guard 等模块。
        ordered_actions = [*residual_actions, *legacy_source_actions]
        ordered_actions, skipped_by_budget = cap_legacy_texture_high_cuts(ordered_actions)
    elif args.eq_stage == "cleanup":
        # 音色塑形前先处理源人声问题；这里不追音色参考。
        ordered_actions = [*residual_actions, *source_actions, *hf_actions]
    elif args.eq_stage == "timbre":
        # 音色相似度只吃音色筛选片段动作；不混入瑕疵修复/HF guard。
        ordered_actions = [*timbre_actions]
    elif args.eq_stage == "post_timbre":
        # 后置阶段只做瑕疵修正和高频兜底，不再追音色，避免把相似度目标反复改写。
        ordered_actions = [*residual_actions, *source_actions, *hf_actions]
        ordered_actions, skipped_by_budget = cap_cumulative_cuts(ordered_actions, timbre_actions, plan)
    else:
        # 兼容旧调用：音色动作优先，最后仍由高频保护削掉危险共振/颗粒。
        ordered_actions = [*timbre_actions, *residual_actions, *source_actions, *hf_actions]
        skipped_by_budget = []
    ordered_actions, skipped_by_core = protect_vocal_core_cuts(ordered_actions)
    skipped_by_budget = [*skipped_by_budget, *skipped_by_core]
    filters = [value for action in ordered_actions if (value := eq_filter(action))]

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if not filters:
        shutil.copyfile(args.input_wav, args.output_wav)
        print(f"[vocal-plan-eq:{args.eq_stage}] no actions")
        return

    print(f"[vocal-plan-eq:{args.eq_stage}] applying:")
    for action in ordered_actions:
        print(
            "  - "
            f"{action.get('source')} {action.get('band')} {action.get('type')} "
            f"{action.get('gain_db')} dB @ {action.get('freq_hz')} Hz"
        )
    for action in skipped_by_budget:
        print(
            "  - skip "
            f"{action.get('source')} {action.get('band')} {action.get('type')} "
            f"{action.get('gain_db')} dB ({action.get('reason')})"
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
            "Vocal plan EQ failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
