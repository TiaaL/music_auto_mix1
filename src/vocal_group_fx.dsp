// ================================================================
// Vocal Stereo Group FX Rack
// ================================================================
// Purpose:
//   - Takes a mono vocal track
//   - Places it onto a stereo vocal group
//   - Adapts gain/send behavior based on phrase-level dynamics
//   - Sends the group to 3 stereo FX buses:
//       1) Shimmer-style pitch/reverb
//       2) Stereo reverb
//       3) SuperTap-style stereo multi-tap delay
//
// Important note:
//   This is an approximation-oriented rack built in Faust.
//   It does NOT claim to be a bit-accurate clone of proprietary
//   Waves / Valhalla plugins. The goal here is to expose familiar
//   routing and parameters inside this project.
//
// Input:  1 channel (mono vocal)
// Output: 2 channels (stereo vocal group)
// ================================================================

declare name        "Vocal Stereo Group FX Rack";
declare version     "1.0";
declare description "Stereo vocal group with adaptive dynamics, shimmer, reverb, and multi-tap delay";

import("stdfaust.lib");

// ----------------------------------------------------------------
// Group / routing parameters
// ----------------------------------------------------------------

GROUP_GAIN_DB      = -0.5;    // dB: trim before stereo group processing
GROUP_PAN          = 0.0;     // -1 = left, 0 = center, +1 = right
GROUP_WIDTH        = 1.0;     // 0 = mono center, 1 = normal stereo spread
OUTPUT_GAIN_DB     = -1.5;    // dB: final stereo group trim

// ----------------------------------------------------------------
// Phrase / macro dynamics control
// ----------------------------------------------------------------
// Volume/loudness-driven processing is currently disabled.
//
// PHRASE_WIN_MS      = 900.0;   // ms: long window for phrase-level loudness
// DETAIL_WIN_MS      = 45.0;    // ms: short window for local wave size
// QUIET_LEVEL_DB     = -30.0;   // dBFS: this and below is treated as a quiet phrase
// LOUD_LEVEL_DB      = -15.0;   // dBFS: this and above is treated as a loud phrase
// QUIET_BOOST_DB     = 2.5;     // dB: gain lift applied to quiet phrases
// LOUD_TRIM_DB       = -1.5;    // dB: gain trim applied to loud phrases
// SEND_DUCK_DB       = 4.0;     // dB: how much FX sends duck on loud phrases
// TRANSIENT_TRIM_DB  = 1.5;     // dB: trims send depth when short-term energy spikes

// ----------------------------------------------------------------
// Shimmer-style send (Valhalla Shimmer inspired)
// ----------------------------------------------------------------

SHIMMER_SEND       = 1.0;     // base send scaler
SHIMMER_SEND_PRE_DB = -18.0;  // dB: pre-fader send level
SHIMMER_MIX        = 100.0;   // % wet inside the shimmer return
SHIMMER_GAIN_DB    = -18.0;   // dB: overall shimmer return trim
SHIMMER_PITCH_ST   = 12.0;    // semitones: +12 = octave up
SHIMMER_SIZE       = 1.05;    // reverb size / density scale
SHIMMER_DIFFUSION  = 0.68;    // diffuser density
SHIMMER_TIME_S     = 3.2;     // sec: long tail
SHIMMER_DAMP       = 0.78;    // 0..1: HF damping
SHIMMER_FEEDBACK   = 0.22;    // shimmer tail feedback
SHIMMER_MOD_DEPTH  = 0.06;    // modulation depth
SHIMMER_MOD_RATE_HZ = 0.10;   // Hz: slow motion
SHIMMER_WINDOW_MS  = 55.0;    // ms: pitch shifter window
SHIMMER_XFADE_MS   = 28.0;    // ms: pitch shifter crossfade

// ----------------------------------------------------------------
// Stereo reverb send (RVerb-style naming)
// ----------------------------------------------------------------

