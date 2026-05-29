// ================================================================
// Sibilance Mono Approx
// ================================================================
// Effect-first approximation of Waves Sibilance "Voice-over".
// It uses a focused detector around the exported 7.5 kHz region and
// applies smooth high-band attenuation rather than a hard wideband cut.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "Sibilance Mono Approx";
declare version     "1.0";
declare description "Focused split-band sibilance controller for vocal";

import("stdfaust.lib");

DETECT_HZ   = 7500.0;
SPLIT_HZ    = 5000.0;
THRESH_DB   = -16.2;
RANGE_DB    = 11.5;
ATTACK_MS   = 0.1;
RELEASE_MS  = 42.0;
DETECT_Q    = 3.0;

db2lin(x) = pow(10.0, x / 20.0);

rms(ms, x) = x <: * : si.smooth(ba.tau2pole(ms * 0.001)) : sqrt;

gainComputer(level) = db2lin(gainDb)
with {
    levelDb = ba.linear2db(max(level, ma.MIN));
    gainDb = 0.0 - min(RANGE_DB, max(0.0, levelDb - THRESH_DB));
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

process(x) = low + high * gain
with {
    low = x : fi.lowpass(2, SPLIT_HZ);
    high = x - low;
    detector = x : fi.peak_eq_cq(10.0, DETECT_HZ, DETECT_Q);
    gain = detector : rms(2.0) : gainComputer : smoothGain;
};
