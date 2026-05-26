# Faust Audio Processing Chain

Faust DSP implementations of classic Waves plugin algorithms, designed for batch audio processing via command line.

## Requirements

Build-only requirements:

```bash
brew install faust sox libsndfile
```

Full workflow requirements:

```bash
brew install faust sox ffmpeg libsndfile
```

Also required:

- `python3`
- `clang++` / Xcode Command Line Tools
- `ffprobe` (usually installed together with `ffmpeg`)

Quick environment check:

```bash
./scripts/check_env.sh
```

## Build

```bash
make          # build all processors → build/
make clean    # remove build artifacts
make svg      # generate signal flow diagrams
make test     # run L2 limiter correctness tests
make smoke    # run lightweight workflow smoke tests
```

Notes:

- shell scripts auto-build missing binaries via `make`
- intermediate workflow files now use unique temp names, so concurrent runs are safer
- `build/` currently contains generated artifacts for convenience, but source-of-truth remains `src/*.dsp`

## Processors

### Human Voice Chain (mono)

Current script order: **RDeEsser → REQ6 → C1**

| Binary | Source | Plugin model |
|--------|--------|--------------|
| `build/req6` | `src/req6.dsp` | Waves REQ6 Renaissance EQ |
| `build/c1_comp` | `src/c1_comp.dsp` | Waves C1 Compressor |
| `build/rdeesser` | `src/rdeesser.dsp` | Waves Renaissance DeEsser |

#### REQ6 — 6-band Parametric EQ

6 bands in series with panel-style enable/type controls.

| Band | Type | Default freq | Parameters |
|------|------|-------------|------------|
| 1 | High-pass | 40 Hz | `B1_ON`, `B1_SLOPE`, `B1_FREQ` |
| 2 | Low shelf / bell | 120 Hz | `B2_ON`, `B2_MODE`, `B2_FREQ`, `B2_GAIN`, `B2_Q` |
| 3 | Bell / peak | 228 Hz | `B3_ON`, `B3_FREQ`, `B3_GAIN`, `B3_Q` |
| 4 | Bell / peak | 7711 Hz | `B4_ON`, `B4_FREQ`, `B4_GAIN`, `B4_Q` |
| 5 | High shelf / bell | 17596 Hz | `B5_ON`, `B5_MODE`, `B5_FREQ`, `B5_GAIN`, `B5_Q` |
| 6 | Low-pass | 20 kHz | `B6_ON`, `B6_SLOPE`, `B6_FREQ` |

#### C1 Comp — Soft-Knee Compressor

RMS detection, quadratic soft-knee transition, dB-domain attack/release smoothing.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `THRESHOLD_DB` | −20 dBFS | Compression onset |
| `RATIO` | 4.0 | Compression ratio (use 20+ for limiting) |
| `ATTACK_MS` | 5 ms | Gain reduction attack |
| `RELEASE_MS` | 50 ms | Gain reduction release |
| `KNEE_DB` | 6 dB | Soft knee width (0 = hard knee) |
| `MAKEUP_DB` | 0 dB | Output makeup gain |
| `RMS_WIN_MS` | 10 ms | RMS detection window |
| `PEAK_WIN_MS` | 1 ms | Peak detector speed |
| `DETECTOR_MIX` | 0..1 | RMS / peak blend |
| `SIDECHAIN_HP_ON` | 0 / 1 | Sidechain high-pass enable |
| `SIDECHAIN_HP_HZ` | 120 Hz | Sidechain high-pass corner |
| `PDR_AMOUNT` | 0..1 | Program-dependent release weight |

#### RDeEsser — Split-Band De-esser

