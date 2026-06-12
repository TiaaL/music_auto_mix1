// ================================================================
// Master Bus L2 Stereo — Waves L2 style snapshot approximation
// ================================================================

declare name        "Master Bus L2 Stereo Approx";
declare version     "1.0";
declare description "Stereo lookahead limiter approximating the posted L2 master-bus settings";

import("stdfaust.lib");

CEILING_DB    = -1.5;
LOOKAHEAD_MS  = 1.5;
ATTACK_MS     = 0.5;
RELEASE_MS    = 60.0;

ceiling       = ba.db2linear(CEILING_DB);
lookahead_n   = int(LOOKAHEAD_MS * ma.SR / 1000.0);

soft_clip(x) = ceiling * ma.tanh(x / ceiling);

limit_gain(peak) = min(1.0, ceiling / max(peak, 1e-10));

attackPole  = ba.tau2pole(ATTACK_MS  * 0.001);
releasePole = ba.tau2pole(RELEASE_MS * 0.001);

smoothG = loop ~ _
with {
    loop(prev, target) = select2(target < prev,
        prev + (target - prev) * (1.0 - releasePole),  // gain returning up
        prev + (target - prev) * (1.0 - attackPole)    // gain going down (more reduction)
    );
};

l2_arc(l, r) = l_out, r_out
with {
    inL     = l;
    inR     = r;
    peak    = max(abs(inL), abs(inR));
    g       = peak : limit_gain : smoothG;
    l_out   = (inL @ lookahead_n) * g : soft_clip;
    r_out   = (inR @ lookahead_n) * g : soft_clip;
};

process = l2_arc;
