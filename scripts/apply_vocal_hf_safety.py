#!/usr/bin/env python3
"""Apply a conservative vocal high-frequency safety pass.

This sits after the selected vocal insert chain and before group FX / external
DelayVerb send. It is intentionally dynamic-first: the goal is to catch harsh
sibilant bursts without darkening the whole mix or changing bus balance.
"""

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
    parser = argparse.ArgumentParser(description="Apply vocal HF/sibilance safety processing.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--intensity", type=float, default=0.46)
    parser.add_argument("--max-deess", type=float, default=0.68)
    parser.add_argument("--freq", type=float, default=0.56)
    parser.add_argument("--static-cut-db", type=float, default=-0.8)
    parser.add_argument("--static-cut-hz", type=float, default=8500.0)
    parser.add_argument("--static-cut-q", type=float, default=1.5)
    args = parser.parse_args()

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = command_path("ffmpeg")
    filt = ",".join(
        [
            (
                "deesser="
                f"i={max(0.0, min(1.0, args.intensity)):.4f}:"
                f"m={max(0.0, min(1.0, args.max_deess)):.4f}:"
                f"f={max(0.0, min(1.0, args.freq)):.4f}"
            ),
            (
                "equalizer="
                f"f={args.static_cut_hz:.2f}:t=q:w={args.static_cut_q:.3f}:"
                f"g={args.static_cut_db:.3f}"
            ),
            "alimiter=limit=0.985:attack=1:release=35:level=false:latency=true",
        ]
    )
    print(
        "[hf-safety] deesser "
        f"i={args.intensity:.2f}, max={args.max_deess:.2f}, f={args.freq:.2f}; "
        f"cut {args.static_cut_db:.1f} dB @ {args.static_cut_hz:.0f} Hz"
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
            filt,
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
    if proc.returncode != 0:
        raise SystemExit(proc.stderr)


if __name__ == "__main__":
    main()
