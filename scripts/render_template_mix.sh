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
#
# 中文流程概览：
#   1. 统一采样率/声道，并可选先跑 auto_volume_mix.py。
#   2. 按模板执行人声 insert 链，再执行 plan 里的 residual/source/HF 修正。
#   3. 伴奏执行模板 EQ、source cleanup 和人声触发的多频段避让。
#   4. 做局部 section balance guard、stereo sum、master tilt、bus 插件和最终响度。
#   5. 可选导出最终人声贡献轨 / stage_report，方便和原曲人声做效果审计。
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
TIMBRE_REFERENCE_VOCAL=""
STAGE_REPORT=""
GLOBAL_DECLICK="auto"
FAST_LOUDNESS_STEPS=""
COMPARE_FAST_LOUDNESS=0
SPATIAL_FX="auto"
EXPORT_VOCAL_GROUP=""
EXPORT_ACCOMP_BUS=""
DIRECT_VOCAL_SIDE_LAYER="off"

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
        --timbre-reference-vocal)
            shift
            TIMBRE_REFERENCE_VOCAL="${1:-}"
            if [[ -z "$TIMBRE_REFERENCE_VOCAL" ]]; then
                echo "Error: --timbre-reference-vocal requires a path" >&2
                exit 1
            fi
            ;;
        --export-vocal-group)
            shift
            EXPORT_VOCAL_GROUP="${1:-}"
            if [[ -z "$EXPORT_VOCAL_GROUP" ]]; then
                echo "Error: --export-vocal-group requires a path" >&2
                exit 1
            fi
            ;;
        --export-accomp-bus)
            shift
            EXPORT_ACCOMP_BUS="${1:-}"
            if [[ -z "$EXPORT_ACCOMP_BUS" ]]; then
                echo "Error: --export-accomp-bus requires a path" >&2
                exit 1
            fi
            ;;
        --direct-vocal-side-layer)
            shift
            DIRECT_VOCAL_SIDE_LAYER="${1:-}"
            if [[ "$DIRECT_VOCAL_SIDE_LAYER" != "off" && "$DIRECT_VOCAL_SIDE_LAYER" != "light" ]]; then
                echo "Error: --direct-vocal-side-layer must be one of: off, light" >&2
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
if [[ -n "$EXPORT_VOCAL_GROUP" ]]; then
    ensure_parent_writable "$EXPORT_VOCAL_GROUP"
fi
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
VOCAL_TIMBRE_PRE="$(make_temp_wav template_vocal_timbre_pre)"
VOCAL_TIMBRE_GUARDED="$(make_temp_wav template_vocal_timbre_guarded)"
VOCAL_SOURCE_EQ="$(make_temp_wav template_vocal_source_eq)"
VOCAL_ARTIFACT_REPAIRED="$(make_temp_wav template_vocal_artifact_repaired)"
VOCAL_DYNAMIC="$(make_temp_wav template_vocal_dynamic)"
VOCAL_EVENT_GUARDED="$(make_temp_wav template_vocal_event_guarded)"
VOCAL_GROUP="$(make_temp_wav template_vocal_group)"
VOCAL_GROUP_SIDE="$(make_temp_wav template_vocal_group_side)"
VOCAL_GROUP_TIMBRE="$(make_temp_wav template_vocal_group_timbre)"
ACCOMP_1="$(make_temp_wav template_accomp_1)"
ACCOMP_SOURCE_EQ="$(make_temp_wav template_accomp_source_eq)"
ACCOMP_DUCKED="$(make_temp_wav template_accomp_ducked)"
ACCOMP_BUS="$(make_temp_wav template_accomp_bus)"
VOCAL_BALANCED="$(make_temp_wav template_vocal_section_balanced)"
ACCOMP_BALANCED="$(make_temp_wav template_accomp_section_balanced)"
MIX_TMP="$(make_temp_wav template_mix)"
MIX_TILTED="$(make_temp_wav template_mix_tilted)"
MASTER_1="$(make_temp_wav template_master_1)"
MASTER_2="$(make_temp_wav template_master_2)"
FINAL_GUARDED="$(make_temp_wav template_final_guarded)"

