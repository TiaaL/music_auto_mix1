#!/usr/bin/env python3
"""Build a per-song vocal_group_fx binary from a resolved spatial_fx plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
SRC_DSP = ROOT / "src" / "vocal_group_fx.dsp"
BUILD_DIR = ROOT / "build"
SPATIAL_DIR = BUILD_DIR / "spatial"

PARAM_TO_DSP = {
    "RVERB_SEND_PRE_DB": ("reverb", "send_pre_db"),
    "RVERB_PREDELAY_MS": ("reverb", "predelay_ms"),
    "RVERB_TIME_S": ("reverb", "time_s"),
    "RVERB_EARLY_REF_DB": ("reverb", "early_ref_db"),
    "RVERB_DAMP": ("reverb", "damp"),
    "RVERB_EQ_HI_GAIN_DB": ("reverb", "eq_hi_gain_db"),
    "OUTPUT_SIDE_TRIM_DB": ("output", "side_trim_db"),
    "SUPERTAP_SEND_PRE_DB": ("delay", "send_pre_db"),
    "SUPERTAP_GAIN_DB": ("delay", "gain_db"),
    "SUPERTAP_FEEDBACK": ("delay", "feedback"),
    "SUPERTAP_WIDTH": ("delay", "width"),
    "SUPERTAP_COLOR_HZ": ("delay", "color_hz"),
    "SHIMMER_SEND_PRE_DB": ("shimmer", "send_pre_db"),
    "SHIMMER_GAIN_DB": ("shimmer", "gain_db"),
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def spatial_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return ((plan.get("reference") or {}).get("overrides") or {}).get("spatial_fx") or {}


def extract_params(spatial: dict[str, Any]) -> dict[str, float]:
    params: dict[str, float] = {}
    for dsp_name, (section, key) in PARAM_TO_DSP.items():
        value = (spatial.get(section) or {}).get(key)
        if isinstance(value, (int, float)):
            params[dsp_name] = float(value)
    return params


def param_hash(params: dict[str, float]) -> str:
    payload = json.dumps(params, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def command_path(name: str) -> str:
    env_value = os.environ.get(name.upper())
    if env_value:
        return env_value
    found = shutil.which(name)
    if found:
        return found
    candidates = [
        ROOT / ".tools" / "faust-local" / "bin" / name,
        ROOT / ".tools" / "msys64" / "usr" / "bin" / f"{name}.exe",
        ROOT / ".tools" / "msys64" / "ucrt64" / "bin" / f"{name}.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return name


def replace_constants(source: str, params: dict[str, float]) -> str:
    out = source
    for name, value in params.items():
        replacement = f"{name:<20} = {value:.6g};"
        pattern = rf"^{re.escape(name)}\s*=.*?;"
        out, count = re.subn(pattern, replacement, out, count=1, flags=re.MULTILINE)
        if count != 1:
            raise RuntimeError(f"Could not replace DSP constant {name}")
    return out


def compile_binary(dsp_path: Path, cpp_path: Path, binary_path: Path) -> None:
    faust = command_path("faust")
    cxx = os.environ.get("CXX") or "clang++"
    arch_proc = subprocess.run([faust, "--archdir"], text=True, capture_output=True, check=False)
    if arch_proc.returncode != 0:
        raise RuntimeError(f"faust --archdir failed: {arch_proc.stderr}")
    archdir = arch_proc.stdout.strip()
    arch_sf = str(Path(archdir) / "sndfile.cpp")
    includes = os.environ.get("INCLUDES", f"-I/opt/homebrew/include -I{archdir}")
    cxxflags = os.environ.get("CXXFLAGS", "-O3 -ffast-math -DFILE_MODE=2")
    ldflags = os.environ.get("LDFLAGS", "-L/opt/homebrew/lib -lsndfile")

    faust_cmd = [faust, "-lang", "cpp", "-a", arch_sf, str(dsp_path), "-o", str(cpp_path)]
    proc = subprocess.run(faust_cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"faust compile failed:\n{proc.stdout}\n{proc.stderr}")

    cxx_cmd = [cxx, *cxxflags.split(), *includes.split(), str(cpp_path), *ldflags.split(), "-o", str(binary_path)]
    proc = subprocess.run(cxx_cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"C++ compile failed:\n{proc.stdout}\n{proc.stderr}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build per-song vocal_group_fx from spatial_fx plan.")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--metadata", type=Path)
    parser.add_argument("--mode", choices=("auto", "off"), default="auto")
    args = parser.parse_args()

    default_binary = BUILD_DIR / "vocal_group_fx"
    plan = load_json(args.plan)
    spatial = spatial_plan(plan)
    enabled = args.mode != "off" and bool(spatial.get("enabled")) and bool(spatial.get("applied_to_render"))
    if not enabled:
        report = {
            "enabled": False,
            "binary": str(default_binary),
            "reason": spatial.get("reason") or ("disabled_by_cli" if args.mode == "off" else "missing_or_disabled_plan"),
            "spatial_fx": spatial,
        }
        if args.metadata:
            write_json(args.metadata, report)
        print(default_binary)
        return

    params = extract_params(spatial)
    if not params:
        raise SystemExit("spatial_fx was enabled but contained no DSP parameters")

    digest = param_hash(params)
    SPATIAL_DIR.mkdir(parents=True, exist_ok=True)
    dsp_path = SPATIAL_DIR / f"vocal_group_fx_{digest}.dsp"
    cpp_path = SPATIAL_DIR / f"vocal_group_fx_{digest}.cpp"
    binary_path = SPATIAL_DIR / f"vocal_group_fx_{digest}"

    compiled = False
    if not binary_path.exists():
        generated = replace_constants(SRC_DSP.read_text(encoding="utf-8"), params)
        dsp_path.write_text(generated, encoding="utf-8")
        compile_binary(dsp_path, cpp_path, binary_path)
        compiled = True

    report = {
        "enabled": True,
        "binary": str(binary_path),
        "compiled": compiled,
        "hash": digest,
        "params": params,
        "spatial_fx": spatial,
        "plan": str(args.plan),
        "source_dsp": str(SRC_DSP),
    }
    if args.metadata:
        write_json(args.metadata, report)
    print(binary_path)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(1)
