#!/usr/bin/env python3
"""Apply external DelayVerb as a pre-fader vocal-group send."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DELAYVERB_ROOT = Path(os.environ.get("DELAYVERB_ROOT", r"D:\code\delayverb\delayverb"))


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for candidate in (
        ROOT / ".tools" / "msys64" / "ucrt64" / "bin" / f"{name}.exe",
        ROOT / ".tools" / "msys64" / "usr" / "bin" / f"{name}.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return name


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", env=env)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def build_delayverb(delayverb_root: Path, binary: Path) -> None:
    if binary.exists():
        return
    make = command_path("make")
    env = os.environ.copy()
    cmd = [make, "-C", str(delayverb_root), "build/delay_reverb"]
    for key in ("FAUST", "CXX", "ARCHDIR", "INCLUDES", "LDFLAGS"):
        value = env.get(key)
        if value:
            cmd.append(f"{key}={value}")
    run(cmd, env=env)


def delayverb_binary(delayverb_root: Path) -> Path:
    candidates = [
        delayverb_root / "build" / "delay_reverb",
        delayverb_root / "build" / "delay_reverb.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def load_delayverb_auto(delayverb_root: Path):
    module_path = delayverb_root / "scripts" / "auto_reverb_from_reference.py"
    if not module_path.exists():
        raise SystemExit(f"DelayVerb auto reference script not found: {module_path}")
    spec = importlib.util.spec_from_file_location("delayverb_auto_reference", module_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"Could not load DelayVerb auto reference script: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_reference_preset(
    delayverb_root: Path,
    cover_dry: Path,
    original_mix: Path,
    original_vocal: Path,
    original_accomp: Path,
    work_dir: Path,
    sample_rate: int = 44100,
) -> Path:
    auto = load_delayverb_auto(delayverb_root)
    work_dir.mkdir(parents=True, exist_ok=True)
    cover_wav = work_dir / "cover_dry_for_reference.wav"
    mix_wav = work_dir / "original_mix.wav"
    dry_wav = work_dir / "original_vocal.wav"
    backing_wav = work_dir / "original_accomp.wav"
    preset_path = work_dir / "generated_delayverb_preset.json"
    metrics_path = work_dir / "delayverb_reference_metrics.json"

    auto.decode_to_wav(cover_dry, cover_wav, sample_rate, 2)
    auto.decode_to_wav(original_mix, mix_wav, sample_rate, 2)
    auto.decode_to_wav(original_vocal, dry_wav, sample_rate, 2)
    auto.decode_to_wav(original_accomp, backing_wav, sample_rate, 2)

    cover = auto.read_wav(cover_wav)
    mix = auto.read_wav(mix_wav)
    dry = auto.read_wav(dry_wav)
    backing = auto.read_wav(backing_wav)

    metrics = auto.solve_reference_space(mix, backing, dry, sample_rate)
    preset = auto.make_preset(metrics, cover, dry)
    # Clamp wet-path lowpass to keep delay/reverb returns from re-introducing
    # sibilance (6–8 kHz) into the vocal group on bright references.
    params = preset.get("params") if isinstance(preset, dict) else None
    if isinstance(params, dict):
        for key, cap in (
            ("wet_lowpass_hz", 6000.0),
            ("tap_a_lowpass_hz", 6500.0),
            ("tap_b_lowpass_hz", 5000.0),
        ):
            value = params.get(key)
            if isinstance(value, (int, float)) and value > cap:
                params[key] = cap
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    preset_path.write_text(json.dumps(preset, ensure_ascii=False, indent=2), encoding="utf-8")
    preset_params = preset.get("params") if isinstance(preset, dict) else {}
    delay_sync = preset_params.get("_delay_sync", {}) if isinstance(preset_params, dict) else {}
    space_profile = preset_params.get("_space_profile", {}) if isinstance(preset_params, dict) else {}
    tempo = metrics.get("tempo", {}) if isinstance(metrics, dict) else {}
    print(
        "[delayverb] reference preset: "
        f"wet_ratio={metrics['wet_ratio']:.3f}, "
        f"tail={metrics['tail_seconds']:.2f}s, "
        f"width={metrics['wet_width']:.3f}, "
        f"centroid={metrics['wet_centroid_hz']:.0f}Hz, "
        f"bpm={tempo.get('bpm', 'n/a')}, "
        f"space={space_profile.get('profile', 'n/a')}/"
        f"{space_profile.get('reverb_mode', 'n/a')}/"
        f"{space_profile.get('reverb_color', 'n/a')}, "
        f"rt={space_profile.get('effective_time_s', 'n/a')}s, "
        f"predelay={space_profile.get('effective_predelay_ms', 'n/a')}ms, "
        f"sync={delay_sync.get('sync_division', 'off')} "
        f"({delay_sync.get('delay_time_ms', 'n/a')}ms)"
    )
    return preset_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply DelayVerb as an external vocal-group send.")
    parser.add_argument("dry_group_wav", type=Path, help="Stereo dry vocal group input")
    parser.add_argument("output_wav", type=Path, help="Stereo vocal group with DelayVerb return")
    parser.add_argument("--delayverb-root", type=Path, default=DEFAULT_DELAYVERB_ROOT)
    parser.add_argument("--preset", type=Path, default=None)
    parser.add_argument("--cover-dry", type=Path, default=None, help="Cover dry vocal for reference-derived preset.")
    parser.add_argument("--original-mix", type=Path, default=None)
    parser.add_argument("--original-vocal", type=Path, default=None)
    parser.add_argument("--original-accomp", type=Path, default=None)
    parser.add_argument("--send", type=float, default=0.85, help="Pre-fader send amount, linear 0..1")
    parser.add_argument("--ffmpeg", default=command_path("ffmpeg"))
    args = parser.parse_args()

    delayverb_root = args.delayverb_root.resolve()
    if not delayverb_root.exists():
        raise SystemExit(f"DelayVerb root not found: {delayverb_root}")
    render_preset = delayverb_root / "scripts" / "render_preset.py"
    binary = delayverb_binary(delayverb_root)
    build_delayverb(delayverb_root, binary)
    binary = delayverb_binary(delayverb_root)

    args.output_wav.parent.mkdir(parents=True, exist_ok=True)
    send_amount = max(0.0, min(1.0, args.send))
    with tempfile.TemporaryDirectory(prefix="delayverb_group_") as tmp:
        tmp_root = Path(tmp)
        if args.original_mix and args.original_vocal and args.original_accomp and args.cover_dry:
            preset = build_reference_preset(
                delayverb_root,
                args.cover_dry,
                args.original_mix,
                args.original_vocal,
                args.original_accomp,
                tmp_root / "reference",
            )
        else:
            preset = args.preset or delayverb_root / "presets" / "vocal_plate.json"
            if not preset.exists():
                raise SystemExit(f"DelayVerb preset not found: {preset}")
            print(f"[delayverb] reference inputs incomplete; using preset: {preset}")

        send_wav = tmp_root / "01_send.wav"
        wet_wav = tmp_root / "02_delayverb_wet.wav"

        print(f"[delayverb] pre-fader send: {send_amount * 100:.1f}%")
        run(
            [
                args.ffmpeg,
                "-y",
                "-hide_banner",
                "-nostats",
                "-i",
                str(args.dry_group_wav),
                "-af",
                f"volume={send_amount:.6f}",
                "-c:a",
                "pcm_f32le",
                str(send_wav),
            ]
        )

        run(
            [
                sys.executable,
                str(render_preset),
                str(send_wav),
                str(wet_wav),
                str(preset),
                "--binary",
                str(binary),
                "--set",
                "dry_wet=100",
            ]
        )

        run(
            [
                args.ffmpeg,
                "-y",
                "-hide_banner",
                "-nostats",
                "-i",
                str(args.dry_group_wav),
                "-i",
                str(wet_wav),
                "-filter_complex",
                "[0:a][1:a]amix=inputs=2:normalize=0[out]",
                "-map",
                "[out]",
                "-c:a",
                "pcm_f32le",
                str(args.output_wav),
            ]
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
