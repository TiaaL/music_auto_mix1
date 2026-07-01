# music-auto-mix

Faust DSP approximations of classic Waves/FabFilter plugins, wired into a Python rule engine and shell workflow for automated vocal/accompaniment mixing.

## 中文快速说明

这是一个“干声 + 伴奏 → 自动混音成品”的实验型工程。核心思路是：

1. 分析干声频谱，自动选择接近 Cubase 模板 A/B/C 的处理链。
2. 生成 `resolved_mix_plan.json`，把残余 EQ、素材清理、人声/伴奏比例、参考曲覆盖项写成可审计参数。
3. 用 Faust 近似插件链和 FFmpeg 后处理渲染最终 WAV。
4. 可选接入飞书表格批量下载、批量渲染、上传结果链接。

最常用命令：

```bash
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --with-volume-automation \
  --report-dir reports/
```

带参考原曲/参考 stem 时：

```bash
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --reference-audio ref/full_mix.wav \
  --reference-vocal ref/vocal.wav \
  --reference-accomp ref/accomp.wav \
  --with-volume-automation
```

没有参考文件也可以跑。此时系统会使用通用人声清理、通用 active 人声/伴奏比例目标和默认响度策略，不会按某首参考歌塑形。

参考目标边界：

- **人声音色**：只和 `--timbre-reference-vocal` 或 `音色筛选片段/` 里的筛选片段保持一致，用于干声/音色 EQ。
- **人声效果**：纵深、混响、动态、宽度、效果高频都和原曲人声 stem 对比，用最终人声贡献轨生成的 `<output>.vocal_effect_audit.json` 排查。
- **总线比例**：人声/伴奏大小只贴原曲 active vocal/accomp 比例或通用兜底，不用音色筛选片段决定。

The system has two entry points:

| Entry point | What it does |
|---|---|
| `auto_template_mix.py` | Full pipeline: analyze → select Cubase template A/B/C → residual EQ → render |
| `auto_volume_mix.py` | Volume/dynamics only, no FX — useful as a standalone pre-step |

---

## Current sync note — 2026-06-15

This version promotes reference-driven vocal spatial FX from roadmap to first rollout.

Problem being addressed:

- Some renders can keep the vocal/accompaniment loudness ratio correct but still feel too forward or too dry compared with the original song.
- The previous reference analysis recorded reverb as diagnostic metadata only, so `vocal_group_fx.dsp` always used the same RVerb/SuperTap/Shimmer constants regardless of the reference.
- Reverb/delay inference is noisy: stem leakage, long sustained notes, pads, and room spill can all make a vocal stem look wetter than it really is.

What changed:

- `analyze_reference.py` now reports reverb confidence, delay-repeat evidence, tail stability, and a vocal-stem leakage guard.
- `plan_mix_template.py` now writes `reference.overrides.spatial_fx`, with bounded RVerb/SuperTap parameters blended from the Faust baseline by confidence.
- `render_template_mix.sh` now applies that plan through `libvocal_group_fx_runtime.dylib`; metadata is written to `<output>.spatial_fx.json`. The older per-song binary builder remains as fallback.
- `auto_template_mix.py` forwards `--spatial-fx auto|off` and records the selected mode in the summary JSON.

Why this shape:

- It changes only vocal-group space, not bus balance, source EQ, master loudness, de-click, or accompaniment duck/carve policy.
- It keeps low-confidence delay near baseline and keeps shimmer hidden by default.
- It can be disabled with `--spatial-fx off` or `--no-spatial-fx` for A/B comparison and rollback.

Known issues / next checks:

- The default 1.1 path now uses `libvocal_group_fx_runtime.dylib`: the 0.1 mono-in/stereo-out and dry/early/reverb/shimmer/delay send path are fixed, while only whitelisted effect parameters are set from the resolved plan. Generated per-song DSP binaries remain as a fallback.
- Accuracy still depends on reference stem quality. Check `<output>.spatial_fx.json` for `reason`, `guards`, and confidence before trusting an unexpected wet/dry result.
- Validate baseline vs. spatial render on the target songs before widening limits, especially `炳超 - 黄昏` and `佳菲 - 阴天`.
- If a render becomes too distant, first compare with `--spatial-fx off`; do not compensate by changing vocal/accompaniment bus balance or master loudness.

---

## Current sync note — 2026-06-25

本次更新主要处理四类问题：干声毛刺/抖动、人声没劲、音色相似度不明显、以及人声整体被推得比原曲大。

最重要的目标拆分：

- **音色相似度** 只追 `音色筛选片段`，也就是筛出来的人声片段；它只进入干声/音色 EQ 和细分频谱包络校正。
- **动态、纵深、混响、宽度、效果高频** 都追 `原曲人声 stem`，也就是最终 vocal_group 应该像原曲里的人声效果，而不是像筛选片段的空间或动态。
- **响度和人声/伴奏比例** 不归音色筛选片段管，避免为了“像”而把所有歌的人声整体推大。

核心约束：

1. **总线比例不再用“弱人声自动前推目标”**
   `compute_render_bus_balance.py` 仍会记录弱/闷/缺咬字诊断，但 `weak_vocal_compensation_db` 固定不参与全局 bus target。总线只负责贴原曲 active vocal/accomp 比例或通用兜底比例，避免所有歌的人声一起变大。

2. **弱人声搬到正确阶段处理**
   `dry_vocal_strategy` 只根据干声音频特征触发动作，例如低中频过厚、presence 缺失、body/presence 失衡。它不会直接加人声音量，而是把有限的伴奏分频段动态让位请求写进 plan。

