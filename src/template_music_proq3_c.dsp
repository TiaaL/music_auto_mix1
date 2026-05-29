declare name "Template C Music Pro-Q3 Approx";
declare version "1.0";
declare description "Template C accompaniment Pro-Q 3 approximation";

import("stdfaust.lib");

eq = fi.highpass(2, 18.0)
   : fi.peak_eq_cq(-0.8, 260.0, 1.1)
   : fi.peak_eq_cq(-0.65, 2100.0, 1.0)
   : fi.peak_eq_cq(-0.25, 6200.0, 0.8);

process = par(i, 2, eq);
