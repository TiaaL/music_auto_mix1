declare name "Template A/B Bus Pro-Q3 Approx";
declare version "1.0";
declare description "A/B Stereo Out Pro-Q 3 approximation";

import("stdfaust.lib");

eq = fi.highpass(2, 21.0)
   : fi.peak_eq_cq(0.65, 70.0, 2.2)
   : fi.peak_eq_cq(-0.6, 302.0, 2.0)
   : fi.peak_eq_cq(-0.2, 4047.0, 1.0)
   : fi.lowpass(2, 20659.0);

process = par(i, 2, eq);
