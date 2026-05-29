#!/usr/bin/env python3
"""Extract readable metadata and parameter snapshots from Cubase .vstpreset files."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import struct
from pathlib import Path
from typing import Any


DEFAULT_PRESET_ROOT = Path(r"D:\cubase\project\混音模版0512\混音参数0512")
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent.parent / "config" / "extracted_vstpresets"


def safe_name(path: Path) -> str:
    stem = "__".join(path.with_suffix("").parts)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") + ".json"


def printable_text(data: bytes) -> str:
    return data.decode("utf-8", errors="ignore")


def extract_meta(text: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for key in ("MediaType", "PlugInCategory", "PlugInName", "PlugInVendor"):
        match = re.search(rf'<Attribute id="{re.escape(key)}" value="([^"]*)"', text)
        if match:
            meta[key] = match.group(1)
    return meta


def parse_values(raw: str) -> list[float | str]:
    values: list[float | str] = []
    for token in re.sub(r"\s+", " ", raw).strip().split(" "):
        if not token:
            continue
        if token == "*":
            values.append(token)
            continue
        try:
            values.append(float(token))
        except ValueError:
            values.append(token)
    return values


def extract_waves_presets(text: str) -> list[dict[str, Any]]:
    presets: list[dict[str, Any]] = []
    for preset_match in re.finditer(r"<Preset\b.*?</Preset>", text, flags=re.S):
        block = preset_match.group(0)
        preset_name = ""
        generic_type = ""
        if m := re.search(r'<Preset Name="([^"]*)"', block):
            preset_name = m.group(1)
        if m := re.search(r'GenericType="([^"]*)"', block):
            generic_type = m.group(1)

        header: dict[str, str] = {}
        for key in ("Group", "PluginName", "PluginSubComp", "PluginVersion", "ActiveSetup", "LoadMenuCategory", "ReadOnly"):
            if m := re.search(rf"<{key}>(.*?)</{key}>", block, flags=re.S):
                header[key] = re.sub(r"\s+", " ", m.group(1)).strip()

        setup_data: list[dict[str, Any]] = []
        for data_match in re.finditer(r"<PresetData\b([^>]*)>(.*?)</PresetData>", block, flags=re.S):
            attrs = data_match.group(1)
            body = data_match.group(2)
            setup = ""
            setup_name = ""
            if m := re.search(r'Setup="([^"]*)"', attrs):
                setup = m.group(1)
            if m := re.search(r'SetupName="([^"]*)"', attrs):
                setup_name = m.group(1)
            parameters: list[float | str] = []
            if m := re.search(r'<Parameters Type="RealWorld">(.*?)</Parameters>', body, flags=re.S):
                parameters = parse_values(m.group(1))
            setup_data.append(
                {
                    "setup": setup,
                    "setup_name": setup_name,
                    "parameter_format": "Waves PresetChunkXMLTree RealWorld",
                    "parameter_count": len(parameters),
                    "parameters": parameters,
                }
            )

        presets.append(
            {
                "preset_name": preset_name,
                "generic_type": generic_type,
                "header": header,
                "setups": setup_data,
            }
        )
    return presets


def extract_valhalla_settings(text: str) -> dict[str, Any] | None:
    match = re.search(r"<MYPLUGINSETTINGS\b([^>]*)/>", text)
    if not match:
        return None
    attrs = match.group(1)
    settings: dict[str, Any] = {}
    for key, value in re.findall(r'(\w+)="([^"]*)"', attrs):
        try:
            settings[key] = float(value)
        except ValueError:
            settings[key] = value
    parameters = {
        key: settings[key]
        for key in sorted(settings, key=lambda k: int(k[9:]) if k.startswith("parameter") and k[9:].isdigit() else 9999)
        if key.startswith("parameter")
    }
    return {
        "parameter_format": "Valhalla MYPLUGINSETTINGS",
        "parameter_count": len(parameters),
        "parameters": parameters,
        "ui": {k: v for k, v in settings.items() if not k.startswith("parameter")},
    }


def extract_fabfilter_summary(data: bytes, text: str) -> dict[str, Any] | None:
    idx = data.find(b"FFBS")
    if idx < 0:
        return None
    end = data.find(b"<?xml", idx)
    if end < 0:
        end = len(data)
    state = data[idx:end]
    ascii_state = state.decode("utf-8", errors="ignore")
    labels = [x for x in ("FQ3p", "FFpr", "FFed", "Default Setting") if x in ascii_state or x in text]

    float_candidates: list[dict[str, float | int]] = []
    # This is diagnostic only. Offsets are not treated as decoded controls.
    for offset in range(0, max(0, min(len(state) - 4, 512))):
        value = struct.unpack_from("<f", state, offset)[0]
        if 10.0 <= value <= 22000.0 or (-36.0 <= value <= 36.0 and abs(value) >= 0.05):
            if len(float_candidates) < 120:
                float_candidates.append({"offset": offset, "value": value})

    return {
        "parameter_format": "FabFilter binary state",
        "state_size_bytes": len(state),
        "state_sha256": hashlib.sha256(state).hexdigest(),
        "state_base64": base64.b64encode(state).decode("ascii"),
        "known_markers": labels,
        "diagnostic_float_candidates": float_candidates,
        "decoded": False,
        "requires_parser": True,
    }


def parse_vstpreset(path: Path, root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    text = printable_text(data)
    rel = path.relative_to(root)
    meta = extract_meta(text)

    result: dict[str, Any] = {
        "source_file": str(path),
        "relative_path": str(rel).replace("\\", "/"),
        "file_size_bytes": len(data),
        "file_sha256": hashlib.sha256(data).hexdigest(),
        "vst3_class_id": data[8:40].decode("ascii", errors="ignore") if data.startswith(b"VST3") and len(data) >= 40 else "",
        "meta": meta,
        "plugin_name": meta.get("PlugInName", ""),
        "plugin_vendor": meta.get("PlugInVendor", ""),
        "plugin_category": meta.get("PlugInCategory", ""),
        "extraction": {
            "waves_presets": extract_waves_presets(text),
            "valhalla_settings": extract_valhalla_settings(text),
            "fabfilter_state": extract_fabfilter_summary(data, text),
        },
    }

    formats = []
    if result["extraction"]["waves_presets"]:
        formats.append("waves_realworld")
    if result["extraction"]["valhalla_settings"]:
        formats.append("valhalla_xml")
    if result["extraction"]["fabfilter_state"]:
        formats.append("fabfilter_binary_state")
    result["extraction"]["formats"] = formats or ["unknown"]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract readable Cubase .vstpreset data to JSON.")
    parser.add_argument("--preset-root", type=Path, default=DEFAULT_PRESET_ROOT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    root = args.preset_root
    output_dir = args.output_dir
    if not root.exists():
        raise SystemExit(f"Preset root not found: {root}")

    output_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(root.rglob("*.vstpreset"))
    manifest: list[dict[str, Any]] = []
    for path in files:
        extracted = parse_vstpreset(path, root)
        out_name = safe_name(path.relative_to(root))
        out_path = output_dir / out_name
        out_path.write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest.append(
            {
                "relative_path": extracted["relative_path"],
                "json_file": out_name,
                "plugin_name": extracted["plugin_name"],
                "plugin_vendor": extracted["plugin_vendor"],
                "formats": extracted["extraction"]["formats"],
            }
        )

    (output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "preset_root": str(root),
                "output_dir": str(output_dir),
                "count": len(manifest),
                "items": manifest,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Extracted {len(manifest)} preset files to {output_dir}")


if __name__ == "__main__":
    main()
