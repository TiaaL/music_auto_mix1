#!/usr/bin/env python3
"""Match post-FX vocal/accomp bus levels to reference stem active-region RMS."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from analyze_reference import (  # noqa: E402
    active_intervals_from_vocal,
    load_audio_as_float,
    measure_loudness,
    rms_db_for_intervals,
    to_mono,
)

BUS_MAX_GAIN_DB = 12.0
BUS_MAX_ATTEN_DB = 12.0
BUS_DEAD_BAND_DB = 0.4
BUS_RATIO_MAX_VOCAL_GAIN_DB = 3.0
BUS_RATIO_MAX_ACCOMP_ATTEN_DB = 2.0
BUS_RATIO_MAX_CORRECTION_DB = 4.8


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def reference_levels(plan: dict | None) -> dict[str, Any]:
    if not plan:
        return {}
    return (
        (plan.get("reference") or {})
        .get("features", {})
        .get("vocal_accomp_balance", {})
    ) or {}


def measure_render_balance(vocal_group: Path, accomp_bus: Path) -> dict[str, float | int]:
    vocal_audio, sr = load_audio_as_float(vocal_group)
    accomp_audio, _ = load_audio_as_float(accomp_bus)
    n = min(vocal_audio.shape[0], accomp_audio.shape[0])
    vocal_audio = vocal_audio[:n]
    accomp_audio = accomp_audio[:n]
    active_regions = active_intervals_from_vocal(vocal_audio, sr)
    vocal_active_rms = rms_db_for_intervals(to_mono(vocal_audio), sr, active_regions)
    accomp_active_rms = rms_db_for_intervals(to_mono(accomp_audio), sr, active_regions)
    vocal_lufs = measure_loudness(vocal_group)["lufs_i"]
    accomp_lufs = measure_loudness(accomp_bus)["lufs_i"]
    return {
        "vocal_lufs_i": round(vocal_lufs, 2),
        "accomp_lufs_i": round(accomp_lufs, 2),
        "vocal_minus_accomp_lufs_db": round(vocal_lufs - accomp_lufs, 2),
        "active_vocal_rms_db": round(vocal_active_rms, 3),
        "active_accomp_rms_db": round(accomp_active_rms, 3),
        "active_vocal_minus_accomp_db": round(vocal_active_rms - accomp_active_rms, 2),
        "active_region_count": len(active_regions),
    }


def apply_dead_band(gain_db: float) -> float:
    if abs(gain_db) < BUS_DEAD_BAND_DB:
        return 0.0
    return round(gain_db, 2)


def compute_bus_gains(ref_balance: dict[str, Any], measured: dict[str, float | int]) -> dict[str, Any]:
    vocal_gain = 0.0
    accomp_gain = 0.0
    reason = "no reference active vocal/accomp ratio; buses unchanged"

    ref_gap_value = ref_balance.get("active_vocal_minus_accomp_db")
    if ref_gap_value is None:
        ref_gap_value = ref_balance.get("vocal_minus_accomp_db")
    render_gap = float(measured["active_vocal_minus_accomp_db"])

    if ref_gap_value is not None:
        ref_gap = float(ref_gap_value)
        correction = clamp(ref_gap - render_gap, -BUS_RATIO_MAX_CORRECTION_DB, BUS_RATIO_MAX_CORRECTION_DB)
        if correction > 0.0:
            vocal_gain = apply_dead_band(clamp(correction * 0.60, 0.0, BUS_RATIO_MAX_VOCAL_GAIN_DB))
            accomp_gain = apply_dead_band(clamp(-correction * 0.40, -BUS_RATIO_MAX_ACCOMP_ATTEN_DB, 0.0))
        else:
            vocal_gain = apply_dead_band(clamp(correction * 0.60, -BUS_MAX_ATTEN_DB, 0.0))
            accomp_gain = 0.0
        predicted_gap = round(render_gap + vocal_gain - accomp_gain, 2)
        reason = (
            f"match reference active vocal/accomp ratio conservatively: "
            f"render gap {render_gap:+.1f} dB -> predicted {predicted_gap:+.1f} dB; "
            f"reference {ref_gap:+.1f} dB; correction capped to {correction:+.1f} dB."
        )

    return {
        "vocal_bus_gain_db": vocal_gain,
        "accomp_bus_gain_db": accomp_gain,
        "reference_vocal_lufs_i": ref_balance.get("vocal_lufs"),
        "reference_accomp_lufs_i": ref_balance.get("accomp_lufs"),
        "reference_vocal_minus_accomp_lufs_db": ref_balance.get("vocal_minus_accomp_db"),
        "reference_active_vocal_minus_accomp_db": ref_gap_value,
        "policy": "match_reference_active_vocal_accomp_ratio_conservative",
        "measurement_basis": "post_fx_vocal_group_and_accomp_bus_active_vocal_regions",
        "reason": reason,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Compute bus balance from post-FX render buses.")
    parser.add_argument("vocal_group", type=Path, help="Post-FX vocal group WAV.")
    parser.add_argument("accomp_bus", type=Path, help="Post-FX accompaniment bus WAV.")
    parser.add_argument("--plan", type=Path, default=None, help="Resolved mix plan with reference features.")
    parser.add_argument("--metadata", type=Path, default=None, help="Optional JSON report path.")
    args = parser.parse_args()

    plan = load_json(args.plan) if args.plan and args.plan.exists() else None
    measured = measure_render_balance(args.vocal_group, args.accomp_bus)
    ref_balance = reference_levels(plan)
    bus = compute_bus_gains(ref_balance, measured)

    metadata = {**measured, **bus, "reference_balance": ref_balance}
    if args.metadata:
        args.metadata.parent.mkdir(parents=True, exist_ok=True)
        args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"[bus-balance] vocal {bus['vocal_bus_gain_db']:+.2f} dB, "
        f"accomp {bus['accomp_bus_gain_db']:+.2f} dB",
        file=sys.stderr,
    )
    print(f"[bus-balance] {bus['reason']}", file=sys.stderr)
    print(f"{bus['vocal_bus_gain_db']:.3f} {bus['accomp_bus_gain_db']:.3f}")


if __name__ == "__main__":
    main()
