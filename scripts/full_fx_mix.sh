#!/usr/bin/env bash
# ================================================================
# full_fx_mix.sh — Volume automation first, then FX, then mixdown
# ================================================================
# Signal flow:
#   1) vocal/accompaniment volume automation
#   2) vocal FX chain: rdeesser -> req6 -> c1_comp -> vocal_group_fx(dry) -> external DelayVerb send
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
REFERENCE_AUDIO=""
REFERENCE_VOCAL=""
REFERENCE_ACCOMP=""

shift 3 || true
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
# Volume trim logic is currently disabled.
# VOCAL_MIX_TRIM_DB="-2.5"
# ACCOMP_MIX_TRIM_DB="0.0"
VOCAL_MIX_TRIM_DB="0.0"
ACCOMP_MIX_TRIM_DB="0.0"
D_PREFINAL_PEAK_CONTROL="${D_PREFINAL_PEAK_CONTROL:-1}"
D_PREFINAL_COMP_THRESHOLD="${D_PREFINAL_COMP_THRESHOLD:-0.32}"
D_PREFINAL_COMP_RATIO="${D_PREFINAL_COMP_RATIO:-3.5}"
D_PREFINAL_COMP_MAKEUP="${D_PREFINAL_COMP_MAKEUP:-1.2}"
D_PREFINAL_LIMIT="${D_PREFINAL_LIMIT:-0.94}"
D_PREFINAL_DEBUG_DIR="${D_PREFINAL_DEBUG_DIR:-}"

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

for target in rdeesser req6 c1_comp vocal_group_fx accomp_proq3 accomp_l2_stereo master_l2_stereo; do
    ensure_binary "$target"
done

AUTO_VOCAL="$(make_temp_wav full_chain_auto_vocal)"
AUTO_ACCOMP="$(make_temp_wav full_chain_auto_accomp)"
VOCAL_FX="$(make_temp_wav full_chain_vocal_fx)"
ACCOMP_FX="$(make_temp_wav full_chain_accomp_fx)"
MIX_TMP="$(make_temp_wav full_chain_mix_tmp)"
MIX_HF_SAFE="$(make_temp_wav full_chain_mix_hf_safe)"
MIX_PEAK_SAFE="$(make_temp_wav full_chain_mix_peak_safe)"
ACCOMP_SAFE="$(make_temp_wav full_chain_accomp_safe)"

trap 'rm -f "$AUTO_VOCAL" "$AUTO_ACCOMP" "$VOCAL_FX" "$ACCOMP_FX" "$MIX_TMP" "$MIX_HF_SAFE" "$MIX_PEAK_SAFE" "$ACCOMP_SAFE"' EXIT

echo "[step 1/4] Volume + dynamics shaping (vo/bc rules)"
python3 "$SCRIPT_DIR/auto_volume_mix.py" \
    "$VOCAL_IN" \
    "$ACCOMP_IN" \
    --vocal-out "$AUTO_VOCAL" \
    --accomp-out "$AUTO_ACCOMP"

echo "[step 2/4] Vocal FX chain"
VOCAL_GROUP_CMD=("$SCRIPT_DIR/vocal_stereo_group.sh" "$AUTO_VOCAL" "$VOCAL_FX" --cover-dry "$AUTO_VOCAL")
if [[ -n "$REFERENCE_AUDIO" ]]; then
    VOCAL_GROUP_CMD+=(--reference-audio "$REFERENCE_AUDIO")
fi
if [[ -n "$REFERENCE_VOCAL" ]]; then
    VOCAL_GROUP_CMD+=(--reference-vocal "$REFERENCE_VOCAL")
fi
if [[ -n "$REFERENCE_ACCOMP" ]]; then
    VOCAL_GROUP_CMD+=(--reference-accomp "$REFERENCE_ACCOMP")
fi
"${VOCAL_GROUP_CMD[@]}"

echo "[step 3/4] Accompaniment FX chain"
python3 "$SCRIPT_DIR/apply_accomp_safety.py" "$AUTO_ACCOMP" "$ACCOMP_SAFE"
"$SCRIPT_DIR/accomp_chain.sh" "$ACCOMP_SAFE" "$ACCOMP_FX"

