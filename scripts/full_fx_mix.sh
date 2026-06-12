#!/usr/bin/env bash
# ================================================================
# full_fx_mix.sh — Volume automation first, then FX, then mixdown
# ================================================================
# Signal flow:
#   1) vocal/accompaniment volume automation
#   2) vocal FX chain: rdeesser -> req6 -> c1_comp -> vocal_group_fx
#   3) accompaniment FX chain: accomp_proq3 -> accomp_l2_stereo
#   4) stereo mixdown
#
# Usage:
#   ./scripts/full_fx_mix.sh vocal.wav accomp.wav final_mix.wav
# ================================================================

set -euo pipefail

VOCAL_IN="${1:-}"
ACCOMP_IN="${2:-}"
FINAL_OUT="${3:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"
PYTHON_BIN="$(project_python_bin)"
# Volume trim logic is currently disabled.
# VOCAL_MIX_TRIM_DB="-2.5"
# ACCOMP_MIX_TRIM_DB="0.0"
VOCAL_MIX_TRIM_DB="0.0"
ACCOMP_MIX_TRIM_DB="0.0"

if [[ -z "$VOCAL_IN" || -z "$ACCOMP_IN" || -z "$FINAL_OUT" ]]; then
    echo "Usage: $0 <vocal.wav> <accomp.wav> <final_mix.wav>"
    exit 1
fi

if [[ ! -f "$VOCAL_IN" ]]; then
    echo "Error: vocal input not found: $VOCAL_IN"
    exit 1
fi

if [[ ! -f "$ACCOMP_IN" ]]; then
    echo "Error: accompaniment input not found: $ACCOMP_IN"
    exit 1
fi

# ensure_command "python3" "Install Python 3 to run scripts/auto_volume_mix.py"
ensure_command "ffmpeg" "Install FFmpeg to run the full mix workflow"
ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"
ensure_parent_writable "$FINAL_OUT"
ensure_audio_channels "$VOCAL_IN" "1" "vocal input"
ensure_audio_channels "$ACCOMP_IN" "2" "accompaniment input"
ensure_matching_sample_rate "$VOCAL_IN" "$ACCOMP_IN" "vocal input" "accompaniment input"

for target in rdeesser req6 c1_comp vocal_group_fx accomp_proq3 accomp_l2_stereo; do
    ensure_binary "$target"
done

AUTO_VOCAL="$(make_temp_wav full_chain_auto_vocal)"
AUTO_ACCOMP="$(make_temp_wav full_chain_auto_accomp)"
VOCAL_FX="$(make_temp_wav full_chain_vocal_fx)"
ACCOMP_FX="$(make_temp_wav full_chain_accomp_fx)"
MIX_TMP="$(make_temp_wav full_chain_mix_tmp)"

trap 'rm -f "$AUTO_VOCAL" "$AUTO_ACCOMP" "$VOCAL_FX" "$ACCOMP_FX" "$MIX_TMP"' EXIT

echo "[step 1/4] Volume + dynamics shaping (vo/bc rules)"
"$PYTHON_BIN" "$SCRIPT_DIR/auto_volume_mix.py" \
    "$VOCAL_IN" \
    "$ACCOMP_IN" \
    --vocal-out "$AUTO_VOCAL" \
    --accomp-out "$AUTO_ACCOMP"

echo "[step 2/4] Vocal FX chain"
"$SCRIPT_DIR/vocal_stereo_group.sh" "$AUTO_VOCAL" "$VOCAL_FX"

echo "[step 3/4] Accompaniment FX chain"
"$SCRIPT_DIR/accomp_chain.sh" "$AUTO_ACCOMP" "$ACCOMP_FX"

echo "[step 4/4] Stereo mixdown"
ffmpeg -y -hide_banner \
    -i "$VOCAL_FX" \
    -i "$ACCOMP_FX" \
    -filter_complex "[0:a]volume=${VOCAL_MIX_TRIM_DB}dB[v];[1:a]volume=${ACCOMP_MIX_TRIM_DB}dB[a];[v][a]amix=inputs=2:dropout_transition=0[m]" \
    -map "[m]" \
    "$MIX_TMP" >/dev/null 2>&1

# MIX_MAX_DB="$(ffmpeg -hide_banner -i "$MIX_TMP" -af volumedetect -f null - 2>&1 | awk '/max_volume:/ {print $5}' | tail -n 1)"
# FINAL_GAIN="0"
# if [[ -n "$MIX_MAX_DB" ]]; then
#     FINAL_GAIN="$(python3 - "$MIX_MAX_DB" <<'PY'
# import sys
# max_db = float(sys.argv[1])
# print(-1.0 - max_db if max_db > -1.0 else 0.0)
# PY
# )"
# fi
#
# ffmpeg -y -hide_banner \
#     -i "$MIX_TMP" \
#     -af "volume=${FINAL_GAIN}dB" \
#     "$FINAL_OUT" >/dev/null 2>&1

ffmpeg -y -hide_banner \
    -i "$MIX_TMP" \
    -c:a pcm_s16le \
    "$FINAL_OUT" >/dev/null 2>&1

echo ""
echo "[done] Full FX mix render finished."
echo "       Order used:"
echo "       volume+dynamics shaping -> vocal FX chain -> accompaniment FX chain -> stereo mix"
echo "       Vocal FX: rdeesser -> req6 -> c1_comp -> vocal_group_fx"
echo "       Accomp FX: accomp_proq3 -> accomp_l2_stereo"
echo "       Mix trim: vocal ${VOCAL_MIX_TRIM_DB} dB, accomp ${ACCOMP_MIX_TRIM_DB} dB"
echo "       Output: $FINAL_OUT"
