#!/usr/bin/env python3
"""Finalize master loudness: gain on master bus BEFORE L2, never boost after limiter."""

from __future__ import annotations

import argparse
import array
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import numpy as np
except ImportError:  # pragma: no cover - production envs should have numpy, keep fallback.
    np = None

try:
    from probe_loudness_libebur128 import libebur128_measure
except Exception:  # pragma: no cover - optional experiment dependency.
    libebur128_measure = None


ROOT = Path(__file__).resolve().parent.parent

FINAL_LOUDNESS_MIN_LUFS = -13.5
FINAL_LOUDNESS_MAX_LUFS = -12.5
DEFAULT_FINAL_TARGET_LUFS = -13.0
PREGAIN_INPUT_TP_HEADROOM_DB = -0.3
DEFAULT_MAX_GAIN_DB = 18.0
CONTROLLED_LIMITER_MAKEUP_MAX_DB = 8.0
CONTROLLED_LIMITER_MAKEUP_STEP_DB = 4.0
CONTROLLED_LIMITER_MAKEUP_MAX_STEPS = 2
CONTROLLED_LIMITER_MAKEUP_TOLERANCE_DB = 0.35
LOUDNESS_MISS_TOLERANCE_DB = 0.5
DEFAULT_DECLICK_THRESHOLD = 0.60
DEFAULT_MAX_DECLICK_SAMPLES = 4
DEFAULT_MAX_DECLICK_EVENTS = 2000


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


def loudness_delta(
    fast: dict[str, float | str],
    ffmpeg: dict[str, float | str],
) -> dict[str, float]:
    return {
        key: round(float(fast[key]) - float(ffmpeg[key]), 3)
        for key in ("input_i", "input_tp", "input_lra", "input_thresh")
        if key in fast and key in ffmpeg
    }


def conservative_tenth_db(value: float) -> float:
    if not math.isfinite(value):
        return value
    return math.ceil(value * 10.0) / 10.0


