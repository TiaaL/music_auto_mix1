// ================================================================
// Master Bus L2 Stereo — Waves L2 style snapshot approximation
// ================================================================

declare name        "Master Bus L2 Stereo Approx";
declare version     "1.0";
declare description "Stereo lookahead limiter approximating the posted L2 master-bus settings";

import("stdfaust.lib");

CEILING_DB    = -1.0;
LOOKAHEAD_MS  = 1.0;

ceiling       = ba.db2linear(CEILING_DB);
lookahead_n   = int(LOOKAHEAD_MS * ma.SR / 1000.0);

hard_clip = min(ceiling) : max(0.0 - ceiling);

limit_gain(peak) = min(1.0, ceiling / max(peak, 1e-10));

l2_arc(l, r) = l_out, r_out
with {
    inL     = l;
    inR     = r;
    peak    = max(abs(inL), abs(inR));
    g       = peak : limit_gain;
    l_out   = (inL @ lookahead_n) * g : hard_clip;
    r_out   = (inR @ lookahead_n) * g : hard_clip;
};

process = l2_arc;
