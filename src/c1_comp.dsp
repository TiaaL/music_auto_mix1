// ================================================================
// C1 Compressor Simulation — Mono
// ================================================================
// Simulates the key behaviors of the Waves C1 Compressor:
//   1. RMS level detection (windowed average)
//   2. Soft-knee gain computer (quadratic transition at threshold)
//   3. Separate attack / release smoothing on gain reduction (dB domain)
//   4. Makeup gain
//
// Gain computer (soft knee, in dB):
//   below  (T - W/2):  no reduction
//   within (±W/2):     quadratic blend: (1/R-1)*(x-T+W/2)² / (2W)
//   above  (T + W/2):  full ratio:      (1/R-1)*(x-T)
//
// Input:  1 channel (mono)
// Output: 1 channel (mono)
// ================================================================

declare name        "C1 Compressor Simulation";
declare version     "1.0";
declare description "RMS soft-knee compressor with attack/release and makeup gain";

import("stdfaust.lib");

mix(a, b, t) = a * (1.0 - t) + b * t;

// ----------------------------------------------------------------
// Parameters — edit here
// ----------------------------------------------------------------

THRESHOLD_DB = -16.0;   // dBFS: compression threshold
RATIO        = 4.0;     // compression ratio (e.g. 4 = 4:1; use 20+ for limiting)
ATTACK_MS    = 6.0;     // ms: gain reduction attack time
RELEASE_MS   = 180.0;   // ms: gain reduction release time
KNEE_DB      = 8.0;     // dB: soft knee width (0 = hard knee)
MAKEUP_DB    = 3.0;     // dB: output makeup gain
RMS_WIN_MS   = 10.0;    // ms: RMS detection window (larger = slower, more musical)
PEAK_WIN_MS  = 1.0;     // ms: faster detector path for peak-sensitive behavior
DETECTOR_MIX = 1.0;     // 0 = peak only, 1 = RMS only, between = blend
SIDECHAIN_HP_ON = 0;    // 0 = full-band detector, 1 = high-passed sidechain
SIDECHAIN_HP_HZ = 120.0;// Hz: sidechain high-pass cutoff
PDR_AMOUNT   = 4.0;     // program-dependent release depth (C1-style "PDR" feel)

// ----------------------------------------------------------------
// RMS level detector
// ----------------------------------------------------------------
// Computes windowed RMS: square → one-pole smooth → sqrt
// The smoothing time constant equals the RMS window length.

rmsCoeff = exp(-1.0 / (RMS_WIN_MS * 0.001 * ma.SR));
peakCoeff = exp(-1.0 / (PEAK_WIN_MS * 0.001 * ma.SR));

rmsLevel = _ <: * : si.smooth(rmsCoeff) : sqrt;
peakLevel = abs : si.smooth(peakCoeff);
//         squaring  smoothing           RMS envelope

// ----------------------------------------------------------------
// Soft-knee gain computer (operates entirely in dB)
// ----------------------------------------------------------------
// Input:  level in linear amplitude
// Output: gain to apply, in dB (always ≤ 0)

gainComputer(thresh, ratio, knee, levelLin) = gainDB
with {
    levelDB  = ba.linear2db(max(levelLin, ma.MIN));
    excess   = levelDB - thresh;          // positive when above threshold
    W2       = knee * 0.5;
    inKnee   = (2.0 * abs(excess)) <= knee;
    above    = excess > W2;
    kneeGain = (1.0/ratio - 1.0) * pow(excess + W2, 2.0) / (2.0 * knee);
    fullGain = (1.0/ratio - 1.0) * excess;
    gainDB   = select2(above,
                   select2(inKnee, 0.0, kneeGain),
                   fullGain);
};

// ----------------------------------------------------------------
// Attack / release smoother (dB domain)
// ----------------------------------------------------------------
// Gain reduction is smoothed in the dB domain for perceptually
// uniform behavior.  Attack engages when gain decreases (more
// negative); release engages when gain returns toward 0 dB.

coeffA = exp(-1.0 / (ATTACK_MS  * 0.001 * ma.SR));
coeffR = exp(-1.0 / (RELEASE_MS * 0.001 * ma.SR));

smoothGainDB = loop ~ _
with {
    pdrScale(target) = 1.0 + PDR_AMOUNT * abs(target) / 24.0;
    coeffRelease(target) = exp(-1.0 / ((RELEASE_MS / pdrScale(target)) * 0.001 * ma.SR));
    loop(prev, target) = select2(target < prev,
        prev + (target - prev) * (1.0 - coeffRelease(target)),   // release: gain returning up
        prev + (target - prev) * (1.0 - coeffA)    // attack:  gain going down
    );
};

// ----------------------------------------------------------------
// C1 compressor
// ----------------------------------------------------------------

c1(x) = x * gainLin
with {
    detectorIn = select2(SIDECHAIN_HP_ON, x, x : fi.highpass(1, SIDECHAIN_HP_HZ));
    levelRMS   = detectorIn : rmsLevel;
    levelPeak  = detectorIn : peakLevel;
    level      = mix(levelPeak, levelRMS, DETECTOR_MIX);
    gainDB_i = gainComputer(THRESHOLD_DB, RATIO, KNEE_DB, level);
    gainDB_s = gainDB_i : smoothGainDB;
    gainLin  = gainDB_s : ba.db2linear;
};

// ----------------------------------------------------------------
// Main process
// ----------------------------------------------------------------

process = _ : c1 : *(ba.db2linear(MAKEUP_DB));