RVERB_SEND         = 1.0;     // base send scaler
RVERB_SEND_PRE_DB  = -5.39;   // dB: pre-fader send level
RVERB_TYPE         = 1;       // 0 = hall, 1 = plate, 2 = room (approximate voicing)
RVERB_PREDELAY_MS  = 0.0;     // ms: matches plugin "PreDelay"
RVERB_TIME_S       = 2.40;    // sec: plugin "Time"
RVERB_SIZE         = 100.0;   // 0..100: plugin "Size"
RVERB_DIFFUSION    = 0.0;     // 0..100: plugin "Diffusion"
RVERB_DECAY_SHAPE  = 0.5;     // 0..1: approximates "Decay Linear" style control
RVERB_EARLY_REF_DB = -2.0;    // dB: plugin "Early Ref"
RVERB_REVERB_DB    = 0.0;     // dB: plugin "Reverb"
RVERB_WET_DRY      = 100.0;   // % wet inside the return
RVERB_GAIN_DB      = 0.0;     // dB: output trim for the return
RVERB_DAMP         = 0.35;    // 0..1: tail damping amount
RVERB_DAMP_LO_HZ   = 600.0;   // Hz: low damping hinge
RVERB_DAMP_HI_HZ   = 4200.0;  // Hz: high damping hinge
RVERB_EQ_LO_HZ     = 700.0;   // Hz: tonal contour helper
RVERB_EQ_HI_HZ     = 4200.0;  // Hz: tonal contour helper
RVERB_EQ_LO_GAIN_DB = 0.0;    // dB: post-reverb low-mid contour
RVERB_EQ_HI_GAIN_DB = -4.0;   // dB: post-reverb high contour

// ----------------------------------------------------------------
// Stereo delay send (SuperTap-style naming)
// ----------------------------------------------------------------

SUPERTAP_SEND      = 1.0;     // base send scaler
SUPERTAP_SEND_PRE_DB = -24.0; // dB: pre-fader send level
SUPERTAP_MIX       = 100.0;   // % wet inside the delay return
SUPERTAP_GAIN_DB   = -18.5;   // dB: delay return trim
SUPERTAP_BPM       = 89.0;    // BPM: tempo reference
SUPERTAP_TAP1_BEATS = 0.25;   // note length for tap 1
SUPERTAP_TAP2_BEATS = 0.5;    // note length for tap 2
SUPERTAP_TAP1_DB   = 0.0;     // dB: tap 1 level
SUPERTAP_TAP2_DB   = -3.0;    // dB: tap 2 level
SUPERTAP_TAP1_PAN  = -0.22;   // -1..+1: tap 1 pan graph position
SUPERTAP_TAP2_PAN  = 0.22;    // -1..+1: tap 2 pan graph position
SUPERTAP_FEEDBACK  = 0.12;    // 0..1: repeat depth
SUPERTAP_WIDTH     = 0.45;    // 0..1: stereo spread
SUPERTAP_COLOR_HZ  = 2400.0;  // Hz: darken repeats
SUPERTAP_PINGPONG  = 0;       // 0 = normal stereo spread, 1 = ping-pong
SUPERTAP_CROSS_FB  = 0.0;     // 0..1: cross feedback amount for ping-pong motion
SUPERTAP_OFFSET    = 0.0;     // 0..1: small L/R timing offset
SUPERTAP_DIRECT_ON = 0;       // 0 = off, 1 = on
SUPERTAP_DIRECT_GAIN_DB = -6.0; // dB: direct section gain
SUPERTAP_DIRECT_ROTATE = 0.0; // -1..+1: rotate direct stereo image
SUPERTAP_MOD_SYNC  = 0;       // 0 = manual Hz, 1 = tempo-related feel
SUPERTAP_MOD_RATE_HZ = 0.12;  // Hz: modulation rate
SUPERTAP_MOD_DEPTH_MS = 0.01; // ms: modulation depth
SUPERTAP_EQ1_ON    = 1;       // top EQ band enable
SUPERTAP_EQ1_TYPE  = 2;       // 0 = low shelf, 1 = bell, 2 = high shelf
SUPERTAP_EQ1_FREQ_HZ = 2600.0; // Hz
SUPERTAP_EQ1_GAIN_DB = -5.0;   // dB
SUPERTAP_EQ2_ON    = 1;       // middle EQ band enable
SUPERTAP_EQ2_TYPE  = 1;       // 0 = low shelf, 1 = bell, 2 = high shelf
SUPERTAP_EQ2_FREQ_HZ = 700.0; // Hz
SUPERTAP_EQ2_GAIN_DB = -2.0;  // dB
SUPERTAP_EQ3_ON    = 1;       // bottom EQ band enable
SUPERTAP_EQ3_TYPE  = 0;       // 0 = low shelf, 1 = bell, 2 = high shelf
SUPERTAP_EQ3_FREQ_HZ = 140.0; // Hz
SUPERTAP_EQ3_GAIN_DB = -2.0;  // dB

