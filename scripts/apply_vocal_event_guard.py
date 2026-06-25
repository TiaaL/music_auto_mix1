#!/usr/bin/env python3
"""处理人声里的短时事件，不负责整体 EQ 或响度。

它只做两件小事：
1. active 句子中极短的人声塌陷，参考原曲仍有人声时轻微补一点；
2. 句首 breath / 过渡气声被误当前景人声时，轻微压住，避免触发后面伴奏避让后变得很突兀。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from analyze_reference import load_audio_as_float, to_mono


ENABLE_MICRO_CONTINUITY_GUARD = False


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def db(value: float | np.ndarray) -> float | np.ndarray:
    return 20.0 * np.log10(np.maximum(value, 1e-10))


def lin(db_value: float | np.ndarray) -> np.ndarray:
    return np.power(10.0, np.asarray(db_value) / 20.0)


def source_reference_vocal(plan: dict[str, Any]) -> Path | None:
    sources = (((plan.get("reference") or {}).get("features") or {}).get("sources") or {})
    value = sources.get("vocal")
    return Path(value) if value else None


def rms_frames(samples: np.ndarray, sr: int, frame_ms: float, hop_ms: float) -> tuple[np.ndarray, np.ndarray, list[tuple[int, int]]]:
    frame = max(512, int(round(sr * frame_ms / 1000.0)))
    hop = max(128, int(round(sr * hop_ms / 1000.0)))
    if samples.size < frame:
        rms = math.sqrt(float(np.mean(samples**2)) + 1e-12)
        return np.array([0.0]), np.array([rms]), [(0, samples.size)]
    starts = np.arange(0, samples.size - frame + 1, hop)
    window = np.hanning(frame)
    norm = max(float(np.mean(window**2)), 1e-12)
    values = []
    spans = []
    for start in starts:
        end = int(start + frame)
        values.append(math.sqrt(float(np.mean((samples[start:end] * window) ** 2)) / norm + 1e-12))
        spans.append((int(start), end))
    times = (starts + frame * 0.5) / sr
    return times, np.asarray(values), spans


def high_ratio_db(samples: np.ndarray, sr: int) -> float:
    """粗略估计 breath 高频占比；只用于句首气声保护，不当作音色指标。"""
    if samples.size < 256:
        return -120.0
    x = samples - float(np.mean(samples))
    spectrum = np.abs(np.fft.rfft(x * np.hanning(x.size))) + 1e-12
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    high = float(np.mean(spectrum[(freqs >= 4200.0) & (freqs < min(12000.0, sr / 2.0))]))
    body = float(np.mean(spectrum[(freqs >= 700.0) & (freqs < 3600.0)]))
    return float(db(high / max(body, 1e-12)))


def smooth_curve(curve: np.ndarray, sr: int, attack_ms: float = 18.0, release_ms: float = 90.0) -> np.ndarray:
    attack = math.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    release = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    out = np.empty_like(curve)
    prev = float(curve[0])
    for idx, target in enumerate(curve):
        coeff = attack if target > prev else release
        prev = coeff * prev + (1.0 - coeff) * float(target)
        out[idx] = prev
    return out


def build_gain_curve(
    audio: np.ndarray,
    sr: int,
    ref_audio: np.ndarray | None,
) -> tuple[np.ndarray, dict[str, Any]]:
    x = to_mono(audio)
    times, rms, spans = rms_frames(x, sr, frame_ms=70.0, hop_ms=20.0)
    frame_db = np.asarray(db(rms), dtype=np.float64)
    if frame_db.size < 5:
        return np.zeros(x.size), {"enabled": False, "reason": "audio too short"}

    ref_db = None
    ref_active_floor = None
    if ref_audio is not None:
        ref_x = to_mono(ref_audio)[: x.size]
        _, ref_rms, _ = rms_frames(ref_x, sr, frame_ms=70.0, hop_ms=20.0)
        m = min(ref_rms.size, frame_db.size)
        ref_db = np.asarray(db(ref_rms[:m]), dtype=np.float64)
        ref_active_floor = max(-52.0, float(np.percentile(ref_db, 70)) - 16.0)

    active_floor = max(-56.0, float(np.percentile(frame_db, 75)) - 21.0)
    frame_gain = np.zeros_like(frame_db)
    continuity_candidates: list[tuple[float, int, float, float]] = []
    breath_candidates: list[tuple[float, int, float, float, float]] = []

    look = max(3, int(round(0.34 / 0.02)))
    for idx, value in enumerate(frame_db):
        left = frame_db[max(0, idx - look) : idx]
        right = frame_db[idx + 1 : min(frame_db.size, idx + look + 1)]
        if left.size >= 3 and right.size >= 3:
            neighbor = min(float(np.percentile(left, 65)), float(np.percentile(right, 65)))
            ref_ok = True
            if ref_db is not None and idx < ref_db.size and ref_active_floor is not None:
                ref_ok = bool(ref_db[idx] >= ref_active_floor)
            # 只救 active 句子内部的短塌陷；完全静音不硬拉，避免把噪声底抬出来。
            if (
                ref_ok
                and ref_db is not None
                and idx < ref_db.size
                and ref_active_floor is not None
                and ref_db[idx] >= ref_active_floor + 3.0
                and value >= active_floor
                and neighbor >= active_floor + 8.0
                and neighbor - value >= 13.0
            ):
                lift = min(1.4, max(0.0, (neighbor - value - 8.0) * 0.26))
                if lift >= 0.45:
                    continuity_candidates.append((neighbor - value, idx, lift, neighbor))

        future = frame_db[idx + 2 : min(frame_db.size, idx + int(round(0.50 / 0.02)))]
        past = frame_db[max(0, idx - int(round(0.24 / 0.02))) : idx]
        if future.size >= 5:
            future_voice = float(np.percentile(future, 75))
            past_quiet = float(np.percentile(past, 55)) if past.size >= 3 else value
            start_like = future_voice - value >= 10.0 and past_quiet <= value + 3.5
            if start_like and active_floor - 7.0 <= value <= future_voice - 6.0:
                s0, s1 = spans[idx]
                ratio = high_ratio_db(x[s0:s1], sr)
                if ratio >= -5.5:
                    cut = -min(2.2, max(0.0, (future_voice - value - 6.0) * 0.30))
                    if cut <= -0.6:
                        breath_candidates.append((future_voice - value, idx, cut, future_voice, ratio))

    continuity_events: list[dict[str, float]] = []
    if ENABLE_MICRO_CONTINUITY_GUARD:
        for _, idx, lift, neighbor in sorted(continuity_candidates, reverse=True)[:48]:
            frame_gain[idx] = max(frame_gain[idx], lift)
            continuity_events.append({
                "time_sec": round(float(times[idx]), 3),
                "frame_db": round(float(frame_db[idx]), 2),
                "neighbor_db": round(neighbor, 2),
                "gain_db": round(lift, 2),
            })

    breath_events: list[dict[str, float]] = []
    for _, idx, cut, future_voice, ratio in sorted(breath_candidates, reverse=True)[:48]:
        # breath 保护优先于短塌陷补偿，因为句首气声被抬起更容易刺耳。
        frame_gain[idx] = min(frame_gain[idx], cut)
        breath_events.append({
            "time_sec": round(float(times[idx]), 3),
            "frame_db": round(float(frame_db[idx]), 2),
            "future_voice_db": round(future_voice, 2),
            "high_ratio_db": round(ratio, 2),
            "gain_db": round(cut, 2),
        })

    sample_times = np.arange(x.size, dtype=np.float64) / sr
    gain_curve = np.interp(sample_times, times, frame_gain, left=0.0, right=0.0)
    gain_curve = smooth_curve(gain_curve, sr)
    gain_curve = np.clip(gain_curve, -2.2, 1.4)
    return gain_curve, {
        "enabled": bool(continuity_events or breath_events),
        "active_floor_db": round(active_floor, 3),
        "reference_active_floor_db": round(ref_active_floor, 3) if ref_active_floor is not None else None,
        "peak_lift_db": round(float(np.max(gain_curve)) if gain_curve.size else 0.0, 3),
        "peak_cut_db": round(float(np.min(gain_curve)) if gain_curve.size else 0.0, 3),
        "continuity_events": continuity_events[:80],
        "breath_events": breath_events[:80],
        "continuity_event_count": len(continuity_events),
        "breath_event_count": len(breath_events),
        "micro_continuity_guard_enabled": ENABLE_MICRO_CONTINUITY_GUARD,
        "policy": "短时事件保护：参考曲管 active 可信度，音量变化只在很小范围内修正。",
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply conservative vocal event guard.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    args = parser.parse_args()

    plan = load_json(args.plan)
    ref_path = source_reference_vocal(plan)
    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    if ref_path is None or not ref_path.exists():
        shutil.copyfile(args.input_wav, args.output_wav)
        report = {"enabled": False, "reason": "missing reference vocal path"}
        if args.metadata:
            write_json(args.metadata, report)
        print("[vocal-event-guard] skipped")
        return

    audio, sr = sf.read(args.input_wav, always_2d=True, dtype="float64")
    ref_audio, _ = load_audio_as_float(ref_path, target_sr=int(sr))
    gain_curve, report = build_gain_curve(audio, int(sr), ref_audio)
    out = audio * lin(gain_curve)[:, None]
    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 0.98:
        out *= 0.98 / peak
        report["safety_trim_db"] = round(20.0 * math.log10(0.98 / peak), 3)
    else:
        report["safety_trim_db"] = 0.0
    sf.write(args.output_wav, np.clip(out, -1.0, 1.0), int(sr), subtype="FLOAT")
    if args.metadata:
        write_json(args.metadata, report)
    print(
        "[vocal-event-guard] "
        f"continuity={report['continuity_event_count']} breath={report['breath_event_count']} "
        f"gain {report['peak_cut_db']}..{report['peak_lift_db']} dB"
    )


if __name__ == "__main__":
    main()
