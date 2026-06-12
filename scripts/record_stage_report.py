#!/usr/bin/env python3
"""Append timing and LUFS/true-peak measurements for one render stage."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path


def command_path(name: str) -> str:
    found = shutil.which(name)
    return found or name


def loudness(path: Path) -> dict[str, float | str]:
    proc = subprocess.run(
        [
            command_path("ffmpeg"),
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            "loudnorm=I=-23.0:TP=-2.0:LRA=11.0:print_format=json",
            "-f",
            "null",
            "-",
        ],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr[-500:]}
    match = re.search(r"\{[\s\S]*?\}", proc.stderr)
    if not match:
        return {"error": "loudnorm JSON not found"}
    raw = json.loads(match.group(0))
    return {
        "i_lufs": float(raw["input_i"]),
        "tp_db": float(raw["input_tp"]),
        "lra": float(raw["input_lra"]),
    }


def load_report(path: Path) -> dict:
    if not path.exists():
        return {"stages": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        data = {"stages": []}
    if "stages" not in data or not isinstance(data["stages"], list):
        data["stages"] = []
    return data


def labeled_path(value: str) -> tuple[str, Path]:
    if "=" in value:
        label, path = value.split("=", 1)
        return label, Path(path)
    return "main", Path(value)


def measured_paths(values: list[str] | None) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    for value in values or []:
        label, path = labeled_path(value)
        if path.exists():
            out[label] = {"path": str(path), "loudness": loudness(path)}
        else:
            out[label] = {"path": str(path), "loudness": {"error": "file not found"}}
    return out


def short_loudness(group: dict[str, dict[str, object]]) -> str:
    parts: list[str] = []
    for label, info in group.items():
        loud = info.get("loudness", {})
        if isinstance(loud, dict):
            parts.append(f"{label} {loud.get('i_lufs', 'n/a')} LUFS/{loud.get('tp_db', 'n/a')} dBTP")
    return "; ".join(parts) if parts else "n/a"


def main() -> None:
    parser = argparse.ArgumentParser(description="Append one stage timing/loudness row.")
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--elapsed-sec", type=float, required=True)
    parser.add_argument("--input", action="append", default=[], help="Path or label=path. May be repeated.")
    parser.add_argument("--output", action="append", default=[], help="Path or label=path. May be repeated.")
    args = parser.parse_args()

    row: dict[str, object] = {
        "stage": args.stage,
        "elapsed_sec": round(args.elapsed_sec, 3),
    }
    row["inputs"] = measured_paths(args.input)
    row["outputs"] = measured_paths(args.output)

    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    report = load_report(args.metadata)
    report["stages"].append(row)
    report["total_measured_stage_sec"] = round(
        sum(float(stage.get("elapsed_sec") or 0.0) for stage in report["stages"]),
        3,
    )
    args.metadata.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "[stage-report] "
        f"{args.stage}: {row['elapsed_sec']:.3f}s "
        f"in [{short_loudness(row['inputs'])}] "
        f"-> out [{short_loudness(row['outputs'])}]"
    )


if __name__ == "__main__":
    main()
