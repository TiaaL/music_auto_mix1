#!/usr/bin/env python3
"""Audit vocal/accompaniment level balance on voiced regions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from auto_volume_mix import (
    DetectionConfig,
    Segment,
    balance_stats,
    detect_paragraphs,
    detect_silences,
    ffprobe_duration,
    invert_silences,
    measure_max_volume,
    merge_short_gaps,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REFERENCE_ROOT = Path(r"D:\cubase\project\ai_cover\Mixdown\volume")


def voiced_segments(vocal_path: str, detection: DetectionConfig) -> list[Segment]:
    duration = ffprobe_duration(vocal_path)
    silences = detect_silences(vocal_path, detection.silence_noise_db, detection.min_silence_sec)
    voice = invert_silences(duration, silences)
    voice = merge_short_gaps(voice, detection.merge_gap_sec)
    paragraphs = detect_paragraphs(duration, voice, detection)
    return [Segment(start, end, True, 0.0) for start, end in paragraphs if end - start >= detection.min_voiced_sec]


def audit_pair(label: str, vocal: Path, accomp: Path, detection: DetectionConfig) -> dict[str, object]:
    segments = voiced_segments(str(vocal), detection)
    stats = balance_stats(str(vocal), str(accomp), segments)
    return {
        "label": label,
        "vocal": str(vocal),
        "accomp": str(accomp),
        "peaks": {
            "vocal_peak_db": round(measure_max_volume(str(vocal)), 3),
            "accomp_peak_db": round(measure_max_volume(str(accomp)), 3),
        },
        "stats": stats,
    }


def find_reference_pairs(root: Path) -> list[tuple[str, Path, Path]]:
    pairs: list[tuple[str, Path, Path]] = []
    if not root.exists():
        return pairs
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        vocals = sorted(folder.glob("vo*_*.wav"))
        accomp_files = sorted(folder.glob("bc*_*.wav"))
        for suffix in ("yuan", "DOWN"):
            vocal = next((path for path in vocals if suffix.lower() in path.stem.lower()), None)
            accomp = next((path for path in accomp_files if suffix.lower() in path.stem.lower()), None)
            if vocal and accomp:
                pairs.append((f"{folder.name}_{suffix}", vocal, accomp))
    return pairs


def find_reference_transitions(root: Path) -> list[tuple[str, Path, Path, Path, Path]]:
    transitions: list[tuple[str, Path, Path, Path, Path]] = []
    if not root.exists():
        return transitions
    for folder in sorted(path for path in root.iterdir() if path.is_dir()):
        vocal_yuan = next(iter(sorted(folder.glob("vo*_yuan.wav"))), None)
        vocal_down = next(iter(sorted(folder.glob("vo*_DOWN.wav"))), None)
        accomp_yuan = next(iter(sorted(folder.glob("bc*_yuan.wav"))), None)
        accomp_down = next(iter(sorted(folder.glob("bc*_DOWN.wav"))), None)
        if vocal_yuan and vocal_down and accomp_yuan and accomp_down:
            transitions.append((folder.name, vocal_yuan, vocal_down, accomp_yuan, accomp_down))
    return transitions


def audit_transition(
    label: str,
    vocal_yuan: Path,
    vocal_down: Path,
    accomp_yuan: Path,
    accomp_down: Path,
    detection: DetectionConfig,
) -> dict[str, object]:
    yuan = audit_pair(f"{label}_yuan", vocal_yuan, accomp_yuan, detection)
    down = audit_pair(f"{label}_DOWN", vocal_down, accomp_down, detection)
    yuan_stats = yuan["stats"]
    down_stats = down["stats"]
    assert isinstance(yuan_stats, dict)
    assert isinstance(down_stats, dict)
    return {
        "label": label,
        "yuan": yuan,
        "down": down,
        "gap_delta": {
            "median_db": round(float(down_stats["median_gap_db"]) - float(yuan_stats["median_gap_db"]), 3),
            "p90_db": round(float(down_stats["p90_gap_db"]) - float(yuan_stats["p90_gap_db"]), 3),
            "max_db": round(float(down_stats["max_gap_db"]) - float(yuan_stats["max_gap_db"]), 3),
        },
        "peak_delta": {
            "vocal_db": round(float(down["peaks"]["vocal_peak_db"]) - float(yuan["peaks"]["vocal_peak_db"]), 3),
            "accomp_db": round(float(down["peaks"]["accomp_peak_db"]) - float(yuan["peaks"]["accomp_peak_db"]), 3),
        },
    }


def write_markdown(path: Path, audits: list[dict[str, object]]) -> None:
    lines = [
        "# Vocal Balance Audit",
        "",
        "| label | segments | median gap | p90 gap | max gap | vocal peak | accomp peak | too-forward segments |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in audits:
        stats = item["stats"]
        peaks = item["peaks"]
        assert isinstance(stats, dict)
        assert isinstance(peaks, dict)
        lines.append(
            "| {label} | {count} | {median:.2f} dB | {p90:.2f} dB | {max_gap:.2f} dB | {vpeak:.2f} dBFS | {apeak:.2f} dBFS | {too} |".format(
                label=item["label"],
                count=stats["segment_count"],
                median=stats["median_gap_db"],
                p90=stats["p90_gap_db"],
                max_gap=stats["max_gap_db"],
                vpeak=peaks["vocal_peak_db"],
                apeak=peaks["accomp_peak_db"],
                too=stats["too_forward_segments"],
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_transition_markdown(path: Path, transitions: list[dict[str, object]]) -> None:
    lines = [
        "# Cubase Volume Reference Transitions",
        "",
        "| label | median gap delta | p90 gap delta | max gap delta | vocal peak delta | accomp peak delta |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for item in transitions:
        gap_delta = item["gap_delta"]
        peak_delta = item["peak_delta"]
        assert isinstance(gap_delta, dict)
        assert isinstance(peak_delta, dict)
        lines.append(
            "| {label} | {median:.2f} dB | {p90:.2f} dB | {max_gap:.2f} dB | {vpeak:.2f} dB | {apeak:.2f} dB |".format(
                label=item["label"],
                median=gap_delta["median_db"],
                p90=gap_delta["p90_db"],
                max_gap=gap_delta["max_db"],
                vpeak=peak_delta["vocal_db"],
                apeak=peak_delta["accomp_db"],
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit vocal/accompaniment balance on voiced regions.")
    parser.add_argument("--vocal", type=Path, help="Vocal wav to audit.")
    parser.add_argument("--accomp", type=Path, help="Accompaniment wav to audit.")
    parser.add_argument("--label", default="input_pair")
    parser.add_argument("--reference-root", type=Path, default=DEFAULT_REFERENCE_ROOT)
    parser.add_argument("--out-json", type=Path, required=True)
    parser.add_argument("--out-md", type=Path)
    args = parser.parse_args()

    detection = DetectionConfig(
        silence_noise_db=-38.0,
        min_silence_sec=0.20,
        merge_gap_sec=0.14,
        min_voiced_sec=0.08,
        paragraph_gap_sec=1.20,
        paragraph_pad_sec=0.08,
    )

    audits: list[dict[str, object]] = []
    if args.vocal and args.accomp:
        audits.append(audit_pair(args.label, args.vocal, args.accomp, detection))
    for label, vocal, accomp in find_reference_pairs(args.reference_root):
        audits.append(audit_pair(f"cubase_volume_{label}", vocal, accomp, detection))
    transitions = [
        audit_transition(f"cubase_volume_{label}", vocal_yuan, vocal_down, accomp_yuan, accomp_down, detection)
        for label, vocal_yuan, vocal_down, accomp_yuan, accomp_down in find_reference_transitions(args.reference_root)
    ]

    payload = {
        "reference_root": str(args.reference_root),
        "audits": audits,
        "transitions": transitions,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(args.out_md, audits)
        write_transition_markdown(args.out_md.with_name(args.out_md.stem + "_transitions.md"), transitions)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
