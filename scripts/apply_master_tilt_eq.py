#!/usr/bin/env python3
"""Apply reference-driven master tilt EQ stored in a resolved mix plan."""

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
    parser = argparse.ArgumentParser(description="Apply master tilt EQ derived from reference features.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan)
    overrides = (plan.get("reference") or {}).get("overrides") or {}
    tilt = overrides.get("master_tilt_eq") or {}
    actions = tilt.get("actions", []) if tilt.get("enabled") else []
    filters = [value for action in actions if (value := eq_filter(action))]

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if not filters:
        shutil.copyfile(args.input_wav, args.output_wav)
        print("[master-tilt-eq] no actions")
        return

    print("[master-tilt-eq] applying:")
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
        str(args.output_wav),
    ]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            "Master tilt EQ failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