3. **伴奏 duck 侧再做硬上限**
   `apply_accomp_vocal_duck.py` 读取 `dry_vocal_strategy.duck_profile` 后，会按频段再次截顶。弱/闷/缺咬字通过伴奏局部让位解决，不通过把人声 bus 整体推大解决。

4. **“没劲”只做微动态，不改响度**
   `apply_vocal_dynamic_lift.py` 只在输入人声短帧动态、活动 RMS 或峰值明显弱于参考 stem 时启用，做小幅动态对比增强，并在脚本侧加硬上限，防止异常 plan 把人声推炸。

5. **音色相似度从 8-band 扩展到细分频谱包络**
   `analyze_reference.py` 新增 `vocal_spectral_envelope`，只在人声活动区提取、并归一到中频主体，避免响度差被当成音色差。`plan_mix_template.py` 的 timbre EQ 先用 8-band 判断大方向，再用细分包络补足更可听的差异；`apply_timbre_chain_guard.py` 在模板链后和 vocal group 后也会用细分包络轻校，避免模板链把相似度洗掉。

6. **空间和段落比例继续保守化**
   居中型参考人声的 reverb wet/time/high return 会被更严格限制，避免“比原曲湿、高频多”。局部 section balance 遇到副歌埋声时优先压伴奏、少推人声；自动音量前处理的人声段落负增益和相邻跳变也收小，减少忽大忽小。

7. **最终人声效果要和原曲人声 stem 对比**
   `audit_vocal_effect_match.py` 会把最终入 stereo sum 的人声贡献轨和原曲人声 stem 做同一活动区对比，覆盖空间/纵深、混响尾巴、delay 线索、短帧动态、效果高频和细分包络。它的职责不是裁判音色筛选片段，而是定位最终人声是否比原曲更散、更湿、更平或更亮。

8. **效果目标进入统一上下文，不按测试歌名单独调参**
   `plan_mix_template.py` 会把原曲人声 stem 的空间、混响、delay 和动态统一写入 `vocal_processing_context.vocal_effect_target`。后续 `spatial_fx`、微动态和审计都消费这个上下文；触发条件来自音频特征，例如原曲人声是否 center-led、active side/mid、短帧动态差、干声 presence 是否缺失。任何动作都有上限，不根据歌曲名或当前四首回归 case 做点对点处理。

9. **审计复用已算好的参考特征**
   `auto_template_mix.py` 调用 `audit_vocal_effect_match.py` 时会传入 `resolved_mix_plan.json`。审计脚本优先复用 plan 里的 `reference.features` 和活动人声区间，只重新分析最终人声贡献轨，避免重复跑原曲人声的动态、混响、delay 和频谱包络。

10. **禁止爆音，人声/伴奏关系只按原曲指标修**
   `apply_final_fusion_pass.py` 把原曲 active vocal/accomp gap、局部 section gap、宽度和伴奏 masking 当作核心目标，并在同一个 pass 内输出 `reference_targets -> current_errors -> corrections`。1.1 只在超过 8 dB 的全局差异上做软膝盖补偿，不做 residual gap 追满；section 只做诊断和极端救急，不再按窗口切人声或抬伴奏；duck/global 后如果 presence/air 仍比原曲更遮挡人声，只对伴奏做全曲静态小幅 residual masking trim。pass 结束后通过 `post_fusion_measure` 复测最终入 stereo sum 的关系，供排查听感/指标冲突。默认链路不再额外叠加 `compute_render_bus_balance.py` 和 `apply_section_balance_guard.py`。最终 `apply_final_transient_guard.py` 只做 loudness finalizer 后的短促高频安全闸。

11. **center-led 也进入有界 spatial 映射**
   1.1 已先注释掉旧的“center-led / RT60 高就直接禁用 spatial_fx”硬 guard。`plan_mix_template.py` 仍会把这些风险写进 `guards`，但会继续生成有上限的 per-song `vocal_group_fx` 参数；center-led 参考会自动降低 wet、width 和 delay，而不是退成 `__neutral_stereo__` 或固定 rack。

### no-template1.1 分支说明

`no-template1.1` 分支不改模板分类代码：`plan_mix_template.py` 仍负责把外部分频分类结果映射到 template A/B/C，并继续生成原来的 plan。1.1 的变化只发生在 `render_template_mix.sh` 的渲染阶段：当传入的 plan 同时包含 `reference` 和 `timbre_features` 时，渲染器把模板当作历史分类/兼容信息，不再让模板 rack 预先决定声音。

中性链的阶段顺序是：

