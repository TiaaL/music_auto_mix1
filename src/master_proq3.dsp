// ================================================================
// Master Bus EQ — FabFilter Pro-Q3 style snapshot approximation
// ================================================================

declare name        "Master Bus Pro-Q3 Approx";
declare version     "1.0";
declare description "Stereo master-bus EQ with fixed Pro-Q3-style settings";

import("stdfaust.lib");

HP_FREQ     = 21.0;
LOW_FREQ    = 70.0;
LOW_GAIN    = 0.85;
LOW_Q       = 2.6;
MID1_FREQ   = 302.0;
MID1_GAIN   = -0.74;
MID1_Q      = 2.3;
MID2_FREQ   = 4047.0;
MID2_GAIN   = -0.18;
MID2_Q      = 1.0;
LP_FREQ     = 20659.0;

eqChain = fi.highpass(2, HP_FREQ)
       : fi.peak_eq_cq(LOW_GAIN, LOW_FREQ, LOW_Q)
       : fi.peak_eq_cq(MID1_GAIN, MID1_FREQ, MID1_Q)
       : fi.peak_eq_cq(MID2_GAIN, MID2_FREQ, MID2_Q)
       : fi.lowpass(2, LP_FREQ);

process = par(i, 2, eqChain);
