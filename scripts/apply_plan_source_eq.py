#!/usr/bin/env python3
"""从 resolved mix plan 应用 source_cleanup EQ 动作。"""

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
    """把 source_cleanup/source_eq 动作转换成 FFmpeg equalizer filter。"""
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
    parser = argparse.ArgumentParser(description="Apply source EQ actions stored in a resolved mix plan.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--section", choices=("vocal_eq", "accomp_eq"), required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan)
    source_cleanup = plan.get("source_cleanup") or {}
    # 新 plan 会把“保留素材特点的 EQ”放在 reference 之外；
    # 兜底只用于让旧 resolved_mix_plan.json 还能正常渲染。
    overrides = source_cleanup or ((plan.get("reference") or {}).get("overrides") or {})
    source_eq = overrides.get("source_eq") or {}
    # --section 决定当前处理人声还是伴奏；脚本本身不做策略判断，只执行 plan。
    section = source_eq.get(args.section) or {}
    actions = section.get("actions", []) if section.get("enabled") else []
    filters = [value for action in actions if (value := eq_filter(action))]

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if not filters:
        shutil.copyfile(args.input_wav, args.output_wav)
        print(f"[source-eq:{args.section}] no actions")
        return

    print(f"[source-eq:{args.section}] applying:")
    for action in actions:
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
        "-c:a",
        "pcm_f32le",
        str(args.output_wav),
    ]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            "Source EQ failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
