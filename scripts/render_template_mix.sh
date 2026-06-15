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
WITH_STAGE_REPORT=0
WITH_STAGE_REPORT_LOUDNESS=0
MIX_PLAN=""
REFERENCE_AUDIO=""
STAGE_REPORT=""
GLOBAL_DECLICK="auto"
FAST_LOUDNESS_STEPS=""
COMPARE_FAST_LOUDNESS=0
SPATIAL_FX="auto"

shift 4 || true
while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-volume-automation)
            WITH_VOLUME_AUTOMATION=1
            ;;
        --no-loudness-finalizer)
            WITH_LOUDNESS_FINALIZER=0
            ;;
        --global-declick)
            shift
            GLOBAL_DECLICK="${1:-}"
            if [[ "$GLOBAL_DECLICK" != "auto" && "$GLOBAL_DECLICK" != "always" && "$GLOBAL_DECLICK" != "off" ]]; then
                echo "Error: --global-declick must be one of: auto, always, off" >&2
                exit 1
            fi
            ;;
        --no-global-declick)
            GLOBAL_DECLICK="off"
            ;;
        --fast-loudness-steps)
            shift
            FAST_LOUDNESS_STEPS="${1:-}"
            ;;
        --compare-fast-loudness)
            COMPARE_FAST_LOUDNESS=1
            ;;
        --spatial-fx)
            shift
            SPATIAL_FX="${1:-}"
            if [[ "$SPATIAL_FX" != "auto" && "$SPATIAL_FX" != "off" ]]; then
                echo "Error: --spatial-fx must be one of: auto, off" >&2
                exit 1
            fi
            ;;
        --no-spatial-fx)
            SPATIAL_FX="off"
            ;;
        --stage-report)
            WITH_STAGE_REPORT=1
            ;;
        --stage-report-loudness)
            WITH_STAGE_REPORT=1
            WITH_STAGE_REPORT_LOUDNESS=1
            ;;
        --stage-report-path)
            shift
            STAGE_REPORT="${1:-}"
            if [[ -z "$STAGE_REPORT" ]]; then
                echo "Error: --stage-report-path requires a path" >&2
                exit 1
            fi
            WITH_STAGE_REPORT=1
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
        --reference-vocal|--reference-accomp)
            shift
            # accepted for caller compatibility; pipeline does not use delayverb
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
PYTHON_BIN="$(project_python_bin)"

if [[ -z "$TEMPLATE_ID" || -z "$VOCAL_IN" || -z "$ACCOMP_IN" || -z "$FINAL_OUT" ]]; then
    echo "Usage: $0 <template_a|template_b|template_c|template_d> <vocal.wav> <accomp.wav> <final.wav> [--with-volume-automation] [--no-loudness-finalizer] [--global-declick auto|always|off] [--mix-plan plan.json]"
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

if [[ "$WITH_STAGE_REPORT" == "1" ]]; then
    if [[ -z "$STAGE_REPORT" ]]; then
        STAGE_REPORT="${FINAL_OUT%.*}.stage_report.json"
    fi
    rm -f "$STAGE_REPORT"
    if [[ "$WITH_STAGE_REPORT_LOUDNESS" == "1" ]]; then
        echo "[stage-report] enabled with loudness measurements: $STAGE_REPORT"
    else
        echo "[stage-report] enabled (lite, no loudness measurements): $STAGE_REPORT"
    fi
fi

now_ts() {
    "$PYTHON_BIN" -c 'import time; print(f"{time.time():.6f}")'
}