1. **源人声清理**：`apply_vocal_plan_eq.py --eq-stage cleanup` 只吃 `source_cleanup.source_eq.vocal_eq` 和 `vocal_hf_guard`，修闷、刺、齿音、Nyquist 颗粒等素材瑕疵，不吃 `residual_vocal_eq`。
2. **参考音色塑形**：`--eq-stage timbre` 只吃音色筛选片段生成的 `timbre_vocal_eq`，随后 `apply_timbre_chain_guard.py` 用当前音频重新测量并轻校。若原曲人声 stem 显示当前干声的 upper/harsh/sib/air 已经比原曲更暗，`reference_clarity_guard` 会保护这些清晰度频段，阻止音色筛选片段继续削高频；这是通用边界，不按歌名或 case 特判。
3. **音色后安全兜底**：`--eq-stage post_timbre` 只允许高频安全保护，不重复源清理，也不把模板 residual correction 拉回来；cleanup 已做过的低通不会在 post_timbre 默认重复执行，避免参考音色的空气感被二次压暗。
4. **常规模板链**：保留模板压缩、de-ess/limiter、vocal_group 空间和伴奏处理，但它们发生在源清理/参考音色之后；模板不再先决定人声颜色。
5. **空间/纵深**：有可靠 `spatial_fx` 时通过 `libvocal_group_fx_runtime.dylib` 传入白名单参数；动态库失败时才回退到 per-song `vocal_group_fx` binary，否则回到常规 `vocal_group_fx`。
6. **伴奏让位/融合**：模板伴奏处理先提供常规基底；`apply_final_fusion_pass.py` 最后统一按原曲 active gap、原曲局部窗口和原曲伴奏 masking 目标处理人声/伴奏关系。局部窗口默认只写诊断，只有连续窗口显示当前人声明显比原曲更被埋时才极轻压伴奏；不会按窗口降低人声或抬高伴奏。
7. **母带**：跳过模板 bus EQ / GW MixCentric，只保留 loudness finalizer、L2、安全去爆点等最终保护。

这个分支的目标是把模板从“音色起点”降级为“常规处理基底”：人声颜色先由音色筛选片段决定，模板链只提供压缩、空间和伴奏常规处理；最终融合来自原曲 active vocal/accomp gap、局部窗口和 masking 误差。不同 case 会因为指标误差不同触发不同修正，但实现上不能写歌名、歌手、风格或 profile 的点对点规则；母带只做响度和安全。

2026-07-01 听感标记：`no_template_1_1_clarity_guard_20260701_v1` 这一版混响和动态听感已通过，尤其小步舞曲的空间/动态比较稳定。已知问题是阴天 1:18-1:28 附近伴奏/limiter 后会冒出 isolated sample click；它不是人声链或 spatial/fusion 自动化造成的，当前在 final transient guard 末端用极短 sample-level de-click 兜底，只修 1-8 samples 的点状毛刺，不改混响、动态或人声/伴奏比例。

排查入口：

- `<output>.final_fusion_pass.json`：确认 `reference_targets`、`current_errors`、`corrections` 是否都来自对应原曲；`section.diagnostic_event_count` 只是诊断窗口，真正改音频看 `section.audio_event_count/audio_applied`；`residual_masking_trim` 只按 presence/air 等 masking 残差做伴奏静态小修；`post_fusion_measure` 只复测最终结果，不再二次追满；`debug_profile` 只能解释，不能作为决策核心。
- `<output>.vocal_dynamic_lift.json`：查看微动态触发条件、实际增益范围和 `hard_caps`。
- `<output>.timbre_chain_guard.json` / `<output>.post_group_timbre_guard.json`：查看 8-band 与细分包络的音色回正动作；若 `skipped` 里出现 `reference_clarity_guard`，说明原曲人声 stem 要求保留清晰度，所以跳过了音色参考驱动的高频 cut。
- `<output>.vocal_group_transient_guard.json` / `<output>.final_transient_guard.json`：查看短促高频爆点是否在来源层或最终层被衰减。
- `<output>.vocal_effect_audit.json`：查看最终人声贡献轨相对原曲人声 stem 的纵深、动态、混响、宽度和效果高频误差。
- `resolved_mix_plan.json` 里的 `vocal_processing_context.vocal_effect_target`：查看效果目标来源和每个动作的通用触发证据。

---

## Current sync note — 2026-06-29

本次更新把“融合度”从多个分散模块里收口到最终融合层：先有只读决策报告，再由渲染链里的 `apply_final_fusion_pass.py` 真正落到音频。

核心原则：

- **每首歌只对齐自己的原曲。** 勇气只对齐梁静茹《勇气》，阴天只对齐莫文蔚《阴天》，黄昏只对齐周传雄《黄昏》，小步舞曲只对齐陈绮贞《小步舞曲》。
- **不按歌名、风格标签或四首测试 case 硬套参数。** `fusion_intent.profile` 只用于解释当前画像，不作为核心决策。
- **真正的决策核心是 reference target error correction。** 也就是先读原曲 reference.features，再读当前渲染报告，最后输出“参考目标 → 当前误差 → 建议修正”。

新增脚本：

- `scripts/diagnose_fusion_intent.py`：只读聚合已有报告，解释当前融合画像和冲突点。
- `scripts/plan_final_fusion_pass.py`：只读生成 final fusion pass 决策 JSON，不写音频。
- `scripts/apply_final_fusion_pass.py`：渲染时统一应用最终融合，替代默认链路里分散的 accompaniment duck、render bus balance 和 section balance guard。

典型用法：

```bash
python3 scripts/plan_final_fusion_pass.py \
  --render-dir calibration_outputs/flow_refactor_listen_20260626 \
  --out-json calibration_outputs/flow_refactor_listen_20260626/final_fusion_decisions.json \
  --out-md calibration_outputs/flow_refactor_listen_20260626/final_fusion_decisions.md
```

输出结构：

```json
{
  "reference_targets": {
    "global_active_gap_db": -1.05,
    "vocal_width": {},
    "vocal_reverb": {},
    "vocal_dynamics": {}
  },
  "current_errors": {
    "global_gap_error_db": -8.73,
    "width_error_db": 7.11,
    "reverb_rt60_error_ms": 616.3
  },
  "corrections": {
    "global_gain": {},
    "section": {},
    "duck_budget": {},
    "spatial": {}
  },
  "render_consumption": {
    "active": false
  }
}
```