// ----------------------------------------------------------------
// Utilities
// ----------------------------------------------------------------

db2lin(x) = pow(10.0, x / 20.0);
clamp01(x) = min(1.0, max(0.0, x));
mix(a, b, t) = a * (1.0 - t) + b * t;
delaySamples(ms) = int(ms * 0.001 * ma.SR);
stereoGain(g) = par(i, 2, *(g));
beatMs(bpm) = 60000.0 / max(bpm, 1.0);
panL(p) = sqrt((1.0 - clamp01((p + 1.0) * 0.5)));
panR(p) = sqrt(clamp01((p + 1.0) * 0.5));

rmsLevel(ms, x) = x <: * : si.smooth(exp(-1.0 / (ms * 0.001 * ma.SR))) : sqrt;
levelDB(ms, x) = rmsLevel(ms, x) : max(ma.MIN) : ba.linear2db;

// Constant-power mono -> stereo panner.
panStereo(x) = l, r
with {
    angle = (GROUP_PAN + 1.0) * ma.PI / 4.0;
    baseL = x * cos(angle);
    baseR = x * sin(angle);
    mid   = (baseL + baseR) * 0.5;
    l     = mix(mid, baseL, GROUP_WIDTH);
    r     = mix(mid, baseR, GROUP_WIDTH);
};

// ----------------------------------------------------------------
// Phrase-aware macro control
// ----------------------------------------------------------------
// Volume/loudness-driven macro shaping is commented out for now.
//
// phraseNorm(x) = clamp01((levelDB(PHRASE_WIN_MS, x) - QUIET_LEVEL_DB) / max(LOUD_LEVEL_DB - QUIET_LEVEL_DB, 0.001));
// transientNorm(x) = clamp01((levelDB(DETAIL_WIN_MS, x) - levelDB(PHRASE_WIN_MS, x)) / 12.0);
//
// macroGain(x) = db2lin(gainDB)
// with {
//     gainDB = mix(QUIET_BOOST_DB, LOUD_TRIM_DB, phraseNorm(x));
// };
//
// macroSend(x) = db2lin(sendTrimDB)
// with {
//     sendTrimDB = -(phraseNorm(x) * SEND_DUCK_DB + transientNorm(x) * TRANSIENT_TRIM_DB);
// };

macroGain(x) = 1.0;
macroSend(x) = 1.0;

// ----------------------------------------------------------------
// Shimmer-style FX
// ----------------------------------------------------------------

shimmerFx = par(i, 2, ef.transpose(winSamples, xfadeSamples, SHIMMER_PITCH_ST))
          : re.greyhole(SHIMMER_TIME_S, SHIMMER_DAMP, SHIMMER_SIZE,
                         SHIMMER_DIFFUSION, SHIMMER_FEEDBACK,
                         SHIMMER_MOD_DEPTH, SHIMMER_MOD_RATE_HZ)
with {
    winSamples   = delaySamples(SHIMMER_WINDOW_MS);
    xfadeSamples = delaySamples(SHIMMER_XFADE_MS);
};

// ----------------------------------------------------------------
// Stereo pre-delay helper
// ----------------------------------------------------------------

stereoPredelay(ms) = de.delay(65536, delaySamples(ms)), de.delay(65536, delaySamples(ms));

tapPanStereo(p) = l, r
with {
    l = _ : *(panL(p));
    r = _ : *(panR(p));
};

eqBand(on, type, freq, gain, x) = out
with {
    shelfLow  = x : fi.low_shelf(gain, freq);
    bell      = x : fi.peak_eq_cq(gain, freq, 0.8);
    shelfHigh = x : fi.high_shelf(gain, freq);
    isLow     = float(type == 0);
    isBell    = float(type == 1);
    isHigh    = float(type == 2);
    shaped    = shelfLow * isLow + bell * isBell + shelfHigh * isHigh;
    out       = x * float(on == 0) + shaped * float(on != 0);
};

