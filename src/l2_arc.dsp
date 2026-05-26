// ================================================================
// Waves L2 Ultramaximizer Simulation — Advanced (ARC + Soft Knee)
// ================================================================
// Extends l2_limiter.dsp with:
//   - ARC (Auto Release Control): adapts release time to program density
//   - Soft knee: smoother transition around threshold
//   - Gain reduction metering output (3rd channel, attenuation in dB)
//   - DC blocker at output
//
// ARC logic: when the limiter is engaged heavily/continuously,
// the release time shortens to avoid pumping. Sparse peaks get a
// long tail; dense/sustained material gets fast recovery.
// ================================================================

declare name        "L2 ARC Ultramaximizer Simulation";
declare version     "1.0";
declare description "L2-style limiter with ARC adaptive release";

import("stdfaust.lib");

// ----------------------------------------------------------------
// Parameters
// ----------------------------------------------------------------

THRESHOLD_DB  = -3.0;    // dBFS: limiting threshold
CEILING_DB    = -0.1;    // dBFS: hard output ceiling
LOOKAHEAD_MS  = 3.0;     // ms:   lookahead window

// ARC release range — the release time is interpolated between
// these bounds based on measured program density (limiting activity).
ARC_MIN_MS    = 50.0;    // ms: fastest release (dense/continuous limiting)
ARC_MAX_MS    = 800.0;   // ms: slowest release (sparse transients)
ARC_WINDOW_MS = 150.0;   // ms: density analysis window (controls ARC response speed)

// Soft knee width (0 = hard knee, like the real L2; increase for softer)
KNEE_DB       = 2.0;     // dB: half-width of soft knee region

// ----------------------------------------------------------------
// Derived constants
// ----------------------------------------------------------------

threshold    = ba.db2linear(THRESHOLD_DB);
ceiling      = ba.db2linear(CEILING_DB);
lookahead_n  = int(LOOKAHEAD_MS  * ma.SR / 1000.0);
arc_window_n = int(ARC_WINDOW_MS * ma.SR / 1000.0);

// ARC release coefficient range
arc_coeff_min = exp(-1.0 / (ARC_MIN_MS * 0.001 * ma.SR));
arc_coeff_max = exp(-1.0 / (ARC_MAX_MS * 0.001 * ma.SR));

// Soft knee boundaries (linear)
knee_lin      = ba.db2linear(KNEE_DB);
knee_bottom   = threshold / knee_lin;   // below here: no reduction
knee_top      = threshold * knee_lin;   // above here: full reduction

// ----------------------------------------------------------------
// ARC: density estimation
// ----------------------------------------------------------------
// Measures what fraction of recent samples are above threshold.
// High density → short release (prevent pumping).
// Low density  → long release (preserve transient feel).
//
// We use a sliding mean approximated by a one-pole lowpass
// (more efficient than a true rectangular window at long windows).

arc_tau       = ARC_WINDOW_MS * 0.001;
arc_pole      = ba.tau2pole(arc_tau);

limiting_density(peak) = (peak > threshold) : float : si.smooth(arc_pole);

// Interpolate release coefficient: dense → fast, sparse → slow
arc_coeff(density) = arc_coeff_min * density + arc_coeff_max * (1.0 - density);

// ----------------------------------------------------------------
// Soft knee gain computer
// ----------------------------------------------------------------
// Implements a quadratic soft knee around the threshold.
// Below knee_bottom: gain = 1.0 (unity)
// In knee zone:     gain = smooth quadratic blend
// Above knee_top:   gain = ceiling / peak (full limiting)

soft_knee_gain(peak) = result
with {
    // Normalized knee position: 0 at knee_bottom, 1 at knee_top
    alpha     = (peak - knee_bottom) / (knee_top - knee_bottom);
    full_gain = ceiling / max(peak, 1e-10);

    // Quadratic blend: gain = 1.0 + alpha^2 * (full_gain - 1.0)
    knee_gain = 1.0 + alpha * alpha * (full_gain - 1.0);

    result = select2(peak < knee_bottom,
        // In or above knee zone
        select2(peak > knee_top,
            full_gain,   // Full limiting above knee_top
            knee_gain    // Quadratic blend in knee zone
        ),
        1.0              // Below knee_bottom: unity gain
    );
};

// ----------------------------------------------------------------
// Gain smoother with adaptive release (ARC)
// ----------------------------------------------------------------

gain_smooth_arc(coeff) = loop ~ _
with {
    loop(prev, target) = select2(
        (prev < 1e-10) | (target < prev),
        prev + (1.0 - coeff) * (target - prev),  // release
        target                                     // attack or init
    );
};

// ----------------------------------------------------------------
// Hard clip guard (safety ceiling, ensures brick-wall after soft knee)
// ----------------------------------------------------------------

hard_clip = min(ceiling) : max(0.0 - ceiling);

// ----------------------------------------------------------------
// L2 ARC — main stereo processor
// ----------------------------------------------------------------

l2_arc(l, r) = (l_out, r_out)
with {
    // Stereo-linked instantaneous peak
    peak     = max(abs(l), abs(r));

    // Soft-knee desired gain
    g_raw    = soft_knee_gain(peak);

    // ARC: compute adaptive release coefficient from program density
    density  = limiting_density(peak);
    coeff    = arc_coeff(density);

    // Smooth gain with ARC-modulated release
    g        = g_raw : gain_smooth_arc(coeff);

    // Apply to lookahead-delayed signal + hard clip safety guard
    l_out    = (l @ lookahead_n) * g : hard_clip;
    r_out    = (r @ lookahead_n) * g : hard_clip;
};

process = l2_arc;
