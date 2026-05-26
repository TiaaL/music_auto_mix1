// ================================================================
// Accompaniment L2 Stereo — Waves L2 style snapshot approximation
// ================================================================

declare name        "Accompaniment L2 Stereo Approx";
declare version     "1.0";
declare description "Stereo lookahead limiter approximating the posted L2 settings";

import("stdfaust.lib");

THRESHOLD_DB  = -2.1;
CEILING_DB    = 0.0;
LOOKAHEAD_MS  = 3.0;
ARC_MIN_MS    = 50.0;
ARC_MAX_MS    = 800.0;
ARC_WINDOW_MS = 150.0;
KNEE_DB       = 0.0;

threshold    = ba.db2linear(THRESHOLD_DB);
ceiling      = ba.db2linear(CEILING_DB);
lookahead_n  = int(LOOKAHEAD_MS  * ma.SR / 1000.0);
arc_coeff_min = exp(-1.0 / (ARC_MIN_MS * 0.001 * ma.SR));
arc_coeff_max = exp(-1.0 / (ARC_MAX_MS * 0.001 * ma.SR));
knee_lin      = ba.db2linear(max(KNEE_DB, 0.0001));
knee_bottom   = threshold / knee_lin;
knee_top      = threshold * knee_lin;
arc_tau       = ARC_WINDOW_MS * 0.001;
arc_pole      = ba.tau2pole(arc_tau);

limiting_density(peak) = (peak > threshold) : float : si.smooth(arc_pole);
arc_coeff(density) = arc_coeff_min * density + arc_coeff_max * (1.0 - density);

soft_knee_gain(peak) = result
with {
    alpha     = (peak - knee_bottom) / max(knee_top - knee_bottom, 1e-10);
    full_gain = ceiling / max(peak, 1e-10);
    knee_gain = 1.0 + alpha * alpha * (full_gain - 1.0);
    result = select2(peak < knee_bottom,
        select2(peak > knee_top, full_gain, knee_gain),
        1.0
    );
};

gain_smooth_arc(coeff) = loop ~ _
with {
    loop(prev, target) = select2(
        (prev < 1e-10) | (target < prev),
        prev + (1.0 - coeff) * (target - prev),
        target
    );
};

hard_clip = min(ceiling) : max(0.0 - ceiling);

l2_arc(l, r) = (l_out, r_out)
with {
    peak     = max(abs(l), abs(r));
    g_raw    = soft_knee_gain(peak);
    density  = limiting_density(peak);
    coeff    = arc_coeff(density);
    g        = g_raw : gain_smooth_arc(coeff);
    l_out    = (l @ lookahead_n) * g : hard_clip;
    r_out    = (r @ lookahead_n) * g : hard_clip;
};

process = l2_arc;