Complementary crossover splits signal into sub-band and ess-band. Gain reduction applies only to the ess-band — low frequencies are untouched.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ESS_FREQ` | 7500 Hz | Crossover / detection frequency |
| `THRESH_DB` | −20 dBFS | Detection threshold |
| `RANGE_DB` | 12 dB | Maximum gain reduction on ess-band |
| `ATTACK_MS` | 1 ms | De-esser attack |
| `RELEASE_MS` | 40 ms | De-esser release |
| `RMS_WIN_MS` | 3 ms | RMS detection window |
| `DEESS_MODE` | 0 / 1 | `0 = split-band`, `1 = wideband` |
| `MONITOR_ESS` | 0 / 1 | Solo ess band for tuning |

---

### Accompaniment + Master Bus (stereo)

| Binary | Source | Plugin model |
|--------|--------|--------------|
| `build/accomp_proq3` | `src/accomp_proq3.dsp` | FabFilter Pro-Q3 style accompaniment EQ |
| `build/accomp_c6_sc` | `src/accomp_c6_sc.dsp` | Waves C6 SideChain Stereo style multiband dynamics |
| `build/accomp_l2_stereo` | `src/accomp_l2_stereo.dsp` | Waves L2 Stereo style limiter |
| `build/vocal_group_fx` | `src/vocal_group_fx.dsp` | Stereo vocal group FX rack |

Current accompaniment chain order:

```bash
accomp_proq3 -> accomp_c6_sc -> accomp_l2_stereo
```

Notes:

- `accomp_proq3` approximates the requested `FabFilter Pro-Q3` snapshot:
  - `14 Hz` high-pass
  - `2241 Hz  -0.74 dB  Q 1.0`
  - `6119 Hz  +0.39 dB  Q 0.5`
- `accomp_c6_sc` approximates the posted `Waves C6 SideChain Stereo` screenshot with fixed band layout and multiband dynamic behavior
- `accomp_l2_stereo` approximates the posted `Waves L2 Stereo` snapshot:
  - `Threshold = -1.1 dB`
  - `Out Ceiling = 0.0 dB`
  - `ARC-style release behavior`

| Parameter | Value | Description |
|-----------|-------|-------------|
| `THRESHOLD_DB` | −1.1 dBFS | L2-style limiting threshold |
| `CEILING_DB` | 0.0 dBFS | L2-style output ceiling |
| `LOOKAHEAD_MS` | 3 ms | Lookahead window |

---

### Mixer (vocal + accompaniment)

`build/mixer` takes a **3-channel interleaved file** (ch0 = vocal mono, ch1/ch2 = accompaniment stereo) and outputs a stereo master mix with FX send/return reverb bus.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `VOCAL_GAIN_DB` | 0 dB | Vocal channel fader |
| `VOCAL_PAN` | 0 | Pan position (−1 left … +1 right) |
| `ACCOMP_GAIN_DB` | 0 dB | Accompaniment fader |
| `FX_SEND_VOCAL` | 0.3 | Post-fader FX send (vocal) |
| `FX_SEND_ACCOMP` | 0.2 | Post-fader FX send (accompaniment) |
| `FX_RETURN_DB` | −6 dB | FX return level |
| `MASTER_GAIN_DB` | 0 dB | Master output fader |

---

## Usage

### Vocal chain script

If you want to run the full vocal chain in one command, use:

```bash
./scripts/vocal_chain.sh vocal.wav vocal_out.wav
```

Current built-in order:

```bash
rdeesser -> req6 -> c1_comp
```

If you want to change the order later, edit the three `run_stage` lines near the bottom of `scripts/vocal_chain.sh`.

### Vocal stereo group script

If you want the vocal to render through the mono insert chain and then into a stereo vocal-group style FX rack, use:

```bash
./scripts/vocal_stereo_group.sh vocal.wav vocal_group_out.wav
```

Current built-in order:

```bash
rdeesser -> req6 -> c1_comp -> vocal_group_fx
```

`vocal_group_fx` adds:

- shimmer-style stereo send
- RVerb-style stereo reverb send
- SuperTap 2-Taps Stereo style delay send

Current note: loudness-driven macro gain/send shaping inside `vocal_group_fx` is disabled right now. The rack is currently being used as a fixed-FX stereo group.

The current group send balance is tuned as:

- Waves reverb style send: `-5.39 dB`
- Valhalla shimmer style send: `-18.0 dB`
- SuperTap 2-Taps Stereo style send: `-24.0 dB`

The current delay tempo target is `89 BPM`, with normal stereo spread (`ping-pong` is currently off).

Recent tuning changes for the current vocal FX sound:

- shimmer is shorter, darker, and quieter
- delay is shorter, darker, and quieter
- the second delay tap is now confirmed active even when `SUPERTAP_CROSS_FB = 0`
- reverb, shimmer, and delay were verified with an impulse-style test and are all active

Important: this rack is an approximation-oriented Faust implementation with plugin-style parameters. It does not claim to be a bit-accurate clone of proprietary Waves or Valhalla algorithms.

### Vocal chain

```bash
build/rdeesser vocal.wav   /tmp/v1.wav
build/req6     /tmp/v1.wav /tmp/v2.wav
build/c1_comp  /tmp/v2.wav vocal_out.wav
```

### Accompaniment / master bus

```bash
./scripts/accomp_chain.sh accomp.wav accomp_out.wav
```

Current accompaniment FX chain:

```bash
accomp_proq3 -> accomp_c6_sc -> accomp_l2_stereo
```

### Master bus

```bash
./scripts/master_bus_chain.sh stereo_mix.wav stereo_master.wav
```

Current master-bus chain:

```bash
master_proq3 -> master_softclipper -> master_l2_stereo
```

Current built-in settings:

- Pro-Q3 style EQ: HPF `21 Hz`, `70 Hz +0.85 dB Q 2.6`, `302 Hz -0.74 dB Q 2.3`, `4047 Hz -0.18 dB Q 1.0`, LPF `20659 Hz`
- SoftClipper style stage: input `+4.0 dB`, mix `100%`, output `-0.7 dB`, second/third emphasis `60/40`
- L2 Stereo style stage: threshold `-5.9 dB`, out ceiling `-1.0 dB`, ARC-style adaptive release enabled

### Full mix

Recommended full workflow:

```bash
./scripts/full_fx_mix.sh vocal.wav accomp.wav final_mix.wav
```

This is the main end-to-end script if you want:

1. volume automation on vocal and accompaniment first
2. vocal FX after volume automation
3. accompaniment FX chain after volume automation
4. stereo mix export
5. master-bus processing

Current built-in order:

```bash
volume+dynamics shaping -> vocal FX chain -> accompaniment FX chain -> stereo mix -> master bus
```

Details:

- vocal automation: segment-based rule-engine decisions
- accompaniment automation: segment-based rule-engine decisions plus vocal/accompaniment conflict handling
- accompaniment intro/outro: left untouched
- accompaniment FX chain: `accomp_proq3 -> accomp_c6_sc -> accomp_l2_stereo`
- master bus chain: `master_proq3 -> master_softclipper -> master_l2_stereo`
- `full_fx_mix.sh` expects mono vocal + stereo accompaniment with matching sample rates
- temporary workflow files now use unique `.wav` names, so nested calls and repeated runs are stable again

### Region loudness feature extraction

If you want region-based loudness features instead of fixed millisecond windows, use:

```bash
python3 scripts/extract_loudness_regions.py raw.wav --output raw_regions.csv
```

If you also have a paired processed file and want `proc_*` plus `gain_delta_db`:

```bash
python3 scripts/extract_loudness_regions.py \
  raw.wav \
  --processed-audio processed.wav \
  --output region_features.csv
