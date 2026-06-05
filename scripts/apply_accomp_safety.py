#!/usr/bin/env python3
"""Repair clipped/clicky accompaniment transients before template processing."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        ROOT / ".tools" / "msys64" / "ucrt64" / "bin" / f"{name}.exe",
        ROOT / ".tools" / "msys64" / "usr" / "bin" / f"{name}.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return name


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply accompaniment declip/declick safety processing.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--declick-threshold", type=float, default=3.0)
    parser.add_argument("--wide-declick-threshold", type=float, default=2.6)
    parser.add_argument("--limit", type=float, default=0.965)
    args = parser.parse_args()

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = command_path("ffmpeg")
    filters = ",".join(
        [
            "adeclip=w=55:o=75:a=8:t=10",
            f"adeclick=w=55:o=75:a=2:t={args.declick_threshold:.3f}",
            f"adeclick=w=85:o=85:a=4:t={args.wide_declick_threshold:.3f}:b=4",
            f"alimiter=limit={args.limit:.6f}:attack=2:release=60:level=false:latency=true",
        ]
    )
    proc = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-nostdin",
            "-i",
            str(args.input_wav),
            "-af",
            filters,
            "-c:a",
            "pcm_f32le",
            str(args.output_wav),
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.stderr:
        print(proc.stderr, end="")
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


if __name__ == "__main__":
    main()
