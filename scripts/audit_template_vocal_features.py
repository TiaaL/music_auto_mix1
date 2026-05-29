#!/usr/bin/env python3
"""Audit whether a selected template vocal chain corrected classifier features."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ANALYZER = Path(r"D:\code\spectral-mix-template-selector\spectrum_template_analyzer.py")
DEFAULT_TARGETS = ROOT / "config" / "template_feature_targets.json"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_analyzer(analyzer_python: str, analyzer_script: Path, audio_path: Path) -> dict[str, Any]:
    cmd = [analyzer_python, str(analyzer_script), str(audio_path)]
    proc = subprocess.run(
        cmd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(
            "Analyzer failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Analyzer did not output valid JSON: {exc}\nOutput:\n{proc.stdout}") from exc


def selected_template(analysis: dict[str, Any]) -> str:
    label = str(analysis.get("classification", {}).get("label") or "").lower()
    return label if label in {"template_a", "template_b", "template_c"} else "template_d"


def feature_value(analysis: dict[str, Any], feature: str) -> float | None:
    if feature in analysis.get("ratios", {}):
        return float(analysis["ratios"][feature])
    if feature in analysis.get("diagnostic_ratios", {}):
        return float(analysis["diagnostic_ratios"][feature])
    if feature in analysis.get("group_ratios", {}):
        return float(analysis["group_ratios"][feature])
    if feature in analysis.get("body_presence_diagnosis", {}):
        value = analysis["body_presence_diagnosis"][feature]
        return float(value) if isinstance(value, (int, float)) else None
    if feature in analysis:
        value = analysis[feature]
        return float(value) if isinstance(value, (int, float)) else None
    return None


def ratio_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for name, before_value in before.get("ratios", {}).items():
        if name not in after.get("ratios", {}):
            continue
        b = float(before_value)
        a = float(after["ratios"][name])
        out[name] = {
            "before": round(b, 6),
            "after": round(a, 6),
            "delta": round(a - b, 6),
            "delta_percent_points": round((a - b) * 100.0, 3),
        }
    return out


def range_status(value: float, low: float, high: float) -> str:
    if value < low:
        return "low"
    if value > high:
        return "high"
    return "ok"


def band_balance(after: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    ranges = targets["neutral_ratio_ranges"]
    rows = []
    for band, value in after.get("ratios", {}).items():
        if band not in ranges:
            continue
        low, high = ranges[band]
        status = range_status(float(value), float(low), float(high))
        rows.append(
            {
                "band": band,
                "after": round(float(value), 6),
                "target_low": low,
                "target_high": high,
                "status": status,
            }
        )
    return rows


def diagnostic_band_balance(after: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    ranges = targets.get("diagnostic_ratio_ranges", {})
    rows = []
    for band, value in after.get("diagnostic_ratios", {}).items():
        if band not in ranges:
            continue
        low, high = ranges[band]
        status = range_status(float(value), float(low), float(high))
        rows.append(
            {
                "band": band,
                "after": round(float(value), 6),
                "target_low": low,
                "target_high": high,
                "status": status,
            }
        )
    return rows


def spectral_deviation_rows(after: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for band, data in after.get("spectral_deviation", {}).items():
        rows.append(
            {
                "band": band,
                "actual_db": round(float(data.get("actual_db", 0.0)), 3),
                "target_db": round(float(data.get("target_db", 0.0)), 3),
                "deviation_db": round(float(data.get("deviation_db", 0.0)), 3),
                "action": str(data.get("action", "ok")),
                "suggested_db": round(float(data.get("suggested_db", 0.0)), 3),
            }
        )
    return rows


def objective_results(template_id: str, before: dict[str, Any], after: dict[str, Any], targets: dict[str, Any]) -> list[dict[str, Any]]:
    objective = targets["template_objectives"].get(template_id, {})
    rows = []
    for direction in ("reduce", "increase"):
        for feature in objective.get(direction, []):
            b = feature_value(before, feature)
            a = feature_value(after, feature)
            if b is None or a is None:
                continue
            delta = a - b
            passed = delta < 0 if direction == "reduce" else delta > 0
            rows.append(
                {
                    "feature": feature,
                    "goal": direction,
                    "before": round(b, 6),
                    "after": round(a, 6),
                    "delta": round(delta, 6),
                    "passed": passed,
                }
            )
    return rows


def suggestions(
    template_id: str,
    objective_rows: list[dict[str, Any]],
    balance_rows: list[dict[str, Any]],
    diagnostic_rows: list[dict[str, Any]],
    deviation_rows: list[dict[str, Any]],
    body_presence: dict[str, Any],
    targets: dict[str, Any],
) -> list[str]:
    hints = targets.get("processor_hints", {})
    template = targets.get("template_objectives", {}).get(template_id, {})
    primary = ", ".join(template.get("primary_processors", []))
    out = []
    failed = [row for row in objective_rows if not row["passed"]]
    for row in failed:
        hint = hints.get(row["feature"], row["feature"])
        if row["goal"] == "reduce":
            out.append(f"{row['feature']} did not decrease enough; adjust {hint}. Primary processors: {primary}.")
        else:
            out.append(f"{row['feature']} did not increase enough; adjust {hint}. Primary processors: {primary}.")
    for row in balance_rows:
        if row["status"] == "high":
            out.append(f"{row['band']} remains high after processing; consider reducing {hints.get(row['band'], row['band'])}.")
        elif row["status"] == "low":
            out.append(f"{row['band']} is low after processing; avoid further cuts or add {hints.get(row['band'], row['band'])}.")
    for row in diagnostic_rows:
        if row["status"] == "high":
            out.append(f"{row['band']} diagnostic band is high; consider {hints.get(row['band'], row['band'])}.")
        elif row["status"] == "low":
            out.append(f"{row['band']} diagnostic band is low; consider {hints.get(row['band'], row['band'])}.")
    for row in deviation_rows:
        if row["action"] in {"cut", "boost"}:
            out.append(
                f"{row['band']} deviates from target curve by {row['deviation_db']} dB; "
                f"{row['action']} about {row['suggested_db']} dB via {hints.get(row['band'], row['band'])}."
            )
        elif row["action"] == "low_content":
            out.append(f"{row['band']} has very low analyzable content; do not apply blind boost without listening.")
    if body_presence:
        action = body_presence.get("action")
        if action and action != "balanced":
            out.append(
                "Body/presence diagnosis: "
                f"{action} (body {body_presence.get('body_level_db')} dB, "
                f"presence {body_presence.get('presence_level_db')} dB)."
            )
    if not out:
        out.append("Processed vocal features are within the current heuristic target ranges.")
    return out


def write_markdown(path: Path, audit: dict[str, Any]) -> None:
    lines = [
        "# Template Vocal Feature Audit",
        "",
        f"- Selected template: `{audit['selected_template']}`",
        f"- Processed vocal: `{audit['processed_vocal']}`",
        f"- Objective: {audit['template_objective'].get('description', '')}",
        "",
        "## Template Objectives",
        "",
        "| Feature | Goal | Before | After | Delta | Pass |",
        "| --- | --- | ---: | ---: | ---: | --- |",
    ]
    for row in audit["objective_results"]:
        lines.append(
            f"| {row['feature']} | {row['goal']} | {row['before']} | {row['after']} | {row['delta']} | {row['passed']} |"
        )
    lines.extend(
        [
            "",
            "## Band Balance After Processing",
            "",
            "| Band | After Ratio | Target Low | Target High | Status |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in audit["band_balance"]:
        lines.append(
            f"| {row['band']} | {row['after']} | {row['target_low']} | {row['target_high']} | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "## Diagnostic Bands",
            "",
            "| Band | After Ratio | Target Low | Target High | Status |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    for row in audit["diagnostic_band_balance"]:
        lines.append(
            f"| {row['band']} | {row['after']} | {row['target_low']} | {row['target_high']} | {row['status']} |"
        )
    lines.extend(
        [
            "",
            "## Target Curve Deviation",
            "",
            "| Band | Actual dB | Target dB | Deviation dB | Action | Suggested dB |",
            "| --- | ---: | ---: | ---: | --- | ---: |",
        ]
    )
    for row in audit["spectral_deviation"]:
        lines.append(
            f"| {row['band']} | {row['actual_db']} | {row['target_db']} | {row['deviation_db']} | {row['action']} | {row['suggested_db']} |"
        )
    lines.extend(["", "## Suggestions", ""])
    for item in audit["suggestions"]:
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare classifier vocal features before/after template vocal processing.")
    parser.add_argument("--analysis-json", type=Path, required=True)
    parser.add_argument("--processed-vocal", type=Path, required=True)
    parser.add_argument("--analyzer", type=Path, default=DEFAULT_ANALYZER)
    parser.add_argument("--analyzer-python", default=sys.executable)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    before = load_json(args.analysis_json)
    after = run_analyzer(args.analyzer_python, args.analyzer, args.processed_vocal)
    template_id = selected_template(before)
    targets = load_json(args.targets)
    objective_rows = objective_results(template_id, before, after, targets)
    balance_rows = band_balance(after, targets)
    diagnostic_rows = diagnostic_band_balance(after, targets)
    deviation_rows = spectral_deviation_rows(after)
    body_presence = after.get("body_presence_diagnosis", {})
    audit = {
        "selected_template": template_id,
        "processed_vocal": str(args.processed_vocal),
        "template_objective": targets.get("template_objectives", {}).get(template_id, {}),
        "before_analysis": before,
        "after_analysis": after,
        "ratio_deltas": ratio_deltas(before, after),
        "objective_results": objective_rows,
        "band_balance": balance_rows,
        "diagnostic_band_balance": diagnostic_rows,
        "spectral_deviation": deviation_rows,
        "body_presence_diagnosis": body_presence,
        "suggestions": suggestions(template_id, objective_rows, balance_rows, diagnostic_rows, deviation_rows, body_presence, targets),
    }
    args.out_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.out_dir / "vocal_feature_audit.json"
    md_path = args.out_dir / "vocal_feature_audit.md"
    write_json(json_path, audit)
    write_markdown(md_path, audit)
    print(json.dumps({"audit_json": str(json_path), "audit_report": str(md_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
