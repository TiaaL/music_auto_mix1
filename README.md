# music-auto-mix

Faust DSP approximations of classic Waves/FabFilter plugins, wired into a Python rule engine and shell workflow for automated vocal/accompaniment mixing.

The system has two entry points:

| Entry point | What it does |
|---|---|
| `auto_template_mix.py` | Full pipeline: analyze → select Cubase template A/B/C → residual EQ → render |
| `auto_volume_mix.py` | Volume/dynamics only, no FX — useful as a standalone pre-step |

---

## Requirements

### Toolchain

| Tool | Purpose |
|---|---|
| [Faust](https://faust.grame.fr) | Compile `.dsp` → C++ |
| `g++` / `clang++` | Compile C++ → native binary |
| `make` | Orchestrate builds |
| `ffmpeg` + `ffprobe` | Audio I/O, volume analysis, mix down |
| `python3` (≥ 3.10) | Automation scripts and rule engine |
| `libsndfile` | Required by compiled Faust binaries |

### Python packages

```
librosa numpy scipy soundfile
```

### Quick check

```bash
./scripts/check_env.sh
```

### Windows / MSYS2 local toolchain

The project ships a self-contained MSYS2 environment under `.tools/msys64` and a
Windows Faust binary under `.tools/faust`. Activate it once per shell session:

```bash
source scripts/msys_template_env.sh
```

Then build normally with `make`.

---

## Build

```bash
make          # build all processors → build/
make clean    # remove build artifacts
make svg      # generate signal flow diagrams
make test     # run L2 limiter correctness tests
make smoke    # end-to-end smoke tests with synthetic audio
```

Shell scripts auto-build any missing binary via `make` before running.

---

## Main workflows

### 1 — Auto template mix (recommended)

Runs the full pipeline in one command:

```bash
python3 scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav
```

Steps performed internally:

1. **Spectral analysis** — external `spectrum_template_analyzer.py` classifies the vocal
2. **Template selection** — maps classification label → template A, B, or C
3. **Mix plan** — `plan_mix_template.py` builds a residual EQ plan (see [Strategy layer](#strategy-layer))
4. **Render** — `render_template_mix.sh` runs the template DSP chain, applies residual EQ, and runs the master bus

Optional flags:

```bash
# Run volume automation before the template DSP chain
python3 scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --with-volume-automation

# Skip final LUFS/true-peak normalization
python3 scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --no-loudness-finalizer

# Write analysis, plan, and summary JSON to a specific directory
python3 scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --report-dir reports/
```

The analyzer script location defaults to
`D:\code\spectral-mix-template-selector\spectrum_template_analyzer.py`.
Override with `--analyzer <path>`.

---

### 2 — Template render directly (skip analysis)

```bash
./scripts/render_template_mix.sh template_a vocal.wav accomp.wav final_mix.wav
./scripts/render_template_mix.sh template_b vocal.wav accomp.wav final_mix.wav
./scripts/render_template_mix.sh template_c vocal.wav accomp.wav final_mix.wav
```

Template vocal plugin chains:

| Template | Vocal insert chain |
|---|---|
| `template_a` | `c1_gate → template_a_vocal_proq3 → c1_comp → sibilance_mono` |
| `template_b` | `rbass_mono → f6_rta_mono → c1_comp → sibilance_mono → l1_limiter_mono` |
| `template_c` | `template_c_vocal_proq3 → vocal_rider_mono → c1_comp → oneknob_brighter_mono` |

All templates share:

```
vocal insert chain
  → [step 1b] residual vocal EQ (from mix plan, if --mix-plan is passed)
  → vocal_group_fx
  → [accompaniment] template_music_proq3_{ab|c}
  → amix stereo sum
  → template_bus_proq3_{ab|c} → gw_mixcentric_stereo → master_l2_stereo
  → loudness finalizer (master_loudness_finalize.py)
```

Add `--with-volume-automation` to run `auto_volume_mix.py` before the plugin chain.

---

### 3 — Volume automation only

Adjusts vocal and accompaniment levels without applying any FX:

```bash
python3 scripts/auto_volume_mix.py vocal.wav accomp.wav \
  --vocal-out /tmp/vo_proc.wav \
  --accomp-out /tmp/bc_proc.wav
```

Optionally produce a stereo mix and a balance audit report:

```bash
python3 scripts/auto_volume_mix.py vocal.wav accomp.wav final_mix.wav \
  --vocal-out /tmp/vo_proc.wav \
  --accomp-out /tmp/bc_proc.wav \
  --balance-report reports/balance.json
```

**What it does**

- Silence-detection segments the vocal track
- Per-segment gain is computed toward a −18 dBFS RMS target (gains capped at 0 dB — no positive boost)
- Gain changes are applied via a single continuous FFmpeg `volume` expression (no `atrim`/`concat`), avoiding segment-boundary amplitude dips
- Accompaniment is kept below the vocal during voiced sections; intro/outro stay at base gain
- A second-pass balance check trims either track if the vocal/accompaniment gap is outside limits

Key limits (`config/bc_vo_mix_rules.json`):

| Setting | Value | Meaning |
|---|---|---|
| `vocal.gain_max_db` | `0.0` | No positive boost — only attenuation |
| `vocal.gain_min_db` | `−3.0` | Maximum attenuation per segment |
| `accompaniment.gain_max_db` | `0.0` | Accompaniment never boosted |
| `accompaniment.gain_min_db` | `−3.0` | Maximum accompaniment attenuation |

---

### 4 — Legacy FX mix (no template)

```bash
./scripts/full_fx_mix.sh vocal.wav accomp.wav final_mix.wav
```

Uses the older default chain without Cubase template approximations:

```
volume automation → rdeesser → req6 → c1_comp → vocal_group_fx
                 → accomp_proq3 → accomp_l2_stereo
                 → stereo mix
```

---

## Strategy layer

`plan_mix_template.py` builds a per-session plan with two independent stacks:

### Stack 1 — Residual vocal EQ (driven by classifier)

After the selected template processes the vocal insert chain, `apply_residual_vocal_eq.py` applies up to 4 corrective EQ bands (FFmpeg `equalizer` filters).

The plan merges three action sources; within each band the strongest gain wins:

| Priority | Source | Trigger condition |
|---|---|---|
| 1 | `classification_hits` | Classifier rule fired for the selected template label |
| 2 | `spectral_deviation` | Band deviates ≥ 1.5 dB from the target spectral curve and is **not** covered by the template |
| 3 | `ratio_excess` | Band ratio exceeds the neutral range by ≥ 0.04 and is **not** covered by the template |
| 4 | `covered_strong_hits` | Strong hit in a band the template already covers → small reinforcement (gain × 0.5, capped at 1.2 dB) |

Template coverage (which bands each template handles, leaving others for residual EQ):

| Template | Covered bands |
|---|---|
| `template_a` | sub, low, lowmid, mid, nasal |
| `template_b` | upper, harsh, sib, nasal |
| `template_c` | lowmid, mid, upper, air |

Configuration lives in `config/residual_vocal_eq_rules.json`.

### Stack 2 — Reference-driven overrides (per-song reference audio)

When a reference full-mix, vocal stem, and accompaniment stem are provided (auto-resolved by song name from `downloads/feishu_long_audio_screened/{原曲,原曲人声,伴奏}/`, or explicit via `--reference-audio` / `--reference-vocal` / `--reference-accomp`), `analyze_reference.py` extracts features and `plan_mix_template.build_reference_overrides` translates them into:

- **`bus_balance`** — `volume=` filters on each bus inside the amix call. Matches the vocal-vs-accomp LUFS gap to the reference. **Bus gain ≤ 0 always** (memory rule: no positive gain on either bus); if more vocal is needed, accompaniment is cut instead.
- **`master_tilt_eq`** — up to 4 EQ moves between amix and master Pro-Q3, applied by `apply_master_tilt_eq.py`. Pushes the mix's 8-band tonal shape toward the reference's.

Master tilt safety rules (in `plan_mix_template.MASTER_TILT_*`):

| Constant | Value | Why |
|---|---|---|
| `MASTER_TILT_DEAD_BAND_DB` | 1.5 | Ignore deltas smaller than this — avoid pointless tweaks |
| `MASTER_TILT_MAX_CUT_DB` | 3.0 | Cuts can go up to 3 dB |
| `MASTER_TILT_MAX_BOOST_DB` | 0.8 | Boosts are tightly capped — master-bus boosts amplify all sources at once |
| `MASTER_TILT_MAX_ACTIONS` | 4 | Take only the 4 worst deltas |
| `harsh` (6.2 kHz), `sib` (9.5 kHz) | **cut-only** | Boosting these on a complete mix amplifies sibilance and cymbal hash. Brightness deficit must be accepted, not boosted. |

Reverb characteristics from the reference (`reverb_proxy`) and dynamics are recorded for diagnostics but **not yet applied** — the Faust reverb is a fixed binary. A future reverb-preset library will pick the closest preset to the reference instead of parameterizing live.

---

## Processors

### Vocal chain (mono input)

| Binary | Source | Plugin model |
|---|---|---|
| `c1_gate` | `src/c1_gate.dsp` | Waves C1 Gate |
| `rdeesser` | `src/rdeesser.dsp` | Waves Renaissance DeEsser |
| `req6` | `src/req6.dsp` | Waves REQ6 Renaissance EQ (6-band) |
| `c1_comp` | `src/c1_comp.dsp` | Waves C1 Compressor |
| `sibilance_mono` | `src/sibilance_mono.dsp` | Waves Sibilance (mono) |
| `f6_rta_mono` | `src/f6_rta_mono.dsp` | Waves F6 RTA (mono dynamic EQ) |
| `l1_limiter_mono` | `src/l1_limiter_mono.dsp` | Waves L1 Limiter (mono) |
| `rbass_mono` | `src/rbass_mono.dsp` | Waves RBass (mono) |
| `vocal_rider_mono` | `src/vocal_rider_mono.dsp` | Waves Vocal Rider (mono) |
| `oneknob_brighter_mono` | `src/oneknob_brighter_mono.dsp` | Waves OneKnob Brighter (mono) |
| `template_a_vocal_proq3` | `src/template_a_vocal_proq3.dsp` | Template A Pro-Q3 snapshot |
| `template_c_vocal_proq3` | `src/template_c_vocal_proq3.dsp` | Template C Pro-Q3 snapshot |

### Vocal group FX (mono → stereo)

| Binary | Source |
|---|---|
| `vocal_group_fx` | `src/vocal_group_fx.dsp` |

Three parallel send buses summed onto the stereo dry path:

| Bus | Model | Send level | Reverb/delay time |
|---|---|---|---|
| Shimmer | Valhalla Shimmer-style | −18 dB | 3.2 s |
| Reverb | RVerb plate-style | −12.5 dB | 1.75 s, 12 ms predelay, DAMP 0.35 |
| Delay | SuperTap 2-tap stereo | −27 dB | tempo-synced at 89 BPM |

The reverb send is intentionally clean (not dry, not washy): short tail, pre-delay
separates dry vocal from reverb onset.

### Accompaniment + master bus (stereo)

| Binary | Source | Purpose |
|---|---|---|
| `template_music_proq3_ab` | `src/template_music_proq3_ab.dsp` | Template A/B accompaniment EQ |
| `template_music_proq3_c` | `src/template_music_proq3_c.dsp` | Template C accompaniment EQ |
| `template_bus_proq3_ab` | `src/template_bus_proq3_ab.dsp` | Template A/B master bus EQ |
| `template_bus_proq3_c` | `src/template_bus_proq3_c.dsp` | Template C master bus EQ |
| `gw_mixcentric_stereo` | `src/gw_mixcentric_stereo.dsp` | GW MixCentric stereo bus processor |
| `master_l2_stereo` | `src/master_l2_stereo.dsp` | Waves L2 Stereo style limiter |
| `accomp_proq3` | `src/accomp_proq3.dsp` | Legacy accompaniment EQ (full_fx_mix.sh) |
| `accomp_l2_stereo` | `src/accomp_l2_stereo.dsp` | Legacy accompaniment limiter |
| `accomp_c6_sc` | `src/accomp_c6_sc.dsp` | Waves C6 SC approximation — research only, disabled in all runtime chains |

### Compression overview (where dynamics control happens)

| Stage | Compressor / limiter | Notes |
|---|---|---|
| Vocal — all templates | `c1_comp` (Waves C1 model) | Sits inside every template's vocal insert chain |
| Vocal — template B only | `l1_limiter_mono` | Tail limiter for the harsh-vocal template |
| Vocal — all templates | `sibilance_mono` | De-essing on harsh/sib bands |
| Accompaniment | **gap** — Cubase template specifies `C6-SideChain Stereo` (Faust binary `accomp_c6_sc` exists in `src/`) but is currently bypassed in `render_template_mix.sh` (step 2 only runs the Pro-Q 3 stage and copies into the accomp bus). Enabling it is a planned change. |
| Master bus | `gw_mixcentric_stereo` provides the glue compression / saturation (Greg Wells MixCentric is an all-in-one EQ + comp + saturation processor); `master_l2_stereo` is the peak limiter on top; `master_loudness_finalize.py` then handles LUFS / true-peak normalization |

### Loudness defaults (`master_loudness_finalize.py`)

| Flag | Default | Effect |
|---|---|---|
| `--target-i` | `-10.0` LUFS | Replaced by reference LUFS when `--reference-audio` is passed |
| `--target-tp` | `-0.8` dBTP | True-peak ceiling for the limiter |
| `--target-lra` | `11.0` LU | Loudness range target (gets clamped against the reference's LRA when provided) |
| `--max-pre-gain-db` | `9.0` | Cap on the pre-limiter integrated-gain push |
| `--max-attenuation-db` | `12.0` | Cap on attenuation when the input is hotter than the target |
| `--max-residual-gain-db` | `3.5` | Cap on the post-limiter touch-up gain — bumped from 2.0 so the finalizer can recover loudness when the L2 peak ceiling clamps the pre-limiter push |
| `--max-true-peak-limiter-reduction-db` | `4.0` | Caps how hard the limiter is allowed to work while chasing LUFS; if the song is too peaky, finalizer preserves peak safety instead of creating crackle |

---

## Configuration files

| File | Purpose |
|---|---|
| `config/bc_vo_mix_rules.json` | Vocal/accompaniment gain limits and compressor parameters for `auto_volume_mix.py` |
| `config/residual_vocal_eq_rules.json` | Residual EQ rules, band parameters, coverage map, and strategy thresholds |
| `config/template_feature_targets.json` | Neutral ratio ranges, spectral target curve, template objectives |
| `config/daw_calibration_stages.json` | Stage-to-Faust-binary mapping and DAW reference file locations |
| `config/plugin_mapping.json` | Plugin name → DSP binary mapping |
| `config/cubase_templates/` | Per-template plugin chain definitions (JSON) |
| `config/extracted_vstpresets/` | Decoded VST preset parameters (JSON) |

### Editing vocal gain limits

`config/bc_vo_mix_rules.json` — the most commonly tuned file:

```json
{
  "vocal": {
    "target_db": -18.0,
    "gain_min_db": -3.0,
    "gain_max_db": 0.0
  },
  "accompaniment": {
    "base_gain_db": 0.0,
    "gain_min_db": -3.0,
    "gain_max_db": 0.0
  }
}
```

`gain_max_db` must stay at `0.0` for both channels. Positive gain causes sudden vocal jumps and is prohibited by the mixing rules.

---

## DAW reference calibration

Cubase reference exports live under `D:/cubase/project/ai_cover/Mixdown/mix_results`.
Stage-to-binary mappings are in `config/daw_calibration_stages.json`.

Volume balance reference (raw, pre-fader levels from `通用混音模板_纯净版.cpr`):

| Track | Mean | Peak |
|---|---|---|
| Vocal (`ACAPELLA-勇气hbc.wav`) | −16.8 dB | −0.9 dB |
| Accompaniment (`勇气伴奏1.wav`) | −17.5 dB | 0.0 dB |
| Gap (vocal − accomp) | +0.7 dB | — |

The CPR file is binary; exact fader positions are not directly readable. Export processed stems from Cubase to get post-fader reference levels.

Generate Faust stage outputs and compare against DAW references:

```bash
./scripts/render_calibration_stages.sh \
  --vocal-in vocal.wav \
  --music-in accomp.wav \
  --bus-in rough_mix.wav \
  --out-dir calibration_outputs/faust_stages

python3 scripts/daw_reference_compare.py \
  --candidate-root calibration_outputs/faust_stages \
  --out-dir calibration_outputs/daw_reference_compare
```

The report shows peak/RMS deltas, correlation, banded spectral error, and a match score per stage.

---

## Audit tools

### Vocal balance audit

```bash
python3 scripts/audit_vocal_balance.py \
  --vocal-wav /tmp/vo_proc.wav \
  --accomp-wav /tmp/bc_proc.wav \
  --out-dir reports/
```

### Template feature audit (before vs. after template chain)

```bash
python3 scripts/audit_template_vocal_features.py \
  --analysis-json reports/analysis.json \
  --processed-vocal /tmp/vocal_after_template.wav \
  --out-dir reports/audit/
```

Outputs `vocal_feature_audit.json` and `vocal_feature_audit.md` with ratio deltas,
band balance status, spectral deviation, and tuning suggestions per template objective.

### Resolve and inspect the mix plan

```bash
python3 scripts/plan_mix_template.py analysis.json --output resolved_plan.json
```

---

## Modifying DSP parameters

All parameters are constants at the top of each `.dsp` file. Edit and rebuild:

```bash
# Example: tighten the reverb tail
# Edit RVERB_TIME_S in src/vocal_group_fx.dsp, then:
make build/vocal_group_fx
```

On Windows with the local toolchain:

```bash
source scripts/msys_template_env.sh
make build/vocal_group_fx
```

---

## Architecture

| Layer | Files | Purpose |
|---|---|---|
| DSP | `src/*.dsp` | Plugin approximations compiled to native binaries |
| Volume automation | `scripts/auto_volume_mix.py` | Rule-based level control; single-pass FFmpeg volume expression |
| Template strategy | `scripts/plan_mix_template.py` | Spectral analysis → template → residual EQ plan |
| Residual EQ | `scripts/apply_residual_vocal_eq.py` | Apply plan-driven EQ between template chain and group FX |
| Render orchestration | `scripts/render_template_mix.sh` | Runs the full DSP pipeline in order |
| Full auto pipeline | `scripts/auto_template_mix.py` | End-to-end: analyze → plan → render → report |
| Calibration | `scripts/daw_reference_compare.py` | Faust stage vs. Cubase reference comparison |

Signal flow for template A/B/C:

```
vocal.wav ──[optional: auto_volume_mix]──► volume-automated vocal
                                              │
                                    template vocal insert chain (incl. C1 compressor)
                                              │
                                    residual vocal EQ (plan-driven, classifier-source)
                                              │
                                       vocal_group_fx (stereo, reverb + shimmer + delay)
                                              │
                                  [step 3a] bus volume (reference-driven, ≤ 0 dB only)
                                              │
accomp.wav ──[optional: auto_volume_mix]──► template music EQ ── [step 3a] bus volume ──┐
                                                                                          ▼
                                                                              amix stereo sum
                                                                                          │
                                                          [step 3b] master tilt EQ (reference-driven)
                                                                                          │
                                                          template bus EQ → GW MixCentric → L2
                                                                                          │
                                                              loudness finalizer (LUFS target = ref)
                                                                                          │
                                                                                  final_mix.wav
```

Reference-driven stages activate when the input vocal's song name resolves to a reference triplet under `downloads/feishu_long_audio_screened/{原曲,原曲人声,伴奏}/`, or when explicit `--reference-*` flags are passed. Without a reference, the bus volumes stay at 0 dB and master tilt EQ does nothing — the rest of the chain is unaffected.
