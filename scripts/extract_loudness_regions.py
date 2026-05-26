#!/usr/bin/env python3
"""
Extract loudness-oriented region features from audio.

Segmentation logic:
- Use short-frame RMS to detect silence
- Consecutive non-silent frames are merged into one region
- Each region corresponds to "a part with content between silences"

This keeps the final segmentation phrase/region-based instead of fixed windows.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path


EPSILON = 1.0e-12


@dataclass
class AudioBuffer:
    samples: list[float]
    sample_rate: int

    @property
    def duration(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return len(self.samples) / self.sample_rate


@dataclass
class Region:
    index: int
    start_sample: int
    end_sample: int
    start_time: float
    end_time: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end_time - self.start_time)


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=True)


def ffprobe_stream_info(path: str) -> tuple[int, float]:
    cp = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    lines = [line.strip() for line in cp.stdout.splitlines() if line.strip()]
    if len(lines) < 2:
        raise RuntimeError(f"Could not read audio info from: {path}")
    return int(float(lines[0])), float(lines[1])


def decode_audio(path: str, sample_rate: int) -> AudioBuffer:
    cmd = [
        "ffmpeg",
        "-v",
        "error",
        "-i",
        path,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-",
    ]
    cp = subprocess.run(cmd, capture_output=True, check=True)
    raw = cp.stdout
    if len(raw) % 4 != 0:
        raise RuntimeError(f"Unexpected PCM byte length from ffmpeg for: {path}")
    sample_count = len(raw) // 4
    samples = list(struct.unpack(f"<{sample_count}f", raw))
    return AudioBuffer(samples=samples, sample_rate=sample_rate)


def db_from_linear(value: float) -> float:
    return 20.0 * math.log10(max(value, EPSILON))


def rms_db(samples: list[float]) -> float:
    if not samples:
        return -120.0
    power = 0.0
    for sample in samples:
        power += sample * sample
    return db_from_linear(math.sqrt(power / len(samples)))


def peak_db(samples: list[float]) -> float:
    if not samples:
        return -120.0
    peak = max(abs(sample) for sample in samples)
    return db_from_linear(peak)


def build_frame_rms_db(
    samples: list[float],
    sample_rate: int,
    frame_ms: float,
    hop_ms: float,
) -> tuple[list[float], int, int]:
    frame_size = max(1, int(round(sample_rate * frame_ms / 1000.0)))
    hop_size = max(1, int(round(sample_rate * hop_ms / 1000.0)))
    values: list[float] = []

    if not samples:
        return values, frame_size, hop_size

    for start in range(0, len(samples), hop_size):
        end = min(len(samples), start + frame_size)
        frame = samples[start:end]
        if not frame:
            continue
        values.append(rms_db(frame))
        if end >= len(samples):
            break

    return values, frame_size, hop_size


def detect_regions(
    audio: AudioBuffer,
    silence_threshold_db: float,
    frame_ms: float,
    hop_ms: float,
    min_region_ms: float,
    min_silence_ms: float,
) -> list[Region]:
    frame_rms, frame_size, hop_size = build_frame_rms_db(
        audio.samples, audio.sample_rate, frame_ms, hop_ms
    )
    if not frame_rms:
        return []

    silent = [value < silence_threshold_db for value in frame_rms]
    min_region_frames = max(1, int(math.ceil(min_region_ms / hop_ms)))
    min_silence_frames = max(1, int(math.ceil(min_silence_ms / hop_ms)))

    regions: list[Region] = []
    run_start: int | None = None
    silence_run = 0

    for idx, is_silent in enumerate(silent):
        if not is_silent:
            if run_start is None:
                run_start = idx
            silence_run = 0
            continue

        if run_start is None:
            continue

        silence_run += 1
        if silence_run < min_silence_frames:
            continue

        first_silent_frame = idx - silence_run + 1
        end_frame = first_silent_frame
        if end_frame - run_start >= min_region_frames:
            start_sample = run_start * hop_size
            end_sample = min(len(audio.samples), first_silent_frame * hop_size)
            start_sample, end_sample = refine_region_bounds(
                audio, start_sample, end_sample, silence_threshold_db
            )
            regions.append(
                Region(
                    index=len(regions),
                    start_sample=start_sample,
                    end_sample=end_sample,
                    start_time=start_sample / audio.sample_rate,
                    end_time=end_sample / audio.sample_rate,
                )
            )
        run_start = None
        silence_run = 0

    if run_start is not None:
        end_frame = len(silent)
        if end_frame - run_start >= min_region_frames:
            start_sample = run_start * hop_size
            end_sample = len(audio.samples)
            start_sample, end_sample = refine_region_bounds(
                audio, start_sample, end_sample, silence_threshold_db
            )
            regions.append(
                Region(
                    index=len(regions),
                    start_sample=start_sample,
                    end_sample=end_sample,
                    start_time=start_sample / audio.sample_rate,
                    end_time=end_sample / audio.sample_rate,
                )
            )

    return regions


def refine_region_bounds(
    audio: AudioBuffer,
    start_sample: int,
    end_sample: int,
    silence_threshold_db: float,
    frame_ms: float = 12.0,
    hop_ms: float = 4.0,
) -> tuple[int, int]:
    region_samples = slice_samples(audio.samples, start_sample, end_sample)
    if not region_samples:
        return start_sample, end_sample

    frame_rms, frame_size, hop_size = build_frame_rms_db(
        region_samples, audio.sample_rate, frame_ms, hop_ms
    )
    active_frames = [idx for idx, value in enumerate(frame_rms) if value >= silence_threshold_db]
    if not active_frames:
        return start_sample, end_sample

    first_active = active_frames[0]
    last_active = active_frames[-1]
    refined_start = start_sample + first_active * hop_size
    refined_end = min(end_sample, start_sample + last_active * hop_size + frame_size)
    return refined_start, max(refined_start, refined_end)


def slice_samples(samples: list[float], start_sample: int, end_sample: int) -> list[float]:
    start = max(0, start_sample)
    end = max(start, min(len(samples), end_sample))
    return samples[start:end]


def subwindow_rms_values(
    region_samples: list[float],
    sample_rate: int,
    subwindow_ms: float,
) -> list[float]:
    window = max(1, int(round(sample_rate * subwindow_ms / 1000.0)))
    values: list[float] = []
    for start in range(0, len(region_samples), window):
        part = region_samples[start : start + window]
        if part:
            values.append(rms_db(part))
    return values


def stddev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return math.sqrt(max(0.0, variance))


def extract_region_stats(
    audio: AudioBuffer,
    region: Region,
    subwindow_ms: float,
) -> dict[str, float | bool]:
    samples = slice_samples(audio.samples, region.start_sample, region.end_sample)
    raw_rms = rms_db(samples)
    raw_peak = peak_db(samples)
    crest = raw_peak - raw_rms
    sub_rms = subwindow_rms_values(samples, audio.sample_rate, subwindow_ms)
    min_rms = min(sub_rms) if sub_rms else raw_rms
    local_var = stddev(sub_rms)
    position = ((region.start_time + region.end_time) * 0.5) / max(audio.duration, EPSILON)

    return {
        "raw_rms_db": raw_rms,
        "raw_peak_db": raw_peak,
        "raw_min_rms_db": min_rms,
        "raw_crest_db": crest,
        "local_variation": local_var,
        "position_ratio": max(0.0, min(1.0, position)),
        "is_silent": raw_rms < -60.0,
    }


def attach_neighbor_features(rows: list[dict[str, float | bool | int | str]]) -> None:
    for idx, row in enumerate(rows):
        prev_rms = rows[idx - 1]["yuan_rms_db"] if idx > 0 else row["yuan_rms_db"]
        next_rms = rows[idx + 1]["yuan_rms_db"] if idx + 1 < len(rows) else row["yuan_rms_db"]
        row["prev_yuan_rms_db"] = prev_rms
        row["next_yuan_rms_db"] = next_rms
        row["level_slope"] = float(row["yuan_rms_db"]) - float(prev_rms)


def attach_processed_features(
    rows: list[dict[str, float | bool | int | str]],
    regions: list[Region],
    processed_audio: AudioBuffer,
    subwindow_ms: float,
) -> None:
    for row, region in zip(rows, regions):
        stats = extract_region_stats(processed_audio, region, subwindow_ms)
        row["down_rms_db"] = stats["raw_rms_db"]
        row["down_peak_db"] = stats["raw_peak_db"]
        row["down_min_rms_db"] = stats["raw_min_rms_db"]
        row["down_crest_db"] = stats["raw_crest_db"]
        row["down_local_variation"] = stats["local_variation"]
        row["gain_delta_db"] = float(row["yuan_rms_db"]) - float(row["down_rms_db"])
        row["rms_delta_db"] = row["gain_delta_db"]
        row["peak_delta_db"] = float(row["yuan_peak_db"]) - float(row["down_peak_db"])
        row["min_rms_delta_db"] = float(row["yuan_min_rms_db"]) - float(row["down_min_rms_db"])
        row["crest_delta_db"] = float(row["yuan_crest_db"]) - float(row["down_crest_db"])
        row["local_variation_delta"] = (
            float(row["yuan_local_variation"]) - float(row["down_local_variation"])
        )


def write_csv(path: str, rows: list[dict[str, float | bool | int | str]]) -> None:
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_json(path: str, rows: list[dict[str, float | bool | int | str]]) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, ensure_ascii=False, indent=2)


def build_rows(
    raw_audio: AudioBuffer,
    regions: list[Region],
    subwindow_ms: float,
    group_id: str = "",
    role: str = "",
    track_id: str = "",
    pair_id: str = "",
    raw_file: str = "",
    processed_file: str = "",
) -> list[dict[str, float | bool | int | str]]:
    rows: list[dict[str, float | bool | int | str]] = []
    for region in regions:
        stats = extract_region_stats(raw_audio, region, subwindow_ms)
        rows.append(
            {
                "group_id": group_id,
                "role": role,
                "track_id": track_id,
                "pair_id": pair_id,
                "yuan_file": raw_file,
                "down_file": processed_file,
                "region_index": region.index,
                "start_sec": round(region.start_time, 6),
                "end_sec": round(region.end_time, 6),
                "duration_sec": round(region.duration, 6),
                "yuan_rms_db": stats["raw_rms_db"],
                "yuan_peak_db": stats["raw_peak_db"],
                "yuan_min_rms_db": stats["raw_min_rms_db"],
                "yuan_crest_db": stats["raw_crest_db"],
                "yuan_local_variation": stats["local_variation"],
                "position_ratio": stats["position_ratio"],
                "is_silent": stats["is_silent"],
            }
        )
    attach_neighbor_features(rows)
    return rows


def collect_pairs(
    directory: str,
    raw_suffix: str,
    processed_suffix: str,
) -> list[tuple[str, Path, Path]]:
    root = Path(directory)
    raw_map: dict[str, Path] = {}
    proc_map: dict[str, Path] = {}

    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        stem = path.stem
        if stem.endswith(raw_suffix):
            pair_id = stem[: -len(raw_suffix)]
            raw_map[pair_id] = path
        elif stem.endswith(processed_suffix):
            pair_id = stem[: -len(processed_suffix)]
            proc_map[pair_id] = path

    pair_ids = sorted(set(raw_map) & set(proc_map))
    return [(pair_id, raw_map[pair_id], proc_map[pair_id]) for pair_id in pair_ids]


def infer_role(track_id: str, vocal_prefix: str, accomp_prefix: str) -> str:
    lowered = track_id.lower()
    if lowered.startswith(vocal_prefix.lower()):
        return "vocal"
    if lowered.startswith(accomp_prefix.lower()):
        return "accomp"
    return "unknown"


def process_single(
    raw_audio_path: str,
    processed_audio_path: str | None,
    silence_threshold_db: float,
    frame_ms: float,
    hop_ms: float,
    min_region_ms: float,
    min_silence_ms: float,
    subwindow_ms: float,
    group_id: str = "",
    role: str = "",
    track_id: str = "",
    pair_id: str = "",
) -> list[dict[str, float | bool | int | str]]:
    sample_rate, _ = ffprobe_stream_info(raw_audio_path)
    raw_audio = decode_audio(raw_audio_path, sample_rate)
    regions = detect_regions(
        raw_audio,
        silence_threshold_db=silence_threshold_db,
        frame_ms=frame_ms,
        hop_ms=hop_ms,
        min_region_ms=min_region_ms,
        min_silence_ms=min_silence_ms,
    )

    rows = build_rows(
        raw_audio,
        regions,
        subwindow_ms,
        group_id=group_id,
        role=role,
        track_id=track_id,
        pair_id=pair_id,
        raw_file=os.path.basename(raw_audio_path),
        processed_file=os.path.basename(processed_audio_path) if processed_audio_path else "",
    )

    if processed_audio_path:
        processed_audio = decode_audio(processed_audio_path, sample_rate)
        attach_processed_features(rows, regions, processed_audio, subwindow_ms)

    return rows


def process_group_directory(
    directory: str,
    raw_suffix: str,
    processed_suffix: str,
    vocal_prefix: str,
    accomp_prefix: str,
    silence_threshold_db: float,
    frame_ms: float,
    hop_ms: float,
    min_region_ms: float,
    min_silence_ms: float,
    subwindow_ms: float,
    group_id: str | None = None,
) -> list[dict[str, float | bool | int | str]]:
    pairs = collect_pairs(directory, raw_suffix, processed_suffix)
    rows: list[dict[str, float | bool | int | str]] = []
    resolved_group_id = group_id or Path(directory).name

    for pair_id, raw_path, processed_path in pairs:
        role = infer_role(pair_id, vocal_prefix, accomp_prefix)
        rows.extend(
            process_single(
                str(raw_path),
                str(processed_path),
                silence_threshold_db=silence_threshold_db,
                frame_ms=frame_ms,
                hop_ms=hop_ms,
                min_region_ms=min_region_ms,
                min_silence_ms=min_silence_ms,
                subwindow_ms=subwindow_ms,
                group_id=resolved_group_id,
                role=role,
                track_id=pair_id,
                pair_id=pair_id,
            )
        )

    return rows


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract loudness-oriented features from silence-delimited audio regions"
    )
    parser.add_argument("raw_audio", nargs="?", help="Input audio used for silence/region detection")
    parser.add_argument(
        "--processed-audio",
        help="Optional paired processed audio; extracts proc_* features on the same regions",
    )
    parser.add_argument(
        "--pair-dir",
        help="Treat this directory as one group and scan it for *_yuan / *_DOWN pairs",
    )
    parser.add_argument(
        "--group-root",
        help="Batch mode: each immediate subdirectory is treated as one 4-file group",
    )
    parser.add_argument(
        "--raw-suffix",
        default="_yuan",
        help="Raw filename suffix used in --pair-dir mode. Default: _yuan",
    )
    parser.add_argument(
        "--processed-suffix",
        default="_DOWN",
        help="Processed filename suffix used in --pair-dir mode. Default: _DOWN",
    )
    parser.add_argument(
        "--vocal-prefix",
        default="vo",
        help="Track prefix that identifies vocal files. Default: vo",
    )
    parser.add_argument(
        "--accomp-prefix",
        default="bc",
        help="Track prefix that identifies accompaniment files. Default: bc",
    )
    parser.add_argument(
        "--output",
        default="region_features.csv",
        help="Output path (.csv or .json). Default: region_features.csv",
    )
    parser.add_argument(
        "--silence-threshold-db",
        type=float,
        default=-45.0,
        help="Frames below this RMS are considered silent. Default: -45 dB",
    )
    parser.add_argument(
        "--frame-ms",
        type=float,
        default=50.0,
        help="Analysis frame size for silence detection. Default: 50 ms",
    )
    parser.add_argument(
        "--hop-ms",
        type=float,
        default=20.0,
        help="Analysis hop size for silence detection. Default: 20 ms",
    )
    parser.add_argument(
        "--min-region-ms",
        type=float,
        default=120.0,
        help="Discard very short non-silent regions shorter than this. Default: 120 ms",
    )
    parser.add_argument(
        "--min-silence-ms",
        type=float,
        default=120.0,
        help="Require at least this much consecutive silence to split regions. Default: 120 ms",
    )
    parser.add_argument(
        "--subwindow-ms",
        type=float,
        default=80.0,
        help="Subwindow size used for raw_min_rms_db and local_variation. Default: 80 ms",
    )
    args = parser.parse_args()

    if not args.group_root and not args.pair_dir and not args.raw_audio:
        parser.error("provide raw_audio, --pair-dir, or --group-root")

    rows: list[dict[str, float | bool | int | str]] = []
    processed_group_count = 0
    if args.group_root:
        group_dirs = [path for path in sorted(Path(args.group_root).iterdir()) if path.is_dir()]
        for group_dir in group_dirs:
            group_rows = process_group_directory(
                str(group_dir),
                raw_suffix=args.raw_suffix,
                processed_suffix=args.processed_suffix,
                vocal_prefix=args.vocal_prefix,
                accomp_prefix=args.accomp_prefix,
                silence_threshold_db=args.silence_threshold_db,
                frame_ms=args.frame_ms,
                hop_ms=args.hop_ms,
                min_region_ms=args.min_region_ms,
                min_silence_ms=args.min_silence_ms,
                subwindow_ms=args.subwindow_ms,
                group_id=group_dir.name,
            )
            if group_rows:
                rows.extend(group_rows)
                processed_group_count += 1
    elif args.pair_dir:
        rows = process_group_directory(
            args.pair_dir,
            raw_suffix=args.raw_suffix,
            processed_suffix=args.processed_suffix,
            vocal_prefix=args.vocal_prefix,
            accomp_prefix=args.accomp_prefix,
            silence_threshold_db=args.silence_threshold_db,
            frame_ms=args.frame_ms,
            hop_ms=args.hop_ms,
            min_region_ms=args.min_region_ms,
            min_silence_ms=args.min_silence_ms,
            subwindow_ms=args.subwindow_ms,
            group_id=Path(args.pair_dir).name,
        )
        processed_group_count = 1 if rows else 0
    else:
        rows = process_single(
            args.raw_audio,
            args.processed_audio,
            silence_threshold_db=args.silence_threshold_db,
            frame_ms=args.frame_ms,
            hop_ms=args.hop_ms,
            min_region_ms=args.min_region_ms,
            min_silence_ms=args.min_silence_ms,
            subwindow_ms=args.subwindow_ms,
            role=infer_role(Path(args.raw_audio).stem, args.vocal_prefix, args.accomp_prefix),
            track_id=Path(args.raw_audio).stem,
            pair_id=Path(args.raw_audio).stem,
        )
        processed_group_count = 1 if rows else 0

    output_path = Path(args.output)
    if output_path.suffix.lower() == ".json":
        write_json(str(output_path), rows)
    else:
        write_csv(str(output_path), rows)

    if args.group_root or args.pair_dir:
        print(f"Processed {processed_group_count} group(s)")
    print(f"Detected {len(rows)} row(s)")
    print(f"Output: {output_path}")
    print("Segmentation: silence-delimited regions based on short-frame RMS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
