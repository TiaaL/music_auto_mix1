#!/usr/bin/env bash
# ================================================================
# vocal_stereo_group.sh — Full vocal chain to stereo group output
# ================================================================
# Signal flow:
#   1) rdeesser       -> mono
#   2) req6           -> mono
#   3) c1_comp        -> mono
#   4) vocal_group_fx -> stereo vocal group with 3 FX sends
#
# Usage:
#   ./scripts/vocal_stereo_group.sh input.wav output_stereo.wav
#
# What this script is for:
#   Use this when you want the final rendered vocal to already be a
#   stereo vocal-group style output instead of a plain dry mono track.
#
# If you want to change plugin order:
#   Edit the "run_stage" lines near the bottom of this file.
# ================================================================

set -euo pipefail

INPUT="${1:-}"
OUTPUT="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
    echo "Usage: $0 <input.wav> <output_stereo.wav>"
    exit 1
fi

if [[ ! -f "$INPUT" ]]; then
    echo "Error: input file not found: $INPUT"
    exit 1
fi

ensure_command "sox" "Install SoX to inspect input and output stats"
ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"
ensure_parent_writable "$OUTPUT"
ensure_audio_channels "$INPUT" "1" "vocal input"

TMP1="$(make_temp_wav faust_vocal_group_stage1)"
TMP2="$(make_temp_wav faust_vocal_group_stage2)"
TMP3="$(make_temp_wav faust_vocal_group_stage3)"
trap 'rm -f "$TMP1" "$TMP2" "$TMP3"' EXIT

show_stats() {
    local label="$1"
    local file="$2"

    echo ""
    echo "=== $label: $file ==="
    sox "$file" -n stat 2>&1 | grep -E "Duration|Channels|Sample Rate|Maximum|Minimum|RMS ampl" || true
}

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

show_stats "Input" "$INPUT"

# ------------------------------------------------
# Vocal chain order
# ------------------------------------------------
run_stage "rdeesser" "$INPUT" "$TMP1"
run_stage "req6" "$TMP1" "$TMP2"
run_stage "c1_comp" "$TMP2" "$TMP3"
run_stage "vocal_group_fx" "$TMP3" "$OUTPUT"

show_stats "Output" "$OUTPUT"

echo ""
echo "[done] Stereo vocal group render finished."
echo "       Order used: rdeesser -> req6 -> c1_comp -> vocal_group_fx"
echo "       Output: $OUTPUT"
