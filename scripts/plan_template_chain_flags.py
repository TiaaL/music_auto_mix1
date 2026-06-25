#!/usr/bin/env python3
"""从 resolved mix plan 读取模板链的按歌调整开关。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def source_overrides(plan: dict[str, Any]) -> dict[str, Any]:
    return (plan.get("source_cleanup") or {}) or ((plan.get("reference") or {}).get("overrides") or {})


def timbre_upper_delta(plan: dict[str, Any]) -> float | None:
    source_eq = (source_overrides(plan).get("source_eq") or {})
    timbre_eq = source_eq.get("timbre_vocal_eq") or {}
    for action in timbre_eq.get("actions") or []:
        if action.get("band") != "upper":
            continue
        evidence = action.get("evidence") or {}
        delta = evidence.get("delta_db")
        if isinstance(delta, (int, float)):
            return float(delta)
    ref = ((plan.get("reference") or {}).get("timbre_features") or {}).get("vocal_tonal_balance") or {}
    inp = ((plan.get("reference") or {}).get("input_features") or {}).get("vocal_tonal_balance") or {}
    if isinstance(ref.get("upper"), (int, float)) and isinstance(inp.get("upper"), (int, float)):
        return float(ref["upper"]) - float(inp["upper"])
    return None


def reference_vocal_balance_db(plan: dict[str, Any]) -> float | None:
    """读取原曲 stem 的 active 人声-伴奏比例，作为模板链改动的硬约束。"""
    bus = (((plan.get("reference") or {}).get("overrides") or {}).get("bus_balance") or {})
    value = bus.get("reference_vocal_minus_accomp_db")
    return float(value) if isinstance(value, (int, float)) else None


def skip_oneknob_brighter(plan: dict[str, Any]) -> bool:
    """判断是否跳过模板 C 的 brighter。

    新 plan 统一由 vocal_processing_context 决策；下面的旧逻辑只兼容历史 plan。
    """
    context = plan.get("vocal_processing_context") or {}
    chain = context.get("template_chain") or {}
    if isinstance(chain.get("skip_oneknob_brighter"), bool):
        return bool(chain["skip_oneknob_brighter"])

    delta = timbre_upper_delta(plan)
    ref_balance = reference_vocal_balance_db(plan)
    if ref_balance is not None and ref_balance >= -1.2:
        return False
    if delta is not None and delta <= -4.0:
        return True
    hf = source_overrides(plan).get("vocal_hf_guard") or {}
    peakiness = hf.get("peakiness") or {}
    upper_peak = float(peakiness.get("upper") or 0.0)
    if delta is not None and delta <= -2.0 and upper_peak >= 14.0:
        return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--flag", choices=("skip-oneknob-brighter",), required=True)
    args = parser.parse_args()

    plan = load_json(args.plan)
    if args.flag == "skip-oneknob-brighter":
        print("1" if skip_oneknob_brighter(plan) else "0")


if __name__ == "__main__":
    main()
