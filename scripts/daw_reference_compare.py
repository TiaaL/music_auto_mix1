#!/usr/bin/env python3
"""Compare Faust stage renders against DAW/Cubase reference WAVs.

This script is intentionally analysis-only: it does not tune DSP parameters.
It creates a repeatable report so parameter edits can be judged against the
same DAW truth stages.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

try:
    from scipy import signal
except Exception:  # pragma: no cover - optional but available in the project env
    signal = None


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "daw_calibration_stages.json"
DEFAULT_REFERENCE_ROOT = Path(r"D:\cubase\project\ai_cover\Mixdown\mix_results")

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


@dataclass(frozen=True)
class StageConfig:
    branch: str
    stage_id: str
    reference: str
    faust_stage: str | None
    status: str
    notes: str | None = None


def db(value: float, floor: float = -160.0) -> float:
    if value <= 0 or not math.isfinite(value):
        return floor
    return 20.0 * math.log10(value)


def read_config(path: Path) -> tuple[Path, dict[str, StageConfig]]:
    if not path.exists():
        return DEFAULT_REFERENCE_ROOT, {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    reference_root = Path(raw.get("reference_root") or DEFAULT_REFERENCE_ROOT)
    stages: dict[str, StageConfig] = {}
    for branch, branch_info in raw.get("branches", {}).items():
        for stage in branch_info.get("stages", []):
            reference = str(stage["reference"]).replace("\\", "/")
            stages[reference.lower()] = StageConfig(
                branch=branch,
                stage_id=str(stage["stage_id"]),
                reference=reference,
                faust_stage=stage.get("faust_stage"),
                status=str(stage.get("status", "unknown")),
                notes=stage.get("notes"),
            )
    return reference_root, stages


def audio_info(path: Path) -> dict[str, Any]:
    info = sf.info(str(path))
    return {
        "path": str(path),
        "sample_rate": int(info.samplerate),
        "channels": int(info.channels),
        "frames": int(info.frames),
        "duration_seconds": float(info.duration),
        "format": info.format,
        "subtype": info.subtype,
    }


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(path), always_2d=True, dtype="float64")
    if data.size == 0:
        raise ValueError(f"empty audio file: {path}")
    data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
    return data, int(sr)


def to_mono(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        return data
    return data.mean(axis=1)


def match_channels(candidate: np.ndarray, reference: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cand_ch = candidate.shape[1]
    ref_ch = reference.shape[1]
    if cand_ch == ref_ch:
        return candidate, reference
    if cand_ch == 1 and ref_ch == 2:
        return np.repeat(candidate, 2, axis=1), reference
    if cand_ch == 2 and ref_ch == 1:
        return candidate.mean(axis=1, keepdims=True), reference
    channels = min(cand_ch, ref_ch)
    return candidate[:, :channels], reference[:, :channels]


def resample_if_needed(data: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr:
        return data
    if signal is None:
        raise ValueError(f"sample-rate mismatch and scipy unavailable: {src_sr} -> {dst_sr}")
    gcd = math.gcd(src_sr, dst_sr)
    up = dst_sr // gcd
    down = src_sr // gcd
    return signal.resample_poly(data, up, down, axis=0)


def basic_stats(data: np.ndarray) -> dict[str, float]:
    abs_data = np.abs(data)
    peak = float(np.max(abs_data))
    rms = float(np.sqrt(np.mean(np.square(data))))
    frame = frame_rms(to_mono(data), 2048, 1024)
    active = frame[frame > 1e-7]
    if active.size:
        dyn = db(float(np.percentile(active, 95))) - db(float(np.percentile(active, 10)))
    else:
        dyn = 0.0
    return {
        "peak_dbfs": round(db(peak), 3),
        "rms_dbfs": round(db(rms), 3),
        "crest_db": round(db(peak) - db(rms), 3),
        "dynamic_range_db": round(float(dyn), 3),
    }


def frame_rms(x: np.ndarray, frame_size: int, hop: int) -> np.ndarray:
    if x.size < frame_size:
        return np.array([float(np.sqrt(np.mean(np.square(x))))])
    frames = []
    for start in range(0, x.size - frame_size + 1, hop):
        frame = x[start : start + frame_size]
        frames.append(float(np.sqrt(np.mean(np.square(frame)))))
    return np.asarray(frames)


def estimate_lag(candidate: np.ndarray, reference: np.ndarray, sr: int, max_lag_ms: float) -> tuple[int, float]:
    cand = to_mono(candidate)
    ref = to_mono(reference)
    limit = min(cand.size, ref.size, int(sr * 60.0))
    cand = cand[:limit]
    ref = ref[:limit]
    stride = max(1, sr // 4000)
    cand = cand[::stride] - float(np.mean(cand[::stride]))
    ref = ref[::stride] - float(np.mean(ref[::stride]))
    max_lag = max(1, int(max_lag_ms * 0.001 * sr / stride))
    if cand.size < 2 or ref.size < 2:
        return 0, 0.0
    if signal is not None:
        corr = signal.correlate(cand, ref, mode="full", method="fft")
    else:
        corr = np.correlate(cand, ref, mode="full")
    lags = np.arange(-ref.size + 1, cand.size)
    mask = (lags >= -max_lag) & (lags <= max_lag)
    if not np.any(mask):
        return 0, 0.0
    local_corr = corr[mask]
    local_lags = lags[mask]
    best = int(local_lags[int(np.argmax(np.abs(local_corr)))])
    lag_samples = int(best * stride)
    lag_ms = lag_samples * 1000.0 / sr
    return lag_samples, float(lag_ms)


def align_by_lag(candidate: np.ndarray, reference: np.ndarray, lag_samples: int) -> tuple[np.ndarray, np.ndarray]:
    if lag_samples > 0:
        candidate = candidate[lag_samples:]
    elif lag_samples < 0:
        reference = reference[-lag_samples:]
    n = min(candidate.shape[0], reference.shape[0])
    return candidate[:n], reference[:n]


def spectrum_profile(data: np.ndarray, sr: int) -> dict[str, float]:
    x = to_mono(data)
    max_samples = min(x.size, sr * 90)
    x = x[:max_samples]
    if x.size < 16:
        return {name: -160.0 for name, _, _ in BANDS}
    x = x - float(np.mean(x))
    window = np.hanning(x.size)
    mag = np.abs(np.fft.rfft(x * window)) + 1e-12
    freqs = np.fft.rfftfreq(x.size, 1.0 / sr)
    profile: dict[str, float] = {}
    for name, low, high in BANDS:
        mask = (freqs >= low) & (freqs < min(high, sr / 2.0))
        profile[name] = round(db(float(np.mean(mag[mask]))) if np.any(mask) else -160.0, 3)
    return profile


def compare_spectrum(candidate: np.ndarray, reference: np.ndarray, sr: int) -> dict[str, Any]:
    cand_profile = spectrum_profile(candidate, sr)
    ref_profile = spectrum_profile(reference, sr)
    bands = {}
    diffs = []
    for name, _, _ in BANDS:
        delta = cand_profile[name] - ref_profile[name]
        bands[name] = {
            "candidate_db": cand_profile[name],
            "reference_db": ref_profile[name],
            "delta_db": round(delta, 3),
        }
        diffs.append(abs(delta))
    worst = max(bands.items(), key=lambda item: abs(item[1]["delta_db"]))
    return {
        "band_mae_db": round(float(np.mean(diffs)), 3),
        "worst_band": worst[0],
        "bands": bands,
    }


def corrcoef(candidate: np.ndarray, reference: np.ndarray) -> float:
    x = to_mono(candidate)
    y = to_mono(reference)
    if x.size < 2 or y.size < 2:
        return 0.0
    x = x - float(np.mean(x))
    y = y - float(np.mean(y))
    denom = float(np.linalg.norm(x) * np.linalg.norm(y))
    if denom <= 0:
        return 0.0
    return float(np.dot(x, y) / denom)


def score(metrics: dict[str, Any]) -> float:
    deltas = metrics["delta"]
    spectral = metrics["spectrum"]["band_mae_db"]
    corr_penalty = max(0.0, 1.0 - abs(metrics["correlation"])) * 25.0
    penalty = (
        spectral * 3.5
        + abs(deltas["rms_db"]) * 3.0
        + abs(deltas["peak_db"]) * 1.25
        + abs(deltas["dynamic_range_db"]) * 1.5
        + min(abs(metrics["lag_ms"]) / 10.0, 8.0)
        + corr_penalty
    )
    return round(max(0.0, 100.0 - penalty), 2)


def compare_pair(candidate_path: Path, reference_path: Path, max_lag_ms: float) -> dict[str, Any]:
    ref, ref_sr = load_audio(reference_path)
    cand, cand_sr = load_audio(candidate_path)
    cand = resample_if_needed(cand, cand_sr, ref_sr)
    cand, ref = match_channels(cand, ref)
    lag_samples, lag_ms = estimate_lag(cand, ref, ref_sr, max_lag_ms)
    cand_aligned, ref_aligned = align_by_lag(cand, ref, lag_samples)
    cand_aligned, ref_aligned = match_channels(cand_aligned, ref_aligned)
    cand_stats = basic_stats(cand_aligned)
    ref_stats = basic_stats(ref_aligned)
    spectrum = compare_spectrum(cand_aligned, ref_aligned, ref_sr)
    metrics = {
        "candidate": str(candidate_path),
        "reference": str(reference_path),
        "sample_rate": ref_sr,
        "compared_frames": int(cand_aligned.shape[0]),
        "lag_samples": lag_samples,
        "lag_ms": round(lag_ms, 3),
        "correlation": round(corrcoef(cand_aligned, ref_aligned), 5),
        "candidate_stats": cand_stats,
        "reference_stats": ref_stats,
        "delta": {
            "peak_db": round(cand_stats["peak_dbfs"] - ref_stats["peak_dbfs"], 3),
            "rms_db": round(cand_stats["rms_dbfs"] - ref_stats["rms_dbfs"], 3),
            "dynamic_range_db": round(cand_stats["dynamic_range_db"] - ref_stats["dynamic_range_db"], 3),
        },
        "spectrum": spectrum,
    }
    metrics["score"] = score(metrics)
    return metrics


def build_manifest(reference_root: Path, stage_configs: dict[str, StageConfig]) -> list[dict[str, Any]]:
    entries = []
    for path in sorted(reference_root.rglob("*.wav")):
        rel = path.relative_to(reference_root).as_posix()
        cfg = stage_configs.get(rel.lower())
        branch = cfg.branch if cfg else path.parent.name
        stage_id = cfg.stage_id if cfg else path.stem
        entry = audio_info(path)
        entry.update(
            {
                "relative_path": rel,
                "branch": branch,
                "stage_id": stage_id,
                "faust_stage": cfg.faust_stage if cfg else None,
                "mapping_status": cfg.status if cfg else "unconfigured",
                "notes": cfg.notes if cfg else None,
            }
        )
        entries.append(entry)
    return entries


def find_candidate(candidate_root: Path, entry: dict[str, Any]) -> Path | None:
    rel = Path(entry["relative_path"])
    candidates = [
        candidate_root / rel,
        candidate_root / entry["branch"] / f"{entry['stage_id']}.wav",
        candidate_root / f"{entry['stage_id']}.wav",
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


def write_markdown(path: Path, manifest: list[dict[str, Any]], comparisons: list[dict[str, Any]]) -> None:
    lines = [
        "# DAW Reference Compare Report",
        "",
        f"- Reference stages: {len(manifest)}",
        f"- Compared stages: {len(comparisons)}",
        "",
    ]
    if comparisons:
        lines.extend(
            [
                "## Scores",
                "",
                "| Stage | Score | RMS Delta dB | Peak Delta dB | Spectrum MAE dB | Worst Band | Lag ms | Corr |",
                "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |",
            ]
        )
        for item in sorted(comparisons, key=lambda row: row["metrics"]["score"]):
            m = item["metrics"]
            lines.append(
                "| {stage} | {score:.2f} | {rms:.3f} | {peak:.3f} | {spec:.3f} | {band} | {lag:.3f} | {corr:.5f} |".format(
                    stage=item["stage_id"],
                    score=m["score"],
                    rms=m["delta"]["rms_db"],
                    peak=m["delta"]["peak_db"],
                    spec=m["spectrum"]["band_mae_db"],
                    band=m["spectrum"]["worst_band"],
                    lag=m["lag_ms"],
                    corr=m["correlation"],
                )
            )
        lines.append("")
    missing = [entry for entry in manifest if not entry.get("candidate_found")]
    if missing:
        lines.extend(["## Missing Candidates", ""])
        for entry in missing:
            lines.append(
                f"- `{entry['relative_path']}` -> expected `{entry['branch']}/{entry['stage_id']}.wav` "
                f"({entry['mapping_status']})"
            )
        lines.append("")
    lines.extend(["## Reference Manifest", ""])
    for entry in manifest:
        lines.append(
            "- `{rel}`: {dur:.3f}s, {sr} Hz, {ch} ch, mapping `{status}`".format(
                rel=entry["relative_path"],
                dur=entry["duration_seconds"],
                sr=entry["sample_rate"],
                ch=entry["channels"],
                status=entry["mapping_status"],
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare Faust stage outputs against DAW reference stages.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--reference-root", type=Path, default=None)
    parser.add_argument("--candidate-root", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "calibration_outputs" / "daw_reference_compare")
    parser.add_argument("--max-lag-ms", type=float, default=80.0)
    args = parser.parse_args()

    config_reference_root, stage_configs = read_config(args.config)
    reference_root = args.reference_root or config_reference_root
    if not reference_root.exists():
        raise SystemExit(f"Reference root not found: {reference_root}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(reference_root, stage_configs)
    comparisons = []
    if args.candidate_root:
        if not args.candidate_root.exists():
            raise SystemExit(f"Candidate root not found: {args.candidate_root}")
        for entry in manifest:
            candidate = find_candidate(args.candidate_root, entry)
            entry["candidate_found"] = candidate is not None
            if candidate is None:
                continue
            try:
                metrics = compare_pair(candidate, Path(entry["path"]), args.max_lag_ms)
            except Exception as exc:
                comparisons.append(
                    {
                        "branch": entry["branch"],
                        "stage_id": entry["stage_id"],
                        "relative_path": entry["relative_path"],
                        "candidate": str(candidate),
                        "reference": entry["path"],
                        "error": str(exc),
                    }
                )
                continue
            comparisons.append(
                {
                    "branch": entry["branch"],
                    "stage_id": entry["stage_id"],
                    "relative_path": entry["relative_path"],
                    "mapping_status": entry["mapping_status"],
                    "metrics": metrics,
                }
            )
    else:
        for entry in manifest:
            entry["candidate_found"] = False

    manifest_path = args.out_dir / "daw_reference_manifest.json"
    comparison_path = args.out_dir / "daw_reference_compare.json"
    markdown_path = args.out_dir / "daw_reference_compare.md"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    comparison_path.write_text(json.dumps(comparisons, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(markdown_path, manifest, [row for row in comparisons if "metrics" in row])
    print(
        json.dumps(
            {
                "reference_root": str(reference_root),
                "candidate_root": str(args.candidate_root) if args.candidate_root else None,
                "manifest": str(manifest_path),
                "comparison": str(comparison_path),
                "report": str(markdown_path),
                "reference_count": len(manifest),
                "compared_count": len([row for row in comparisons if "metrics" in row]),
                "error_count": len([row for row in comparisons if "error" in row]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
