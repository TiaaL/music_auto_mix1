#!/usr/bin/env python3
"""
Automatic vocal/accompaniment loudness and dynamics shaping.

Rule preset derived from bc/vo summary:
1. vo: lift more aggressively and apply gentle dynamic convergence
2. bc: keep slightly tucked and reduce level fluctuations for glue
3. Keep accompaniment below vocal during sung sections
4. Output premixed stems and optional stereo mix
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from audio_gain_rules import AudioFeatures, GainDecision, GainRuleEngine


SILENCE_RE = re.compile(r"silence_(start|end):\s*([-0-9.]+)")
MEAN_VOL_RE = re.compile(r"mean_volume:\s*([-0-9.]+)\s*dB")
MAX_VOL_RE = re.compile(r"max_volume:\s*([-0-9.]+)\s*dB")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "bc_vo_mix_rules.json"


def command_path(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(prefix) / name
        if candidate.exists():
            return str(candidate)
    return name


FFMPEG = command_path("ffmpeg")
FFPROBE = command_path("ffprobe")


@dataclass
class CompressorConfig:
    threshold: float
    ratio: float
    attack_ms: float
    release_ms: float
    makeup: float


@dataclass
class RoleConfig:
    target_db: float
    gain_min_db: float
    gain_max_db: float
    compressor: CompressorConfig
    limiter_ceiling: float


@dataclass
class AccompanimentConfig:
    base_gain_db: float
    body_gap_db: float
    body_extra_duck_db: float
    gain_min_db: float
    gain_max_db: float
    compressor: CompressorConfig
    limiter_ceiling: float


@dataclass
class DetectionConfig:
    silence_noise_db: float
    min_silence_sec: float
    merge_gap_sec: float
    min_voiced_sec: float
    paragraph_gap_sec: float
    paragraph_pad_sec: float


@dataclass
class MixConfig:
    vocal: RoleConfig
    accompaniment: AccompanimentConfig
    detection: DetectionConfig


def default_config() -> MixConfig:
    return MixConfig(
        vocal=RoleConfig(
            target_db=-14.5,
            gain_min_db=6.0,
            gain_max_db=20.0,
            compressor=CompressorConfig(
                threshold=0.14,
                ratio=2.2,
                attack_ms=5.0,
                release_ms=90.0,
                makeup=1.0,
            ),
            limiter_ceiling=0.97,
        ),
        accompaniment=AccompanimentConfig(
            base_gain_db=-1.5,
            body_gap_db=4.5,
            body_extra_duck_db=-1.0,
            gain_min_db=-6.0,
            gain_max_db=2.0,
            compressor=CompressorConfig(
                threshold=0.10,
                ratio=1.7,
                attack_ms=20.0,
                release_ms=180.0,
                makeup=1.0,
            ),
            limiter_ceiling=0.98,
        ),
        detection=DetectionConfig(
            silence_noise_db=-38.0,
            min_silence_sec=0.20,
            merge_gap_sec=0.14,
            min_voiced_sec=0.08,
            paragraph_gap_sec=1.20,
            paragraph_pad_sec=0.08,
        ),
    )


def compressor_filter(cfg: CompressorConfig) -> str:
    return (
        "acompressor="
        f"threshold={cfg.threshold}:"
        f"ratio={cfg.ratio}:"
        f"attack={cfg.attack_ms}:"
        f"release={cfg.release_ms}:"
        f"makeup={cfg.makeup}"
    )


def limiter_filter(ceiling: float) -> str:
    return f"alimiter=limit={ceiling}"


def config_to_dict(cfg: MixConfig) -> dict[str, object]:
    return asdict(cfg)


def load_config(path: str | None) -> MixConfig:
    if not path:
        return default_config()
    data = config_to_dict(default_config())
    user_data = json.loads(Path(path).read_text(encoding="utf-8"))
    merge_config_dict(data, user_data)
    return MixConfig(
        vocal=RoleConfig(
            target_db=float(data["vocal"]["target_db"]),
            gain_min_db=float(data["vocal"]["gain_min_db"]),
            gain_max_db=float(data["vocal"]["gain_max_db"]),
            compressor=CompressorConfig(**data["vocal"]["compressor"]),
            limiter_ceiling=float(data["vocal"]["limiter_ceiling"]),
        ),
        accompaniment=AccompanimentConfig(
            base_gain_db=float(data["accompaniment"]["base_gain_db"]),
            body_gap_db=float(data["accompaniment"]["body_gap_db"]),
            body_extra_duck_db=float(data["accompaniment"]["body_extra_duck_db"]),
            gain_min_db=float(data["accompaniment"]["gain_min_db"]),
            gain_max_db=float(data["accompaniment"]["gain_max_db"]),
            compressor=CompressorConfig(**data["accompaniment"]["compressor"]),
            limiter_ceiling=float(data["accompaniment"]["limiter_ceiling"]),
        ),
        detection=DetectionConfig(**data["detection"]),
    )


def merge_config_dict(base: dict[str, object], overlay: dict[str, object]) -> None:
    for key, value in overlay.items():
        current = base.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merge_config_dict(current, value)
        else:
            base[key] = value


def write_config(path: str, cfg: MixConfig) -> None:
    Path(path).write_text(
        json.dumps(config_to_dict(cfg), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


@dataclass
class Segment:
    start: float
    end: float
    is_voice: bool
    gain_db: float = 0.0

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class SegmentDecision:
    start: float
    end: float
    gain_db: float
    rule_id: str
    note: str


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, check=check)


def ffprobe_duration(path: str) -> float:
    cp = run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            path,
        ]
    )
    return float(cp.stdout.strip())


def detect_silences(path: str, noise_db: float, min_silence: float) -> list[tuple[float, float]]:
    cmd = [
        FFMPEG,
        "-hide_banner",
        "-i",
        path,
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    cp = run(cmd, check=False)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")

    starts: list[float] = []
    intervals: list[tuple[float, float]] = []
    for kind, value in SILENCE_RE.findall(text):
        t = float(value)
        if kind == "start":
            starts.append(t)
        elif starts:
            intervals.append((starts.pop(0), t))
    return intervals


def invert_silences(duration: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not silences:
        return [(0.0, duration)]

    voice: list[tuple[float, float]] = []
    pos = 0.0
    for s0, s1 in silences:
        if s0 > pos:
            voice.append((pos, s0))
        pos = max(pos, s1)
    if pos < duration:
        voice.append((pos, duration))
    return [(max(0.0, a), min(duration, b)) for a, b in voice if b - a > 0.03]


def merge_short_gaps(voice: list[tuple[float, float]], max_gap: float) -> list[tuple[float, float]]:
    if not voice:
        return []
    merged = [list(voice[0])]
    for s, e in voice[1:]:
        prev = merged[-1]
        if s - prev[1] <= max_gap:
            prev[1] = e
        else:
            merged.append([s, e])
    return [(a, b) for a, b in merged]


def pad_intervals(
    intervals: list[tuple[float, float]],
    duration: float,
    pad: float,
) -> list[tuple[float, float]]:
    padded: list[tuple[float, float]] = []
    for idx, (start, end) in enumerate(intervals):
        left_limit = 0.0
        right_limit = duration
        if idx > 0:
            left_limit = (intervals[idx - 1][1] + start) / 2.0
        if idx + 1 < len(intervals):
            right_limit = (end + intervals[idx + 1][0]) / 2.0
        padded_start = max(left_limit, start - pad)
        padded_end = min(right_limit, end + pad)
        if padded_end - padded_start > 0.03:
            padded.append((padded_start, padded_end))
    return padded


def detect_paragraphs(
    duration: float,
    voice: list[tuple[float, float]],
    cfg: DetectionConfig,
) -> list[tuple[float, float]]:
    valid_voice = [(start, end) for start, end in voice if end - start >= cfg.min_voiced_sec]
    long_regions = merge_short_gaps(valid_voice, max_gap=cfg.paragraph_gap_sec)
    return pad_intervals(long_regions, duration, cfg.paragraph_pad_sec)


def build_timeline(duration: float, voice: list[tuple[float, float]]) -> list[Segment]:
    out: list[Segment] = []
    pos = 0.0
    for s, e in voice:
        if s > pos:
            out.append(Segment(pos, s, False, 0.0))
        out.append(Segment(s, e, True, 0.0))
        pos = e
    if pos < duration:
        out.append(Segment(pos, duration, False, 0.0))
    return [seg for seg in out if seg.duration > 0.0]


def measure_mean_volume(path: str, start: float | None = None, end: float | None = None) -> float:
    cmd = [FFMPEG, "-hide_banner"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-to", f"{end:.6f}"]
    cmd += ["-i", path, "-af", "volumedetect", "-f", "null", "-"]
    cp = run(cmd, check=False)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    m = MEAN_VOL_RE.search(text)
    if not m:
        return -60.0
    return float(m.group(1))


def measure_max_volume(path: str) -> float:
    cmd = [FFMPEG, "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-"]
    cp = run(cmd, check=False)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    m = MAX_VOL_RE.search(text)
    if not m:
        return -99.0
    return float(m.group(1))


def measure_peak_volume(path: str, start: float | None = None, end: float | None = None) -> float:
    cmd = [FFMPEG, "-hide_banner"]
    if start is not None:
        cmd += ["-ss", f"{start:.6f}"]
    if end is not None:
        cmd += ["-to", f"{end:.6f}"]
    cmd += ["-i", path, "-af", "volumedetect", "-f", "null", "-"]
    cp = run(cmd, check=False)
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    m = MAX_VOL_RE.search(text)
    if not m:
        return -99.0
    return float(m.group(1))


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def segment_features(
    path: str,
    start: float,
    end: float,
    rms_target: float,
    peak_limit: float,
    is_segment_start: bool = False,
    is_segment_end: bool = False,
    vo_rms_yuan: float | None = None,
    bc_rms_yuan: float | None = None,
) -> AudioFeatures:
    rms = measure_mean_volume(path, start, end)
    peak = measure_peak_volume(path, start, end)
    crest = peak - rms
    dynamic_range = max(0.0, crest * 0.5)
    return AudioFeatures(
        rms_yuan=rms,
        peak_yuan=peak,
        rms_target=rms_target,
        peak_limit=peak_limit,
        crest_factor=crest,
        dynamic_range=dynamic_range,
        is_segment_start=is_segment_start,
        is_segment_end=is_segment_end,
        is_stable=dynamic_range < 2.0,
        vo_rms_yuan=vo_rms_yuan,
        bc_rms_yuan=bc_rms_yuan,
    )


def decide_vocal_segments(
    path: str,
    timeline: list[Segment],
    cfg: MixConfig,
    engine: GainRuleEngine,
) -> list[SegmentDecision]:
    voiced = [s for s in timeline if s.is_voice and s.duration >= cfg.detection.min_voiced_sec]
    out: list[SegmentDecision] = []
    total = len(voiced)
    for idx, seg in enumerate(voiced):
        features = segment_features(
            path,
            seg.start,
            seg.end,
            cfg.vocal.target_db,
            -1.0,
            is_segment_start=(idx == 0),
            is_segment_end=(idx == total - 1),
        )
        decision = engine.evaluate(features)
        out.append(SegmentDecision(seg.start, seg.end, decision.delta_db, decision.rule_id, decision.note))
    return out


def decide_accompaniment_segments(
    path: str,
    vocal_path: str,
    duration: float,
    vocal_sections: list[tuple[float, float]],
    cfg: MixConfig,
    engine: GainRuleEngine,
) -> tuple[list[SegmentDecision], list[GainDecision]]:
    if not vocal_sections:
        return (
            [SegmentDecision(0.0, duration, cfg.accompaniment.base_gain_db, "BASE", "未检测到人声段落，伴奏保持基础增益")],
            [],
        )

    decisions: list[SegmentDecision] = []
    body_decisions: list[GainDecision] = []
    pos = 0.0
    for idx, (start, end) in enumerate(vocal_sections):
        if start > pos:
            label = "前奏保持基础增益" if idx == 0 else "段落间隙保持基础增益"
            decisions.append(SegmentDecision(pos, start, cfg.accompaniment.base_gain_db, "BASE", label))

        vocal_section_mean = measure_mean_volume(vocal_path, start, end)
        accomp_section_mean = measure_mean_volume(path, start, end)
        section_features = segment_features(
            path,
            start,
            end,
            vocal_section_mean - cfg.accompaniment.body_gap_db,
            -1.0,
            vo_rms_yuan=vocal_section_mean,
            bc_rms_yuan=accomp_section_mean,
        )
        section_decision = engine.evaluate(section_features)
        body_decisions.append(section_decision)
        section_gain = clamp(
            cfg.accompaniment.base_gain_db + section_decision.delta_db + cfg.accompaniment.body_extra_duck_db,
            cfg.accompaniment.gain_min_db,
            cfg.accompaniment.gain_max_db,
        )
        decisions.append(SegmentDecision(start, end, section_gain, section_decision.rule_id, section_decision.note))
        pos = end

    if pos < duration:
        decisions.append(SegmentDecision(pos, duration, cfg.accompaniment.base_gain_db, "BASE", "尾奏保持基础增益"))
    return [d for d in decisions if d.end - d.start > 0.0], body_decisions


def build_segment_filter(
    segments: list[Segment],
    post_chain: list[str] | None = None,
    apply_all_gains: bool = False,
    fade_voice_segments: bool = True,
) -> str:
    parts: list[str] = []
    labels: list[str] = []
    fade_default = 0.035

    for idx, seg in enumerate(segments):
        label = f"s{idx}"
        labels.append(f"[{label}]")
        chain = [f"atrim=start={seg.start:.6f}:end={seg.end:.6f}", "asetpts=PTS-STARTPTS"]
        if apply_all_gains or seg.is_voice:
            chain.append(f"volume={seg.gain_db:.4f}dB")
        if fade_voice_segments and seg.is_voice:
            fade = min(fade_default, max(0.0, seg.duration / 4.0))
            if fade > 0.005 and seg.duration > fade * 2.2:
                chain.append(f"afade=t=in:st=0:d={fade:.4f}")
                chain.append(f"afade=t=out:st={seg.duration - fade:.4f}:d={fade:.4f}")
        parts.append(f"[0:a]{','.join(chain)}[{label}]")

    post = ""
    if post_chain:
        post = "," + ",".join(post_chain)
    parts.append(f"{''.join(labels)}concat=n={len(labels)}:v=0:a=1{post}[out]")
    return ";".join(parts)


def render_vocal(
    input_path: str,
    output_path: str,
    segments: list[Segment],
    cfg: MixConfig,
) -> None:
    fc = build_segment_filter(
        segments,
        [
            compressor_filter(cfg.vocal.compressor),
            limiter_filter(cfg.vocal.limiter_ceiling),
        ],
    )
    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-i",
            input_path,
            "-filter_complex",
            fc,
            "-map",
            "[out]",
            "-ac",
            "1",
            output_path,
        ]
    )


def render_volume_only(input_path: str, output_path: str, gain_db: float) -> None:
    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-i",
            input_path,
            "-af",
            f"volume={gain_db:.4f}dB",
            output_path,
        ]
    )


def render_segmented_volume(
    input_path: str,
    output_path: str,
    segments: list[Segment],
    cfg: MixConfig,
) -> None:
    fc = build_segment_filter(
        segments,
        [
            compressor_filter(cfg.accompaniment.compressor),
            limiter_filter(cfg.accompaniment.limiter_ceiling),
        ],
        apply_all_gains=True,
        fade_voice_segments=False,
    )
    run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-i",
            input_path,
            "-filter_complex",
            fc,
            "-map",
            "[out]",
            output_path,
        ]
    )


def mix_tracks(vocal_path: str, accomp_path: str, output_path: str) -> None:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        mix_tmp = tmp.name
    try:
        run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-i",
                vocal_path,
                "-i",
                accomp_path,
                "-filter_complex",
                "[0:a]pan=stereo|c0=c0|c1=c0[v];[v][1:a]amix=inputs=2:normalize=0[m]",
                "-map",
                "[m]",
                mix_tmp,
            ]
        )
        max_db = measure_max_volume(mix_tmp)
        final_gain = 0.0
        if max_db > -1.0:
            final_gain = -1.0 - max_db
        render_volume_only(mix_tmp, output_path, final_gain)
    finally:
        if os.path.exists(mix_tmp):
            os.unlink(mix_tmp)


def process(
    vocal_in: str,
    accomp_in: str,
    out_mix: str,
    out_vocal: str,
    out_accomp: str,
    cfg: MixConfig,
) -> None:
    engine = GainRuleEngine()
    duration = ffprobe_duration(vocal_in)
    silences = detect_silences(
        vocal_in,
        noise_db=cfg.detection.silence_noise_db,
        min_silence=cfg.detection.min_silence_sec,
    )
    voice = invert_silences(duration, silences)
    voice = merge_short_gaps(voice, max_gap=cfg.detection.merge_gap_sec)
    paragraphs = detect_paragraphs(duration, voice, cfg.detection)
    timeline = build_timeline(duration, voice)

    vocal_decisions = decide_vocal_segments(vocal_in, timeline, cfg, engine)
    vocal_map = {(d.start, d.end): d for d in vocal_decisions}
    for seg in timeline:
        decision = vocal_map.get((seg.start, seg.end))
        if decision is not None:
            seg.gain_db = decision.gain_db

    render_vocal(vocal_in, out_vocal, timeline, cfg)

    voiced = [s for s in timeline if s.is_voice and s.duration >= cfg.detection.min_voiced_sec]
    accomp_decisions, body_decision = decide_accompaniment_segments(
        accomp_in,
        out_vocal,
        duration,
        paragraphs,
        cfg,
        engine,
    )
    accomp_timeline = [Segment(d.start, d.end, True, d.gain_db) for d in accomp_decisions]

    render_segmented_volume(accomp_in, out_accomp, accomp_timeline, cfg)
    if out_mix:
        mix_tracks(out_vocal, out_accomp, out_mix)

    print("Voice segments:", len(voiced))
    print("Vocal paragraphs:", len(paragraphs))
    if paragraphs:
        preview = ", ".join(f"{start:.2f}-{end:.2f}s" for start, end in paragraphs[:8])
        suffix = " ..." if len(paragraphs) > 8 else ""
        print(f"Paragraph ranges: {preview}{suffix}")
    if vocal_decisions:
        gains = [d.gain_db for d in vocal_decisions]
        print(f"Vocal gain range: {min(gains):.2f} dB to {max(gains):.2f} dB")
        rules = ", ".join(sorted({d.rule_id for d in vocal_decisions}))
        print(f"Vocal rules hit: {rules}")
    print(f"Vocal rule target: {cfg.vocal.target_db:.2f} dBFS body RMS")
    print(f"Vocal compressor ratio: {cfg.vocal.compressor.ratio:.2f}")
    print(f"Accompaniment base gain: {cfg.accompaniment.base_gain_db:.2f} dB")
    body_gains = [d.gain_db for d in accomp_decisions if d.rule_id != "BASE"]
    if body_gains:
        print(f"Accompaniment paragraph gain range: {min(body_gains):.2f} dB to {max(body_gains):.2f} dB")
    body_rules = ", ".join(sorted({d.rule_id for d in body_decision})) if body_decision else "BASE"
    print(f"Accompaniment paragraph rules hit: {body_rules}")
    print(f"Accompaniment compressor ratio: {cfg.accompaniment.compressor.ratio:.2f}")
    print(f"Lead-in/out accompaniment gain: {cfg.accompaniment.base_gain_db:.2f} dB")
    print(f"Vocal out: {out_vocal}")
    print(f"Accompaniment out: {out_accomp}")
    if out_mix:
        print(f"Mix out: {out_mix}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatic vocal/accompaniment loudness and dynamics mixer")
    parser.add_argument("vocal_in")
    parser.add_argument("accomp_in")
    parser.add_argument("mix_out", nargs="?", default="")
    parser.add_argument("--vocal-out", default="/tmp/auto_vocal.wav")
    parser.add_argument("--accomp-out", default="/tmp/auto_accomp.wav")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--export-default-config", help="Write the default rule table JSON and exit")
    parser.add_argument("--vo-target-db", type=float, help="Override vocal target body RMS in dBFS")
    parser.add_argument("--vo-ratio", type=float, help="Override vocal compressor ratio")
    parser.add_argument("--bc-base-gain-db", type=float, help="Override accompaniment base gain in dB")
    parser.add_argument("--bc-gap-db", type=float, help="Override accompaniment gap below vocal in dB")
    parser.add_argument("--bc-ratio", type=float, help="Override accompaniment compressor ratio")
    args = parser.parse_args()

    if args.export_default_config:
        write_config(args.export_default_config, default_config())
        print(f"Default config written to: {args.export_default_config}")
        return 0

    cfg = load_config(args.config if args.config else None)
    if args.vo_target_db is not None:
        cfg.vocal.target_db = args.vo_target_db
    if args.vo_ratio is not None:
        cfg.vocal.compressor.ratio = args.vo_ratio
    if args.bc_base_gain_db is not None:
        cfg.accompaniment.base_gain_db = args.bc_base_gain_db
    if args.bc_gap_db is not None:
        cfg.accompaniment.body_gap_db = args.bc_gap_db
    if args.bc_ratio is not None:
        cfg.accompaniment.compressor.ratio = args.bc_ratio

    process(args.vocal_in, args.accomp_in, args.mix_out, args.vocal_out, args.accomp_out, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