trap 'rm -f "$RESAMPLED_VOCAL" "$RESAMPLED_ACCOMP" "$AUTO_VOCAL" "$AUTO_ACCOMP" "$VOCAL_1" "$VOCAL_2" "$VOCAL_3" "$VOCAL_4" "$VOCAL_5" "$VOCAL_CORRECTED" "$VOCAL_TIMBRE_PRE" "$VOCAL_TIMBRE_GUARDED" "$VOCAL_SOURCE_EQ" "$VOCAL_ARTIFACT_REPAIRED" "$VOCAL_DYNAMIC" "$VOCAL_EVENT_GUARDED" "$VOCAL_GROUP" "$VOCAL_GROUP_SIDE" "$VOCAL_GROUP_TIMBRE" "$ACCOMP_1" "$ACCOMP_SOURCE_EQ" "$ACCOMP_DUCKED" "$ACCOMP_BUS" "$VOCAL_BALANCED" "$ACCOMP_BALANCED" "$MIX_TMP" "$MIX_TILTED" "$MASTER_1" "$MASTER_2" "$FINAL_GUARDED"' EXIT

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

SKIP_ONEKNOB_BRIGHTER=0
if [[ -n "$MIX_PLAN" ]]; then
    # 模板链开关由统一 plan 决策；例如目标音色偏暗时，模板 C 的 brighter 可以跳过。
    SKIP_ONEKNOB_BRIGHTER="$("$PYTHON_BIN" "$SCRIPT_DIR/plan_template_chain_flags.py" "$MIX_PLAN" --flag skip-oneknob-brighter)"
fi

VOCAL_TEMPLATE_IN="$VOCAL_SOURCE"
if [[ -n "$MIX_PLAN" ]]; then
    # 音色相似度先进入模板 insert 链，让后面的压缩/模板 EQ 基于更接近筛选片段的干声工作。
    # 这里只处理 timbre 动作，不做瑕疵清理，避免把两个目标混在一起。
    echo "[step 0b] Timbre reference EQ before vocal insert chain"
    STAGE_START="$(now_ts)"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_vocal_plan_eq.py" \
        "$VOCAL_SOURCE" \
        "$VOCAL_TIMBRE_PRE" \
        --plan "$MIX_PLAN" \
        --eq-stage timbre
    record_stage "vocal_timbre_pre_eq" "$STAGE_START" \
        --input "vocal=$VOCAL_SOURCE" \
        --output "vocal=$VOCAL_TIMBRE_PRE"
    VOCAL_TEMPLATE_IN="$VOCAL_TIMBRE_PRE"
fi

echo "[step 1] Vocal insert chain: $TEMPLATE_ID"
VOCAL_CHAIN_OUT=""
VOCAL_CHAIN_IN="$VOCAL_TEMPLATE_IN"
STAGE_START="$(now_ts)"
case "$TEMPLATE_ID" in
    template_a)
        run_stage "c1_gate" "$VOCAL_TEMPLATE_IN" "$VOCAL_1"
        run_stage "template_a_vocal_proq3" "$VOCAL_1" "$VOCAL_2"
        run_stage "c1_comp" "$VOCAL_2" "$VOCAL_3"
        run_stage "sibilance_mono" "$VOCAL_3" "$VOCAL_4"
        VOCAL_CHAIN_OUT="$VOCAL_4"
        ;;
    template_b)
        run_stage "rbass_mono" "$VOCAL_TEMPLATE_IN" "$VOCAL_1"
        run_stage "f6_rta_mono" "$VOCAL_1" "$VOCAL_2"
        run_stage "c1_comp" "$VOCAL_2" "$VOCAL_3"
        run_stage "sibilance_mono" "$VOCAL_3" "$VOCAL_4"
        run_stage "l1_limiter_mono" "$VOCAL_4" "$VOCAL_5"
        VOCAL_CHAIN_OUT="$VOCAL_5"
        ;;
    template_c)
        run_stage "template_c_vocal_proq3" "$VOCAL_TEMPLATE_IN" "$VOCAL_1"
        run_stage "vocal_rider_mono" "$VOCAL_1" "$VOCAL_2"
        run_stage "c1_comp" "$VOCAL_2" "$VOCAL_3"
        if [[ "$SKIP_ONEKNOB_BRIGHTER" == "1" ]]; then
            echo ""
            echo "[skip] oneknob_brighter_mono (timbre target is darker than template C brighter)"
            cp "$VOCAL_3" "$VOCAL_4"
        else
            run_stage "oneknob_brighter_mono" "$VOCAL_3" "$VOCAL_4"
        fi
        VOCAL_CHAIN_OUT="$VOCAL_4"
        ;;
esac
record_stage "vocal_insert_chain" "$STAGE_START" \
    --input "vocal=$VOCAL_CHAIN_IN" \
    --output "vocal=$VOCAL_CHAIN_OUT"

