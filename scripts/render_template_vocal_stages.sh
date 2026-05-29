#!/usr/bin/env bash
# ================================================================
# render_template_vocal_stages.sh - render selected template vocal stages
# ================================================================
# Usage:
#   ./scripts/render_template_vocal_stages.sh template_b vocal.wav out_dir
#
# Saves per-insert stage WAVs plus final_insert.wav, which is the processed
# mono vocal before stereo group FX/reverb. This is for feature auditing and
# effect tuning; final production renders still use render_template_mix.sh.
# ================================================================

set -euo pipefail

TEMPLATE_ID="${1:-}"
VOCAL_IN="${2:-}"
OUT_DIR="${3:-}"

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

if [[ -z "$TEMPLATE_ID" || -z "$VOCAL_IN" || -z "$OUT_DIR" ]]; then
    echo "Usage: $0 <template_a|template_b|template_c> <vocal.wav> <out_dir>" >&2
    exit 1
fi

case "$TEMPLATE_ID" in
    template_a|template_b|template_c) ;;
    *)
        echo "Error: unsupported template id: $TEMPLATE_ID" >&2
        exit 1
        ;;
esac

if [[ ! -f "$VOCAL_IN" ]]; then
    echo "Error: vocal input not found: $VOCAL_IN" >&2
    exit 1
fi

ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"
ensure_audio_channels "$VOCAL_IN" "1" "vocal input"
mkdir -p "$OUT_DIR"

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

echo "[branch] vocal feature chain: $TEMPLATE_ID"
case "$TEMPLATE_ID" in
    template_a)
        run_stage "c1_gate" "$VOCAL_IN" "$OUT_DIR/01_c1_gate.wav"
        run_stage "template_a_vocal_proq3" "$OUT_DIR/01_c1_gate.wav" "$OUT_DIR/02_template_a_vocal_proq3.wav"
        run_stage "c1_comp" "$OUT_DIR/02_template_a_vocal_proq3.wav" "$OUT_DIR/03_c1_comp.wav"
        run_stage "sibilance_mono" "$OUT_DIR/03_c1_comp.wav" "$OUT_DIR/04_sibilance_mono.wav"
        cp "$OUT_DIR/04_sibilance_mono.wav" "$OUT_DIR/final_insert.wav"
        run_stage "vocal_group_fx" "$OUT_DIR/final_insert.wav" "$OUT_DIR/05_vocal_group_fx.wav"
        ;;
    template_b)
        run_stage "rbass_mono" "$VOCAL_IN" "$OUT_DIR/01_rbass_mono.wav"
        run_stage "f6_rta_mono" "$OUT_DIR/01_rbass_mono.wav" "$OUT_DIR/02_f6_rta_mono.wav"
        run_stage "c1_comp" "$OUT_DIR/02_f6_rta_mono.wav" "$OUT_DIR/03_c1_comp.wav"
        run_stage "sibilance_mono" "$OUT_DIR/03_c1_comp.wav" "$OUT_DIR/04_sibilance_mono.wav"
        run_stage "l1_limiter_mono" "$OUT_DIR/04_sibilance_mono.wav" "$OUT_DIR/05_l1_limiter_mono.wav"
        cp "$OUT_DIR/05_l1_limiter_mono.wav" "$OUT_DIR/final_insert.wav"
        run_stage "vocal_group_fx" "$OUT_DIR/final_insert.wav" "$OUT_DIR/06_vocal_group_fx.wav"
        ;;
    template_c)
        run_stage "template_c_vocal_proq3" "$VOCAL_IN" "$OUT_DIR/01_template_c_vocal_proq3.wav"
        run_stage "vocal_rider_mono" "$OUT_DIR/01_template_c_vocal_proq3.wav" "$OUT_DIR/02_vocal_rider_mono.wav"
        run_stage "c1_comp" "$OUT_DIR/02_vocal_rider_mono.wav" "$OUT_DIR/03_c1_comp.wav"
        run_stage "oneknob_brighter_mono" "$OUT_DIR/03_c1_comp.wav" "$OUT_DIR/04_oneknob_brighter_mono.wav"
        cp "$OUT_DIR/04_oneknob_brighter_mono.wav" "$OUT_DIR/final_insert.wav"
        run_stage "vocal_group_fx" "$OUT_DIR/final_insert.wav" "$OUT_DIR/05_vocal_group_fx.wav"
        ;;
esac

echo ""
echo "[done] Vocal stage render finished."
echo "       Template: $TEMPLATE_ID"
echo "       Output directory: $OUT_DIR"
