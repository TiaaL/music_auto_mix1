#!/usr/bin/env python3
"""把批量混音 WAV 上传到飞书云盘，并把链接写回表格 G 列。"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = ROOT / "calibration_outputs" / "feishu_mix_compare_C0LiHq_20260617"
SPREADSHEET_TOKEN = "MCHGsMfZ0h8QCRt8RMrcU5rWnDe"
SHEET_ID = "C0LiHq"


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """统一通过 lark-cli 执行飞书 API，并关闭本地代理绕路。"""
    return subprocess.run(
        ["env", "LARK_CLI_NO_PROXY=1", *cmd],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def parse_json_from_output(text: str) -> dict[str, Any]:
    """lark-cli 可能先打印日志；这里从第一段 JSON 开始解析。"""
    start = text.find("{")
    if start < 0:
        raise ValueError(f"No JSON object found in output: {text[:500]}")
    return json.loads(text[start:])


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sheet_row_for_label(label: str) -> int:
    """label 形如 r12_歌名；当前目标表格从数据区上一行开始写，所以行号减一。"""
    match = re.match(r"r(\d+)_", label)
    if not match:
        raise ValueError(f"Cannot infer source row from label: {label}")
    source_row = int(match.group(1))
    return source_row - 1


def upload_file(path: Path, name: str) -> dict[str, Any]:
    upload_path = path
    try:
        # 在项目目录内时使用相对路径，让 lark-cli 输出更短，也更便于日志复现。
        upload_path = path.resolve().relative_to(ROOT.resolve())
    except ValueError:
        pass
    proc = run(["lark-cli", "drive", "+upload", "--file", str(upload_path), "--name", name, "--as", "user"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout + proc.stderr)
    data = parse_json_from_output(proc.stdout)
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False))
    return data["data"]


def get_url(file_token: str) -> str:
    """上传接口只返回 token；再查一次 metas 才能拿到可打开的 URL。"""
    body = json.dumps(
        {
            "request_docs": [{"doc_token": file_token, "doc_type": "file"}],
            "with_url": True,
        },
        ensure_ascii=False,
    )
    proc = run(["lark-cli", "drive", "metas", "batch_query", "--data", body, "--as", "user"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout + proc.stderr)
    data = parse_json_from_output(proc.stdout)
    metas = (data.get("data") or {}).get("metas") or []
    if not metas or not metas[0].get("url"):
        raise RuntimeError(f"No URL returned for token {file_token}: {data}")
    return str(metas[0]["url"])


def write_sheet(row: int, url: str) -> None:
    """把单个结果链接写回 G 列；批量执行时逐行提交，失败范围更小。"""
    values = json.dumps([[url]], ensure_ascii=False)
    proc = run(
        [
            "lark-cli",
            "sheets",
            "+write",
            "--spreadsheet-token",
            SPREADSHEET_TOKEN,
            "--sheet-id",
            SHEET_ID,
            "--range",
            f"G{row}",
            "--values",
            values,
            "--as",
            "user",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stdout + proc.stderr)
    data = parse_json_from_output(proc.stdout)
    if not data.get("ok"):
        raise RuntimeError(json.dumps(data, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--upload-cache", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest or args.out_dir / "batch_manifest.json"
    cache_path = args.upload_cache or args.out_dir / "feishu_uploads.json"
    manifest = load_json(manifest_path, [])
    cache: dict[str, Any] = load_json(cache_path, {})

    completed = 0
    for item in manifest:
        if item.get("status") not in {"ok", "exists"}:
            continue
        label = str(item["label"])
        output_wav = Path(str(item["output_wav"]))
        if not output_wav.exists():
            print(f"[skip] missing output: {label}", flush=True)
            continue
        if args.limit is not None and completed >= args.limit:
            break

        sheet_row = sheet_row_for_label(label)
        name = f"row_{int(label[1:label.index('_')]):03d}_{label.split('_', 1)[1]}_mix2.wav"
        entry = cache.get(label)
        if not entry:
            if args.dry_run:
                print(f"[dry-run] upload/write row {sheet_row}: {name}", flush=True)
                completed += 1
                continue
            # 缓存以 label 为键：同一行重复上传时复用已有 file token，避免飞书盘里堆重复文件。
            upload = upload_file(output_wav, name)
            url = get_url(str(upload["file_token"]))
            entry = {
                "sheet_row": sheet_row,
                "file_name": name,
                "file_token": upload["file_token"],
                "size": upload.get("size"),
                "url": url,
            }
            cache[label] = entry
            write_json(cache_path, cache)

        if not args.dry_run:
            write_sheet(sheet_row, str(entry["url"]))
        print(f"[ok] G{sheet_row}: {label}", flush=True)
        completed += 1

    print(json.dumps({"processed": completed, "cache": str(cache_path)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
