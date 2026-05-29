declare name "Template C Bus Pro-Q3 Approx";
declare version "1.0";
declare description "Template C Stereo Out Pro-Q 3 approximation";

import("stdfaust.lib");

eq = fi.highpass(2, 21.0)
   : fi.peak_eq_cq(0.45, 80.0, 1.8)
   : fi.peak_eq_cq(-0.75, 340.0, 1.7)
   : fi.peak_eq_cq(-0.2, 5200.0, 1.0)
   : fi.lowpass(2, 20659.0);

process = par(i, 2, eq);
