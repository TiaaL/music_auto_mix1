#!/usr/bin/env bash
# Source this on macOS to use the project-local Faust build under .tools/faust-local.
#
#   source scripts/mac_faust_env.sh
#   make

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FAUST_ROOT="$ROOT_DIR/.tools/faust-local"

if [[ ! -x "$FAUST_ROOT/bin/faust" ]]; then
    echo "Local Faust not found at $FAUST_ROOT/bin/faust" >&2
    echo "Install from https://github.com/grame-cncm/faust/releases or build source into .tools/faust-local" >&2
    return 1 2>/dev/null || exit 1
fi

export PATH="/opt/homebrew/bin:${PATH:-}"
export FAUST="$FAUST_ROOT/bin/faust"
export ARCHDIR="${ARCHDIR:-$("$FAUST" --archdir)}"
export CXX="${CXX:-clang++}"
export CXXFLAGS="${CXXFLAGS:--O3 -ffast-math -DFILE_MODE=2 -std=c++17}"
export INCLUDES="${INCLUDES:--I/opt/homebrew/include -I$ARCHDIR -I$FAUST_ROOT/include}"
export LDFLAGS="${LDFLAGS:--L/opt/homebrew/lib -lsndfile}"

if [[ "${MAC_FAUST_ENV_QUIET:-0}" != "1" ]]; then
    echo "macOS Faust env loaded."
    echo "  faust: $FAUST"
    echo "  arch:  $ARCHDIR"
fi
