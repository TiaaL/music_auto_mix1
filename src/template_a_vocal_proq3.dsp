declare name "Template A Vocal Pro-Q3 Approx";
declare version "1.0";
declare description "Template A vocal EQ approximation for the active Pro-Q 3 insert";

import("stdfaust.lib");

eq = fi.highpass(2, 75.0)
   : fi.peak_eq_cq(-2.0, 230.0, 1.2)
   : fi.peak_eq_cq(-1.0, 650.0, 1.1)
   : fi.peak_eq_cq(1.2, 3600.0, 0.9)
   : fi.high_shelf(0.8, 9500.0);

process = eq;
