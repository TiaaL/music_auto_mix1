// ================================================================
// OneKnob Brighter Mono Approx
// ================================================================
// Template C brightness enhancer. The exported snapshot carries a
// non-zero macro amount; this approximation combines high shelving
// with a very small high-band saturation component.
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "OneKnob Brighter Mono Approx";
declare version     "1.0";
declare description "One-knob style vocal brightness and presence enhancer";

import("stdfaust.lib");

AMOUNT = 0.22;
SHELF_HZ = 3600.0;
SHELF_DB = 3.0 * AMOUNT;
AIR_HZ = 9200.0;
AIR_DB = 1.0 * AMOUNT;
SAT_DB = -42.0;

db2lin(x) = pow(10.0, x / 20.0);
softsat(x) = x / (1.0 + abs(x));

process(x) = shaped + excite
with {
    bright = x : fi.high_shelf(SHELF_DB, SHELF_HZ) : fi.high_shelf(AIR_DB, AIR_HZ);
    high = x : fi.highpass(1, SHELF_HZ);
    excite = (softsat(high * (1.0 + AMOUNT * 6.0)) - high) * db2lin(SAT_DB);
    shaped = bright;
};
