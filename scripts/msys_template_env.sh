#!/usr/bin/env bash
# Source this from Git Bash/MSYS2 to use the project-local native toolchain.

SCRIPT_PATH="${BASH_SOURCE[0]}"
case "$SCRIPT_PATH" in
    */*) SCRIPT_DIR="${SCRIPT_PATH%/*}" ;;
    *) SCRIPT_DIR="." ;;
esac
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MSYS_ROOT="$ROOT_DIR/.tools/msys64"

export PATH="$MSYS_ROOT/ucrt64/bin:$MSYS_ROOT/usr/bin:$PATH"
if [[ -x "$ROOT_DIR/.tools/faust/bin/faust.exe" && -z "${FAUST:-}" ]]; then
    export FAUST="$ROOT_DIR/.tools/faust/bin/faust.exe"
else
    export FAUST="${FAUST:-faust}"
fi
export CXX="${CXX:-g++}"
if command -v cygpath >/dev/null 2>&1; then
    export ARCHDIR="${ARCHDIR:-$(cygpath -u "$("$FAUST" --archdir 2>/dev/null || printf /usr/share/faust)")}"
else
    export ARCHDIR="${ARCHDIR:-$("$FAUST" --archdir 2>/dev/null || printf /usr/share/faust)}"
fi
FAUST_INCLUDEDIR="$ROOT_DIR/.tools/faust/include"
export INCLUDES="${INCLUDES:--I/ucrt64/include -I$ARCHDIR -I$FAUST_INCLUDEDIR}"
export LDFLAGS="${LDFLAGS:--L/ucrt64/lib -lsndfile}"

if [[ "${MSYS_TEMPLATE_ENV_QUIET:-0}" != "1" ]]; then
    echo "MSYS template env loaded."
    echo "  make:   $(command -v make || true)"
    echo "  cxx:    $(command -v "$CXX" || true)"
    echo "  sox:    $(command -v sox || true)"
    echo "  ffmpeg: $(command -v ffmpeg || true)"
    echo "  faust:  $(command -v faust || true)"
    echo "  FAUST:  $FAUST"
fi
