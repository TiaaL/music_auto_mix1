// ================================================================
// C1 Compressor Simulation — Vocal Core-Safe Mono
// ================================================================
// A lighter lead-vocal compressor for templates A/B/C. It keeps the C1-style RMS
// soft-knee behavior but raises the threshold, lowers the ratio and slows the
// attack so 1k-1.6k lead core and short-frame articulation are less flattened.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "C1 Compressor Simulation Vocal Core-Safe";
declare version     "1.0";
declare description "Lighter RMS soft-knee vocal compressor preserving lead core";

import("stdfaust.lib");

mix(a, b, t) = a * (1.0 - t) + b * t;

THRESHOLD_DB = -12.0;
RATIO        = 1.6;
ATTACK_MS    = 24.0;
RELEASE_MS   = 280.0;
KNEE_DB      = 8.0;
MAKEUP_DB    = 0.8;
RMS_WIN_MS   = 10.0;
PEAK_WIN_MS  = 1.0;
DETECTOR_MIX = 1.0;
SIDECHAIN_HP_ON = 0;
SIDECHAIN_HP_HZ = 120.0;
PDR_AMOUNT   = 1.0;

rmsCoeff = exp(-1.0 / (RMS_WIN_MS * 0.001 * ma.SR));
peakCoeff = exp(-1.0 / (PEAK_WIN_MS * 0.001 * ma.SR));

rmsLevel = _ <: * : si.smooth(rmsCoeff) : sqrt;
peakLevel = abs : si.smooth(peakCoeff);

gainComputer(thresh, ratio, knee, levelLin) = gainDB
with {
    levelDB  = ba.linear2db(max(levelLin, ma.MIN));
    excess   = levelDB - thresh;
    W2       = knee * 0.5;
    inKnee   = (2.0 * abs(excess)) <= knee;
    above    = excess > W2;
    kneeGain = (1.0/ratio - 1.0) * pow(excess + W2, 2.0) / (2.0 * knee);
    fullGain = (1.0/ratio - 1.0) * excess;
    gainDB   = select2(above,
                   select2(inKnee, 0.0, kneeGain),
                   fullGain);
};

coeffA = exp(-1.0 / (ATTACK_MS  * 0.001 * ma.SR));
coeffR = exp(-1.0 / (RELEASE_MS * 0.001 * ma.SR));

smoothGainDB = loop ~ _
with {
    pdrScale(target) = 1.0 + PDR_AMOUNT * abs(target) / 24.0;
    coeffRelease(target) = exp(-1.0 / ((RELEASE_MS / pdrScale(target)) * 0.001 * ma.SR));
    loop(prev, target) = select2(target < prev,
        prev + (target - prev) * (1.0 - coeffRelease(target)),
        prev + (target - prev) * (1.0 - coeffA)
    );
};

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

process = _ : c1 : *(ba.db2linear(MAKEUP_DB));
