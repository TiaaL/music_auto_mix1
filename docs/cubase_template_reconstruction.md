# Cubase Template Reconstruction

Source CPR:

```text
d:/cubase/project/混音模版0512/通用混音模板_纯净版.cpr
```

The CPR is a Cubase 12.0.0 binary project. This reconstruction is based on
printable UTF-8/XML fragments anchored by the named Cubase tracks shown in the
project:

```text
伴奏轨处理-模版a&模版b
伴奏轨模版c
模版a
模版b
模版c
Group 01
FX 01-SuperTap 2-Taps Stereo
FX 02-RVerb Stereo
FX 03-ValhallaVintageVerb
Stereo Out
```

The raw reconstruction files are:

```text
config/cubase_templates/template_a.raw.json
config/cubase_templates/template_b.raw.json
config/cubase_templates/template_c.raw.json
config/cubase_templates/common_group_fx.raw.json
```

## Template Routing

Template A:

```text
模版a -> Group 01 -> Stereo Out
伴奏轨处理-模版a&模版b -> Stereo Out
shared FX returns: FX 01, FX 02, FX 03
```

Template B:

```text
模版b -> Group 01 -> Stereo Out
伴奏轨处理-模版a&模版b -> Stereo Out
shared FX returns: FX 01, FX 02, FX 03
```

Template C:

```text
模版c -> Group 01 -> Stereo Out
伴奏轨模版c -> Stereo Out
shared FX returns: FX 01, FX 02, FX 03
```

The vocal template tracks resolve to `Group 01` through `OutputBusValue` bus
UID `R`, which is `Group 01`'s input bus. The accompaniment tracks resolve to
`Stereo Out` through bus UID `&`, so they appear to bypass `Group 01`.

The visible channel fader/output fields look like unity, so the raw JSON records
these feeds as `0.0 dB` with low confidence.

FX send levels are confirmed from the user-provided Cubase Sends screenshot:

```text
FX 02-RVerb Stereo: -7.73 dB, post-fader
FX 03-ValhallaVintageVerb / ValhallaShimmer: -18.2 dB, post-fader
FX 01-SuperTap 2-Taps Stereo: -26.0 dB, post-fader
```

These screenshot values should be treated as higher-confidence than the CPR
binary scan for send levels.

## Insert Chains

Template A vocal chain:

```text
C1 gate Mono
Pro-Q 3
C1 comp Mono
Sibilance Mono
```

Template B vocal chain:

```text
RBass Mono
F6-RTA Mono
C1 comp Mono
Sibilance Mono
L1 limiter Mono
```

Template C vocal chain:

```text
Pro-Q 3
Vocal Rider Mono
C1 comp Mono
OneKnob Brighter Mono
```

A/B shared accompaniment chain:

```text
Pro-Q 3
```

C accompaniment chain:

```text
Pro-Q 3
```

## Shared Group And FX

Group:

```text
Group 01: no active Cubase EQ plugin
input bus UID: R
output bus UID: & -> Stereo Out
confirmed sends:
  FX 02-RVerb Stereo: -7.73 dB
  FX 03-ValhallaVintageVerb / ValhallaShimmer: -18.2 dB
  FX 01-SuperTap 2-Taps Stereo: -26.0 dB
```

FX returns:

```text
FX 01-SuperTap 2-Taps Stereo: SuperTap 2-Taps Stereo
  input bus UID: T, output bus UID: & -> Stereo Out
FX 02-RVerb Stereo: RVerb Stereo
  input bus UID: V, output bus UID: & -> Stereo Out
FX 03-ValhallaVintageVerb: ValhallaShimmer
  input bus UID: X, output bus UID: & -> Stereo Out
```

Master bus:

```text
Stereo Out: Pro-Q 3, GW MixCentric Stereo, L2 Stereo
input bus UID: &
hardware outputs: Focusrite USB ASIO Output 1/2
```

User correction: red-boxed insert slots in the Cubase screenshot are inactive.
Do not include Aphex/DeEsser on A, Pro-Q3/Aphex on B, RBass/C1 gate/DeEsser/
Sibilance on C, or the red-boxed GW MixCentric/SoftClipper instances on the
A/B/C master buses. Cubase EQ is not active in the templates. C6 is preserved as
extracted DAW context, but the default render skips it because prior Faust C6
renders caused noise.

Note: the third FX channel is named `FX 03-ValhallaVintageVerb`, but the plugin
record inside it is `ValhallaShimmer`.

## Extraction Caveats

- Waves plugin names, sub-components, versions, and preset names are readable
  from embedded `PresetChunkXMLTree` fragments.
- ValhallaShimmer exposes a readable `MYPLUGINSETTINGS` XML parameter snapshot.
- FabFilter Pro-Q 3 and Cubase EQ parameter payloads are visible only as mixed
  binary/text records in this pass, so their detailed band parameters remain
  undecoded.
- The current files intentionally preserve uncertain details as `confidence:
  "medium"` instead of guessing exact values.
