#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"
PYTHON_BIN="$(project_python_bin)"

ensure_command "sox" "Install SoX to generate smoke-test input files"
ensure_command "$PYTHON_BIN" "Create the project .venv or set PYTHON_BIN"
ensure_command "ffmpeg" "Install FFmpeg to run workflow smoke tests"
ensure_command "ffprobe" "Install FFmpeg so ffprobe is available"

for target in rdeesser req6 c1_comp vocal_group_fx l2_arc master_proq3 master_softclipper master_l2_stereo; do
    ensure_binary "$target"
done

tmpdir="$(mktemp -d "${TMPDIR:-/tmp}/faust_smoke.XXXXXX")"
trap 'rm -rf "$tmpdir"' EXIT

vocal_in="$tmpdir/vocal.wav"
accomp_in="$tmpdir/accomp.wav"
stereo_vocal_in="$tmpdir/vocal_stereo.wav"
vocal_out="$tmpdir/vocal_out.wav"
group_out="$tmpdir/group_out.wav"
mix_out="$tmpdir/final_mix.wav"

echo "[smoke] Generating synthetic test inputs"
sox -n -r 44100 -b 16 -c 1 "$vocal_in" synth 1.5 sine 440 fade 0.02 1.4 0.08
sox -n -r 44100 -b 16 -c 2 "$accomp_in" synth 1.5 sine 220 sine 330 gain -9
sox -n -r 44100 -b 16 -c 2 "$stereo_vocal_in" synth 0.5 sine 440 sine 550 gain -12

echo "[smoke] Verifying input validation"
if "$SCRIPT_DIR/vocal_chain.sh" "$stereo_vocal_in" "$tmpdir/should_fail.wav" >/dev/null 2>&1; then
    echo "Smoke test failed: vocal_chain.sh accepted stereo vocal input" >&2
    exit 1
fi

echo "[smoke] Running vocal_chain.sh"
"$SCRIPT_DIR/vocal_chain.sh" "$vocal_in" "$vocal_out" >/dev/null

echo "[smoke] Running vocal_stereo_group.sh"
"$SCRIPT_DIR/vocal_stereo_group.sh" "$vocal_in" "$group_out" >/dev/null

echo "[smoke] Running full_fx_mix.sh"
"$SCRIPT_DIR/full_fx_mix.sh" "$vocal_in" "$accomp_in" "$mix_out" >/dev/null

for output in "$vocal_out" "$group_out" "$mix_out"; do
    if [[ ! -s "$output" ]]; then
        echo "Smoke test failed: expected output file missing or empty: $output" >&2
        exit 1
    fi
done

echo "[smoke] OK"