superTapEq(l, r) = outL, outR
with {
    eq1L = eqBand(SUPERTAP_EQ1_ON, SUPERTAP_EQ1_TYPE, SUPERTAP_EQ1_FREQ_HZ, SUPERTAP_EQ1_GAIN_DB, l);
    eq1R = eqBand(SUPERTAP_EQ1_ON, SUPERTAP_EQ1_TYPE, SUPERTAP_EQ1_FREQ_HZ, SUPERTAP_EQ1_GAIN_DB, r);
    eq2L = eqBand(SUPERTAP_EQ2_ON, SUPERTAP_EQ2_TYPE, SUPERTAP_EQ2_FREQ_HZ, SUPERTAP_EQ2_GAIN_DB, eq1L);
    eq2R = eqBand(SUPERTAP_EQ2_ON, SUPERTAP_EQ2_TYPE, SUPERTAP_EQ2_FREQ_HZ, SUPERTAP_EQ2_GAIN_DB, eq1R);
    outL = eqBand(SUPERTAP_EQ3_ON, SUPERTAP_EQ3_TYPE, SUPERTAP_EQ3_FREQ_HZ, SUPERTAP_EQ3_GAIN_DB, eq2L);
    outR = eqBand(SUPERTAP_EQ3_ON, SUPERTAP_EQ3_TYPE, SUPERTAP_EQ3_FREQ_HZ, SUPERTAP_EQ3_GAIN_DB, eq2R);
};

directRotate(inL, inR) = outL, outR
with {
    rot  = clamp01((SUPERTAP_DIRECT_ROTATE + 1.0) * 0.5);
    outL = mix(inL, inR, rot * 0.35) * db2lin(SUPERTAP_DIRECT_GAIN_DB);
    outR = mix(inR, inL, (1.0 - rot) * 0.35) * db2lin(SUPERTAP_DIRECT_GAIN_DB);
};

// ----------------------------------------------------------------
// Stereo reverb FX
// ----------------------------------------------------------------

reverbTypeEarlyDiff = select2(RVERB_TYPE == 0,
    select2(RVERB_TYPE == 1, 0.82, 0.70),
    0.88
);

reverbTypeModDepth = select2(RVERB_TYPE == 0,
    select2(RVERB_TYPE == 1, 0.18, 0.08),
    0.12
);

reverbTypeModRate = select2(RVERB_TYPE == 0,
    select2(RVERB_TYPE == 1, 0.35, 0.20),
    0.25
);

reverbSizeScale = mix(0.7, 2.5, clamp01(RVERB_SIZE / 100.0));
reverbDiffusionNorm = mix(0.35, 0.92, clamp01(RVERB_DIFFUSION / 100.0));
reverbLowMult = mix(1.15, 0.95, RVERB_DECAY_SHAPE);
reverbMidMult = 1.0;
reverbHighMult = mix(0.65, 0.95, 1.0 - RVERB_DAMP);

stereoReverbFx = stereoPredelay(RVERB_PREDELAY_MS)
              : par(i, 2, fi.highpass(1, 35.0))
              : re.jpverb(RVERB_TIME_S, RVERB_DAMP, reverbSizeScale,
                          max(reverbDiffusionNorm, reverbTypeEarlyDiff),
                          reverbTypeModDepth, reverbTypeModRate,
                          reverbLowMult, reverbMidMult, reverbHighMult,
                          RVERB_DAMP_LO_HZ, RVERB_DAMP_HI_HZ)
              : par(i, 2, fi.peak_eq_cq(RVERB_EQ_LO_GAIN_DB, RVERB_EQ_LO_HZ, 0.7))
              : par(i, 2, fi.high_shelf(RVERB_EQ_HI_GAIN_DB, RVERB_EQ_HI_HZ));

// ----------------------------------------------------------------
// Stereo multi-tap delay FX
// ----------------------------------------------------------------

tapDelayStereo(delayMs, gain, pan, mod) =
    de.fdelay4(1048576, delaySamples(delayMs) + mod) : *(gain) <: *(panL(pan)), *(panR(pan));

superTapDelayProc =
    _ <: tapDelayStereo(tap1Ms, tap1g, tap1Pan, mod1),
         tapDelayStereo(tap2Ms, tap2g, tap2Pan, mod2),
         tapDelayStereo(tap1Ms * 2.0, tap1g * feedbackGain, feedbackPan1, mod1),
         tapDelayStereo(tap2Ms * 2.0, tap2g * feedbackGain, feedbackPan2, mod2)
      :> _,_
