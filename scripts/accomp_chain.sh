#!/usr/bin/env bash
# ================================================================
# accomp_chain.sh — Accompaniment chain render
# ================================================================
# Order:
#   1) accomp_proq3
#   2) accomp_l2_stereo
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
ensure_audio_channels "$INPUT" "2" "accompaniment input"

TMP1="$(make_temp_wav accomp_chain_stage1)"
trap 'rm -f "$TMP1"' EXIT

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

run_stage "accomp_proq3" "$INPUT" "$TMP1"
run_stage "accomp_l2_stereo" "$TMP1" "$OUTPUT"

echo ""
echo "[done] Accompaniment chain render finished."
echo "       Order used: accomp_proq3 -> accomp_l2_stereo"
echo "       Output: $OUTPUT"
