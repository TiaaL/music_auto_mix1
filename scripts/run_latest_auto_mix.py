#!/usr/bin/env python3
"""Run classifier -> template render into one fixed latest folder."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "calibration_outputs" / "latest"
BATCH_INDEX_NAME = "mix_batches.jsonl"


def run(cmd: list[str]) -> None:
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"Command failed with code {proc.returncode}: {' '.join(cmd)}")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def clear_directory_contents(path: Path) -> None:
    resolved = path.resolve(strict=False)
    allowed_root = (ROOT / "calibration_outputs").resolve(strict=False)
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise SystemExit(f"Refusing to clear output directory outside calibration_outputs: {resolved}")
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def sanitize_batch_label(label: str) -> str:
    clean = re.sub(r"[^0-9A-Za-z._-]+", "_", label.strip())
    clean = clean.strip("._-")
    return clean or datetime.now().strftime("%Y%m%d_%H%M%S")


def unique_batch_label(out_dir: Path, label: str) -> str:
    base = sanitize_batch_label(label)
    candidate = base
    index = 2
    while (
        (out_dir / f"mix_{candidate}.wav").exists()
        or (out_dir / f"{candidate}_summary.json").exists()
        or (out_dir / f"{candidate}_analysis.json").exists()
    ):
        candidate = f"{base}_{index:02d}"
        index += 1
    return candidate


def append_batch_index(out_dir: Path, latest: dict) -> None:
    index_path = out_dir / BATCH_INDEX_NAME
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(latest, ensure_ascii=False) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render one auto-selected mix into calibration_outputs/latest.")
    parser.add_argument("vocal_wav", type=Path)
    parser.add_argument("accomp_wav", type=Path)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--batch-label", help="Batch label used in output filenames.")
    parser.add_argument("--clear-output", action="store_true", help="Clear the fixed output directory before rendering.")
    parser.add_argument("--keep-existing", action="store_true", help="Deprecated; history is kept by default.")
    parser.add_argument(
        "--no-volume-automation",
        action="store_true",
        help="Skip vocal leveling and accompaniment ducking before template rendering.",
    )
    parser.add_argument(
        "--no-loudness-finalizer",
        action="store_true",
        help="Skip final LUFS/true-peak normalization after the template master bus.",
    )
    parser.add_argument(
        "--with-vocal-debug",
        action="store_true",
        help="Also export vocal stage WAVs and a feature audit for debugging.",
    )
    parser.add_argument(
        "--reference-audio",
        type=Path,
        default=None,
        help="Reference track; its integrated LUFS becomes the loudness target.",
    )
    parser.add_argument(
        "--stage-report",
        action="store_true",
        help="Measure elapsed time plus LUFS/true-peak at each large render stage.",
    )
    args = parser.parse_args()

    out_dir = args.out_dir
    if args.clear_output and out_dir.exists():
        clear_directory_contents(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    requested_label = args.batch_label or datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_label = unique_batch_label(out_dir, requested_label)
    mix_path = out_dir / f"mix_{batch_label}.wav"
    latest_mix_path = out_dir / "mix.wav"
    report_dir = out_dir
    report_prefix = f"{batch_label}_"

    auto_mix_cmd = [
        sys.executable,
        str(ROOT / "scripts" / "auto_template_mix.py"),
        str(args.vocal_wav),
        str(args.accomp_wav),
        str(mix_path),
        "--report-dir",
        str(report_dir),
        "--report-prefix",
        report_prefix,
    ]
    if not args.no_volume_automation:
        auto_mix_cmd.append("--with-volume-automation")
    if args.no_loudness_finalizer:
        auto_mix_cmd.append("--no-loudness-finalizer")
    if args.reference_audio:
        auto_mix_cmd += ["--reference-audio", str(args.reference_audio)]
    if args.stage_report:
        auto_mix_cmd.append("--stage-report")
    run(auto_mix_cmd)

    summary = load_json(report_dir / f"{batch_label}_summary.json")
    template_id = str(summary.get("selected_template") or "template_d")

    feature_audit = None
    if args.with_vocal_debug and template_id in {"template_a", "template_b", "template_c"}:
        vocal_stage_dir = out_dir / "vocal_stages"
        run(
            [
                str(ROOT / ".tools" / "msys64" / "usr" / "bin" / "bash.exe"),
                "-lc",
                'cd "$1" && source scripts/msys_template_env.sh >/dev/null && shift && bash "$@"',
                "latest-vocal-stages",
                str(ROOT).replace("\\", "/").replace("D:", "/d"),
                str(ROOT / "scripts" / "render_template_vocal_stages.sh").replace("\\", "/").replace("D:", "/d"),
                template_id,
                str(args.vocal_wav.resolve()).replace("\\", "/").replace("D:", "/d"),
                str(vocal_stage_dir.resolve()).replace("\\", "/").replace("D:", "/d"),
            ]
        )
        run(
            [
                sys.executable,
                str(ROOT / "scripts" / "audit_template_vocal_features.py"),
                "--analysis-json",
                str(report_dir / f"{batch_label}_analysis.json"),
                "--processed-vocal",
                str(vocal_stage_dir / "final_insert.wav"),
                "--out-dir",
                str(out_dir / "feature_audit"),
            ]
        )
        feature_audit = str(out_dir / "feature_audit" / "vocal_feature_audit.md")

    latest = {
        "batch_label": batch_label,
        "vocal_wav": str(args.vocal_wav),
        "accomp_wav": str(args.accomp_wav),
        "mix_wav": str(mix_path),
        "latest_mix_wav": str(latest_mix_path),
        "report_dir": str(report_dir),
        "analysis_json": str(report_dir / f"{batch_label}_analysis.json"),
        "resolved_mix_plan": str(report_dir / f"{batch_label}_resolved_mix_plan.json"),
        "summary_json": str(report_dir / f"{batch_label}_summary.json"),
        "selected_template": summary.get("selected_template"),
        "with_volume_automation": not args.no_volume_automation,
        "loudness_finalizer": not args.no_loudness_finalizer,
        "loudness": summary.get("loudness"),
        "balance": summary.get("balance"),
        "stage_report": summary.get("stage_report"),
        "feature_audit": feature_audit,
    }
    shutil.copy2(mix_path, latest_mix_path)
    (out_dir / "LATEST.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    append_batch_index(out_dir, latest)
    print(json.dumps(latest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