record_stage() {
    local label="$1"
    local start_ts="$2"
    shift 2
    if [[ "$WITH_STAGE_REPORT" != "1" ]]; then
        return
    fi
    local end_ts
    local elapsed
    end_ts="$(now_ts)"
    elapsed="$("$PYTHON_BIN" -c 'import sys; print(f"{float(sys.argv[2]) - float(sys.argv[1]):.6f}")' "$start_ts" "$end_ts")"
    local report_cmd=("$PYTHON_BIN" "$SCRIPT_DIR/record_stage_report.py" \
        --metadata "$STAGE_REPORT" \
        --stage "$label" \
        --elapsed-sec "$elapsed")
    if [[ "$WITH_STAGE_REPORT_LOUDNESS" == "1" ]]; then
        report_cmd+=(--measure-loudness)
    fi
    report_cmd+=("$@")
    "${report_cmd[@]}"
}

RESAMPLED_VOCAL=""
RESAMPLED_ACCOMP=""
TARGET_SAMPLE_RATE=44100
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
VOCAL_SOURCE_EQ="$(make_temp_wav template_vocal_source_eq)"
VOCAL_GROUP="$(make_temp_wav template_vocal_group)"
ACCOMP_1="$(make_temp_wav template_accomp_1)"
ACCOMP_SOURCE_EQ="$(make_temp_wav template_accomp_source_eq)"
ACCOMP_DUCKED="$(make_temp_wav template_accomp_ducked)"
ACCOMP_BUS="$(make_temp_wav template_accomp_bus)"
MIX_TMP="$(make_temp_wav template_mix)"
MIX_TILTED="$(make_temp_wav template_mix_tilted)"
MASTER_1="$(make_temp_wav template_master_1)"
MASTER_2="$(make_temp_wav template_master_2)"

trap 'rm -f "$RESAMPLED_VOCAL" "$RESAMPLED_ACCOMP" "$AUTO_VOCAL" "$AUTO_ACCOMP" "$VOCAL_1" "$VOCAL_2" "$VOCAL_3" "$VOCAL_4" "$VOCAL_5" "$VOCAL_CORRECTED" "$VOCAL_SOURCE_EQ" "$VOCAL_GROUP" "$ACCOMP_1" "$ACCOMP_SOURCE_EQ" "$ACCOMP_DUCKED" "$ACCOMP_BUS" "$MIX_TMP" "$MIX_TILTED" "$MASTER_1" "$MASTER_2"' EXIT

VOCAL_RATE="$(audio_sample_rate "$VOCAL_IN")"
ACCOMP_RATE="$(audio_sample_rate "$ACCOMP_IN")"
RESAMPLED_VOCAL="$(make_temp_wav template_prepped_vocal)"
RESAMPLED_ACCOMP="$(make_temp_wav template_prepped_accomp)"
STAGE_START="$(now_ts)"
echo "[prep] Normalizing input format: vocal ${VOCAL_RATE} Hz -> ${TARGET_SAMPLE_RATE} Hz, mono, pcm_f32le"
ffmpeg -y -hide_banner \
    -i "$VOCAL_IN" \
    -ar "$TARGET_SAMPLE_RATE" \
    -ac 1 \
    -c:a pcm_f32le \
    "$RESAMPLED_VOCAL" >/dev/null 2>&1
VOCAL_SOURCE="$RESAMPLED_VOCAL"
echo "[prep] Normalizing input format: accomp ${ACCOMP_RATE} Hz -> ${TARGET_SAMPLE_RATE} Hz, stereo, pcm_f32le"
ffmpeg -y -hide_banner \
    -i "$ACCOMP_IN" \
    -ar "$TARGET_SAMPLE_RATE" \
    -ac 2 \
    -c:a pcm_f32le \
    "$RESAMPLED_ACCOMP" >/dev/null 2>&1
ACCOMP_SOURCE="$RESAMPLED_ACCOMP"
ensure_matching_sample_rate "$VOCAL_SOURCE" "$ACCOMP_SOURCE" "vocal render input" "accompaniment render input"
record_stage "prep_normalize" "$STAGE_START" \
    --input "vocal=$VOCAL_IN" \
    --input "accomp=$ACCOMP_IN" \
    --output "vocal=$VOCAL_SOURCE" \
    --output "accomp=$ACCOMP_SOURCE"

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

