#!/usr/bin/env bash
# ================================================================
# render_template_mix.sh - render a selected Cubase-style template
# ================================================================
# Usage:
#   ./scripts/render_template_mix.sh template_a vocal.wav accomp.wav out.wav
#   ./scripts/render_template_mix.sh template_b vocal.wav accomp.wav out.wav --with-volume-automation
#   ./scripts/render_template_mix.sh template_b vocal.wav accomp.wav out.wav --no-loudness-finalizer
#
# A/B/C use template-specific plugin-order DSP approximations.
# Template D delegates to the current full_fx_mix.sh fallback.
# ================================================================

set -euo pipefail

TEMPLATE_ID="${1:-}"
VOCAL_IN="${2:-}"
ACCOMP_IN="${3:-}"
FINAL_OUT="${4:-}"
WITH_VOLUME_AUTOMATION=0
WITH_LOUDNESS_FINALIZER=1
MIX_PLAN=""
REFERENCE_AUDIO=""

shift 4 || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-volume-automation)
            WITH_VOLUME_AUTOMATION=1
            ;;
        --no-loudness-finalizer)
            WITH_LOUDNESS_FINALIZER=0
            ;;
        --mix-plan)
            shift
            MIX_PLAN="${1:-}"
            if [[ -z "$MIX_PLAN" ]]; then
                echo "Error: --mix-plan requires a path" >&2
                exit 1
            fi
            ;;
        --reference-audio)
            shift
            REFERENCE_AUDIO="${1:-}"
            if [[ -z "$REFERENCE_AUDIO" ]]; then
                echo "Error: --reference-audio requires a path" >&2
                exit 1
            fi
            ;;
        *)
            echo "Error: unsupported option: $1" >&2
            exit 1
            ;;
    esac
    shift
done

