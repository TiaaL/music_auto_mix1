// ================================================================
// Waves L2 Ultramaximizer Simulation — Core Version
// ================================================================
// Simulates the key algorithmic behaviors of the L2:
//   1. Lookahead brickwall limiting (instantaneous attack via delay)
//   2. Stereo-linked gain reduction (prevents image shift)
//   3. Exponential release with instant attack
//   4. Hard brick-wall output ceiling (ISP-safe via conservative ceiling)
//
// Usage: faust2sndfile -o l2 l2_limiter.dsp
//        ./l2 input.wav output.wav
// ================================================================

declare name        "L2 Ultramaximizer Simulation";
declare version     "1.0";
declare description "Waves L2-style stereo lookahead brickwall limiter";

import("stdfaust.lib");

// ----------------------------------------------------------------
// Parameters — adjust here, no UI needed
// ----------------------------------------------------------------

THRESHOLD_DB = -3.0;    // dBFS: limiting threshold (where gain reduction begins)
CEILING_DB   = -0.1;    // dBFS: hard output ceiling (conservative for ISP safety)
RELEASE_MS   = 300.0;   // ms:   release time (L2 default ~200-500ms)
LOOKAHEAD_MS = 3.0;     // ms:   lookahead window (L2 hardware ≈ 3ms)

// ----------------------------------------------------------------
// Derived constants (computed at compile time from SR)
// ----------------------------------------------------------------

threshold    = ba.db2linear(THRESHOLD_DB);   // linear threshold (~0.708 at -3dB)
ceiling      = ba.db2linear(CEILING_DB);     // linear ceiling  (~0.989 at -0.1dB)
lookahead_n  = int(LOOKAHEAD_MS * ma.SR / 1000.0);
rel_coeff    = exp(-1.0 / (RELEASE_MS * 0.001 * ma.SR));

// ----------------------------------------------------------------
// Gain computer
// ----------------------------------------------------------------
// Below threshold → unity gain (no reduction)
// Above threshold → reduce proportionally so output = ceiling
//
// gain = ceiling / peak  when peak > threshold
// gain = 1.0             when peak <= threshold
//
// Note: this is equivalent to a hard-knee brickwall at ceiling.
// The threshold sets the point where limiting engages.

desired_gain(peak) = select2(peak > threshold,
    1.0,
    ceiling / peak
);

// ----------------------------------------------------------------
// Gain smoother
// ----------------------------------------------------------------
// - Attack: instantaneous (the lookahead delay provides the time window)
// - Release: exponential decay toward unity (rel_coeff controls speed)
//
// Initialization guard: if prev ≈ 0 (first sample), snap to target
// to avoid a fade-in artifact at stream start.

gain_smooth(coeff) = loop ~ _
with {
    loop(prev, target) = select2(
        (prev < 1e-10) | (target < prev),
        // Release branch: smoothly return toward unity gain
        prev + (1.0 - coeff) * (target - prev),
        // Attack branch (or init): snap to required gain immediately
        target
    );
};

// ----------------------------------------------------------------
// Stereo-linked peak detection
// ----------------------------------------------------------------
// Both channels share the same gain value.
// This prevents stereo image rotation when one channel peaks louder.

stereo_peak(l, r) = max(abs(l), abs(r));

// ----------------------------------------------------------------
// L2 Limiter — main stereo processor
// ----------------------------------------------------------------

l2(l, r) = (l_out, r_out)
with {
    // 1. Detect instantaneous peak (undelayed signal = "future" relative to output)
    peak  = stereo_peak(l, r);

    // 2. Compute required gain reduction
    g_raw = desired_gain(peak);

    // 3. Apply release smoothing (attack is free via lookahead)
    g     = g_raw : gain_smooth(rel_coeff);

    // 4. Apply gain to the delayed (lookahead-compensated) signal
    l_out = (l @ lookahead_n) * g;
    r_out = (r @ lookahead_n) * g;
};

process = l2;
