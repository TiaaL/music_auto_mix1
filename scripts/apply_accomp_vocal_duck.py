#!/usr/bin/env python3
"""Apply vocal-aware accompaniment ducking before the stereo sum."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy import signal

try:
    from numba import njit
except ImportError:  # pragma: no cover - optional speed path
    njit = None


TEMPLATE_PROFILES = {
    "template_a": {
        "low_base_db": 1.0,
        "low_extra_db": 1.3,
        "body_base_db": 0.25,
        "presence_base_db": 1.15,
        "presence_extra_db": 0.85,
        "air_base_db": 0.45,
    },
    "template_b": {
        "low_base_db": 0.7,
        "low_extra_db": 1.0,
        "body_base_db": 0.25,
        "presence_base_db": 1.1,
        "presence_extra_db": 0.8,
        "air_base_db": 0.45,
    },
    "template_c": {
        "low_base_db": 0.6,
        "low_extra_db": 0.8,
        "body_base_db": 0.2,
        "presence_base_db": 0.7,
        "presence_extra_db": 0.6,
        "air_base_db": 0.3,
    },
}


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(path, always_2d=True, dtype="float64")
    return audio, int(sr)


def mono(audio: np.ndarray) -> np.ndarray:
    return audio.mean(axis=1)


def rms_frames(samples: np.ndarray, sr: int, frame_ms: float = 40.0, hop_ms: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    frame = max(128, int(round(sr * frame_ms / 1000.0)))
    hop = max(32, int(round(sr * hop_ms / 1000.0)))
    if samples.size < frame:
        value = float(np.sqrt(np.mean(samples**2) + 1e-12))
        return np.array([0.0]), np.array([value])
    starts = np.arange(0, samples.size - frame + 1, hop)
    values = np.empty(starts.size, dtype=np.float64)
    window = np.hanning(frame)
    norm = np.mean(window**2)
    for idx, start in enumerate(starts):
        chunk = samples[start : start + frame] * window
        values[idx] = np.sqrt(np.mean(chunk**2) / max(norm, 1e-12) + 1e-12)
    times = (starts + frame * 0.5) / sr
    return times, values


def interpolate_frames(times: np.ndarray, values: np.ndarray, n: int, sr: int) -> np.ndarray:
    sample_times = np.arange(n, dtype=np.float64) / sr
    return np.interp(sample_times, times, values, left=values[0], right=values[-1])


def smooth_gain_db(gain_db: np.ndarray, sr: int, attack_ms: float = 35.0, release_ms: float = 180.0) -> np.ndarray:
    attack = np.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    release = np.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    if _smooth_gain_db_numba is not None:
        return _smooth_gain_db_numba(gain_db, float(attack), float(release))
    out = np.empty_like(gain_db)
    prev = float(gain_db[0])
    for idx, target in enumerate(gain_db):
        coeff = attack if target < prev else release
        prev = coeff * prev + (1.0 - coeff) * float(target)
        out[idx] = prev
    return out


if njit is not None:
    @njit(cache=True)
    def _smooth_gain_db_numba(gain_db: np.ndarray, attack: float, release: float) -> np.ndarray:
        out = np.empty_like(gain_db)
        prev = float(gain_db[0])
        for idx in range(gain_db.shape[0]):
            target = float(gain_db[idx])
            coeff = attack if target < prev else release
            prev = coeff * prev + (1.0 - coeff) * target
            out[idx] = prev
        return out
else:
    _smooth_gain_db_numba = None


def butter_filter(samples: np.ndarray, sr: int, kind: str, cutoff: float | tuple[float, float]) -> np.ndarray:
    nyq = sr * 0.5
    if isinstance(cutoff, tuple):
        wn: float | list[float] = [max(20.0, cutoff[0]) / nyq, min(nyq * 0.95, cutoff[1]) / nyq]
    else:
        wn = min(nyq * 0.95, max(20.0, cutoff)) / nyq
    sos = signal.butter(4, wn, btype=kind, output="sos")
    return signal.sosfiltfilt(sos, samples, axis=0)


def db(values: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(values, 1e-8))


def strength_from_vocal(vocal: np.ndarray, sr: int, n: int) -> np.ndarray:
    times, rms = rms_frames(vocal, sr)
    rms_db = db(rms)
    active_floor = max(float(np.percentile(rms_db, 65) - 12.0), -46.0)
    frame_strength = np.clip((rms_db - active_floor) / 16.0, 0.0, 1.0)
    return interpolate_frames(times, frame_strength, n, sr)


def pressure_curve(band: np.ndarray, sr: int, n: int) -> np.ndarray:
    times, rms = rms_frames(mono(band), sr)
    rms_db = db(rms)
    base = float(np.percentile(rms_db, 55))
    pressure = np.clip((rms_db - base) / 10.0, 0.0, 1.0)
    return interpolate_frames(times, pressure, n, sr)


def lin_gain(gain_db: np.ndarray) -> np.ndarray:
    return 10.0 ** (gain_db / 20.0)


VOCAL_LIFT_DEFICIT_DEAD_BAND_DB = 1.5
VOCAL_LIFT_DEFICIT_FULL_DB = 3.0
VOCAL_LIFT_DUCK_GAIN = {
    "low_extra_db": 1.0,
    "body_base_db": 0.45,
    "presence_base_db": 0.75,
    "air_base_db": 0.1,
}


def profile_from_plan(template: str, plan: dict[str, Any]) -> dict[str, float]:
    profile = dict(TEMPLATE_PROFILES.get(template, TEMPLATE_PROFILES["template_a"]))
    overrides = (plan.get("reference") or {}).get("overrides", {})
    accomp_eq = (overrides.get("source_eq") or {}).get("accomp_eq", {})
    needed_lift = float(accomp_eq.get("needed_relative_vocal_lift_db") or 0.0)
    deficit = max(0.0, needed_lift - VOCAL_LIFT_DEFICIT_DEAD_BAND_DB)
    scale = min(deficit / VOCAL_LIFT_DEFICIT_FULL_DB, 1.0)
    for band, gain in VOCAL_LIFT_DUCK_GAIN.items():
        profile[band] += gain * scale
    dry_strategy = overrides.get("dry_vocal_strategy") or {}
    dry_duck = dry_strategy.get("duck_profile") or {}
    profile["low_extra_db"] += float(dry_duck.get("low_extra_db") or 0.0)
    profile["body_base_db"] += float(dry_duck.get("body_extra_db") or 0.0)
    profile["presence_base_db"] += float(dry_duck.get("presence_extra_db") or 0.0)
    profile["air_base_db"] += float(dry_duck.get("air_extra_db") or 0.0)
    coordination = accomp_eq.get("duck_coordination") or {}
    regions = coordination.get("regions") or {}
    preserve_duck = coordination.get("preserve_dynamic_duck") or {}
    presence_carve = float(regions.get("presence") or 0.0)
    body_carve = float(regions.get("body") or 0.0)
    if presence_carve > 0.0:
        reduction = min(presence_carve / 2.6, 1.0)
        preserve = min(max(float(preserve_duck.get("presence") or 0.0), 0.0), 1.0)
        reduction *= 1.0 - 0.75 * preserve
        profile["presence_base_db"] *= 1.0 - 0.55 * reduction
        profile["presence_extra_db"] *= 1.0 - 0.70 * reduction
        profile["air_base_db"] *= 1.0 - 0.45 * reduction
    if body_carve > 0.0:
        reduction = min(body_carve / 2.6, 1.0)
        preserve = min(max(float(preserve_duck.get("body") or 0.0), 0.0), 1.0)
        reduction *= 1.0 - 0.60 * preserve
        profile["low_extra_db"] *= 1.0 - 0.35 * reduction
        profile["body_base_db"] *= 1.0 - 0.55 * reduction
    for key, value in list(profile.items()):
        profile[key] = round(max(0.0, float(value)), 4)
    return profile


def process(
    accomp: np.ndarray,
    vocal: np.ndarray,
    sr: int,
    template: str,
    plan: dict[str, Any],
    profile_timing: bool = False,
) -> tuple[np.ndarray, dict[str, Any]]:
    timings: dict[str, float] = {}

    def mark(label: str, start: float) -> float:
        if profile_timing:
            timings[label] = round(time.perf_counter() - start, 4)
        return time.perf_counter()

    section_start = time.perf_counter()
    n = min(accomp.shape[0], vocal.shape[0])
    accomp = accomp[:n]
    vocal = vocal[:n]
    vocal_mono = mono(vocal)
    profile = profile_from_plan(template, plan)
    section_start = mark("prepare", section_start)

    low = butter_filter(accomp, sr, "lowpass", 180.0)
    body = butter_filter(accomp, sr, "bandpass", (180.0, 1200.0))
    presence = butter_filter(accomp, sr, "bandpass", (1200.0, 5000.0))
    air = butter_filter(accomp, sr, "highpass", 5000.0)
    section_start = mark("split_bands_sosfiltfilt", section_start)

    vocal_strength = strength_from_vocal(vocal_mono, sr, n)
    low_pressure = pressure_curve(low, sr, n)
    presence_pressure = pressure_curve(presence + air, sr, n)
    section_start = mark("envelopes_and_pressure", section_start)

    low_gain_db = -vocal_strength * (profile["low_base_db"] + profile["low_extra_db"] * low_pressure)
    body_gain_db = -vocal_strength * profile["body_base_db"]
    presence_gain_db = -vocal_strength * (
        profile["presence_base_db"] + profile["presence_extra_db"] * presence_pressure
    )
    air_gain_db = -vocal_strength * profile["air_base_db"]

    low_gain_db = smooth_gain_db(low_gain_db, sr)
    body_gain_db = smooth_gain_db(body_gain_db, sr)
    presence_gain_db = smooth_gain_db(presence_gain_db, sr)
    air_gain_db = smooth_gain_db(air_gain_db, sr)
    section_start = mark("smooth_gain_curves", section_start)

    out = accomp.copy()
    np.multiply(low, (lin_gain(low_gain_db) - 1.0)[:, None], out=low)
    out += low
    np.multiply(body, (lin_gain(body_gain_db) - 1.0)[:, None], out=body)
    out += body
    np.multiply(presence, (lin_gain(presence_gain_db) - 1.0)[:, None], out=presence)
    out += presence
    np.multiply(air, (lin_gain(air_gain_db) - 1.0)[:, None], out=air)
    out += air
    out = np.clip(out, -0.98, 0.98)
    section_start = mark("apply_gains_and_clip", section_start)

    active = vocal_strength > 0.2
    report = {
        "enabled": True,
        "template": template,
        "profile": profile,
        "duck_coordination": (
            ((plan.get("reference") or {}).get("overrides", {}).get("source_eq", {}).get("accomp_eq", {}))
            .get("duck_coordination")
        ),
        "active_fraction": round(float(np.mean(active)), 4),
        "low_duck_db_active_p50": round(float(np.median(low_gain_db[active])) if np.any(active) else 0.0, 3),
        "low_duck_db_active_p90": round(float(np.percentile(low_gain_db[active], 10)) if np.any(active) else 0.0, 3),
        "presence_duck_db_active_p50": round(float(np.median(presence_gain_db[active])) if np.any(active) else 0.0, 3),
        "presence_duck_db_active_p90": round(float(np.percentile(presence_gain_db[active], 10)) if np.any(active) else 0.0, 3),
        "policy": "template-profiled multiband accompaniment ducking keyed from post-FX vocal activity",
    }
    if profile_timing:
        report["timings_sec"] = timings
    return out, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply vocal-aware dynamic accompaniment ducking.")
    parser.add_argument("accomp_in", type=Path)
    parser.add_argument("vocal_sidechain", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--template", default="template_a")
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--metadata", type=Path, default=None)
    parser.add_argument("--profile-timing", action="store_true", help="Record internal duck timing in metadata.")
    args = parser.parse_args()

    read_start = time.perf_counter()
    accomp, sr = read_audio(args.accomp_in)
    vocal, vocal_sr = read_audio(args.vocal_sidechain)
    read_elapsed = round(time.perf_counter() - read_start, 4)
    if vocal_sr != sr:
        raise SystemExit(f"sample-rate mismatch: accomp {sr}, vocal {vocal_sr}")

    plan = load_json(args.plan)
    out, report = process(accomp, vocal, sr, args.template, plan, profile_timing=args.profile_timing)
    if args.profile_timing:
        report.setdefault("timings_sec", {})["read_audio"] = read_elapsed

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    write_start = time.perf_counter()
    sf.write(args.output_wav, out, sr, subtype="PCM_16")
    if args.profile_timing:
        report.setdefault("timings_sec", {})["write_wav"] = round(time.perf_counter() - write_start, 4)
    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        "[accomp-duck] "
        f"template={args.template} low p50 {report['low_duck_db_active_p50']} dB, "
        f"presence p50 {report['presence_duck_db_active_p50']} dB"
    )


if __name__ == "__main__":
    main()