run_binary_stage() {
    local label="$1"
    local binary="$2"
    local in_file="$3"
    local out_file="$4"

    if [[ ! -x "$binary" ]]; then
        echo "Error: binary not executable for $label: $binary" >&2
        exit 1
    fi
    echo ""
    echo "[run] $label"
    echo "      bin: $binary"
    echo "      in : $in_file"
    echo "      out: $out_file"
    "$binary" "$in_file" "$out_file"
}

if [[ "$WITH_VOLUME_AUTOMATION" == "1" ]]; then
    echo "[step 0] Optional volume automation"
    BALANCE_REPORT="${FINAL_OUT%.*}.balance.json"
    STAGE_START="$(now_ts)"
    "$PYTHON_BIN" "$SCRIPT_DIR/auto_volume_mix.py" \
        "$VOCAL_SOURCE" \
        "$ACCOMP_SOURCE" \
        --vocal-out "$AUTO_VOCAL" \
        --accomp-out "$AUTO_ACCOMP" \
        --balance-report "$BALANCE_REPORT"
    VOCAL_SOURCE="$AUTO_VOCAL"
    ACCOMP_SOURCE="$AUTO_ACCOMP"
    ensure_matching_sample_rate "$VOCAL_SOURCE" "$ACCOMP_SOURCE" "auto vocal render input" "auto accompaniment render input"
    record_stage "volume_automation" "$STAGE_START" \
        --input "vocal=$RESAMPLED_VOCAL" \
        --input "accomp=$RESAMPLED_ACCOMP" \
        --output "vocal=$VOCAL_SOURCE" \
        --output "accomp=$ACCOMP_SOURCE"
fi

echo "[step 1] Vocal insert chain: $TEMPLATE_ID"
VOCAL_CHAIN_OUT=""
VOCAL_CHAIN_IN="$VOCAL_SOURCE"
STAGE_START="$(now_ts)"
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
record_stage "vocal_insert_chain" "$STAGE_START" \
    --input "vocal=$VOCAL_CHAIN_IN" \
    --output "vocal=$VOCAL_CHAIN_OUT"

if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 1b] Combined residual/source vocal EQ"
    STAGE_START="$(now_ts)"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_vocal_plan_eq.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_SOURCE_EQ" \
        --plan "$MIX_PLAN"
    record_stage "vocal_plan_eq" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --output "vocal=$VOCAL_SOURCE_EQ"
    VOCAL_CHAIN_OUT="$VOCAL_SOURCE_EQ"
else
    echo "[step 1b] Vocal plan EQ skipped: no mix plan"
fi
STAGE_START="$(now_ts)"
VOCAL_GROUP_FX_BIN="$BUILD_DIR/vocal_group_fx"
ensure_binary "vocal_group_fx"
if [[ -n "$MIX_PLAN" && "$SPATIAL_FX" != "off" ]]; then
    echo "[step 1c] Reference spatial vocal group FX"
    SPATIAL_META="${FINAL_OUT%.*}.spatial_fx.json"
    VOCAL_GROUP_FX_BIN="$("$PYTHON_BIN" "$SCRIPT_DIR/build_spatial_vocal_group.py" \
        --plan "$MIX_PLAN" \
        --metadata "$SPATIAL_META" \
        --mode "$SPATIAL_FX")"
fi
run_binary_stage "vocal_group_fx" "$VOCAL_GROUP_FX_BIN" "$VOCAL_CHAIN_OUT" "$VOCAL_GROUP"
record_stage "vocal_group_fx" "$STAGE_START" \
    --input "vocal=$VOCAL_CHAIN_OUT" \
    --output "vocal=$VOCAL_GROUP"

