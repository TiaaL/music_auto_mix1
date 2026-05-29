#!/usr/bin/env bash
# ================================================================
# render_calibration_stages.sh - save Faust stage outputs for DAW comparison
# ================================================================
# Usage:
#   ./scripts/render_calibration_stages.sh --vocal-in vocal.wav --music-in accomp.wav --bus-in mix.wav --out-dir out/calibration
#
# The output filenames mirror config/daw_calibration_stages.json so
# scripts/daw_reference_compare.py can match them against Cubase mix_results.
# This is a calibration/debug entrypoint; production renders still use
# render_template_mix.sh.
# ================================================================

set -euo pipefail

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

VOCAL_IN=""
MUSIC_IN=""
BUS_IN=""
OUT_DIR="calibration_outputs/faust_stages"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vocal-in)
            VOCAL_IN="${2:-}"
            shift 2
            ;;
        --music-in|--accomp-in)
            MUSIC_IN="${2:-}"
            shift 2
            ;;
        --bus-in)
            BUS_IN="${2:-}"
            shift 2
            ;;
        --out-dir)
            OUT_DIR="${2:-}"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--vocal-in mono.wav] [--music-in stereo.wav] [--bus-in stereo.wav] [--out-dir DIR]"
            exit 0
            ;;
        *)
            echo "Error: unknown argument: $1" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$VOCAL_IN" && -z "$MUSIC_IN" && -z "$BUS_IN" ]]; then
    echo "Error: provide at least one of --vocal-in, --music-in, or --bus-in" >&2
    exit 1
fi

ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"
mkdir -p "$OUT_DIR"

run_stage() {
    local name="$1"
    local in_file="$2"
    local out_file="$3"

    ensure_binary "$name"
    mkdir -p "$(dirname "$out_file")"
    echo ""
    echo "[run] $name"
    echo "      in : $in_file"
    echo "      out: $out_file"
    "$BUILD_DIR/$name" "$in_file" "$out_file"
}

if [[ -n "$VOCAL_IN" ]]; then
    if [[ ! -f "$VOCAL_IN" ]]; then
        echo "Error: vocal input not found: $VOCAL_IN" >&2
        exit 1
    fi
    ensure_audio_channels "$VOCAL_IN" "1" "vocal input"
    mkdir -p "$OUT_DIR/vocal"

    VOCAL_DEESSER="$OUT_DIR/vocal/vocal_deesser.wav"
    VOCAL_EQ="$OUT_DIR/vocal/vocal_deesser_eq.wav"
    VOCAL_COMP="$OUT_DIR/vocal/vocal_deesser_eq_compressor.wav"
    VOCAL_FX="$OUT_DIR/vocal/vocal_deesser_eq_compressor_platereverb_bigreverb_delay.wav"

    echo "[branch] vocal calibration chain"
    run_stage "rdeesser" "$VOCAL_IN" "$VOCAL_DEESSER"
    run_stage "req6" "$VOCAL_DEESSER" "$VOCAL_EQ"
    run_stage "c1_comp" "$VOCAL_EQ" "$VOCAL_COMP"
    run_stage "vocal_group_fx" "$VOCAL_COMP" "$VOCAL_FX"
fi

if [[ -n "$MUSIC_IN" ]]; then
    if [[ ! -f "$MUSIC_IN" ]]; then
        echo "Error: music input not found: $MUSIC_IN" >&2
        exit 1
    fi
    ensure_audio_channels "$MUSIC_IN" "2" "music input"
    mkdir -p "$OUT_DIR/music"

    MUSIC_EQ="$OUT_DIR/music/music_eq.wav"
    MUSIC_COMP="$OUT_DIR/music/music_eq_compressor.wav"
    MUSIC_LIMITER="$OUT_DIR/music/music_eq_compressor_limiter.wav"

    echo "[branch] music calibration chain"
    run_stage "accomp_proq3" "$MUSIC_IN" "$MUSIC_EQ"
    run_stage "accomp_c6_sc" "$MUSIC_EQ" "$MUSIC_COMP"
    run_stage "accomp_l2_stereo" "$MUSIC_COMP" "$MUSIC_LIMITER"
fi

if [[ -n "$BUS_IN" ]]; then
    if [[ ! -f "$BUS_IN" ]]; then
        echo "Error: bus input not found: $BUS_IN" >&2
        exit 1
    fi
    ensure_audio_channels "$BUS_IN" "2" "bus input"
    mkdir -p "$OUT_DIR/bus"

    BUS_EQ="$OUT_DIR/bus/bus_eq.wav"
    BUS_LIMITER="$OUT_DIR/bus/bus_eq_limiter.wav"
    BUS_COMP="$OUT_DIR/bus/bus_eq_limiter_compressor.wav"

    echo "[branch] bus calibration chain"
    run_stage "master_proq3" "$BUS_IN" "$BUS_EQ"
    run_stage "master_l2_stereo" "$BUS_EQ" "$BUS_LIMITER"
    run_stage "gw_mixcentric_stereo" "$BUS_LIMITER" "$BUS_COMP"
fi

echo ""
echo "[done] Calibration stage render finished."
echo "       Output directory: $OUT_DIR"