echo "[step 4/4] Stereo mixdown (float intermediate)"
# normalize=0 keeps the relative vocal/accomp balance. The raw sum can exceed
# 0 dBFS, so the intermediate is kept in float (pcm_f32le) to avoid hard-clipping
# the overshoot into broadband "滋啦" crackle. The mix HF-safety alimiter
# (limit=0.985, step 4b) is the clean peak ceiling before the final s16 write.
ffmpeg -y -hide_banner \
    -i "$VOCAL_FX" \
    -i "$ACCOMP_FX" \
    -filter_complex "[0:a]volume=${VOCAL_MIX_TRIM_DB}dB[v];[1:a]volume=${ACCOMP_MIX_TRIM_DB}dB[a];[v][a]amix=inputs=2:normalize=0[m]" \
    -map "[m]" \
    -c:a pcm_f32le \
    "$MIX_TMP" >/dev/null 2>&1

echo "[step 4b/4] Mix HF safety for residual crackle"
python3 "$SCRIPT_DIR/apply_vocal_hf_safety.py" \
    "$MIX_TMP" \
    "$MIX_HF_SAFE" \
    --intensity 0.24 \
    --max-deess 0.44 \
    --static-cut-db -0.35 \
    --static-cut-q 1.2

if [[ -n "$D_PREFINAL_DEBUG_DIR" ]]; then
    mkdir -p "$D_PREFINAL_DEBUG_DIR"
    cp "$MIX_HF_SAFE" "$D_PREFINAL_DEBUG_DIR/mix_hf_safe.wav"
fi

MIX_TO_FINALIZER="$MIX_HF_SAFE"
if [[ "$D_PREFINAL_PEAK_CONTROL" != "0" ]]; then
    echo "[step 4c/4] Pre-finalizer peak control"
    ffmpeg -y -hide_banner \
        -i "$MIX_HF_SAFE" \
        -af "acompressor=threshold=${D_PREFINAL_COMP_THRESHOLD}:ratio=${D_PREFINAL_COMP_RATIO}:attack=1:release=120:knee=3:makeup=${D_PREFINAL_COMP_MAKEUP}:detection=peak:link=maximum,alimiter=limit=${D_PREFINAL_LIMIT}:attack=2:release=120:level=false:latency=true" \
        -c:a pcm_f32le \
        "$MIX_PEAK_SAFE" >/dev/null 2>&1
    if [[ -n "$D_PREFINAL_DEBUG_DIR" ]]; then
        cp "$MIX_PEAK_SAFE" "$D_PREFINAL_DEBUG_DIR/mix_peak_safe.wav"
    fi
    MIX_TO_FINALIZER="$MIX_PEAK_SAFE"
fi

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

echo "[step 4d/4] Master loudness finalizer"
LOUDNESS_CMD=(python3 "$SCRIPT_DIR/master_loudness_finalize.py"
    "$MIX_TO_FINALIZER"
    "$FINAL_OUT"
    --limiter "$BUILD_DIR/master_l2_stereo")
if [[ -n "$REFERENCE_AUDIO" ]]; then
    LOUDNESS_CMD+=(--reference-audio "$REFERENCE_AUDIO")
fi
"${LOUDNESS_CMD[@]}"

echo ""
echo "[done] Full FX mix render finished."
echo "       Order used:"
echo "       volume+dynamics shaping -> vocal FX chain -> accompaniment FX chain -> stereo mix -> mix HF safety -> pre-finalizer peak control -> loudness finalizer"
echo "       Vocal FX: rdeesser -> req6 -> c1_comp -> vocal_group_fx(dry) -> DelayVerb send"
echo "       Accomp FX: accomp_proq3 -> accomp_l2_stereo"
echo "       Mix trim: vocal ${VOCAL_MIX_TRIM_DB} dB, accomp ${ACCOMP_MIX_TRIM_DB} dB"
echo "       Pre-final peak control: enabled=${D_PREFINAL_PEAK_CONTROL}, comp_threshold=${D_PREFINAL_COMP_THRESHOLD}, comp_ratio=${D_PREFINAL_COMP_RATIO}, comp_makeup=${D_PREFINAL_COMP_MAKEUP}, limit=${D_PREFINAL_LIMIT}"
echo "       Output: $FINAL_OUT"
