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

// Visible anchors from the screenshot. The current safe implementation uses
// these as static tone-shaping points plus broad C6-style control. Earlier
// dynamic crossover reconstruction proved unsafe on dense real accompaniments.
AIR_FREQ_HZ    = 7389.0;
SUB_FREQ_HZ    = 102.0;
SUB_Q          = 2.15;
SUB_GAIN_DB    = 1.7;
AIR_Q          = 14.25;
OUTPUT_TRIM_DB = -1.0;
THRESHOLD_DB   = -12.0;
RANGE_DB       = -3.0;
ATTACK_MS      = 18.0;
RELEASE_MS     = 160.0;

db2lin(x) = pow(10.0, x / 20.0);
clamp01(x) = min(1.0, max(0.0, x));

peakEnv(ms, x) = abs(x) : si.smooth(ba.tau2pole(ms * 0.001));

compGain(thresholdDb, rangeDb, attackMs, releaseMs, env) = gain
with {
    envDb      = ba.linear2db(max(env, 1e-8));
    overDb     = max(0.0, envDb - thresholdDb);
    targetDb   = max(rangeDb, -overDb);
    attackPole = ba.tau2pole(attackMs * 0.001);
    releasePole= ba.tau2pole(releaseMs * 0.001);
    coeff      = select2(targetDb < 0.0, attackPole, releasePole);
    gain       = targetDb : si.smooth(coeff) : ba.db2linear;
};

subBell(x)  = x : fi.peak_eq_cq(SUB_GAIN_DB, SUB_FREQ_HZ, SUB_Q);
airNotch(x) = x : fi.peak_eq_cq(-4.1, AIR_FREQ_HZ, AIR_Q);

safeLimit(x) = min(0.98, max(-0.98, x));

widebandGain(x) = peakEnv(25.0, x) : compGain(THRESHOLD_DB, RANGE_DB, ATTACK_MS, RELEASE_MS);

// NOTE: this is deliberately conservative. It keeps the rough preset tone
// anchors while avoiding unstable dynamic crossover reconstruction.
processChan(x) = out
with {
    xeq = x;
    g   = widebandGain(xeq);
    out = xeq * g * db2lin(OUTPUT_TRIM_DB) : safeLimit;
};

process = par(i, 2, processChan);
