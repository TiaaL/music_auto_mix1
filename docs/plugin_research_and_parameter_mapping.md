# Plugin Research And Parameter Mapping

This document tracks how Cubase template plugins should be interpreted before
we approximate them in Faust. It separates confirmed project data from research
and from items that require user confirmation.

## Ground Rules

- Prefer official manuals/product pages when interpreting plugin behavior.
- Do not guess Waves `RealWorld` parameter indexes unless the index-to-control
  mapping is confirmed by documentation, screenshots, or a controlled export.
- Preserve raw preset values first; decode only what is confidently mapped.
- Ask the user before filling gaps that affect audible behavior.

## Official References

| Plugin | Official reference | Use in template |
| --- | --- | --- |
| Waves Renaissance Bass | https://assets.wavescdn.com/pdf/plugins/renaissance-bass.pdf | Low-frequency harmonic/bass enhancement |
| Waves C1 Compressor/Gate | https://assets.wavescdn.com/pdf/plugins/c1-compressor.pdf | Gate and compression |
| Waves DeEsser | https://assets.wavescdn.com/pdf/plugins/deesser.pdf | De-essing |
| Waves Sibilance | https://www.waves.com/1lib/pdf/plugins/sibilance.pdf | Sibilance control |
| Waves Aphex Vintage Aural Exciter | https://assets.wavescdn.com/pdf/plugins/aphex-vintage-aural-exciter.pdf | Harmonic excitation/brightness |
| Waves C6 Multiband Compressor | https://www.waves.com/1lib/pdf/plugins/c6-multiband-compressor.pdf | Multiband dynamics/sidechain shaping; disabled in current Faust runtime |
| Waves F6 Floating-Band Dynamic EQ | https://www.waves.com/1lib/pdf/plugins/f6.pdf | Dynamic EQ |
| Waves Vocal Rider | https://assets.wavescdn.com/pdf/plugins/vocal-rider.pdf | Automatic vocal level riding |
| Waves L1 Ultramaximizer | https://assets.wavescdn.com/pdf/plugins/l1-ultramaximizer.pdf | Peak limiting/maximizing |
| Waves L2 Ultramaximizer | https://assets.wavescdn.com/pdf/plugins/l2-ultramaximizer.pdf | Master limiting/maximizing |
| ValhallaShimmer | https://valhalladsp.com/2011/01/24/valhallashimmer-the-manual/ | Shimmer reverb |
| FabFilter Pro-Q 3 | https://www.fabfilter.com/downloads/pdf/help/ffproq3-manual.pdf | Parametric/dynamic EQ |

## CPR Parameter Coverage

| Plugin instance | Preset found | Raw parameter data found | Decode status |
| --- | --- | ---: | --- |
| Renaissance Bass | Acappella Bass Voice | 18 `RealWorld` values | Raw preserved; control indexes need confirmation |
| C1 gate Mono | Classic gate | 50 `RealWorld` values | Raw preserved; `src/c1_gate.dsp` provides an effect-first approximation |
| C1 comp Mono | Classic compressor | 50 `RealWorld` values | Raw preserved; current `c1_comp` can approximate after mapping |
| Vocal Rider Mono | none visible | 46 `RealWorld` values | Raw preserved; `src/vocal_rider_mono.dsp` approximates target/range/sensitivity |
| DeEsser Mono | Full Mix | 15 `RealWorld` values | Raw preserved; needs index mapping before exact tuning |
| OneKnob Brighter Mono | none visible | 36 `RealWorld` values | Raw preserved; needs mapping/screenshot |
| Sibilance Mono | Voice-over | 47 `RealWorld` values | Raw preserved; `src/sibilance_mono.dsp` uses readable threshold/frequency/range anchors |
| L1 limiter Mono | Voice | 17 `RealWorld` values | Raw preserved; `src/l1_limiter_mono.dsp` approximates threshold/ceiling/release |
| L2 Stereo | Hi Res CD Master | 14 `RealWorld` values | Raw preserved; likely limiter approximation after mapping |
| GW MixCentric Stereo | GW You Clean Up Real Nice | 35 `RealWorld` values | Raw preserved; `src/gw_mixcentric_stereo.dsp` approximates macro glue/tone/saturation |
| ValhallaShimmer | none visible | XML `parameter0` to `parameter11` | Raw XML preserved; parameter names need official/manual mapping |
| FabFilter Pro-Q 3 | Default Setting | binary/text state block | Not decoded; requires screenshot/export or FabFilter state parser |
| Cubase EQ | none visible | binary/text channel state | Not decoded; requires Cubase screenshot/export |
| SoftClipper | none visible | small binary/text state | Not decoded; requires Cubase screenshot/export |

## Exported Preset Coverage

Exported `.vstpreset` files from:

```text
D:/cubase/project/混音模版0512/混音参数0512
```

have been extracted to:

