#!/usr/bin/env python3
"""应用最终融合 pass。

这一步只做“参考误差修正”：
1. 读取这首歌对应原曲的融合目标；
2. 测当前渲染的人声/伴奏关系；
3. 计算 current_errors；
4. 只按这些误差生成 corrections；
5. 修正后只复测最终状态并写入 post_fusion_measure，不做二次追满。

profile / 歌名 / 风格标签只能进入 debug，不参与决策。四首回归歌只是验证集，
不是四套手写策略。
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


MASKING_BANDS = {
    "low": ("lowpass", 180.0),
    "body": ("bandpass", (180.0, 1200.0)),
    "presence": ("bandpass", (1200.0, 5000.0)),
    "air": ("highpass", 5000.0),
}

MASKING_DUCK_CAPS_DB = {
    "low": 0.95,
    "body": 0.85,
    "presence": 1.15,
    "air": 0.60,
}

GLOBAL_GAP_HARD_KNEE_DB = 8.0
GLOBAL_GAP_SOFT_CEILING_DB = 9.0

# section reference 只做“同位置偏差诊断 + 极端救急”。
# 不再按窗口降低人声或抬高伴奏，避免翻唱分句和原曲 stem 稍有错位时产生每段不同的听感。
SECTION_DIAGNOSTIC_DEADBAND_DB = 1.85
SECTION_RESCUE_DEADBAND_DB = 4.5
SECTION_RESCUE_MAX_ACCOMP_DUCK_DB = 0.45

# duck 之后再复查一次 masking 残差；这里只做全曲静态小修，不做 1 秒级局部自动化。
# presence/air 的阈值和上限按指标分流，不绑定任何歌名或 profile。
RESIDUAL_MASKING_TRIM_POLICY = {
    "low": {"deadband_db": 99.0, "weight": 0.0, "cap_db": 0.0},
    "body": {"deadband_db": 3.0, "weight": 0.12, "cap_db": 0.35},
    "presence": {"deadband_db": 2.5, "weight": 0.22, "cap_db": 0.85},
    "air": {"deadband_db": 3.5, "weight": 0.18, "cap_db": 0.65},
}


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


def reference_features(plan: dict[str, Any]) -> dict[str, Any]:
    return ((plan.get("reference") or {}).get("features") or {})


def rounded(value: Any, ndigits: int = 3) -> float | None:
    return round(float(value), ndigits) if isinstance(value, (int, float)) else None


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


def band_levels_for_regions(audio: np.ndarray, sr: int, regions: list[tuple[float, float]]) -> dict[str, float]:
    """按 final fusion 使用的四个遮挡区间测 active RMS。

    这些 band 不是风格 profile，只是执行层需要的频段：低频、主体、咬字、空气。
    """
    levels: dict[str, float] = {}
    for band, (kind, cutoff) in MASKING_BANDS.items():
        filtered = butter_filter(audio, sr, kind, cutoff)
        levels[band] = round(rms_for_regions(to_mono(filtered), sr, regions), 3)
    return levels


def masking_state(vocal: np.ndarray, accomp: np.ndarray, sr: int) -> dict[str, Any]:
    regions = active_regions_from_vocal(vocal, sr)
    vocal_levels = band_levels_for_regions(vocal, sr, regions)
    accomp_levels = band_levels_for_regions(accomp, sr, regions)
    masking = {
        band: round(accomp_levels[band] - vocal_levels[band], 3)
        for band in MASKING_BANDS
    }
    return {
        "active_region_count": len(regions),
        "vocal_band_db": vocal_levels,
        "accomp_band_db": accomp_levels,
        "accomp_minus_vocal_db": masking,
    }


def masking_error_by_band(reference_masking: dict[str, Any], current_masking: dict[str, Any]) -> dict[str, float]:
    """返回“当前伴奏遮挡 - 原曲伴奏遮挡”。

    正值表示当前伴奏在人声区间比原曲更挡；这个误差是通用指标，
    后续 duck/残差 trim 都只看它，不看歌名、歌手或风格 profile。
    """
    target = reference_masking.get("accomp_minus_vocal_db") or {}
    current = current_masking.get("accomp_minus_vocal_db") or {}
    errors: dict[str, float] = {}
    for band in MASKING_BANDS:
        if isinstance(current.get(band), (int, float)) and isinstance(target.get(band), (int, float)):
            errors[band] = round(float(current[band]) - float(target[band]), 3)
    return errors


def reference_masking_targets(plan: dict[str, Any], sr: int) -> dict[str, Any]:
    """从对应原曲 stem 计算伴奏在人声区间的频段遮挡目标。"""
    ref_vocal_path, ref_accomp_path = reference_sources(plan)
    if ref_vocal_path is None or ref_accomp_path is None or not ref_vocal_path.exists() or not ref_accomp_path.exists():
        return {"available": False, "reason": "missing reference vocal/accomp"}
    ref_vocal, _ = load_audio_as_float(ref_vocal_path, target_sr=sr)
    ref_accomp, _ = load_audio_as_float(ref_accomp_path, target_sr=sr)
    n = min(ref_vocal.shape[0], ref_accomp.shape[0])
    return {
        "available": True,
        **masking_state(ref_vocal[:n], ref_accomp[:n], sr),
    }


def build_duck_budget_from_errors(
    reference_masking: dict[str, Any],
    current_masking: dict[str, Any],
    plan: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """只按“当前遮挡比原曲多多少”生成 duck 预算。

    没有参考 stem 时才使用极轻兜底；有参考时，profile/dry tags 只进入 debug，
    不作为 final fusion 的决策核心。
    """
    current = current_masking.get("accomp_minus_vocal_db") or {}
    target = reference_masking.get("accomp_minus_vocal_db") or {}
    spatial = reference_spatial(plan)
    near_mono = bool(spatial.get("near_mono_center_led"))
    if not reference_masking.get("available") or not target:
        budgets = {
            "low": 0.35,
            "body": 0.25,
            "presence": 0.45,
            "air": 0.15 if near_mono else 0.25,
            "source": "generic_missing_reference",
            "policy": "缺少参考 stem 时只做极轻兜底；有参考时必须按 reference masking error 计算。",
        }
        return budgets, {
            "available": False,
            "reason": reference_masking.get("reason", "missing reference masking target"),
            "by_band_db": {},
        }

    deadband = {"low": 1.4, "body": 1.1, "presence": 0.9, "air": 1.2}
    weights = {"low": 0.28, "body": 0.34, "presence": 0.42, "air": 0.30}
    budgets: dict[str, Any] = {
        "source": "reference_masking_error",
        "policy": "只在当前伴奏遮挡超过原曲同频段遮挡时让位；不按歌曲类型/profile 套参数。",
    }
    errors = masking_error_by_band(reference_masking, current_masking)
    for band in MASKING_BANDS:
        if not isinstance(current.get(band), (int, float)) or not isinstance(target.get(band), (int, float)):
            budgets[band] = 0.0
            continue
        excess = float(errors.get(band, 0.0))
        amount = max(0.0, excess - deadband[band]) * weights[band]
        if band == "air" and near_mono:
            amount = min(amount, 0.45)
        budgets[band] = round(clamp(amount, 0.0, MASKING_DUCK_CAPS_DB[band]), 3)
    return budgets, {
        "available": True,
        "by_band_db": errors,
        "deadband_db": deadband,
        "policy": "positive means current accompaniment masks vocal more than the original song in that band.",
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


def build_residual_masking_trim(reference_masking: dict[str, Any], current_masking: dict[str, Any]) -> tuple[dict[str, float], dict[str, Any]]:
    """在 duck/global 后按最终预估遮挡残差生成全曲静态 trim。

    这一步只处理“伴奏仍比原曲更挡人声”的频段，尤其是 presence/air；
    它不追 active gap，也不改变人声 EQ，避免把不同 case 写成点对点规则。
    """
    if not reference_masking.get("available"):
        return {band: 0.0 for band in MASKING_BANDS}, {
            "enabled": False,
            "reason": reference_masking.get("reason", "missing reference masking target"),
            "policy": "没有原曲 stem masking 目标时不做残差 trim。",
        }
    errors = masking_error_by_band(reference_masking, current_masking)
    trim_db: dict[str, float] = {}
    for band in MASKING_BANDS:
        policy = RESIDUAL_MASKING_TRIM_POLICY[band]
        error = float(errors.get(band, 0.0))
        amount = max(0.0, error - float(policy["deadband_db"])) * float(policy["weight"])
        trim_db[band] = round(clamp(amount, 0.0, float(policy["cap_db"])), 3)
    enabled = any(amount > 0.0 for amount in trim_db.values())
    return trim_db, {
        "enabled": enabled,
        "trim_db": trim_db,
        "residual_masking_error_db": errors,
        "policy_by_band": RESIDUAL_MASKING_TRIM_POLICY,
        "policy": (
            "duck/global 后仍有遮挡残差时，只对伴奏做全曲静态小幅 presence/air 让位；"
            "不按歌名/profile 分支，也不做逐段局部自动化。"
        ),
    }


def apply_residual_masking_trim(accomp: np.ndarray, sr: int, trim_db: dict[str, float]) -> np.ndarray:
    """应用静态残差 trim；0 dB band 直接跳过，保持伴奏主体稳定。"""
    if not any(float(trim_db.get(band, 0.0)) > 0.0 for band in MASKING_BANDS):
        return accomp
    out = accomp.copy()
    for band, amount in trim_db.items():
        cut_db = float(amount)
        if cut_db <= 0.0:
            continue
        kind, cutoff = MASKING_BANDS[band]
        filtered = butter_filter(accomp, sr, kind, cutoff)
        out += filtered * (float(lin(-cut_db)) - 1.0)
    return np.clip(out, -0.98, 0.98)


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
    elif needed > GLOBAL_GAP_HARD_KNEE_DB:
        # 1.1 不做 v4 那种 residual 闭环追满；超过 8 dB 只进软膝盖，
        # 给明显埋的人声一点余量，同时避免 active gap 数字绑架听感。
        excess = needed - GLOBAL_GAP_HARD_KNEE_DB
        correction = clamp(
            GLOBAL_GAP_HARD_KNEE_DB + excess * 0.35,
            0.0,
            GLOBAL_GAP_SOFT_CEILING_DB,
        )
    else:
        correction = clamp(needed, -8.0, GLOBAL_GAP_HARD_KNEE_DB)
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
        "soft_knee": {
            "hard_knee_db": GLOBAL_GAP_HARD_KNEE_DB,
            "positive_ceiling_db": GLOBAL_GAP_SOFT_CEILING_DB,
            "excess_ratio": 0.35,
            "policy": "超过 hard knee 只做软补偿；最终误差只记录，不再二次追满。",
        },
        "active_region_count": len(regions),
    }
    return v_gain, a_gain, report


def apply_section_reference(
    vocal: np.ndarray,
    accomp: np.ndarray,
    sr: int,
    plan: dict[str, Any],
    frame_sec: float = 8.0,
    hop_sec: float = 2.0,
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
    diagnostic_deltas: list[float] = []
    rescue_candidates: list[float] = []
    events: list[dict[str, Any]] = []
    deadband = SECTION_DIAGNOSTIC_DEADBAND_DB
    for start in starts:
        end = min(start + frame, n)
        center = (start + (end - start) * 0.5) / sr
        frame_times.append(center)
        if end <= start:
            diagnostic_deltas.append(0.0)
            rescue_candidates.append(0.0)
            continue
        ref_v_db = rms_db(ref_v[start:end])
        if ref_v_db < active_floor or rms_db(cur_v[start:end]) < -48.0:
            diagnostic_deltas.append(0.0)
            rescue_candidates.append(0.0)
            continue
        ref_gap = ref_v_db - rms_db(ref_a[start:end])
        cur_gap = rms_db(cur_v[start:end]) - rms_db(cur_a[start:end])
        delta = ref_gap - cur_gap
        diagnostic_deltas.append(delta)
        if abs(delta) <= deadband:
            rescue_candidates.append(0.0)
            continue
        # 负 delta 只说明当前局部人声比原曲更靠前；这里不再切人声/抬伴奏，
        # 因为翻唱分句错位时这类动作最容易造成“每段效果不同”。
        rescue_duck = 0.0
        if delta > SECTION_RESCUE_DEADBAND_DB:
            rescue_duck = -min(
                SECTION_RESCUE_MAX_ACCOMP_DUCK_DB,
                max(0.0, delta - SECTION_RESCUE_DEADBAND_DB) * 0.08,
            )
        rescue_candidates.append(rescue_duck)
        events.append({
            "time_sec": round(center, 3),
            "reference_gap_db": round(ref_gap, 2),
            "current_gap_db": round(cur_gap, 2),
            "delta_db": round(delta, 2),
            "vocal_gain_db": 0.0,
            "accomp_gain_db": round(rescue_duck, 2),
            "action": "rescue_accomp_duck_candidate" if rescue_duck < 0.0 else "diagnostic_only",
        })

    # 只有连续窗口都显示“当前人声比原曲更被埋”才执行救急伴奏下压；
    # 孤立窗口只写报告，避免按 1 秒/2 秒窗口抽动。
    sustained_candidates: list[float] = []
    for idx, candidate in enumerate(rescue_candidates):
        if candidate >= 0.0:
            sustained_candidates.append(0.0)
            continue
        left = max(0, idx - 1)
        right = min(len(rescue_candidates), idx + 2)
        nearby = [value for value in rescue_candidates[left:right] if value < 0.0]
        sustained_candidates.append(candidate if len(nearby) >= 2 else 0.0)

    sample_times = np.arange(n, dtype=np.float64) / sr
    v_curve = np.zeros(n, dtype=np.float64)
    a_curve = np.interp(sample_times, frame_times, sustained_candidates, left=0.0, right=0.0)
    a_curve = smooth_gain_db(a_curve, sr, 650.0, 1200.0)
    out_v = vocal
    out_a = accomp * lin(a_curve)[:, None]
    audio_event_count = sum(1 for value in sustained_candidates if value < 0.0)
    report = {
        "enabled": True,
        "event_count": len(events),
        "diagnostic_event_count": len(events),
        "audio_event_count": audio_event_count,
        "events": events[:100],
        "peak_vocal_gain_db": round(float(np.max(v_curve)) if v_curve.size else 0.0, 3),
        "peak_vocal_cut_db": round(float(np.min(v_curve)) if v_curve.size else 0.0, 3),
        "peak_accomp_gain_db": round(float(np.max(a_curve)) if a_curve.size else 0.0, 3),
        "peak_accomp_cut_db": round(float(np.min(a_curve)) if a_curve.size else 0.0, 3),
        "deadband_db": deadband,
        "rescue_deadband_db": SECTION_RESCUE_DEADBAND_DB,
        "audio_applied": audio_event_count > 0,
        "policy": (
            "局部 section 默认只诊断；不切人声、不抬伴奏。"
            "只有连续窗口显示当前人声明显比原曲更被埋时，才极轻压伴奏救急。"
        ),
    }
    return np.clip(out_v, -0.98, 0.98), np.clip(out_a, -0.98, 0.98), report


def build_reference_targets(
    plan: dict[str, Any],
    reference_masking: dict[str, Any],
) -> dict[str, Any]:
    """把 final fusion 会对齐的原曲目标集中写入 metadata。"""
    ref = reference_features(plan)
    balance = ref.get("vocal_accomp_balance") or {}
    spatial = ref.get("vocal_spatial_profile") or {}
    reverb = ref.get("reverb_proxy") or {}
    dynamics = ref.get("vocal_dynamics") or {}
    return {
        "available": bool(ref),
        "global_active_gap_db": rounded(
            balance.get("active_vocal_minus_accomp_db", balance.get("vocal_minus_accomp_db")),
            3,
        ),
        "section_gap_curve": {
            "source": "reference vocal/accomp stems",
            "frame_sec": 8.0,
            "hop_sec": 2.0,
            "computed_by": "apply_section_reference",
            "mode": "diagnostic_with_sustained_rescue_duck",
        },
        "vocal_width": {
            "active_side_minus_mid_db": rounded(spatial.get("active_side_minus_mid_db"), 3),
            "near_mono_center_led": bool(spatial.get("near_mono_center_led")),
            "lr_correlation_active": rounded(spatial.get("lr_correlation_active"), 5),
        },
        "vocal_reverb": {
            "tail_to_onset_ratio_db": rounded(reverb.get("tail_to_onset_ratio_db"), 3),
            "est_rt60_ms": rounded(reverb.get("est_rt60_ms"), 1),
            "confidence": rounded(reverb.get("confidence"), 3),
            "note": "Final fusion 只记录目标；混响/空间湿度在前面的 spatial stage 处理。",
        },
        "vocal_dynamics": {
            "frame_range_p90_p10_db": rounded(dynamics.get("frame_range_p90_p10_db"), 3),
            "micro_range_p95_p50_db": rounded(dynamics.get("micro_range_p95_p50_db"), 3),
            "micro_range_p99_p50_db": rounded(dynamics.get("micro_range_p99_p50_db"), 3),
            "note": "Final fusion 只记录目标；动态稳定性在 vocal_dynamic_lift/event guard 处理。",
        },
        "accomp_masking_bands": {
            "available": bool(reference_masking.get("available")),
            "accomp_minus_vocal_db": reference_masking.get("accomp_minus_vocal_db") or {},
            "vocal_band_db": reference_masking.get("vocal_band_db") or {},
            "accomp_band_db": reference_masking.get("accomp_band_db") or {},
        },
        "policy": "每首只对齐自己的原曲 reference.features/reference stems；profile 只用于 debug/explain。",
    }


def build_post_fusion_measure(
    vocal: np.ndarray,
    accomp: np.ndarray,
    sr: int,
    plan: dict[str, Any],
    reference_masking: dict[str, Any],
    section_report: dict[str, Any],
) -> dict[str, Any]:
    """复测最终入 stereo sum 的人声/伴奏关系，只写 metadata，不再修正。"""
    target_gap = reference_gap(plan)
    regions = active_regions_from_vocal(vocal, sr)
    final_v_db = rms_for_regions(to_mono(vocal), sr, regions)
    final_a_db = rms_for_regions(to_mono(accomp), sr, regions)
    final_gap = final_v_db - final_a_db
    final_masking = masking_state(vocal, accomp, sr)
    ref_masking = reference_masking.get("accomp_minus_vocal_db") or {}
    cur_masking = final_masking.get("accomp_minus_vocal_db") or {}
    masking_error = {
        band: round(float(cur_masking[band]) - float(ref_masking[band]), 3)
        for band in MASKING_BANDS
        if isinstance(cur_masking.get(band), (int, float)) and isinstance(ref_masking.get(band), (int, float))
    }
    spatial = reference_spatial(plan)
    target_width = spatial.get("active_side_minus_mid_db")
    final_width = active_side_minus_mid(vocal, sr, regions)
    return {
        "enabled": True,
        "target_active_gap_db": rounded(target_gap, 3),
        "final_active_gap_db": round(final_gap, 3),
        "final_gap_error_db": (
            round(final_gap - float(target_gap), 3)
            if isinstance(target_gap, (int, float))
            else None
        ),
        "final_vocal_active_rms_db": round(final_v_db, 3),
        "final_accomp_active_rms_db": round(final_a_db, 3),
        "final_masking_bands": final_masking,
        "final_masking_error_db": masking_error,
        "final_width": {
            "active_side_minus_mid_db": final_width,
            "target_active_side_minus_mid_db": rounded(target_width, 3),
            "width_error_db": (
                round(float(final_width) - float(target_width), 3)
                if isinstance(final_width, (int, float)) and isinstance(target_width, (int, float))
                else None
            ),
        },
        "section_summary": {
            "event_count": section_report.get("event_count", 0),
            "diagnostic_event_count": section_report.get("diagnostic_event_count", section_report.get("event_count", 0)),
            "audio_event_count": section_report.get("audio_event_count", 0),
            "audio_applied": bool(section_report.get("audio_applied")),
            "peak_vocal_gain_db": section_report.get("peak_vocal_gain_db"),
            "peak_vocal_cut_db": section_report.get("peak_vocal_cut_db"),
            "peak_accomp_gain_db": section_report.get("peak_accomp_gain_db"),
            "peak_accomp_cut_db": section_report.get("peak_accomp_cut_db"),
        },
        "policy": "最终复测只用于排查听感/指标冲突；不做 residual gap 追满。",
    }


def build_current_errors(
    global_report: dict[str, Any],
    spatial_report: dict[str, Any],
    current_masking: dict[str, Any],
    masking_errors: dict[str, Any],
    section_report: dict[str, Any],
    post_fusion_measure: dict[str, Any],
) -> dict[str, Any]:
    target_gap = global_report.get("target_active_gap_db")
    current_gap = global_report.get("current_active_gap_db")
    return {
        "global_gap_error_db": (
            round(float(current_gap) - float(target_gap), 3)
            if isinstance(current_gap, (int, float)) and isinstance(target_gap, (int, float))
            else None
        ),
        "needed_global_gap_correction_db": global_report.get("needed_gap_correction_db"),
        "post_fusion_gap_error_db": post_fusion_measure.get("final_gap_error_db"),
        "width_error_db": spatial_report.get("error_db"),
        "duck_error": masking_errors,
        "current_accomp_masking_bands": current_masking,
        "section_gap_error": {
            "event_count": section_report.get("event_count", 0),
            "diagnostic_event_count": section_report.get("diagnostic_event_count", section_report.get("event_count", 0)),
            "audio_event_count": section_report.get("audio_event_count", 0),
            "events": section_report.get("events", []),
            "policy": "局部窗口只作为诊断/极端救急依据；不再按窗口切人声或抬伴奏。",
        },
        "reverb_error": {
            "available": False,
            "reason": "Final fusion 不重新估计混响；由 spatial stage/audit 对比原曲人声 stem。",
        },
        "dynamic_error": {
            "available": False,
            "reason": "Final fusion 不重新做动态处理；由 vocal_dynamic_lift/event guard 与后续 audit 负责。",
        },
    }


def build_corrections(
    global_report: dict[str, Any],
    spatial_report: dict[str, Any],
    duck_report: dict[str, Any],
    section_report: dict[str, Any],
    residual_trim_report: dict[str, Any],
    safety_trim_db: float,
) -> dict[str, Any]:
    return {
        "global_gain": {
            "enabled": abs(float(global_report.get("applied_gap_correction_db") or 0.0)) > 0.0,
            "vocal_gain_db": global_report.get("vocal_gain_db"),
            "accomp_gain_db": global_report.get("accomp_gain_db"),
            "applied_gap_correction_db": global_report.get("applied_gap_correction_db"),
        },
        "section_moves": {
            "enabled": bool(section_report.get("audio_applied")),
            "event_count": section_report.get("event_count", 0),
            "diagnostic_event_count": section_report.get("diagnostic_event_count", section_report.get("event_count", 0)),
            "audio_event_count": section_report.get("audio_event_count", 0),
            "peak_vocal_gain_db": section_report.get("peak_vocal_gain_db"),
            "peak_vocal_cut_db": section_report.get("peak_vocal_cut_db"),
            "peak_accomp_gain_db": section_report.get("peak_accomp_gain_db"),
            "peak_accomp_cut_db": section_report.get("peak_accomp_cut_db"),
        },
        "duck_budget": duck_report.get("budgets_db") or {},
        "residual_masking_trim": residual_trim_report,
        "spatial_trim": spatial_report,
        "safety_trim_db": round(safety_trim_db, 3),
        "policy": "修正只来自 reference_targets 与 current_errors 的差异；不按歌名、风格或 profile 分流。",
    }


def debug_profile(plan: dict[str, Any]) -> dict[str, Any]:
    fusion = plan.get("fusion_intent") or ((plan.get("reference") or {}).get("fusion_intent") or {})
    return {
        "profile": fusion.get("profile"),
        "profile_usage": "debug/explain only",
        "reasons": fusion.get("reasons") or [],
        "policy": "profile 不参与 apply_final_fusion_pass 的目标、误差或修正计算。",
    }


def process(vocal: np.ndarray, accomp: np.ndarray, sr: int, plan: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    n = min(vocal.shape[0], accomp.shape[0])
    vocal = vocal[:n]
    accomp = accomp[:n]
    vocal, spatial_report = apply_side_trim(vocal, sr, plan)
    reference_masking = reference_masking_targets(plan, sr)
    current_masking = masking_state(vocal, accomp, sr)
    budgets, masking_errors = build_duck_budget_from_errors(reference_masking, current_masking, plan)
    accomp, duck_report = apply_multiband_duck(accomp, vocal, sr, budgets)
    v_gain, a_gain, global_report = compute_global_gain(vocal, accomp, sr, plan)
    vocal = vocal * float(lin(v_gain))
    accomp = accomp * float(lin(a_gain))
    vocal, accomp, section_report = apply_section_reference(vocal, accomp, sr, plan)
    residual_masking = masking_state(vocal, accomp, sr)
    residual_trim_db, residual_trim_report = build_residual_masking_trim(reference_masking, residual_masking)
    accomp = apply_residual_masking_trim(accomp, sr, residual_trim_db)
    peak = max(float(np.max(np.abs(vocal))) if vocal.size else 0.0, float(np.max(np.abs(accomp))) if accomp.size else 0.0)
    safety_trim_db = 0.0
    if peak > 0.98:
        scale = 0.98 / peak
        vocal *= scale
        accomp *= scale
        safety_trim_db = 20.0 * math.log10(scale)
    reference_targets = build_reference_targets(plan, reference_masking)
    post_fusion_measure = build_post_fusion_measure(
        vocal,
        accomp,
        sr,
        plan,
        reference_masking,
        section_report,
    )
    current_errors = build_current_errors(
        global_report,
        spatial_report,
        current_masking,
        masking_errors,
        section_report,
        post_fusion_measure,
    )
    corrections = build_corrections(
        global_report,
        spatial_report,
        duck_report,
        section_report,
        residual_trim_report,
        safety_trim_db,
    )
    report = {
        "enabled": True,
        "schema": "final_fusion_pass.v2_2.reference_error_correction_stable_sections",
        "reference_targets": reference_targets,
        "current_errors": current_errors,
        "corrections": corrections,
        "post_fusion_measure": post_fusion_measure,
        "debug_profile": debug_profile(plan),
        "spatial": spatial_report,
        "duck": duck_report,
        "residual_masking_trim": residual_trim_report,
        "global_balance": global_report,
        "section": section_report,
        "safety_trim_db": round(safety_trim_db, 3),
        "policy": (
            "最终融合统一应用：reference targets -> current errors -> corrections。"
            "每首只对齐自己的原曲；section 只诊断/极端救急；post_fusion_measure 只复测不追满；"
            "profile 只用于解释，不作为决策核心。"
        ),
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
        f"section_diag={sec.get('diagnostic_event_count', sec.get('event_count', 0))}; "
        f"section_audio={sec.get('audio_event_count', 0)}; "
        f"final_gap={report.get('post_fusion_measure', {}).get('final_active_gap_db')}"
    )


if __name__ == "__main__":
    main()
