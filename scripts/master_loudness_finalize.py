#!/usr/bin/env python3
"""Finalize master loudness with whole-song static gain and limiting."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
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


FFMPEG = command_path("ffmpeg")


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def parse_loudnorm_json(text: str) -> dict[str, float | str]:
    match = re.search(r"\{[\s\S]*?\}", text)
    if not match:
        raise RuntimeError(f"Could not find loudnorm JSON in ffmpeg output:\n{text[-2000:]}")
    raw = json.loads(match.group(0))
    parsed: dict[str, float | str] = {}
    for key, value in raw.items():
        try:
            parsed[key] = float(value)
        except (TypeError, ValueError):
            parsed[key] = value
    return parsed


def loudnorm_measure(path: Path, target_i: float, target_tp: float, target_lra: float) -> dict[str, float | str]:
    proc = run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    return parse_loudnorm_json(proc.stderr)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return -70.0
    ordered = sorted(values)
    idx = int(round((len(ordered) - 1) * clamp(q, 0.0, 1.0)))
    return ordered[idx]


def ebur128_sections(path: Path) -> dict[str, float | list[dict[str, float]]]:
    proc = run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    rows: list[dict[str, float]] = []
    for line in proc.stderr.splitlines():
        match = re.search(r"t:\s*([0-9.]+).*?M:\s*([-0-9.]+)\s*S:\s*([-0-9.]+)", line)
        if not match:
            continue
        momentary = float(match.group(2))
        shortterm = float(match.group(3))
        if shortterm <= -70.0:
            continue
        rows.append(
            {
                "time": float(match.group(1)),
                "momentary_lufs": momentary,
                "shortterm_lufs": shortterm,
            }
        )
    shortterms = [row["shortterm_lufs"] for row in rows]
    return {
        "p50_shortterm_lufs": percentile(shortterms, 0.50),
        "p85_shortterm_lufs": percentile(shortterms, 0.85),
        "p90_shortterm_lufs": percentile(shortterms, 0.90),
        "p95_shortterm_lufs": percentile(shortterms, 0.95),
        "sections": rows,
    }


def ebur128_window_summary(
    sections: dict[str, float | list[dict[str, float]]],
    start: float,
    end: float,
) -> dict[str, float | int]:
    rows = [
        row
        for row in sections.get("sections", [])
        if isinstance(row, dict) and start <= float(row["time"]) <= end
    ]
    momentary = [float(row["momentary_lufs"]) for row in rows]
    shortterm = [float(row["shortterm_lufs"]) for row in rows]
    return {
        "start": start,
        "end": end,
        "samples": len(rows),
        "momentary_min_lufs": min(momentary) if momentary else -70.0,
        "momentary_max_lufs": max(momentary) if momentary else -70.0,
        "momentary_avg_lufs": sum(momentary) / len(momentary) if momentary else -70.0,
        "shortterm_min_lufs": min(shortterm) if shortterm else -70.0,
        "shortterm_max_lufs": max(shortterm) if shortterm else -70.0,
        "shortterm_avg_lufs": sum(shortterm) / len(shortterm) if shortterm else -70.0,
    }


def apply_gain(input_path: Path, output_path: Path, gain_db: float) -> None:
    proc = run(
        [
            FFMPEG,
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
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)


def apply_true_peak_limiter(input_path: Path, output_path: Path, ceiling_db: float) -> None:
    ceiling = 10.0 ** (ceiling_db / 20.0)
    filter_graph = (
        "aresample=192000,"
        f"alimiter=limit={ceiling:.8f}:attack=5:release=80:level=false:latency=true,"
        "aresample=44100"
    )
    proc = run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-nostats",
            "-i",
            str(input_path),
            "-af",
            filter_graph,
            str(output_path),
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def measure_reference_lufs(ref_path: Path) -> tuple[float, float]:
    m = loudnorm_measure(ref_path, target_i=-23.0, target_tp=-2.0, target_lra=11.0)
    return float(m["input_i"]), float(m["input_lra"])


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize master loudness.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--limiter", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path, default=None,
                        help="Reference track; its integrated LUFS becomes the target.")
    parser.add_argument("--target-i", type=float, default=-10.0)
    parser.add_argument("--target-tp", type=float, default=-0.8)
    parser.add_argument("--target-lra", type=float, default=11.0)
    parser.add_argument("--max-pre-gain-db", type=float, default=9.0)
    parser.add_argument("--max-attenuation-db", type=float, default=12.0)
    parser.add_argument("--max-residual-gain-db", type=float, default=3.5)
    parser.add_argument("--max-true-peak-limiter-reduction-db", type=float, default=4.0,
                        help="Maximum peak reduction allowed while chasing LUFS; prevents harsh limiter crackle.")
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = args.metadata or args.output_wav.with_suffix(".loudness.json")

    reference_meta: dict | None = None
    if args.reference_audio:
        print(f"[ref] Measuring reference: {args.reference_audio}")
        ref_i, ref_lra = measure_reference_lufs(args.reference_audio)
        args.target_i = ref_i
        args.target_lra = max(6.0, min(14.0, ref_lra))
        reference_meta = {
            "path": str(args.reference_audio),
            "input_i_lufs": ref_i,
            "input_lra": ref_lra,
            "derived_target_i": args.target_i,
            "derived_target_lra": args.target_lra,
        }
        print(f"[ref] Derived target: {args.target_i:.1f} LUFS, LRA {args.target_lra:.1f} LU")

    pre_measure = loudnorm_measure(args.input_wav, args.target_i, args.target_tp, args.target_lra)
    pre_sections = ebur128_sections(args.input_wav)
    input_i = float(pre_measure["input_i"])
    desired_gain_db = clamp(
        args.target_i - input_i,
        -args.max_attenuation_db,
        args.max_pre_gain_db,
    )
    whole_song_gain_db = desired_gain_db
    target_limited_by_true_peak = False

    with tempfile.TemporaryDirectory(prefix="master_loudness_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        gained = tmp_root / "01_pregain.wav"
        limited = tmp_root / "02_limited.wav"
        tp_limited = tmp_root / "03_true_peak_limited.wav"

        limited_measure: dict[str, float | str] | None = None
        for attempt in range(2):
            apply_gain(args.input_wav, gained, whole_song_gain_db)
            limiter_proc = run([str(args.limiter), str(gained), str(limited)])
            if limiter_proc.returncode != 0:
                raise RuntimeError(limiter_proc.stderr)
            limited_measure = loudnorm_measure(limited, args.target_i, args.target_tp, args.target_lra)
            required_tp_reduction = float(limited_measure["input_tp"]) - args.target_tp
            if required_tp_reduction <= args.max_true_peak_limiter_reduction_db or attempt == 1:
                break
            excess_reduction = required_tp_reduction - args.max_true_peak_limiter_reduction_db
            whole_song_gain_db -= excess_reduction
            target_limited_by_true_peak = True

        assert limited_measure is not None
        limited_measure = loudnorm_measure(limited, args.target_i, args.target_tp, args.target_lra)
        residual_needed_db = args.target_i - float(limited_measure["input_i"])
        peak_headroom_db = args.target_tp - float(limited_measure["input_tp"])
        residual_gain_high = args.max_residual_gain_db if peak_headroom_db >= 0.0 else 0.0
        residual_gain_db = clamp(
            residual_needed_db,
            0.0 - args.max_attenuation_db,
            min(args.max_residual_gain_db, residual_gain_high),
        )
        apply_gain(limited, tp_limited, residual_gain_db)
        apply_true_peak_limiter(tp_limited, args.output_wav, args.target_tp)

    final_measure = loudnorm_measure(args.output_wav, args.target_i, args.target_tp, args.target_lra)
    final_sections = ebur128_sections(args.output_wav)
    final_focus_windows = {
        "38_50s": ebur128_window_summary(final_sections, 38.0, 50.0),
    }
    metadata = {
        "enabled": True,
        "mode": "integrated_static_gain",
        "reference_audio": reference_meta,
        "target_i_lufs": args.target_i,
        "target_tp_db": args.target_tp,
        "target_lra": args.target_lra,
        "max_pre_gain_db": args.max_pre_gain_db,
        "max_attenuation_db": args.max_attenuation_db,
        "max_residual_gain_db": args.max_residual_gain_db,
        "max_true_peak_limiter_reduction_db": args.max_true_peak_limiter_reduction_db,
        "desired_gain_db": desired_gain_db,
        "target_limited_by_true_peak": target_limited_by_true_peak,
        "whole_song_gain_db": whole_song_gain_db,
        "residual_gain_db": residual_gain_db,
        "pre_limiter_gain_db": whole_song_gain_db,
        "pre_master": pre_measure,
        "pre_master_sections": {key: value for key, value in pre_sections.items() if key != "sections"},
        "post_limiter": limited_measure,
        "final": final_measure,
        "actual_i_lufs": final_measure.get("input_i"),
        "actual_tp_db": final_measure.get("input_tp"),
        "actual_lra": final_measure.get("input_lra"),
        "final_sections": {key: value for key, value in final_sections.items() if key != "sections"},
        "final_focus_windows": final_focus_windows,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