```text
config/extracted_vstpresets
```

Current coverage:

```text
26 total preset files
19 Waves RealWorld parameter snapshots
1 Valhalla XML parameter snapshot
6 FabFilter binary state snapshots
```

Template-to-preset links are tracked in:

```text
config/cubase_templates/preset_links.json
```

Inactive/red-boxed inserts:

```text
template_a vocal: Aphex Vintage Exciter Mono, DeEsser Mono
template_b vocal: Pro-Q 3 vocal instance, Aphex Vintage Exciter Mono
template_c vocal: RBass Mono, C1 gate Mono, DeEsser Mono, Sibilance Mono
master bus: red-boxed GW MixCentric/SoftClipper instances
```

User correction: red-boxed insert slots in the Cubase screenshot are inactive.
Do not implement, calibrate, or list them as missing active plugins. Keep their
raw preset records only as historical extraction context.

Cubase EQ is not active in these templates. C6 is present in extracted DAW
context, but the default Faust/native render skips it because previous C6
renders caused noise.

If these presets exist elsewhere, add them to the source preset folder and rerun:

```powershell
.\.venv\Scripts\python.exe scripts\extract_vstpresets.py
```

## Template Intent From Plugin Chains

These are working interpretations, not final DSP specs.

### Template A

Chain:

```text
C1 gate -> Pro-Q 3 -> C1 comp -> Sibilance -> group FX
```

Likely intent:

- Clean noise/gaps with C1 gate.
- Shape tone with Pro-Q 3.
- Stabilize level with C1 compression.
- Control sibilance with Sibilance.

Do not implement exact gains/frequencies until Pro-Q 3 screenshots or state
decode are available.

### Template B

Chain:

```text
RBass -> F6-RTA -> C1 comp -> Sibilance -> L1 -> group FX
```

Likely intent:

- Add/restore perceived body with RBass.
- Use F6 as dynamic EQ before compression.
- Compress and control tone without inactive Pro-Q/Aphex inserts.
- Control sibilance, then constrain peaks with L1.

Needs confirmation: F6 band settings and L1 threshold/out ceiling.

### Template C

Chain:

```text
Pro-Q 3 -> Vocal Rider -> C1 comp -> OneKnob Brighter -> group FX
```

Likely intent:

- Correct tone early with Pro-Q 3.
- Ride vocal level, then compress.
- Brighten with OneKnob only; red-boxed RBass/Gate/DeEsser/Sibilance are inactive.

Needs confirmation: Pro-Q 3 bands, Vocal Rider target/range, and OneKnob Brighter
amount.

## Shared Group/FX/Master

Confirmed from CPR plus user screenshot:

```text
Group 01 -> post-fader sends:
  FX 02-RVerb Stereo: -7.73 dB
  FX 03-ValhallaVintageVerb / ValhallaShimmer: -18.2 dB
  FX 01-SuperTap 2-Taps Stereo: -26.0 dB

FX returns -> Stereo Out
Stereo Out: Pro-Q 3 -> GW MixCentric Stereo -> L2 Stereo
```

FX plugin presets:

```text
SuperTap 2-Taps Stereo: Ping pong
RVerb Stereo: Vocal Plate
ValhallaShimmer: XML parameters preserved in common_group_fx.raw.json
L2 Stereo: Hi Res CD Master
GW MixCentric: GW You Clean Up Real Nice
```

## User Confirmation Needed

Ask before implementing these as exact DSP values:

1. FabFilter Pro-Q 3 band settings for `模版a`, `模版b`, `模版c`,
   `伴奏轨处理-模版a&模版b`, `伴奏轨模版c`, and `Stereo Out`.
2. Cubase channel EQ settings on vocal tracks, accompaniment tracks, Group 01,
   FX returns, and Stereo Out.
3. F6 band frequency/gain/Q/range/threshold settings in template B.
4. Vocal Rider target/range/sensitivity settings in template C.
5. OneKnob Brighter amount in template C.
6. L1 limiter threshold/out ceiling/release settings in template B.
7. Whether the Waves `RealWorld` arrays should be decoded by screenshots,
   preset exports, or a controlled parameter-sweep test.

## Proposed Next Step

Current implementation step:

```text
scripts/render_template_mix.sh
  template_a: C1 gate -> Pro-Q3 approx -> C1 comp -> Sibilance -> vocal_group_fx
  template_b: RBass -> F6-RTA -> C1 comp -> Sibilance -> L1 -> vocal_group_fx
  template_c: Pro-Q3 approx -> Vocal Rider -> C1 comp -> OneKnob Brighter -> vocal_group_fx
  music: template Pro-Q3 approx
  master: template bus Pro-Q3 approx -> GW MixCentric approx -> L2
```

Next, improve one plugin at a time with screenshots, controlled sweeps, or a
confirmed Waves/FabFilter parameter map.
