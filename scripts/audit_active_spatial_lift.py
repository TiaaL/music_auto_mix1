#!/usr/bin/env python3
"""Audit active-region Mid/Side lift against a reference vocal target.

This is intentionally offline: it reads existing renders and reference files, and
does not participate in the normal render path.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np

from analyze_reference import active_intervals_from_vocal, db, interval_mask, load_audio_as_float


NEAR_MONO_SIDE_MINUS_MID_DB = -32.0
SIDE_DEFICIT_DB = 3.0
MID_FORWARD_DB = 2.5
MIN_SAFE_LR_CORR = 0.15
MIN_SAFE_MONO_LOSS_DB = -6.0


def parse_candidate(value: str) -> tuple[str, Path]:
    if "=" in value:
        label, path = value.split("=", 1)
        return label.strip() or Path(path).stem, Path(path).expanduser()
    path = Path(value).expanduser()
    return path.stem, path


def load_summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def stereo_audio(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = load_audio_as_float(path)
    if audio.ndim == 1:
        audio = np.column_stack([audio, audio])
    if audio.shape[1] == 1:
        audio = np.repeat(audio, 2, axis=1)
    return audio[:, :2], sr


def rms_db(values: np.ndarray) -> float:
    if values.size == 0:
        return -120.0
    return db(float(np.sqrt(np.mean(np.square(values)))))


def lr_corr(left: np.ndarray, right: np.ndarray) -> float:
    if left.size < 2 or right.size < 2:
        return 1.0
    left_centered = left - float(np.mean(left))
    right_centered = right - float(np.mean(right))
    denom = float(np.sqrt(np.dot(left_centered, left_centered) * np.dot(right_centered, right_centered)))
    if denom <= 1e-12:
        return 1.0
    return float(np.dot(left_centered, right_centered) / denom)


def mono_fold_down_loss_db(left: np.ndarray, right: np.ndarray) -> float:
    if left.size == 0 or right.size == 0:
        return 0.0
    stereo_rms = float(np.sqrt(np.mean((np.square(left) + np.square(right)) * 0.5)))
    mono_rms = float(np.sqrt(np.mean(np.square((left + right) * 0.5))))
    return db(mono_rms) - db(stereo_rms)


def ms_metrics(path: Path, intervals: list[tuple[float, float]]) -> dict[str, Any]:
    audio, sr = stereo_audio(path)
    mask = interval_mask(audio.shape[0], sr, intervals)
    left = audio[:, 0]
    right = audio[:, 1]
    mid = (left + right) / math.sqrt(2.0)
    side = (left - right) / math.sqrt(2.0)

    mid_active = rms_db(mid[mask])
    mid_inactive = rms_db(mid[~mask])
    side_active = rms_db(side[mask])
    side_inactive = rms_db(side[~mask])

    return {
        "path": str(path),
        "mid_active_db": round(mid_active, 3),
        "mid_inactive_db": round(mid_inactive, 3),
        "mid_lift_db": round(mid_active - mid_inactive, 3),
        "side_active_db": round(side_active, 3),
        "side_inactive_db": round(side_inactive, 3),
        "side_lift_db": round(side_active - side_inactive, 3),
        "active_side_minus_mid_db": round(side_active - mid_active, 3),
        "inactive_side_minus_mid_db": round(side_inactive - mid_inactive, 3),
        "lr_correlation_active": round(lr_corr(left[mask], right[mask]), 5),
        "lr_correlation_inactive": round(lr_corr(left[~mask], right[~mask]), 5),
        "mono_fold_down_loss_active_db": round(mono_fold_down_loss_db(left[mask], right[mask]), 3),
        "mono_fold_down_loss_inactive_db": round(mono_fold_down_loss_db(left[~mask], right[~mask]), 3),
    }


def compare_to_reference(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mid_lift_db",
        "side_lift_db",
        "active_side_minus_mid_db",
        "inactive_side_minus_mid_db",
    )
    return {
        f"{key}_error_db": round(float(candidate[key]) - float(reference[key]), 3)
        for key in keys
    }


def recommendation(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    ref_side_mid = float(reference["active_side_minus_mid_db"])
    cand_side_mid = float(candidate["active_side_minus_mid_db"])
    side_deficit = ref_side_mid - cand_side_mid
    mid_error = float(candidate["mid_lift_db"]) - float(reference["mid_lift_db"])
    lr_active = float(candidate["lr_correlation_active"])
    mono_loss = float(candidate["mono_fold_down_loss_active_db"])

    if ref_side_mid <= NEAR_MONO_SIDE_MINUS_MID_DB:
        return {
            "action": "do_not_add_direct_side",
            "reason": "reference_vocal_stem_is_near_mono",
            "side_deficit_db": round(side_deficit, 3),
        }
    if lr_active < MIN_SAFE_LR_CORR or mono_loss < MIN_SAFE_MONO_LOSS_DB:
        return {
            "action": "do_not_add_direct_side",
            "reason": "candidate_phase_or_mono_fold_guard",
            "lr_correlation_active": round(lr_active, 5),
            "mono_fold_down_loss_active_db": round(mono_loss, 3),
        }
    if side_deficit < SIDE_DEFICIT_DB:
        return {
            "action": "keep_current_vocal_group",
            "reason": "current_active_side_is_close_to_reference",
            "side_deficit_db": round(side_deficit, 3),
        }

    action = "consider_light_voice_correlated_side_layer"
    notes = ["add_side_only_no_mid_gain"]
    if mid_error > MID_FORWARD_DB:
        notes.append("current_mid_forward_consider_lighter_presence_duck")
    return {
        "action": action,
        "reason": "reference_has_active_vocal_side_but_current_vocal_group_is_narrower",
        "side_deficit_db": round(side_deficit, 3),
        "mid_lift_error_db": round(mid_error, 3),
        "notes": notes,
    }


def build_report(
    reference_target: Path,
    reference_vocal: Path,
    candidates: list[tuple[str, Path]],
    reference_audio: Path | None = None,
    reference_target_kind: str = "vocal_stem",
) -> dict[str, Any]:
    ref_vocal_audio, ref_vocal_sr = load_audio_as_float(reference_vocal)
    intervals = active_intervals_from_vocal(ref_vocal_audio, ref_vocal_sr)
    coverage_sec = sum(end - start for start, end in intervals)

    reference_metrics = ms_metrics(reference_target, intervals)
    candidate_reports: list[dict[str, Any]] = []
    for label, path in candidates:
        metrics = ms_metrics(path, intervals)
        candidate_reports.append({
            "label": label,
            **metrics,
            "reference_error": compare_to_reference(metrics, reference_metrics),
            "recommendation": recommendation(metrics, reference_metrics),
        })

    return {
        "reference_target": str(reference_target),
        "reference_target_kind": reference_target_kind,
        "reference_audio": str(reference_audio) if reference_audio else None,
        "reference_vocal": str(reference_vocal),
        "active_regions": {
            "count": len(intervals),
            "coverage_sec": round(coverage_sec, 3),
            "basis": "reference_vocal_active_regions",
        },
        "reference": reference_metrics,
        "candidates": candidate_reports,
        "policy_note": (
            "Use reference-relative Mid/Side active lift; do not treat side_lift as "
            "a fixed global target across songs."
        ),
    }


def candidate_rows(report: dict[str, Any]) -> str:
    lines = [
        "| track | mid lift | side lift | mid error | side error | active side-mid | L/R corr | mono loss | action |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    ref = report["reference"]
    lines.append(
        "| reference | "
        f"{ref['mid_lift_db']:+.2f} | {ref['side_lift_db']:+.2f} | "
        "+0.00 | +0.00 | "
        f"{ref['active_side_minus_mid_db']:+.2f} | "
        f"{ref['lr_correlation_active']:+.2f} | "
        f"{ref['mono_fold_down_loss_active_db']:+.2f} | target |"
    )
    for item in report["candidates"]:
        err = item["reference_error"]
        rec = item.get("recommendation") or {}
        lines.append(
            f"| {item['label']} | "
            f"{item['mid_lift_db']:+.2f} | {item['side_lift_db']:+.2f} | "
            f"{err['mid_lift_db_error_db']:+.2f} | {err['side_lift_db_error_db']:+.2f} | "
            f"{item['active_side_minus_mid_db']:+.2f} | "
            f"{item['lr_correlation_active']:+.2f} | "
            f"{item['mono_fold_down_loss_active_db']:+.2f} | "
            f"{rec.get('action', 'n/a')} |"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit active Mid/Side lift against a reference song.")
    parser.add_argument("--summary-json", type=Path, help="auto_template_mix summary JSON to infer reference/candidate")
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--reference-vocal", type=Path)
    parser.add_argument(
        "--reference-target",
        choices=("vocal_stem", "full_mix"),
        default="vocal_stem",
        help="Use reference vocal stem by default; full_mix is legacy diagnostic mode.",
    )
    parser.add_argument("--candidate", action="append", default=[], help="Path or label=path. Can be repeated.")
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--markdown", action="store_true", help="Print a compact markdown table instead of JSON.")
    args = parser.parse_args()

    candidates = [parse_candidate(value) for value in args.candidate]
    reference_audio = args.reference_audio
    reference_vocal = args.reference_vocal

    if args.summary_json:
        summary = load_summary(args.summary_json)
        reference_used = summary.get("reference_used") or {}
        if reference_audio is None and reference_used.get("full_mix"):
            reference_audio = Path(reference_used["full_mix"])
        if reference_vocal is None and reference_used.get("vocal"):
            reference_vocal = Path(reference_used["vocal"])
        if not candidates:
            if summary.get("vocal_group_output"):
                candidates.append(("vocal_group", Path(summary["vocal_group_output"])))
            elif summary.get("output_wav"):
                candidates.append((Path(summary["output_wav"]).stem, Path(summary["output_wav"])))

    if reference_vocal is None:
        raise SystemExit("Provide --reference-vocal, or --summary-json with reference_used.vocal.")
    if args.reference_target == "full_mix" and reference_audio is None:
        raise SystemExit("Provide --reference-audio for --reference-target full_mix.")
    if not candidates:
        raise SystemExit("Provide at least one --candidate, or a summary JSON with output_wav.")

    reference_target = reference_vocal if args.reference_target == "vocal_stem" else reference_audio
    if reference_target is None:
        raise SystemExit("Reference target could not be resolved.")
    report = build_report(
        reference_target,
        reference_vocal,
        candidates,
        reference_audio=reference_audio,
        reference_target_kind=args.reference_target,
    )
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.markdown:
        print(candidate_rows(report))
    else:
        json.dump(report, sys.stdout, ensure_ascii=False, indent=2)
        print()


if __name__ == "__main__":
    main()
