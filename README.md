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
| project Python (≥ 3.10) | Automation scripts and rule engine; scripts prefer `.venv/bin/python` when present |
| `libsndfile` | Required by compiled Faust binaries |

### Python packages

```
librosa numpy scipy soundfile openpyxl
```

`openpyxl` is only needed by Feishu sheet download helpers. The render path uses the
project-local Python environment; avoid installing packages into an unrelated system
Python when debugging this repo.

### Quick check

```bash
./scripts/check_env.sh
```

This checks the Faust binary, FFmpeg tools, project Python, and the Python packages used by the scripts.

### macOS local Faust

If Homebrew `faust` is unavailable, the repo can use a source-built Faust under
`.tools/faust-local/`. Activate once per shell session:

```bash
source scripts/mac_faust_env.sh
make
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
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav
```

Steps performed internally:

1. **Spectral analysis** — external `spectrum_template_analyzer.py` classifies the vocal
2. **Template selection** — maps classification label → template A, B, or C
3. **Mix plan** — `plan_mix_template.py` builds residual EQ, reference overrides, and dry-vocal strategy (see [Strategy layer](#strategy-layer))
4. **Render** — `render_template_mix.sh` normalizes input format, runs the template DSP chain, applies vocal-aware accompaniment yielding, and runs the master bus

Optional flags:

```bash
# Run volume automation before the template DSP chain
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --with-volume-automation

# Skip final LUFS/true-peak normalization
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --no-loudness-finalizer

# Write analysis, plan, and summary JSON to a specific directory
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --report-dir reports/

# Point at a reference full mix (drives stem balance, source EQ, master tilt, loudness target)
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --reference-audio "ref/原曲.mp3" \
  --reference-root "ref_dataset/"
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
./scripts/render_template_mix.sh template_d vocal.wav accomp.wav final_mix.wav
```

Template vocal plugin chains:

| Template | Vocal insert chain |
|---|---|
| `template_a` | `c1_gate → template_a_vocal_proq3 → c1_comp → sibilance_mono` |
| `template_b` | `rbass_mono → f6_rta_mono → c1_comp → sibilance_mono → l1_limiter_mono` |
| `template_c` | `template_c_vocal_proq3 → vocal_rider_mono → c1_comp → oneknob_brighter_mono` |
| `template_d` | legacy current chain: `rdeesser → req6 → c1_comp → vocal_group_fx` |

Templates A/B/C share:

```
vocal insert chain
  → [step 1b] residual vocal EQ (from mix plan, if --mix-plan is passed)
  → [step 1c] reference vocal source EQ (plan-driven, optional)
  → vocal_group_fx
accompaniment
  → template_music_proq3_{ab|c}
  → [step 2b] reference accomp carve EQ (plan-driven, optional)
  → [step 2c] vocal-aware multiband accompaniment ducking (template + dry-vocal strategy)
  → [step 3a] bus balance (render-time, see below)
  → amix stereo sum
  → [step 3b] master tilt EQ (reference-driven, optional)
  → template_bus_proq3_{ab|c} → gw_mixcentric_stereo
  → loudness finalizer (master_loudness_finalize.py; includes L2 + soft true-peak ceiling + global de-click)
```

When `--no-loudness-finalizer` is passed, the render script applies `master_l2_stereo`
once at the end instead of calling `master_loudness_finalize.py`.

Add `--with-volume-automation` to run `auto_volume_mix.py` before the plugin chain.

---

### 3 — Volume automation only

Adjusts vocal and accompaniment levels without applying any FX:

```bash
.venv/bin/python scripts/auto_volume_mix.py vocal.wav accomp.wav \
  --vocal-out /tmp/vo_proc.wav \
  --accomp-out /tmp/bc_proc.wav
```

Optionally produce a stereo mix and a balance audit report:

```bash
.venv/bin/python scripts/auto_volume_mix.py vocal.wav accomp.wav final_mix.wav \
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

When a reference full-mix, vocal stem, and accompaniment stem are provided (auto-resolved by song name from `downloads/feishu_long_audio_screened/{原曲,原曲人声,伴奏}/` or a sibling workspace folder such as `/Users/sly/Desktop/code/music/feishu_long_audio_screened`, or explicit via `--reference-audio` / `--reference-vocal` / `--reference-accomp` / `--reference-root`), `analyze_reference.py` extracts features and `plan_mix_template.build_reference_overrides` translates them into:

- **Reference/input feature cache is safety-keyed.** `auto_template_mix.py` stores feature JSON under `calibration_outputs/cache/features/`; the cache key includes the input file path/size/mtime plus the analysis code signatures, so changed audio or analyzer logic invalidates old feature data.
- **`bus_balance`** — the original song's active vocal/accompaniment ratio. The plan stores the reference gap; **gains are computed at render time** by `compute_render_bus_balance.py` (step 3a), after `vocal_group_fx` and the accompaniment chain have run. The correction is conservative: when the vocal is behind the reference, it splits the move between a limited vocal lift and a limited accompaniment cut; it does not independently chase each stem's LUFS.
- **`source_eq.vocal_eq`** — source EQ moves after the selected vocal template chain, based on the current dry vocal's active-region tonal shape vs. the original-song vocal stem. Upper/air boosts are evidence-gated by template and sibilance/harshness safety; 14 kHz air is conservative and is never a default lift.
- **`source_eq.accomp_eq`** — cut-only accompaniment carve EQ after the music template EQ, focused on bands where the current accompaniment masks the current vocal and the vocal sits behind the reference balance. One problem region should only be carved once, and carve decisions are coordinated with dynamic ducking so the same upper/mid issue is not cut twice.
- **`dry_vocal_strategy`** — current dry-vocal tags and a ducking profile. Low-mid-heavy, dark, or presence-masked vocals ask the accompaniment to yield more in body/presence/air bands while voiced sections are active.
- **`master_tilt_eq`** — up to 4 EQ moves between amix and master Pro-Q3, applied by `apply_master_tilt_eq.py`. Pushes the mix's 8-band tonal shape toward the reference's.

Master tilt safety rules (in `plan_mix_template.MASTER_TILT_*`):

| Constant | Value | Why |
|---|---|---|
| `MASTER_TILT_DEAD_BAND_DB` | 1.5 | Ignore deltas smaller than this — avoid pointless tweaks |
| `MASTER_TILT_MAX_CUT_DB` | 3.0 | Cuts can go up to 3 dB |
| `MASTER_TILT_MAX_BOOST_DB` | 0.8 | Boosts are tightly capped — master-bus boosts amplify all sources at once |
| `MASTER_TILT_MAX_ACTIONS` | 4 | Take only the 4 worst deltas |
| `harsh` (6.2 kHz), `sib` (9.5 kHz) | **cut-only** | Boosting these on a complete mix amplifies sibilance and cymbal hash. Brightness deficit must be accepted, not boosted. |

Reverb characteristics from the reference (`reverb_proxy`) and dynamics are recorded for diagnostics but **not yet applied** — spatial effects still come from the built-in `vocal_group_fx.dsp` sends (Shimmer / RVerb / SuperTap). External `delayverb` is not wired into this pipeline yet.

---

## Current plan constraints

Recent tuning focuses on plan quality rather than fader or master-loudness changes.
Do not use volume balance or loudness as a substitute for better source decisions:

- **Do not touch volume balance for tone fixes.** `auto_volume_mix.py` and `compute_render_bus_balance.py` own level relationship. Source EQ, carve, and ducking should make space without rewriting the vocal/accompaniment ratio.
- **Do not touch master loudness for masking fixes.** `master_loudness_finalize.py` stays master-bus only. If a render is too quiet because earlier EQ/ducking removed too much energy, fix the earlier tonal decision rather than adding more master gain.
- **Do not duplicate the same accompaniment treatment.** `source_eq.accomp_eq` may statically carve a spectral problem region, and `apply_accomp_vocal_duck.py` may dynamically duck while the vocal is active. If carve already handles `presence`/`body`, the duck profile is reduced for that same region.
- **Do not default to air boosts.** 14 kHz air boosts are only allowed when upper/air is genuinely deficient and harsh/sibilance evidence is safe. Template B may use stronger upper recovery when evidence supports it; air remains conservative.
- **Prefer dry-vocal profile evidence.** Thick, muffled, nasal, thin, harsh, sibilant, and dynamically weak vocals should become explicit strategy tags and plan evidence, not ad hoc EQ moves.

High-frequency boost evidence currently checks:

| Evidence | Purpose |
|---|---|
| `harsh` and `sib` ratios | Avoid adding brightness when harshness or sibilance is already high |
| `peakiness_harsh` / `peakiness_upper` | Avoid boosting sharp upper-band spikes |
| active-region vocal tonal deltas | Confirm `upper` or `air` is actually below the reference vocal |
| selected template | Template B can recover more upper presence than A/C when the evidence is safe |

The resolved plan records skipped high-frequency boosts under `source_eq.vocal_eq.skipped`
or `residual_vocal_eq.suppressed_high_boosts`, so a rejected boost is auditable instead
of silently disappearing.

---

## Dry-vocal strategy and accompaniment yielding

The mix plan now uses the dry vocal as a strategy input, not only as something to EQ.
This is aimed at cases where the accompaniment is technically at the right loudness but
still masks the vocal, especially after dense sections such as the last chorus.

### What is measured

`plan_mix_template.py` reads the dry-vocal feature ratios and strong classifier rules:

- low / low-mid / body / presence / harsh / air ratios
- body-to-presence ratio
- peaky upper or harsh bands
- template classification label and strong rule hits

It writes `reference.overrides.dry_vocal_strategy` into the resolved mix plan. Typical tags:

| Tag | Meaning |
|---|---|
| `lowmid_body_heavy` | Vocal energy is concentrated around low-mid/body, so accompaniment body can mask words |
| `presence_masked_by_body` | Vocal presence is weak compared with body |
| `dark_or_muffled_dry_vocal` | Vocal needs more room in presence/air instead of just more level |
| `dry_vocal_low_pressure` | Sub/low energy can overload the later mix |
| `peaky_presence` | Avoid over-opening the presence band because the vocal already has sharp peaks |

### How accompaniment yields

`render_template_mix.sh` applies accompaniment processing in this order:

1. Template music EQ from `template_music_proq3_{ab|c}`.
2. Reference/source carve EQ from `source_eq.accomp_eq`; this is cut-only and only carves masking bands when the active vocal is behind the reference balance. The plan keeps one action per problem region, such as `presence` or `body`.
3. Vocal-aware multiband ducking from `apply_accomp_vocal_duck.py`; this is keyed by the post-FX vocal group, so the accompaniment yields mainly while the singer is active. If carve already handled the same region, ducking is reduced there.
4. Conservative bus balance from `compute_render_bus_balance.py`; this matches the reference active vocal/accompaniment gap, rather than boosting both stems toward their own LUFS targets.

The ducking bands are low (`<180 Hz`), body (`180-1200 Hz`), presence (`1200-5000 Hz`),
and air (`>5000 Hz`). Template A/B/C provide different base profiles, then the dry-vocal
strategy adds small extra cuts where the current vocal needs space.

Output metadata:

| File | Meaning |
|---|---|
| `<output>.accomp_duck.json` | Per-band ducking profile and applied reduction stats |
| `<output>.bus_balance.json` | Active-region reference gap, render-time gap, and final vocal/accomp gains |
| `<output>.loudness.json` | Master loudness, true-peak, and global de-click report |

Important constraint: this layer is for **space making**, not loudness rewriting. Overall
vocal/accompaniment balance still follows the original song's active ratio conservatively,
and final loudness stays on the master bus only.

The plan metadata for accompaniment coordination lives under:

- `source_eq.accomp_eq.actions[*].region`
- `source_eq.accomp_eq.duck_coordination`
- `<output>.accomp_duck.json.profile`
- `<output>.accomp_duck.json.duck_coordination`

---

## Bus balance at render time (`compute_render_bus_balance.py`)

Older versions derived bus `volume=` filters from the mix plan at plan time, or matched
each post-FX bus to its reference stem LUFS independently. That could make the track
technically louder while still losing the original vocal/accompaniment relationship.

The current render path measures the **actual post-FX buses** and matches the reference
song's active vocal/accompaniment gap:

1. Render vocal through insert chain → `vocal_group_fx` → `VOCAL_GROUP`
2. Render accompaniment through music EQ / carve EQ / vocal-aware ducking → `ACCOMP_BUS`
3. Measure active vocal sections on both buses
4. Compare `vocal_minus_accomp_db` with the original song's active reference gap
5. Apply a capped correction inside the `amix` call in step 3

When the vocal is behind, the correction is split between vocal lift and accompaniment
attenuation. Current caps are intentionally small:

| Limit | Value |
|---|---|
| Maximum vocal lift from bus balance | `+3.0 dB` |
| Maximum accompaniment attenuation from bus balance | `-2.0 dB` |
| Maximum total ratio correction | `4.8 dB` |

Example (黄昏 v5, template C): HJF measured around a `-7.2 dB` render gap before bus
balance, reference gap was about `-2.8 dB`, so step 3a applied vocal `+2.65 dB` and
accompaniment `-1.76 dB`. 炳超 similarly applied vocal `+2.76 dB` and accompaniment
`-1.84 dB`. The goal is to restore the reference ratio without large fader moves.

Output metadata: `<output>.bus_balance.json`.

`apply_master_level_staging.py` (pre-master bus staging / window correction) is **no longer
called** — loudness is handled only on the master bus in `master_loudness_finalize.py`.

Normal template renders call `compute_render_bus_balance.py --skip-loudness`. Integrated
LUFS for the isolated vocal/accompaniment buses is audit-only metadata and is not used
to compute the final bus gains; skipping it avoids two full `loudnorm` scans without
changing the rendered balance. Run `compute_render_bus_balance.py` directly without
`--skip-loudness` when that metadata is needed for a manual audit.

---

## Master loudness finalizer (`master_loudness_finalize.py`)

Final loudness is applied **only on the master bus**, after `gw_mixcentric_stereo`.
The design goal is to hit a streaming loudness window without re-touching vocal/accompaniment
balance and without creating digital crackle from harsh peak limiting.

### Target

| Quantity | Value | Notes |
|---|---|---|
| Integrated LUFS (`I`) | **−13.5 … −12.5** | Hard window; reference LUFS is clamped into this range |
| True peak (`TP`) | **−0.8 dBTP** | Ceiling for the soft limiter |
| Reference | `--reference-audio` | Uses the reference track's measured `I` as the render target (clamped to the window above) |

### Processing chain (mode: `master_safe_pregain_l2_controlled_makeup_soft_tp`)

```
MASTER_2 (post MixCentric, typically ~−24 … −26 LUFS)
  → [1] safe master-bus pregain (capped by true-peak headroom and max gain)
  → [2] Faust master_l2_stereo (sample-peak lookahead limiter, ceiling −1.0 dBFS)
  → [3] post-L2 trim when true-peak headroom allows it
  → [4] controlled limiter makeup when the mix is still under target
  → [5] ffmpeg soft true-peak ceiling (176.4 kHz oversample → alimiter → 44.1 kHz)
  → [6] global isolated-click scan (sample interpolation only; no gain/loudness change)
  → [7] true-peak safety trim if the final file measures above the TP ceiling
  → final_mix.wav
```

**Rules that must not be broken:**

- **Master-bus only** — all loudness compensation stays on the master bus; vocal/accompaniment balance remains owned by step 3a.
- **Safe pregain first** — pregain is capped by both `--max-gain-db` and the input true-peak headroom before L2.
- **Controlled makeup only when needed** — post-L2 makeup is split into at most two small passes through the soft true-peak limiter instead of blindly adding master gain.
- **No loudnorm on the output** — the finalizer does not run FFmpeg `loudnorm` dynamic normalization on the rendered file.
- **True-peak safety can cut, not boost** — the final safety trim only attenuates files that still measure above the TP ceiling.
- **No bus staging** — do not push level on individual buses to “make room” for mastering; stem balance stays in step 3a.
- **De-click is not loudness control** — the final global scan only interpolates very short isolated sample spikes and records the touched times in `.loudness.json`.

### Why the limiter was changed (crackle / 爆破音)

Peaky mixes need a large master pregain (often +12 … +14 dB) to reach −11 LUFS. That
creates large **inter-sample peaks** (e.g. +5 dBTP after pregain). An earlier version used:

- Faust L2 (sample peaks only), then
- `alimiter` at **192 kHz** with **5 ms attack / 80 ms release**

That second stage crushed multi-dBTP overshoots very fast and produced audible crackle on
hot vocal phrases (e.g. 2:51, 2:59 in test renders). Waveform analysis showed those
time ranges were clean **before** mastering; discontinuities appeared only after pregain +
limiting.

The current stage [3] keeps oversampling for true-peak safety but uses a **much slower**
limiter: **50 ms attack, 300 ms release** at 176.4 kHz. Stage [2] still uses L2 to catch
sample peaks before the FFmpeg ceiling.

### CLI flags

| Flag | Default | Effect |
|---|---|---|
| `--target-i` | `−13.0` LUFS | Overridden by reference / mix-plan target, then clamped to [−13.5, −12.5] |
| `--target-tp` | `−0.8` dBTP | True-peak ceiling |
| `--target-lra` | `11.0` LU | LRA hint for measurement; clamped against reference LRA when provided |
| `--max-gain-db` | `18.0` | Maximum master pregain before the safe true-peak cap |
| `--max-attenuation-db` | `12.0` | Maximum master attenuation when input is hotter than target |
| `--controlled-limiter-makeup-max-db` | `8.0` | Maximum post-L2 makeup routed through the soft true-peak limiter |
| `--limiter` | `build/master_l2_stereo` | Faust L2 binary (passed by `render_template_mix.sh`) |
| `--reference-audio` | — | Reference full mix; its integrated LUFS becomes the target |
| `--mix-plan` | — | Resolved plan; may supply a clamped `loudness_target.lufs_i` |
| `--no-global-declick` | off | Skip the final isolated-click scan |
| `--declick-threshold` | `0.6` | Residual threshold for global click detection |
| `--max-declick-samples` | `4` | Longest burst treated as an isolated click |
| `--detailed-loudness-report` | off | Also measure EBU R128 section/focus diagnostics; slower |

Metadata is written to `<output>.loudness.json` (`pre_master`, `post_pregain`, `post_l2`,
`post_trim`, `controlled_limiter_makeup`, `post_limiter`, `global_declick`, `final`,
plus focus windows such as `168_182s` when `--detailed-loudness-report` is enabled). The report includes
`needed_gain_db`, `available_gain_db`, `true_peak_safety_trim_db`,
`target_error_db`, and `loudness_under_compensated` so failed loudness recovery is
visible instead of silently producing a quiet render.

### Skip final loudness

```bash
./scripts/render_template_mix.sh template_a vocal.wav accomp.wav out.wav --no-loudness-finalizer
```

Applies `master_l2_stereo` once with no pregain or FFmpeg true-peak stage.

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
| Accompaniment | Template music EQ → reference carve EQ → `apply_accomp_vocal_duck.py` | Multiband, vocal-aware yielding keyed from the post-FX vocal group. `accomp_c6_sc` remains research-only and is not in the runtime chain. |
| Master bus | `gw_mixcentric_stereo` (glue / saturation) → `master_loudness_finalize.py` (pregain + L2 + soft true-peak ceiling). When `--no-loudness-finalizer` is set, only `master_l2_stereo` runs at the end of the render script. |

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

.venv/bin/python scripts/daw_reference_compare.py \
  --candidate-root calibration_outputs/faust_stages \
  --out-dir calibration_outputs/daw_reference_compare
```

The report shows peak/RMS deltas, correlation, banded spectral error, and a match score per stage.

---

## Audit tools

### Vocal balance audit

```bash
.venv/bin/python scripts/audit_vocal_balance.py \
  --vocal-wav /tmp/vo_proc.wav \
  --accomp-wav /tmp/bc_proc.wav \
  --out-dir reports/
```

### Template feature audit (before vs. after template chain)

```bash
.venv/bin/python scripts/audit_template_vocal_features.py \
  --analysis-json reports/analysis.json \
  --processed-vocal /tmp/vocal_after_template.wav \
  --out-dir reports/audit/
```

Outputs `vocal_feature_audit.json` and `vocal_feature_audit.md` with ratio deltas,
band balance status, spectral deviation, and tuning suggestions per template objective.

### Resolve and inspect the mix plan

```bash
.venv/bin/python scripts/plan_mix_template.py analysis.json --output resolved_plan.json
```

### Profile render cost and parity

`profile_render_job.py` is a safety harness for future fast-path work. It does not
change the render chain; it either runs the current legacy renderer with
`--stage-report`, or summarizes an existing summary JSON, then writes a sorted timing
report and optional WAV parity metrics.

```bash
# Run the current renderer, collect per-stage timing, and write profile JSON/Markdown
.venv/bin/python scripts/profile_render_job.py vocal.wav accomp.wav \
  --label profile_bingchao_huanghun \
  --out-dir calibration_outputs/profiles

# Summarize an existing render without re-rendering
.venv/bin/python scripts/profile_render_job.py \
  --summary-json calibration_outputs/latest/finaltest_bingchao_huanghun_summary.json \
  --out-dir calibration_outputs/profiles

# Summarize an existing stage report directly
.venv/bin/python scripts/profile_render_job.py \
  --stage-report-json calibration_outputs/latest/mix_fixed_loudness_bingchao_huanghun.stage_report.json \
  --mix-wav calibration_outputs/latest/mix_fixed_loudness_bingchao_huanghun.wav \
  --label fixed_loudness_bingchao_huanghun \
  --out-dir calibration_outputs/profiles

# Compare a candidate output against a baseline WAV for parity
.venv/bin/python scripts/profile_render_job.py \
  --summary-json calibration_outputs/latest/finaltest_bingchao_huanghun_summary.json \
  --compare-to calibration_outputs/latest/mix_reference.wav \
  --out-dir calibration_outputs/profiles
```

The parity report includes correlation, diff RMS, max absolute sample difference,
loudness for both files, and coarse band deltas. Use it before promoting any fast
engine change; the legacy renderer remains the reference path.

For the current cold render path, start by profiling `accomp_vocal_duck` and
`bus_balance_analysis`: historical reports show those two stages dominate measured
stage time. `apply_accomp_vocal_duck.py --profile-timing` records its internal read,
filter, envelope, smoothing, gain, and write timings in the duck metadata without
changing the output audio.

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
| Accompaniment yielding | `scripts/apply_accomp_vocal_duck.py` | Template + dry-vocal-driven multiband ducking keyed by the post-FX vocal |
| Bus balance | `scripts/compute_render_bus_balance.py` | Conservative active vocal/accomp ratio matching at render time (step 3a) |
| Master loudness | `scripts/master_loudness_finalize.py` | Safe master-bus pregain → L2 → post-trim / controlled makeup → soft true-peak ceiling → global de-click |
| Render orchestration | `scripts/render_template_mix.sh` | Runs the full DSP pipeline in order |
| Full auto pipeline | `scripts/auto_template_mix.py` | End-to-end: analyze → plan → render → report |
| Calibration | `scripts/daw_reference_compare.py` | Faust stage vs. Cubase reference comparison |

Signal flow for template A/B/C:

```
vocal.wav ──► normalize to 44.1 kHz / pcm_f32le ──[optional: auto_volume_mix]──►
                                              │
                                    template vocal insert chain (incl. C1 compressor)
                                              │
                                    residual vocal EQ + reference vocal source EQ (plan)
                                              │
                                       vocal_group_fx (stereo: shimmer + reverb + delay)
                                              │
accomp.wav ─► normalize to 44.1 kHz / pcm_f32le ──[optional: auto_volume_mix]──► template music EQ + reference carve EQ
                                              │
                                    vocal-aware multiband accompaniment ducking
                                              │
                    [step 3a] compute_render_bus_balance (active ratio → conservative bus gains)
                                              │
                                                                              amix (dropout_transition=0)
                                                                                          │
                                                          [step 3b] master tilt EQ (reference)
                                                                                          │
                                                          template bus EQ → GW MixCentric
                                                                                          │
                              master_loudness_finalize: pregain → L2 → soft TP ceiling
                                                                                          │
                                                                                  final_mix.wav
                                                                               (+ .accomp_duck.json
                                                                                  + .bus_balance.json
                                                                                  + .loudness.json)
```

Reference-driven stages activate when the input vocal's song name resolves to a reference triplet under `downloads/feishu_long_audio_screened/{原曲,原曲人声,伴奏}/`, a sibling `feishu_long_audio_screened/{原曲,原曲人声,伴奏}/` folder, or when explicit `--reference-*` flags are passed. Without a reference, bus gains stay at 0 dB and reference EQ stages copy audio through unchanged.

### Related projects in a full cover workflow

| Project | Role in pipeline |
|---|---|
| `spectral-mix-template-selector` | Spectral analysis → template A/B/C label (`spectrum_template_analyzer.py`) |
| `music_auto_mix1` (this repo) | Template DSP render, reference overrides, master loudness |
| `delayverb` | Standalone delay/reverb tool — **not yet integrated** into `render_template_mix.sh` |
