// ================================================================
// Renaissance Bass Mono Approx
// ================================================================
// Low-frequency enhancement for template B vocal. Uses the exported
// "Acappella Bass Voice" snapshot as a tonal guide: frequency around
// 153 Hz, moderate intensity, and a small output trim.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "Renaissance Bass Mono Approx";
declare version     "1.0";
declare description "Low-band harmonic enhancement inspired by Waves Renaissance Bass";

import("stdfaust.lib");

BASS_FREQ_HZ   = 153.0;
INTENSITY      = 0.38;
HARMONIC_DB    = -13.5;
DIRECT_LOW_DB  = 2.5;
OUTPUT_DB      = -0.8;

db2lin(x) = pow(10.0, x / 20.0);
softsat(x) = x / (1.0 + abs(x));
drive(x) = softsat(x * (1.0 + INTENSITY * 8.0));

lowBand(x) = x : fi.lowpass(2, BASS_FREQ_HZ);
bodyBand(x) = x : fi.highpass(1, 35.0) : fi.lowpass(2, BASS_FREQ_HZ * 2.1);

harmonics(x) = h
with {
    low = x : lowBand;
    even = abs(drive(low)) : fi.highpass(1, 28.0);
    odd = drive(low) - low;
    h = (even * 0.62 + odd * 0.38) : bodyBand;
};

process(x) = out
with {
    low = x : lowBand;
    lowLift = low * (db2lin(DIRECT_LOW_DB) - 1.0) * 0.35;
    synth = x : harmonics : *(db2lin(HARMONIC_DB));
    out = (x + lowLift + synth * INTENSITY) * db2lin(OUTPUT_DB);
};
