#!/usr/bin/env bash
# ================================================================
# process.sh — Process audio files through the L2 limiter
# ================================================================
# Usage:
#   ./scripts/process.sh input.wav output.wav [basic|arc]
#
# Mode:
#   basic (default) — l2_limiter: hard knee, fixed release
#   arc             — l2_arc: soft knee, ARC adaptive release
# ================================================================

set -euo pipefail

INPUT="${1:-}"
OUTPUT="${2:-}"
MODE="${3:-basic}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if [[ -z "$INPUT" || -z "$OUTPUT" ]]; then
    echo "Usage: $0 <input.wav> <output.wav> [basic|arc]"
    exit 1
fi

if [[ ! -f "$INPUT" ]]; then
    echo "Error: input file not found: $INPUT"
    exit 1
fi

case "$MODE" in
    basic) TARGET="l2_limiter" ;;
    arc)   TARGET="l2_arc"     ;;
    *)     echo "Unknown mode '$MODE'. Use: basic | arc"; exit 1 ;;
esac

ensure_command "sox" "Install SoX to inspect input and output stats"
ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"
ensure_parent_writable "$OUTPUT"
ensure_audio_channels "$INPUT" "2" "limiter input"
ensure_binary "$TARGET"

BINARY="$BUILD_DIR/$TARGET"

echo ""
echo "=== Input: $INPUT ==="
sox "$INPUT" -n stat 2>&1 | grep -E "Duration|Channels|Sample Rate|Maximum|Minimum|RMS ampl" || true

echo ""
echo "=== Processing with $TARGET (mode=$MODE) ==="
"$BINARY" "$INPUT" "$OUTPUT"
echo "[process] Output → $OUTPUT"

echo ""
echo "=== Output: $OUTPUT ==="
sox "$OUTPUT" -n stat 2>&1 | grep -E "Duration|Channels|Sample Rate|Maximum|Minimum|RMS ampl" || true

IN_PEAK=$(sox  "$INPUT"  -n stat 2>&1 | grep "Maximum amplitude" | awk '{print $3}')
OUT_PEAK=$(sox "$OUTPUT" -n stat 2>&1 | grep "Maximum amplitude" | awk '{print $3}')
echo ""
echo "=== Summary ==="
echo "  Input  peak: $IN_PEAK"
echo "  Output peak: $OUT_PEAK"
