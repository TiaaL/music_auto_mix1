// ================================================================
// L1 Limiter Mono Approx
// ================================================================
// Mono peak limiter for template B vocal. Uses the exported Voice
// preset values as readable anchors: threshold -3.6 dB, ceiling
// -2.7 dB, release around 250 ms.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "L1 Limiter Mono Approx";
declare version     "1.0";
declare description "Mono lookahead peak limiter inspired by Waves L1 Voice";

import("stdfaust.lib");

THRESHOLD_DB = -3.6;
CEILING_DB   = -2.7;
RELEASE_MS   = 250.7;
LOOKAHEAD_MS = 1.0;

threshold = ba.db2linear(THRESHOLD_DB);
ceiling = ba.db2linear(CEILING_DB);
lookahead = int(LOOKAHEAD_MS * ma.SR / 1000.0);
releasePole = ba.tau2pole(RELEASE_MS * 0.001);

targetGain(peak) = select2(peak > threshold, 1.0, ceiling / max(peak, 1e-9));

smoothGain = loop ~ _
with {
    loop(prev, target) = select2(
        (prev < 1e-10) | (target < prev),
        prev + (target - prev) * (1.0 - releasePole),
        target
    );
};

clip = min(ceiling) : max(0.0 - ceiling);

process(x) = ((x @ lookahead) * gain) : clip
with {
    gain = x : abs : targetGain : smoothGain;
};
