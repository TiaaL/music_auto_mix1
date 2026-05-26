#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=./common.sh
source "$SCRIPT_DIR/common.sh"

missing=0

check_cmd() {
    local name="$1"
    local scope="$2"
    if command -v "$name" >/dev/null 2>&1; then
        printf '[ok]      %-10s %s\n' "$name" "$scope"
    else
        printf '[missing] %-10s %s\n' "$name" "$scope"
        missing=1
    fi
}

echo "Faust repo environment check"
echo "Root: $ROOT_DIR"
echo ""
echo "Build requirements:"
check_cmd "faust" "required to compile DSP sources"
check_cmd "clang++" "required to compile generated C++"
check_cmd "make" "required for build orchestration"

echo ""
echo "Workflow requirements:"
check_cmd "sox" "required for stats, tests, and sample generation"
check_cmd "ffmpeg" "required for mix/render workflows"
check_cmd "ffprobe" "required for duration analysis in auto_volume_mix.py"
check_cmd "python3" "required for auto_volume_mix.py"

echo ""
echo "Library / path note:"
echo "  The Makefile currently expects libsndfile headers/libs via local toolchain paths."
echo "  On Apple Silicon macOS, Homebrew defaults usually live under /opt/homebrew."

if [[ "$missing" -ne 0 ]]; then
    echo ""
    echo "One or more required commands are missing."
    echo "Suggested starting point on macOS/Homebrew:"
    echo "  brew install faust sox ffmpeg libsndfile"
    exit 1
fi

echo ""
echo "Environment looks ready."