echo "[step 2] Accompaniment insert chain: $TEMPLATE_ID"
ACCOMP_CHAIN_IN="$ACCOMP_SOURCE"
STAGE_START="$(now_ts)"
case "$TEMPLATE_ID" in
    template_a|template_b)
        run_stage "template_music_proq3_ab" "$ACCOMP_SOURCE" "$ACCOMP_1"
        ;;
    template_c)
        run_stage "template_music_proq3_c" "$ACCOMP_SOURCE" "$ACCOMP_1"
        ;;
esac
ACCOMP_CHAIN_OUT="$ACCOMP_1"
record_stage "accomp_insert_chain" "$STAGE_START" \
    --input "accomp=$ACCOMP_CHAIN_IN" \
    --output "accomp=$ACCOMP_CHAIN_OUT"
if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 2b] Reference accompaniment carve EQ"
    STAGE_START="$(now_ts)"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_plan_source_eq.py" \
        "$ACCOMP_CHAIN_OUT" \
        "$ACCOMP_SOURCE_EQ" \
        --plan "$MIX_PLAN" \
        --section accomp_eq
    record_stage "accomp_plan_eq" "$STAGE_START" \
        --input "accomp=$ACCOMP_CHAIN_OUT" \
        --output "accomp=$ACCOMP_SOURCE_EQ"
    ACCOMP_CHAIN_OUT="$ACCOMP_SOURCE_EQ"
else
    echo "[step 2b] Reference accompaniment carve EQ skipped: no mix plan"
fi
echo "[step 2c] Vocal-aware accompaniment ducking"
STAGE_START="$(now_ts)"
ACCOMP_DUCK_META="${FINAL_OUT%.*}.accomp_duck.json"
DUCK_CMD=("$PYTHON_BIN" "$SCRIPT_DIR/apply_accomp_vocal_duck.py"
    "$ACCOMP_CHAIN_OUT"
    "$VOCAL_GROUP"
    "$ACCOMP_DUCKED"
    --template "$TEMPLATE_ID"
    --metadata "$ACCOMP_DUCK_META"
    --profile-timing)
if [[ -n "$MIX_PLAN" ]]; then
    DUCK_CMD+=(--plan "$MIX_PLAN")
fi
"${DUCK_CMD[@]}"
cp "$ACCOMP_DUCKED" "$ACCOMP_BUS"
record_stage "accomp_vocal_duck" "$STAGE_START" \
    --input "accomp=$ACCOMP_CHAIN_OUT" \
    --input "vocal=$VOCAL_GROUP" \
    --output "accomp=$ACCOMP_BUS"

VOCAL_BUS_GAIN_DB="0.0"
ACCOMP_BUS_GAIN_DB="0.0"
if [[ -n "$MIX_PLAN" ]]; then
    BUS_BALANCE_META="${FINAL_OUT%.*}.bus_balance.json"
    STAGE_START="$(now_ts)"
    read -r _V_GAIN _A_GAIN < <(
        "$PYTHON_BIN" "$SCRIPT_DIR/compute_render_bus_balance.py" \
            "$VOCAL_GROUP" "$ACCOMP_BUS" \
            --plan "$MIX_PLAN" \
            --metadata "$BUS_BALANCE_META" \
            --skip-loudness | tail -1
    )
    [[ -n "${_V_GAIN:-}" ]] && VOCAL_BUS_GAIN_DB="$_V_GAIN"
    [[ -n "${_A_GAIN:-}" ]] && ACCOMP_BUS_GAIN_DB="$_A_GAIN"
    echo "[step 3a] Bus balance from post-FX buses: vocal ${VOCAL_BUS_GAIN_DB} dB, accomp ${ACCOMP_BUS_GAIN_DB} dB"
    record_stage "bus_balance_analysis" "$STAGE_START" \
        --input "vocal=$VOCAL_GROUP" \
        --input "accomp=$ACCOMP_BUS"
fi

