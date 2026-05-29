declare name "Template C Vocal Pro-Q3 Approx";
declare version "1.0";
declare description "Template C vocal EQ approximation for the active Pro-Q 3 insert";

import("stdfaust.lib");

eq = fi.highpass(2, 70.0)
   : fi.peak_eq_cq(-1.4, 280.0, 1.2)
   : fi.peak_eq_cq(-0.8, 950.0, 1.0)
   : fi.peak_eq_cq(0.7, 4800.0, 0.9)
   : fi.high_shelf(0.2, 11000.0);

process = eq;
