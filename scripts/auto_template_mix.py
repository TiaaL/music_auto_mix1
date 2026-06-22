#!/usr/bin/env python3
"""调用外部频谱分析器、生成混音 plan，再启动实际渲染器。"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from plan_mix_template import build_plan
from analyze_reference import analyse as analyse_reference, analyse_input_pair, resolve_reference_files


ROOT = Path(__file__).resolve().parent.parent
CACHE_VERSION = "auto_template_mix_features_v4"


def default_analyzer() -> Path:
    """按常见本地目录寻找外部频谱分类器。"""
    candidates = [
        ROOT.parent / "spectral-mix-template-selector" / "spectrum_template_analyzer.py",
        ROOT.parent.parent / "spectral-mix-template-selector" / "spectrum_template_analyzer.py",
        Path(r"D:\code\spectral-mix-template-selector\spectrum_template_analyzer.py"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0]


def default_analyzer_python(analyzer_script: Path) -> str:
    """优先使用分析器项目自己的 Python，避免依赖装在错的环境里。"""
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


def optional_resolved_path(path: Path | None) -> Path | None:
    return resolve_path(path) if path is not None else None


def default_renderer() -> str:
    """Windows/MSYS2 环境优先走仓库内 bash；其他平台使用系统 bash。"""
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
        tail = text[2:].replace("\\", "/")
        return f"/{text[0].lower()}{tail}"
    return text.replace("\\", "/")


def build_bash_command(renderer: str, script: Path, script_args: list[str | Path]) -> list[str]:
    if is_msys_bash(renderer):
        # MSYS2 需要先切到项目根目录并注入本仓库 toolchain 环境；
        # 同时把 Windows 路径转换成 /c/... 形式，避免 bash 找不到文件。
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


def file_signature(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path.resolve(strict=False)),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def cache_key(kind: str, paths: list[Path]) -> str:
    """缓存 key 同时绑定输入文件和特征提取代码，代码改动后自动失效。"""
    code_paths = [
        ROOT / "scripts" / "analyze_reference.py",
        ROOT / "scripts" / "auto_template_mix.py",
    ]
    payload = {
        "version": CACHE_VERSION,
        "kind": kind,
        "files": [file_signature(path) for path in paths],
        "code": [file_signature(path) for path in code_paths if path.exists()],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def cached_feature(kind: str, paths: list[Path], compute) -> dict:
    """按文件签名缓存重型音频特征，批量渲染时可复用同一首歌的分析结果。"""
    cache_dir = ROOT / "calibration_outputs" / "cache" / "features"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{kind}_{cache_key(kind, paths)}.json"
    if cache_path.exists():
        print(f"[cache] {kind}: {cache_path}")
        return json.loads(cache_path.read_text(encoding="utf-8-sig"))
    value = compute()
    cache_path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[cache] {kind}: wrote {cache_path}")
    return value


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
    stage_report: bool = False,
    stage_report_loudness: bool = False,
    global_declick: str = "auto",
    fast_loudness_steps: str = "",
    compare_fast_loudness: bool = False,
    spatial_fx: str = "auto",
    export_vocal_group: Path | None = None,
    direct_vocal_side_layer: str = "off",
) -> dict:
    if legacy_current_renderer:
        script = ROOT / "scripts" / "full_fx_mix.sh"
        cmd = build_bash_command(renderer, script, [vocal, accomp, output])
    elif render_backend == "wasm":
        script = ROOT / "scripts" / "render_template_mix_wasm.mjs"
        cmd = ["node", str(script), template_id, str(vocal), str(accomp), str(output)]
        if with_volume_automation:
            cmd.append("--with-volume-automation")
        if not loudness_finalizer:
            cmd.append("--no-loudness-finalizer")
        if reference_audio is not None:
            cmd += ["--reference-audio", str(reference_audio)]
        if mix_plan is not None:
            cmd += ["--mix-plan", str(mix_plan)]
        if stage_report:
            cmd.append("--stage-report")
        if stage_report_loudness:
            cmd.append("--stage-report-loudness")
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
        cmd += ["--global-declick", global_declick]
        if fast_loudness_steps:
            cmd += ["--fast-loudness-steps", fast_loudness_steps]
        if compare_fast_loudness:
            cmd.append("--compare-fast-loudness")
        cmd += ["--spatial-fx", spatial_fx]
        if export_vocal_group is not None:
            export_arg = to_msys_path(export_vocal_group) if is_msys_bash(renderer) else str(export_vocal_group)
            cmd += ["--export-vocal-group", export_arg]
        if direct_vocal_side_layer != "off":
            cmd += ["--direct-vocal-side-layer", direct_vocal_side_layer]
        if stage_report:
            cmd.append("--stage-report")
        if stage_report_loudness:
            cmd.append("--stage-report-loudness")
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


def run_vocal_group_spatial_audit(
    reference_vocal: Path,
    vocal_group: Path,
    output_json: Path,
    reference_audio: Path | None = None,
) -> dict:
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "audit_active_spatial_lift.py"),
        "--reference-vocal",
        str(reference_vocal),
        "--reference-target",
        "vocal_stem",
        "--candidate",
        f"vocal_group={vocal_group}",
        "--output-json",
        str(output_json),
    ]
    if reference_audio is not None:
        cmd += ["--reference-audio", str(reference_audio)]
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, cwd=ROOT)
    report = None
    if output_json.exists():
        report = json.loads(output_json.read_text(encoding="utf-8-sig"))
    return {
        "ran": True,
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output_json": str(output_json),
        "report": report,
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
        "--global-declick",
        choices=("auto", "always", "off"),
        default="auto",
        help="Final isolated-click handling in the loudness finalizer.",
    )
    parser.add_argument(
        "--fast-loudness-steps",
        default="",
        help="Experimental comma-separated finalizer steps to measure with libebur128.",
    )
    parser.add_argument(
        "--compare-fast-loudness",
        action="store_true",
        help="Also run FFmpeg loudnorm for experimental fast-loudness steps and write deltas.",
    )
    parser.add_argument(
        "--spatial-fx",
        choices=("auto", "off"),
        default="auto",
        help="Use reference-driven vocal-group spatial FX when the resolved plan enables it.",
    )
    parser.add_argument(
        "--no-spatial-fx",
        action="store_true",
        help="Legacy alias for --spatial-fx off.",
    )
    parser.add_argument(
        "--spatial-audit",
        choices=("auto", "off"),
        default="auto",
        help="Export and audit the post-FX vocal_group bus against the reference vocal stem when references are available.",
    )
    parser.add_argument(
        "--export-vocal-group",
        action="store_true",
        help="Keep a copy of the post-FX vocal_group bus. Implied by --spatial-audit auto when reference vocal is available.",
    )
    parser.add_argument(
        "--vocal-group-output",
        type=Path,
        default=None,
        help="Path for --export-vocal-group. Defaults to <output>.vocal_group.wav.",
    )
    parser.add_argument(
        "--direct-vocal-side-layer",
        choices=("off", "light"),
        default="off",
        help="Experimental second-stage direct side layer. Keep off unless the vocal_group spatial audit recommends it.",
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
        help="服务端/调用方显式传入的参考原曲。",
    )
    parser.add_argument(
        "--reference-vocal",
        type=Path,
        default=None,
        help="服务端/调用方显式传入的参考人声 stem。",
    )
    parser.add_argument(
        "--reference-accomp",
        type=Path,
        default=None,
        help="服务端/调用方显式传入的参考伴奏 stem。",
    )
    parser.add_argument(
        "--reference-root",
        type=Path,
        default=None,
        help="仅本地测试使用：包含 原曲/、原曲人声/、伴奏/ 的目录，用于按歌名自动匹配参考。",
    )
    parser.add_argument(
        "--no-reference",
        action="store_true",
        help="Skip reference-driven feature extraction and overrides.",
    )
    parser.add_argument(
        "--stage-report",
        action="store_true",
        help="Record elapsed time and file paths at each large render stage.",
    )
    parser.add_argument(
        "--stage-report-loudness",
        action="store_true",
        help="Also measure LUFS/true-peak for stage inputs/outputs. Slower; cached by file signature.",
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
    ref_vocal: Path | None = None
    ref_accomp: Path | None = None
    ref_features: dict | None = None
    input_features: dict | None = None
    reference_status: dict[str, object] = {
        "requested": not args.no_reference,
        "used": False,
        "mode": "disabled_by_flag" if args.no_reference else "pending",
        "fallback_policy": (
            "无参考时使用 source_cleanup 自驱动问题频段清理、通用 active 人声/伴奏比例目标、"
            "默认最终响度；不使用原曲音色塑形"
        ),
    }
    if not args.no_reference:
        ref_full_mix = optional_resolved_path(args.reference_audio)
        ref_vocal = optional_resolved_path(args.reference_vocal)
        ref_accomp = optional_resolved_path(args.reference_accomp)
        if (ref_full_mix is None or ref_vocal is None or ref_accomp is None) and args.reference_root:
            # 只有显式传 --reference-root 时才按本地歌名自动找参考。
            # 线上服务应由服务端直接传 reference-audio/vocal/accomp，避免扫到本机旧文件。
            reference_root = resolve_path(args.reference_root)
            resolved_refs = resolve_reference_files(vocal_wav, downloads_root=reference_root, accomp_input=accomp_wav)
            ref_full_mix = ref_full_mix or resolved_refs["full_mix"]
            ref_vocal = ref_vocal or resolved_refs["vocal"]
            ref_accomp = ref_accomp or resolved_refs["accomp"]
        refs_ready = all(path is not None and path.exists() for path in (ref_full_mix, ref_vocal, ref_accomp))
        if refs_ready:
            print(f"[ref] full_mix:   {ref_full_mix}")
            print(f"[ref] ref_vocal:  {ref_vocal}")
            print(f"[ref] ref_accomp: {ref_accomp}")
            reference_status.update({
                "used": True,
                "mode": "resolved",
                "full_mix": str(ref_full_mix),
                "vocal": str(ref_vocal),
                "accomp": str(ref_accomp),
            })
            ref_features = cached_feature(
                "reference",
                [ref_full_mix, ref_vocal, ref_accomp],
                lambda: analyse_reference(ref_full_mix, ref_vocal, ref_accomp),
            )
            input_features = cached_feature(
                "input_pair",
                [vocal_wav, accomp_wav],
                lambda: analyse_input_pair(vocal_wav, accomp_wav),
            )
        else:
            # 缺任意一个参考 stem 都不启用参考塑形，避免“半套参考”把比例或音色带偏。
            missing = [
                name for name, value in (
                    ("full_mix", ref_full_mix),
                    ("vocal", ref_vocal),
                    ("accomp", ref_accomp),
                )
                if value is None or not value.exists()
            ]
            reference_status.update({
                "used": False,
                "mode": "missing_reference_fallback",
                "missing": missing,
                "resolved_candidates": {
                    "full_mix": str(ref_full_mix) if ref_full_mix else None,
                    "vocal": str(ref_vocal) if ref_vocal else None,
                    "accomp": str(ref_accomp) if ref_accomp else None,
                },
            })
            print(
                "[ref] Reference files not all resolved; rendering without reference overrides. "
                f"(full_mix={ref_full_mix}, vocal={ref_vocal}, accomp={ref_accomp})"
            )
            ref_full_mix = None
            ref_vocal = None
            ref_accomp = None
    if input_features is None:
        # 通用清理块只依赖用户提交的干声和伴奏。
        # 即使 --no-reference 也要计算它，这样快速通用清理仍然生效。
        input_features = cached_feature(
            "input_pair",
            [vocal_wav, accomp_wav],
            lambda: analyse_input_pair(vocal_wav, accomp_wav),
        )

    plan = build_plan(analysis, ref_features=ref_features, input_features=input_features)

    report_dir.mkdir(parents=True, exist_ok=True)
    report_prefix = args.report_prefix.replace("\\", "_").replace("/", "_").replace(":", "_")
    analysis_path = report_dir / f"{report_prefix}analysis.json"
    plan_path = report_dir / f"{report_prefix}resolved_mix_plan.json"
    summary_path = report_dir / f"{report_prefix}summary.json"
    write_json(analysis_path, analysis)
    write_json(plan_path, plan)

    template_id = str(plan.get("selected_template") or "template_d")
    should_export_vocal_group = (
        args.export_vocal_group
        or (args.spatial_audit == "auto" and ref_vocal is not None and template_id != "template_d")
    )
    # 空间审计需要 post-FX vocal_group；只有有参考人声且不是 legacy 模板时才默认导出。
    vocal_group_output = (
        resolve_path(args.vocal_group_output)
        if args.vocal_group_output
        else output_wav.with_suffix(".vocal_group.wav")
    ) if should_export_vocal_group else None
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
        stage_report=args.stage_report or args.stage_report_loudness,
        stage_report_loudness=args.stage_report_loudness,
        global_declick=args.global_declick,
        fast_loudness_steps=args.fast_loudness_steps,
        compare_fast_loudness=args.compare_fast_loudness,
        spatial_fx="off" if args.no_spatial_fx else args.spatial_fx,
        export_vocal_group=vocal_group_output,
        direct_vocal_side_layer=args.direct_vocal_side_layer,
    )
    spatial_audit = None
    if (
        args.spatial_audit == "auto"
        and ref_vocal is not None
        and vocal_group_output is not None
        and render.get("ran")
        and render.get("returncode") == 0
        and vocal_group_output.exists()
    ):
        spatial_audit_path = output_wav.with_suffix(".vocal_group_spatial_audit.json")
        spatial_audit = run_vocal_group_spatial_audit(
            ref_vocal,
            vocal_group_output,
            spatial_audit_path,
            reference_audio=ref_full_mix,
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
        "reference_status": reference_status,
        "reference_used": (plan.get("reference") or {}).get("features", {}).get("sources") if plan.get("reference") else None,
        "reference_overrides": (plan.get("reference") or {}).get("overrides"),
        "render": render,
        "loudness_finalizer": not args.no_loudness_finalizer,
        "global_declick": args.global_declick,
        "fast_loudness_steps": args.fast_loudness_steps,
        "compare_fast_loudness": args.compare_fast_loudness,
        "spatial_fx": "off" if args.no_spatial_fx else args.spatial_fx,
        "direct_vocal_side_layer": args.direct_vocal_side_layer,
        "spatial_audit": spatial_audit,
        "vocal_group_output": str(vocal_group_output) if vocal_group_output else None,
        "loudness": loudness,
        "stage_report": str(output_wav.with_suffix(".stage_report.json")) if (args.stage_report or args.stage_report_loudness) else None,
        "stage_report_loudness": args.stage_report_loudness,
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
