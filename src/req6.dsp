// ================================================================
// REQ6-style 6-Band Parametric EQ — Mono
// ================================================================
// Simulates the Waves Renaissance EQ 6-band architecture:
//   Band 1: High-pass filter        (2nd-order Butterworth)
//   Band 2: Low shelf                (3rd-order Butterworth shelf)
//   Band 3: Low-mid bell             (constant-Q peaking EQ)
//   Band 4: High-mid bell            (constant-Q peaking EQ)
//   Band 5: High shelf               (3rd-order Butterworth shelf)
//   Band 6: Low-pass filter          (2nd-order Butterworth)
//
// Uses Faust standard library filters (filters.lib):
//   fi.highpass(N, fc), fi.lowpass(N, fc)
//   fi.low_shelf(dBgain, fc), fi.high_shelf(dBgain, fc)
//   fi.peak_eq_cq(dBgain, fc, Q)
//
// Input:  1 channel (mono)
// Output: 1 channel (mono)
// ================================================================

declare name        "REQ6 Renaissance EQ Simulation";
declare version     "1.0";
declare description "6-band parametric EQ: HPF + low shelf + 2x bell + high shelf + LPF";

import("stdfaust.lib");

// ----------------------------------------------------------------
// Parameters — edit here
// ----------------------------------------------------------------

// Band 1: High-pass / low-cut
B1_ON    = 1;         // 0 = bypass, 1 = enabled
B1_SLOPE = 0;         // 0 = 12 dB/oct, 1 = 24 dB/oct
B1_FREQ  = 40.0;      // Hz: cutoff frequency (12 dB/oct)

// Band 2: Low shelf
B2_ON    = 0;         // 0 = bypass, 1 = enabled
B2_MODE  = 1;         // 1 = low shelf, 2 = bell
B2_FREQ  = 120.0;     // Hz: shelf frequency
B2_GAIN  = 0.0;       // dB: shelf gain  (-20 … +20)
B2_Q     = 0.7;       // used when B2_MODE = 2 (bell)

// Band 3: Low-mid bell
B3_ON    = 1;         // 0 = bypass, 1 = enabled
B3_FREQ  = 228.0;     // Hz: center frequency
B3_GAIN  = -2.5;      // dB: peak/cut gain
B3_Q     = 0.8;       // Q factor (bandwidth = fc / Q)

// Band 4: High-mid bell
B4_ON    = 1;         // 0 = bypass, 1 = enabled
B4_FREQ  = 7711.0;    // Hz: center frequency
B4_GAIN  = -0.7;      // dB: peak/cut gain
B4_Q     = 1.4;       // Q factor

// Band 5: High shelf
B5_ON    = 1;         // 0 = bypass, 1 = enabled
B5_MODE  = 1;         // 1 = high shelf, 2 = bell
B5_FREQ  = 17596.0;   // Hz: shelf frequency
B5_GAIN  = 1.0;       // dB: shelf gain
B5_Q     = 0.72;      // used when B5_MODE = 2 (bell)

// Band 6: Low-pass / high-cut
B6_ON    = 0;         // 0 = bypass, 1 = enabled
B6_SLOPE = 0;         // 0 = 12 dB/oct, 1 = 24 dB/oct
B6_FREQ  = 20000.0;   // Hz: cutoff frequency (12 dB/oct)

// Output gain trim
OUT_GAIN = 0.0;       // dB

// ----------------------------------------------------------------
// Main process — 6 bands in series
// ----------------------------------------------------------------

band1(x) = select2(B1_ON, x,
    select2(B1_SLOPE, x : fi.highpass(2, B1_FREQ), x : fi.highpass(4, B1_FREQ))
);
band2(x) = select2(B2_ON, x,
    select2(B2_MODE == 1, x : fi.peak_eq_cq(B2_GAIN, B2_FREQ, B2_Q), x : fi.low_shelf(B2_GAIN, B2_FREQ))
);
band3(x) = select2(B3_ON, x, x : fi.peak_eq_cq(B3_GAIN, B3_FREQ, B3_Q));
band4(x) = select2(B4_ON, x, x : fi.peak_eq_cq(B4_GAIN, B4_FREQ, B4_Q));
band5(x) = select2(B5_ON, x,
    select2(B5_MODE == 1, x : fi.peak_eq_cq(B5_GAIN, B5_FREQ, B5_Q), x : fi.high_shelf(B5_GAIN, B5_FREQ))
);
band6(x) = select2(B6_ON, x,
    select2(B6_SLOPE, x : fi.lowpass(2, B6_FREQ), x : fi.lowpass(4, B6_FREQ))
);

process = band1 : band2 : band3 : band4 : band5 : band6 : *(ba.db2linear(OUT_GAIN));
