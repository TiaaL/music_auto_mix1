#!/usr/bin/env python3
"""按 plan 对人声做轻量瑕疵修复。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def ffmpeg_filter(action: dict[str, Any]) -> str | None:
    kind = action.get("type")
    if kind == "adeclick":
        # adeclick 只修短促点击/毛刺；threshold 越大越保守。
        return (
            "adeclick="
            f"w={float(action.get('window', 40))}:"
            f"o={float(action.get('overlap', 75))}:"
            f"a={float(action.get('arorder', 4))}:"
            f"t={float(action.get('threshold', 2.5))}:"
            f"b={float(action.get('burst', 2))}"
        )
    if kind == "afftdn":
        # afftdn 这里只做轻度频谱平滑；nr 很低，避免把人声细节洗掉。
        return (
            "afftdn="
            f"nr={float(action.get('noise_reduction', 3.5))}:"
            f"nf={float(action.get('noise_floor', -56))}:"
            f"rf={float(action.get('residual_floor', -42))}:"
            f"ad={float(action.get('adaptivity', 0.35))}:"
            f"gs={int(action.get('gain_smooth', 10))}"
        )
    if kind == "afwtdn":
        # 严重受损时用于高频层的 wavelet 平滑，比继续加深 EQ 更自然。
        return (
            "afwtdn="
            f"sigma={float(action.get('sigma', 0.018))}:"
            f"levels={int(action.get('levels', 8))}:"
            f"percent={float(action.get('percent', 35))}:"
            f"softness={float(action.get('softness', 3.0))}:"
            f"samples={int(action.get('samples', 8192))}"
        )
    if kind == "deesser":
        # 只在严重受损档追加；作为最后一道边缘控制，避免金属/齿音继续扎耳。
        return (
            "deesser="
            f"i={float(action.get('intensity', 0.22))}:"
            f"m={float(action.get('max_deessing', 0.38))}:"
            f"f={float(action.get('frequency', 0.55))}"
        )
    return None


def repair_block(plan: dict[str, Any]) -> dict[str, Any]:
    source_cleanup = plan.get("source_cleanup") or {}
    if source_cleanup:
        return source_cleanup.get("vocal_artifact_repair") or {}
    # 兼容旧 plan：如果以后旧字段里也放了 repair，仍能读取。
    overrides = (plan.get("reference") or {}).get("overrides") or {}
    return overrides.get("vocal_artifact_repair") or {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Apply light vocal artifact repair from mix plan.")
    parser.add_argument("input_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, default=None)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()

    plan = load_json(args.plan)
    block = repair_block(plan)
    actions = block.get("actions", []) if block.get("enabled") else []
    filters = [value for action in actions if (value := ffmpeg_filter(action))]

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "enabled": bool(filters),
        "mode": block.get("mode", "inline_repair"),
        "crossover_hz": block.get("crossover_hz"),
        "high_layer_gain_db": block.get("high_layer_gain_db", 0.0),
        "actions": actions,
        "filters": filters,
        "trigger": block.get("trigger"),
        "reasons": block.get("reasons", []),
        "policy": block.get("policy"),
    }

    if not filters:
        shutil.copyfile(args.input_wav, args.output_wav)
        metadata["skipped"] = True
        metadata["reason"] = block.get("reason", "no repair actions")
        if args.metadata:
            args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        print("[vocal-artifact-repair] skipped")
        return

    mode = block.get("mode")
    if mode == "split_high_repair":
        # 严重受损时只处理高频层：低/中频主体保持原样，避免把人声洗薄。
        crossover_hz = float(block.get("crossover_hz") or 2600.0)
        high_layer_gain_db = float(block.get("high_layer_gain_db") or 0.0)
        high_chain = ",".join(filters)
        filter_complex = (
            f"[0:a]asplit=2[lo][hi];"
            f"[lo]lowpass=f={crossover_hz:.1f}:width_type=q:width=0.707[lo2];"
            f"[hi]highpass=f={crossover_hz:.1f}:width_type=q:width=0.707,{high_chain},"
            f"volume={high_layer_gain_db:.2f}dB[hi2];"
            "[lo2][hi2]amix=inputs=2:normalize=0[out]"
        )
        cmd = [
            args.ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(args.input_wav),
            "-filter_complex",
            filter_complex,
            "-map",
            "[out]",
            "-c:a",
            "pcm_f32le",
            str(args.output_wav),
        ]
    else:
        cmd = [
            args.ffmpeg,
            "-y",
            "-hide_banner",
            "-i",
            str(args.input_wav),
            "-af",
            ",".join(filters),
            "-c:a",
            "pcm_f32le",
            str(args.output_wav),
        ]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if proc.returncode != 0:
        raise SystemExit(
            "Vocal artifact repair failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    metadata["skipped"] = False
    if args.metadata:
        args.metadata.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[vocal-artifact-repair] applied {len(filters)} filter(s)")


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
