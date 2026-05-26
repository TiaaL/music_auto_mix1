// ================================================================
// Vocal + Accompaniment Mixer
// ================================================================
// Mixing workflow with FX send/return bus.
//
// Signal flow:
//
//   vocal (mono) ──→ gain ──→ pan (mono→stereo) ─────────────────→ ┐
//                          ↘ ×fxSendVocal ─────→ FX bus ─────────→ │ master
//   accomp (stereo) → gain ────────────────────────────────────────→ │  L/R
//                          ↘ ×fxSendAccomp ────→ FX bus           │
//                                                 FX bus → reverb → ┘
//
// Inputs:  3 channels — [0] vocal (mono), [1] accomp L, [2] accomp R
// Outputs: 2 channels — master L, master R
//
// Preparing a 3-channel input file with sox:
//   sox -M vocal.wav accomp.wav combined.wav
//
// ================================================================

declare name        "Vocal + Accompaniment Mixer";
declare version     "1.0";
declare description "Post-fader FX send, mono vocal pan, stereo master";

import("stdfaust.lib");

// ----------------------------------------------------------------
// Parameters
// ----------------------------------------------------------------

VOCAL_GAIN_DB    = 0.0;    // dB: vocal channel fader
VOCAL_PAN        = 0.0;    // -1=left … 0=center … +1=right
ACCOMP_GAIN_DB   = 0.0;    // dB: accompaniment channel fader
FX_SEND_VOCAL    = 0.3;    // 0–1: post-fader send amount (vocal → FX bus)
FX_SEND_ACCOMP   = 0.2;    // 0–1: post-fader send amount (accomp → FX bus)
FX_RETURN_DB     = -6.0;   // dB: FX return level (wet signal into master)
MASTER_GAIN_DB   = 0.0;    // dB: master output fader

// Reverb character (freeverb model)
REV_ROOM         = 0.85;   // 0–1: room size / tail length
REV_DAMP         = 0.40;   // 0–1: high-frequency damping
REV_SPREAD       = 23;     // samples: stereo decorrelation offset

// ----------------------------------------------------------------
// Utilities
// ----------------------------------------------------------------

db2lin(x) = pow(10.0, x / 20.0);

// ----------------------------------------------------------------
// FX processor: stereo freeverb
// ----------------------------------------------------------------
// 2 in → 2 out  (no dry signal, pure wet)

reverb = re.stereo_freeverb(REV_ROOM, REV_ROOM, REV_DAMP, REV_SPREAD);

// ----------------------------------------------------------------
// Step 1 — Channel gain staging
// ----------------------------------------------------------------
// (vocal, accompL, accompR)  →  (vg, aLg, aRg)

applyGains(vocal, aL, aR) =
    vocal * db2lin(VOCAL_GAIN_DB),
    aL    * db2lin(ACCOMP_GAIN_DB),
    aR    * db2lin(ACCOMP_GAIN_DB);

// ----------------------------------------------------------------
// Step 2 — Vocal panning + FX send bus
// ----------------------------------------------------------------
// Constant-power pan law: maps [-1, +1] → angle in [0, π/2]
//   center (0): L = R = 1/√2 (-3 dBFS each)
//   full left:  L = 1, R = 0
//   full right: L = 0, R = 1
//
// FX sends are post-fader (tapped after gain staging).
//
// (vg, aLg, aRg)  →  (vgL, vgR, aLg, aRg, fxInL, fxInR)

panAndRoute(vg, aLg, aRg) = vgL, vgR, aLg, aRg, fxInL, fxInR
with {
    panAngle = (VOCAL_PAN + 1.0) * ma.PI / 4.0;  // [-1,1] → [0, π/2]
    vgL      = vg  * cos(panAngle);               // vocal group L (panned)
    vgR      = vg  * sin(panAngle);               // vocal group R (panned)
    fxInL    = vg  * FX_SEND_VOCAL + aLg * FX_SEND_ACCOMP;  // FX bus L
    fxInR    = vg  * FX_SEND_VOCAL + aRg * FX_SEND_ACCOMP;  // FX bus R
};

// ----------------------------------------------------------------
// Step 3 — FX processing (reverb)
// ----------------------------------------------------------------
// Passes dry group signals through while routing the FX bus
// into the reverb.  Single reverb instance, no duplication.
//
// (vgL, vgR, aLg, aRg, fxInL, fxInR)
//   →  (vgL, vgR, aLg, aRg, fxOutL, fxOutR)

processReverb(vgL, vgR, aLg, aRg, fxInL, fxInR) =
    vgL, vgR, aLg, aRg, ((fxInL, fxInR) : reverb);

// ----------------------------------------------------------------
// Step 4 — Group summing + master gain
// ----------------------------------------------------------------
// Sums vocal group + accomp group + FX return into stereo master.
// Note: vocal panning means vgL ≠ vgR, but aLg/aRg preserve the
// original stereo image of the accompaniment.
//
// (vgL, vgR, aLg, aRg, fxOutL, fxOutR)  →  (masterL, masterR)

sumToMaster(vgL, vgR, aLg, aRg, fxL, fxR) = masterL, masterR
with {
    retGain = db2lin(FX_RETURN_DB);
    mGain   = db2lin(MASTER_GAIN_DB);
    masterL = (vgL + aLg + fxL * retGain) * mGain;
    masterR = (vgR + aRg + fxR * retGain) * mGain;
};

// ----------------------------------------------------------------
// Main process
// ----------------------------------------------------------------
// 3 in:  [0] vocal (mono), [1] accomp L, [2] accomp R
// 2 out: master L, master R

process = applyGains : panAndRoute : processReverb : sumToMaster;
