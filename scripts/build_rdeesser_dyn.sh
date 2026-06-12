#!/usr/bin/env bash
# ================================================================
# build_rdeesser_dyn.sh — Compile a song-specific rdeesser_dyn binary
# ================================================================
# Reads a sibilance profile JSON (produced by plan_mix_template.py
# build_vocal_sibilance_profile, written as a sub-object of the
# resolved mix plan or a standalone vocal_profile.json) and substitutes
# ESS_FREQ / THRESH_DB / RANGE_DB into src/rdeesser.dsp.tmpl, then
# compiles to build/rdeesser_dyn.
#
# Usage:
#   ./scripts/build_rdeesser_dyn.sh <profile.json> [output_binary]
#
# profile.json must contain either at the top level, or under the key
# "vocal_sibilance_profile", the fields:
#   ess_freq_hz, thresh_db, range_db
#
# Output binary defaults to build/rdeesser_dyn.
# ================================================================

set -euo pipefail

PROFILE_JSON="${1:-}"
OUTPUT_BIN="${2:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

if [[ -z "$PROFILE_JSON" ]]; then
    echo "Usage: $0 <profile.json> [output_binary]" >&2
    exit 1
fi

if [[ ! -f "$PROFILE_JSON" ]]; then
    echo "Error: profile JSON not found: $PROFILE_JSON" >&2
    exit 1
fi

SRC_TMPL="$ROOT_DIR/src/rdeesser.dsp.tmpl"
if [[ ! -f "$SRC_TMPL" ]]; then
    echo "Error: template not found: $SRC_TMPL" >&2
    exit 1
fi

OUTPUT_BIN="${OUTPUT_BIN:-$BUILD_DIR/rdeesser_dyn}"
mkdir -p "$BUILD_DIR"

PYTHON_BIN="$(project_python_bin)"
FAUST_BIN="$(project_faust_bin)"
CXX_BIN="${CXX:-clang++}"

ensure_command "$PYTHON_BIN" "Install Python 3 or set PYTHON_BIN"

read -r ESS_FREQ THRESH_DB RANGE_DB <<EOF
$("$PYTHON_BIN" - "$PROFILE_JSON" <<'PY'
import json, sys
path = sys.argv[1]
with open(path, "r", encoding="utf-8-sig") as fh:
    data = json.load(fh)
profile = data.get("vocal_sibilance_profile", data)
ess = float(profile["ess_freq_hz"])
thresh = float(profile["thresh_db"])
rng = float(profile["range_db"])
print(f"{ess:.4f} {thresh:.4f} {rng:.4f}")
PY
)
EOF

if [[ -z "${ESS_FREQ:-}" || -z "${THRESH_DB:-}" || -z "${RANGE_DB:-}" ]]; then
    echo "Error: failed to extract sibilance profile fields from $PROFILE_JSON" >&2
    exit 1
fi

GEN_DSP="$BUILD_DIR/rdeesser_dyn.dsp"
GEN_CPP="$BUILD_DIR/rdeesser_dyn.cpp"

sed \
    -e "s|__ESS_FREQ__|${ESS_FREQ}|g" \
    -e "s|__THRESH_DB__|${THRESH_DB}|g" \
    -e "s|__RANGE_DB__|${RANGE_DB}|g" \
    "$SRC_TMPL" > "$GEN_DSP"

ARCHDIR="$("$FAUST_BIN" --archdir)"
ARCH_SF="$ARCHDIR/sndfile.cpp"

INCLUDES="${INCLUDES:--I/opt/homebrew/include -I$ARCHDIR}"
LDFLAGS="${LDFLAGS:--L/opt/homebrew/lib -lsndfile}"
CXXFLAGS="${CXXFLAGS:--O3 -ffast-math -DFILE_MODE=2}"

echo "[rdeesser_dyn] ESS_FREQ=${ESS_FREQ} THRESH_DB=${THRESH_DB} RANGE_DB=${RANGE_DB}"
echo "[faust] $GEN_DSP -> $GEN_CPP"
"$FAUST_BIN" -lang cpp -a "$ARCH_SF" "$GEN_DSP" -o "$GEN_CPP"

echo "[cxx]   $GEN_CPP -> $OUTPUT_BIN"
# shellcheck disable=SC2086
"$CXX_BIN" $CXXFLAGS $INCLUDES "$GEN_CPP" $LDFLAGS -o "$OUTPUT_BIN"

echo "[ok]    $OUTPUT_BIN"