```

If one group directory contains files like `vo_S2_yuan.wav`, `vo_S2_DOWN.wav`, `bc_yuan.wav`, `bc_DOWN.wav`, you can process that whole 4-file group with:

```bash
python3 scripts/extract_loudness_regions.py \
  --pair-dir ./your_group_dir \
  --output grouped_region_features.csv
```

Default grouping rules:

- `vo*` = vocal
- `bc*` = accompaniment
- `_yuan` = raw/original side
- `_DOWN` = processed side
- all four files in the folder are treated as one group

If you have many groups and each group is stored in its own subdirectory, use:

```bash
python3 scripts/extract_loudness_regions.py \
  --group-root ./all_groups_dir \
  --output grouped_region_features.csv
```

Segmentation logic:

- short-frame RMS below threshold = silence
- the audio between two silence blocks = one region
- this matches the usual DAW waveform judgment of "here has content, here is empty"

Main output columns:

- `raw_rms_db`: region average RMS
- `raw_peak_db`: region peak level
- `raw_min_rms_db`: quietest subwindow RMS inside the region
- `raw_crest_db`: `raw_peak_db - raw_rms_db`
- `local_variation`: standard deviation of short subwindow RMS values
- `level_slope`: current region RMS minus previous region RMS
- `prev_rms_db` / `next_rms_db`: neighboring region RMS
- `position_ratio`: region midpoint position in the whole file (`0.0` to `1.0`)
- `is_silent`: whether the region RMS is below `-60 dB`
- `proc_rms_db` / `proc_peak_db` / `proc_crest_db`: same measurements on paired `_DOWN` audio
- `gain_delta_db`: `raw_rms_db - proc_rms_db`, i.e. `yuan - DOWN`
- `rms_delta_db` / `peak_delta_db` / `min_rms_delta_db` / `crest_delta_db`: `yuan - DOWN`
- `group_id`: current 4-file group id
- `role`: `vocal` or `accomp`
- `track_id`: paired track id such as `V9_S2` or `zcx`

Useful knobs:

- `--silence-threshold-db`: default `-45`
- `--min-silence-ms`: default `120`
- `--min-region-ms`: default `120`
- `--subwindow-ms`: default `80`
- vocal FX chain: `rdeesser -> req6 -> c1_comp -> vocal_group_fx`
- final mix trim: vocal `-2.5 dB`, accompaniment `0 dB`
- final export: automatic peak safety trim if needed

## Operational Notes

### Environment and dependency checks

Before running the full workflow on a new machine:

```bash
./scripts/check_env.sh
```

This verifies the commands used by build and render scripts:

- `faust`
- `clang++`
- `make`
- `sox`
- `ffmpeg`
- `ffprobe`
- `python3`

The main scripts also validate expected audio shapes before processing:

- `vocal_chain.sh` expects mono vocal input
- `vocal_stereo_group.sh` expects mono vocal input
- `full_fx_mix.sh` expects mono vocal + stereo accompaniment with matching sample rates
- `process.sh` expects stereo limiter input

### Smoke testing

If you want to confirm that the main workflows still run end-to-end after edits:

```bash
make smoke
```

This smoke test generates short synthetic inputs and validates:

- `scripts/vocal_chain.sh`
- `scripts/vocal_stereo_group.sh`
- `scripts/full_fx_mix.sh`

### Build behavior

The shell workflows use a unified build path:

- if a required binary is missing under `build/`, the script runs `make build/<target>`
- this keeps script behavior aligned with the main `Makefile`

Legacy/manual basic full mix example:

```bash
# Merge vocal (mono) + accompaniment (stereo) into a 3-channel file
sox -M vocal_out.wav accomp_out.wav combined.wav

