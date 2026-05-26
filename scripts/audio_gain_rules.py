"""
音频增益/动态处理规则引擎
========================
术语约定:
  - yuan  : 处理前的原始音频特征
  - down  : 处理后的音频特征
  - delta : 增益差值 (down = yuan + delta)
  - vo    : 人声 (vocal)
  - bc    : 伴奏 (backing / accompaniment)
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class AudioFeatures:
    rms_yuan: float
    peak_yuan: float
    rms_target: float
    peak_limit: float
    crest_factor: float
    dynamic_range: float
    is_segment_start: bool = False
    is_segment_end: bool = False
    is_stable: bool = False
    vo_rms_yuan: Optional[float] = None
    bc_rms_yuan: Optional[float] = None


@dataclass
class GainDecision:
    rule_id: str
    delta_db: float
    attack_ms: float = 100.0
    release_ms: float = 300.0
    is_fade: bool = False
    scale_factor: float = 1.0
    note: str = ""


@dataclass
class RuleThresholds:
    rms_low_offset: float = -3.0
    peak_near_limit_margin: float = 2.0
    dynamic_range_high: float = 6.0
    rms_on_target_margin: float = 1.0
    crest_factor_high: float = 12.0
    rms_high_offset: float = 3.0
    dynamic_range_stable: float = 2.0
    vo_bc_conflict_margin: float = 3.0


class GainRuleEngine:
    def __init__(self, thresholds: Optional[RuleThresholds] = None):
        self.th = thresholds or RuleThresholds()

    def _rms_diff(self, f: AudioFeatures) -> float:
        return f.rms_yuan - f.rms_target

    def _peak_near_limit(self, f: AudioFeatures) -> bool:
        return f.peak_yuan > (f.peak_limit - self.th.peak_near_limit_margin)

    def _clamp(self, value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _r1(self, f: AudioFeatures) -> Optional[GainDecision]:
        diff = self._rms_diff(f)
        if diff < self.th.rms_low_offset and not self._peak_near_limit(f):
            delta = self._clamp(-diff * 0.6, 1.0, 3.0)
            return GainDecision("R1", delta, 100.0, 300.0, note="RMS偏低且Peak余量充足，提升补偿")
        return None

    def _r2(self, f: AudioFeatures) -> Optional[GainDecision]:
        diff = self._rms_diff(f)
        if diff < self.th.rms_low_offset and self._peak_near_limit(f):
            delta = self._clamp(-diff * 0.1, 0.0, 0.5)
            return GainDecision("R2", delta, 200.0, 500.0, note="RMS偏低但Peak已高，保守处理防削波")
        return None

    def _r3(self, f: AudioFeatures) -> Optional[GainDecision]:
        if f.dynamic_range > self.th.dynamic_range_high:
            base_delta = -(self._rms_diff(f))
            delta = base_delta * 0.5
            return GainDecision("R3", delta, 200.0, 500.0, scale_factor=0.5, note="动态波动大，补偿减半避免pumping")
        return None

    def _r4(self, f: AudioFeatures) -> Optional[GainDecision]:
        if abs(self._rms_diff(f)) <= self.th.rms_on_target_margin:
            return GainDecision("R4", 0.0, note="已在目标范围，保持不动")
        return None

    def _r5(self, f: AudioFeatures) -> Optional[GainDecision]:
        if f.crest_factor > self.th.crest_factor_high:
            base_delta = max(0.0, -(self._rms_diff(f)))
            delta = base_delta * 0.3
            return GainDecision("R5", delta, 10.0, 500.0, note="瞬态过强，限制提升，快attack慢release")
        return None

    def _r6(self, f: AudioFeatures) -> Optional[GainDecision]:
        diff = self._rms_diff(f)
        if diff > self.th.rms_high_offset:
            delta = -self._clamp(diff * 0.6, 1.0, 3.0)
            return GainDecision("R6", delta, 100.0, 300.0, note="电平过高，降低避免压迫感")
        return None

    def _r7(self, f: AudioFeatures) -> Optional[GainDecision]:
        if f.dynamic_range < self.th.dynamic_range_stable:
            diff = self._rms_diff(f)
            delta = self._clamp(-diff, -1.0, 1.0)
            return GainDecision("R7", delta, 200.0, 500.0, note="信号稳定，精细微调")
        return None

    def _r8(self, f: AudioFeatures) -> Optional[GainDecision]:
        if f.is_segment_start:
            return GainDecision("R8", 0.0, 150.0, 300.0, True, note="片段开始，平滑渐入")
        return None

    def _r9(self, f: AudioFeatures) -> Optional[GainDecision]:
        if f.is_segment_end:
            return GainDecision("R9", 0.0, 100.0, 500.0, True, note="片段结束，平滑恢复")
        return None

    def _r10(self, f: AudioFeatures) -> Optional[GainDecision]:
        if f.vo_rms_yuan is not None and f.bc_rms_yuan is not None:
            gap = f.vo_rms_yuan - f.bc_rms_yuan
            if abs(gap) < self.th.vo_bc_conflict_margin:
                delta = -self._clamp(self.th.vo_bc_conflict_margin - abs(gap) + 2.0, 2.0, 4.0)
                return GainDecision("R10", delta, 80.0, 300.0, note="背景与主体冲突，伴奏压低保证人声清晰")
        return None

    def evaluate(self, f: AudioFeatures) -> GainDecision:
        pipeline = [
            self._r8,
            self._r9,
            self._r10,
            self._r5,
            self._r2,
            self._r1,
            self._r6,
            self._r4,
            self._r3,
            self._r7,
        ]
        for rule_fn in pipeline:
            decision = rule_fn(f)
            if decision is not None:
                return decision
        return GainDecision("NONE", 0.0, note="无规则命中")
