#!/usr/bin/env python3
"""
Automatic vocal/accompaniment loudness and dynamics shaping.

Rule preset derived from bc/vo summary:
1. vo: lift more aggressively and apply gentle dynamic convergence
2. bc: keep slightly tucked and reduce level fluctuations for glue
3. Keep accompaniment below vocal during sung sections
4. Output premixed stems and optional stereo mix
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_gain_rules import AudioFeatures, GainDecision


SILENCE_RE = re.compile(r"silence_(start|end):\s*([-0-9.]+)")
MEAN_VOL_RE = re.compile(r"mean_volume:\s*([-0-9.]+)\s*dB")
MAX_VOL_RE = re.compile(r"max_volume:\s*([-0-9.]+)\s*dB")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "bc_vo_mix_rules.json"
BALANCE_MEDIAN_LIMIT_DB = 2.0
BALANCE_P90_LIMIT_DB = 4.0
BURIED_MEDIAN_LIMIT_DB = -2.0
BALANCE_MAX_VOCAL_TRIM_DB = 2.0
BALANCE_MAX_ACCOMP_TRIM_DB = 2.5
PEAK_CEILING_DB = -1.0
ENERGY_FOLLOW_START_DB = 4.0
ENERGY_FOLLOW_STRENGTH = 0.35
ENERGY_FOLLOW_MAX_TRIM_DB = 2.0
MAX_ADJACENT_GAIN_JUMP_DB = 1.5
BOUNDARY_FADE_SEC = 0.008


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    root = Path(__file__).resolve().parent.parent
    for candidate in (
        root / ".tools" / "msys64" / "ucrt64" / "bin" / f"{name}.exe",
        root / ".tools" / "msys64" / "usr" / "bin" / f"{name}.exe",
    ):
        if candidate.exists():
            return str(candidate)
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return name


FFMPEG = command_path("ffmpeg")
FFPROBE = command_path("ffprobe")
_AUDIO_CACHE: dict[str, tuple[np.ndarray, int]] = {}


@dataclass
class CompressorConfig:
    threshold: float
    ratio: float
    attack_ms: float
    release_ms: float
    makeup: float


@dataclass
class RoleConfig:
    target_db: float
    gain_min_db: float
    gain_max_db: float
    compressor: CompressorConfig
    limiter_ceiling: float


@dataclass
class AccompanimentConfig:
    base_gain_db: float
    body_gap_db: float
    body_extra_duck_db: float
    gain_min_db: float
    gain_max_db: float
    compressor: CompressorConfig
    limiter_ceiling: float


@dataclass
class DetectionConfig:
    silence_noise_db: float
    min_silence_sec: float
    merge_gap_sec: float
    min_voiced_sec: float
    paragraph_gap_sec: float
    paragraph_pad_sec: float


@dataclass
class MixConfig:
    vocal: RoleConfig
    accompaniment: AccompanimentConfig
    detection: DetectionConfig


def default_config() -> MixConfig:
    return MixConfig(
        vocal=RoleConfig(
            target_db=-18.0,
            gain_min_db=-3.0,
            gain_max_db=0.0,
            compressor=CompressorConfig(
                threshold=0.14,
                ratio=2.2,
                attack_ms=5.0,
                release_ms=90.0,
                makeup=1.0,
            ),
            limiter_ceiling=0.97,
        ),
        accompaniment=AccompanimentConfig(
            base_gain_db=0.0,
            body_gap_db=4.5,
            body_extra_duck_db=0.0,
            gain_min_db=-3.0,
            gain_max_db=0.0,
            compressor=CompressorConfig(
                threshold=0.10,
                ratio=1.7,
                attack_ms=20.0,
                release_ms=180.0,
                makeup=1.0,
            ),
            limiter_ceiling=0.98,
        ),
        detection=DetectionConfig(
            silence_noise_db=-38.0,
            min_silence_sec=0.20,
            merge_gap_sec=0.14,
            min_voiced_sec=0.08,
            paragraph_gap_sec=1.20,
            paragraph_pad_sec=0.08,
        ),
    )


def compressor_filter(cfg: CompressorConfig) -> str:
    return (
        "acompressor="
        f"threshold={cfg.threshold}:"
        f"ratio={cfg.ratio}:"
        f"attack={cfg.attack_ms}:"
        f"release={cfg.release_ms}:"
        f"makeup={cfg.makeup}"
    )


def limiter_filter(ceiling: float) -> str:
    return f"alimiter=limit={ceiling}"


def config_to_dict(cfg: MixConfig) -> dict[str, object]:
    return asdict(cfg)


def load_config(path: str | None) -> MixConfig:
    if not path:
        return default_config()
    data = config_to_dict(default_config())
    user_data = json.loads(Path(path).read_text(encoding="utf-8"))
    merge_config_dict(data, user_data)
    return MixConfig(
        vocal=RoleConfig(
            target_db=float(data["vocal"]["target_db"]),
            gain_min_db=float(data["vocal"]["gain_min_db"]),
            gain_max_db=float(data["vocal"]["gain_max_db"]),
            compressor=CompressorConfig(**data["vocal"]["compressor"]),
            limiter_ceiling=float(data["vocal"]["limiter_ceiling"]),
        ),
        accompaniment=AccompanimentConfig(
            base_gain_db=float(data["accompaniment"]["base_gain_db"]),
            body_gap_db=float(data["accompaniment"]["body_gap_db"]),
            body_extra_duck_db=float(data["accompaniment"]["body_extra_duck_db"]),
            gain_min_db=float(data["accompaniment"]["gain_min_db"]),
            gain_max_db=float(data["accompaniment"]["gain_max_db"]),
            compressor=CompressorConfig(**data["accompaniment"]["compressor"]),
            limiter_ceiling=float(data["accompaniment"]["limiter_ceiling"]),
        ),
        detection=DetectionConfig(**data["detection"]),
    )


def merge_config_dict(base: dict[str, object], overlay: dict[str, object]) -> None:
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merge_config_dict(current, value)
        else:
            base[key] = value


def write_config(path: str, cfg: MixConfig) -> None:
    Path(path).write_text(
        json.dumps(config_to_dict(cfg), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass
class Segment:
    start: float
    end: float
    is_voice: bool
    gain_db: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SegmentDecision:
    start: float
    end: float
    gain_db: float
    rule_id: str
    note: str


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=check,
    )


def cache_key(path: str) -> str:
    return str(Path(path).resolve(strict=False))


def invalidate_audio_cache(path: str) -> None:
    _AUDIO_CACHE.pop(cache_key(path), None)


def load_audio_cached(path: str) -> tuple[np.ndarray, int]:
    key = cache_key(path)
    cached = _AUDIO_CACHE.get(key)
    if cached is not None:
        return cached
    data, sr = sf.read(path, always_2d=True, dtype="float64")
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    _AUDIO_CACHE[key] = (data, int(sr))
    return data, int(sr)


def audio_window(path: str, start: float | None = None, end: float | None = None) -> np.ndarray:
    data, sr = load_audio_cached(path)
    start_frame = 0 if start is None else max(0, int(start * sr))
    end_frame = data.shape[0] if end is None else min(data.shape[0], int(end * sr))
    if end_frame <= start_frame:
        return data[:0]
    return data[start_frame:end_frame]


def dbfs(value: float, floor: float = -99.0) -> float:
    if value <= 0.0 or not math.isfinite(value):
        return floor
    return 20.0 * math.log10(value)


def ffprobe_duration(path: str) -> float:
    cp = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    return float(cp.stdout.strip())


def detect_silences(path: str, noise_db: float, min_silence: float) -> list[tuple[float, float]]:
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-i",
        path,
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    cp = run(cmd, check=False)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")

    starts: list[float] = []
    intervals: list[tuple[float, float]] = []
    for kind, value in SILENCE_RE.findall(text):
        t = float(value)
        if kind == "start":
            starts.append(t)
        elif starts:
            intervals.append((starts.pop(0), t))
    return intervals


def invert_silences(duration: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not silences:
        return [(0.0, duration)]

    voice: list[tuple[float, float]] = []
    pos = 0.0
    for s0, s1 in silences:
        if s0 > pos:
            voice.append((pos, s0))
        pos = max(pos, s1)
    if pos < duration:
        voice.append((pos, duration))
    return [(max(0.0, a), min(duration, b)) for a, b in voice if b - a > 0.03]


def merge_short_gaps(voice: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    if not voice:
        return []
    merged = [list(voice[0])]
    for s, e in voice[1:]:
        prev = merged[-1]
        if s - prev[1] <= max_gap:
            prev[1] = e
        else:
            merged.append([s, e])
    return [(a, b) for a, b in merged]


def pad_intervals(
    intervals: list[tuple[float, float]],
    duration: float,
    pad: float,
) -> list[tuple[float, float]]:
    padded: list[tuple[float, float]] = []
    for idx, (start, end) in enumerate(intervals):
        left_limit = 0.0
        right_limit = duration
        if idx > 0:
            left_limit = (intervals[idx - 1][1] + start) / 2.0
        if idx + 1 < len(intervals):
            right_limit = (end + intervals[idx + 1][0]) / 2.0
        padded_start = max(left_limit, start - pad)
        padded_end = min(right_limit, end + pad)
        if padded_end - padded_start > 0.03:
            padded.append((padded_start, padded_end))
    return padded


def detect_paragraphs(
    duration: float,
    voice: list[tuple[float, float]],
    cfg: DetectionConfig,
) -> list[tuple[float, float]]:
    valid_voice = [(start, end) for start, end in voice if end - start >= cfg.min_voiced_sec]
    long_regions = merge_short_gaps(valid_voice, max_gap=cfg.paragraph_gap_sec)
    return pad_intervals(long_regions, duration, cfg.paragraph_pad_sec)


def build_timeline(duration: float, voice: list[tuple[float, float]]) -> list[Segment]:
    out: list[Segment] = []
    pos = 0.0
    for s, e in voice:
        if s > pos:
            out.append(Segment(pos, s, False, 0.0))
        out.append(Segment(s, e, True, 0.0))
        pos = e
    if pos < duration:
        out.append(Segment(pos, duration, False, 0.0))
    return [seg for seg in out if seg.duration > 0.0]


def measure_mean_volume(path: str, start: float | None = None, end: float | None = None) -> float:
    try:
        window = audio_window(path, start, end)
        if window.size == 0:
            return -60.0
        return max(-60.0, dbfs(float(np.sqrt(np.mean(np.square(window)))), floor=-60.0))
    except Exception:
        cmd = [FFMPEG, "-hide_banner"]
        if start is not None:
            cmd += ["-ss", f"{start:.6f}"]
        if end is not None:
            cmd += ["-to", f"{end:.6f}"]
        cmd += ["-i", path, "-af", "volumedetect", "-f", "null", "-"]
        cp = run(cmd, check=False)
        text = (cp.stdout or "") + "\n" + (cp.stderr or "")
        m = MEAN_VOL_RE.search(text)
        if not m:
            return -60.0
        return float(m.group(1))


def measure_max_volume(path: str) -> float:
    try:
        data = audio_window(path)
        if data.size == 0:
            return -99.0
        return dbfs(float(np.max(np.abs(data))), floor=-99.0)
    except Exception:
        cmd = [FFMPEG, "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"]
        cp = run(cmd, check=False)
        text = (cp.stdout or "") + "\n" + (cp.stderr or "")
        m = MAX_VOL_RE.search(text)
        if not m:
            return -99.0
        return float(m.group(1))


def measure_peak_volume(path: str, start: float | None = None, end: float | None = None) -> float:
    try:
        window = audio_window(path, start, end)
        if window.size == 0:
            return -99.0
        return dbfs(float(np.max(np.abs(window))), floor=-99.0)
    except Exception:
        cmd = [FFMPEG, "-hide_banner"]
        if start is not None:
            cmd += ["-ss", f"{start:.6f}"]
        if end is not None:
            cmd += ["-to", f"{end:.6f}"]
        cmd += ["-i", path, "-af", "volumedetect", "-f", "null", "-"]
        cp = run(cmd, check=False)
        text = (cp.stdout or "") + "\n" + (cp.stderr or "")
        m = MAX_VOL_RE.search(text)
        if not m:
            return -99.0
        return float(m.group(1))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def peak_trim_db(peak_db: float, ceiling_db: float = PEAK_CEILING_DB) -> float:
    if peak_db > ceiling_db:
        return ceiling_db - peak_db
    return 0.0


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def energy_follow_reference(path: str, segments: list[Segment]) -> float:
    means = [
        measure_mean_volume(path, seg.start, seg.end)
        for seg in segments
        if seg.duration >= 0.5
    ]
    means = [value for value in means if not math.isinf(value)]
    return percentile(means, 0.85) if means else -60.0


def energy_follow_trim(
    accomp_mean_db: float,
    accomp_ref_db: float,
    current_gap_db: float | None = None,
) -> float:
    """Negative-only trim so low-energy sections stay low after master gain."""
    if math.isinf(accomp_mean_db) or math.isinf(accomp_ref_db):
        return 0.0
    drop = accomp_ref_db - accomp_mean_db
    trim = -clamp(
        (drop - ENERGY_FOLLOW_START_DB) * ENERGY_FOLLOW_STRENGTH,
        0.0,
        ENERGY_FOLLOW_MAX_TRIM_DB,
    )
    if current_gap_db is None:
        return trim
    lowest_safe_trim = min(0.0, BURIED_MEDIAN_LIMIT_DB - current_gap_db)
    return max(trim, lowest_safe_trim)


def nearest_voiced_gain(timeline: list[Segment], index: int, direction: int) -> float | None:
    pos = index + direction
    while 0 <= pos < len(timeline):
        if timeline[pos].is_voice:
            return timeline[pos].gain_db
        pos += direction
    return None


def inherit_unvoiced_gains(timeline: list[Segment], allow_positive: bool = False) -> None:
    def cap(value: float) -> float:
        return value if allow_positive else min(0.0, value)

    for idx, seg in enumerate(timeline):
        if seg.is_voice:
            continue
        prev_gain = nearest_voiced_gain(timeline, idx, -1)
        next_gain = nearest_voiced_gain(timeline, idx, 1)
        if prev_gain is None and next_gain is None:
            seg.gain_db = cap(seg.gain_db)
        elif prev_gain is None:
            seg.gain_db = cap(next_gain if next_gain is not None else seg.gain_db)
        elif next_gain is None:
            seg.gain_db = cap(prev_gain)
        elif seg.duration <= 1.25:
            seg.gain_db = cap(min(prev_gain, next_gain))
        else:
            seg.gain_db = cap((prev_gain + next_gain) / 2.0)


def limit_adjacent_gain_jumps(
    timeline: list[Segment],
    max_jump_db: float = MAX_ADJACENT_GAIN_JUMP_DB,
    allow_positive: bool = False,
) -> None:
    def cap(value: float) -> float:
        return value if allow_positive else min(0.0, value)

    if not timeline:
        return
    for idx in range(1, len(timeline)):
        prev = timeline[idx - 1]
        current = timeline[idx]
        if current.gain_db - prev.gain_db > max_jump_db:
            current.gain_db = cap(prev.gain_db + max_jump_db)
        elif prev.gain_db - current.gain_db > max_jump_db:
            current.gain_db = cap(prev.gain_db - max_jump_db)
    for idx in range(len(timeline) - 2, -1, -1):
        next_seg = timeline[idx + 1]
        current = timeline[idx]
        if current.gain_db - next_seg.gain_db > max_jump_db:
            current.gain_db = cap(next_seg.gain_db + max_jump_db)
        elif next_seg.gain_db - current.gain_db > max_jump_db:
            current.gain_db = cap(next_seg.gain_db - max_jump_db)


def segment_gain_rows(segments: list[Segment]) -> list[dict[str, float | bool]]:
    return [
        {
            "start": round(seg.start, 3),
            "end": round(seg.end, 3),
            "duration": round(seg.duration, 3),
            "is_voice": seg.is_voice,
            "gain_db": round(seg.gain_db, 3),
        }
        for seg in segments
    ]


def gain_jump_rows(
    segments: list[Segment],
    threshold_db: float = MAX_ADJACENT_GAIN_JUMP_DB,
) -> list[dict[str, float | bool]]:
    rows: list[dict[str, float | bool]] = []
    for prev, current in zip(segments, segments[1:]):
        delta = current.gain_db - prev.gain_db
        rows.append(
            {
                "time": round(current.start, 3),
                "prev_gain_db": round(prev.gain_db, 3),
                "next_gain_db": round(current.gain_db, 3),
                "delta_db": round(delta, 3),
                "prev_is_voice": prev.is_voice,
                "next_is_voice": current.is_voice,
                "flagged": abs(delta) > threshold_db,
            }
        )
    return rows


def balance_stats(
    vocal_path: str,
    accomp_path: str,
    voice_segments: list[Segment],
    median_limit_db: float = BALANCE_MEDIAN_LIMIT_DB,
    p90_limit_db: float = BALANCE_P90_LIMIT_DB,
) -> dict[str, object]:
    rows: list[dict[str, float | bool]] = []
    gaps: list[float] = []
    for seg in voice_segments:
        if seg.duration < 0.08:
            continue
        vocal_mean = measure_mean_volume(vocal_path, seg.start, seg.end)
        accomp_mean = measure_mean_volume(accomp_path, seg.start, seg.end)
        gap = vocal_mean - accomp_mean
        gaps.append(gap)
        rows.append(
            {
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "duration": round(seg.duration, 3),
                "vocal_mean_db": round(vocal_mean, 3),
                "accomp_mean_db": round(accomp_mean, 3),
                "gap_db": round(gap, 3),
                "too_forward": gap > p90_limit_db,
            }
        )

    median_gap = percentile(gaps, 0.5)
    p90_gap = percentile(gaps, 0.9)
    max_gap = max(gaps) if gaps else 0.0
    return {
        "segment_count": len(gaps),
        "median_gap_db": round(median_gap, 3),
        "p90_gap_db": round(p90_gap, 3),
        "max_gap_db": round(max_gap, 3),
        "median_limit_db": median_limit_db,
        "p90_limit_db": p90_limit_db,
        "too_forward_segments": sum(1 for row in rows if row["too_forward"]),
        "segments": rows,
    }


def vocal_trim_for_balance(stats: dict[str, object]) -> float:
    median_gap = float(stats.get("median_gap_db") or 0.0)
    p90_gap = float(stats.get("p90_gap_db") or 0.0)
    trim = max(
        0.0,
        median_gap - BALANCE_MEDIAN_LIMIT_DB,
        p90_gap - BALANCE_P90_LIMIT_DB,
    )
    return clamp(trim, 0.0, BALANCE_MAX_VOCAL_TRIM_DB)


def accomp_trim_for_balance(stats: dict[str, object]) -> float:
    median_gap = float(stats.get("median_gap_db") or 0.0)
    if median_gap >= BURIED_MEDIAN_LIMIT_DB:
        return 0.0
    return clamp(BURIED_MEDIAN_LIMIT_DB - median_gap, 0.0, BALANCE_MAX_ACCOMP_TRIM_DB)


def apply_in_place_gain(path: str, gain_db: float) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        render_volume_only(path, tmp_path, gain_db)
        os.replace(tmp_path, path)
        invalidate_audio_cache(path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def segment_features(
    path: str,
    start: float,
    end: float,
    rms_target: float,
    peak_limit: float,
    is_segment_start: bool = False,
    is_segment_end: bool = False,
    vo_rms_yuan: float | None = None,
    bc_rms_yuan: float | None = None,
) -> AudioFeatures:
    rms = measure_mean_volume(path, start, end)
    peak = measure_peak_volume(path, start, end)
    crest = peak - rms
    dynamic_range = max(0.0, crest * 0.5)
    return AudioFeatures(
        rms_yuan=rms,
        peak_yuan=peak,
        rms_target=rms_target,
        peak_limit=peak_limit,
        crest_factor=crest,
        dynamic_range=dynamic_range,
        is_segment_start=is_segment_start,
        is_segment_end=is_segment_end,
        is_stable=dynamic_range < 2.0,
        vo_rms_yuan=vo_rms_yuan,
        bc_rms_yuan=bc_rms_yuan,
    )


def decide_vocal_segments(
    path: str,
    accomp_path: str,
    timeline: list[Segment],
    cfg: MixConfig,
) -> list[SegmentDecision]:
    voiced = [s for s in timeline if s.is_voice and s.duration >= cfg.detection.min_voiced_sec]
    accomp_ref = energy_follow_reference(accomp_path, voiced)
    out: list[SegmentDecision] = []
    for seg in voiced:
        peak = measure_peak_volume(path, seg.start, seg.end)
        vocal_mean = measure_mean_volume(path, seg.start, seg.end)
        accomp_mean = measure_mean_volume(accomp_path, seg.start, seg.end)
        gap = vocal_mean - accomp_mean
        follow_trim = energy_follow_trim(accomp_mean, accomp_ref, gap)
        # Target-based gain: allow positive boost when vocal is below target,
        # but cap so post-gain peak stays within ceiling.
        target_raw = cfg.vocal.target_db - vocal_mean
        headroom = PEAK_CEILING_DB - peak
        target_gain = min(target_raw, headroom)
        gain = clamp(
            target_gain + follow_trim,
            cfg.vocal.gain_min_db,
            cfg.vocal.gain_max_db,
        )
        if follow_trim < -0.01 and gain < target_gain - 0.05:
            rule = "ENERGY_FOLLOW"
            note = "vocal follows low accompaniment energy"
        elif target_raw > 0.5:
            rule = "TARGET_BOOST"
            note = f"vocal lifted toward {cfg.vocal.target_db:.1f} dBFS target"
        elif gain < 0.0:
            rule = "PEAK_TRIM"
            note = "vocal peak protection"
        else:
            rule = "KEEP"
            note = "vocal near target level"
        out.append(SegmentDecision(seg.start, seg.end, gain, rule, note))
    return out


def decide_accompaniment_segments(
    path: str,
    vocal_path: str,
    duration: float,
    vocal_sections: list[tuple[float, float]],
    cfg: MixConfig,
) -> tuple[list[SegmentDecision], list[GainDecision]]:
    if not vocal_sections:
        return (
            [SegmentDecision(0.0, duration, min(0.0, cfg.accompaniment.base_gain_db), "BASE", "未检测到人声段落，伴奏不做正向补偿")],
            [],
        )

    decisions: list[SegmentDecision] = []
    body_decisions: list[GainDecision] = []
    section_segments = [Segment(start, end, True, 0.0) for start, end in vocal_sections]
    accomp_ref = energy_follow_reference(path, section_segments)
    pos = 0.0
    for idx, (start, end) in enumerate(vocal_sections):
        if start > pos:
            label = "前奏保持基础增益" if idx == 0 else "段落间隙保持基础增益"
            inter_peak = measure_peak_volume(path, pos, start)
            inter_mean = measure_mean_volume(path, pos, start)
            inter_follow = energy_follow_trim(inter_mean, accomp_ref)
            inter_gain = clamp(
                min(0.0, cfg.accompaniment.base_gain_db) + peak_trim_db(inter_peak) + inter_follow,
                min(cfg.accompaniment.gain_min_db, 0.0),
                min(cfg.accompaniment.gain_max_db, 0.0),
            )
            decisions.append(SegmentDecision(pos, start, inter_gain, "BASE", label))

        vocal_section_mean = measure_mean_volume(vocal_path, start, end)
        accomp_section_mean = measure_mean_volume(path, start, end)
        accomp_peak = measure_peak_volume(path, start, end)
        gap = vocal_section_mean - accomp_section_mean
        peak_trim = peak_trim_db(accomp_peak)
        buried_duck = 0.0
        if gap < BURIED_MEDIAN_LIMIT_DB:
            buried_duck = -(BURIED_MEDIAN_LIMIT_DB - gap)
        follow_trim = energy_follow_trim(accomp_section_mean, accomp_ref)
        total_gain = min(0.0, cfg.accompaniment.base_gain_db) + peak_trim + buried_duck + follow_trim
        section_gain = clamp(
            total_gain,
            min(cfg.accompaniment.gain_min_db, 0.0),
            min(cfg.accompaniment.gain_max_db, 0.0),
        )
        if follow_trim < -0.01:
            section_decision = GainDecision("ENERGY_FOLLOW", follow_trim, note="accompaniment low-energy section kept lower")
        elif buried_duck < 0.0:
            section_decision = GainDecision("RATIO_DUCK", buried_duck, note="人声被埋，只允许负向衰减伴奏")
        elif peak_trim < 0.0:
            section_decision = GainDecision("PEAK_TRIM", peak_trim, note="伴奏峰值超限，只做负向衰减")
        else:
            section_decision = GainDecision("KEEP", 0.0, note="比例和峰值安全，不做正向补偿")
        body_decisions.append(section_decision)
        decisions.append(SegmentDecision(start, end, section_gain, section_decision.rule_id, section_decision.note))
        pos = end

    if pos < duration:
        tail_peak = measure_peak_volume(path, pos, duration)
        tail_mean = measure_mean_volume(path, pos, duration)
        tail_follow = energy_follow_trim(tail_mean, accomp_ref)
        tail_gain = clamp(
            min(0.0, cfg.accompaniment.base_gain_db) + peak_trim_db(tail_peak) + tail_follow,
            min(cfg.accompaniment.gain_min_db, 0.0),
            min(cfg.accompaniment.gain_max_db, 0.0),
        )
        decisions.append(SegmentDecision(pos, duration, tail_gain, "BASE", "尾奏不做正向补偿"))
    return [d for d in decisions if d.end - d.start > 0.0], body_decisions


def build_volume_expr(segments: list[Segment]) -> str:
    """FFmpeg volume expression for piecewise gain automation, evaluated per frame.

    Generates a nested if() expression so the full audio passes through a single
    volume filter instead of being cut into segments and concatenated.  This
    avoids the fade-out/fade-in pair that atrim+concat places at every segment
    boundary, which was the cause of the choppy/intermittent artefacts.
    """
    if not segments:
        return "1.0"

    def db_to_lin(db_val: float) -> float:
        return 10.0 ** (db_val / 20.0)

    segs = sorted(segments, key=lambda s: s.start)

    if len(segs) == 1:
        return f"{db_to_lin(segs[0].gain_db):.6f}"

    # Build right-to-left: innermost expression is the last segment's gain
    expr = f"{db_to_lin(segs[-1].gain_db):.6f}"
    for seg in reversed(segs[:-1]):
        lin = db_to_lin(seg.gain_db)
        expr = f"if(lt(t,{seg.end:.6f}),{lin:.6f},{expr})"
    return expr


def build_segment_filter(
    segments: list[Segment],
    post_chain: list[str] | None = None,
    apply_all_gains: bool = False,
    fade_voice_segments: bool = True,
) -> str:
    parts: list[str] = []
    labels: list[str] = []
    fade_default = 0.035

    for idx, seg in enumerate(segments):
        label = f"s{idx}"
        labels.append(f"[{label}]")
        chain = [f"atrim=start={seg.start:.6f}:end={seg.end:.6f}", "asetpts=PTS-STARTPTS"]
        if apply_all_gains or seg.is_voice:
            chain.append(f"volume={seg.gain_db:.4f}dB")
        if fade_voice_segments and seg.is_voice:
            fade = min(fade_default, max(0.0, seg.duration / 4.0))
            if fade > 0.005 and seg.duration > fade * 2.2:
                chain.append(f"afade=t=in:st=0:d={fade:.4f}")
                chain.append(f"afade=t=out:st={seg.duration - fade:.4f}:d={fade:.4f}")
        elif seg.duration > BOUNDARY_FADE_SEC * 2.5:
            # Short boundary fade prevents clicks at hard-cut segment boundaries.
            chain.append(f"afade=t=in:st=0:d={BOUNDARY_FADE_SEC:.4f}")
            chain.append(f"afade=t=out:st={seg.duration - BOUNDARY_FADE_SEC:.4f}:d={BOUNDARY_FADE_SEC:.4f}")
        parts.append(f"[0:a]{','.join(chain)}[{label}]")

    post = ""
    if post_chain:
        post = "," + ",".join(post_chain)
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1{post}[out]")
    return ";".join(parts)


def render_vocal(
    input_path: str,
    output_path: str,
    segments: list[Segment],
    cfg: MixConfig,
) -> None:
    vol_expr = build_volume_expr(segments)
    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-i",
            input_path,
            "-af",
            f"volume='{vol_expr}':eval=frame,{limiter_filter(cfg.vocal.limiter_ceiling)}",
            "-ac",
            "1",
            output_path,
        ]
    )


def render_volume_only(input_path: str, output_path: str, gain_db: float) -> None:
    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-i",
            input_path,
            "-af",
            f"volume={gain_db:.4f}dB",
            output_path,
        ]
    )


def render_segmented_volume(
    input_path: str,
    output_path: str,
    segments: list[Segment],
    cfg: MixConfig,
) -> None:
    vol_expr = build_volume_expr(segments)
    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-i",
            input_path,
            "-af",
            f"volume='{vol_expr}':eval=frame,{limiter_filter(cfg.accompaniment.limiter_ceiling)}",
            output_path,
        ]
    )


def mix_tracks(vocal_path: str, accomp_path: str, output_path: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        mix_tmp = tmp.name
    try:
        run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-i",
                vocal_path,
                "-i",
                accomp_path,
                "-filter_complex",
                "[0:a]pan=stereo|c0=c0|c1=c0[v];[v][1:a]amix=inputs=2:dropout_transition=0[m]",
                "-map",
                "[m]",
                mix_tmp,
            ]
        )
        max_db = measure_max_volume(mix_tmp)
        headroom_gain = 0.0
        if max_db > -1.0:
            headroom_gain = -1.0 - max_db
        run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-i",
                mix_tmp,
                "-af",
                f"volume={headroom_gain:.4f}dB,alimiter=limit=0.9772:attack=3:release=25",
                output_path,
            ]
        )
    finally:
        if os.path.exists(mix_tmp):
            os.unlink(mix_tmp)


def process(
    vocal_in: str,
    accomp_in: str,
    out_mix: str,
    out_vocal: str,
    out_accomp: str,
    cfg: MixConfig,
    balance_report: str = "",
) -> None:
    duration = ffprobe_duration(vocal_in)
    silences = detect_silences(
        vocal_in,
        noise_db=cfg.detection.silence_noise_db,
        min_silence=cfg.detection.min_silence_sec,
    )
    voice = invert_silences(duration, silences)
    voice = merge_short_gaps(voice, max_gap=cfg.detection.merge_gap_sec)
    paragraphs = detect_paragraphs(duration, voice, cfg.detection)
    timeline = build_timeline(duration, voice)
    voiced = [s for s in timeline if s.is_voice and s.duration >= cfg.detection.min_voiced_sec]
    raw_balance = balance_stats(vocal_in, accomp_in, voiced)
    raw_peaks = {
        "vocal_peak_db": round(measure_max_volume(vocal_in), 3),
        "accomp_peak_db": round(measure_max_volume(accomp_in), 3),
    }

    vocal_decisions = decide_vocal_segments(vocal_in, accomp_in, timeline, cfg)
    vocal_map = {(d.start, d.end): d for d in vocal_decisions}
    for seg in timeline:
        decision = vocal_map.get((seg.start, seg.end))
        if decision is not None:
            seg.gain_db = decision.gain_db
    inherit_unvoiced_gains(timeline, allow_positive=False)
    limit_adjacent_gain_jumps(timeline, allow_positive=False)

    render_vocal(vocal_in, out_vocal, timeline, cfg)

    voiced_ranges = [(seg.start, seg.end) for seg in voiced]
    accomp_decisions, body_decision = decide_accompaniment_segments(
        accomp_in,
        vocal_in,
        duration,
        voiced_ranges,
        cfg,
    )
    accomp_timeline = [
        Segment(
            d.start,
            d.end,
            any(abs(d.start - start) < 0.001 and abs(d.end - end) < 0.001 for start, end in voiced_ranges),
            d.gain_db,
        )
        for d in accomp_decisions
    ]
    inherit_unvoiced_gains(accomp_timeline)
    limit_adjacent_gain_jumps(accomp_timeline)

    render_segmented_volume(accomp_in, out_accomp, accomp_timeline, cfg)
    balance_before = balance_stats(out_vocal, out_accomp, voiced)
    vocal_balance_trim_db = vocal_trim_for_balance(balance_before)
    accomp_balance_trim_db = 0.0
    balance_after_vocal_trim = balance_before
    balance_after = balance_before
    if vocal_balance_trim_db > 0.05:
        apply_in_place_gain(out_vocal, -vocal_balance_trim_db)
        balance_after_vocal_trim = balance_stats(out_vocal, out_accomp, voiced)
        balance_after = balance_after_vocal_trim
    accomp_balance_trim_db = accomp_trim_for_balance(balance_after)
    if accomp_balance_trim_db > 0.05:
        apply_in_place_gain(out_accomp, -accomp_balance_trim_db)
        balance_after = balance_stats(out_vocal, out_accomp, voiced)
    processed_peaks = {
        "vocal_peak_db": round(measure_max_volume(out_vocal), 3),
        "accomp_peak_db": round(measure_max_volume(out_accomp), 3),
    }
    vocal_timeline_gains = [seg.gain_db for seg in timeline]
    all_gains = vocal_timeline_gains + [d.gain_db for d in accomp_decisions]
    positive_gain_violation = any(gain > 0.0001 for gain in all_gains)

    if out_mix:
        mix_tracks(out_vocal, out_accomp, out_mix)

    if balance_report:
        Path(balance_report).parent.mkdir(parents=True, exist_ok=True)
        Path(balance_report).write_text(
            json.dumps(
                {
                    "vocal_path": out_vocal,
                    "accomp_path": out_accomp,
                    "raw": raw_balance,
                    "raw_peaks": raw_peaks,
                    "processed_peaks": processed_peaks,
                    "median_limit_db": BALANCE_MEDIAN_LIMIT_DB,
                    "p90_limit_db": BALANCE_P90_LIMIT_DB,
                    "buried_median_limit_db": BURIED_MEDIAN_LIMIT_DB,
                    "max_vocal_trim_db": BALANCE_MAX_VOCAL_TRIM_DB,
                    "max_accomp_trim_db": BALANCE_MAX_ACCOMP_TRIM_DB,
                    "energy_follow_start_db": ENERGY_FOLLOW_START_DB,
                    "energy_follow_strength": ENERGY_FOLLOW_STRENGTH,
                    "energy_follow_max_trim_db": ENERGY_FOLLOW_MAX_TRIM_DB,
                    "max_adjacent_gain_jump_db": MAX_ADJACENT_GAIN_JUMP_DB,
                    "positive_gain_violation": positive_gain_violation,
                    "max_decision_gain_db": round(max(all_gains) if all_gains else 0.0, 3),
                    "min_decision_gain_db": round(min(all_gains) if all_gains else 0.0, 3),
                    "vocal_balance_trim_db": round(vocal_balance_trim_db, 3),
                    "accomp_balance_trim_db": round(accomp_balance_trim_db, 3),
                    "vocal_timeline_gains": segment_gain_rows(timeline),
                    "vocal_gain_jumps": gain_jump_rows(timeline),
                    "accomp_timeline_gains": segment_gain_rows(accomp_timeline),
                    "accomp_gain_jumps": gain_jump_rows(accomp_timeline),
                    "before": balance_before,
                    "after_vocal_trim": balance_after_vocal_trim,
                    "after": balance_after,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print("Voice segments:", len(voiced))
    print("Vocal paragraphs:", len(paragraphs))
    if paragraphs:
        preview = ", ".join(f"{start:.2f}-{end:.2f}s" for start, end in paragraphs[:8])
        suffix = " ..." if len(paragraphs) > 8 else ""
        print(f"Paragraph ranges: {preview}{suffix}")
    if vocal_decisions:
        gains = [d.gain_db for d in vocal_decisions]
        print(f"Vocal gain range: {min(gains):.2f} dB to {max(gains):.2f} dB")
        rules = ", ".join(sorted({d.rule_id for d in vocal_decisions}))
        print(f"Vocal rules hit: {rules}")
    print(f"Vocal rule target: {cfg.vocal.target_db:.2f} dBFS (per-segment boost enabled, max +{cfg.vocal.gain_max_db:.1f} dB)")
    print(f"Vocal compressor ratio: {cfg.vocal.compressor.ratio:.2f}")
    print(f"Accompaniment base gain: {cfg.accompaniment.base_gain_db:.2f} dB")
    body_gains = [d.gain_db for d in accomp_decisions if d.rule_id != "BASE"]
    if body_gains:
        print(f"Accompaniment paragraph gain range: {min(body_gains):.2f} dB to {max(body_gains):.2f} dB")
    body_rules = ", ".join(sorted({d.rule_id for d in body_decision})) if body_decision else "BASE"
    print(f"Accompaniment paragraph rules hit: {body_rules}")
    print(f"Accompaniment compressor ratio: {cfg.accompaniment.compressor.ratio:.2f}")
    print(f"Lead-in/out accompaniment gain: {cfg.accompaniment.base_gain_db:.2f} dB")
    print(
        "Raw balance gap: "
        f"median {raw_balance['median_gap_db']:.2f} dB, "
        f"p90 {raw_balance['p90_gap_db']:.2f} dB, "
        f"max {raw_balance['max_gap_db']:.2f} dB"
    )
    print(
        "Balance gap before trim: "
        f"median {balance_before['median_gap_db']:.2f} dB, "
        f"p90 {balance_before['p90_gap_db']:.2f} dB, "
        f"max {balance_before['max_gap_db']:.2f} dB"
    )
    print(f"Vocal balance trim: -{vocal_balance_trim_db:.2f} dB")
    print(f"Accompaniment balance trim: -{accomp_balance_trim_db:.2f} dB")
    flagged_vocal_jumps = [row for row in gain_jump_rows(timeline) if row["flagged"]]
    flagged_accomp_jumps = [row for row in gain_jump_rows(accomp_timeline) if row["flagged"]]
    print(f"Flagged vocal gain jumps: {len(flagged_vocal_jumps)}")
    print(f"Flagged accompaniment gain jumps: {len(flagged_accomp_jumps)}")
    print(f"Positive gain violation: {positive_gain_violation}")
    print(
        "Balance gap after trim: "
        f"median {balance_after['median_gap_db']:.2f} dB, "
        f"p90 {balance_after['p90_gap_db']:.2f} dB, "
        f"max {balance_after['max_gap_db']:.2f} dB"
    )
    if balance_report:
        print(f"Balance report: {balance_report}")
    print(f"Vocal out: {out_vocal}")
    print(f"Accompaniment out: {out_accomp}")
    if out_mix:
        print(f"Mix out: {out_mix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatic vocal/accompaniment loudness and dynamics mixer")
    parser.add_argument("vocal_in")
    parser.add_argument("accomp_in")
    parser.add_argument("mix_out", nargs="?", default="")
    parser.add_argument("--vocal-out", default="/tmp/auto_vocal.wav")
    parser.add_argument("--accomp-out", default="/tmp/auto_accomp.wav")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--export-default-config", help="Write the default rule table JSON and exit")
    parser.add_argument("--vo-target-db", type=float, help="Override vocal target body RMS in dBFS")
    parser.add_argument("--vo-ratio", type=float, help="Override vocal compressor ratio")
    parser.add_argument("--bc-base-gain-db", type=float, help="Override accompaniment base gain in dB")
    parser.add_argument("--bc-gap-db", type=float, help="Override accompaniment gap below vocal in dB")
    parser.add_argument("--bc-ratio", type=float, help="Override accompaniment compressor ratio")
    parser.add_argument("--balance-report", default="", help="Write vocal/accompaniment balance audit JSON")
    args = parser.parse_args()

    if args.export_default_config:
        write_config(args.export_default_config, default_config())
        print(f"Default config written to: {args.export_default_config}")
        return 0

    cfg = load_config(args.config if args.config else None)
    if args.vo_target_db is not None:
        cfg.vocal.target_db = args.vo_target_db
    if args.vo_ratio is not None:
        cfg.vocal.compressor.ratio = args.vo_ratio
    if args.bc_base_gain_db is not None:
        cfg.accompaniment.base_gain_db = args.bc_base_gain_db
    if args.bc_gap_db is not None:
        cfg.accompaniment.body_gap_db = args.bc_gap_db
    if args.bc_ratio is not None:
        cfg.accompaniment.compressor.ratio = args.bc_ratio

    process(args.vocal_in, args.accomp_in, args.mix_out, args.vocal_out, args.accomp_out, cfg, args.balance_report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
