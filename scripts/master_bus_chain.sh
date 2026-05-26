#!/usr/bin/env bash
# ================================================================
# master_bus_chain.sh — Stereo master-bus chain render
# ================================================================
# Order:
#   1) master_proq3
#   2) master_softclipper
#   3) master_l2_stereo
# ================================================================

set -euo pipefail

INPUT="${1:-}"
OUTPUT="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
    echo "Usage: $0 <input_stereo.wav> <output_stereo.wav>"
    exit 1
fi

if [[ ! -f "$INPUT" ]]; then
    echo "Error: input file not found: $INPUT"
    exit 1
fi

ensure_parent_writable "$OUTPUT"
ensure_audio_channels "$INPUT" "2" "master-bus input"

TMP1="$(make_temp_wav master_chain_stage1)"
TMP2="$(make_temp_wav master_chain_stage2)"
trap 'rm -f "$TMP1" "$TMP2"' EXIT

run_stage() {
    local name="$1"
    local in_file="$2"
    local out_file="$3"

    ensure_binary "$name"
    echo ""
    echo "[run] $name"
    echo "      in : $in_file"
    echo "      out: $out_file"
    "$BUILD_DIR/$name" "$in_file" "$out_file"
}

run_stage "master_proq3" "$INPUT" "$TMP1"
run_stage "master_softclipper" "$TMP1" "$TMP2"
run_stage "master_l2_stereo" "$TMP2" "$OUTPUT"

echo ""
echo "[done] Master-bus chain render finished."
echo "       Order used: master_proq3 -> master_softclipper -> master_l2_stereo"
echo "       Output: $OUTPUT"
