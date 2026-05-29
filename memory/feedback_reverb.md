# Reverb Feedback Memory

- Current direction: keep vocal reverb controlled; when asked to adjust, move gradually.
- Current RVerb baseline: send `-12.5 dB`, time `1.75 s`, predelay `12 ms`, damp `0.35`.
- Current delay baseline: send `-27 dB`, feedback `0.10`.
- If user says "reverb is still small" or "再大一点", first move is a small increase:
  - raise `RVERB_SEND_PRE_DB` by about `+1 dB`, or
  - extend `RVERB_TIME_S` by about `+0.1~0.2 s`.
- Avoid changing shimmer/delay first unless the user specifically asks for wider or more obvious FX tails.