def ffmpeg_ebur128_true_peak(path: Path) -> dict[str, float | str]:
    start = time.perf_counter()
    proc = subprocess.run(
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
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    matches = re.findall(r"Peak:\s+(-?inf|[-+0-9.]+)\s+dBFS", proc.stderr, flags=re.IGNORECASE)
    if not matches:
        raise RuntimeError(f"Could not find ebur128 true peak in ffmpeg output:\n{proc.stderr[-2000:]}")
    raw_peak = float("-inf") if matches[-1].lower() == "-inf" else float(matches[-1])
    return {
        "engine": "ffmpeg_ebur128_true_peak",
        "input_tp": round(conservative_tenth_db(raw_peak), 3),
        "raw_input_tp": raw_peak,
        "elapsed_sec": round(time.perf_counter() - start, 3),
    }


def shifted_loudness_measure(measure: dict[str, float | str], gain_db: float) -> dict[str, float | str]:
    shifted = dict(measure)
    for key in ("input_i", "input_tp", "input_thresh", "output_i", "output_tp", "output_thresh"):
        value = shifted.get(key)
        if isinstance(value, (int, float)):
            shifted[key] = round(float(value) + gain_db, 3)
    target_offset = shifted.get("target_offset")
    if isinstance(target_offset, (int, float)):
        shifted["target_offset"] = round(float(target_offset) - gain_db, 3)
    shifted["estimated_after_linear_gain_db"] = round(gain_db, 3)
    return shifted


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
    if abs(gain_db) < 0.01:
        shutil.copyfile(input_path, output_path)
        return
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


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def peak_safety_trim_db(
    measured_tp: float,
    target_tp: float,
    limiter_headroom_db: float = 2.0,
) -> float:
    """Static attenuation only (never boost). Leaves headroom for a gentle limiter."""
    overshoot = measured_tp - (target_tp + limiter_headroom_db)
    if overshoot <= 0.0:
        return 0.0
    return -overshoot


def apply_soft_peak_limit(
    input_path: Path,
    output_path: Path,
    ceiling_db: float,
    attack_ms: float = 50.0,
    release_ms: float = 300.0,
) -> None:
    """Gentle true-peak ceiling. Slow attack avoids the crackle of the old 5 ms limiter."""
    ceiling = 10.0 ** (ceiling_db / 20.0)
    filter_graph = (
        "aresample=176400,"
        f"alimiter=limit={ceiling:.8f}:attack={attack_ms:.1f}:"
        f"release={release_ms:.1f}:level=false,"
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


def audio_stream_info(path: Path) -> tuple[int, int]:
    proc = run(
        [
            command_path("ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr)
    streams = json.loads(proc.stdout).get("streams") or []
    if not streams:
        raise RuntimeError(f"No audio stream found: {path}")
    stream = streams[0]
    return int(stream["sample_rate"]), int(stream["channels"])


def decode_f32le_bytes(path: Path, sample_rate: int, channels: int) -> bytes:
    proc = subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-i",
            str(path),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))
    return proc.stdout


def decode_f32le(path: Path, sample_rate: int, channels: int) -> array.array:
    raw = decode_f32le_bytes(path, sample_rate, channels)
    samples = array.array("f")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def encode_f32le_wav(samples: array.array, sample_rate: int, channels: int, output_path: Path) -> None:
    payload = array.array("f", samples)
    if sys.byteorder != "little":
        payload.byteswap()
    encode_f32le_bytes_wav(payload.tobytes(), sample_rate, channels, output_path)


def encode_f32le_bytes_wav(payload: bytes, sample_rate: int, channels: int, output_path: Path) -> None:
    proc = subprocess.run(
        [
            FFMPEG,
            "-y",
            "-v",
            "error",
            "-f",
            "f32le",
            "-ar",
            str(sample_rate),
            "-ac",
            str(channels),
            "-i",
            "-",
            "-c:a",
            "pcm_s16le",
            str(output_path),
        ],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", errors="replace"))


def repair_isolated_clicks(
    samples: array.array,
    sample_rate: int,
    channels: int,
    threshold: float,
    max_click_samples: int,
    max_events: int,
) -> dict[str, object]:
    """Replace isolated full-band sample spikes with linear interpolation.

    This intentionally avoids any gain, compression, or loudness compensation. It only
    touches very short discontinuities whose neighboring samples already agree with
    each other, which is the waveform shape of digital clicks after limiting.
    """
    frames = len(samples) // channels
    if frames < 8:
        return {"enabled": True, "events": 0, "samples_repaired": 0, "threshold": threshold, "examples": []}

    events: list[dict[str, float | int]] = []
    samples_repaired = 0
    total_events = 0
    per_channel_counts = [0 for _ in range(channels)]

    for channel in range(channels):
        def record_event(
            start: int,
            width: int,
            max_residual: float,
            repair_type: str,
        ) -> None:
            nonlocal samples_repaired, total_events
            samples_repaired += width
            total_events += 1
            per_channel_counts[channel] += 1
            if len(events) < 40:
                events.append(
                    {
                        "time_sec": round(start / sample_rate, 6),
                        "channel": channel,
                        "samples": width,
                        "max_residual": round(max_residual, 6),
                        "type": repair_type,
                    }
                )

        candidates: list[int] = []
        for index in range(2, frames - 2):
            prev_value = samples[(index - 1) * channels + channel]
            value = samples[index * channels + channel]
            next_value = samples[(index + 1) * channels + channel]
            predicted = (prev_value + next_value) * 0.5
            residual = abs(value - predicted)
            neighbor_delta = abs(next_value - prev_value)
            if residual >= threshold and neighbor_delta <= max(0.15, residual):
                candidates.append(index)

        groups: list[tuple[int, int]] = []
        pos = 0
        while pos < len(candidates):
            start = candidates[pos]
            end = start
            pos += 1
            while pos < len(candidates) and candidates[pos] <= end + 1:
                end = candidates[pos]
                pos += 1
            if end - start + 1 <= max_click_samples:
                groups.append((start, end))

        for start, end in groups:
            if total_events >= max_events:
                break
            left_index = start - 1
            right_index = end + 1
            left = samples[left_index * channels + channel]
            right = samples[right_index * channels + channel]
            width = end - start + 1
            max_residual = 0.0
            for offset, index in enumerate(range(start, end + 1), start=1):
                sample_index = index * channels + channel
                replacement = left + (right - left) * (offset / (width + 1))
                max_residual = max(max_residual, abs(samples[sample_index] - replacement))
                samples[sample_index] = replacement
            record_event(start, width, max_residual, "isolated_sample")
        if total_events >= max_events:
            break

        jump_threshold = threshold * 1.55
        jump_candidates: list[int] = []
        for index in range(2, frames - 3):
            current = samples[index * channels + channel]
            next_value = samples[(index + 1) * channels + channel]
            if abs(next_value - current) >= jump_threshold:
                jump_candidates.append(index)

        jump_groups: list[tuple[int, int]] = []
        pos = 0
        while pos < len(jump_candidates):
            start = jump_candidates[pos]
            end = start
            pos += 1
            while pos < len(jump_candidates) and jump_candidates[pos] <= end + 2:
                end = jump_candidates[pos]
                pos += 1
            repair_start = start
            repair_end = end + 1
            if repair_end - repair_start + 1 <= max_click_samples * 2:
                jump_groups.append((repair_start, repair_end))

        for start, end in jump_groups:
            if total_events >= max_events:
                break
            left_index = start - 1
            right_index = end + 1
            left = samples[left_index * channels + channel]
            right = samples[right_index * channels + channel]
            width = end - start + 1
            max_residual = 0.0
            for offset, index in enumerate(range(start, end + 1), start=1):
                sample_index = index * channels + channel
                replacement = left + (right - left) * (offset / (width + 1))
                max_residual = max(max_residual, abs(samples[sample_index] - replacement))
                samples[sample_index] = replacement
            record_event(start, width, max_residual, "short_jump_burst")
        if total_events >= max_events:
            break

    return {
        "enabled": True,
        "threshold": threshold,
        "max_click_samples": max_click_samples,
        "max_events": max_events,
        "events": total_events,
        "samples_repaired": samples_repaired,
        "per_channel_events": per_channel_counts,
        "examples": events,
        "truncated": total_events >= max_events,
    }


def first_repairable_group(
    indices: object,
    max_width: int,
    merge_gap: int,
    repair_end_extra: int = 0,
) -> tuple[int, int] | None:
    if len(indices) == 0:
        return None
    start = int(indices[0])
    end = start
    for raw_index in indices[1:]:
        index = int(raw_index)
        if index <= end + merge_gap:
            end = index
            continue
        repair_end = end + repair_end_extra
        if repair_end - start + 1 <= max_width:
            return start, repair_end
        start = index
        end = index
    repair_end = end + repair_end_extra
    if repair_end - start + 1 <= max_width:
        return start, repair_end
    return None


def grouped_repair_ranges(
    indices: object,
    max_width: int,
    merge_gap: int,
    repair_end_extra: int = 0,
) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    if len(indices) == 0:
        return groups
    start = int(indices[0])
    end = start
    for raw_index in indices[1:]:
        index = int(raw_index)
        if index <= end + merge_gap:
            end = index
            continue
        repair_end = end + repair_end_extra
        if repair_end - start + 1 <= max_width:
            groups.append((start, repair_end))
        start = index
        end = index
    repair_end = end + repair_end_extra
    if repair_end - start + 1 <= max_width:
        groups.append((start, repair_end))
    return groups


def probe_global_declick(
    input_path: Path,
    threshold: float,
    max_click_samples: int,
    max_events: int,
) -> dict[str, object]:
    sample_rate, channels = audio_stream_info(input_path)
    if np is None:
        samples = decode_f32le(input_path, sample_rate, channels)
        report = repair_isolated_clicks(
            samples,
            sample_rate=sample_rate,
            channels=channels,
            threshold=threshold,
            max_click_samples=max_click_samples,
            max_events=max_events,
        )
        return {
            "engine": "python_fallback",
            "triggered": int(report["samples_repaired"]) > 0,
            "events": report["events"],
            "samples_repaired_if_always": report["samples_repaired"],
            "threshold": threshold,
            "max_click_samples": max_click_samples,
            "max_events": max_events,
            "examples": report.get("examples", []),
        }

    raw = decode_f32le_bytes(input_path, sample_rate, channels)
    data = np.frombuffer(raw, dtype="<f4")
    frames = int(data.size // channels)
    if frames < 8:
        return {
            "engine": "numpy",
            "triggered": False,
            "events": 0,
            "threshold": threshold,
            "max_click_samples": max_click_samples,
            "max_events": max_events,
            "examples": [],
        }
    data = data[: frames * channels].reshape(frames, channels)
    examples: list[dict[str, float | int | str]] = []
    candidate_counts = [0 for _ in range(channels)]

    for channel in range(channels):
        channel_samples = data[:, channel]
        residual = np.abs(
            channel_samples[2:-2]
            - ((channel_samples[1:-3] + channel_samples[3:-1]) * 0.5)
        )
        neighbor_delta = np.abs(channel_samples[3:-1] - channel_samples[1:-3])
        isolated_indices = np.flatnonzero(
            (residual >= threshold)
            & (neighbor_delta <= np.maximum(0.15, residual))
        ) + 2
        candidate_counts[channel] += int(isolated_indices.size)
        group = first_repairable_group(
            isolated_indices,
            max_width=max_click_samples,
            merge_gap=1,
        )
        if group is not None:
            start, end = group
            width = end - start + 1
            examples.append(
                {
                    "time_sec": round(start / sample_rate, 6),
                    "channel": channel,
                    "samples": width,
                    "type": "isolated_sample",
                }
            )
            return {
                "engine": "numpy",
                "triggered": True,
                "events": 1,
                "threshold": threshold,
                "max_click_samples": max_click_samples,
                "max_events": max_events,
                "candidate_counts": candidate_counts,
                "examples": examples,
            }

        jump_threshold = threshold * 1.55
        jump_indices = np.flatnonzero(
            np.abs(channel_samples[3:-2] - channel_samples[2:-3]) >= jump_threshold
        ) + 2
        candidate_counts[channel] += int(jump_indices.size)
        group = first_repairable_group(
            jump_indices,
            max_width=max_click_samples * 2,
            merge_gap=2,
            repair_end_extra=1,
        )
        if group is not None:
            start, end = group
            width = end - start + 1
            examples.append(
                {
                    "time_sec": round(start / sample_rate, 6),
                    "channel": channel,
                    "samples": width,
                    "type": "short_jump_burst",
                }
            )
            return {
                "engine": "numpy",
                "triggered": True,
                "events": 1,
                "threshold": threshold,
                "max_click_samples": max_click_samples,
                "max_events": max_events,
                "candidate_counts": candidate_counts,
                "examples": examples,
            }

    return {
        "engine": "numpy",
        "triggered": False,
        "events": 0,
        "threshold": threshold,
        "max_click_samples": max_click_samples,
        "max_events": max_events,
        "candidate_counts": candidate_counts,
        "examples": examples,
    }


def repair_isolated_clicks_numpy(
    samples: object,
    sample_rate: int,
    channels: int,
    threshold: float,
    max_click_samples: int,
    max_events: int,
) -> dict[str, object]:
    """Vectorized detector with the same sparse linear repair as repair_isolated_clicks."""
    frames = int(samples.shape[0])
    if frames < 8:
        return {
            "enabled": True,
            "engine": "numpy",
            "events": 0,
            "samples_repaired": 0,
            "threshold": threshold,
            "examples": [],
        }

    events: list[dict[str, float | int]] = []
    samples_repaired = 0
    total_events = 0
    per_channel_counts = [0 for _ in range(channels)]

    for channel in range(channels):
        channel_samples = samples[:, channel]

        def record_event(
            start: int,
            width: int,
            max_residual: float,
            repair_type: str,
        ) -> None:
            nonlocal samples_repaired, total_events
            samples_repaired += width
            total_events += 1
            per_channel_counts[channel] += 1
            if len(events) < 40:
                events.append(
                    {
                        "time_sec": round(start / sample_rate, 6),
                        "channel": channel,
                        "samples": width,
                        "max_residual": round(max_residual, 6),
                        "type": repair_type,
                    }
                )

        values = channel_samples.astype(np.float64)
        residual = np.abs(values[2:-2] - ((values[1:-3] + values[3:-1]) * 0.5))
        neighbor_delta = np.abs(values[3:-1] - values[1:-3])
        candidates = np.flatnonzero(
            (residual >= threshold)
            & (neighbor_delta <= np.maximum(0.15, residual))
        ) + 2
        groups = grouped_repair_ranges(
            candidates,
            max_width=max_click_samples,
            merge_gap=1,
        )

        for start, end in groups:
            if total_events >= max_events:
                break
            left = float(channel_samples[start - 1])
            right = float(channel_samples[end + 1])
            width = end - start + 1
            max_residual = 0.0
            for offset, index in enumerate(range(start, end + 1), start=1):
                replacement = left + (right - left) * (offset / (width + 1))
                max_residual = max(max_residual, abs(float(channel_samples[index]) - replacement))
                channel_samples[index] = replacement
            record_event(start, width, max_residual, "isolated_sample")
        if total_events >= max_events:
            break

        values = channel_samples.astype(np.float64)
        jump_threshold = threshold * 1.55
        jump_candidates = np.flatnonzero(
            np.abs(values[3:-2] - values[2:-3]) >= jump_threshold
        ) + 2
        jump_groups = grouped_repair_ranges(
            jump_candidates,
            max_width=max_click_samples * 2,
            merge_gap=2,
            repair_end_extra=1,
        )

        for start, end in jump_groups:
            if total_events >= max_events:
                break
            left = float(channel_samples[start - 1])
            right = float(channel_samples[end + 1])
            width = end - start + 1
            max_residual = 0.0
            for offset, index in enumerate(range(start, end + 1), start=1):
                replacement = left + (right - left) * (offset / (width + 1))
                max_residual = max(max_residual, abs(float(channel_samples[index]) - replacement))
                channel_samples[index] = replacement
            record_event(start, width, max_residual, "short_jump_burst")
        if total_events >= max_events:
            break

    return {
        "enabled": True,
        "engine": "numpy",
        "threshold": threshold,
        "max_click_samples": max_click_samples,
        "max_events": max_events,
        "events": total_events,
        "samples_repaired": samples_repaired,
        "per_channel_events": per_channel_counts,
        "examples": events,
        "truncated": total_events >= max_events,
    }


def apply_global_declick(
    input_path: Path,
    output_path: Path,
    threshold: float,
    max_click_samples: int,
    max_events: int,
) -> dict[str, object]:
    sample_rate, channels = audio_stream_info(input_path)
    if np is not None:
        raw = decode_f32le_bytes(input_path, sample_rate, channels)
        samples = np.frombuffer(raw, dtype="<f4").copy()
        frames = int(samples.size // channels)
        if frames < 8:
            shutil.copyfile(input_path, output_path)
            return {
                "enabled": True,
                "engine": "numpy",
                "threshold": threshold,
                "max_click_samples": max_click_samples,
                "max_events": max_events,
                "events": 0,
                "samples_repaired": 0,
                "per_channel_events": [0 for _ in range(channels)],
                "examples": [],
                "truncated": False,
            }
        samples = samples[: frames * channels].reshape(frames, channels)
        report = repair_isolated_clicks_numpy(
            samples,
            sample_rate=sample_rate,
            channels=channels,
            threshold=threshold,
            max_click_samples=max_click_samples,
            max_events=max_events,
        )
        if int(report["samples_repaired"]) > 0:
            payload = np.ascontiguousarray(samples, dtype="<f4").tobytes()
            encode_f32le_bytes_wav(payload, sample_rate, channels, output_path)
        else:
            shutil.copyfile(input_path, output_path)
        return report

    samples = decode_f32le(input_path, sample_rate, channels)
    report = repair_isolated_clicks(
        samples,
        sample_rate=sample_rate,
        channels=channels,
        threshold=threshold,
        max_click_samples=max_click_samples,
        max_events=max_events,
    )
    if int(report["samples_repaired"]) > 0:
        encode_f32le_wav(samples, sample_rate, channels, output_path)
    else:
        shutil.copyfile(input_path, output_path)
    return report


def measure_reference_lufs(ref_path: Path) -> tuple[float, float]:
    m = loudnorm_measure(ref_path, target_i=-23.0, target_tp=-2.0, target_lra=11.0)
    return float(m["input_i"]), float(m["input_lra"])


def load_mix_plan_loudness(path: Path | None) -> dict[str, float] | None:
    if path is None or not path.exists():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None
    loudness = (plan.get("reference") or {}).get("overrides", {}).get("loudness_target", {})
    target = loudness.get("lufs_i")
    if target is None:
        return None
    out = {"target_i": clamp(float(target), FINAL_LOUDNESS_MIN_LUFS, FINAL_LOUDNESS_MAX_LUFS)}
    if loudness.get("lra") is not None:
        out["target_lra"] = max(6.0, min(14.0, float(loudness["lra"])))
    if loudness.get("true_peak_db") is not None:
        out["reference_true_peak_db"] = float(loudness["true_peak_db"])
    return out


def master_pregain_db(
    measure: dict[str, float | str],
    target_i: float,
    max_gain_db: float,
    max_attenuation_db: float,
) -> float:
    """Integrated gain applied on the master bus before the limiter."""
    return clamp(
        target_i - float(measure["input_i"]),
        -max_attenuation_db,
        max_gain_db,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize master loudness on the master bus.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument(
        "--limiter",
        type=Path,
        default=None,
        help="Faust master_l2_stereo binary (sample-peak stage before soft true-peak ceiling).",
    )
    parser.add_argument("--reference-audio", type=Path, default=None,
                        help="Reference track; its integrated LUFS becomes the target.")
    parser.add_argument("--mix-plan", type=Path, default=None,
                        help="Resolved mix plan; may supply a clamped final loudness target.")
    parser.add_argument("--target-i", type=float, default=DEFAULT_FINAL_TARGET_LUFS)
    parser.add_argument("--target-tp", type=float, default=-0.8)
    parser.add_argument("--target-lra", type=float, default=11.0)
    parser.add_argument("--max-gain-db", type=float, default=DEFAULT_MAX_GAIN_DB,
                        help="Maximum master-bus gain before L2.")
    parser.add_argument("--max-attenuation-db", type=float, default=12.0)
    parser.add_argument("--controlled-limiter-makeup-max-db", type=float,
                        default=CONTROLLED_LIMITER_MAKEUP_MAX_DB,
                        help="Maximum post-L2 makeup sent through the soft true-peak limiter.")
    parser.add_argument("--global-declick", choices=("auto", "always", "off"), default="auto",
                        help="Final isolated-click handling: probe first, always repair-scan, or off.")
    parser.add_argument("--no-global-declick", action="store_true",
                        help="Disable final isolated-sample click scan/repair (legacy alias for --global-declick off).")
    parser.add_argument("--declick-threshold", type=float, default=DEFAULT_DECLICK_THRESHOLD,
                        help="Minimum isolated sample residual to repair; no gain is applied.")
    parser.add_argument("--max-declick-samples", type=int, default=DEFAULT_MAX_DECLICK_SAMPLES,
                        help="Maximum contiguous samples per repaired click event.")
    parser.add_argument("--max-declick-events", type=int, default=DEFAULT_MAX_DECLICK_EVENTS,
                        help="Safety cap for repaired click events.")
    parser.add_argument("--detailed-loudness-report", action="store_true",
                        help="Measure EBU R128 section/focus diagnostics; slower.")
    parser.add_argument("--fast-loudness-steps", default="",
        help=(
            "Experimental comma-separated steps to measure with libebur128 "
                            "instead of FFmpeg loudnorm: pre_master,post_l2,controlled_makeup_1,controlled_makeup_2."
                        ))
    parser.add_argument("--compare-fast-loudness", action="store_true",
                        help="When a fast step is enabled, also run FFmpeg loudnorm and store deltas.")
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()
    if args.no_global_declick:
        args.global_declick = "off"
    fast_loudness_steps = {
        step.strip()
        for step in args.fast_loudness_steps.split(",")
        if step.strip()
    }
    supported_fast_steps = {"pre_master", "post_l2", "controlled_makeup_1", "controlled_makeup_2"}
    unknown_fast_steps = sorted(fast_loudness_steps - supported_fast_steps)
    if unknown_fast_steps:
        raise SystemExit(f"Unsupported --fast-loudness-steps: {', '.join(unknown_fast_steps)}")
    if fast_loudness_steps and libebur128_measure is None:
        raise SystemExit("libebur128 fast loudness requested, but probe_loudness_libebur128/libebur128 is unavailable.")

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = args.metadata or args.output_wav.with_suffix(".loudness.json")
    timings_sec: dict[str, float] = {}
    fast_loudness_comparisons: dict[str, dict[str, object]] = {}
    total_start = time.perf_counter()

    def mark_timing(label: str, start: float) -> None:
        timings_sec[label] = round(time.perf_counter() - start, 3)

    def measure_loudness_for_step(path: Path, step: str) -> dict[str, float | str]:
        if step not in fast_loudness_steps:
            stage_start = time.perf_counter()
            measured = loudnorm_measure(path, args.target_i, args.target_tp, args.target_lra)
            mark_timing(f"{step}_loudnorm", stage_start)
            return measured
        stage_start = time.perf_counter()
        fast = libebur128_measure(path)
        mark_timing(f"{step}_libebur128", stage_start)
        libebur128_tp = fast.get("input_tp")
        stage_start = time.perf_counter()
        true_peak = ffmpeg_ebur128_true_peak(path)
        mark_timing(f"{step}_ffmpeg_true_peak", stage_start)
        fast["libebur128_input_tp"] = libebur128_tp
        fast["input_tp"] = true_peak["input_tp"]
        fast["true_peak_source"] = true_peak
        fast["normalization_type"] = "measurement_only"
        if args.compare_fast_loudness:
            stage_start = time.perf_counter()
            ffmpeg = loudnorm_measure(path, args.target_i, args.target_tp, args.target_lra)
            mark_timing(f"{step}_loudnorm_compare", stage_start)
            fast_loudness_comparisons[step] = {
                "libebur128": fast,
                "ffmpeg_loudnorm": ffmpeg,
                "delta_fast_minus_ffmpeg": loudness_delta(fast, ffmpeg),
            }
        return fast

    stage_start = time.perf_counter()
    plan_loudness = load_mix_plan_loudness(args.mix_plan)
    mark_timing("load_mix_plan_loudness", stage_start)
    if plan_loudness is not None:
        args.target_i = plan_loudness["target_i"]
        args.target_lra = plan_loudness.get("target_lra", args.target_lra)

    reference_meta: dict | None = None
    if args.reference_audio:
        if plan_loudness is not None:
            ref_i = plan_loudness["target_i"]
            ref_lra = plan_loudness.get("target_lra", args.target_lra)
            print(f"[ref] Using cached reference loudness from mix plan: {ref_i:.1f} LUFS")
        else:
            print(f"[ref] Measuring reference: {args.reference_audio}")
            stage_start = time.perf_counter()
            ref_i, ref_lra = measure_reference_lufs(args.reference_audio)
            mark_timing("measure_reference_loudness", stage_start)
            args.target_i = ref_i
            args.target_lra = max(6.0, min(14.0, ref_lra))
        reference_meta = {
            "path": str(args.reference_audio),
            "input_i_lufs": ref_i,
            "input_lra": ref_lra,
            "derived_target_i": clamp(args.target_i, FINAL_LOUDNESS_MIN_LUFS, FINAL_LOUDNESS_MAX_LUFS),
            "derived_target_lra": args.target_lra,
        }
        print(f"[ref] Reference measured: {ref_i:.1f} LUFS; render target {args.target_i:.1f} LUFS")

    args.target_i = clamp(args.target_i, FINAL_LOUDNESS_MIN_LUFS, FINAL_LOUDNESS_MAX_LUFS)
    print(
        f"[target] Master pregain -> soft TP trim; window "
        f"[{FINAL_LOUDNESS_MIN_LUFS:.1f}, {FINAL_LOUDNESS_MAX_LUFS:.1f}] LUFS -> {args.target_i:.1f} LUFS"
    )

    pre_measure = measure_loudness_for_step(args.input_wav, "pre_master")
    if args.detailed_loudness_report:
        stage_start = time.perf_counter()
        pre_sections = ebur128_sections(args.input_wav)
        mark_timing("pre_master_ebur128_sections", stage_start)
    else:
        pre_sections = {}
    needed_gain_db = args.target_i - float(pre_measure["input_i"])
    desired_pregain_db = master_pregain_db(
        pre_measure, args.target_i, args.max_gain_db, args.max_attenuation_db
    )
    input_tp = float(pre_measure.get("input_tp", -10.0))
    safe_pregain_ceiling_db = max(0.0, PREGAIN_INPUT_TP_HEADROOM_DB - input_tp)
    pre_l2_gain_ceiling_db = min(args.max_gain_db, safe_pregain_ceiling_db)
    pregain_db = clamp(
        needed_gain_db,
        -args.max_attenuation_db,
        pre_l2_gain_ceiling_db,
    )
    print(
        f"[master] Initial pregain: {pregain_db:.2f} dB "
        f"(needed {needed_gain_db:.2f}, input TP {input_tp:.2f}, "
        f"safe ceiling {pre_l2_gain_ceiling_db:.2f}); "
        f"L2 handles only the safe pregain stage before soft TP makeup."
    )

    with tempfile.TemporaryDirectory(prefix="master_loudness_") as tmp_dir:
        tmp_root = Path(tmp_dir)
        gained = tmp_root / "01_pregain.wav"
        l2_out = tmp_root / "02_l2.wav"
        trimmed = tmp_root / "03_post_trim.wav"
        limited = tmp_root / "04_limited.wav"
        declicked = tmp_root / "05_declicked.wav"
        tp_safe = tmp_root / "06_true_peak_safe.wav"

        stage_start = time.perf_counter()
        apply_gain(args.input_wav, gained, pregain_db)
        mark_timing("apply_pregain", stage_start)
        post_gain_measure = shifted_loudness_measure(pre_measure, pregain_db)
        if args.limiter is not None and args.limiter.exists():
            stage_start = time.perf_counter()
            limiter_proc = run([str(args.limiter), str(gained), str(l2_out)])
            mark_timing("master_l2", stage_start)
            if limiter_proc.returncode != 0:
                raise RuntimeError(limiter_proc.stderr)
            post_l2_measure = measure_loudness_for_step(l2_out, "post_l2")
        else:
            stage_start = time.perf_counter()
            shutil.copyfile(gained, l2_out)
            mark_timing("copy_pregain_to_l2_out", stage_start)
            post_l2_measure = post_gain_measure

        pre_trim_db = 0.0
        residual_needed_db = args.target_i - float(post_l2_measure["input_i"])
        # Soft TP limiter sits after this stage with a 50 ms attack — leave a small
        # margin so makeup gain doesn't force aggressive gain reduction that audibly
        # squashes the chorus.
        post_l2_tp = float(post_l2_measure["input_tp"])
        post_trim_headroom_db = max(0.0, args.target_tp - post_l2_tp - 0.5)
        post_trim_db = clamp(residual_needed_db, 0.0, post_trim_headroom_db)
        stage_start = time.perf_counter()
        apply_gain(l2_out, trimmed, post_trim_db)
        mark_timing("apply_post_trim", stage_start)
        post_trim_measure = (
            shifted_loudness_measure(post_l2_measure, post_trim_db)
            if post_trim_db > 0.005
            else post_l2_measure
        )

        controlled_makeup_steps: list[dict[str, float]] = []
        controlled_makeup_db = 0.0
        controlled_makeup_residual_before_db = max(
            0.0,
            args.target_i - float(post_trim_measure["input_i"]),
        )
        controlled_current = trimmed
        controlled_current_measure = post_trim_measure
        remaining_makeup_db = max(0.0, args.controlled_limiter_makeup_max_db)
        step_index = 0
        while remaining_makeup_db > 0.005 and step_index < CONTROLLED_LIMITER_MAKEUP_MAX_STEPS:
            residual_db = args.target_i - float(controlled_current_measure["input_i"])
            if residual_db <= 0.05:
                break
            step_index += 1
            step_gain_db = clamp(
                residual_db,
                0.0,
                min(remaining_makeup_db, CONTROLLED_LIMITER_MAKEUP_STEP_DB),
            )
            makeup_in = tmp_root / f"04_makeup_{step_index}.wav"
            makeup_limited = tmp_root / f"04_makeup_limited_{step_index}.wav"
            before_i = float(controlled_current_measure["input_i"])
            stage_start = time.perf_counter()
            apply_gain(controlled_current, makeup_in, step_gain_db)
            mark_timing(f"controlled_makeup_{step_index}_gain", stage_start)
            stage_start = time.perf_counter()
            apply_soft_peak_limit(makeup_in, makeup_limited, args.target_tp)
            mark_timing(f"controlled_makeup_{step_index}_soft_peak_limit", stage_start)
            after_measure = measure_loudness_for_step(makeup_limited, f"controlled_makeup_{step_index}")
            controlled_makeup_steps.append(
                {
                    "gain_db": round(step_gain_db, 3),
                    "input_i_before": round(before_i, 3),
                    "input_i_after": round(float(after_measure["input_i"]), 3),
                    "input_tp_after": round(float(after_measure["input_tp"]), 3),
                }
            )
            controlled_makeup_db += step_gain_db
            remaining_makeup_db -= step_gain_db
            controlled_current = makeup_limited
            controlled_current_measure = after_measure
            residual_after_step_db = args.target_i - float(after_measure["input_i"])
            if residual_after_step_db <= CONTROLLED_LIMITER_MAKEUP_TOLERANCE_DB:
                break

        if controlled_makeup_db > 0.005:
            stage_start = time.perf_counter()
            shutil.copyfile(controlled_current, limited)
            mark_timing("copy_controlled_makeup_to_limited", stage_start)
            post_limiter_measure = controlled_current_measure
        else:
            stage_start = time.perf_counter()
            apply_soft_peak_limit(trimmed, limited, args.target_tp)
            mark_timing("soft_peak_limit", stage_start)
            stage_start = time.perf_counter()
            post_limiter_measure = loudnorm_measure(limited, args.target_i, args.target_tp, args.target_lra)
            mark_timing("post_limiter_loudnorm", stage_start)
        controlled_makeup_residual_after_db = max(
            0.0,
            args.target_i - float(post_limiter_measure["input_i"]),
        )
        declick_threshold = max(0.05, args.declick_threshold)
        max_declick_samples = max(1, args.max_declick_samples)
        max_declick_events = max(1, args.max_declick_events)
        if args.global_declick == "off":
            declick_report = {
                "enabled": False,
                "mode": "off",
                "events": 0,
                "samples_repaired": 0,
            }
            stage_start = time.perf_counter()
            shutil.copyfile(limited, args.output_wav)
            mark_timing("copy_limited_to_output", stage_start)
        elif args.global_declick == "auto":
            stage_start = time.perf_counter()
            declick_report = apply_global_declick(
                limited,
                declicked,
                threshold=declick_threshold,
                max_click_samples=max_declick_samples,
                max_events=max_declick_events,
            )
            declick_report["mode"] = "auto"
            declick_report["triggered"] = int(declick_report["samples_repaired"]) > 0
            mark_timing("global_declick", stage_start)
            stage_start = time.perf_counter()
            if declick_report["triggered"]:
                shutil.copyfile(declicked, args.output_wav)
                mark_timing("copy_declicked_to_output", stage_start)
            else:
                shutil.copyfile(limited, args.output_wav)
                mark_timing("copy_limited_to_output", stage_start)
        else:
            stage_start = time.perf_counter()
            declick_report = apply_global_declick(
                limited,
                declicked,
                threshold=declick_threshold,
                max_click_samples=max_declick_samples,
                max_events=max_declick_events,
            )
            declick_report["mode"] = "always"
            mark_timing("global_declick", stage_start)
            stage_start = time.perf_counter()
            shutil.copyfile(declicked, args.output_wav)
            mark_timing("copy_declicked_to_output", stage_start)
        stage_start = time.perf_counter()
        output_peak_measure = loudnorm_measure(args.output_wav, args.target_i, args.target_tp, args.target_lra)
        mark_timing("output_loudnorm", stage_start)
        output_tp = float(output_peak_measure["input_tp"])
        true_peak_safety_trim_db = 0.0
        if output_tp > args.target_tp:
            true_peak_safety_trim_db = args.target_tp - output_tp
            stage_start = time.perf_counter()
            apply_gain(args.output_wav, tp_safe, true_peak_safety_trim_db)
            mark_timing("true_peak_safety_trim_gain", stage_start)
            stage_start = time.perf_counter()
            shutil.copyfile(tp_safe, args.output_wav)
            mark_timing("copy_true_peak_safe_to_output", stage_start)
            if args.detailed_loudness_report:
                stage_start = time.perf_counter()
                final_measure = loudnorm_measure(args.output_wav, args.target_i, args.target_tp, args.target_lra)
                mark_timing("final_loudnorm_after_true_peak_trim", stage_start)
            else:
                final_measure = shifted_loudness_measure(output_peak_measure, true_peak_safety_trim_db)
        else:
            final_measure = output_peak_measure
        print(
            f"[master] Pregain {pregain_db:.2f} dB; post-pregain TP "
            f"{float(post_gain_measure['input_tp']):.2f} dBTP; post-L2 TP "
            f"{post_l2_tp:.2f} dBTP; post-trim {post_trim_db:.2f} dB "
            f"(residual need {residual_needed_db:.2f}, headroom {post_trim_headroom_db:.2f}); "
            f"controlled makeup {controlled_makeup_db:.2f} dB "
            f"(residual after {controlled_makeup_residual_after_db:.2f}); "
            f"TP safety trim {true_peak_safety_trim_db:.2f} dB; "
            f"final TP {float(final_measure['input_tp']):.2f} dBTP "
            f"(ceiling {args.target_tp:.1f})"
        )
        if declick_report.get("enabled") and declick_report.get("mode") == "auto" and not declick_report.get("triggered", True):
            print("[master] Global de-click auto skipped: no click candidates")
        elif declick_report.get("enabled"):
            print(
                f"[master] Global de-click repaired {declick_report['samples_repaired']} sample(s) "
                f"in {declick_report['events']} isolated event(s)"
            )

    if args.detailed_loudness_report:
        stage_start = time.perf_counter()
        final_sections = ebur128_sections(args.output_wav)
        mark_timing("final_ebur128_sections", stage_start)
    else:
        final_sections = {}
    final_focus_windows = (
        {
            "38_50s": ebur128_window_summary(final_sections, 38.0, 50.0),
            "168_182s": ebur128_window_summary(final_sections, 168.0, 182.0),
        }
        if args.detailed_loudness_report
        else {}
    )
    actual_i_lufs = float(final_measure["input_i"])
    actual_tp_db = float(final_measure["input_tp"])
    target_error_db = round(actual_i_lufs - args.target_i, 3)
    available_gain_db = round(
        max(0.0, pregain_db)
        + post_trim_headroom_db
        + max(0.0, args.controlled_limiter_makeup_max_db),
        3,
    )
    loudness_under_compensated = (args.target_i - actual_i_lufs) > LOUDNESS_MISS_TOLERANCE_DB
    if loudness_under_compensated:
        print(
            "[master][warning] loudness under-compensated: "
            f"needed {needed_gain_db:.2f} dB, available {available_gain_db:.2f} dB, "
            f"target error {target_error_db:.2f} dB"
        )
    timings_sec["total"] = round(time.perf_counter() - total_start, 3)
    metadata = {
        "enabled": True,
        "mode": "master_safe_pregain_l2_controlled_makeup_soft_tp",
        "reference_audio": reference_meta,
        "final_loudness_window_lufs": [FINAL_LOUDNESS_MIN_LUFS, FINAL_LOUDNESS_MAX_LUFS],
        "target_i_lufs": args.target_i,
        "target_tp_db": args.target_tp,
        "target_lra": args.target_lra,
        "max_gain_db": args.max_gain_db,
        "max_attenuation_db": args.max_attenuation_db,
        "controlled_limiter_makeup_max_db": args.controlled_limiter_makeup_max_db,
        "controlled_limiter_makeup_max_steps": CONTROLLED_LIMITER_MAKEUP_MAX_STEPS,
        "detailed_loudness_report": args.detailed_loudness_report,
        "fast_loudness_steps": sorted(fast_loudness_steps),
        "fast_loudness_comparisons": fast_loudness_comparisons,
        "timings_sec": timings_sec,
        "pregain_db": pregain_db,
        "desired_pregain_db": desired_pregain_db,
        "needed_gain_db": round(needed_gain_db, 3),
        "available_gain_db": available_gain_db,
        "safe_pregain_ceiling_db": round(safe_pregain_ceiling_db, 3),
        "pre_l2_gain_ceiling_db": round(pre_l2_gain_ceiling_db, 3),
        "pre_trim_db": pre_trim_db,
        "post_trim_db": post_trim_db if abs(post_trim_db) >= 0.05 else 0.0,
        "post_trim_headroom_db": round(post_trim_headroom_db, 3),
        "true_peak_safety_trim_db": round(true_peak_safety_trim_db, 3),
        "controlled_limiter_makeup": {
            "enabled": controlled_makeup_db > 0.005,
            "applied_db": round(controlled_makeup_db, 3),
            "residual_before_db": round(controlled_makeup_residual_before_db, 3),
            "residual_after_db": round(controlled_makeup_residual_after_db, 3),
            "steps": controlled_makeup_steps,
        },
        "post_pregain": post_gain_measure,
        "post_l2": post_l2_measure,
        "post_trim": post_trim_measure,
        "pre_master": pre_measure,
        "pre_master_sections": {key: value for key, value in pre_sections.items() if key != "sections"},
        "post_limiter": post_limiter_measure,
        "global_declick": declick_report,
        "final": final_measure,
        "actual_i_lufs": actual_i_lufs,
        "actual_tp_db": actual_tp_db,
        "actual_lra": final_measure.get("input_lra"),
        "in_target_window": FINAL_LOUDNESS_MIN_LUFS <= actual_i_lufs <= FINAL_LOUDNESS_MAX_LUFS,
        "target_error_db": target_error_db,
        "loudness_under_compensated": loudness_under_compensated,
        "final_sections": {key: value for key, value in final_sections.items() if key != "sections"},
        "final_focus_windows": final_focus_windows,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
