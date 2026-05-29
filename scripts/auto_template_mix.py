#!/usr/bin/env python3
"""Run external spectrum analyzer, resolve template, then call the mix renderer."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from plan_mix_template import build_plan
from analyze_reference import analyse as analyse_reference, analyse_input_pair, resolve_reference_files


ROOT = Path(__file__).resolve().parent.parent


def default_analyzer() -> Path:
    candidates = [
        ROOT.parent.parent / "spectral-mix-template-selector" / "spectrum_template_analyzer.py",
        Path(r"D:\code\spectral-mix-template-selector\spectrum_template_analyzer.py"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def default_analyzer_python(analyzer_script: Path) -> str:
    analyzer_root = analyzer_script.resolve(strict=False).parent
    candidates = [
        analyzer_root / "python" / "python.exe",
        analyzer_root / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return sys.executable


def resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def default_renderer() -> str:
    local_bash = ROOT / ".tools" / "msys64" / "usr" / "bin" / "bash.exe"
    if local_bash.exists():
        return str(local_bash)
    return "bash"


def is_msys_bash(renderer: str) -> bool:
    path = Path(renderer)
    return path.name.lower() == "bash.exe" and "msys64" in {part.lower() for part in path.parts}


def to_msys_path(path: Path) -> str:
    resolved = path if path.is_absolute() else (ROOT / path)
    text = str(resolved.resolve(strict=False))
    if len(text) >= 2 and text[1] == ":":
        return f"/{text[0].lower()}{text[2:].replace('\\', '/')}"
    return text.replace("\\", "/")


def build_bash_command(renderer: str, script: Path, script_args: list[str | Path]) -> list[str]:
    if is_msys_bash(renderer):
        return [
            renderer,
            "-lc",
            'cd "$1" && source scripts/msys_template_env.sh >/dev/null && shift && bash "$@"',
            "template-render",
            to_msys_path(ROOT),
            to_msys_path(script),
            *[to_msys_path(arg) if isinstance(arg, Path) else arg for arg in script_args],
        ]
    return [renderer, str(script), *[str(arg) for arg in script_args]]


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def run_analyzer(analyzer_python: str, analyzer_script: Path, audio_path: Path) -> dict:
    cmd = [analyzer_python, str(analyzer_script), str(audio_path)]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
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


def run_renderer(
    template_id: str,
    vocal: Path,
    accomp: Path,
    output: Path,
    renderer: str,
    render_backend: str,
    dry_run: bool,
    with_volume_automation: bool,
    loudness_finalizer: bool,
    legacy_current_renderer: bool,
    reference_audio: Path | None = None,
    mix_plan: Path | None = None,
) -> dict:
    if legacy_current_renderer:
        script = ROOT / "scripts" / "full_fx_mix.sh"
        cmd = build_bash_command(renderer, script, [vocal, accomp, output])
    elif render_backend == "wasm":
        script = ROOT / "scripts" / "render_template_mix_wasm.mjs"
        cmd = ["node", str(script), template_id, str(vocal), str(accomp), str(output)]
        if with_volume_automation:
            cmd.append("--with-volume-automation")
    else:
        script = ROOT / "scripts" / "render_template_mix.sh"
        cmd = build_bash_command(renderer, script, [template_id, vocal, accomp, output])
        if with_volume_automation:
            cmd.append("--with-volume-automation")
        if not loudness_finalizer:
            cmd.append("--no-loudness-finalizer")
        if reference_audio is not None:
            ref_arg = to_msys_path(reference_audio) if is_msys_bash(renderer) else str(reference_audio)
            cmd += ["--reference-audio", ref_arg]
        if mix_plan is not None:
            plan_arg = to_msys_path(mix_plan) if is_msys_bash(renderer) else str(mix_plan)
            cmd += ["--mix-plan", plan_arg]
    if dry_run:
        return {"ran": False, "command": cmd}
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, cwd=ROOT)
    return {
        "ran": True,
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyze, select template, render mix, and write reports.")
    parser.add_argument("vocal_wav", type=Path)
    parser.add_argument("accomp_wav", type=Path)
    parser.add_argument("output_wav", type=Path)
    parser.add_argument("--analyzer", type=Path, default=None)
    parser.add_argument("--analyzer-python", default=None)
    parser.add_argument("--renderer", default=default_renderer())
    parser.add_argument("--render-backend", choices=("bash", "wasm"), default="bash")
    parser.add_argument("--report-dir", type=Path, default=None)
    parser.add_argument("--report-prefix", default="", help="Prefix report JSON filenames within --report-dir.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--with-volume-automation",
        action="store_true",
        help="Run the project's volume automation before the selected template DSP chain.",
    )
    parser.add_argument(
        "--no-loudness-finalizer",
        action="store_true",
        help="Skip final LUFS/true-peak normalization after the template master bus.",
    )
    parser.add_argument(
        "--legacy-current-renderer",
        action="store_true",
        help="Ignore A/B/C template DSP chains and render through full_fx_mix.sh.",
    )
    parser.add_argument(
        "--reference-audio",
        type=Path,
        default=None,
        help="Reference full-mix override; otherwise auto-resolved by song name from downloads/feishu_long_audio_screened/原曲.",
    )
    parser.add_argument(
        "--reference-vocal",
        type=Path,
        default=None,
        help="Reference vocal stem override; otherwise auto-resolved from 原曲人声/.",
    )
    parser.add_argument(
        "--reference-accomp",
        type=Path,
        default=None,
        help="Reference accompaniment override; otherwise auto-resolved from 伴奏/.",
    )
    parser.add_argument(
        "--no-reference",
        action="store_true",
        help="Skip reference-driven feature extraction and overrides.",
    )
    args = parser.parse_args()

    vocal_wav = resolve_path(args.vocal_wav)
    accomp_wav = resolve_path(args.accomp_wav)
    output_wav = resolve_path(args.output_wav)
    analyzer = resolve_path(args.analyzer) if args.analyzer else default_analyzer()
    analyzer_python = args.analyzer_python or default_analyzer_python(analyzer)
    report_dir = resolve_path(args.report_dir) if args.report_dir else output_wav.parent

    if not analyzer.exists():
        raise SystemExit(
            "Analyzer script not found. Pass --analyzer explicitly, for example:\n"
            r"  --analyzer D:\code\spectral-mix-template-selector\spectrum_template_analyzer.py"
        )

    analysis = run_analyzer(analyzer_python, analyzer, vocal_wav)

    ref_full_mix: Path | None = None
    ref_features: dict | None = None
    input_features: dict | None = None
    if not args.no_reference:
        ref_full_mix = resolve_path(args.reference_audio) if args.reference_audio else None
        ref_vocal = resolve_path(args.reference_vocal) if args.reference_vocal else None
        ref_accomp = resolve_path(args.reference_accomp) if args.reference_accomp else None
        if ref_full_mix is None or ref_vocal is None or ref_accomp is None:
            resolved_refs = resolve_reference_files(vocal_wav)
            ref_full_mix = ref_full_mix or resolved_refs["full_mix"]
            ref_vocal = ref_vocal or resolved_refs["vocal"]
            ref_accomp = ref_accomp or resolved_refs["accomp"]
        if ref_full_mix and ref_vocal and ref_accomp:
            print(f"[ref] full_mix:   {ref_full_mix}")
            print(f"[ref] ref_vocal:  {ref_vocal}")
            print(f"[ref] ref_accomp: {ref_accomp}")
            ref_features = analyse_reference(ref_full_mix, ref_vocal, ref_accomp)
            input_features = analyse_input_pair(vocal_wav, accomp_wav)
        else:
            print(
                "[ref] Reference files not all resolved; rendering without reference overrides. "
                f"(full_mix={ref_full_mix}, vocal={ref_vocal}, accomp={ref_accomp})"
            )
            ref_full_mix = None

    plan = build_plan(analysis, ref_features=ref_features, input_features=input_features)

    report_dir.mkdir(parents=True, exist_ok=True)
    report_prefix = args.report_prefix.replace("\\", "_").replace("/", "_").replace(":", "_")
    analysis_path = report_dir / f"{report_prefix}analysis.json"
    plan_path = report_dir / f"{report_prefix}resolved_mix_plan.json"
    summary_path = report_dir / f"{report_prefix}summary.json"
    write_json(analysis_path, analysis)
    write_json(plan_path, plan)

    template_id = str(plan.get("selected_template") or "template_d")
    render = run_renderer(
        template_id,
        vocal_wav,
        accomp_wav,
        output_wav,
        args.renderer,
        args.render_backend,
        args.dry_run,
        args.with_volume_automation,
        not args.no_loudness_finalizer,
        args.legacy_current_renderer,
        reference_audio=ref_full_mix,
        mix_plan=plan_path,
    )
    loudness_path = output_wav.with_suffix(".loudness.json")
    loudness = json.loads(loudness_path.read_text(encoding="utf-8-sig")) if loudness_path.exists() else None
    summary = {
        "classification_label": plan.get("classification_label"),
        "selected_template": plan.get("selected_template"),
        "selected_template_name": plan.get("selected_template_name"),
        "analysis_json": str(analysis_path),
        "resolved_mix_plan": str(plan_path),
        "output_wav": str(output_wav),
        "reference_used": (plan.get("reference") or {}).get("features", {}).get("sources") if plan.get("reference") else None,
        "reference_overrides": (plan.get("reference") or {}).get("overrides"),
        "render": render,
        "loudness_finalizer": not args.no_loudness_finalizer,
        "loudness": loudness,
        "important_note": (
            "A/B/C now render through template-specific Faust approximation chains. "
            "Default backend is native Faust shell rendering. Use --render-backend wasm only for "
            "development smoke checks, or --legacy-current-renderer to force the older full_fx_mix.sh path."
        ),
    }
    write_json(summary_path, summary)

    if render.get("ran") and render.get("returncode") != 0:
        raise SystemExit(
            f"Renderer failed with code {render.get('returncode')}.\n"
            f"Summary written to {summary_path}"
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
