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


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply plan-driven vocal EQ actions.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument(
        "--eq-stage",
        choices=("all", "cleanup", "timbre", "post_timbre"),
        default="all",
        help="all=兼容旧流程；cleanup=音色前源人声清理；timbre=只做音色参考；post_timbre=音色之后的清理/保护。",
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan)
    residual = plan.get("residual_vocal_eq", {})
    residual_actions = residual.get("actions", []) if residual.get("enabled") else []

    source_cleanup = plan.get("source_cleanup") or {}
    # 优先读取新的自驱动清理块；reference fallback 只用于兼容更早保存的旧 plan。
    overrides = source_cleanup or ((plan.get("reference") or {}).get("overrides") or {})
    source_eq = overrides.get("source_eq") or {}
    vocal_eq = source_eq.get("vocal_eq") or {}
    source_actions = vocal_eq.get("actions", []) if vocal_eq.get("enabled") else []
    timbre_eq = source_eq.get("timbre_vocal_eq") or {}
    timbre_actions = timbre_eq.get("actions", []) if timbre_eq.get("enabled") else []

    hf_guard = overrides.get("vocal_hf_guard") or {}
    hf_actions = hf_guard.get("actions", []) if hf_guard.get("enabled") else []

    skipped_by_budget: list[dict[str, Any]] = []
    if args.eq_stage == "cleanup":
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
