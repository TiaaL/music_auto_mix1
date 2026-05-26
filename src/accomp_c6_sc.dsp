// ================================================================
// Accompaniment Multiband Dynamics — Waves C6 SideChain Stereo style
// ================================================================
// Approximation of the posted C6 screenshot for accompaniment use.
// The goal is to preserve the requested band layout and general
// dynamic behavior rather than claim a bit-accurate clone.

declare name        "Accompaniment C6 SideChain Stereo Approx";
declare version     "1.0";
declare description "Stereo multiband dynamics approximating a Waves C6 setup";

import("stdfaust.lib");

// Visible crossover / band centers from the screenshot
LOW_XOVER_HZ   = 45.0;
MID_CENTER_HZ  = 2197.0;
HIGH_XOVER_HZ  = 5003.0;
AIR_FREQ_HZ    = 7389.0;

// Additional visible EQ nodes
SUB_FREQ_HZ    = 102.0;
SUB_Q          = 2.15;
SUB_GAIN_DB    = 1.7;

AIR_Q          = 14.25;
OUTPUT_TRIM_DB = -8.5;

db2lin(x) = pow(10.0, x / 20.0);
clamp01(x) = min(1.0, max(0.0, x));

peakEnv(ms, x) = abs(x) : si.smooth(ba.tau2pole(ms * 0.001));

compGain(env, thresholdDb, rangeDb, attackMs, releaseMs) = gain
with {
    envDb      = ba.linear2db(max(env, 1e-8));
    overDb     = max(0.0, envDb - thresholdDb);
    targetDb   = max(rangeDb, -overDb);
    attackPole = ba.tau2pole(attackMs * 0.001);
    releasePole= ba.tau2pole(releaseMs * 0.001);
    coeff      = select2(targetDb < 0.0, attackPole, releasePole);
    gain       = targetDb : si.smooth(coeff) : ba.db2linear;
};

dynamicBand(thresholdDb, rangeDb, attackMs, releaseMs, band) = bandOut
with {
    detector = band : peakEnv(10.0);
    gain     = detector : compGain(thresholdDb, rangeDb, attackMs, releaseMs);
    bandOut  = band * gain;
};

subBell(x)  = x : fi.peak_eq_cq(SUB_GAIN_DB, SUB_FREQ_HZ, SUB_Q);
airNotch(x) = x : fi.peak_eq_cq(-4.1, AIR_FREQ_HZ, AIR_Q);

lowBand(x)  = x : fi.lowpass(2, LOW_XOVER_HZ);
midBand(x)  = x : fi.highpass(2, LOW_XOVER_HZ) : fi.lowpass(2, MID_CENTER_HZ);
highBand(x) = x : fi.highpass(2, MID_CENTER_HZ) : fi.lowpass(2, HIGH_XOVER_HZ);
airBand(x)  = x : fi.highpass(2, HIGH_XOVER_HZ);

// NOTE: subBell and airNotch are applied as pre-EQ to the full signal,
// then the signal is split into four exclusive frequency bands for dynamics.
// Previously all six terms were summed directly which tripled the output level.
processChan(x) = out
with {
    xeq = x : subBell : airNotch;
    b1  = lowBand(xeq)  : dynamicBand(-5.4, -4.5, 69.82, 40.27);
    b2  = midBand(xeq)  : dynamicBand(0.5,  -3.1,  7.50, 29.92);
    b3  = highBand(xeq) : dynamicBand(0.5,  -4.5, 30.06, 20.04);
    b4  = airBand(xeq)  : dynamicBand(1.2,  -4.5, 20.00,  5.00);
    out = (b1 + b2 + b3 + b4) * db2lin(OUTPUT_TRIM_DB);
};

process = par(i, 2, processChan);
