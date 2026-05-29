// ================================================================
// Vocal Rider Mono Approx
// ================================================================
// Template C level rider inspired by the exported Vocal Rider
// snapshot. This is a transparent gain rider, not a compressor:
// it slowly pulls phrases toward target loudness within a fixed
// range before the C1 comp stage.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "Vocal Rider Mono Approx";
declare version     "1.0";
declare description "Slow vocal level rider inspired by Waves Vocal Rider";

import("stdfaust.lib");

TARGET_DB    = -22.5;
MIN_RIDE_DB  = -2.6;
MAX_RIDE_DB  = 2.6;
SENSITIVITY  = 0.55;
SMOOTH_MS    = 1000.0;
NOISE_FLOOR_DB = -60.0;

db2lin(x) = pow(10.0, x / 20.0);
clamp(lo, hi, x) = min(hi, max(lo, x));

phraseRms(x) = x <: * : si.smooth(ba.tau2pole(SMOOTH_MS * 0.001)) : sqrt;

rideGain(level) = db2lin(rideDb)
with {
    levelDb = ba.linear2db(max(level, ma.MIN));
    desired = (TARGET_DB - levelDb) * SENSITIVITY;
    active = float(levelDb > NOISE_FLOOR_DB);
    rideDb = clamp(MIN_RIDE_DB, MAX_RIDE_DB, desired) * active;
};

process(x) = x * (x : phraseRms : rideGain);
