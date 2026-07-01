#!/usr/bin/env python3
"""应用最终融合 pass。

这一步统一处理人声/伴奏融合：动态让位、全局 active gap、参考窗口局部比例、
以及人声宽度轻收。它替代渲染链里分散的 duck + bus balance + section guard。
核心目标始终是“每首歌对齐自己的原曲”，不是按歌名或风格套参数。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf
from scipy import signal

from analyze_reference import load_audio_as_float, to_mono


def load_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def db(value: float | np.ndarray) -> float | np.ndarray:
    return 20.0 * np.log10(np.maximum(value, 1e-10))


def lin(value_db: float | np.ndarray) -> float | np.ndarray:
    return np.power(10.0, np.asarray(value_db) / 20.0)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    audio, sr = sf.read(path, always_2d=True, dtype="float64")
    return audio, int(sr)


def rms(samples: np.ndarray) -> float:
    if samples.size == 0:
        return 1e-10
    return float(np.sqrt(np.mean(np.square(samples)) + 1e-12))


def rms_db(samples: np.ndarray) -> float:
    return float(db(rms(samples)))


def reference_sources(plan: dict[str, Any]) -> tuple[Path | None, Path | None]:
    sources = (((plan.get("reference") or {}).get("features") or {}).get("sources") or {})
    vocal = sources.get("vocal")
    accomp = sources.get("accomp") or sources.get("provided_accomp")
    return (Path(vocal) if vocal else None, Path(accomp) if accomp else None)


def reference_gap(plan: dict[str, Any]) -> float | None:
    balance = (((plan.get("reference") or {}).get("features") or {}).get("vocal_accomp_balance") or {})
    value = balance.get("active_vocal_minus_accomp_db")
    if value is None:
        value = balance.get("vocal_minus_accomp_db")
    return float(value) if isinstance(value, (int, float)) else None


def reference_spatial(plan: dict[str, Any]) -> dict[str, Any]:
    return (((plan.get("reference") or {}).get("features") or {}).get("vocal_spatial_profile") or {})


def rms_frames(samples: np.ndarray, sr: int, frame_ms: float = 40.0, hop_ms: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    frame = max(128, int(round(sr * frame_ms / 1000.0)))
    hop = max(32, int(round(sr * hop_ms / 1000.0)))
    if samples.size < frame:
        return np.array([0.0]), np.array([rms(samples)])
    starts = np.arange(0, samples.size - frame + 1, hop)
    window = np.hanning(frame)
    norm = max(float(np.mean(window**2)), 1e-12)
    values = np.empty(starts.size, dtype=np.float64)
    for idx, start in enumerate(starts):
        chunk = samples[start : start + frame] * window
        values[idx] = math.sqrt(float(np.mean(chunk**2)) / norm + 1e-12)
    times = (starts + frame * 0.5) / sr
    return times, values


def interp_samples(times: np.ndarray, values: np.ndarray, n: int, sr: int) -> np.ndarray:
    sample_times = np.arange(n, dtype=np.float64) / sr
    return np.interp(sample_times, times, values, left=float(values[0]), right=float(values[-1]))


def smooth_gain_db(curve: np.ndarray, sr: int, attack_ms: float, release_ms: float) -> np.ndarray:
    attack = math.exp(-1.0 / max(1.0, attack_ms * 0.001 * sr))
    release = math.exp(-1.0 / max(1.0, release_ms * 0.001 * sr))
    out = np.empty_like(curve)
    prev = float(curve[0])
    for idx, target in enumerate(curve):
        coeff = attack if abs(float(target)) > abs(prev) else release
        prev = coeff * prev + (1.0 - coeff) * float(target)
        out[idx] = prev
    return out


def butter_filter(samples: np.ndarray, sr: int, kind: str, cutoff: float | tuple[float, float]) -> np.ndarray:
    nyq = sr * 0.5
    if isinstance(cutoff, tuple):
        wn: float | list[float] = [max(20.0, cutoff[0]) / nyq, min(nyq * 0.95, cutoff[1]) / nyq]
    else:
        wn = min(nyq * 0.95, max(20.0, cutoff)) / nyq
    sos = signal.butter(4, wn, btype=kind, output="sos")
    return signal.sosfiltfilt(sos, samples, axis=0)


def active_regions_from_vocal(vocal: np.ndarray, sr: int) -> list[tuple[float, float]]:
    x = to_mono(vocal)
    times, frame_rms = rms_frames(x, sr, frame_ms=80.0, hop_ms=40.0)
    frame_db = np.asarray(db(frame_rms), dtype=np.float64)
    if frame_db.size == 0:
        return [(0.0, x.size / sr)]
    threshold = max(float(np.percentile(frame_db, 70) - 18.0), -52.0)
    active = frame_db >= threshold
    regions: list[tuple[float, float]] = []
    start: float | None = None
    hop_sec = 0.04
    for idx, flag in enumerate(active):
        t = float(times[idx])
        if flag and start is None:
            start = max(0.0, t - hop_sec)
        elif not flag and start is not None:
            end = min(x.size / sr, t + hop_sec)
            if end - start >= 0.20:
                regions.append((start, end))
            start = None
    if start is not None:
        end = x.size / sr
        if end - start >= 0.20:
            regions.append((start, end))
    return regions or [(0.0, x.size / sr)]


def rms_for_regions(samples: np.ndarray, sr: int, regions: list[tuple[float, float]]) -> float:
    chunks = []
    for start, end in regions:
        s = max(0, int(round(start * sr)))
        e = min(samples.size, int(round(end * sr)))
        if e > s:
            chunks.append(samples[s:e])
    if not chunks:
        return rms_db(samples)
    return rms_db(np.concatenate(chunks))


def dry_strategy(plan: dict[str, Any]) -> dict[str, Any]:
    source_cleanup = plan.get("source_cleanup") or {}
    if source_cleanup.get("dry_vocal_strategy"):
        return source_cleanup.get("dry_vocal_strategy") or {}
    return (((plan.get("reference") or {}).get("overrides") or {}).get("dry_vocal_strategy") or {})


def duck_budgets(plan: dict[str, Any]) -> dict[str, Any]:
    """把干声遮挡请求转成最终融合预算。

    这里不再照搬模板 duck profile，而是把它压成最终 pass 的预算上限；
    避免前面/后面多层重复挖伴奏。
    """
    dry = dry_strategy(plan)
    profile = dry.get("duck_profile") or {}
    presence_extra = float(profile.get("presence_extra_db") or 0.0)
    body_extra = float(profile.get("body_extra_db") or 0.0)
    low_extra = float(profile.get("low_extra_db") or 0.0)
    air_extra = float(profile.get("air_extra_db") or 0.0)
    spatial = reference_spatial(plan)
    near_mono = bool(spatial.get("near_mono_center_led"))
    return {
        "low": round(clamp(0.45 + low_extra * 0.55, 0.35, 0.95), 3),
        "body": round(clamp(0.35 + body_extra * 0.70, 0.25, 0.85), 3),
        "presence": round(clamp(0.65 + presence_extra * 0.35, 0.45, 1.15), 3),
        "air": round(clamp(0.20 + air_extra * 0.35, 0.15, 0.45 if near_mono else 0.60), 3),
        "policy": "final fusion 内部统一预算；干声策略只提供遮挡线索，不直接决定最终融合。",
    }


def vocal_strength_curve(vocal: np.ndarray, sr: int, n: int) -> np.ndarray:
    times, values = rms_frames(to_mono(vocal), sr)
    values_db = np.asarray(db(values), dtype=np.float64)
    floor = max(float(np.percentile(values_db, 65) - 12.0), -46.0)
    strength = np.clip((values_db - floor) / 16.0, 0.0, 1.0)
    return interp_samples(times, strength, n, sr)


def pressure_curve(band: np.ndarray, sr: int, n: int) -> np.ndarray:
    times, values = rms_frames(to_mono(band), sr)
    values_db = np.asarray(db(values), dtype=np.float64)
    base = float(np.percentile(values_db, 55))
    pressure = np.clip((values_db - base) / 10.0, 0.0, 1.0)
    return interp_samples(times, pressure, n, sr)


def apply_multiband_duck(accomp: np.ndarray, vocal: np.ndarray, sr: int, budgets: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    n = min(accomp.shape[0], vocal.shape[0])
    accomp = accomp[:n]
    vocal = vocal[:n]
    strength = vocal_strength_curve(vocal, sr, n)
    low = butter_filter(accomp, sr, "lowpass", 180.0)
    body = butter_filter(accomp, sr, "bandpass", (180.0, 1200.0))
    presence = butter_filter(accomp, sr, "bandpass", (1200.0, 5000.0))
    air = butter_filter(accomp, sr, "highpass", 5000.0)
    low_pressure = pressure_curve(low, sr, n)
    presence_pressure = pressure_curve(presence + air, sr, n)

    low_gain = -strength * float(budgets["low"]) * (0.65 + 0.35 * low_pressure)
    body_gain = -strength * float(budgets["body"])
    presence_gain = -strength * float(budgets["presence"]) * (0.70 + 0.30 * presence_pressure)
    air_gain = -strength * float(budgets["air"])
    low_gain = smooth_gain_db(low_gain, sr, 35.0, 180.0)
    body_gain = smooth_gain_db(body_gain, sr, 35.0, 180.0)
    presence_gain = smooth_gain_db(presence_gain, sr, 35.0, 180.0)
    air_gain = smooth_gain_db(air_gain, sr, 35.0, 180.0)

    out = accomp.copy()
    out += low * (lin(low_gain)[:, None] - 1.0)
    out += body * (lin(body_gain)[:, None] - 1.0)
    out += presence * (lin(presence_gain)[:, None] - 1.0)
    out += air * (lin(air_gain)[:, None] - 1.0)
    active = strength > 0.2
    report = {
        "budgets_db": budgets,
        "low_duck_db_active_p50": round(float(np.median(low_gain[active])) if np.any(active) else 0.0, 3),
        "low_duck_db_active_p90": round(float(np.percentile(low_gain[active], 10)) if np.any(active) else 0.0, 3),
        "presence_duck_db_active_p50": round(float(np.median(presence_gain[active])) if np.any(active) else 0.0, 3),
        "presence_duck_db_active_p90": round(float(np.percentile(presence_gain[active], 10)) if np.any(active) else 0.0, 3),
    }
    return np.clip(out, -0.98, 0.98), report


def active_side_minus_mid(audio: np.ndarray, sr: int, regions: list[tuple[float, float]]) -> float | None:
    if audio.shape[1] < 2:
        return None
    mid = (audio[:, 0] + audio[:, 1]) / math.sqrt(2.0)
    side = (audio[:, 0] - audio[:, 1]) / math.sqrt(2.0)
    mid_db = rms_for_regions(mid, sr, regions)
    side_db = rms_for_regions(side, sr, regions)
    return round(side_db - mid_db, 3)


def apply_side_trim(vocal: np.ndarray, sr: int, plan: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
    spatial = reference_spatial(plan)
    target = spatial.get("active_side_minus_mid_db")
    if vocal.shape[1] < 2 or not isinstance(target, (int, float)):
        return vocal, {"enabled": False, "reason": "missing stereo vocal or reference spatial target"}
    regions = active_regions_from_vocal(vocal, sr)
    current = active_side_minus_mid(vocal, sr, regions)
    if current is None:
        return vocal, {"enabled": False, "reason": "could not measure current width"}
    error = current - float(target)
    if error <= 1.5:
        return vocal, {"enabled": False, "current_active_side_minus_mid_db": current, "target_active_side_minus_mid_db": round(float(target), 3), "error_db": round(error, 3)}
    trim_db = -clamp(error * 0.45, 0.5, 6.0)
    if bool(spatial.get("near_mono_center_led")) and error > 5.0:
        trim_db = min(trim_db, -3.0)
    mid = (vocal[:, 0] + vocal[:, 1]) / math.sqrt(2.0)
    side = (vocal[:, 0] - vocal[:, 1]) / math.sqrt(2.0) * float(lin(trim_db))
    out = np.column_stack(((mid + side) / math.sqrt(2.0), (mid - side) / math.sqrt(2.0)))
    return out, {
        "enabled": True,
        "side_trim_db": round(trim_db, 3),
        "current_active_side_minus_mid_db": current,
        "target_active_side_minus_mid_db": round(float(target), 3),
        "error_db": round(error, 3),
        "policy": "只收过宽侧向能量；不改变音色相似度目标。",
    }


def compute_global_gain(vocal: np.ndarray, accomp: np.ndarray, sr: int, plan: dict[str, Any]) -> tuple[float, float, dict[str, Any]]:
    target = reference_gap(plan)
    if target is None:
        target = -2.0
        source = "generic"
    else:
        source = "reference"
    regions = active_regions_from_vocal(vocal, sr)
    v_db = rms_for_regions(to_mono(vocal), sr, regions)
    a_db = rms_for_regions(to_mono(accomp), sr, regions)
    current = v_db - a_db
    needed = float(target) - current
    if abs(needed) < 0.35:
        correction = 0.0
    else:
        correction = clamp(needed, -8.0, 8.0)
    if correction > 0.0:
        v_gain = correction * 0.58
        a_gain = -correction * 0.42
    else:
        v_gain = correction * 0.78
        a_gain = -correction * 0.22
    report = {
        "target_source": source,
        "target_active_gap_db": round(float(target), 3),
        "current_active_gap_db": round(current, 3),
        "needed_gap_correction_db": round(needed, 3),
        "applied_gap_correction_db": round(correction, 3),
        "vocal_gain_db": round(v_gain, 3),
        "accomp_gain_db": round(a_gain, 3),
        "active_region_count": len(regions),
    }
    return v_gain, a_gain, report


def apply_section_reference(
    vocal: np.ndarray,
    accomp: np.ndarray,
    sr: int,
    plan: dict[str, Any],
    frame_sec: float = 4.0,
    hop_sec: float = 1.0,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    ref_vocal_path, ref_accomp_path = reference_sources(plan)
    if ref_vocal_path is None or ref_accomp_path is None or not ref_vocal_path.exists() or not ref_accomp_path.exists():
        return vocal, accomp, {"enabled": False, "reason": "missing reference vocal/accomp"}
    ref_vocal, _ = load_audio_as_float(ref_vocal_path, target_sr=sr)
    ref_accomp, _ = load_audio_as_float(ref_accomp_path, target_sr=sr)
    n = min(vocal.shape[0], accomp.shape[0], ref_vocal.shape[0], ref_accomp.shape[0])
    vocal = vocal[:n]
    accomp = accomp[:n]
    ref_v = to_mono(ref_vocal[:n])
    ref_a = to_mono(ref_accomp[:n])
    cur_v = to_mono(vocal)
    cur_a = to_mono(accomp)
    frame = max(1024, int(round(frame_sec * sr)))
    hop = max(256, int(round(hop_sec * sr)))
    starts = np.arange(0, max(1, n - frame + 1), hop)

    ref_frame_v = []
    for start in starts:
        end = min(start + frame, n)
        ref_frame_v.append(rms_db(ref_v[start:end]))
    active_floor = max(float(np.percentile(np.asarray(ref_frame_v), 65) - 14.0), -46.0)

    frame_times: list[float] = []
    vocal_gains: list[float] = []
    accomp_gains: list[float] = []
    events: list[dict[str, Any]] = []
    deadband = 1.15
    for start in starts:
        end = min(start + frame, n)
        center = (start + (end - start) * 0.5) / sr
        frame_times.append(center)
        if end <= start:
            vocal_gains.append(0.0)
            accomp_gains.append(0.0)
            continue
        ref_v_db = rms_db(ref_v[start:end])
        if ref_v_db < active_floor or rms_db(cur_v[start:end]) < -48.0:
            vocal_gains.append(0.0)
            accomp_gains.append(0.0)
            continue
        ref_gap = ref_v_db - rms_db(ref_a[start:end])
        cur_gap = rms_db(cur_v[start:end]) - rms_db(cur_a[start:end])
        delta = ref_gap - cur_gap
        if abs(delta) <= deadband:
            vocal_gains.append(0.0)
            accomp_gains.append(0.0)
            continue
        signed = math.copysign(min(2.0, abs(delta) - deadband * 0.35), delta)
        if signed > 0:
            v_gain = min(1.2, signed * 0.45)
            a_gain = -min(1.0, signed * 0.55)
        else:
            v_gain = -min(1.2, abs(signed) * 0.75)
            a_gain = min(0.6, abs(signed) * 0.25)
        vocal_gains.append(v_gain)
        accomp_gains.append(a_gain)
        events.append({
            "time_sec": round(center, 3),
            "reference_gap_db": round(ref_gap, 2),
            "current_gap_db": round(cur_gap, 2),
            "delta_db": round(delta, 2),
            "vocal_gain_db": round(v_gain, 2),
            "accomp_gain_db": round(a_gain, 2),
        })

    sample_times = np.arange(n, dtype=np.float64) / sr
    v_curve = np.interp(sample_times, frame_times, vocal_gains, left=0.0, right=0.0)
    a_curve = np.interp(sample_times, frame_times, accomp_gains, left=0.0, right=0.0)
    v_curve = smooth_gain_db(v_curve, sr, 120.0, 300.0)
    a_curve = smooth_gain_db(a_curve, sr, 120.0, 300.0)
    out_v = vocal * lin(v_curve)[:, None]
    out_a = accomp * lin(a_curve)[:, None]
    report = {
        "enabled": True,
        "event_count": len(events),
        "events": events[:100],
        "peak_vocal_gain_db": round(float(np.max(v_curve)) if v_curve.size else 0.0, 3),
        "peak_vocal_cut_db": round(float(np.min(v_curve)) if v_curve.size else 0.0, 3),
        "peak_accomp_gain_db": round(float(np.max(a_curve)) if a_curve.size else 0.0, 3),
        "peak_accomp_cut_db": round(float(np.min(a_curve)) if a_curve.size else 0.0, 3),
        "policy": "参考窗口双向轻修：人声埋时同时轻推人声/退伴奏，人声过前时优先收人声。",
    }
    return np.clip(out_v, -0.98, 0.98), np.clip(out_a, -0.98, 0.98), report


def process(vocal: np.ndarray, accomp: np.ndarray, sr: int, plan: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    n = min(vocal.shape[0], accomp.shape[0])
    vocal = vocal[:n]
    accomp = accomp[:n]
    vocal, spatial_report = apply_side_trim(vocal, sr, plan)
    budgets = duck_budgets(plan)
    accomp, duck_report = apply_multiband_duck(accomp, vocal, sr, budgets)
    v_gain, a_gain, global_report = compute_global_gain(vocal, accomp, sr, plan)
    vocal = vocal * float(lin(v_gain))
    accomp = accomp * float(lin(a_gain))
    vocal, accomp, section_report = apply_section_reference(vocal, accomp, sr, plan)
    peak = max(float(np.max(np.abs(vocal))) if vocal.size else 0.0, float(np.max(np.abs(accomp))) if accomp.size else 0.0)
    safety_trim_db = 0.0
    if peak > 0.98:
        scale = 0.98 / peak
        vocal *= scale
        accomp *= scale
        safety_trim_db = 20.0 * math.log10(scale)
    report = {
        "enabled": True,
        "schema": "final_fusion_pass.v1",
        "spatial": spatial_report,
        "duck": duck_report,
        "global_balance": global_report,
        "section": section_report,
        "safety_trim_db": round(safety_trim_db, 3),
        "policy": "最终融合统一应用：width trim -> duck budget -> global active gap -> section reference windows。",
    }
    return np.clip(vocal, -0.98, 0.98), np.clip(accomp, -0.98, 0.98), report


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply final reference-driven vocal/accompaniment fusion pass.")
    parser.add_argument("vocal_in", type=Path)
    parser.add_argument("accomp_in", type=Path)
    parser.add_argument("vocal_out", type=Path)
    parser.add_argument("accomp_out", type=Path)
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--metadata", type=Path, default=None)
    args = parser.parse_args()

    vocal, sr = read_audio(args.vocal_in)
    accomp, sr2 = read_audio(args.accomp_in)
    if sr2 != sr:
        raise SystemExit(f"sample-rate mismatch: vocal={sr}, accomp={sr2}")
    plan = load_json(args.plan)
    vocal_out, accomp_out, report = process(vocal, accomp, int(sr), plan)
    args.vocal_out.parent.mkdir(parents=True, exist_ok=True)
    sf.write(args.vocal_out, vocal_out, int(sr), subtype="FLOAT")
    sf.write(args.accomp_out, accomp_out, int(sr), subtype="FLOAT")
    if args.metadata:
        write_json(args.metadata, report)
    gb = report["global_balance"]
    sec = report["section"]
    print(
        "[final-fusion-pass] "
        f"gap {gb.get('current_active_gap_db')} -> target {gb.get('target_active_gap_db')}; "
        f"v {gb.get('vocal_gain_db')} dB / a {gb.get('accomp_gain_db')} dB; "
        f"section_events={sec.get('event_count', 0)}"
    )


if __name__ == "__main__":
    main()
