// ================================================================
// Accompaniment EQ — FabFilter Pro-Q3 style snapshot approximation
// ================================================================

declare name        "Accompaniment Pro-Q3 Approx";
declare version     "1.0";
declare description "Stereo accompaniment EQ with fixed Pro-Q3-style settings";

import("stdfaust.lib");

HP_FREQ     = 14.0;
BAND1_FREQ  = 2241.0;
BAND1_GAIN  = -0.74;
BAND1_Q     = 1.0;
BAND2_FREQ  = 6119.0;
BAND2_GAIN  = 0.39;
BAND2_Q     = 0.5;

eqChain = fi.highpass(2, HP_FREQ)
       : fi.peak_eq_cq(BAND1_GAIN, BAND1_FREQ, BAND1_Q)
       : fi.peak_eq_cq(BAND2_GAIN, BAND2_FREQ, BAND2_Q);

process = par(i, 2, eqChain);
