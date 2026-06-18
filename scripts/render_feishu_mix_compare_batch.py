#!/usr/bin/env python3
"""按飞书表格行顺序渲染混音对比。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AUDIO_ROOT = ROOT.parent / "feishu_long_audio_screened"
DEFAULT_OUTPUT_DIR = ROOT / "calibration_outputs" / "feishu_mix_compare_C0LiHq_20260617"
SHEET_RECORDS = DEFAULT_AUDIO_ROOT / "sheet_records.json"
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".flac")


@dataclass
class BatchJob:
    row: int
    label: str
    vocal: str | None
    accomp: str | None
    reference_audio: str | None
    reference_vocal: str | None
    reference_accomp: str | None
    reference_status: str
    output_wav: str
    summary_json: str
    status: str
    returncode: int | None = None
    error: str | None = None


def sanitize_label(value: str) -> str:
    """把表格行信息变成可用于文件名的短标签。"""
    value = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", value.strip())
    value = value.strip("._-")
    return value or "unnamed"


def file_stem(case_name: str, extra_name: str, role: str, row: int) -> str:
    """复刻下载脚本的命名规则，用表格行反查本地音频文件。"""
    base = f"{case_name}{extra_name}" if extra_name else case_name
    stem = f"{base}_{role}"
    if case_name == "线上数据-5.25":
        stem = f"{stem}_row{row}"
    return stem


def find_audio(audio_root: Path, role: str, stem: str) -> Path | None:
    """按常见音频扩展名查找；找不到精确扩展名时再用 glob 兜底。"""
    role_dir = audio_root / role
    for ext in AUDIO_EXTS:
        candidate = role_dir / f"{stem}{ext}"
        if candidate.exists():
            return candidate
    matches = sorted(role_dir.glob(f"{stem}.*"))
    return matches[0] if matches else None


def resolve_role_audio(audio_root: Path, case_name: str, extra_name: str, role: str, row: int) -> Path | None:
    path = find_audio(audio_root, role, file_stem(case_name, extra_name, role, row))
    if path is not None:
        return path
    if case_name == "线上数据-5.25":
        return find_audio(audio_root, role, f"{case_name}_{role}")
    return None


def resolve_reference_audio(
    audio_root: Path,
    case_name: str,
    extra_name: str,
    row: int,
    accomp: Path | None,
) -> tuple[Path | None, Path | None, Path | None]:
    """解析 D/G/H 列下载出的参考素材。

    D 列原曲 -> 原曲/，G 列原曲人声 -> 原曲人声/，H 列歌曲伴奏 -> 伴奏/。
    H 列同时也是本次渲染伴奏，所以找不到单独参考伴奏时可复用 accomp。
    """
    ref_audio = resolve_role_audio(audio_root, case_name, extra_name, "原曲", row)
    ref_vocal = resolve_role_audio(audio_root, case_name, extra_name, "原曲人声", row)
    ref_accomp = resolve_role_audio(audio_root, case_name, extra_name, "伴奏", row) or accomp
    return ref_audio, ref_vocal, ref_accomp


def load_records(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return list(data.get("records") or [])


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-root", type=Path, default=DEFAULT_AUDIO_ROOT)
    parser.add_argument("--records-json", type=Path, default=SHEET_RECORDS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--start-row", type=int, default=1)
    parser.add_argument("--end-row", type=int)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-reference", action="store_true")
    parser.add_argument(
        "--reference-policy",
        choices=("sheet-only", "local-auto", "off"),
        default="sheet-only",
        help="批处理参考策略：默认只信 records_json 的显式参考；本地测试可用 local-auto 按歌名匹配本地参考。",
    )
    parser.add_argument("--allow-local-auto-reference", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-volume-automation", action="store_true")
    parser.add_argument("--global-declick", choices=("auto", "always", "off"), default="auto")
    parser.add_argument("--stage-report", action="store_true")
    args = parser.parse_args()

    records = load_records(args.records_json)
    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[BatchJob] = []
    rendered = 0

    for record in records:
        row = int(record.get("row") or 0)
        if row < args.start_row:
            continue
        if args.end_row is not None and row > args.end_row:
            continue

        case_name = str(record.get("case_name") or "").strip()
        extra_name = str(record.get("extra_name") or "").strip()
        if not case_name:
            continue

        label = sanitize_label(f"r{row}_{case_name}{extra_name}")
        # 干声/伴奏是本次要混的素材；原曲/原曲人声/伴奏是参考素材。
        # 注意 H 列伴奏同时可作为渲染伴奏和参考伴奏。
        vocal = resolve_role_audio(args.audio_root, case_name, extra_name, "干声", row)
        accomp = resolve_role_audio(args.audio_root, case_name, extra_name, "伴奏", row)
        ref_audio, ref_vocal, ref_accomp = resolve_reference_audio(
            args.audio_root,
            case_name,
            extra_name,
            row,
            accomp,
        )
        output_wav = args.out_dir / f"mix_{label}.wav"
        summary_json = args.out_dir / f"{label}_summary.json"
        reference_policy = "local-auto" if args.allow_local_auto_reference else args.reference_policy
        if args.no_reference:
            reference_policy = "off"

        # 批处理默认只信 records_json 里的显式参考；本地按歌名自动匹配必须显式选择 local-auto。
        # 线上跑批宁可走通用兜底，也不要把同名旧文件误当参考曲。
        force_generic_fallback = ref_audio is None or ref_vocal is None
        if reference_policy == "off":
            reference_status = "disabled_by_flag"
        elif ref_audio and ref_vocal and ref_accomp:
            reference_status = "explicit_reference_ready"
        elif reference_policy == "local-auto":
            reference_status = "missing_explicit_reference_use_local_auto_reference"
        else:
            reference_status = "missing_explicit_reference_use_generic_fallback"
        job = BatchJob(
            row=row,
            label=label,
            vocal=str(vocal) if vocal else None,
            accomp=str(accomp) if accomp else None,
            reference_audio=str(ref_audio) if ref_audio else None,
            reference_vocal=str(ref_vocal) if ref_vocal else None,
            reference_accomp=str(ref_accomp) if ref_accomp else None,
            reference_status=reference_status,
            output_wav=str(output_wav),
            summary_json=str(summary_json),
            status="pending",
        )

        if vocal is None or accomp is None:
            job.status = "skipped_missing_input"
            job.error = "missing vocal or accompaniment"
            jobs.append(job)
            continue
        if args.resume and output_wav.exists() and summary_json.exists():
            # resume 只看最终 WAV 和 summary，避免重复跑长任务。
            job.status = "exists"
            jobs.append(job)
            continue
        if args.limit is not None and rendered >= args.limit:
            job.status = "not_run_limit"
            jobs.append(job)
            continue
        if args.dry_run:
            job.status = "dry_run"
            jobs.append(job)
            rendered += 1
            continue

        cmd = [
            sys.executable,
            str(ROOT / "scripts" / "auto_template_mix.py"),
            str(vocal),
            str(accomp),
            str(output_wav),
            "--report-dir",
            str(args.out_dir),
            "--report-prefix",
            f"{label}_",
            "--reference-root",
            str(args.audio_root),
            "--global-declick",
            args.global_declick,
        ]
        if not args.no_reference and ref_audio and ref_vocal and ref_accomp:
            # 线上数据的参考曲来自表格 D/G/H 列，显式传入可避免按歌名自动匹配失败。
            cmd += [
                "--reference-audio",
                str(ref_audio),
                "--reference-vocal",
                str(ref_vocal),
                "--reference-accomp",
                str(ref_accomp),
            ]
        if not args.no_volume_automation:
            cmd.append("--with-volume-automation")
        if reference_policy == "off" or (force_generic_fallback and reference_policy != "local-auto"):
            # ponytail: 批处理默认只信显式参考；缺 D/G 时不猜本地歌名，直接走通用兜底。
            cmd.append("--no-reference")
        if args.stage_report:
            cmd.append("--stage-report")

        proc = run(cmd, ROOT)
        job.returncode = proc.returncode
        if proc.returncode == 0 and output_wav.exists():
            job.status = "ok"
        else:
            job.status = "failed"
            job.error = f"returncode={proc.returncode}"
        (args.out_dir / f"{label}_stdout.txt").write_text(proc.stdout, encoding="utf-8")
        (args.out_dir / f"{label}_stderr.txt").write_text(proc.stderr, encoding="utf-8")
        jobs.append(job)
        rendered += 1
        write_json(args.out_dir / "batch_manifest.json", [asdict(item) for item in jobs])
        print(f"[{rendered}] row {row} {job.status}: {label}", flush=True)

    write_json(args.out_dir / "batch_manifest.json", [asdict(item) for item in jobs])
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    summary = {"output_dir": str(args.out_dir), "counts": counts, "total": len(jobs)}
    write_json(args.out_dir / "batch_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