if [[ -n "$MIX_PLAN" ]]; then
    # 模板链会重新染色；链后再按音色筛选片段做一次轻校，防止相似度被模板洗掉。
    echo "[step 1a2] Post-template timbre preservation"
    STAGE_START="$(now_ts)"
    VOCAL_TIMBRE_GUARD_META="${FINAL_OUT%.*}.timbre_chain_guard.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_timbre_chain_guard.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_TIMBRE_GUARDED" \
        --plan "$MIX_PLAN" \
        --metadata "$VOCAL_TIMBRE_GUARD_META"
    record_stage "vocal_timbre_chain_guard" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --output "vocal=$VOCAL_TIMBRE_GUARDED"
    VOCAL_CHAIN_OUT="$VOCAL_TIMBRE_GUARDED"
    # timbre 之后只做清理/保护：低中频堆积、刺耳、齿音、Nyquist 颗粒等都在这里兜底。
    echo "[step 1b] Post-timbre vocal cleanup EQ"
    STAGE_START="$(now_ts)"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_vocal_plan_eq.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_SOURCE_EQ" \
        --plan "$MIX_PLAN" \
        --eq-stage post_timbre
    record_stage "vocal_plan_eq" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --output "vocal=$VOCAL_SOURCE_EQ"
    VOCAL_CHAIN_OUT="$VOCAL_SOURCE_EQ"
    echo "[step 1b2] Vocal artifact repair"
    STAGE_START="$(now_ts)"
    VOCAL_ARTIFACT_META="${FINAL_OUT%.*}.vocal_artifact_repair.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_vocal_artifact_repair.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_ARTIFACT_REPAIRED" \
        --plan "$MIX_PLAN" \
        --metadata "$VOCAL_ARTIFACT_META"
    record_stage "vocal_artifact_repair" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --output "vocal=$VOCAL_ARTIFACT_REPAIRED"
    VOCAL_CHAIN_OUT="$VOCAL_ARTIFACT_REPAIRED"
    # 人声“没劲”只在微动态层处理；不改变总线比例，也不直接提高整体响度。
    echo "[step 1b3] Vocal dynamic lift"
    STAGE_START="$(now_ts)"
    VOCAL_DYNAMIC_META="${FINAL_OUT%.*}.vocal_dynamic_lift.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_vocal_dynamic_lift.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_DYNAMIC" \
        --plan "$MIX_PLAN" \
        --metadata "$VOCAL_DYNAMIC_META"
    record_stage "vocal_dynamic_lift" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --output "vocal=$VOCAL_DYNAMIC"
    VOCAL_CHAIN_OUT="$VOCAL_DYNAMIC"
    # 短事件保护只处理句首气声/短塌陷等局部问题，避免这些瞬态误触发后面的伴奏让位。
    echo "[step 1b4] Vocal short-event guard"
    STAGE_START="$(now_ts)"
    VOCAL_EVENT_META="${FINAL_OUT%.*}.vocal_event_guard.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_vocal_event_guard.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_EVENT_GUARDED" \
        --plan "$MIX_PLAN" \
        --metadata "$VOCAL_EVENT_META"
    record_stage "vocal_event_guard" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --output "vocal=$VOCAL_EVENT_GUARDED"
    VOCAL_CHAIN_OUT="$VOCAL_EVENT_GUARDED"
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
if [[ "$DIRECT_VOCAL_SIDE_LAYER" != "off" ]]; then
    echo "[step 1d] Direct vocal side layer: $DIRECT_VOCAL_SIDE_LAYER"
    STAGE_START="$(now_ts)"
    DIRECT_SIDE_META="${FINAL_OUT%.*}.direct_vocal_side_layer.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_direct_vocal_side_layer.py" \
        "$VOCAL_CHAIN_OUT" \
        "$VOCAL_GROUP" \
        "$VOCAL_GROUP_SIDE" \
        --mode "$DIRECT_VOCAL_SIDE_LAYER" \
        --metadata "$DIRECT_SIDE_META"
    record_stage "direct_vocal_side_layer" "$STAGE_START" \
        --input "vocal=$VOCAL_CHAIN_OUT" \
        --input "vocal_group=$VOCAL_GROUP" \
        --output "vocal=$VOCAL_GROUP_SIDE"
    cp "$VOCAL_GROUP_SIDE" "$VOCAL_GROUP"
fi
if [[ -n "$MIX_PLAN" ]]; then
    # vocal_group_fx 的空间/总线处理也可能改变听感音色；最终入总线前再轻校一次。
    echo "[step 1e] Post-vocal-group timbre preservation"
    STAGE_START="$(now_ts)"
    VOCAL_GROUP_TIMBRE_META="${FINAL_OUT%.*}.post_group_timbre_guard.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_timbre_chain_guard.py" \
        "$VOCAL_GROUP" \
        "$VOCAL_GROUP_TIMBRE" \
        --plan "$MIX_PLAN" \
        --stage post_group \
        --metadata "$VOCAL_GROUP_TIMBRE_META"
    record_stage "post_group_timbre_guard" "$STAGE_START" \
        --input "vocal_group=$VOCAL_GROUP" \
        --output "vocal_group=$VOCAL_GROUP_TIMBRE"
    VOCAL_GROUP="$VOCAL_GROUP_TIMBRE"
