#!/usr/bin/env bash
# ================================================================
# vocal_chain.sh — Run the full vocal FX chain in one command
# ================================================================
# Current order:
#   1) rdeesser   -> tame harsh "s"/"sh" sounds first
#   2) req6       -> shape tone with EQ
#   3) c1_comp    -> control dynamics at the end
#
# Usage:
#   ./scripts/vocal_chain.sh input.wav output.wav
#
# Example:
#   ./scripts/vocal_chain.sh vocal_raw.wav vocal_processed.wav
#
# If you want to change the processing order later:
#   Edit the three "run_stage" lines near the bottom of this file.
#   The order of those lines is the actual effect order.
# ================================================================

set -euo pipefail

INPUT="${1:-}"
OUTPUT="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
    echo "Usage: $0 <input.wav> <output.wav>"
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

# Temporary files for intermediate stages.
# Unique names avoid collisions across concurrent runs.
TMP1="$(make_temp_wav faust_vocal_stage1)"
TMP2="$(make_temp_wav faust_vocal_stage2)"
trap 'rm -f "$TMP1" "$TMP2"' EXIT

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
# If you want to change the order in the future,
# just reorder these three lines.
run_stage "rdeesser" "$INPUT" "$TMP1"
run_stage "req6" "$TMP1" "$TMP2"
run_stage "c1_comp" "$TMP2" "$OUTPUT"

show_stats "Output" "$OUTPUT"

echo ""
echo "[done] Vocal chain finished."
echo "       Order used: rdeesser -> req6 -> c1_comp"
echo "       Output: $OUTPUT"