注意：`plan_final_fusion_pass.py` 仍是只读报告；真正改变声音的是 `render_template_mix.sh` 里的 `apply_final_fusion_pass.py`，输出 `<output>.final_fusion_pass.json`。这样同一类融合职责只在一个位置执行，避免旧 duck、bus balance、section guard 互相覆盖。

---

## Requirements

### Toolchain

| Tool | Purpose |
|---|---|
| [Faust](https://faust.grame.fr) | Compile `.dsp` → C++ |
| `g++` / `clang++` | Compile C++ → native binary |
| `make` | Orchestrate builds |
| `ffmpeg` + `ffprobe` | Audio I/O, volume analysis, mix down |
| `libebur128` | Optional fast intermediate LUFS/LRA measurement for `--fast-loudness-steps` |
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

# Collect lightweight stage timing while rendering
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --stage-report

# Use the validated hybrid fast loudness probes for intermediate finalizer decisions
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --fast-loudness-steps pre_master,post_l2,controlled_makeup_1,controlled_makeup_2

# Point at a reference full mix (drives stem balance, source EQ, master tilt, loudness target)
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --reference-audio "ref/原曲.mp3" \
  --reference-root "ref_dataset/"

# Disable the first-rollout reference-driven vocal-group spatial FX
.venv/bin/python scripts/auto_template_mix.py vocal.wav accomp.wav final_mix.wav \
  --reference-audio "ref/原曲.mp3" \
  --spatial-fx off
```

The analyzer script location defaults to
`D:\code\spectral-mix-template-selector\spectrum_template_analyzer.py`.
Override with `--analyzer <path>`.

---

### 1b — 飞书表格批量渲染

适合按飞书表格行顺序批量生成对比混音。典型流程是：

1. 用下载脚本把表格里的音频附件落到本地目录。
2. 用批量脚本逐行调用 `auto_template_mix.py`。
3. 检查 `batch_manifest.json` / `batch_summary.json`。
4. 用上传脚本把 WAV 上传飞书云盘，并把链接写回表格 G 列。

批量渲染：

```bash
.venv/bin/python scripts/render_feishu_mix_compare_batch.py \
  --audio-root ../feishu_long_audio_screened \
  --records-json ../feishu_long_audio_screened/sheet_records.json \
  --out-dir calibration_outputs/feishu_mix_compare_C0LiHq_20260617 \
  --resume
```

默认参考策略是 `sheet-only`：只信表格记录里明确下载到的 D/G/H 列参考素材。缺参考时走通用兜底，不按本地歌名乱猜。仅本地排查时才使用：

```bash
.venv/bin/python scripts/render_feishu_mix_compare_batch.py \
  --reference-policy local-auto
```

上传并写回表格：

```bash
.venv/bin/python scripts/upload_feishu_mix_compare_results.py \
  --out-dir calibration_outputs/feishu_mix_compare_C0LiHq_20260617
```

上传脚本会维护 `feishu_uploads.json` 缓存，重复执行时优先复用已有 file token 和 URL。

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
legacy/no-reference mode:
  vocal insert chain
    → timbre/source plan stages if --mix-plan is passed
    → vocal_group_fx (baseline or reference spatial plan)
  accompaniment
    → template_music_proq3_{ab|c}
    → final fusion pass
    → amix stereo sum
    → template_bus_proq3_{ab|c} → gw_mixcentric_stereo
    → loudness finalizer

no-template1.1 reference+timbre mode:
  source vocal cleanup
    → screened timbre shaping
    → post-timbre HF safety
    → reference vocal dynamics/event guard
    → template compression / de-ess / limiter
    → reference spatial FX or regular vocal_group_fx
  accompaniment
    → template music processing
    → final fusion pass
    → amix stereo sum
    → loudness finalizer / L2 / transient safety
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
- Gain changes are applied without `atrim`/`concat`: short maps use one continuous FFmpeg `volume` expression; long maps switch to an `enable=` filter chain to avoid FFmpeg nested-expression failures
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
- **`spatial_fx`** — bounded vocal-group RVerb/SuperTap parameters derived from reference reverb/delay evidence. The render path only disables this for quality guards such as severe stem leakage, too few stable tails, or very low reverb confidence. In the 1.1 branch, center-led references or very large RT60 proxies are no longer hard-disabled; they keep generating per-song parameters with tighter wet/width/delay limits, and the bypassed legacy guard is recorded for debugging.

Master tilt safety rules (in `plan_mix_template.MASTER_TILT_*`):

| Constant | Value | Why |
|---|---|---|
| `MASTER_TILT_DEAD_BAND_DB` | 1.5 | Ignore deltas smaller than this — avoid pointless tweaks |
| `MASTER_TILT_MAX_CUT_DB` | 3.0 | Cuts can go up to 3 dB |
| `MASTER_TILT_MAX_BOOST_DB` | 0.8 | Boosts are tightly capped — master-bus boosts amplify all sources at once |
| `MASTER_TILT_MAX_ACTIONS` | 4 | Take only the 4 worst deltas |
| `harsh` (6.2 kHz), `sib` (9.5 kHz) | **cut-only** | Boosting these on a complete mix amplifies sibilance and cymbal hash. Brightness deficit must be accepted, not boosted. |

Reverb characteristics from the reference (`reverb_proxy`) now feed a reference-split
`spatial_fx` plan when reference stems are available. Rendering defaults to
`--spatial-fx auto`: if stem-quality and stability guards pass, the renderer loads
`build/libvocal_group_fx_runtime.dylib` and passes the whitelisted parameters at
runtime. If the dylib path fails, it falls back to the older generated per-song
`vocal_group_fx` binary under `build/spatial/`; otherwise it falls back to the fixed
built-in `vocal_group_fx.dsp` baseline. Center-led and suspiciously large RT60
readings are treated as mapping inputs in 1.1, not as reasons to fall back to
`__neutral_stereo__` or the fixed rack. External `delayverb` is not wired into this
pipeline yet.

### Reference spatial FX

Problem being investigated: current renders can place the vocal slightly too far
forward even when vocal/accompaniment level balance is correct. The likely cause
is not fader level, but fixed vocal-group spatial constants. Baseline values are:

| Module | Current fixed baseline |
|---|---|
| RVerb | send `-12.5 dB`, time `1.75 s`, predelay `12 ms`, return path has an additional `-6 dB` trim |
| SuperTap | send `-27 dB`, return `-18.5 dB`, dark repeats, low feedback |
| Shimmer | send `-18 dB` plus return `-18 dB`, intentionally very hidden |

The design is **v0.1 Faust I/O/send-path locked, reference-parameter spatial
planning**, not simply "more reverb". Reference analysis still produces a
`spatial_decision` contract, but 1.1 keeps the 0.1 vocal-group rack contract
fixed: mono input, stereo output, and the dry/early/reverb/shimmer/delay parallel
send path are not changed by the plan. Here “send path” only means how the dry
signal and effect returns are connected; it does **not** mean the send level is
fixed. The reference may change whitelisted
effect parameters, including send level, reverb time, predelay, early reflections,
return tone, delay feedback, and delay width. Those parameters are capped by the
`189f4a7` / v0.1 preset. In other words, the renderer can make space shorter,
narrower, darker, drier, or more controlled than v0.1, but it cannot become
wetter, wider, longer, or brighter than that preset. This keeps runtime cheap:
no second spatial pass, no segment automation, no render-audit-rerender loop.

`spatial_decision` splits the problem into separate axes:

| Axis | Meaning | Downstream control |
|---|---|---|
| `width` | center-led / near-mono / open width | delay width only, capped by v0.1; output path stays v0.1 |
| `depth` | front/back placement | predelay and early reflections, capped by v0.1 |
| `wet` | wet amount | send level can move drier, never wetter than v0.1 |
| `tail` | decay length | Used as evidence only; plugin time cannot exceed v0.1 |
| `early` | wrap/room cue | early reflection level cannot exceed v0.1 |
| `delay` | repeat/depth cue | SuperTap send/return, feedback, and width can move down, never exceed v0.1 |
| `clarity_risk` | whether space may blur diction | darker return tone, shorter time, narrower delay |

Reference analysis produces a bounded `spatial_fx` plan that drives only the vocal
group space. Current 1.1 policy prefers
`v0_1_faust_io_send_path_locked_reference_params_with_ceiling`:

| Evidence | Intended control | Safety rule |
|---|---|---|
| `tail_to_onset_ratio_db` | Reverb evidence / audit context | Can reduce send/time for dry or risky references; cannot exceed v0.1 |
| `est_rt60_ms` | Tail-risk evidence | Do not map directly to seconds; plugin time is capped at v0.1 `1.75 s` |
| `confidence`, `valid_tail_count`, `tail_iqr_db` | Whether to apply the reverb plan | Disable only when evidence is truly too weak; otherwise penalize/limit unstable tail evidence |
| `delay_proxy` | Delay evidence / guard context | Stable center-led delay can change send/return, feedback, and width within v0.1 ceilings |
| `vocal_stem_quality` | Leakage guard | Do not apply spatial mapping when inactive vocal-stem energy suggests bleed/residual |

Accuracy remains the main risk. Stem leakage, long sung notes, pad/room spill, and
delay/reverb confusion can all inflate the proxy. The plan therefore outputs both
values and confidence:

```json
{
  "spatial_fx": {
    "enabled": true,
    "applied_to_render": true,
    "policy": "v0_1_faust_io_send_path_locked_reference_params_with_ceiling",
    "confidence": 0.72,
    "reverb": {
      "send_pre_db": -12.85,
      "time_s": 1.65,
      "predelay_ms": 12.0,
      "damp": 0.32,
      "confidence": 0.78
    },
    "delay": {
      "send_pre_db": -28.5,
      "gain_db": -19.4,
      "feedback": 0.1,
      "width": 0.32,
      "confidence": 0.52
    },
    "shimmer": {
      "enabled": false,
      "confidence": 0.31
    }
  }
}
```

Renderer policy should stay one-pass and parameter-only:

```text
reference.features -> spatial_decision -> v0.1-I/O/send-path-locked Faust params -> runtime dylib -> one vocal_group_fx render
```

Current rollout policy:

1. RVerb keeps the 0.1 input/output and send path, and can reduce send below `-12.5 dB`; it cannot exceed `-12.5 dB`, `1.75 s`, `12 ms`, stronger-than-`-2 dB` early reflection, or brighter-than-`-4 dB` return tone.
2. SuperTap keeps the 0.1 input/output and send path, and can reduce send/return below `-27 dB` / `-18.5 dB`; feedback cannot exceed `0.10` and width cannot exceed `0.45`.
3. Shimmer remains hidden by default (`policy: hidden_by_default_first_rollout`).
4. Center-led references keep the dry/output path identical to 0.1; width is guarded through delay width and effect tone, not a post side-trim.
5. The runtime library path and applied parameters are recorded in `<output>.spatial_fx.json`; generated per-song binaries are fallback only.
6. The post-FX vocal bus can be exported as `<output>.vocal_group.wav` and audited against the reference vocal stem before judging vocal width.
7. Faust can later be compiled into a code-hosted plugin/dynamic component, but DAW hosting is not part of the render path.

Active vocal-width decisions use a two-stage workflow:

1. **Measurement stage**: compare `reference vocal stem` against the current
   post-FX `vocal_group` bus, using active regions detected from the reference
   vocal. The audit reports Mid/Side active lift, active side/mid ratio, L/R
   correlation, and mono fold-down loss. Full-mix Mid/Side is diagnostic only
   because accompaniment, drums, guitars, and master width can dominate side
   energy.
2. **Processing stage**: only consider a light voice-correlated side layer when
   the reference vocal stem has meaningful active side, the current `vocal_group`
   is at least `3 dB` narrower, and correlation/mono guards pass. Near-mono
   reference vocals keep the existing balance/duck/carve path; already-wide
   current vocals are left alone.

`auto_template_mix.py` runs this audit automatically when reference stems are
available for A/B/C template renders. Disable it with `--spatial-audit off`, or
keep only the exported bus with `--export-vocal-group`.

The executable second-stage hook is intentionally opt-in:
`--direct-vocal-side-layer light`. The first light preset adds a band-limited
pure-side layer from the post-source-EQ vocal (`-20 dB`, `8 ms`, `180-6500 Hz`)
before accompaniment ducking and bus-balance analysis. It should only be used
for A/B after the audit recommends `consider_light_voice_correlated_side_layer`.

Spatial work must not change:

- vocal/accompaniment bus balance;
- master loudness target or final loudness validation;
- accompaniment carve/duck policy;
- source EQ decisions;
- final global de-click behavior.

The current implementation uses a runtime-parameter dynamic library:

```text
src/vocal_group_fx.dsp
  -> build/runtime/vocal_group_fx_runtime.dsp
  -> build/libvocal_group_fx_runtime.dylib
  -> scripts/apply_vocal_group_runtime.py
```

The generated runtime DSP only converts approved constants into Faust controls.
Input/output and routing stay fixed: mono vocal in, stereo vocal group out, with
dry/early/reverb/shimmer/delay summed in the same 0.1 send path. The old
`scripts/build_spatial_vocal_group.py` per-song binary path remains as a guarded
fallback for local recovery, not the default online path.

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

`render_template_mix.sh` applies accompaniment/fusion processing in this order:

1. Template music EQ from `template_music_proq3_{ab|c}`.
2. Reference/source carve EQ from `source_eq.accomp_eq`; this is cut-only and only carves masking bands when the active vocal is behind the reference balance. The plan keeps one action per problem region, such as `presence` or `body`.
3. Final fusion pass from `apply_final_fusion_pass.py`; this uses the post-FX vocal group and post-carve accompaniment, then统一做伴奏让位、active vocal/accomp gap、参考窗口局部比例和必要的人声宽度轻收。

The fusion duck bands are low (`<180 Hz`), body (`180-1200 Hz`), presence (`1200-5000 Hz`),
and air (`>5000 Hz`). Dry-vocal strategy only provides遮挡线索；最终比例仍以每首歌自己的原曲 reference target error 为核心。

Output metadata:

| File | Meaning |
|---|---|
| `<output>.final_fusion_pass.json` | Final duck budget, active gap correction, section reference events, and safety trim |
| `<output>.loudness.json` | Master loudness, true-peak, and global de-click report |

Important constraint: this layer is for **space making**, not loudness rewriting. Overall
vocal/accompaniment balance still follows the original song's active ratio conservatively,
and final loudness stays on the master bus only.

The plan metadata for accompaniment coordination lives under:

- `source_eq.accomp_eq.actions[*].region`
- `source_eq.accomp_eq.duck_coordination`
- `<output>.final_fusion_pass.json.duck`

---

## Final fusion at render time (`apply_final_fusion_pass.py`)

Older versions split fusion across three runtime steps: accompaniment ducking,
bus-balance gain calculation, and section balance guard. Those steps could each be
reasonable in isolation, but together they made it easy to change the same vocal/accompaniment
relationship more than once.

The current render path measures the **actual post-FX vocal group** and **post-carve
accompaniment**, then applies fusion once. The decision shape is deliberately data-first:

```json
{
  "reference_targets": {
    "global_active_gap_db": "...",
    "section_gap_curve": "...",
    "vocal_width": "...",
    "vocal_reverb": "...",
    "vocal_dynamics": "...",
    "accomp_masking_bands": "..."
  },
  "current_errors": {
    "global_gap_error_db": "...",
    "post_fusion_gap_error_db": "...",
    "section_gap_error": "...",
    "width_error_db": "...",
    "duck_error": "..."
  },
  "corrections": {
    "global_gain": "...",
    "section_moves": "...",
    "duck_budget": "...",
    "residual_masking_trim": "...",
    "spatial_trim": "..."
  },
  "debug_profile": {
    "profile_usage": "debug/explain only"
  },
  "post_fusion_measure": {
    "final_active_gap_db": "...",
    "final_gap_error_db": "...",
    "final_masking_error_db": "...",
    "final_width": "..."
  }
}
```

`debug_profile.profile` can explain why a render feels “front”, “embedded”, or “narrow”,
but it is not allowed to choose the correction. The correction comes from:

```text
对应原曲目标 → 当前渲染误差 → 往对应原曲修
```

1. Render vocal through insert chain → `vocal_group_fx` → `VOCAL_GROUP`
2. Render accompaniment through music EQ / carve EQ → `ACCOMP_CHAIN_OUT`
3. Apply `apply_final_fusion_pass.py` once:
   - necessary vocal side trim against reference width
   - reference-masking-error-driven multiband accompaniment yielding
   - global active vocal/accomp gap correction with a soft knee above `8.0 dB`
   - reference-window section diagnostics, with only sustained buried-vocal rescue duck allowed
   - static residual masking trim when presence/air still mask vocal more than the original
   - final post-fusion measurement for audit only
4. Feed the resulting `VOCAL_BALANCED` and `ACCOMP_BALANCED` into `amix` at `0.0 dB`

When the vocal is behind, correction is split between vocal lift and accompaniment
attenuation. Local section windows no longer pull the vocal back or raise the
accompaniment, because those moves can make phrase-by-phrase tone and depth unstable
when a cover performance is not sample-aligned to the original. Different renders can
still receive different corrections, but only because their measured reference errors
are different, never because of hand-written song/profile branches.

| Limit | Value |
|---|---|
| Global ratio hard knee | `8.0 dB` |
| Positive soft ceiling | `9.0 dB` |
| Section diagnostic frame / hop | `8.0 s` / `2.0 s` |
| Section diagnostic deadband | `1.85 dB` |
| Section audio action | sustained buried-vocal rescue only; no vocal cut, no accompaniment boost |
| Residual masking trim | static accompaniment trim, mainly presence/air, capped per band |
| Output subtype | `FLOAT` WAV |

Output metadata: `<output>.final_fusion_pass.json`. The schema is
`final_fusion_pass.v2_2.reference_error_correction_stable_sections`.

`apply_accomp_vocal_duck.py`, `compute_render_bus_balance.py`, and
`apply_section_balance_guard.py` are retained for legacy comparison/manual debugging,
but normal `render_template_mix.sh` no longer chains them by default.

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
- **No loudnorm normalization on the output** — the finalizer does not run FFmpeg `loudnorm` dynamic normalization on the rendered file. The final FFmpeg `loudnorm` pass is kept as a measurement/validation report.
- **True-peak safety can cut, not boost** — the final safety trim only attenuates files that still measure above the TP ceiling.
- **No bus staging** — do not push level on individual buses to “make room” for mastering; stem balance stays in step 3a.
- **De-click is not loudness control** — the final global scan only interpolates very short isolated sample spikes and records the touched times in `.loudness.json`.
- **Do not merge gain into limiter stages without parity proof** — tests showed that combining controlled-makeup gain and soft limiter into one FFmpeg pass changed limiter behavior and final LUFS on `阴天`; keep these pass boundaries unless a new implementation proves sample/audible parity.

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
| `--global-declick` | `auto` | Probe for isolated clicks first; run the full repair scan only when candidates are found. Use `always` for the old full scan or `off` to skip |
| `--no-global-declick` | off | Legacy alias for `--global-declick off` |
| `--declick-threshold` | `0.6` | Residual threshold for global click detection |
| `--max-declick-samples` | `4` | Longest burst treated as an isolated click |
| `--detailed-loudness-report` | off | Also measure EBU R128 section/focus diagnostics; slower |
| `--fast-loudness-steps` | off | Experimental comma-separated fast measurement steps: `pre_master,post_l2,controlled_makeup_1,controlled_makeup_2` |
| `--compare-fast-loudness` | off | For enabled fast steps, also run FFmpeg `loudnorm` and record deltas. Diagnostic only; adds back the slow passes |

### Fast loudness measurement

The finalizer supports an experimental hybrid measurement mode for intermediate
decision points. It is designed for online latency work while preserving the final
delivery validator:

- `input_i`, `input_lra`, and `input_thresh` come from `libebur128`.
- `input_tp` comes from FFmpeg `ebur128=peak=true`, conservatively rounded upward to 0.1 dB.
- Final `output_loudnorm` remains FFmpeg `loudnorm` and must stay in place.

Recommended fast path once validated for a batch:

```bash
--fast-loudness-steps pre_master,post_l2,controlled_makeup_1,controlled_makeup_2
```

Why hybrid instead of pure `libebur128`: `阴天` showed that `libebur128` true-peak could
under-read headroom by roughly 0.3-0.6 dB at intermediate steps, which changed the
master gain decision and moved the final render by about 0.8 LU. The hybrid path keeps
LUFS/LRA speedups while using FFmpeg for true-peak-sensitive decisions.

Measured examples from `calibration_outputs/probe`:

| Song / path | Baseline finalizer | Hybrid finalizer | Final result |
|---|---:|---:|---|
| `黄昏` | ~33.5 s | ~14 s | `-12.55 LUFS`, `-0.8 dBTP` |
| `春天里` | `27.7 s` | `14.6 s` | `-12.53 LUFS`, `-1.21 dBTP` |
| `阴天` fast3 → fast4 | `19.1 s` | `15.4 s` | sample-identical output; `controlled_makeup_2` measurement replaced |

`--compare-fast-loudness` should be used before promoting a new song class or a new
measurement implementation. Do not treat the compare run as a real performance number:
it intentionally runs both fast probes and slow FFmpeg `loudnorm` passes.

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
report and optional WAV parity metrics. `--stage-report` is intentionally lightweight:
it records stage elapsed time and file paths only. Use `--stage-report-loudness` only
when you need LUFS/true-peak for every stage input/output; those measurements are
diagnostic-only, slower, and cached by file signature within the report.

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

For runtime optimization, use probe outputs under `calibration_outputs/probe/`.
Avoid `run_latest_auto_mix.py` for timing experiments unless you intentionally want to
refresh `calibration_outputs/latest/mix.wav`.

### Current runtime optimization status

Goal: keep normal online renders below roughly **40 seconds** for a full-length song
on the current local CPU path, without audible or sample-level regressions in the
mastering decisions.

Changes already validated:

| Area | Status | Notes |
|---|---|---|
| Stage report | Lite by default | `--stage-report` records elapsed time and paths only. `--stage-report-loudness` is opt-in because per-stage loudness scans were creating dozens of extra `loudnorm` passes |
| Final global de-click | Vectorized | NumPy scan/repair keeps the same isolated-sample interpolation behavior and reduced a full-file Python pass by several seconds |
| Finalizer intermediate loudness | Hybrid fast path | `libebur128` for LUFS/LRA/threshold, FFmpeg `ebur128=peak=true` for true peak, final FFmpeg `output_loudnorm` retained |
| `controlled_makeup_2` | Safe to fast-measure | On `阴天`, adding `controlled_makeup_2` to fast steps made the output sample-identical to fast3 hybrid while reducing finalizer timing from ~19.1 s to ~15.4 s |
| Volume automation long expressions | Fixed | Long per-segment gain maps switch away from deeply nested FFmpeg `if()` expressions; `春天里` no longer fails in volume automation |

Known recent measurements:

| Song | Current relevant result |
|---|---|
| `黄昏` | Fast intermediate loudness brought finalizer to ~14 s and total wall time to ~34-35 s |
| `春天里` | Previously failed in volume automation; now renders. Hybrid finalizer ~14.6 s |
| `阴天` | Baseline was under-compensated (`~ -14.8 LUFS`) because peak headroom is the bottleneck. Hybrid preserves that behavior; pure `libebur128` true peak did not |

Optimization directions still open:

| Area | Direction | Risk |
|---|---|---|
| `final_fusion_pass` | Reduce cost of multiband split/envelope/gain arrays; current timing should focus on the unified fusion pass | Medium: changes the ducking/section curve and therefore the mix |
| `vocal_group_fx` | Inspect Faust/native binary performance, compile flags, and possible DSP simplification | Medium/high: this is audible spatial FX, not just measurement |
| Finalizer copies | Only remove copies proven to be bit/sample equivalent | Medium: pass-boundary changes can alter limiter input and output |
| Final `output_loudnorm` | Keep as FFmpeg for now | High: replacing it weakens the delivery report and final TP/LUFS validation |

Legacy note: `apply_accomp_vocal_duck.py --profile-timing` still records internal read,
filter, envelope, smoothing, gain, and write timings when run manually. Recent `阴天`
timing before the final-fusion refactor showed the main costs were roughly:

| Duck sub-step | Approx. cost |
|---|---:|
| `split_bands_sosfiltfilt` | ~1.5 s |
| `envelopes_and_pressure` | ~0.7 s |
| `smooth_gain_curves` | ~0.5 s |
| `apply_gains_and_clip` | ~0.6 s |

### Runtime guardrails

Do not take these shortcuts without a parity report:

- Do **not** replace final `output_loudnorm` with an approximate meter. Intermediate decisions can be fast; final validation stays FFmpeg.
- Do **not** use pure `libebur128` true peak for headroom decisions. It under-read true peak on `阴天` intermediate files and changed final loudness.
- Do **not** merge controlled-makeup gain and the soft peak limiter into one FFmpeg pass. A trial changed `阴天` from `-14.85 LUFS` to `-14.61 LUFS` and produced nonzero waveform differences.
- Do **not** optimize by changing vocal/accompaniment bus balance or master loudness policy. Runtime work must preserve the existing mix decisions.
- Do **not** remove global de-click by default. It is useful on these renders; optimize its implementation, not its existence.

---

## Modifying DSP parameters

Most DSPs still use constants at the top of each `.dsp` file. Edit and rebuild:

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

`vocal_group_fx` is the exception in the 1.1 reference path: approved spatial
parameters are runtime controls in `build/libvocal_group_fx_runtime.dylib`.
Build it explicitly with:

```bash
make vocal-group-runtime
```

---

## Architecture

| Layer | Files | Purpose |
|---|---|---|
| DSP | `src/*.dsp` | Plugin approximations compiled to native binaries |
| Volume automation | `scripts/auto_volume_mix.py` | Rule-based level control; continuous FFmpeg gain automation with long-expression fallback |
| Template strategy | `scripts/plan_mix_template.py` | Spectral analysis → template → residual EQ plan |
| Residual EQ | `scripts/apply_residual_vocal_eq.py` | Apply plan-driven EQ between template chain and group FX |
| Spatial FX plan apply | `scripts/apply_vocal_group_runtime.py`, `scripts/build_vocal_group_runtime.py` | Load `libvocal_group_fx_runtime.dylib` and pass approved `spatial_fx` parameters at runtime |
| Spatial FX fallback | `scripts/build_spatial_vocal_group.py` | Generate/cache per-song `vocal_group_fx` binaries if the runtime dylib path fails |
| Final fusion | `scripts/apply_final_fusion_pass.py` | Render-time final vocal/accompaniment fusion: duck budget, active gap, section reference, width trim |
| Legacy/manual balance tools | `scripts/apply_accomp_vocal_duck.py`, `scripts/compute_render_bus_balance.py`, `scripts/apply_section_balance_guard.py` | Kept for comparison and debugging, no longer chained by default render |
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
                                       vocal_group_fx (baseline or reference spatial plan)
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
                                                                                  + .spatial_fx.json
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
