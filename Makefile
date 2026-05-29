# ================================================================
# Waves L2 Ultramaximizer Simulation — Build System
# ================================================================
# Targets:
#   make          — build both sndfile CLI binaries (default)
#   make test     — run limiter correctness tests with sox
#   make smoke    — run lightweight workflow smoke tests
#   make svg      — generate signal flow diagrams
#   make clean    — remove build artifacts
#
# Requirements: brew install faust sox ffmpeg libsndfile
# ================================================================

FAUST       ?= faust
ARCHDIR     ?= $(shell $(FAUST) --archdir)
ARCH_SF     := $(ARCHDIR)/sndfile.cpp

CXX         ?= clang++
CXXFLAGS    ?= -O3 -ffast-math -DFILE_MODE=2
INCLUDES    ?= -I/opt/homebrew/include -I$(ARCHDIR)
LDFLAGS     ?= -L/opt/homebrew/lib -lsndfile

SRC_DIR     := src
BUILD_DIR   := build
DSPS        := l2_limiter l2_arc mixer req6 c1_comp rdeesser vocal_group_fx accomp_proq3 accomp_c6_sc accomp_l2_stereo master_proq3 master_softclipper master_l2_stereo c1_gate rbass_mono f6_rta_mono sibilance_mono l1_limiter_mono vocal_rider_mono oneknob_brighter_mono gw_mixcentric_stereo template_a_vocal_proq3 template_c_vocal_proq3 template_music_proq3_ab template_music_proq3_c template_bus_proq3_ab template_bus_proq3_c

.PHONY: all test smoke svg clean

# ----------------------------------------------------------------
# Default: compile both DSPs to sndfile CLI binaries
# ----------------------------------------------------------------

all: $(addprefix $(BUILD_DIR)/, $(DSPS))

$(BUILD_DIR)/%: $(SRC_DIR)/%.dsp | $(BUILD_DIR)
	@echo "[faust] $< → $@"
	$(FAUST) -lang cpp -a $(ARCH_SF) $< -o $(BUILD_DIR)/$*.cpp
	$(CXX) $(CXXFLAGS) $(INCLUDES) $(BUILD_DIR)/$*.cpp $(LDFLAGS) -o $@
	@echo "[ok]    $@  (usage: $@ input.wav output.wav)"

# ----------------------------------------------------------------
# Test: verify limiting and passthrough behavior
# ----------------------------------------------------------------

test: $(BUILD_DIR)/l2_limiter $(BUILD_DIR)/l2_arc
	@echo "=== Generating test signals ==="
	sox -n -r 44100 -b 16 -c 2 /tmp/l2_test_above.wav synth 3 sine 1000 gain 3
	sox -n -r 44100 -b 16 -c 2 /tmp/l2_test_below.wav synth 3 sine 1000 gain -6
	@echo ""
	@echo "=== l2_limiter: 0dBFS input → expect output ≈ -0.1dBFS (0.9886) ==="
	$(BUILD_DIR)/l2_limiter /tmp/l2_test_above.wav /tmp/l2_out_basic.wav
	@echo "  Input peak:  $$(sox /tmp/l2_test_above.wav -n stat 2>&1 | grep 'Maximum amplitude' | awk '{print $$3}')"
	@echo "  Output peak: $$(sox /tmp/l2_out_basic.wav  -n stat 2>&1 | grep 'Maximum amplitude' | awk '{print $$3}')"
	@echo ""
	@echo "=== l2_arc: 0dBFS input with soft knee + ARC ==="
	$(BUILD_DIR)/l2_arc /tmp/l2_test_above.wav /tmp/l2_out_arc.wav
	@echo "  Output peak: $$(sox /tmp/l2_out_arc.wav -n stat 2>&1 | grep 'Maximum amplitude' | awk '{print $$3}')"
	@echo ""
	@echo "=== Passthrough: -6dBFS input (below -3dBFS threshold) ==="
	$(BUILD_DIR)/l2_limiter /tmp/l2_test_below.wav /tmp/l2_out_pass.wav
	@echo "  Input peak:  $$(sox /tmp/l2_test_below.wav -n stat 2>&1 | grep 'Maximum amplitude' | awk '{print $$3}')"
	@echo "  Output peak: $$(sox /tmp/l2_out_pass.wav   -n stat 2>&1 | grep 'Maximum amplitude' | awk '{print $$3}') (expect unchanged)"

smoke:
	./scripts/smoke_test.sh

# ----------------------------------------------------------------
# SVG signal flow diagrams
# ----------------------------------------------------------------

svg: | $(BUILD_DIR)
	@for dsp in $(SRC_DIR)/*.dsp; do \
		base=$$(basename $$dsp .dsp); \
		echo "[svg] $$dsp → $(BUILD_DIR)/$$base-svg/"; \
		$(FAUST) -svg $$dsp -o $(BUILD_DIR)/$$base-svg/ 2>&1; \
	done

# ----------------------------------------------------------------

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

clean:
	rm -rf $(BUILD_DIR)
