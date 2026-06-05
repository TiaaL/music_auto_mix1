#!/usr/bin/env bash
# ================================================================
# vocal_stereo_group.sh — Full vocal chain to stereo group output
# ================================================================
# Signal flow:
#   1) rdeesser       -> mono
#   2) req6           -> mono
#   3) c1_comp        -> mono
#   4) vocal HF safety -> mono
#   5) vocal_group_fx -> dry stereo vocal group
#   6) external DelayVerb -> pre-fader send return at 85%
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
REFERENCE_AUDIO=""
REFERENCE_VOCAL=""
REFERENCE_ACCOMP=""
COVER_DRY=""

shift 2 || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --reference-audio)
            shift
            REFERENCE_AUDIO="${1:-}"
            ;;
        --reference-vocal)
            shift
            REFERENCE_VOCAL="${1:-}"
            ;;
        --reference-accomp)
            shift
            REFERENCE_ACCOMP="${1:-}"
            ;;
        --cover-dry)
            shift
            COVER_DRY="${1:-}"
            ;;
        *)
            echo "Error: unsupported option: $1" >&2
            exit 1
            ;;
    esac
    shift
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"
DELAYVERB_SEND="${DELAYVERB_SEND:-0.85}"
DELAYVERB_SEND_PCT="$(python3 - "$DELAYVERB_SEND" <<'PY'
import sys
print(f"{float(sys.argv[1]) * 100:.1f}")
PY
)"

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
TMP_SAFE="$(make_temp_wav faust_vocal_group_hf_safe)"
TMP4="$(make_temp_wav faust_vocal_group_dry)"
trap 'rm -f "$TMP1" "$TMP2" "$TMP3" "$TMP_SAFE" "$TMP4"' EXIT

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
echo "[step 4] Vocal HF safety before group send"
python3 "$SCRIPT_DIR/apply_vocal_hf_safety.py" "$TMP3" "$TMP_SAFE"
run_stage "vocal_group_fx" "$TMP_SAFE" "$TMP4"
echo "[step 6] External DelayVerb group send: pre-fader send ${DELAYVERB_SEND_PCT}%"
DELAYVERB_CMD=(python3 "$SCRIPT_DIR/apply_delayverb_group_fx.py"
    "$TMP4"
    "$OUTPUT"
    --send "$DELAYVERB_SEND"
    --cover-dry "${COVER_DRY:-$INPUT}")
if [[ -n "$REFERENCE_AUDIO" ]]; then
    DELAYVERB_CMD+=(--original-mix "$REFERENCE_AUDIO")
fi
if [[ -n "$REFERENCE_VOCAL" ]]; then
    DELAYVERB_CMD+=(--original-vocal "$REFERENCE_VOCAL")
fi
if [[ -n "$REFERENCE_ACCOMP" ]]; then
    DELAYVERB_CMD+=(--original-accomp "$REFERENCE_ACCOMP")
fi
"${DELAYVERB_CMD[@]}"

show_stats "Output" "$OUTPUT"

echo ""
echo "[done] Stereo vocal group render finished."
echo "       Order used: rdeesser -> req6 -> c1_comp -> hf_safety -> vocal_group_fx(dry) -> delayverb send"
echo "       Output: $OUTPUT"
