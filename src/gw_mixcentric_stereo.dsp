// ================================================================
// GW MixCentric Stereo Approx
// ================================================================
// Master macro processor inspired by Greg Wells MixCentric. The
// exported preset has a non-zero macro value and a small output trim;
// this processor adds gentle glue compression, tonal lift, and
// controlled saturation before the final L2 limiter.
//
// Input:  2 channels
// Output: 2 channels
// ================================================================

declare name        "GW MixCentric Stereo Approx";
declare version     "1.0";
declare description "Stereo glue, tone, and saturation macro inspired by GW MixCentric";

import("stdfaust.lib");

MACRO = 0.34;
OUTPUT_DB = -1.3;
THRESHOLD_DB = -18.0;
RATIO = 1.0 + MACRO * 2.2;
ATTACK_MS = 18.0;
RELEASE_MS = 180.0;

db2lin(x) = pow(10.0, x / 20.0);

env(l, r) = max(abs(l), abs(r)) : si.smooth(ba.tau2pole(0.030));

gainComputer(level) = db2lin(gainDb)
with {
    levelDb = ba.linear2db(max(level, ma.MIN));
    overDb = max(0.0, levelDb - THRESHOLD_DB);
    gainDb = (1.0 / RATIO - 1.0) * overDb;
};

smoothGain = loop ~ _
with {
    attackPole = ba.tau2pole(ATTACK_MS * 0.001);
    releasePole = ba.tau2pole(RELEASE_MS * 0.001);
    loop(prev, target) = select2(
        target < prev,
        prev + (target - prev) * (1.0 - releasePole),
        prev + (target - prev) * (1.0 - attackPole)
    );
};

tone = fi.low_shelf(1.1 * MACRO, 110.0)
     : fi.peak_eq_cq(-0.25 * MACRO, 320.0, 1.0)
     : fi.high_shelf(0.35 * MACRO, 5200.0);

softsat(x) = x / (1.0 + abs(x));
sat(x) = softsat(x * (1.0 + MACRO * 1.5)) * (1.0 + MACRO * 1.5);

process(l, r) = outL, outR
with {
    g = env(l, r) : gainComputer : smoothGain;
    wetL = l : tone : sat;
    wetR = r : tone : sat;
    mixL = l * (1.0 - MACRO * 0.35) + wetL * (MACRO * 0.35);
    mixR = r * (1.0 - MACRO * 0.35) + wetR * (MACRO * 0.35);
    outL = mixL * g * db2lin(OUTPUT_DB);
    outR = mixR * g * db2lin(OUTPUT_DB);
};