# Mix to stereo master, then limit
build/mixer       combined.wav  /tmp/mix.wav
build/l2_arc      /tmp/mix.wav  master_out.wav
```

### Volume-only automation mix

If you want a separate workflow that only adjusts volume and does not use any effects, use:

```bash
python3 scripts/auto_volume_mix.py vocal.wav accomp.wav final_mix.wav
```

This script is independent from the FX chain above. It does not replace `vocal_chain.sh` or `vocal_stereo_group.sh`.

The current gain rules now live in:

- [audio_gain_rules.py](/Users/xy/Desktop/code/claude/music/faust/scripts/audio_gain_rules.py)

Typical workflow:

```bash
python3 scripts/auto_volume_mix.py \
  vo.wav \
  bc.wav \
  --vocal-out /tmp/vo_proc.wav \
  --accomp-out /tmp/bc_proc.wav
```

What it does:

- detects vocal phrases from the vocal track
- groups nearby vocal phrases into larger vocal paragraphs
- extracts per-segment features such as RMS, peak, crest factor, and rough dynamic range
- evaluates the current `R1 ~ R10` gain rules for each segment
- applies the returned `delta_db` to each segment
- keeps accompaniment intro, outro, and inter-paragraph gaps separate from sung paragraphs
- applies accompaniment gain/ducking per detected vocal paragraph
- mixes the adjusted vocal and accompaniment to a stereo output

Typical vocal-side rules you may see in logs:

- `R1`: RMS low and peak has headroom, so lift the segment
- `R5`: transient too strong, so limit the boost
- `R8` / `R9`: segment boundary fade handling
- `R10`: vocal/backing conflict, so backing is reduced

Useful example with explicit intermediate outputs:

```bash
python3 scripts/auto_volume_mix.py \
  vocal.wav \
  accomp.wav \
  /tmp/auto_mix.wav \
  --vocal-out /tmp/auto_vocal.wav \
  --accomp-out /tmp/auto_accomp.wav
```

The script currently uses this strategy:

- vocal target around `-14.5 dBFS` mean per detected phrase
- vocal phrase gain rules are clamped by the configured rule engine and limiter
- vocal paragraph detection groups phrase gaps shorter than `1.2 s`
- accompaniment paragraph target centered around `4.5 dB` below the processed vocal
- accompaniment intro, gaps, and outro stay at the configured base gain

You can also use this script as a preprocessing step only. If you omit `final_mix.wav`, it will still write the volume-adjusted vocal and accompaniment files specified by `--vocal-out` and `--accomp-out`.

## Modifying Parameters

All parameters are constants at the top of each `.dsp` file. Edit them and rebuild:

```bash
# Example: tighten the de-esser
# Edit ESS_FREQ = 8500.0 and THRESH_DB = -18.0 in src/rdeesser.dsp
make build/rdeesser
```

## Architecture notes

| Algorithm | Implementation |
|-----------|---------------|
| EQ filters | Faust standard library (`fi.highpass`, `fi.low_shelf`, `fi.peak_eq_cq`, …) |
| Biquad coefficients | Audio EQ Cookbook (R. Bristow-Johnson) via `fi.tf2s` |
| Compressor envelope | dB-domain attack/release via `loop ~ _` feedback |
| De-esser split | Complementary LP/HP: `LP(x) + (x − LP(x)) = x` (perfect reconstruction) |
| Reverb (mixer) | `re.stereo_freeverb` (Faust standard library) |
| L2 limiting | Stereo-linked lookahead via `@ N` delay + exponential release |
