declare name "Template A Vocal Pro-Q3 Approx";
declare version "1.0";
declare description "Template A vocal EQ approximation for the active Pro-Q 3 insert";

import("stdfaust.lib");

// Template A is the muddy / dark / boxy vocal class, so it gets a slightly stronger
// presence lift and high shelf than before (+1.6 @ 3.6k, +1.2 shelf @ 9.5k) to add
// brightness/air. Safe because the vocal HF guard low-passes resampling grain above
// ~10.5 kHz downstream, so the shelf brightens real presence without amplifying hiss.
eq = fi.highpass(2, 75.0)
   : fi.peak_eq_cq(-2.0, 230.0, 1.2)
   : fi.peak_eq_cq(-1.0, 650.0, 1.1)
   : fi.peak_eq_cq(1.6, 3600.0, 0.9)
   : fi.high_shelf(1.2, 9500.0);

process = eq;
