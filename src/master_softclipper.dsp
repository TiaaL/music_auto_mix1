// ================================================================
// Master Bus SoftClipper — plugin-style audible approximation
// ================================================================

declare name        "Master Bus SoftClipper Approx";
declare version     "1.0";
declare description "Stereo soft clipper approximating the posted soft-clip snapshot";

import("stdfaust.lib");

INPUT_DB          = 4.0;
MIX_PERCENT       = 100.0;
OUTPUT_DB         = -0.7;
SECOND_PERCENT    = 60.0;
THIRD_PERCENT     = 40.0;
DRIVE_STRENGTH    = 2.2;

wetMix      = min(max(MIX_PERCENT / 100.0, 0.0), 1.0);
dryMix      = 1.0 - wetMix;
shapeTilt   = (SECOND_PERCENT - THIRD_PERCENT) / max(SECOND_PERCENT + THIRD_PERCENT, 1e-9);
inGain      = ba.db2linear(INPUT_DB);
outGain     = ba.db2linear(OUTPUT_DB);

clipCore(x) = x / (1.0 + abs(x) * clipSlope)
with {
    clipSlope = 0.95 + 0.25 * abs(shapeTilt) + 0.1 * (DRIVE_STRENGTH - 1.0);
};

softclip(x) = x * dryMix + clipCore(x * inGain) * wetMix;

process = par(i, 2, softclip : *(outGain));