fi
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
if [[ -n "$EXPORT_ACCOMP_BUS" ]]; then
    mkdir -p "$(dirname "$EXPORT_ACCOMP_BUS")"
    cp "$ACCOMP_BUS" "$EXPORT_ACCOMP_BUS"
    echo "[audit] Exported post-FX accompaniment bus: $EXPORT_ACCOMP_BUS"
fi
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

VOCAL_SUM_INPUT="$VOCAL_GROUP"
ACCOMP_SUM_INPUT="$ACCOMP_BUS"
VOCAL_SUM_GAIN_DB="$VOCAL_BUS_GAIN_DB"
ACCOMP_SUM_GAIN_DB="$ACCOMP_BUS_GAIN_DB"
if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 3b] Reference-window section balance guard"
    STAGE_START="$(now_ts)"
    SECTION_BALANCE_META="${FINAL_OUT%.*}.section_balance_guard.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_section_balance_guard.py" \
        "$VOCAL_GROUP" \
        "$ACCOMP_BUS" \
        "$VOCAL_BALANCED" \
        "$ACCOMP_BALANCED" \
        --plan "$MIX_PLAN" \
        --vocal-gain-db "$VOCAL_BUS_GAIN_DB" \
        --accomp-gain-db "$ACCOMP_BUS_GAIN_DB" \
        --metadata "$SECTION_BALANCE_META"
    VOCAL_SUM_INPUT="$VOCAL_BALANCED"
    ACCOMP_SUM_INPUT="$ACCOMP_BALANCED"
    VOCAL_SUM_GAIN_DB="0.0"
    ACCOMP_SUM_GAIN_DB="0.0"
    record_stage "section_balance_guard" "$STAGE_START" \
        --input "vocal=$VOCAL_GROUP" \
        --input "accomp=$ACCOMP_BUS" \
        --output "vocal=$VOCAL_BALANCED" \
        --output "accomp=$ACCOMP_BALANCED"
fi

if [[ -n "$EXPORT_VOCAL_GROUP" ]]; then
    mkdir -p "$(dirname "$EXPORT_VOCAL_GROUP")"
    # 导出审计用的人声轨必须是最终入 stereo sum 的人声贡献：
    # 包含 vocal_group FX、post-group 音色保护、bus gain 和 section guard 后的动态变化。
    ffmpeg -y -hide_banner \
        -i "$VOCAL_SUM_INPUT" \
        -filter:a "volume=${VOCAL_SUM_GAIN_DB}dB" \
        "$EXPORT_VOCAL_GROUP" >/dev/null 2>&1
    echo "[audit] Exported final vocal contribution: $EXPORT_VOCAL_GROUP"
fi

echo "[step 3] Stereo Out summing"
STAGE_START="$(now_ts)"
ffmpeg -y -hide_banner \
    -i "$VOCAL_SUM_INPUT" \
    -i "$ACCOMP_SUM_INPUT" \
    -filter_complex "[0:a]volume=${VOCAL_SUM_GAIN_DB}dB[v];[1:a]volume=${ACCOMP_SUM_GAIN_DB}dB[a];[v][a]amix=inputs=2:dropout_transition=0[m]" \
    -map "[m]" \
    "$MIX_TMP" >/dev/null 2>&1
record_stage "stereo_sum" "$STAGE_START" \
    --input "vocal=$VOCAL_SUM_INPUT" \
    --input "accomp=$ACCOMP_SUM_INPUT" \
    --output "mix=$MIX_TMP"

if [[ -n "$MIX_PLAN" ]]; then
    echo "[step 3c] Master tilt EQ from reference features"
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
    echo "[step 4b] Final transient safety guard"
    STAGE_START="$(now_ts)"
    FINAL_GUARD_META="${FINAL_OUT%.*}.final_transient_guard.json"
    "$PYTHON_BIN" "$SCRIPT_DIR/apply_final_transient_guard.py" \
        "$FINAL_OUT" \
        "$FINAL_GUARDED" \
        --metadata "$FINAL_GUARD_META"
    cp "$FINAL_GUARDED" "$FINAL_OUT"
    record_stage "final_transient_guard" "$STAGE_START" \
        --input "mix=$FINAL_OUT" \
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