SCRIPT_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_PATH" in
    */*) SCRIPT_DIR_RAW="${SCRIPT_PATH%/*}" ;;
    *) SCRIPT_DIR_RAW="." ;;
esac
SCRIPT_DIR="$(cd "$SCRIPT_DIR_RAW" && pwd)"
if [[ -f "$SCRIPT_DIR/msys_template_env.sh" && -d "$SCRIPT_DIR/../.tools/msys64" ]]; then
    MSYS_TEMPLATE_ENV_QUIET=1 source "$SCRIPT_DIR/msys_template_env.sh"
fi
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if [[ -z "$TEMPLATE_ID" || -z "$VOCAL_IN" || -z "$ACCOMP_IN" || -z "$FINAL_OUT" ]]; then
    echo "Usage: $0 <template_a|template_b|template_c|template_d> <vocal.wav> <accomp.wav> <final.wav> [--with-volume-automation] [--no-loudness-finalizer] [--mix-plan plan.json]"
    exit 1
fi

case "$TEMPLATE_ID" in
    template_a|template_b|template_c|template_d) ;;
    *)
        echo "Error: unsupported template id: $TEMPLATE_ID" >&2
        exit 1
        ;;
esac

if [[ "$TEMPLATE_ID" == "template_d" ]]; then
    exec "$SCRIPT_DIR/full_fx_mix.sh" "$VOCAL_IN" "$ACCOMP_IN" "$FINAL_OUT"
fi

if [[ ! -f "$VOCAL_IN" ]]; then
    echo "Error: vocal input not found: $VOCAL_IN" >&2
    exit 1
fi

if [[ ! -f "$ACCOMP_IN" ]]; then
    echo "Error: accompaniment input not found: $ACCOMP_IN" >&2
    exit 1
fi

ensure_command "ffmpeg" "Install FFmpeg to run template rendering"
ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"
ensure_parent_writable "$FINAL_OUT"
ensure_audio_channels "$VOCAL_IN" "1" "vocal input"
ensure_audio_channels "$ACCOMP_IN" "2" "accompaniment input"

RESAMPLED_VOCAL=""
RESAMPLED_ACCOMP=""
AUTO_VOCAL="$(make_temp_wav template_auto_vocal)"
AUTO_ACCOMP="$(make_temp_wav template_auto_accomp)"
VOCAL_SOURCE="$VOCAL_IN"
ACCOMP_SOURCE="$ACCOMP_IN"
VOCAL_1="$(make_temp_wav template_vocal_1)"
VOCAL_2="$(make_temp_wav template_vocal_2)"
VOCAL_3="$(make_temp_wav template_vocal_3)"
VOCAL_4="$(make_temp_wav template_vocal_4)"
VOCAL_5="$(make_temp_wav template_vocal_5)"
VOCAL_CORRECTED="$(make_temp_wav template_vocal_residual_eq)"
VOCAL_GROUP="$(make_temp_wav template_vocal_group)"
ACCOMP_1="$(make_temp_wav template_accomp_1)"
ACCOMP_BUS="$(make_temp_wav template_accomp_bus)"
MIX_TMP="$(make_temp_wav template_mix)"
MIX_TILTED="$(make_temp_wav template_mix_tilted)"
MASTER_1="$(make_temp_wav template_master_1)"
MASTER_2="$(make_temp_wav template_master_2)"

trap 'rm -f "$RESAMPLED_VOCAL" "$RESAMPLED_ACCOMP" "$AUTO_VOCAL" "$AUTO_ACCOMP" "$VOCAL_1" "$VOCAL_2" "$VOCAL_3" "$VOCAL_4" "$VOCAL_5" "$VOCAL_CORRECTED" "$VOCAL_GROUP" "$ACCOMP_1" "$ACCOMP_BUS" "$MIX_TMP" "$MIX_TILTED" "$MASTER_1" "$MASTER_2"' EXIT

VOCAL_RATE="$(audio_sample_rate "$VOCAL_IN")"
ACCOMP_RATE="$(audio_sample_rate "$ACCOMP_IN")"
if [[ "$VOCAL_RATE" != "$ACCOMP_RATE" ]]; then
    RESAMPLED_VOCAL="$(make_temp_wav template_resampled_vocal)"
    echo "[prep] Resampling vocal $VOCAL_RATE Hz -> $ACCOMP_RATE Hz"
    ffmpeg -y -hide_banner \
        -i "$VOCAL_IN" \
        -ar "$ACCOMP_RATE" \
        -ac 1 \
        "$RESAMPLED_VOCAL" >/dev/null 2>&1
    VOCAL_SOURCE="$RESAMPLED_VOCAL"
fi
ensure_matching_sample_rate "$VOCAL_SOURCE" "$ACCOMP_SOURCE" "vocal render input" "accompaniment render input"

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

if [[ "$WITH_VOLUME_AUTOMATION" == "1" ]]; then
    echo "[step 0] Optional volume automation"
    BALANCE_REPORT="${FINAL_OUT%.*}.balance.json"
    python3 "$SCRIPT_DIR/auto_volume_mix.py" \
        "$VOCAL_SOURCE" \
        "$ACCOMP_SOURCE" \
        --vocal-out "$AUTO_VOCAL" \
        --accomp-out "$AUTO_ACCOMP" \
        --balance-report "$BALANCE_REPORT"
    VOCAL_SOURCE="$AUTO_VOCAL"
    ACCOMP_SOURCE="$AUTO_ACCOMP"
    ensure_matching_sample_rate "$VOCAL_SOURCE" "$ACCOMP_SOURCE" "auto vocal render input" "auto accompaniment render input"
fi

echo "[step 1] Vocal insert chain: $TEMPLATE_ID"
VOCAL_CHAIN_OUT=""
case "$TEMPLATE_ID" in
    template_a)
        run_stage "c1_gate" "$VOCAL_SOURCE" "$VOCAL_1"
        run_stage "template_a_vocal_proq3" "$VOCAL_1" "$VOCAL_2"
        run_stage "c1_comp" "$VOCAL_2" "$VOCAL_3"
        run_stage "sibilance_mono" "$VOCAL_3" "$VOCAL_4"
        VOCAL_CHAIN_OUT="$VOCAL_4"
        ;;
    template_b)
        run_stage "rbass_mono" "$VOCAL_SOURCE" "$VOCAL_1"
        run_stage "f6_rta_mono" "$VOCAL_1" "$VOCAL_2"
        run_stage "c1_comp" "$VOCAL_2" "$VOCAL_3"
        run_stage "sibilance_mono" "$VOCAL_3" "$VOCAL_4"
        run_stage "l1_limiter_mono" "$VOCAL_4" "$VOCAL_5"
        VOCAL_CHAIN_OUT="$VOCAL_5"
        ;;
    template_c)
        run_stage "template_c_vocal_proq3" "$VOCAL_SOURCE" "$VOCAL_1"
        run_stage "vocal_rider_mono" "$VOCAL_1" "$VOCAL_2"
        run_stage "c1_comp" "$VOCAL_2" "$VOCAL_3"
        run_stage "oneknob_brighter_mono" "$VOCAL_3" "$VOCAL_4"
        VOCAL_CHAIN_OUT="$VOCAL_4"
        ;;
esac

if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 1b] Residual vocal EQ from mix plan"
    python3 "$SCRIPT_DIR/apply_residual_vocal_eq.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_CORRECTED" \
        --plan "$MIX_PLAN"
    VOCAL_CHAIN_OUT="$VOCAL_CORRECTED"
else
    echo "[step 1b] Residual vocal EQ skipped: no mix plan"
fi
run_stage "vocal_group_fx" "$VOCAL_CHAIN_OUT" "$VOCAL_GROUP"

echo "[step 2] Accompaniment insert chain: $TEMPLATE_ID"
case "$TEMPLATE_ID" in
    template_a|template_b)
        run_stage "template_music_proq3_ab" "$ACCOMP_SOURCE" "$ACCOMP_1"
        ;;
    template_c)
        run_stage "template_music_proq3_c" "$ACCOMP_SOURCE" "$ACCOMP_1"
        ;;
esac
cp "$ACCOMP_1" "$ACCOMP_BUS"

VOCAL_BUS_GAIN_DB="0.0"
ACCOMP_BUS_GAIN_DB="0.0"
if [[ -n "$MIX_PLAN" ]]; then
    read -r _V_GAIN _A_GAIN < <(python3 "$SCRIPT_DIR/plan_bus_gains.py" "$MIX_PLAN") || true
    [[ -n "${_V_GAIN:-}" ]] && VOCAL_BUS_GAIN_DB="$_V_GAIN"
    [[ -n "${_A_GAIN:-}" ]] && ACCOMP_BUS_GAIN_DB="$_A_GAIN"
    echo "[step 3a] Bus balance from reference: vocal ${VOCAL_BUS_GAIN_DB} dB, accomp ${ACCOMP_BUS_GAIN_DB} dB"
fi

echo "[step 3] Stereo Out summing"
ffmpeg -y -hide_banner \
    -i "$VOCAL_GROUP" \
    -i "$ACCOMP_BUS" \
    -filter_complex "[0:a]volume=${VOCAL_BUS_GAIN_DB}dB[v];[1:a]volume=${ACCOMP_BUS_GAIN_DB}dB[a];[v][a]amix=inputs=2:normalize=0[m]" \
    -map "[m]" \
    "$MIX_TMP" >/dev/null 2>&1

if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 3b] Master tilt EQ from reference features"
    python3 "$SCRIPT_DIR/apply_master_tilt_eq.py" \
        "$MIX_TMP" \
        "$MIX_TILTED" \
        --plan "$MIX_PLAN"
    MIX_INPUT_TO_MASTER="$MIX_TILTED"
else
    MIX_INPUT_TO_MASTER="$MIX_TMP"
fi

echo "[step 4] Master bus chain: Pro-Q3 -> GW MixCentric -> L2 -> loudness finalizer"
case "$TEMPLATE_ID" in
    template_a|template_b)
        run_stage "template_bus_proq3_ab" "$MIX_INPUT_TO_MASTER" "$MASTER_1"
        ;;
    template_c)
        run_stage "template_bus_proq3_c" "$MIX_INPUT_TO_MASTER" "$MASTER_1"
        ;;
esac
run_stage "gw_mixcentric_stereo" "$MASTER_1" "$MASTER_2"
ensure_binary "master_l2_stereo"
if [[ "$WITH_LOUDNESS_FINALIZER" == "1" ]]; then
    LOUDNESS_CMD=(python3 "$SCRIPT_DIR/master_loudness_finalize.py"
        "$MASTER_2"
        "$FINAL_OUT"
        --limiter "$BUILD_DIR/master_l2_stereo")
    if [[ -n "$REFERENCE_AUDIO" ]]; then
        LOUDNESS_CMD+=(--reference-audio "$REFERENCE_AUDIO")
    fi
    "${LOUDNESS_CMD[@]}"
else
    run_stage "master_l2_stereo" "$MASTER_2" "$FINAL_OUT"
fi

echo ""
echo "[done] Template render finished."
echo "       Template: $TEMPLATE_ID"
echo "       Output: $FINAL_OUT"