echo "[step 3] Stereo Out summing"
STAGE_START="$(now_ts)"
ffmpeg -y -hide_banner \
    -i "$VOCAL_GROUP" \
    -i "$ACCOMP_BUS" \
    -filter_complex "[0:a]volume=${VOCAL_BUS_GAIN_DB}dB[v];[1:a]volume=${ACCOMP_BUS_GAIN_DB}dB[a];[v][a]amix=inputs=2:dropout_transition=0[m]" \
    -map "[m]" \
    "$MIX_TMP" >/dev/null 2>&1
record_stage "stereo_sum" "$STAGE_START" \
    --input "vocal=$VOCAL_GROUP" \
    --input "accomp=$ACCOMP_BUS" \
    --output "mix=$MIX_TMP"

if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 3b] Master tilt EQ from reference features"
    STAGE_START="$(now_ts)"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_master_tilt_eq.py" \
        "$MIX_TMP" \
        "$MIX_TILTED" \
        --plan "$MIX_PLAN"
    record_stage "master_tilt_eq" "$STAGE_START" \
        --input "mix=$MIX_TMP" \
        --output "mix=$MIX_TILTED"
    MIX_INPUT_TO_MASTER="$MIX_TILTED"
else
    MIX_INPUT_TO_MASTER="$MIX_TMP"
fi

echo "[step 4] Master bus chain: Pro-Q3 -> GW MixCentric -> L2 -> loudness finalizer"
MASTER_CHAIN_IN="$MIX_INPUT_TO_MASTER"
STAGE_START="$(now_ts)"
case "$TEMPLATE_ID" in
    template_a|template_b)
        run_stage "template_bus_proq3_ab" "$MIX_INPUT_TO_MASTER" "$MASTER_1"
        ;;
    template_c)
        run_stage "template_bus_proq3_c" "$MIX_INPUT_TO_MASTER" "$MASTER_1"
        ;;
esac
run_stage "gw_mixcentric_stereo" "$MASTER_1" "$MASTER_2"
record_stage "master_bus_chain" "$STAGE_START" \
    --input "mix=$MASTER_CHAIN_IN" \
    --output "mix=$MASTER_2"
ensure_binary "master_l2_stereo"
if [[ "$WITH_LOUDNESS_FINALIZER" == "1" ]]; then
    STAGE_START="$(now_ts)"
    LOUDNESS_CMD=("$PYTHON_BIN" "$SCRIPT_DIR/master_loudness_finalize.py"
        "$MASTER_2"
        "$FINAL_OUT"
        --limiter "$BUILD_DIR/master_l2_stereo"
        --global-declick "$GLOBAL_DECLICK")
    if [[ -n "$REFERENCE_AUDIO" ]]; then
        LOUDNESS_CMD+=(--reference-audio "$REFERENCE_AUDIO")
    fi
    if [[ -n "$MIX_PLAN" ]]; then
        LOUDNESS_CMD+=(--mix-plan "$MIX_PLAN")
    fi
    if [[ -n "$FAST_LOUDNESS_STEPS" ]]; then
        LOUDNESS_CMD+=(--fast-loudness-steps "$FAST_LOUDNESS_STEPS")
    fi
    if [[ "$COMPARE_FAST_LOUDNESS" == "1" ]]; then
        LOUDNESS_CMD+=(--compare-fast-loudness)
    fi
    "${LOUDNESS_CMD[@]}"
    record_stage "master_loudness_finalize" "$STAGE_START" \
        --input "mix=$MASTER_2" \
        --output "mix=$FINAL_OUT"
else
    STAGE_START="$(now_ts)"
    run_stage "master_l2_stereo" "$MASTER_2" "$FINAL_OUT"
    record_stage "master_l2_only" "$STAGE_START" \
        --input "mix=$MASTER_2" \
        --output "mix=$FINAL_OUT"
fi

echo ""
echo "[done] Template render finished."
echo "       Template: $TEMPLATE_ID"
echo "       Output: $FINAL_OUT"