with {
    tap1Ms = beatMs(SUPERTAP_BPM) * SUPERTAP_TAP1_BEATS;
    tap2Ms = beatMs(SUPERTAP_BPM) * SUPERTAP_TAP2_BEATS * (1.0 + 0.04 * SUPERTAP_OFFSET);
    tap1g  = db2lin(SUPERTAP_TAP1_DB);
    tap2g  = db2lin(SUPERTAP_TAP2_DB);
    feedbackGain = SUPERTAP_FEEDBACK * mix(1.0, SUPERTAP_CROSS_FB, float(SUPERTAP_PINGPONG != 0));
    modRate = mix(SUPERTAP_MOD_RATE_HZ, SUPERTAP_BPM / 480.0, SUPERTAP_MOD_SYNC);
    modDepthSamples = SUPERTAP_MOD_DEPTH_MS * 0.001 * ma.SR;
    mod1 = modDepthSamples * os.oscrs(modRate);
    mod2 = modDepthSamples * os.oscrc(modRate);
    tap1Pan = select2(SUPERTAP_PINGPONG, SUPERTAP_TAP1_PAN, 0.0 - abs(SUPERTAP_TAP1_PAN));
    tap2Pan = select2(SUPERTAP_PINGPONG, SUPERTAP_TAP2_PAN, abs(SUPERTAP_TAP2_PAN));
    feedbackPan1 = mix(tap1Pan, tap2Pan, float(SUPERTAP_PINGPONG != 0));
    feedbackPan2 = mix(tap2Pan, tap1Pan, float(SUPERTAP_PINGPONG != 0));
};

// ----------------------------------------------------------------
// Path builders
// ----------------------------------------------------------------

groupSource(x) = x * db2lin(GROUP_GAIN_DB);
groupMono(x) = groupSource(x) * macroGain(groupSource(x));
groupSend(x, sendAmount, sendDb) = groupMono(x) * sendAmount * db2lin(sendDb) * macroSend(groupSource(x));

dryPath(x) = groupMono(x) : panStereo;

shimmerPath(x) = groupSend(x, SHIMMER_SEND, SHIMMER_SEND_PRE_DB)
              : panStereo
              : shimmerFx
              : stereoGain(db2lin(SHIMMER_GAIN_DB + mix(-24.0, 0.0, SHIMMER_MIX / 100.0)));

earlyRefPath(x) = groupSend(x, RVERB_SEND, RVERB_SEND_PRE_DB)
               : panStereo
               : stereoPredelay(RVERB_PREDELAY_MS * 0.35)
               : stereoGain(db2lin(RVERB_EARLY_REF_DB - 12.0));

reverbPath(x) = groupSend(x, RVERB_SEND, RVERB_SEND_PRE_DB)
             : panStereo
             : stereoReverbFx
             : stereoGain(db2lin(RVERB_GAIN_DB + RVERB_REVERB_DB + mix(-24.0, 0.0, RVERB_WET_DRY / 100.0) - 6.0));

superTapDirectPath(x) = outL, outR
with {
    rotPan = SUPERTAP_DIRECT_ROTATE;
    gain   = db2lin(SUPERTAP_DIRECT_GAIN_DB);
    outL   = select2(SUPERTAP_DIRECT_ON, 0.0, x * panL(rotPan) * gain);
    outR   = select2(SUPERTAP_DIRECT_ON, 0.0, x * panR(rotPan) * gain);
};

delayWet(x) = groupSend(x, SUPERTAP_SEND, SUPERTAP_SEND_PRE_DB)
           : superTapDelayProc
           : par(i, 2, fi.lowpass(1, SUPERTAP_COLOR_HZ))
           : superTapEq;

delayDirect(x) = groupSend(x, SUPERTAP_SEND, SUPERTAP_SEND_PRE_DB)
              : superTapDirectPath;

delayPath(x) = x <: delayWet, delayDirect
            :> _,_
            : stereoGain(db2lin(SUPERTAP_GAIN_DB + mix(-24.0, 0.0, SUPERTAP_MIX / 100.0)));

// ----------------------------------------------------------------
// Main rack
// ----------------------------------------------------------------

process = _ <: dryPath, earlyRefPath, reverbPath, shimmerPath, delayPath
        :> _,_
        : stereoGain(db2lin(OUTPUT_GAIN_DB));
