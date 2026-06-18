#!/usr/bin/env python3
"""在一次 FFmpeg pass 里应用 residual EQ、source_cleanup EQ 和 HF guard。"""

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply plan-driven vocal EQ actions.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
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

    hf_guard = overrides.get("vocal_hf_guard") or {}
    hf_actions = hf_guard.get("actions", []) if hf_guard.get("enabled") else []

    # 高频保护放最后：先做问题频段修正，再削共振和高频颗粒。
    ordered_actions = [*residual_actions, *source_actions, *hf_actions]
    filters = [value for action in ordered_actions if (value := eq_filter(action))]

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if not filters:
        shutil.copyfile(args.input_wav, args.output_wav)
        print("[vocal-plan-eq] no actions")
        return

    print("[vocal-plan-eq] applying:")
    for action in ordered_actions:
        print(
            "  - "
            f"{action.get('source')} {action.get('band')} {action.get('type')} "
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
