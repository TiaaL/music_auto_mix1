#!/usr/bin/env python3
"""按参考窗口做局部人声/伴奏比例保护。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from analyze_reference import load_audio_as_float, to_mono

def db(value: float) -> float:
    return 20.0 * np.log10(max(value, 1e-12))


def lin(db_value: np.ndarray | float) -> np.ndarray | float:
    return np.power(10.0, np.asarray(db_value) / 20.0)


def rms_db(samples: np.ndarray) -> float:
    if samples.size == 0:
        return -120.0
    return db(float(np.sqrt(np.mean(np.square(samples)))))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resample_ref(path: Path, target_sr: int) -> np.ndarray:
    audio, _ = load_audio_as_float(path, target_sr=target_sr)
    return audio


def source_paths(plan: dict[str, Any]) -> tuple[Path | None, Path | None]:
    """从 plan 里取参考人声/参考伴奏路径；缺失时本脚本直接透传。"""
    sources = (((plan.get("reference") or {}).get("features") or {}).get("sources") or {})
    vocal = sources.get("vocal")
    accomp = sources.get("accomp") or sources.get("provided_accomp")
    return (Path(vocal) if vocal else None, Path(accomp) if accomp else None)


def smooth_gain_db(gain: np.ndarray, sr: int, attack_ms: float = 120.0, release_ms: float = 300.0) -> np.ndarray:
    attack = np.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    release = np.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    out = np.empty_like(gain)
    prev = float(gain[0])
    for idx, target in enumerate(gain):
        coeff = attack if target > prev else release
        prev = coeff * prev + (1.0 - coeff) * float(target)
        out[idx] = prev
    return out


def process(
    vocal: np.ndarray,
    accomp: np.ndarray,
    sr: int,
    ref_vocal: np.ndarray,
    ref_accomp: np.ndarray,
    vocal_gain_db: float,
    accomp_gain_db: float,
    frame_sec: float,
    hop_sec: float,
    deadband_db: float,
    max_total_correction_db: float,
    min_reference_gap_db: float,
    vocal_share: float,
    max_vocal_gain_db: float,
    max_accomp_atten_db: float,
    target_gap_lift_db: float,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    n = min(vocal.shape[0], accomp.shape[0])
    vocal = vocal[:n] * float(lin(vocal_gain_db))
    accomp = accomp[:n] * float(lin(accomp_gain_db))
    ref_n = min(ref_vocal.shape[0], ref_accomp.shape[0], n)
    ref_vocal = ref_vocal[:ref_n]
    ref_accomp = ref_accomp[:ref_n]

    frame = max(int(round(frame_sec * sr)), 1024)
    hop = max(int(round(hop_sec * sr)), 256)
    starts = np.arange(0, max(1, n - frame + 1), hop)
    if starts.size == 0:
        starts = np.array([0])

    render_vocal_mono = to_mono(vocal)
    render_accomp_mono = to_mono(accomp)
    ref_vocal_mono = to_mono(ref_vocal)
    ref_accomp_mono = to_mono(ref_accomp)

    ref_frame_vocal_db = []
    for start in starts:
        end = min(start + frame, ref_n)
        if end <= start:
            ref_frame_vocal_db.append(-120.0)
        else:
            ref_frame_vocal_db.append(rms_db(ref_vocal_mono[start:end]))
    ref_frame_vocal_db_arr = np.array(ref_frame_vocal_db)
    active_threshold = max(float(np.percentile(ref_frame_vocal_db_arr, 65) - 14.0), -46.0)

    frame_times: list[float] = []
    vocal_gain_frames: list[float] = []
    accomp_gain_frames: list[float] = []
    events: list[dict[str, float]] = []
    for start in starts:
        # 只在人声活跃且参考窗口本身也可信时纠偏，避免静音/间奏被硬拉。
        end = min(start + frame, n)
        ref_end = min(start + frame, ref_n)
        center = (start + (end - start) * 0.5) / sr
        frame_times.append(center)
        if ref_end <= start or end <= start:
            vocal_gain_frames.append(0.0)
            accomp_gain_frames.append(0.0)
            continue
        ref_v_db = rms_db(ref_vocal_mono[start:ref_end])
        render_v_db = rms_db(render_vocal_mono[start:end])
        if ref_v_db < active_threshold or render_v_db < -48.0:
            vocal_gain_frames.append(0.0)
            accomp_gain_frames.append(0.0)
            continue
        render_gap = render_v_db - rms_db(render_accomp_mono[start:end])
        # 对不健康干声，局部参考窗口也同样向人声侧补偿一点；
        # 只改人声/伴奏比例，不改变任何频段音色。
        ref_gap = ref_v_db - rms_db(ref_accomp_mono[start:ref_end]) + target_gap_lift_db
        if ref_gap < min_reference_gap_db:
            vocal_gain_frames.append(0.0)
            accomp_gain_frames.append(0.0)
            continue
        deficit = ref_gap - render_gap
        if deficit <= deadband_db:
            vocal_gain_frames.append(0.0)
            accomp_gain_frames.append(0.0)
            continue

        correction = min(max_total_correction_db, max(0.0, deficit - deadband_db * 0.35))
        v_gain = min(max_vocal_gain_db, correction * vocal_share)
        a_gain = -min(max_accomp_atten_db, correction * (1.0 - vocal_share))
        vocal_gain_frames.append(v_gain)
        accomp_gain_frames.append(a_gain)
        events.append({
            "time_sec": round(center, 3),
            "render_gap_db": round(render_gap, 2),
            "reference_gap_db": round(ref_gap, 2),
            "deficit_db": round(deficit, 2),
            "vocal_gain_db": round(v_gain, 2),
            "accomp_gain_db": round(a_gain, 2),
        })

    sample_times = np.arange(n, dtype=np.float64) / sr
    vocal_curve = np.interp(sample_times, frame_times, vocal_gain_frames, left=0.0, right=0.0)
    accomp_curve = np.interp(sample_times, frame_times, accomp_gain_frames, left=0.0, right=0.0)
    vocal_curve = smooth_gain_db(vocal_curve, sr)
    accomp_curve = -smooth_gain_db(-accomp_curve, sr)

    out_vocal = vocal * lin(vocal_curve)[:, None]
    out_accomp = accomp * lin(accomp_curve)[:, None]
    report = {
        "enabled": True,
        "triggered": bool(events),
        "frame_sec": frame_sec,
        "hop_sec": hop_sec,
        "deadband_db": deadband_db,
        "max_total_correction_db": max_total_correction_db,
        "min_reference_gap_db": min_reference_gap_db,
        "base_vocal_gain_db": vocal_gain_db,
        "base_accomp_gain_db": accomp_gain_db,
        "target_gap_lift_db": round(target_gap_lift_db, 3),
        "peak_extra_vocal_gain_db": round(float(np.max(vocal_curve)) if vocal_curve.size else 0.0, 3),
        "peak_extra_accomp_gain_db": round(float(np.min(accomp_curve)) if accomp_curve.size else 0.0, 3),
        "events": events[:80],
        "event_count": len(events),
        "policy": (
            "local reference-window guard: fix buried active vocal windows mainly by attenuating accompaniment; "
            "default vocal lift is disabled so section repair cannot make the singer closer than the original"
        ),
    }
    return np.clip(out_vocal, -0.98, 0.98), np.clip(out_accomp, -0.98, 0.98), report


def main() -> None:
    parser = argparse.ArgumentParser(description="Protect weak active vocal sections against reference-window balance.")
    parser.add_argument("vocal_in", type=Path)
    parser.add_argument("accomp_in", type=Path)
    parser.add_argument("vocal_out", type=Path)
    parser.add_argument("accomp_out", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--vocal-gain-db", type=float, default=0.0)
    parser.add_argument("--accomp-gain-db", type=float, default=0.0)
    parser.add_argument("--frame-sec", type=float, default=4.0)
    parser.add_argument("--hop-sec", type=float, default=1.0)
    parser.add_argument("--deadband-db", type=float, default=1.15)
    # 局部副歌/强伴奏窗口只按参考 active 比例纠偏；默认只压伴奏、不推人声。
    # 弱人声由干声动态/伴奏让位处理，这里不能制造“所有人声比原曲靠前”。
    parser.add_argument("--max-total-correction-db", type=float, default=3.0)
    parser.add_argument("--min-reference-gap-db", type=float, default=-22.0)
    parser.add_argument("--vocal-share", type=float, default=0.0)
    parser.add_argument("--max-vocal-gain-db", type=float, default=0.0)
    parser.add_argument("--max-accomp-atten-db", type=float, default=1.6)
    args = parser.parse_args()

    plan = load_json(args.plan)
    ref_vocal_path, ref_accomp_path = source_paths(plan)
    if ref_vocal_path is None or ref_accomp_path is None or not ref_vocal_path.exists() or not ref_accomp_path.exists():
        # 没有参考曲时，section guard 不做局部参考窗口追踪，直接透传。
        # 全局比例仍由 compute_render_bus_balance 的通用目标处理。
        vocal, sr = sf.read(args.vocal_in, always_2d=True, dtype="float64")
        accomp, sr2 = sf.read(args.accomp_in, always_2d=True, dtype="float64")
        if sr2 != sr:
            raise SystemExit(f"sample-rate mismatch: vocal={sr}, accomp={sr2}")
        args.vocal_out.parent.mkdir(parents=True, exist_ok=True)
        sf.write(args.vocal_out, vocal, sr, subtype="PCM_16")
        sf.write(args.accomp_out, accomp, sr, subtype="PCM_16")
        report = {
            "enabled": False,
            "triggered": False,
            "reason": "missing reference vocal/accomp paths; copied inputs unchanged",
            "policy": "no-reference no-op; global generic bus balance handles coarse ratio",
        }
        if args.metadata:
            args.metadata.parent.mkdir(parents=True, exist_ok=True)
            args.metadata.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("[section-balance-guard] skipped: no reference vocal/accomp")
        return

    vocal, sr = sf.read(args.vocal_in, always_2d=True, dtype="float64")
    accomp, sr2 = sf.read(args.accomp_in, always_2d=True, dtype="float64")
    if sr2 != sr:
        raise SystemExit(f"sample-rate mismatch: vocal={sr}, accomp={sr2}")
    ref_vocal = resample_ref(ref_vocal_path, int(sr))
    ref_accomp = resample_ref(ref_accomp_path, int(sr))
    # 音量平衡默认回到 0.1：section guard 不再继承弱人声全局前推补偿。
    # 本脚本目前只作为手动排查工具保留，默认渲染链不会调用。
    target_gap_lift_db = 0.0
    lift_reasons = ["v0.1_bus_balance_mode:no_section_target_lift"]
    out_vocal, out_accomp, report = process(
        vocal,
        accomp,
        int(sr),
        ref_vocal,
        ref_accomp,
        vocal_gain_db=args.vocal_gain_db,
        accomp_gain_db=args.accomp_gain_db,
        frame_sec=args.frame_sec,
        hop_sec=args.hop_sec,
        deadband_db=args.deadband_db,
        max_total_correction_db=args.max_total_correction_db,
        min_reference_gap_db=args.min_reference_gap_db,
        vocal_share=args.vocal_share,
        max_vocal_gain_db=args.max_vocal_gain_db,
        max_accomp_atten_db=args.max_accomp_atten_db,
        target_gap_lift_db=target_gap_lift_db,
    )
    report["target_gap_lift_reasons"] = lift_reasons
    args.vocal_out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.vocal_out, out_vocal, sr, subtype="PCM_16")
    sf.write(args.accomp_out, out_accomp, sr, subtype="PCM_16")
    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "[section-balance-guard] "
        f"triggered={report['triggered']} events={report['event_count']} "
        f"peak_vocal={report['peak_extra_vocal_gain_db']} dB "
        f"peak_accomp={report['peak_extra_accomp_gain_db']} dB"
    )


if __name__ == "__main__":
    main()
