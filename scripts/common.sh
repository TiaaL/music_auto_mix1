#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
BUILD_DIR="$ROOT_DIR/build"

ensure_command() {
    local name="$1"
    local install_hint="${2:-}"

    if command -v "$name" >/dev/null 2>&1; then
        return
    fi

    echo "Error: required command not found: $name" >&2
    if [[ -n "$install_hint" ]]; then
        echo "Hint: $install_hint" >&2
    fi
    exit 1
}

ensure_binary() {
    local target="$1"
    local binary="$BUILD_DIR/$target"
    local make_target="build/$target"
    local faust_cmd="${FAUST:-faust}"
    local cxx_cmd="${CXX:-clang++}"

    if [[ -x "$binary" ]]; then
        return
    fi

    ensure_command "make"
    if ! command -v "$faust_cmd" >/dev/null 2>&1 && [[ ! -x "$faust_cmd" ]]; then
        echo "Error: required command not found: $faust_cmd" >&2
        echo "Hint: install Faust or export FAUST=/path/to/faust" >&2
        exit 1
    fi
    if ! command -v "$cxx_cmd" >/dev/null 2>&1 && [[ ! -x "$cxx_cmd" ]]; then
        echo "Error: required command not found: $cxx_cmd" >&2
        echo "Hint: install a C++ toolchain or export CXX=/path/to/compiler" >&2
        exit 1
    fi

    echo "[build] Missing $target, compiling it now..."
    mkdir -p "$BUILD_DIR"
    make "$make_target" -C "$ROOT_DIR"
}

make_temp_wav() {
    local prefix="${1:-faust_tmp}"
    local base
    local tmp
    base="$(mktemp "${TMPDIR:-/tmp}/${prefix}.XXXXXX")"
    tmp="${base}.wav"
    mv "$base" "$tmp"
    printf '%s\n' "$tmp"
}

project_python_bin() {
    if [[ -n "${PYTHON_BIN:-}" ]]; then
        printf '%s\n' "$PYTHON_BIN"
    elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
        printf '%s\n' "$ROOT_DIR/.venv/bin/python"
    elif [[ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]]; then
        printf '%s\n' "$ROOT_DIR/.venv/Scripts/python.exe"
    else
        printf '%s\n' "python3"
    fi
}

project_faust_bin() {
    if [[ -n "${FAUST:-}" ]]; then
        printf '%s\n' "$FAUST"
    elif [[ -x "$ROOT_DIR/.tools/faust-local/bin/faust" ]]; then
        printf '%s\n' "$ROOT_DIR/.tools/faust-local/bin/faust"
    elif [[ -x "$ROOT_DIR/.tools/msys64/usr/bin/faust.exe" ]]; then
        printf '%s\n' "$ROOT_DIR/.tools/msys64/usr/bin/faust.exe"
    else
        printf '%s\n' "faust"
    fi
}

audio_channels() {
    local path="$1"
    ffprobe -v error -select_streams a:0 -show_entries stream=channels -of default=noprint_wrappers=1:nokey=1 "$path"
}

audio_sample_rate() {
    local path="$1"
    ffprobe -v error -select_streams a:0 -show_entries stream=sample_rate -of default=noprint_wrappers=1:nokey=1 "$path"
}

ensure_parent_writable() {
    local path="$1"
    local parent
    parent="$(cd "$(dirname "$path")" && pwd)"

    if [[ ! -w "$parent" ]]; then
        echo "Error: output directory is not writable: $parent" >&2
        exit 1
    fi
}

ensure_audio_channels() {
    local path="$1"
    local expected="$2"
    local label="${3:-input}"
    local actual

    actual="$(audio_channels "$path")"
    if [[ "$actual" != "$expected" ]]; then
        echo "Error: $label must have $expected channel(s), got $actual: $path" >&2
        exit 1
    fi
}

ensure_matching_sample_rate() {
    local first="$1"
    local second="$2"
    local first_label="${3:-first input}"
    local second_label="${4:-second input}"
    local first_rate
    local second_rate

    first_rate="$(audio_sample_rate "$first")"
    second_rate="$(audio_sample_rate "$second")"
    if [[ "$first_rate" != "$second_rate" ]]; then
        echo "Error: sample-rate mismatch between $first_label ($first_rate Hz) and $second_label ($second_rate Hz)" >&2
        exit 1
    fi
}
