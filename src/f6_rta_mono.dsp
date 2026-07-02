// ================================================================
// F6-RTA Mono Approx
// ================================================================
// Dynamic EQ inspired by the exported Waves F6-RTA snapshot on
// template B vocal. The preset export contains readable frequency
// anchors, but the exact Waves parameter index map is not confirmed;
// this processor prioritizes the audible role: cleanup, body focus,
// and controlled upper-mid harshness before compression.
// 注意：不改 F6 的原动态质感，只去掉 24 kHz 渲染下会打没信号的无效 21 kHz lowpass。
//
// Input:  1 channel
// Output: 1 channel
// ================================================================

declare name        "F6-RTA Mono Approx";
declare version     "1.0";
declare description "Six-band vocal dynamic EQ approximation for template B";

import("stdfaust.lib");

HP_HZ = 31.03;
F1 = 77.70;
F2 = 137.78;
F3 = 368.26;
F4 = 1037.38;
F5 = 1843.51;
F6 = 3338.25;
LP_HZ = 8713.81;

db2lin(x) = pow(10.0, x / 20.0);

peakEnv(ms, x) = abs(x) : si.smooth(ba.tau2pole(ms * 0.001));

dynCut(freq, q, thresholdDb, rangeDb, x) = x + band * (gain - 1.0)
with {
    band = x : fi.peak_eq_cq(9.0, freq, q) - x;
    envDb = band : peakEnv(12.0) : max(ma.MIN) : ba.linear2db;
    overDb = max(0.0, envDb - thresholdDb);
    gain = db2lin(max(rangeDb, 0.0 - overDb));
};

staticTone = fi.highpass(2, HP_HZ)
          : fi.peak_eq_cq(0.4, F1, 1.0)
          : fi.peak_eq_cq(0.3, F2, 1.0)
          : fi.peak_eq_cq(-0.8, F3, 1.0)
          : fi.peak_eq_cq(-1.2, F4, 0.9)
          : fi.peak_eq_cq(0.2, F5, 0.8)
          : fi.peak_eq_cq(0.0, F6, 0.8)
          : fi.high_shelf(0.0, LP_HZ);

process = staticTone
        : dynCut(244.1, 0.7, -14.0, -2.8)
        : dynCut(3338.25, 2.0, -24.0, -2.4)
        : dynCut(5000.0, 3.0, -22.1, -2.8);
