#!/usr/bin/env python3
"""Build the runtime-parameter vocal_group_fx dynamic library.

1.1 空间链的动态库版本只把 0.1 rack 里的白名单参数变成 runtime 参数；
mono in、stereo out、dry/early/reverb/shimmer/delay 并联发送路径都保持不变。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SRC_DSP = ROOT / "src" / "vocal_group_fx.dsp"
BUILD_DIR = ROOT / "build" / "runtime"
RUNTIME_DSP = BUILD_DIR / "vocal_group_fx_runtime.dsp"
FAUST_CPP = BUILD_DIR / "vocal_group_fx_runtime_faust.cpp"
WRAPPER_CPP = BUILD_DIR / "vocal_group_fx_runtime_wrapper.cpp"
DEFAULT_LIB = ROOT / "build" / "libvocal_group_fx_runtime.dylib"


DYNAMIC_PARAMS = {
    "RVERB_SEND_PRE_DB": ("rverb_send_pre_db", -12.5, -60.0, -12.5, 0.001),
    "RVERB_PREDELAY_MS": ("rverb_predelay_ms", 12.0, 0.0, 12.0, 0.001),
    "RVERB_TIME_S": ("rverb_time_s", 1.75, 0.30, 1.75, 0.001),
    "RVERB_EARLY_REF_DB": ("rverb_early_ref_db", -2.0, -24.0, -2.0, 0.001),
    "RVERB_DAMP": ("rverb_damp", 0.35, 0.0, 1.0, 0.001),
    "RVERB_EQ_HI_GAIN_DB": ("rverb_eq_hi_gain_db", -4.0, -24.0, -4.0, 0.001),
    "SUPERTAP_SEND_PRE_DB": ("supertap_send_pre_db", -27.0, -80.0, -27.0, 0.001),
    "SUPERTAP_GAIN_DB": ("supertap_gain_db", -18.5, -80.0, -18.5, 0.001),
    "SUPERTAP_FEEDBACK": ("supertap_feedback", 0.10, 0.0, 0.10, 0.0001),
    "SUPERTAP_WIDTH": ("supertap_width", 0.45, 0.0, 0.45, 0.0001),
    "SUPERTAP_COLOR_HZ": ("supertap_color_hz", 2400.0, 400.0, 2400.0, 0.1),
    "SHIMMER_SEND_PRE_DB": ("shimmer_send_pre_db", -18.0, -80.0, -18.0, 0.001),
    "SHIMMER_GAIN_DB": ("shimmer_gain_db", -18.0, -80.0, -18.0, 0.001),
}


WRAPPER_SOURCE = r'''
#include <algorithm>
#include <cmath>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

#include "faust/dsp/dsp.h"
#include "faust/gui/UI.h"
#include "faust/gui/meta.h"

#include "vocal_group_fx_runtime_faust.cpp"

struct ParamUI final : public UI {
    std::unordered_map<std::string, FAUSTFLOAT*> zones;

    void openTabBox(const char*) override {}
    void openHorizontalBox(const char*) override {}
    void openVerticalBox(const char*) override {}
    void closeBox() override {}

    void addButton(const char* label, FAUSTFLOAT* zone) override { zones[label] = zone; }
    void addCheckButton(const char* label, FAUSTFLOAT* zone) override { zones[label] = zone; }
    void addVerticalSlider(const char* label, FAUSTFLOAT* zone, FAUSTFLOAT, FAUSTFLOAT, FAUSTFLOAT, FAUSTFLOAT) override { zones[label] = zone; }
    void addHorizontalSlider(const char* label, FAUSTFLOAT* zone, FAUSTFLOAT, FAUSTFLOAT, FAUSTFLOAT, FAUSTFLOAT) override { zones[label] = zone; }
    void addNumEntry(const char* label, FAUSTFLOAT* zone, FAUSTFLOAT, FAUSTFLOAT, FAUSTFLOAT, FAUSTFLOAT) override { zones[label] = zone; }
    void addHorizontalBargraph(const char*, FAUSTFLOAT*, FAUSTFLOAT, FAUSTFLOAT) override {}
    void addVerticalBargraph(const char*, FAUSTFLOAT*, FAUSTFLOAT, FAUSTFLOAT) override {}
    void addSoundfile(const char*, const char*, Soundfile**) override {}

    bool set(const char* label, float value) {
        auto it = zones.find(label);
        if (it == zones.end() || it->second == nullptr) {
            return false;
        }
        *(it->second) = value;
        return true;
    }
};

struct VocalGroupFxParams {
    float rverb_send_pre_db;
    float rverb_time_s;
    float rverb_predelay_ms;
    float rverb_early_ref_db;
    float rverb_damp;
    float rverb_eq_hi_gain_db;
    float supertap_send_pre_db;
    float supertap_gain_db;
    float supertap_feedback;
    float supertap_width;
    float supertap_color_hz;
    float shimmer_send_pre_db;
    float shimmer_gain_db;
};

struct VocalGroupFxHandle {
    VocalGroupFxRuntimeDsp dsp;
    ParamUI ui;

    explicit VocalGroupFxHandle(int sample_rate) {
        dsp.init(sample_rate);
        dsp.buildUserInterface(&ui);
    }
};

static float clampf(float value, float lo, float hi) {
    return std::max(lo, std::min(value, hi));
}

static void set_param(VocalGroupFxHandle* handle, const char* label, float value, float lo, float hi) {
    handle->ui.set(label, clampf(value, lo, hi));
}

extern "C" {

const char* vocal_group_fx_runtime_version() {
    return "vocal_group_fx_runtime.v1";
}

void* vocal_group_fx_create(int sample_rate) {
    if (sample_rate <= 0) {
        return nullptr;
    }
    try {
        return new VocalGroupFxHandle(sample_rate);
    } catch (...) {
        return nullptr;
    }
}

void vocal_group_fx_destroy(void* ptr) {
    delete static_cast<VocalGroupFxHandle*>(ptr);
}

int vocal_group_fx_set_params(void* ptr, const VocalGroupFxParams* params) {
    if (ptr == nullptr || params == nullptr) {
        return 0;
    }
    auto* handle = static_cast<VocalGroupFxHandle*>(ptr);
    set_param(handle, "rverb_send_pre_db", params->rverb_send_pre_db, -60.0f, -12.5f);
    set_param(handle, "rverb_time_s", params->rverb_time_s, 0.30f, 1.75f);
    set_param(handle, "rverb_predelay_ms", params->rverb_predelay_ms, 0.0f, 12.0f);
    set_param(handle, "rverb_early_ref_db", params->rverb_early_ref_db, -24.0f, -2.0f);
    set_param(handle, "rverb_damp", params->rverb_damp, 0.0f, 1.0f);
    set_param(handle, "rverb_eq_hi_gain_db", params->rverb_eq_hi_gain_db, -24.0f, -4.0f);
    set_param(handle, "supertap_send_pre_db", params->supertap_send_pre_db, -80.0f, -27.0f);
    set_param(handle, "supertap_gain_db", params->supertap_gain_db, -80.0f, -18.5f);
    set_param(handle, "supertap_feedback", params->supertap_feedback, 0.0f, 0.10f);
    set_param(handle, "supertap_width", params->supertap_width, 0.0f, 0.45f);
    set_param(handle, "supertap_color_hz", params->supertap_color_hz, 400.0f, 2400.0f);
    set_param(handle, "shimmer_send_pre_db", params->shimmer_send_pre_db, -80.0f, -18.0f);
    set_param(handle, "shimmer_gain_db", params->shimmer_gain_db, -80.0f, -18.0f);
    return 1;
}

int vocal_group_fx_process(void* ptr, const float* input, float* out_left, float* out_right, int frames) {
    if (ptr == nullptr || input == nullptr || out_left == nullptr || out_right == nullptr || frames < 0) {
        return 0;
    }
    auto* handle = static_cast<VocalGroupFxHandle*>(ptr);
    FAUSTFLOAT* inputs[1] = { const_cast<FAUSTFLOAT*>(input) };
    FAUSTFLOAT* outputs[2] = { out_left, out_right };
    handle->dsp.compute(frames, inputs, outputs);
    return 1;
}

}
'''


def command_path(name: str) -> str:
    env_value = os.environ.get(name.upper())
    if env_value:
        return env_value
    found = shutil.which(name)
    if found:
        return found
    return name


def replace_runtime_params(source: str) -> str:
    out = source
    for const_name, (label, default, lo, hi, step) in DYNAMIC_PARAMS.items():
        replacement = (
            f'{const_name:<20} = hslider("{label}", '
            f"{default:.8g}, {lo:.8g}, {hi:.8g}, {step:.8g});"
        )
        pattern = rf"^{re.escape(const_name)}\s*=.*?;"
        out, count = re.subn(pattern, replacement, out, count=1, flags=re.MULTILINE)
        if count != 1:
            raise RuntimeError(f"Could not runtime-parameterize {const_name}")
    out, count = re.subn(
        r"^OUTPUT_SIDE_TRIM_DB\s*=.*?;",
        "OUTPUT_SIDE_TRIM_DB = 0.0;    // locked: runtime path does not use post side trim",
        out,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("Could not lock OUTPUT_SIDE_TRIM_DB")
    return out


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            "command failed:\n"
            + " ".join(cmd)
            + "\nSTDOUT:\n"
            + proc.stdout
            + "\nSTDERR:\n"
            + proc.stderr
        )


def build(output: Path, force: bool = False) -> Path:
    if output.exists() and not force:
        return output
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    RUNTIME_DSP.write_text(replace_runtime_params(SRC_DSP.read_text(encoding="utf-8")), encoding="utf-8")
    WRAPPER_CPP.write_text(WRAPPER_SOURCE, encoding="utf-8")

    faust = command_path("faust")
    cxx = os.environ.get("CXX") or "clang++"
    archdir_proc = subprocess.run([faust, "--archdir"], text=True, capture_output=True, check=False)
    if archdir_proc.returncode != 0:
        raise RuntimeError(f"faust --archdir failed: {archdir_proc.stderr}")
    archdir = Path(archdir_proc.stdout.strip())
    faust_include = archdir.parent / "include"

    run([faust, "-lang", "cpp", "-cn", "VocalGroupFxRuntimeDsp", str(RUNTIME_DSP), "-o", str(FAUST_CPP)])
    includes = os.environ.get("INCLUDES", f"-I/opt/homebrew/include -I{faust_include} -I{BUILD_DIR}")
    cxxflags = os.environ.get("CXXFLAGS", "-O3 -ffast-math -fPIC -std=c++17")
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        cxx,
        *cxxflags.split(),
        *includes.split(),
        "-dynamiclib",
        str(WRAPPER_CPP),
        "-o",
        str(output),
    ])
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build libvocal_group_fx_runtime.dylib.")
    parser.add_argument("--output", type=Path, default=DEFAULT_LIB)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(build(args.output, force=args.force))


if __name__ == "__main__":
    main()
