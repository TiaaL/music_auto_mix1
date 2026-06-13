#!/usr/bin/env python3
"""Profile a legacy render and optionally compare its output against a baseline."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "calibration_outputs" / "profiles"
PROFILE_BANDS: dict[str, tuple[float, float]] = {
    "sub": (20.0, 90.0),
    "low": (90.0, 180.0),
    "lowmid": (180.0, 500.0),
    "mid": (500.0, 1200.0),
    "presence": (1200.0, 5000.0),
    "air": (5000.0, 16000.0),
}


def command_path(name: str) -> str:
    found = shutil.which(name)
    return found or name


def db(value: float, floor: float = -160.0) -> float:
    if value <= 0.0 or not math.isfinite(value):
        return floor
    return 20.0 * math.log10(value)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def loudnorm_measure(path: Path) -> dict[str, float | str]:
    try:
        proc = subprocess.run(
            [
                command_path("ffmpeg"),
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
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return {"error": str(exc)}
    if proc.returncode != 0:
        return {"error": proc.stderr[-500:]}
    start = proc.stderr.find("{")
    end = proc.stderr.rfind("}")
    if start < 0 or end < start:
        return {"error": "loudnorm JSON not found"}
    raw = json.loads(proc.stderr[start : end + 1])
    return {
        "i_lufs": float(raw["input_i"]),
        "tp_db": float(raw["input_tp"]),
        "lra": float(raw["input_lra"]),
    }


def final_loudness_for_mix(path: Path) -> dict[str, float | str]:
    measured = loudnorm_measure(path)
    if "error" not in measured:
        return measured
    metadata_path = path.with_suffix(".loudness.json")
    if not metadata_path.exists():
        return measured
    try:
        metadata = load_json(metadata_path)
    except (OSError, json.JSONDecodeError):
        return measured
    return {
        "i_lufs": metadata.get("actual_i_lufs"),
        "tp_db": metadata.get("actual_tp_db"),
        "lra": metadata.get("actual_lra"),
        "source": str(metadata_path),
    }


def audio_matrix(path: Path) -> tuple[np.ndarray, int]:
    data, sample_rate = sf.read(path, dtype="float32", always_2d=True)
    return data, int(sample_rate)


def band_profile_db(data: np.ndarray, sample_rate: int) -> dict[str, float]:
    mono = np.mean(data, axis=1)
    if mono.size == 0:
        return {name: -160.0 for name in PROFILE_BANDS}
    window = np.hanning(mono.size).astype(np.float32)
    spectrum = np.abs(np.fft.rfft(mono * window))
    freqs = np.fft.rfftfreq(mono.size, d=1.0 / sample_rate)
    out: dict[str, float] = {}
    for name, (low, high) in PROFILE_BANDS.items():
        mask = (freqs >= low) & (freqs < high)
        out[name] = round(db(float(np.mean(spectrum[mask]))) if np.any(mask) else -160.0, 3)
    return out


def wav_metrics(path: Path) -> dict[str, Any]:
    data, sample_rate = audio_matrix(path)
    mono = np.mean(data, axis=1)
    return {
        "path": str(path),
        "sample_rate": sample_rate,
        "frames": int(data.shape[0]),
        "channels": int(data.shape[1]),
        "duration_sec": round(float(data.shape[0] / sample_rate), 3),
        "rms_dbfs": round(db(float(np.sqrt(np.mean(data * data)))), 3),
        "peak_dbfs": round(db(float(np.max(np.abs(data)))), 3),
        "mono_band_profile_db": band_profile_db(data, sample_rate),
        "mono_mean_dbfs": round(db(float(np.sqrt(np.mean(mono * mono)))), 3),
    }


def compare_wavs(candidate: Path, reference: Path) -> dict[str, Any]:
    cand, cand_sr = audio_matrix(candidate)
    ref, ref_sr = audio_matrix(reference)
    frames = min(cand.shape[0], ref.shape[0])
    channels = min(cand.shape[1], ref.shape[1])
    if frames <= 0 or channels <= 0:
        raise ValueError("Cannot compare empty audio files.")
    cand_aligned = cand[:frames, :channels]
    ref_aligned = ref[:frames, :channels]
    diff = cand_aligned - ref_aligned
    cand_flat = cand_aligned.reshape(-1)
    ref_flat = ref_aligned.reshape(-1)
    corr = float(np.corrcoef(cand_flat, ref_flat)[0, 1]) if cand_flat.size > 1 else 1.0
    cand_profile = band_profile_db(cand_aligned, cand_sr)
    ref_profile = band_profile_db(ref_aligned, ref_sr)
    band_delta = {
        name: round(cand_profile[name] - ref_profile.get(name, -160.0), 3)
        for name in cand_profile
    }
    return {
        "candidate": wav_metrics(candidate),
        "reference": wav_metrics(reference),
        "sample_rate_match": cand_sr == ref_sr,
        "compared_frames": int(frames),
        "length_delta_frames": int(cand.shape[0] - ref.shape[0]),
        "channel_delta": int(cand.shape[1] - ref.shape[1]),
        "correlation": round(corr, 9),
        "diff_rms_dbfs": round(db(float(np.sqrt(np.mean(diff * diff)))), 3),
        "max_abs_diff": round(float(np.max(np.abs(diff))), 9),
        "band_delta_db": band_delta,
        "loudness": {
            "candidate": loudnorm_measure(candidate),
            "reference": loudnorm_measure(reference),
        },
    }


def summarize_stage_report(stage_report: Path | None) -> dict[str, Any]:
    if stage_report is None or not stage_report.exists():
        return {"path": str(stage_report) if stage_report else None, "stages": [], "total_sec": 0.0}
    report = load_json(stage_report)
    stages = report.get("stages") if isinstance(report.get("stages"), list) else []
    normalized: list[dict[str, Any]] = []
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        normalized.append(
            {
                "stage": stage.get("stage"),
                "elapsed_sec": round(float(stage.get("elapsed_sec") or 0.0), 3),
                "inputs": stage.get("inputs", {}),
                "outputs": stage.get("outputs", {}),
            }
        )
    return {
        "path": str(stage_report),
        "stages": normalized,
        "total_sec": round(sum(float(stage["elapsed_sec"]) for stage in normalized), 3),
        "slowest": sorted(normalized, key=lambda row: float(row["elapsed_sec"]), reverse=True)[:8],
    }


def run_legacy_render(args: argparse.Namespace, out_dir: Path) -> tuple[dict[str, Any], float]:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "run_latest_auto_mix.py"),
        str(args.vocal_wav),
        str(args.accomp_wav),
        "--batch-label",
        args.label,
        "--out-dir",
        str(out_dir),
        "--stage-report",
    ]
    if args.no_volume_automation:
        cmd.append("--no-volume-automation")
    if args.no_loudness_finalizer:
        cmd.append("--no-loudness-finalizer")
    if args.reference_audio:
        cmd += ["--reference-audio", str(args.reference_audio)]

    start = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    wall_sec = round(time.perf_counter() - start, 3)
    if proc.returncode != 0:
        raise SystemExit(
            "Legacy render failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout[-4000:]}\n"
            f"stderr:\n{proc.stderr[-4000:]}"
        )
    latest_path = out_dir / "LATEST.json"
    if not latest_path.exists():
        raise SystemExit(f"Render completed but LATEST.json was not found: {latest_path}")
    latest = load_json(latest_path)
    latest["profile_command"] = cmd
    return latest, wall_sec


def load_existing_summary(summary_path: Path) -> dict[str, Any]:
    summary = load_json(summary_path)
    return {
        "batch_label": Path(summary_path).stem.replace("_summary", ""),
        "mix_wav": summary.get("output_wav"),
        "summary_json": str(summary_path),
        "analysis_json": summary.get("analysis_json"),
        "resolved_mix_plan": summary.get("resolved_mix_plan"),
        "stage_report": summary.get("stage_report"),
        "selected_template": summary.get("selected_template"),
        "loudness": summary.get("loudness"),
    }


def render_markdown(profile: dict[str, Any]) -> str:
    lines = [
        f"# Render profile: {profile['label']}",
        "",
        f"- mode: `{profile['mode']}`",
        f"- mix: `{profile.get('mix_wav')}`",
        f"- wall time: `{profile.get('wall_sec', 'n/a')}` sec",
        f"- measured stage total: `{profile['stage_report']['total_sec']}` sec",
    ]
    loudness = profile.get("final_loudness") or {}
    if loudness:
        lines.append(
            f"- final loudness: `{loudness.get('i_lufs', 'n/a')}` LUFS / "
            f"`{loudness.get('tp_db', 'n/a')}` dBTP / LRA `{loudness.get('lra', 'n/a')}`"
        )
    lines += ["", "## Stages", "", "| Stage | Seconds |", "|---|---:|"]
    for stage in profile["stage_report"]["stages"]:
        lines.append(f"| `{stage['stage']}` | {stage['elapsed_sec']:.3f} |")
    lines += ["", "## Slowest", "", "| Stage | Seconds |", "|---|---:|"]
    for stage in profile["stage_report"].get("slowest", []):
        lines.append(f"| `{stage['stage']}` | {stage['elapsed_sec']:.3f} |")
    if profile.get("parity"):
        parity = profile["parity"]
        lines += [
            "",
            "## Parity",
            "",
            f"- reference: `{parity['reference']['path']}`",
            f"- correlation: `{parity['correlation']}`",
            f"- diff RMS: `{parity['diff_rms_dbfs']}` dBFS",
            f"- max abs diff: `{parity['max_abs_diff']}`",
            f"- length delta frames: `{parity['length_delta_frames']}`",
            "",
            "| Band | Candidate - Reference dB |",
            "|---|---:|",
        ]
        for band, delta in parity["band_delta_db"].items():
            lines.append(f"| `{band}` | {delta:.3f} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run/summarize a legacy render profile and optionally compare parity against a baseline WAV."
    )
    parser.add_argument("vocal_wav", nargs="?", type=Path)
    parser.add_argument("accomp_wav", nargs="?", type=Path)
    parser.add_argument("--label", default="profile_render")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--summary-json", type=Path, help="Summarize an existing auto_template_mix summary JSON.")
    parser.add_argument("--stage-report-json", type=Path, help="Summarize an existing stage report JSON directly.")
    parser.add_argument("--mix-wav", type=Path, help="Final mix WAV to measure when summarizing an existing report.")
    parser.add_argument("--compare-to", type=Path, help="Baseline WAV for parity metrics.")
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--no-volume-automation", action="store_true")
    parser.add_argument("--no-loudness-finalizer", action="store_true")
    args = parser.parse_args()

    out_dir = args.out_dir.resolve(strict=False)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.summary_json and args.stage_report_json:
        raise SystemExit("Pass either --summary-json or --stage-report-json, not both.")

    if args.summary_json:
        latest = load_existing_summary(args.summary_json)
        wall_sec: float | None = None
        mode = "summarize_existing"
    elif args.stage_report_json:
        latest = {
            "batch_label": args.label,
            "mix_wav": str(args.mix_wav) if args.mix_wav else None,
            "stage_report": str(args.stage_report_json),
        }
        wall_sec = None
        mode = "summarize_stage_report"
    else:
        if args.vocal_wav is None or args.accomp_wav is None:
            raise SystemExit("vocal_wav and accomp_wav are required unless --summary-json is passed.")
        latest, wall_sec = run_legacy_render(args, out_dir)
        mode = "legacy_render"

    mix_wav_value = latest.get("mix_wav") or latest.get("output_wav")
    mix_wav = Path(str(mix_wav_value)) if mix_wav_value else None
    stage_report_path = latest.get("stage_report")
    if not stage_report_path and mix_wav:
        stage_report_path = str(mix_wav.with_suffix(".stage_report.json"))
    stage_summary = summarize_stage_report(Path(stage_report_path) if stage_report_path else None)
    final_loudness = final_loudness_for_mix(mix_wav) if mix_wav and mix_wav.exists() else {}
    summary_json = latest.get("summary_json")
    if not summary_json and args.summary_json:
        summary_json = str(args.summary_json)

    profile: dict[str, Any] = {
        "label": latest.get("batch_label") or args.label,
        "mode": mode,
        "wall_sec": wall_sec,
        "mix_wav": str(mix_wav) if mix_wav else None,
        "summary_json": summary_json,
        "analysis_json": latest.get("analysis_json"),
        "resolved_mix_plan": latest.get("resolved_mix_plan"),
        "selected_template": latest.get("selected_template"),
        "stage_report": stage_summary,
        "final_loudness": final_loudness,
    }
    if args.compare_to:
        if not mix_wav or not mix_wav.exists():
            raise SystemExit("Cannot run parity comparison because the profiled mix WAV was not found.")
        profile["parity"] = compare_wavs(mix_wav, args.compare_to)

    profile_json = out_dir / f"{profile['label']}_profile.json"
    profile_md = out_dir / f"{profile['label']}_profile.md"
    write_json(profile_json, profile)
    profile_md.write_text(render_markdown(profile), encoding="utf-8")
    print(json.dumps({"profile_json": str(profile_json), "profile_md": str(profile_md)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
