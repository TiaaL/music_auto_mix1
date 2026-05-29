declare name "Template A/B Music Pro-Q3 Approx";
declare version "1.0";
declare description "A/B accompaniment Pro-Q 3 approximation";

import("stdfaust.lib");

eq = fi.highpass(2, 14.0)
   : fi.peak_eq_cq(-0.74, 2241.0, 1.0)
   : fi.peak_eq_cq(0.39, 6119.0, 0.5);

process = par(i, 2, eq);
