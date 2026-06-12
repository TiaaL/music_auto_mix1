#!/usr/bin/env python3
"""Extract reference-track features used to drive per-song mix parameters.

Inputs:
  - reference full mix (used for LUFS, 8-band tonal balance, dynamics)
  - reference vocal stem
  - reference accompaniment (the same stem we mix with)

Output JSON keys:
  - loudness:         { lufs_i, true_peak_db, lra }
  - tonal_balance:    { sub..air dB per band, normalised so mid=0 }
  - dynamics:         { crest_db, dr_db }
  - vocal_accomp_balance: { vocal_lufs, accomp_lufs, vocal_minus_accomp_db }
  - reverb_proxy:     { tail_to_onset_ratio_db, est_rt60_ms }  (diagnostic only in v1)
  - sources:          paths actually used
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np


ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_ROOT = ROOT / "downloads" / "feishu_long_audio_screened"
REFERENCE_ROOT_CANDIDATES = (
    DOWNLOADS_ROOT,
    ROOT.parent / "feishu_long_audio_screened",
    ROOT.parent.parent / "feishu_long_audio_screened",
)

BANDS = [
    ("sub", 20.0, 80.0),
    ("low", 80.0, 180.0),
    ("lowmid", 180.0, 500.0),
    ("mid", 500.0, 1000.0),
    ("upper", 1000.0, 4000.0),
    ("harsh", 4000.0, 8000.0),
    ("sib", 8000.0, 12000.0),
    ("air", 12000.0, 20000.0),
]


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


def db(value: float, floor: float = -120.0) -> float:
    if value <= 0 or not math.isfinite(value):
        return floor
    return 20.0 * math.log10(value)


def load_audio_as_float(path: Path, target_sr: int = 48000) -> tuple[np.ndarray, int]:
    """Decode any format through ffmpeg into a float32 numpy array at target_sr."""
    proc = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-f",
            "f32le",
            "-acodec",
            "pcm_f32le",
            "-ar",
            str(target_sr),
            "-ac",
            "2",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg decode failed for {path}: {proc.stderr.decode('utf-8', errors='replace')[-500:]}"
        )
    raw = np.frombuffer(proc.stdout, dtype=np.float32)
    if raw.size == 0:
        raise RuntimeError(f"empty audio after decode: {path}")
    data = raw.reshape(-1, 2).astype(np.float64)
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    return data, target_sr


def to_mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        return data
    return data.mean(axis=1)


def measure_loudness(path: Path) -> dict[str, float]:
    """Run ffmpeg loudnorm in measurement mode to get LUFS-I, true peak, LRA."""
    proc = run(
        [
            FFMPEG,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-23.0:TP=-2.0:LRA=11.0:print_format=json",
            "-f",
            "null",
            "-",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg loudnorm failed for {path}: {proc.stderr[-500:]}")
    match = re.search(r"\{[\s\S]*?\}", proc.stderr)
    if not match:
        raise RuntimeError(f"loudnorm JSON not found in output for {path}")
    raw = json.loads(match.group(0))
    return {
        "lufs_i": float(raw["input_i"]),
        "true_peak_db": float(raw["input_tp"]),
        "lra": float(raw["input_lra"]),
    }


def tonal_balance(data: np.ndarray, sr: int) -> dict[str, float]:
    """8-band energy profile in dB, normalised so the 'mid' band = 0 dB."""
    profile = band_profile(data, sr, normalize_mid=True)
    return {name: round(value, 3) for name, value in profile.items()}


def band_profile(data: np.ndarray, sr: int, normalize_mid: bool) -> dict[str, float]:
    """8-band FFT magnitude profile. Optionally normalise so 'mid' = 0 dB."""
    x = to_mono(data)
    max_samples = min(x.size, sr * 90)
    x = x[:max_samples]
    if x.size < 16:
        return {name: 0.0 for name, _, _ in BANDS}
    x = x - float(np.mean(x))
    window = np.hanning(x.size)
    spectrum = np.abs(np.fft.rfft(x * window)) + 1e-12
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    profile: dict[str, float] = {}
    for name, low, high in BANDS:
        mask = (freqs >= low) & (freqs < min(high, sr / 2.0))
        profile[name] = db(float(np.mean(spectrum[mask]))) if np.any(mask) else -120.0
    if normalize_mid:
        mid_ref = profile.get("mid", 0.0)
        return {name: value - mid_ref for name, value in profile.items()}
    return profile


def intervals_to_rows(intervals: list[tuple[float, float]], limit: int = 60) -> list[dict[str, float]]:
    return [
        {"start": round(start, 3), "end": round(end, 3), "duration": round(end - start, 3)}
        for start, end in intervals[:limit]
    ]


def active_intervals_from_vocal(
    vocal_data: np.ndarray,
    sr: int,
    frame_sec: float = 0.050,
    hop_sec: float = 0.025,
    threshold_below_peak_db: float = 34.0,
    noise_floor_db: float = -58.0,
    merge_gap_sec: float = 0.18,
    min_active_sec: float = 0.08,
    pad_sec: float = 0.04,
) -> list[tuple[float, float]]:
    """Find sung/voiced regions from a vocal stem for reference-aware measurements."""
    x = to_mono(vocal_data)
    if x.size == 0:
        return []
    frame = max(128, int(frame_sec * sr))
    hop = max(64, int(hop_sec * sr))
    if x.size < frame:
        return [(0.0, x.size / sr)]

    starts = np.arange(0, x.size - frame + 1, hop)
    rms = np.array([np.sqrt(np.mean(np.square(x[start : start + frame]))) for start in starts])
    rms_db = np.array([db(float(value)) for value in rms])
    peak_ref = float(np.percentile(rms_db, 95)) if rms_db.size else noise_floor_db
    threshold = max(noise_floor_db, peak_ref - threshold_below_peak_db)
    flags = rms_db >= threshold
    if not np.any(flags):
        return [(0.0, x.size / sr)]

    raw: list[tuple[float, float]] = []
    active_start: int | None = None
    for index, flag in enumerate(flags):
        if flag and active_start is None:
            active_start = index
        elif not flag and active_start is not None:
            start_sec = starts[active_start] / sr
            end_sec = (starts[index - 1] + frame) / sr
            raw.append((start_sec, end_sec))
            active_start = None
    if active_start is not None:
        raw.append((starts[active_start] / sr, (starts[-1] + frame) / sr))

    duration = x.size / sr
    padded = [
        (max(0.0, start - pad_sec), min(duration, end + pad_sec))
        for start, end in raw
        if end - start >= min_active_sec
    ]
    if not padded:
        return [(0.0, duration)]

    merged: list[list[float]] = [[padded[0][0], padded[0][1]]]
    for start, end in padded[1:]:
        previous = merged[-1]
        if start - previous[1] <= merge_gap_sec:
            previous[1] = max(previous[1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged if end - start >= min_active_sec]


def collect_interval_audio(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
    max_seconds: float = 90.0,
) -> np.ndarray:
    if not intervals:
        return data[: int(max_seconds * sr)]
    chunks: list[np.ndarray] = []
    total = 0
    max_samples = int(max_seconds * sr)
    for start, end in intervals:
        s0 = max(0, int(start * sr))
        s1 = min(data.shape[0], int(end * sr))
        if s1 <= s0:
            continue
        chunk = data[s0:s1]
        remaining = max_samples - total
        if remaining <= 0:
            break
        if chunk.shape[0] > remaining:
            chunk = chunk[:remaining]
        chunks.append(chunk)
        total += chunk.shape[0]
    if not chunks:
        return data[: int(max_seconds * sr)]
    return np.concatenate(chunks, axis=0)


def rms_db_for_intervals(data: np.ndarray, sr: int, intervals: list[tuple[float, float]]) -> float:
    active = collect_interval_audio(data, sr, intervals, max_seconds=180.0)
    x = to_mono(active)
    if x.size == 0:
        return -120.0
    return round(db(float(np.sqrt(np.mean(np.square(x))))), 3)


def tonal_balance_for_intervals(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
) -> dict[str, float]:
    active = collect_interval_audio(data, sr, intervals)
    return tonal_balance(active, sr)


def band_levels_for_intervals(
    data: np.ndarray,
    sr: int,
    intervals: list[tuple[float, float]],
) -> dict[str, float]:
    active = collect_interval_audio(data, sr, intervals)
    return {name: round(value, 3) for name, value in band_profile(active, sr, normalize_mid=False).items()}


def dynamics(data: np.ndarray) -> dict[str, float]:
    x = to_mono(data)
    peak = float(np.max(np.abs(x))) if x.size else 0.0
    rms = float(np.sqrt(np.mean(np.square(x)))) if x.size else 0.0
    crest = db(peak) - db(rms)
    frame_size = 4096
    hop = 2048
    if x.size >= frame_size:
        starts = np.arange(0, x.size - frame_size + 1, hop)
        frames = np.array([np.sqrt(np.mean(np.square(x[s : s + frame_size]))) for s in starts])
        active = frames[frames > 1e-6]
        if active.size:
            dr = db(float(np.percentile(active, 95))) - db(float(np.percentile(active, 10)))
        else:
            dr = 0.0
    else:
        dr = 0.0
    return {"crest_db": round(crest, 3), "dr_db": round(dr, 3)}


def reverb_proxy(data: np.ndarray, sr: int) -> dict[str, float]:
    """Crude wet/dry proxy: energy 150-400 ms after each transient onset vs onset peak."""
    x = to_mono(data)
    if x.size < sr:
        return {"tail_to_onset_ratio_db": -60.0, "est_rt60_ms": 0.0}
    frame = 1024
    hop = 512
    starts = np.arange(0, x.size - frame + 1, hop)
    env = np.array([np.sqrt(np.mean(np.square(x[s : s + frame]))) for s in starts])
    if env.size < 8:
        return {"tail_to_onset_ratio_db": -60.0, "est_rt60_ms": 0.0}
    diff = np.diff(env)
    threshold = float(np.percentile(diff, 95))
    onset_frames = np.where(diff > max(threshold, 1e-5))[0]
    if onset_frames.size == 0:
        return {"tail_to_onset_ratio_db": -60.0, "est_rt60_ms": 0.0}
    frames_per_ms = sr / 1000.0 / hop
    tail_start = int(150 * frames_per_ms)
    tail_end = int(400 * frames_per_ms)
    ratios = []
    decays = []
    for onset in onset_frames:
        peak_idx = onset + 1
        if peak_idx >= env.size:
            continue
        peak_val = env[peak_idx]
        if peak_val < 1e-4:
            continue
        tail_slice = env[peak_idx + tail_start : peak_idx + tail_end]
        if tail_slice.size == 0:
            continue
        tail_val = float(np.mean(tail_slice))
        ratios.append(db(tail_val) - db(float(peak_val)))
        post = env[peak_idx : peak_idx + int(2000 * frames_per_ms)]
        if post.size > 4:
            post_db = np.array([db(v) for v in post])
            try:
                slope, _ = np.polyfit(np.arange(post.size), post_db, 1)
                if slope < -1e-6:
                    decays.append(-60.0 / slope / frames_per_ms)
            except (np.linalg.LinAlgError, ValueError):
                pass
    if not ratios:
        return {"tail_to_onset_ratio_db": -60.0, "est_rt60_ms": 0.0}
    ratio_med = float(np.median(ratios))
    rt60_med = float(np.median(decays)) if decays else 0.0
    return {
        "tail_to_onset_ratio_db": round(ratio_med, 2),
        "est_rt60_ms": round(rt60_med, 1),
    }


def lufs_only(path: Path) -> float:
    return measure_loudness(path)["lufs_i"]


def normalize_song_token(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.replace(" ", "").replace("　", "")
    text = text.lower()
    return text


def extract_song_name(vocal_path: Path) -> str:
    """`hjf中文歌曲-黄昏_干声.wav` -> `黄昏`."""
    stem = vocal_path.stem
    for suffix in ("_干声", "_vocal", "-干声", "-vocal"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    if "-" in stem:
        stem = stem.rsplit("-", 1)[-1]
    return stem.strip()


def fuzzy_find(folder: Path, song: str, extensions: tuple[str, ...]) -> Path | None:
    if not folder.exists():
        return None
    needle = normalize_song_token(song)
    if not needle:
        return None
    candidates: list[Path] = []
    for ext in extensions:
        candidates.extend(folder.glob(f"*{ext}"))
    for path in candidates:
        if needle in normalize_song_token(path.stem):
            return path
    return None


def resolve_downloads_root(downloads_root: Path | None = None) -> Path:
    if downloads_root is not None:
        return downloads_root
    for candidate in REFERENCE_ROOT_CANDIDATES:
        if candidate.exists():
            return candidate
    return DOWNLOADS_ROOT


def resolve_accomp_file(
    folder: Path,
    song: str,
    extensions: tuple[str, ...],
    accomp_input: Path | None = None,
) -> Path | None:
    if accomp_input is not None and accomp_input.exists():
        try:
            accomp_input.resolve().relative_to(folder.resolve())
            return accomp_input
        except ValueError:
            same_name = folder / accomp_input.name
            if same_name.exists():
                return same_name
    return fuzzy_find(folder, song, extensions)


def resolve_reference_files(
    vocal_input: Path,
    downloads_root: Path | None = None,
    accomp_input: Path | None = None,
) -> dict[str, Path | None]:
    downloads_root = resolve_downloads_root(downloads_root)
    song = extract_song_name(vocal_input)
    return {
        "song": song,
        "full_mix": fuzzy_find(downloads_root / "原曲", song, (".mp3", ".wav", ".flac", ".m4a")),
        "vocal": fuzzy_find(downloads_root / "原曲人声", song, (".wav", ".mp3", ".flac")),
        "accomp": resolve_accomp_file(
            downloads_root / "伴奏",
            song,
            (".wav", ".mp3", ".flac"),
            accomp_input=accomp_input,
        ),
    }


def analyse(full_mix: Path, vocal: Path, accomp: Path) -> dict[str, Any]:
    full_audio, full_sr = load_audio_as_float(full_mix)
    vocal_audio, _ = load_audio_as_float(vocal)
    accomp_audio, _ = load_audio_as_float(accomp)
    active_regions = active_intervals_from_vocal(vocal_audio, full_sr)

    loudness = measure_loudness(full_mix)
    vocal_lufs = lufs_only(vocal)
    accomp_lufs = lufs_only(accomp)
    vocal_active_rms = rms_db_for_intervals(vocal_audio, full_sr, active_regions)
    accomp_active_rms = rms_db_for_intervals(accomp_audio, full_sr, active_regions)

    return {
        "sources": {
            "full_mix": str(full_mix),
            "vocal": str(vocal),
            "accomp": str(accomp),
        },
        "loudness": loudness,
        "tonal_balance": tonal_balance(full_audio, full_sr),
        "vocal_tonal_balance": tonal_balance_for_intervals(vocal_audio, full_sr, active_regions),
        "accomp_tonal_balance": tonal_balance_for_intervals(accomp_audio, full_sr, active_regions),
        "active_band_levels": {
            "vocal": band_levels_for_intervals(vocal_audio, full_sr, active_regions),
            "accomp": band_levels_for_intervals(accomp_audio, full_sr, active_regions),
        },
        "dynamics": dynamics(full_audio),
        "vocal_accomp_balance": {
            "vocal_lufs": round(vocal_lufs, 2),
            "accomp_lufs": round(accomp_lufs, 2),
            "vocal_minus_accomp_db": round(vocal_lufs - accomp_lufs, 2),
            "active_vocal_rms_db": vocal_active_rms,
            "active_accomp_rms_db": accomp_active_rms,
            "active_vocal_minus_accomp_db": round(vocal_active_rms - accomp_active_rms, 2),
            "basis": "reference_vocal_active_regions",
        },
        "active_vocal_regions": {
            "count": len(active_regions),
            "coverage_sec": round(sum(end - start for start, end in active_regions), 3),
            "regions": intervals_to_rows(active_regions),
        },
        "reverb_proxy": reverb_proxy(vocal_audio, full_sr),
    }


def analyse_input_pair(vocal: Path, accomp: Path) -> dict[str, Any]:
    """Predict the input-mix tonal/dynamics by summing pre-render vocal + accomp."""
    vocal_audio, sr = load_audio_as_float(vocal)
    accomp_audio, _ = load_audio_as_float(accomp)
    active_regions = active_intervals_from_vocal(vocal_audio, sr)
    n = min(vocal_audio.shape[0], accomp_audio.shape[0])
    summed = vocal_audio[:n] + accomp_audio[:n]
    vocal_lufs = lufs_only(vocal)
    accomp_lufs = lufs_only(accomp)
    vocal_active_rms = rms_db_for_intervals(vocal_audio, sr, active_regions)
    accomp_active_rms = rms_db_for_intervals(accomp_audio, sr, active_regions)
    return {
        "sources": {"vocal": str(vocal), "accomp": str(accomp)},
        "tonal_balance": tonal_balance(summed, sr),
        "vocal_tonal_balance": tonal_balance_for_intervals(vocal_audio, sr, active_regions),
        "accomp_tonal_balance": tonal_balance_for_intervals(accomp_audio, sr, active_regions),
        "active_band_levels": {
            "vocal": band_levels_for_intervals(vocal_audio, sr, active_regions),
            "accomp": band_levels_for_intervals(accomp_audio, sr, active_regions),
        },
        "dynamics": dynamics(summed),
        "vocal_accomp_balance": {
            "vocal_lufs": round(vocal_lufs, 2),
            "accomp_lufs": round(accomp_lufs, 2),
            "vocal_minus_accomp_db": round(vocal_lufs - accomp_lufs, 2),
            "active_vocal_rms_db": vocal_active_rms,
            "active_accomp_rms_db": accomp_active_rms,
            "active_vocal_minus_accomp_db": round(vocal_active_rms - accomp_active_rms, 2),
            "basis": "input_vocal_active_regions",
        },
        "active_vocal_regions": {
            "count": len(active_regions),
            "coverage_sec": round(sum(end - start for start, end in active_regions), 3),
            "regions": intervals_to_rows(active_regions),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract reference-track features for the mix planner.")
    parser.add_argument("--vocal-input", type=Path, default=None,
                        help="Vocal-to-be-mixed; used to auto-resolve reference files by song name.")
    parser.add_argument("--full-mix", type=Path, default=None)
    parser.add_argument("--ref-vocal", type=Path, default=None)
    parser.add_argument("--ref-accomp", type=Path, default=None)
    parser.add_argument("--downloads-root", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    if args.vocal_input is not None and (args.full_mix is None or args.ref_vocal is None or args.ref_accomp is None):
        resolved = resolve_reference_files(args.vocal_input.resolve(), args.downloads_root)
        args.full_mix = args.full_mix or resolved["full_mix"]
        args.ref_vocal = args.ref_vocal or resolved["vocal"]
        args.ref_accomp = args.ref_accomp or resolved["accomp"]

    missing = [name for name, value in (("full-mix", args.full_mix), ("ref-vocal", args.ref_vocal), ("ref-accomp", args.ref_accomp)) if value is None]
    if missing:
        raise SystemExit(f"Could not resolve reference inputs: {', '.join(missing)}")

    features = analyse(Path(args.full_mix), Path(args.ref_vocal), Path(args.ref_accomp))
    out_text = json.dumps(features, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out_text, encoding="utf-8")
    print(out_text)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
