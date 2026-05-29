// ================================================================
// C1 Gate Mono Approx
// ================================================================
// Effect-first approximation of the Waves C1 Gate "Classic gate"
// preset found in the template A preset export.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "C1 Gate Mono Approx";
declare version     "1.0";
declare description "Soft expander/gate approximating Waves C1 Gate Classic gate";

import("stdfaust.lib");

THRESHOLD_DB = -55.0;
FLOOR_DB     = -12.0;
KNEE_DB      = 18.0;
ATTACK_MS    = 5.0;
RELEASE_MS   = 180.0;
SIDECHAIN_HP = 90.0;
SIDECHAIN_LP = 7127.0;

db2lin(x) = pow(10.0, x / 20.0);
clamp01(x) = min(1.0, max(0.0, x));
mix(a, b, t) = a * (1.0 - t) + b * t;

envFollower(x) = detector : abs : si.smooth(ba.tau2pole(0.012))
with {
    detector = x : fi.highpass(1, SIDECHAIN_HP) : fi.lowpass(1, SIDECHAIN_LP);
};

targetGain(level) = db2lin(gainDb)
with {
    levelDb = ba.linear2db(max(level, ma.MIN));
    openAmt = clamp01((levelDb - (THRESHOLD_DB - KNEE_DB * 0.5)) / KNEE_DB);
    gainDb  = mix(FLOOR_DB, 0.0, openAmt);
};

smoothGate = loop ~ _
with {
    attackPole  = ba.tau2pole(ATTACK_MS * 0.001);
    releasePole = ba.tau2pole(RELEASE_MS * 0.001);
    loop(prev, target) = select2(
        target > prev,
        prev + (target - prev) * (1.0 - releasePole),
        prev + (target - prev) * (1.0 - attackPole)
    );
};

process(x) = x * (x : envFollower : targetGain : smoothGate);
