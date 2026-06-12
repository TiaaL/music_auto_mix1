#!/usr/bin/env python3
"""Raise vocal and accompaniment bus levels before stereo summing."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_reference import measure_loudness  # noqa: E402

FINAL_LOUDNESS_MIN_LUFS = -13.0
FINAL_LOUDNESS_MAX_LUFS = -11.0
DEFAULT_FINAL_TARGET_LUFS = -12.0
DEFAULT_BUS_TARGET_LUFS = -14.0
MAX_VOCAL_STAGING_DB = 12.0
MAX_ACCOMP_STAGING_DB = 9.0
AMIX_COMPENSATION_DB = 3.0


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def clamp_final_target(lufs: float) -> float:
    return clamp(lufs, FINAL_LOUDNESS_MIN_LUFS, FINAL_LOUDNESS_MAX_LUFS)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-2000:])


def apply_gain(input_path: Path, output_path: Path, gain_db: float, ffmpeg: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if abs(gain_db) < 0.01:
        shutil.copyfile(input_path, output_path)
        return
    run_ffmpeg(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-nostats",
            "-i",
            str(input_path),
            "-af",
            f"volume={gain_db:.4f}dB",
            str(output_path),
        ]
    )


def parse_loudnorm_input_i(stderr: str) -> float:
    match = re.search(r"\{[\s\S]*?\}", stderr)
    if not match:
        raise RuntimeError("loudnorm JSON not found")
    return float(json.loads(match.group(0))["input_i"])


def measure_reference_lufs(path: Path, ffmpeg: str) -> float:
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-23.0:TP=-2.0:LRA=11.0:print_format=json",
            "-f",
            "null",
            "-",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-500:])
    return parse_loudnorm_input_i(proc.stderr)


def staging_config_from_plan(plan: dict | None, reference_audio: Path | None, ffmpeg: str) -> dict:
    staging = {}
    if plan:
        staging = (
            (plan.get("reference") or {})
            .get("overrides", {})
            .get("master_level_staging", {})
        ) or {}

    final_target = staging.get("final_target_lufs_i")
    vocal_target = staging.get("vocal_target_lufs_i")
    accomp_target = staging.get("accomp_target_lufs_i")

    if final_target is None and reference_audio and reference_audio.exists():
        final_target = clamp_final_target(measure_reference_lufs(reference_audio, ffmpeg))

    if final_target is None:
        final_target = DEFAULT_FINAL_TARGET_LUFS

    final_target = clamp_final_target(float(final_target))

    if vocal_target is None:
        vocal_target = DEFAULT_BUS_TARGET_LUFS
    if accomp_target is None:
        accomp_target = DEFAULT_BUS_TARGET_LUFS

    return {
        "enabled": bool(staging.get("enabled", True)),
        "policy": staging.get("policy", "match_reference_bus_lufs_before_sum"),
        "final_target_lufs_i": float(final_target),
        "vocal_target_lufs_i": float(vocal_target),
        "accomp_target_lufs_i": float(accomp_target),
        "max_vocal_staging_db": float(staging.get("max_vocal_staging_db", MAX_VOCAL_STAGING_DB)),
        "max_accomp_staging_db": float(staging.get("max_accomp_staging_db", MAX_ACCOMP_STAGING_DB)),
        "amix_compensation_db": float(staging.get("amix_compensation_db", AMIX_COMPENSATION_DB)),
    }


def compute_staging_gain(
    measured_lufs: float,
    target_lufs: float,
    max_gain_db: float,
    amix_compensation_db: float,
) -> tuple[float, bool]:
    needed = target_lufs - measured_lufs + amix_compensation_db
    capped = needed > max_gain_db + 0.01
    gain_db = clamp(needed, 0.0, max_gain_db)
    return round(gain_db, 3), capped


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply master level staging to vocal/accomp buses.")
    parser.add_argument("vocal_in", type=Path)
    parser.add_argument("accomp_in", type=Path)
    parser.add_argument("vocal_out", type=Path)
    parser.add_argument("accomp_out", type=Path)
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--reference-audio", type=Path, default=None)
    parser.add_argument("--metadata", type=Path, default=None)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan) if args.plan and args.plan.exists() else None
    config = staging_config_from_plan(plan, args.reference_audio, args.ffmpeg)

    if not config["enabled"]:
        shutil.copyfile(args.vocal_in, args.vocal_out)
        shutil.copyfile(args.accomp_in, args.accomp_out)
        print("[staging] disabled; buses copied unchanged")
        return

    vocal_measured = measure_loudness(args.vocal_in)
    accomp_measured = measure_loudness(args.accomp_in)

    vocal_gain, vocal_capped = compute_staging_gain(
        vocal_measured["lufs_i"],
        config["vocal_target_lufs_i"],
        config["max_vocal_staging_db"],
        config["amix_compensation_db"],
    )
    accomp_gain, accomp_capped = compute_staging_gain(
        accomp_measured["lufs_i"],
        config["accomp_target_lufs_i"],
        config["max_accomp_staging_db"],
        config["amix_compensation_db"],
    )

    apply_gain(args.vocal_in, args.vocal_out, vocal_gain, args.ffmpeg)
    apply_gain(args.accomp_in, args.accomp_out, accomp_gain, args.ffmpeg)

    metadata = {
        "enabled": True,
        "policy": config["policy"],
        "final_target_lufs_i": config["final_target_lufs_i"],
        "final_loudness_window_lufs": [FINAL_LOUDNESS_MIN_LUFS, FINAL_LOUDNESS_MAX_LUFS],
        "vocal": {
            "measured_lufs_i": round(vocal_measured["lufs_i"], 2),
            "target_lufs_i": config["vocal_target_lufs_i"],
            "staging_gain_db": vocal_gain,
            "capped": vocal_capped,
        },
        "accomp": {
            "measured_lufs_i": round(accomp_measured["lufs_i"], 2),
            "target_lufs_i": config["accomp_target_lufs_i"],
            "staging_gain_db": accomp_gain,
            "capped": accomp_capped,
        },
        "amix_compensation_db": config["amix_compensation_db"],
    }

    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        "[staging] "
        f"vocal {vocal_measured['lufs_i']:.1f} -> +{vocal_gain:.1f} dB "
        f"(target {config['vocal_target_lufs_i']:.1f}); "
        f"accomp {accomp_measured['lufs_i']:.1f} -> +{accomp_gain:.1f} dB "
        f"(target {config['accomp_target_lufs_i']:.1f}); "
        f"final target {config['final_target_lufs_i']:.1f} LUFS"
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
