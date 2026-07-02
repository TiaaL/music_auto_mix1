// ================================================================
// Vocal Rider Mono Core-Safe Approx
// ================================================================
// Template C lead vocal rider that protects the vocal core.
// Compared with vocal_rider_mono, this version avoids pulling active lead
// phrases down toward a low target; it mainly catches obvious level drift.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "Vocal Rider Mono Core-Safe Approx";
declare version     "1.0";
declare description "Slow vocal level rider that preserves lead core density";

import("stdfaust.lib");

TARGET_DB    = -19.8;
MIN_RIDE_DB  = -0.2;
MAX_RIDE_DB  = 1.4;
SENSITIVITY  = 0.18;
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
