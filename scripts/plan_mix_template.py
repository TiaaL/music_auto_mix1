#!/usr/bin/env python3
"""根据频谱分析 JSON 生成最终混音 plan。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "config" / "cubase_templates"
EXTRACTED_PRESET_DIR = ROOT / "config" / "extracted_vstpresets"
RESIDUAL_EQ_CONFIG = ROOT / "config" / "residual_vocal_eq_rules.json"
FEATURE_TARGETS = ROOT / "config" / "template_feature_targets.json"

LABEL_TO_TEMPLATE = {
    "template_A": "template_a",
    "template_B": "template_b",
    "template_C": "template_c",
    "template_a": "template_a",
    "template_b": "template_b",
    "template_c": "template_c",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def is_enabled(link: Any) -> bool:
    if link is None:
        return False
    if isinstance(link, str):
        return True
    if isinstance(link, dict):
        return bool(link.get("enabled", True)) and bool(link.get("preset_json"))
    return False


def preset_json_name(link: Any) -> str | None:
    if isinstance(link, str):
        return link
    if isinstance(link, dict):
        value = link.get("preset_json")
        return value if isinstance(value, str) else None
    return None


def enrich_chain(chain: list[dict[str, Any]], section_links: Any) -> list[dict[str, Any]]:
    """把 Cubase 模板里的插件链和已导出的 preset JSON 关联起来。"""
    resolved = []
    for item in chain:
        name = item.get("plugin_name") or item.get("processor")
        link = getattr(section_links, name, None) if not isinstance(section_links, dict) else section_links.get(name)
        enabled = item.get("enabled", True)
        if isinstance(link, dict) and link.get("enabled") is False:
            enabled = False
        preset_file = preset_json_name(link)
        enriched = dict(item)
        enriched["enabled"] = bool(enabled)
        if preset_file:
            enriched["preset_json"] = str((EXTRACTED_PRESET_DIR / preset_file).relative_to(ROOT)).replace("\\", "/")
        elif isinstance(link, dict) and link.get("reason"):
            enriched["skip_reason"] = link["reason"]
        elif link is None and "plugin_name" in item:
            enriched["skip_reason"] = "no active exported preset link"
        resolved.append(enriched)
    return resolved


def select_template(analysis: dict[str, Any], fallback: str = "template_d") -> tuple[str, str]:
    """根据外部分类器标签选择 A/B/C；识别失败时回落到 legacy template_d。"""
    label = analysis.get("classification", {}).get("label")
    template_id = LABEL_TO_TEMPLATE.get(label or "", fallback)
    return template_id, label or ""


def residual_action_gain(rule: dict[str, Any], deviation: dict[str, Any]) -> float | None:
    action = str(deviation.get("action") or "")
    if action not in {"cut", "boost"}:
        return None
    allowed_actions = set(rule.get("actions", []))
    if action not in allowed_actions:
        return None
    suggested = abs(float(deviation.get("suggested_db") or 0.0))
    if suggested <= 0.0:
        return None
    if action == "cut":
        amount = clamp(suggested, float(rule.get("min_cut_db", 0.5)), float(rule.get("max_cut_db", 3.0)))
        return -round(amount, 2)
    amount = clamp(suggested, float(rule.get("min_boost_db", 0.5)), float(rule.get("max_boost_db", 1.5)))
    return round(amount, 2)


def append_residual_action(actions: list[dict[str, Any]], action: dict[str, Any]) -> None:
    """同一 band/type 只保留一个动作，保留幅度更大的那个。"""
    for index, existing in enumerate(actions):
        if existing.get("band") == action.get("band") and existing.get("type") == action.get("type"):
            if abs(float(action.get("gain_db", 0.0))) > abs(float(existing.get("gain_db", 0.0))):
                actions[index] = action
            return
    actions.append(action)


def classification_hits(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """展开分类器命中的规则，并标记它们是否属于最终选中的模板。"""
    classification = analysis.get("classification", {})
    selected_label = str(classification.get("label") or "")
    out: list[dict[str, Any]] = []
    for label, data in classification.items():
        if not isinstance(data, dict):
            continue
        for rule in data.get("hit_rules", []):
            out.append({
                "rule": rule,
                "classification_label": label,
                "strong": False,
                "selected_template_rule": label == selected_label,
            })
        for rule in data.get("strong_rules", []):
            out.append({
                "rule": rule,
                "classification_label": label,
                "strong": True,
                "selected_template_rule": label == selected_label,
            })
    return out


def template_to_label(template_id: str) -> str:
    if template_id.startswith("template_") and len(template_id) == len("template_a"):
        return f"template_{template_id[-1].upper()}"
    return template_id


def feature_rule_action(
    feature_rule: str,
    hit: dict[str, Any],
    rule: dict[str, Any],
    source: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "band": rule["band"],
        "type": rule["type"],
        "freq_hz": rule["freq_hz"],
        "q": rule["q"],
        "gain_db": round(float(rule["gain_db"]), 2),
        "source": source,
        "feature_rule": feature_rule,
        "classification_label": hit.get("classification_label"),
        "strong": bool(hit.get("strong") or rule.get("strong")),
        "reason": reason,
    }


def spectral_deviation_residual_actions(
    analysis: dict[str, Any],
    coverage: set[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """把模板未覆盖的频谱目标偏差转成 residual EQ 动作。"""
    band_rules: dict[str, Any] = config.get("band_rules", {})
    threshold_db = float(config.get("deviation_threshold_db", 1.5))
    actions: list[dict[str, Any]] = []
    for band, dev_data in analysis.get("spectral_deviation", {}).items():
        if band in coverage:
            continue
        action_type = str(dev_data.get("action") or "")
        if action_type not in {"cut", "boost"}:
            continue
        suggested = abs(float(dev_data.get("suggested_db") or 0.0))
        if suggested < threshold_db:
            continue
        rule = band_rules.get(band)
        if not rule or action_type not in rule.get("actions", []):
            continue
        if action_type == "cut":
            amount = clamp(suggested, float(rule.get("min_cut_db", 0.5)), float(rule.get("max_cut_db", 3.0)))
            gain = -round(amount, 2)
        else:
            amount = clamp(suggested, float(rule.get("min_boost_db", 0.5)), float(rule.get("max_boost_db", 1.5)))
            gain = round(amount, 2)
        actions.append({
            "band": band,
            "type": action_type,
            "freq_hz": float(rule["freq_hz"]),
            "q": float(rule["q"]),
            "gain_db": gain,
            "source": "spectral_deviation",
            "feature_rule": f"spectral_{band}_{action_type}",
            "classification_label": None,
            "strong": abs(gain) >= 2.0,
            "reason": (
                f"{band} deviates {dev_data.get('deviation_db', 0.0):.1f} dB from target curve; "
                "not covered by selected template"
            ),
        })
    return actions


def ratio_excess_residual_actions(
    analysis: dict[str, Any],
    coverage: set[str],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """把模板未覆盖、超出中性范围的 ratio 频段转成 residual EQ 动作。"""
    band_rules: dict[str, Any] = config.get("band_rules", {})
    excess_threshold = float(config.get("ratio_excess_threshold", 0.04))
    if not FEATURE_TARGETS.exists():
        return []
    targets = load_json(FEATURE_TARGETS)
    neutral_ranges: dict[str, list[float]] = targets.get("neutral_ratio_ranges", {})
    actions: list[dict[str, Any]] = []
    for band, value in analysis.get("ratios", {}).items():
        if band in coverage or band not in neutral_ranges or band not in band_rules:
            continue
        lo, hi = float(neutral_ranges[band][0]), float(neutral_ranges[band][1])
        ratio = float(value)
        rule = band_rules[band]
        if ratio > hi and (ratio - hi) >= excess_threshold and "cut" in rule.get("actions", []):
            excess = ratio - hi
            amount = clamp(
                excess * 15.0,
                float(rule.get("min_cut_db", 0.5)),
                min(float(rule.get("max_cut_db", 3.0)), 2.0),
            )
            actions.append({
                "band": band,
                "type": "cut",
                "freq_hz": float(rule["freq_hz"]),
                "q": float(rule["q"]),
                "gain_db": -round(amount, 2),
                "source": "ratio_excess",
                "feature_rule": f"ratio_{band}_high",
                "classification_label": None,
                "strong": (ratio - hi) >= excess_threshold * 2,
                "reason": (
                    f"{band} ratio {ratio:.3f} > {hi} neutral ceiling; not covered by selected template"
                ),
            })
        elif ratio < lo and (lo - ratio) >= excess_threshold and "boost" in rule.get("actions", []):
            deficit = lo - ratio
            amount = clamp(
                deficit * 12.0,
                float(rule.get("min_boost_db", 0.5)),
                min(float(rule.get("max_boost_db", 1.5)), 1.5),
            )
            actions.append({
                "band": band,
                "type": "boost",
                "freq_hz": float(rule["freq_hz"]),
                "q": float(rule["q"]),
                "gain_db": round(amount, 2),
                "source": "ratio_excess",
                "feature_rule": f"ratio_{band}_low",
                "classification_label": None,
                "strong": False,
                "reason": (
                    f"{band} ratio {ratio:.3f} < {lo} neutral floor; not covered by selected template"
                ),
            })
    return actions


def filter_residual_tone_shaping_boosts(
    actions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """禁止 residual 阶段做 boost，保证这里只移除已检测到的问题。"""
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for action in actions:
        # 提升属于“重塑音色”：可能把歌手变得比原干声更亮或更厚。
        # 清理阶段只做减法，避免改掉干声本来的特点。
        if str(action.get("type") or "") == "boost":
            suppressed.append({
                **action,
                "reason": (
                    f"{action.get('reason', '')}; 已按保留素材特点策略禁用 boost"
                ).strip("; "),
            })
        else:
            kept.append(action)
    return kept, suppressed


def filter_residual_high_boosts(
    actions: list[dict[str, Any]],
    ref_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """高频 boost 需要二次保护，避免把分离噪声/齿音继续推亮。"""
    ref_vocal = (ref_features or {}).get("vocal_tonal_balance") or {}
    input_vocal = (input_features or {}).get("vocal_tonal_balance") or {}
    safety = high_frequency_safety(analysis, input_vocal)
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    for action in actions:
        band = str(action.get("band") or "")
        action_type = str(action.get("type") or "")
        if action_type != "boost" or band not in {"upper", "air"} or not ref_vocal or not input_vocal:
            kept.append(action)
            continue
        delta = float(ref_vocal.get(band, 0.0)) - float(input_vocal.get(band, 0.0))
        cap, reason = vocal_high_boost_cap(
            band, template_id, delta, ref_vocal, input_vocal, analysis, safety
        )
        if cap <= 0.0:
            suppressed.append({
                **action,
                "reason": reason or "high-frequency boost suppressed by safety policy",
                "high_frequency_safety": safety,
            })
            continue
        adjusted = dict(action)
        original_gain = float(adjusted.get("gain_db") or 0.0)
        adjusted["gain_db"] = round(min(original_gain, cap), 2)
        adjusted["high_frequency_safety"] = safety
        if adjusted["gain_db"] < original_gain:
            adjusted["reason"] = f"{adjusted.get('reason', '')}; capped by high-frequency safety policy"
        kept.append(adjusted)
    return kept, suppressed


def build_residual_vocal_eq(
    analysis: dict[str, Any],
    template_id: str,
    ref_features: dict[str, Any] | None = None,
    input_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """生成模板后置 residual EQ，只补模板没覆盖或强命中的问题。"""
    if not RESIDUAL_EQ_CONFIG.exists() or template_id == "template_d":
        return {"enabled": False, "actions": [], "reason": "no residual EQ config or fallback template"}

    config = load_json(RESIDUAL_EQ_CONFIG)
    coverage = set(config.get("template_coverage", {}).get(template_id, []))
    coverage_details = config.get("template_coverage_details", {}).get(template_id, [])
    feature_rules = config.get("feature_rule_map", {})
    covered_strong_policy = config.get("covered_strong_policy", {})
    selected_label = template_to_label(template_id)
    actions: list[dict[str, Any]] = []
    covered_strong_actions: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for hit in classification_hits(analysis):
        if hit.get("classification_label") != selected_label:
            ignored.append({
                "feature_rule": hit.get("rule"),
                "classification_label": hit.get("classification_label"),
                "reason": "non_selected_template_hit",
                "selected_template": template_id,
                "strong": bool(hit.get("strong")),
            })
            continue
        feature_rule = str(hit.get("rule") or "")
        rule = feature_rules.get(feature_rule)
        if not rule:
            continue
        band = rule.get("band")
        if band in coverage:
            if hit.get("strong") and covered_strong_policy.get("enabled", True):
                scaled_gain = clamp(
                    abs(float(rule["gain_db"])) * float(covered_strong_policy.get("gain_scale", 0.5)),
                    0.5,
                    float(covered_strong_policy.get("max_abs_gain_db", 1.2)),
                )
                strong_rule = dict(rule)
                strong_rule["gain_db"] = -scaled_gain if float(rule["gain_db"]) < 0 else scaled_gain
                append_residual_action(
                    covered_strong_actions,
                    feature_rule_action(
                        feature_rule,
                        hit,
                        strong_rule,
                        "covered_strong_hit",
                        f"{feature_rule} is strong; selected template covers {band}, so add a small reinforcement move",
                    ),
                )
            else:
                ignored.append({
                    "feature_rule": feature_rule,
                    "band": band,
                    "reason": "covered_by_selected_template",
                    "template": template_id,
                    "strong": bool(hit.get("strong")),
                })
            continue
        append_residual_action(
            actions,
            feature_rule_action(
                feature_rule,
                hit,
                rule,
                "uncovered_feature_hit",
                f"{feature_rule} hit in selected template but {band} is not covered by {template_id}",
            ),
        )

    # 来源 2：目标曲线偏差，只处理模板没覆盖的频段。
    for action in spectral_deviation_residual_actions(analysis, coverage, config):
        append_residual_action(actions, action)

    # 来源 3：ratio 超出中性范围，也只处理模板没覆盖的频段。
    for action in ratio_excess_residual_actions(analysis, coverage, config):
        append_residual_action(actions, action)

    max_actions = int(config.get("max_actions", 4))
    actions = (actions + covered_strong_actions)[:max_actions]
    actions, suppressed_tone_boosts = filter_residual_tone_shaping_boosts(actions)
    actions, suppressed_high_boosts = filter_residual_high_boosts(
        actions, ref_features, input_features, analysis, template_id
    )
    return {
        "enabled": bool(actions),
        "mode": "post_template_pre_group_fx",
        "policy": (
            "base template first, then second-pass all detected features; "
            "uncovered hits (classification + spectral deviation + ratio excess) get corrective EQ, "
            "covered strong hits get small reinforcement"
        ),
        "selected_template": template_id,
        "covered_bands": sorted(coverage),
        "coverage_details": coverage_details,
        "actions": actions,
        "ignored": ignored,
        "suppressed_high_boosts": suppressed_high_boosts,
        "suppressed_tone_shaping_boosts": suppressed_tone_boosts,
        "diagnostic_only_sources": config.get("diagnostic_only_sources", []),
    }


MASTER_TILT_BANDS = {
    "sub":    {"freq_hz": 60.0,    "q": 0.7, "actions": ("cut", "boost")},
    "low":    {"freq_hz": 130.0,   "q": 0.7, "actions": ("cut", "boost")},
    "lowmid": {"freq_hz": 320.0,   "q": 0.7, "actions": ("cut", "boost")},
    "mid":    {"freq_hz": 800.0,   "q": 0.7, "actions": ("cut", "boost")},
    "upper":  {"freq_hz": 2800.0,  "q": 0.7, "actions": ("cut", "boost")},
    "harsh":  {"freq_hz": 6200.0,  "q": 1.0, "actions": ("cut",)},
    "sib":    {"freq_hz": 9500.0,  "q": 1.2, "actions": ("cut",)},
    "air":    {"freq_hz": 14000.0, "q": 0.7, "actions": ("cut",)},
}

MASTER_TILT_DEAD_BAND_DB = 1.5
MASTER_TILT_MAX_CUT_DB = 5.0
MASTER_TILT_MAX_BOOST_DB = 0.8
MASTER_TILT_MAX_ACTIONS = 4
BUS_BALANCE_MAX_MOVE_DB = 6.0
BUS_BALANCE_DEAD_BAND_DB = 0.6
FINAL_LOUDNESS_MIN_LUFS = -13.0
FINAL_LOUDNESS_MAX_LUFS = -11.0
VOCAL_DYNAMIC_RANGE_WEAK_DB = 2.0
VOCAL_DYNAMIC_MICRO_WEAK_DB = 1.0
VOCAL_DYNAMIC_MICRO_P99_WEAK_DB = 2.4
# “没劲”处理只允许增强短帧重音/微动态，不能把全局人声 bus 往前推。
# 上限稍放宽，但仍由脚本侧二次硬截顶，避免 plan 异常导致忽大忽小。
VOCAL_DYNAMIC_MAX_LIFT_DB = 2.1
VOCAL_DYNAMIC_MAX_CUT_DB = 0.55
VOCAL_DYNAMIC_MAX_CONTRAST = 0.38
DRY_VOCAL_DUCK_CAPS_DB = {
    # 弱/闷/缺咬字的人声只请求伴奏让位，不改全局人声电平。
    # 这里是 plan 侧上限；渲染脚本里还会再做一次硬上限保护。
    "low_extra_db": 0.35,
    "body_extra_db": 0.50,
    "presence_extra_db": 1.20,
    "air_extra_db": 0.40,
}

SOURCE_VOCAL_EQ_BANDS = {
    "low":    {"freq_hz": 130.0,   "q": 0.8, "actions": ("cut",)},
    "lowmid": {"freq_hz": 320.0,   "q": 0.9, "actions": ("cut", "boost")},
    "mid":    {"freq_hz": 800.0,   "q": 0.8, "actions": ("cut", "boost")},
    "upper":  {"freq_hz": 2800.0,  "q": 0.9, "actions": ("cut", "boost")},
    # harsh/sib 默认仍以 cut 为主；只有音色筛选片段明确更亮且安全门控通过时，才允许极小幅 boost。
    "harsh":  {"freq_hz": 6200.0,  "q": 1.2, "actions": ("cut", "boost")},
    "sib":    {"freq_hz": 9500.0,  "q": 1.4, "actions": ("cut", "boost")},
    "air":    {"freq_hz": 14000.0, "q": 0.7, "actions": ("cut", "boost")},
}

ACCOMP_CARVE_BANDS = {
    "lowmid": {"freq_hz": 320.0,  "q": 0.9, "weight": 0.7},
    "mid":    {"freq_hz": 800.0,  "q": 0.8, "weight": 0.9},
    "upper":  {"freq_hz": 2800.0, "q": 0.9, "weight": 1.0},
    "harsh":  {"freq_hz": 6200.0, "q": 1.2, "weight": 0.6},
}

VOCAL_SOURCE_EQ_DEAD_BAND_DB = 1.2
VOCAL_SOURCE_EQ_MAX_CUT_DB = 3.2
VOCAL_SOURCE_EQ_MAX_BOOST_DB = 1.5
VOCAL_SOURCE_EQ_MAX_ACTIONS = 5
VOCAL_SOURCE_EQ_SEVERE_CUT_CAP_DB = {
    "low": 5.5,
    "lowmid": 5.2,
    "upper": 4.0,
    "harsh": 5.2,
    "sib": 4.8,
}
VOCAL_SOURCE_EQ_SEVERE_DELTA_DB = 8.0
# --- 历史兼容：绝对电平参考锚定人声 EQ ---
# 旧逻辑曾用“输入人声 - 参考人声”的绝对 active band 电平来判断 cut/boost。
# 这段常量保留给旧 plan 兼容；新的 source_cleanup 不再用参考曲塑形。
VOCAL_ABS_EQ_DEAD_BAND_DB = 1.5          # |input-ref| 小于这个值时忽略
VOCAL_ABS_EQ_CUT_FRACTION = 0.50         # 旧逻辑：削掉 excess 的比例
VOCAL_ABS_EQ_BOOST_FRACTION = 0.35       # 旧逻辑：补回 deficit 的比例
# 旧逻辑的分频段 cut 上限；高频保持轻量，避免削出鼻/空心感。
VOCAL_ABS_EQ_MAX_CUT_DB = {
    "low": 5.5,
    "lowmid": 4.5,
    "mid": 2.5,
    "upper": 2.0,
    "harsh": 2.5,
    "sib": 2.5,
}
# 历史兼容常量：旧参考驱动逻辑里允许轻微补回的频段；新 source_cleanup 不再使用。
VOCAL_ABS_EQ_BOOST_BANDS = ("upper", "harsh")
VOCAL_ABS_EQ_MAX_BOOST_DB = {
    "upper": 1.5,
    "harsh": 1.0,
}
TIMBRE_EQ_DEAD_BAND_DB = 1.4
TIMBRE_EQ_MAX_ACTIONS = 4
TIMBRE_EQ_TOTAL_MAX_ACTIONS = 6
TIMBRE_EQ_CUT_FRACTION = 0.45
TIMBRE_EQ_BOOST_FRACTION = 0.40
TIMBRE_EQ_MAX_CUT_DB = {
    "low": 2.0,
    "lowmid": 1.8,
    "mid": 1.2,
    "upper": 1.4,
    "harsh": 1.2,
    "sib": 1.0,
    "air": 1.0,
}
TIMBRE_EQ_MAX_BOOST_DB = {
    "lowmid": 1.0,
    "mid": 0.8,
    "upper": 1.4,
    "harsh": 0.8,
    "sib": 0.7,
    "air": 0.5,
}
# 音色参考 boost 的门槛比 cut 高：只有“明显少了”才补，避免把分离噪声/齿音推出来。
TIMBRE_BOOST_MIN_DELTA_DB = {
    "lowmid": 2.2,
    "mid": 2.0,
    "upper": 2.0,
    "harsh": 2.0,
    "sib": 2.5,
    "air": 8.0,
}
# 高频如果本来已经尖，就算筛选片段更亮也不追；后面的高频保护仍会再兜底。
TIMBRE_BOOST_PEAK_LIMIT_DB = {
    "upper": 14.0,
    "harsh": 8.5,
    "sib": 8.0,
}
TIMBRE_BOOST_INPUT_LEVEL_LIMIT_DB = {
    "harsh": -9.0,
    "sib": -15.0,
}
TIMBRE_ENVELOPE_DEAD_BAND_DB = 1.0
TIMBRE_ENVELOPE_GAIN_FRACTION = 0.34
TIMBRE_ENVELOPE_MAX_ACTIONS = 3
TIMBRE_ENVELOPE_MIN_GAIN_DB = 0.18
TIMBRE_ENVELOPE_MAX_CUT_DB = {
    "low": 0.6,
    "lowmid": 0.8,
    "mid": 0.65,
    "upper": 0.95,
    "harsh": 0.75,
    "sib": 0.55,
    "air": 0.45,
}
TIMBRE_ENVELOPE_MAX_BOOST_DB = {
    "lowmid": 0.65,
    "mid": 0.60,
    "upper": 0.85,
    "harsh": 0.45,
    "sib": 0.35,
}
TIMBRE_ENVELOPE_Q = 1.15
# --- 自驱动人声清理 EQ（不匹配参考曲音色） ---
# 不把干声往参考人声的音色塑形，只处理干声自身的问题：
# 低频/低中频堆积，以及刺耳/齿音频段的异常突出。
# 判断方式是把每个频段都锚定到干声自己的 mid，再和通用干净人声阈值比较。
# 过量值 = (band - mid) - 中性基准[band]；
# 只有真的超过预期形状时才 cut，整个过程不查询参考曲。
VOCAL_SELF_EQ_NEUTRAL_OFFSET_DB = {
    # 干净人声里各频段相对 mid 的预期值；超过它才算素材自身的过量。
    "low": -6.0,     # 低频应明显低于 mid；太接近会有低频轰/闷
    "lowmid": -2.0,  # 轻微箱感正常；再高就是低中频堆积
    "harsh": -3.0,   # 存在感共振超过这里容易刺
    "sib": -8.0,     # 齿音应明显低于 mid
}
VOCAL_SELF_EQ_DEAD_BAND_DB = 1.5        # 小于这个幅度不处理，避免过度修
VOCAL_SELF_EQ_CUT_FRACTION = 0.50       # 只削掉自身 excess 的一部分，保留原特点
VOCAL_SELF_EQ_MAX_CUT_DB = {
    "low": 5.5,
    "lowmid": 4.0,
    "harsh": 2.5,
    "sib": 2.5,
}

VOCAL_AIR_BOOST_MAX_DB = 0.8
VOCAL_UPPER_BOOST_MAX_BY_TEMPLATE = {
    "template_a": 1.0,
    "template_b": 2.5,
    "template_c": 1.5,
}
VOCAL_UPPER_BOOST_STRONG_DELTA_DB = 4.0
VOCAL_HIGH_SAFE_RATIO_LIMITS = {
    "harsh": 0.055,
    "sib": 0.02,
}
VOCAL_HIGH_SAFE_PEAK_LIMITS = {
    "harsh": 8.0,
    "sib": 7.0,
}
ACCOMP_CARVE_MAX_CUT_DB = 2.0
ACCOMP_CARVE_MAX_ACTIONS = 2
ACCOMP_MASKING_EXCESS_DEAD_BAND_DB = 1.2
ACCOMP_MASKING_EXCESS_STRONG_DB = 6.0
ACCOMP_CARVE_REGION_BY_BAND = {
    "lowmid": "body",
    "mid": "presence",
    "upper": "presence",
    "harsh": "presence",
}

# --- 人声高频保护（重采样颗粒 / 分离导致的电音感） ---
# 原生采样率 <= 24 kHz 的干声有硬 Nyquist 墙；~10.5 kHz 以上更像重采样/编码颗粒，
# 不是真正空气感。这里先 lowpass；如果 upper/harsh/sib 整体很尖，再用窄带轻削共振。
HF_GUARD_LOWPASS_BY_NYQUIST = (
    # (原生 Nyquist 小于等于这个值, lowpass 频率)
    (12000.0, 10500.0),
    (16000.0, 15000.0),
)
HF_GUARD_LOWPASS_Q = 0.7
# 标记“电/金属感分离噪声”的 peakiness 阈值。
# upper/harsh/sib 同时发尖时，8.5 dB 太保守，会漏掉明显毛刺；
# 降到 8.0 dB 后仍要求多个高频段同时命中，避免健康声音被单点误伤。
HF_GUARD_PEAK_HARD_DB = 8.0   # 单个高频段到这个尖度就轻削
HF_GUARD_ELECTRIC_BANDS = ("upper", "harsh", "sib")
HF_GUARD_ELECTRIC_MIN_HITS = 2  # 高频/刺耳/齿音至少 2 个很尖，才算整体电音感
HF_GUARD_TAME_FREQ_HZ = {
    "upper": 3200.0,
    "harsh": 6200.0,
    "sib": 9000.0,
}
# 保持轻量：金属/电音感只用窄 notch 点一下，不再叠大刀。
# 之前更大的值会和 harsh/sib cut 叠加，把人声削薄。
HF_GUARD_TAME_Q = 3.2          # 窄 notch，只打共振环
HF_GUARD_TAME_MAX_CUT_DB = 1.8
HF_GUARD_TAME_PER_PEAK_DB = 0.45  # 尖峰超阈值越多，削得越多
HF_GUARD_REFERENCE_FORWARD_DB = -1.2
HF_GUARD_REFERENCE_EVEN_CAP_DB = {
    "upper": 0.9,
    "harsh": 0.75,
    "sib": 0.65,
}
HF_GUARD_REFERENCE_SLIGHT_BACK_DB = -2.5
HF_GUARD_REFERENCE_SLIGHT_BACK_CAP_DB = {
    "upper": 1.25,
    "harsh": 1.0,
    "sib": 0.85,
}
PRESENCE_BANDS = {"upper", "harsh", "sib"}
CLARITY_GUARD_BANDS = {"upper", "harsh", "sib", "air"}
CLARITY_GUARD_REF_DELTA_DB = {
    "upper": 2.5,
    "harsh": 1.8,
    "sib": 5.0,
    "air": 10.0,
}
VOCAL_CONTEXT_VERSION = 1
VOCAL_PRESENCE_POLICY = {
    "reference_vocal_forward_or_even": {
        "pre_timbre_cut_scale": 0.48,
        "pre_timbre_cut_caps_db": {"upper": 0.8, "harsh": 0.55, "sib": 0.45},
        # post-group 是最终可听人声贡献轨；这里允许稍多回正偏亮/偏冲频段，
        # 但仍只 cut、不靠总线改变前后，避免音色相似度和“人声靠前”互相打架。
        "post_timbre_cut_scale": 0.58,
        "post_timbre_cut_caps_db": {"upper": 1.35, "harsh": 1.05, "sib": 0.85},
        "hf_cut_caps_db": HF_GUARD_REFERENCE_EVEN_CAP_DB,
        "repair_strength_scale": 0.75,
        "allow_dark_brighter_skip": False,
        "max_total_cut_db": {"low": 5.5, "lowmid": 5.5, "mid": 1.8, "upper": 3.2, "harsh": 2.2, "sib": 1.8},
        "reason": "原曲人声接近持平或靠前，但最终人声过亮/过冲时仍允许小幅音色回正",
    },
    "reference_vocal_slightly_back": {
        "pre_timbre_cut_scale": 0.7,
        "pre_timbre_cut_caps_db": {"upper": 1.1, "harsh": 0.8, "sib": 0.65},
        "post_timbre_cut_scale": 0.65,
        "post_timbre_cut_caps_db": {"upper": 1.4, "harsh": 1.0, "sib": 0.85},
        "hf_cut_caps_db": HF_GUARD_REFERENCE_SLIGHT_BACK_CAP_DB,
        "repair_strength_scale": 0.9,
        "allow_dark_brighter_skip": True,
        "max_total_cut_db": {"low": 5.5, "lowmid": 6.0, "mid": 2.0, "upper": 3.2, "harsh": 2.2, "sib": 1.8},
        "reason": "原曲人声略靠后，允许更明显的音色回正，但仍保留可懂度",
    },
    "reference_vocal_back_or_unknown": {
        "pre_timbre_cut_scale": 1.0,
        "pre_timbre_cut_caps_db": {},
        "post_timbre_cut_scale": 1.0,
        "post_timbre_cut_caps_db": {},
        "hf_cut_caps_db": {},
        "repair_strength_scale": 1.0,
        "allow_dark_brighter_skip": True,
        "max_total_cut_db": {"low": 5.8, "lowmid": 6.5, "mid": 2.2, "upper": 4.0, "harsh": 3.0, "sib": 2.4},
        "reason": "原曲人声靠后或缺少比例参考，按音色差异和瑕疵检测完整执行",
    },
}

SPATIAL_BASELINE = {
    "rverb_send_pre_db": -12.5,
    "rverb_time_s": 1.75,
    "rverb_predelay_ms": 12.0,
    "rverb_early_ref_db": -2.0,
    "rverb_damp": 0.35,
    "rverb_eq_hi_gain_db": -4.0,
    "output_side_trim_db": 0.0,
    "supertap_send_pre_db": -27.0,
    "supertap_gain_db": -18.5,
    "supertap_feedback": 0.10,
    "supertap_width": 0.45,
    "supertap_color_hz": 2400.0,
    "shimmer_send_pre_db": -18.0,
    "shimmer_gain_db": -18.0,
}

SPATIAL_LIMITS = {
    "rverb_send_pre_db": (-16.0, -8.5),
    "rverb_time_s": (1.4, 3.8),
    "rverb_predelay_ms": (8.0, 35.0),
    "rverb_eq_hi_gain_db": (-5.0, -2.0),
    "output_side_trim_db": (-10.0, 0.0),
    "supertap_send_pre_db": (-34.0, -18.0),
    "supertap_gain_db": (-24.0, -8.0),
    "supertap_feedback": (0.06, 0.24),
    "supertap_width": (0.20, 0.65),
    "shimmer_send_pre_db": (-24.0, -16.0),
    "shimmer_gain_db": (-24.0, -16.0),
}


def build_spatial_decision(
    stem_spatial: dict[str, Any],
    reverb: dict[str, Any],
    delay: dict[str, Any],
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """把原曲人声空间拆成 width/depth/wet/tail/early/delay 契约。

    这里不按歌名或 profile 分支，也不直接照搬 center-led=干/无 delay。
    下游 `build_spatial_fx_plan()` 只消费这个契约生成一次 vocal_group_fx 参数，
    避免新增 post pass 或 render 后反复重跑。
    """
    side_mid = float(stem_spatial.get("active_side_minus_mid_db") or 0.0)
    center_led = bool(stem_spatial.get("near_mono_center_led"))
    strict_center = center_led and side_mid <= -30.0

    group_ratios = (analysis or {}).get("group_ratios") or {}
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float((analysis or {}).get("body_to_presence") or 0.0)
    missing_presence = presence_ratio <= 0.03 and body_to_presence >= 16.0
    clarity_risk = "high" if missing_presence else ("medium" if presence_ratio <= 0.05 and body_to_presence >= 12.0 else "normal")

    tail_ratio = float(reverb.get("tail_to_onset_ratio_db") or -60.0)
    rt60_ms = float(reverb.get("est_rt60_ms") or 0.0)
    reverb_conf = float(reverb.get("confidence") or 0.0)
    long_rt_proxy = rt60_ms >= 12000.0
    dense_tail = tail_ratio >= -2.0
    if long_rt_proxy and dense_tail:
        tail_state = "dense_tail_long_rt_proxy"
    elif long_rt_proxy:
        tail_state = "dry_tail_long_rt_proxy"
    elif dense_tail:
        tail_state = "dense_tail_short_rt"
    else:
        tail_state = "dry_or_short_tail"

    delay_corr = float(delay.get("peak_corr") or 0.0)
    delay_lag = float(delay.get("peak_lag_ms") or 0.0)
    delay_conf = float(delay.get("confidence") or 0.0)
    stable_depth_delay = delay_corr >= 0.70 and 45.0 <= delay_lag <= 130.0

    if strict_center:
        width_mode = "near_mono_center_strict"
        side_trim_db = -8.0
        delay_width = 0.20
    elif center_led:
        width_mode = "center_led_depth_allowed"
        side_trim_db = -6.0
        delay_width = 0.30
    else:
        width_mode = "reference_width_open"
        side_trim_db = 0.0
        delay_width = 0.48

    # 1.1 的空间修正遵守 0.1 vocal_group_fx 的接口/发送方式契约：
    # - mono in -> dry/early/reverb/shimmer/delay 并联 -> stereo out 的进出声道不改。
    # - “发送方式”只指 dry 与各效果 return 的并联连接方式；不代表 send 电平锁死。
    # - reference 可以动态调 send level 和效果器内部参数，但不能改成另一套路由/多 pass。
    # - 所有效果强度上限以 189f4a7/v0.1 预制为准：不能比 0.1 更湿、更长、更宽或更亮。
    # 这样不会把“融合”和“空间”混成同一个 fader 问题，也避免每首歌变成不同 rack。
    if strict_center:
        classic_rverb_send_db = -13.25
        classic_rverb_time_s = 1.52
        classic_predelay_ms = 11.0
        classic_delay_send_db = -30.0
        classic_delay_gain_db = -20.5
        classic_delay_width = 0.22
    elif center_led:
        classic_rverb_send_db = -12.85
        classic_rverb_time_s = 1.65
        classic_predelay_ms = 12.0
        classic_delay_send_db = -28.5
        classic_delay_gain_db = -19.4
        classic_delay_width = 0.32
    else:
        classic_rverb_send_db = SPATIAL_BASELINE["rverb_send_pre_db"]
        classic_rverb_time_s = SPATIAL_BASELINE["rverb_time_s"]
        classic_predelay_ms = SPATIAL_BASELINE["rverb_predelay_ms"]
        classic_delay_send_db = SPATIAL_BASELINE["supertap_send_pre_db"]
        classic_delay_gain_db = SPATIAL_BASELINE["supertap_gain_db"]
        classic_delay_width = SPATIAL_BASELINE["supertap_width"]

    classic_early_ref_db = SPATIAL_BASELINE["rverb_early_ref_db"]
    classic_return_hi_db = -4.45 if center_led else -4.15
    classic_delay_feedback = SPATIAL_BASELINE["supertap_feedback"]

    # 纵深不再靠“加宽”解决：center-led 保持窄声像，但允许早反射和窄 delay 提供前后距离。
    if center_led and long_rt_proxy and dense_tail:
        depth_state = "early_wrap_short_tail"
        time_target_s = 1.52 if strict_center else 1.65
        predelay_target_ms = 18.0 if strict_center else 17.0
        wet_scale = 0.52 if strict_center else 0.62
        wet_delta_cap_db = 1.65 if strict_center else 1.85
        early_ref_db = -1.15 if strict_center else -1.05
    elif center_led and long_rt_proxy:
        depth_state = "early_wrap_controlled_tail"
        time_target_s = 1.70 if strict_center else 1.82
        predelay_target_ms = 19.0 if strict_center else 18.0
        wet_scale = 0.58 if strict_center else 0.72
        wet_delta_cap_db = 1.75 if strict_center else 2.05
        early_ref_db = -1.20 if strict_center else -1.10
    elif center_led:
        depth_state = "center_depth_balanced"
        time_target_s = 1.75
        predelay_target_ms = 16.0
        wet_scale = 0.70
        wet_delta_cap_db = 2.0
        early_ref_db = -1.25
    else:
        depth_state = "open_reference_depth"
        time_target_s = reverb_time_target(rt60_ms)
        predelay_target_ms = clamp(12.0 + max(0.0, tail_ratio + 10.0) * 0.8, 10.0, 26.0)
        wet_scale = 1.0
        wet_delta_cap_db = 3.6
        early_ref_db = -1.7

    if clarity_risk == "high":
        # 缺咬字时不能用亮尾巴补空间；改用短尾、早反射和窄 delay 保留深度。
        wet_scale *= 0.74
        wet_delta_cap_db = min(wet_delta_cap_db, 1.25)
        time_target_s = min(time_target_s, 1.58)
        predelay_target_ms = max(predelay_target_ms, 19.0)
        early_ref_db = min(early_ref_db, -1.35)
        delay_width = min(delay_width, 0.22)
        # Faust 底座也只做“护栏式”收敛：空间仍然存在，但尾巴更暗、更窄，
        # 不用过量湿度去弥补原始干声缺咬字。
        classic_rverb_send_db -= 0.45
        classic_rverb_time_s = min(classic_rverb_time_s, 1.82)
        classic_predelay_ms = min(classic_predelay_ms, SPATIAL_BASELINE["rverb_predelay_ms"])
        classic_delay_send_db -= 0.7
        classic_delay_gain_db -= 0.7
        classic_delay_width = min(classic_delay_width, 0.24)
        classic_delay_feedback = 0.085
        classic_return_hi_db = -4.75

    if stable_depth_delay:
        if clarity_risk == "high":
            delay_state = "narrow_depth_delay_clarity_guard"
            delay_send_delta_db = -3.7
            delay_feedback = 0.065
        elif strict_center:
            delay_state = "narrow_depth_delay_strict_center"
            delay_send_delta_db = -3.1
            delay_feedback = 0.070
        elif center_led:
            delay_state = "narrow_depth_delay_center_led"
            delay_send_delta_db = -2.2
            delay_feedback = 0.078
        else:
            delay_state = "reference_depth_delay_open"
            delay_send_delta_db = min(2.0, delay_conf * 2.2)
            delay_feedback = 0.10 + min(0.06, delay_conf * 0.08)
    else:
        delay_state = "weak_or_unstable_delay"
        delay_send_delta_db = -5.2 if center_led else min(0.5, max(0.0, delay_conf * 0.8))
        delay_feedback = 0.060 if center_led else 0.10

    return_hi = -4.85 if clarity_risk == "high" else (-4.65 if center_led else -4.20)

    return {
        "version": 1,
        "width_state": width_mode,
        "depth_state": depth_state,
        "wet_state": "short_wet_wrap" if center_led and long_rt_proxy else "reference_wet",
        "tail_state": tail_state,
        "early_state": "front_wrap_early_reflection",
        "delay_state": delay_state,
        "clarity_risk": clarity_risk,
        "evidence": {
            "active_side_minus_mid_db": round(side_mid, 3),
            "center_led": center_led,
            "tail_to_onset_ratio_db": round(tail_ratio, 3),
            "est_rt60_ms": round(rt60_ms, 1),
            "reverb_confidence": round(reverb_conf, 3),
            "delay_peak_corr": round(delay_corr, 3),
            "delay_peak_lag_ms": round(delay_lag, 1),
            "presence_ratio": round(presence_ratio, 5),
            "body_to_presence": round(body_to_presence, 3),
        },
        "mapping": {
            "classic_faust_anchor": True,
            "side_trim_db": round(side_trim_db, 3),
            "wet_scale": round(wet_scale, 3),
            "wet_delta_cap_db": round(wet_delta_cap_db, 3),
            "time_target_s": round(time_target_s, 3),
            "time_scale": 1.0,
            "predelay_target_ms": round(predelay_target_ms, 3),
            "early_ref_db": round(early_ref_db, 3),
            "reverb_eq_hi_gain_db": round(return_hi, 3),
            "delay_send_delta_db": round(delay_send_delta_db, 3),
            "delay_feedback": round(delay_feedback, 3),
            "delay_width": round(delay_width, 3),
            "v0_1_io_send_path_lock": True,
            "v0_1_effect_ceiling": True,
            "classic_rverb_send_pre_db": round(classic_rverb_send_db, 3),
            "classic_rverb_time_s": round(classic_rverb_time_s, 3),
            "classic_rverb_predelay_ms": round(classic_predelay_ms, 3),
            "classic_rverb_early_ref_db": round(classic_early_ref_db, 3),
            "classic_rverb_eq_hi_gain_db": round(classic_return_hi_db, 3),
            "classic_supertap_send_pre_db": round(classic_delay_send_db, 3),
            "classic_supertap_gain_db": round(classic_delay_gain_db, 3),
            "classic_supertap_feedback": round(classic_delay_feedback, 3),
            "classic_supertap_width": round(classic_delay_width, 3),
        },
        "allowed_moves": [
            "keep_reference_width",
            "shorten_long_rt_proxy",
            "use_early_reflection_for_depth",
            "use_narrow_delay_for_depth" if stable_depth_delay else "keep_delay_near_floor",
            "protect_clarity_from_bright_return" if clarity_risk == "high" else "moderate_return_tone",
        ],
        "policy": "空间保持 0.1 Faust 输入/输出和发送路径；reference 在 0.1 上限内动态调白名单参数。",
    }


def build_dry_vocal_strategy(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
    template_id: str,
) -> dict[str, Any]:
    """识别偏干/偏闷/咬字少的人声素材，并把伴奏避让请求写进 plan。"""
    _ = input_features
    ratios = analysis.get("ratios") or {}
    group_ratios = analysis.get("group_ratios") or {}
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    classification = analysis.get("classification") or {}
    tags: list[str] = []
    triggers: list[dict[str, Any]] = []
    duck_profile = {
        "low_extra_db": 0.0,
        "body_extra_db": 0.0,
        "presence_extra_db": 0.0,
        "air_extra_db": 0.0,
    }

    lowmid_ratio = float(ratios.get("lowmid") or 0.0)
    body_ratio = float(group_ratios.get("body") or 0.0)
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    upper_peak = float(analysis.get("peakiness_upper") or 0.0)
    harsh_peak = float(analysis.get("peakiness_harsh") or 0.0)

    if lowmid_ratio >= 0.55 or body_to_presence >= 8.0:
        tags.append("lowmid_body_heavy")
        triggers.append({
            "tag": "lowmid_body_heavy",
            "condition": "lowmid_ratio>=0.55 or body_to_presence>=8.0",
            "evidence": {
                "lowmid_ratio": round(lowmid_ratio, 4),
                "body_to_presence": round(body_to_presence, 3),
            },
            "action": {"body_extra_db": 0.25, "presence_extra_db": 0.45},
        })
        duck_profile["body_extra_db"] += 0.25
        duck_profile["presence_extra_db"] += 0.45
    if lowmid_ratio >= 0.70 or body_to_presence >= 12.0:
        tags.append("presence_masked_by_body")
        triggers.append({
            "tag": "presence_masked_by_body",
            "condition": "lowmid_ratio>=0.70 or body_to_presence>=12.0",
            "evidence": {
                "lowmid_ratio": round(lowmid_ratio, 4),
                "body_to_presence": round(body_to_presence, 3),
            },
            "action": {"presence_extra_db": 0.35},
        })
        duck_profile["presence_extra_db"] += 0.35
    if presence_ratio <= 0.03 and body_to_presence >= 16.0:
        tags.append("extreme_presence_starvation")
        # presence 极低且 body 明显偏重时，不是缺“亮度 EQ”，而是有效咬字频段太少；
        # 不给干声加亮，只让伴奏在 presence/air 稍微多退一点。
        triggers.append({
            "tag": "extreme_presence_starvation",
            "condition": "presence_ratio<=0.03 and body_to_presence>=16.0",
            "evidence": {
                "presence_ratio": round(presence_ratio, 4),
                "body_to_presence": round(body_to_presence, 3),
            },
            "action": {"presence_extra_db": 0.50, "air_extra_db": 0.15},
        })
        duck_profile["presence_extra_db"] += 0.50
        duck_profile["air_extra_db"] += 0.15
    if body_ratio >= 0.85 and presence_ratio <= 0.08:
        tags.append("dark_or_muffled_dry_vocal")
        triggers.append({
            "tag": "dark_or_muffled_dry_vocal",
            "condition": "body_ratio>=0.85 and presence_ratio<=0.08",
            "evidence": {
                "body_ratio": round(body_ratio, 4),
                "presence_ratio": round(presence_ratio, 4),
            },
            "action": {"air_extra_db": 0.20},
        })
        duck_profile["air_extra_db"] += 0.20
    if float(ratios.get("low") or 0.0) >= 0.06 or float(ratios.get("sub") or 0.0) >= 0.004:
        tags.append("dry_vocal_low_pressure")
        triggers.append({
            "tag": "dry_vocal_low_pressure",
            "condition": "low_ratio>=0.06 or sub_ratio>=0.004",
            "evidence": {
                "low_ratio": round(float(ratios.get("low") or 0.0), 4),
                "sub_ratio": round(float(ratios.get("sub") or 0.0), 4),
            },
            "action": {"low_extra_db": 0.25},
        })
        duck_profile["low_extra_db"] += 0.25
    if upper_peak >= 12.0 or harsh_peak >= 8.0:
        tags.append("peaky_presence")
        triggers.append({
            "tag": "peaky_presence",
            "condition": "upper_peak>=12.0 or harsh_peak>=8.0",
            "evidence": {
                "upper_peak": round(upper_peak, 3),
                "harsh_peak": round(harsh_peak, 3),
            },
            "action": {"presence_extra_db": -0.20},
        })
        duck_profile["presence_extra_db"] = max(0.0, duck_profile["presence_extra_db"] - 0.20)

    capped_duck_profile: dict[str, float] = {}
    for key, value in duck_profile.items():
        # 每个动作必须来自上面的音频特征触发；这里统一截顶，防止多条件叠加过量。
        capped_duck_profile[key] = round(clamp(float(value), 0.0, DRY_VOCAL_DUCK_CAPS_DB[key]), 3)

    strong_rules: list[str] = []
    selected = classification.get(template_to_label(template_id)) or {}
    if isinstance(selected, dict):
        strong_rules = list(selected.get("strong_rules") or [])

    return {
        "enabled": bool(tags),
        "selected_template": template_id,
        "classification_label": classification.get("label"),
        "tags": tags,
        "strong_rules": strong_rules,
        "triggers": triggers,
        "ratios": {
            "lowmid": round(lowmid_ratio, 4),
            "body": round(body_ratio, 4),
            "presence": round(presence_ratio, 4),
            "body_to_presence": round(body_to_presence, 3),
        },
        "duck_profile": capped_duck_profile,
        "duck_profile_caps_db": DRY_VOCAL_DUCK_CAPS_DB,
        "policy": (
            "按干声自身频谱决定伴奏哪些频段需要避让；"
            "整体人声/伴奏比例仍保守跟随参考曲 active 比例或通用兜底比例。"
        ),
    }


def active_balance_value(features: dict[str, Any] | None) -> float | None:
    balance = (features or {}).get("vocal_accomp_balance") or {}
    active = balance.get("active_vocal_minus_accomp_db")
    if active is not None:
        return float(active)
    value = balance.get("vocal_minus_accomp_db")
    return float(value) if value is not None else None


def reference_presence_mode(ref_balance: float | None) -> str:
    if ref_balance is None:
        return "reference_vocal_back_or_unknown"
    if ref_balance >= HF_GUARD_REFERENCE_FORWARD_DB:
        return "reference_vocal_forward_or_even"
    if ref_balance >= HF_GUARD_REFERENCE_SLIGHT_BACK_DB:
        return "reference_vocal_slightly_back"
    return "reference_vocal_back_or_unknown"


def build_vocal_effect_context(
    ref_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """原曲人声效果画像。

    这里专门管“像原曲人声效果”的部分：纵深、宽度、混响、动态。
    音色相似度不从这里取目标，仍然只看音色筛选片段。
    """
    stem_spatial = (ref_features or {}).get("vocal_spatial_profile") or {}
    reverb = (ref_features or {}).get("reverb_proxy") or {}
    delay = (ref_features or {}).get("delay_proxy") or {}
    ref_dyn = (ref_features or {}).get("vocal_dynamics") or {}
    input_dyn = (input_features or {}).get("vocal_dynamics") or {}
    spatial_decision = build_spatial_decision(stem_spatial, reverb, delay, analysis)
    spatial_mapping = spatial_decision.get("mapping") or {}
    side_mid = float(stem_spatial.get("active_side_minus_mid_db") or 0.0)
    center_led = bool(stem_spatial.get("near_mono_center_led"))
    width_mode = str(spatial_decision.get("width_state") or "reference_width_open")
    side_trim_db = float(spatial_mapping.get("side_trim_db") or 0.0)
    wet_scale = float(spatial_mapping.get("wet_scale") or 1.0)
    time_scale = float(spatial_mapping.get("time_scale") or 1.0)
    delay_width = spatial_mapping.get("delay_width")

    group_ratios = (analysis or {}).get("group_ratios") or {}
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float((analysis or {}).get("body_to_presence") or 0.0)
    preserve_missing_presence = presence_ratio <= 0.03 and body_to_presence >= 16.0

    range_gap = float(ref_dyn.get("frame_range_p90_p10_db") or 0.0) - float(
        input_dyn.get("frame_range_p90_p10_db") or 0.0
    )
    micro_gap = float(ref_dyn.get("micro_range_p95_p50_db") or 0.0) - float(
        input_dyn.get("micro_range_p95_p50_db") or 0.0
    )
    micro_p99_gap = float(ref_dyn.get("micro_range_p99_p50_db") or 0.0) - float(
        input_dyn.get("micro_range_p99_p50_db") or 0.0
    )
    level_gap = float(ref_dyn.get("active_rms_db") or 0.0) - float(input_dyn.get("active_rms_db") or 0.0)
    peak_gap = float(ref_dyn.get("peak_db") or 0.0) - float(input_dyn.get("peak_db") or 0.0)

    return {
        "version": 1,
        "target_source": "original_vocal_stem",
        "spatial": {
            "width_mode": width_mode,
            "center_led": center_led,
            "active_side_minus_mid_db": round(side_mid, 3),
            "side_trim_db": round(side_trim_db, 3),
            "reverb_wet_scale": round(wet_scale, 3),
            "reverb_time_scale": round(time_scale, 3),
            "delay_width_cap": round(float(delay_width), 3) if isinstance(delay_width, (int, float)) else None,
            "preserve_missing_presence": bool(preserve_missing_presence),
        },
        "spatial_decision": spatial_decision,
        "reverb": {
            "tail_to_onset_ratio_db": reverb.get("tail_to_onset_ratio_db"),
            "est_rt60_ms": reverb.get("est_rt60_ms"),
            "confidence": reverb.get("confidence"),
        },
        "delay": {
            "peak_corr": delay.get("peak_corr"),
            "peak_lag_ms": delay.get("peak_lag_ms"),
            "confidence": delay.get("confidence"),
        },
        "dynamics": {
            "gap": {
                "frame_range_p90_p10_db": round(range_gap, 3),
                "micro_range_p95_p50_db": round(micro_gap, 3),
                "micro_range_p99_p50_db": round(micro_p99_gap, 3),
                "active_rms_db": round(level_gap, 3),
                "peak_db": round(peak_gap, 3),
            },
            "policy": "动态只追原曲人声 stem；不使用音色筛选片段，也不改变全局 bus 比例。",
        },
        "policy": "音色目标和效果目标分离：本画像只给纵深、宽度、混响、动态边界。",
    }


def build_vocal_processing_context(
    ref_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    timbre_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> dict[str, Any]:
    """统一决策层：同时看音色参考、原曲人声/伴奏比例和干声瑕疵。

    后续各阶段只消费这里给出的边界，不再各自临时判断，避免 guard 之间互相抵消。
    """
    ref_balance = active_balance_value(ref_features)
    mode = reference_presence_mode(ref_balance)
    policy = dict(VOCAL_PRESENCE_POLICY[mode])
    input_tone = (input_features or {}).get("vocal_tonal_balance") or {}
    timbre_tone = (timbre_features or {}).get("vocal_tonal_balance") or {}
    timbre_delta = {
        band: round(float(timbre_tone[band]) - float(input_tone[band]), 2)
        for band in SOURCE_VOCAL_EQ_BANDS
        if isinstance(timbre_tone.get(band), (int, float)) and isinstance(input_tone.get(band), (int, float))
    }
    ref_vocal_tone = (ref_features or {}).get("vocal_tonal_balance") or {}
    clarity_guard: dict[str, Any] = {
        "protected_bands": [],
        "by_band": {},
        "policy": (
            "音色筛选片段可以决定人声颜色，但不能把原曲 stem 证明需要保留的咬字/"
            "空气感继续削掉；这是通用清晰度边界，不按歌曲名分支。"
        ),
    }
    for band in CLARITY_GUARD_BANDS:
        ref_value = ref_vocal_tone.get(band)
        current_value = input_tone.get(band)
        if not isinstance(ref_value, (int, float)) or not isinstance(current_value, (int, float)):
            continue
        ref_minus_current = float(ref_value) - float(current_value)
        threshold = CLARITY_GUARD_REF_DELTA_DB[band]
        protected = ref_minus_current >= threshold
        clarity_guard["by_band"][band] = {
            "reference_db": round(float(ref_value), 2),
            "input_db": round(float(current_value), 2),
            "ref_minus_input_db": round(ref_minus_current, 2),
            "threshold_db": threshold,
            "protected": protected,
        }
        if protected:
            clarity_guard["protected_bands"].append(band)
    peakiness = {
        "upper": float(analysis.get("peakiness_upper") or 0.0),
        "harsh": float(analysis.get("peakiness_harsh") or 0.0),
        "sib": float(analysis.get("peakiness_sib") or 0.0),
    }
    high_hits = [band for band, peak in peakiness.items() if peak >= HF_GUARD_PEAK_HARD_DB]
    delta_upper = timbre_delta.get("upper")
    skip_brighter = False
    if policy["allow_dark_brighter_skip"] and delta_upper is not None:
        skip_brighter = delta_upper <= -4.0 or (delta_upper <= -2.0 and peakiness["upper"] >= 14.0)
    vocal_effect_target = build_vocal_effect_context(ref_features, input_features, analysis)
    return {
        "version": VOCAL_CONTEXT_VERSION,
        "template_id": template_id,
        "reference_balance": {
            "vocal_minus_accomp_db": round(ref_balance, 2) if ref_balance is not None else None,
            "mode": mode,
        },
        "timbre": {
            "has_reference": bool(timbre_tone),
            "delta_db": timbre_delta,
        },
        "reference_clarity_guard": clarity_guard,
        "artifact_profile": {
            "native_sample_rate": analysis.get("native_sample_rate"),
            "effective_nyquist_hz": analysis.get("effective_nyquist_hz"),
            "peakiness": {key: round(value, 2) for key, value in peakiness.items()},
            "high_peak_hits": high_hits,
            "electric_profile": len(high_hits) >= HF_GUARD_ELECTRIC_MIN_HITS,
        },
        "presence_band_policy": {
            "mode": mode,
            "pre_timbre_cut_scale": policy["pre_timbre_cut_scale"],
            "pre_timbre_cut_caps_db": policy["pre_timbre_cut_caps_db"],
            "post_timbre_cut_scale": policy["post_timbre_cut_scale"],
            "post_timbre_cut_caps_db": policy["post_timbre_cut_caps_db"],
            "hf_cut_caps_db": policy["hf_cut_caps_db"],
            "repair_strength_scale": policy["repair_strength_scale"],
            "reason": policy["reason"],
        },
        "band_budget": {
            "max_total_cut_db": policy["max_total_cut_db"],
            "policy": "同一频段跨 timbre、模板后回正、residual/source/HF 的累计 cut 不能超过这里。",
        },
        "template_chain": {
            "skip_oneknob_brighter": bool(skip_brighter),
            "reason": (
                "模板 brighter 是否跳过由统一决策层决定：音色目标更暗时可跳过，"
                "但原曲人声靠前/持平时不跳过。"
            ),
        },
        "vocal_effect_target": vocal_effect_target,
        "vocal_event_guard": {
            "enabled": True,
            "stage": "post_dynamic_pre_vocal_group",
            "micro_continuity": {
                "enabled": False,
                "max_lift_db": 2.2,
                "reference_required": True,
                "policy": "默认只诊断不自动补；参考同位置也断开时不硬拉，避免把停顿/噪声拉出来。",
            },
            "breath_transition": {
                "max_cut_db": 3.0,
                "policy": "句首低能量高频气声不当作前景人声处理，避免后续伴奏避让把 breath 露出来。",
            },
        },
        "post_group_timbre_guard": {
            "enabled": bool(timbre_tone),
            "stage": "post_vocal_group_fx",
            "policy": (
                "最终可听 vocal_group 再做一次小幅音色校验；"
                "音色方向来自筛选片段，边界仍由原曲人声/伴奏位置决定。"
            ),
        },
        "policy": "音色差异给方向，原曲人声效果给纵深/动态边界，原曲人声/伴奏比例给总线边界，干声瑕疵给最低限度修复。"
    }

def build_fusion_intent(
    analysis: dict[str, Any],
    ref_features: dict[str, Any] | None,
    template_id: str,
    vocal_processing_context: dict[str, Any],
) -> dict[str, Any]:
    """渲染前的融合意图。

    这里只声明目标和模块边界，不改变任何处理参数。后续 bus/duck/section/spatial
    如果要消费它，需要单独接入并逐项验证。
    """
    group_ratios = analysis.get("group_ratios") or {}
    ratios = analysis.get("ratios") or {}
    body = float(group_ratios.get("body") or 0.0)
    presence = float(group_ratios.get("presence") or 0.0)
    lowmid = float(ratios.get("lowmid") or 0.0)
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    ref_gap = active_balance_value(ref_features)
    spatial = (ref_features or {}).get("vocal_spatial_profile") or {}
    near_mono = bool(spatial.get("near_mono_center_led"))
    side_mid = float(spatial.get("active_side_minus_mid_db") or 0.0)

    profile = "reference_balanced"
    reasons: list[str] = []
    if near_mono and side_mid <= -29.0 and ref_gap is not None and ref_gap >= -0.6:
        profile = "intimate_light"
        reasons.append("原曲人声接近持平且非常居中/窄，优先保持近、小、贴。")
    elif template_id == "template_a" and ref_gap is not None and ref_gap >= -1.6:
        profile = "front_pop"
        reasons.append("原曲 active 人声靠前，模板为强情绪/前置型。")
        if presence <= 0.03:
            reasons.append("干声 presence 极低，清晰度不能只靠伴奏大幅让位。")
    elif body >= 0.93 and body_to_presence >= 20.0 and ref_gap is not None and ref_gap >= -1.9:
        profile = "warm_mid_embedded"
        reasons.append("人声主体厚、presence 少，但原曲不是深埋型，重点是包裹和灰度。")
    elif lowmid >= 0.70 and body >= 0.85:
        profile = "dense_male"
        reasons.append("低中频/主体占比高，重点是男声厚度和伴奏低中频分工。")
    elif body_to_presence >= 16.0:
        profile = "warm_mid_embedded"
        reasons.append("主体对 presence 比例很高，按温暖嵌入型处理。")
    else:
        reasons.append("没有命中强特征，先按通用参考融合型处理。")

    targets = {
        "front_pop": {
            "frontness": "人声可前置，但伴奏 presence 不能被挖空。",
            "duck": "只解决遮挡；presence duck 需要和 bus/section 共用预算。",
            "space": "中心稳定，宽度/湿度不能让人声浮到伴奏外。",
        },
        "warm_mid_embedded": {
            "frontness": "人声应贴在伴奏里，清晰度来自频段分工和空间，不来自硬推。",
            "duck": "少做高频洞，优先保护灰暗包裹感。",
            "space": "控制湿度和高频 return，避免变亮变宽。",
        },
        "dense_male": {
            "frontness": "男声可略靠后，厚度不能被 bus/duck 改成贴脸。",
            "duck": "重点低中频错位，避免全频压伴奏。",
            "space": "保持中心厚度，空间只做贴合。",
        },
        "intimate_light": {
            "frontness": "近、小、自然，不要做成大流行前景人声。",
            "duck": "极轻，只处理明显遮挡。",
            "space": "窄、干、居中，side/wet 是首要约束。",
        },
        "reference_balanced": {
            "frontness": "跟随参考 active gap，但必须避免多模块重复修正。",
            "duck": "按遮挡证据使用。",
            "space": "跟随原曲人声 stem 审计。",
        },
    }

    return {
        "version": 1,
        "enabled": True,
        "profile": profile,
        "profile_usage": "explain_only_not_decision_core",
        "target_source": "pre_render_plan_features",
        "decision_core": {
            "mode": "reference_target_error_correction",
            "policy": "最终融合应按每首自己的原曲 reference.features 计算目标和误差；profile 只解释当前画像，不直接决定参数。",
        },
        "targets": targets[profile],
        "reasons": reasons,
        "evidence": {
            "selected_template": template_id,
            "reference_active_vocal_minus_accomp_db": round(ref_gap, 2) if ref_gap is not None else None,
            "reference_near_mono_center_led": near_mono,
            "reference_active_side_minus_mid_db": round(side_mid, 3),
            "body_ratio": round(body, 4),
            "presence_ratio": round(presence, 4),
            "lowmid_ratio": round(lowmid, 4),
            "body_to_presence": round(body_to_presence, 3),
        },
        "module_contracts": {
            "timbre": {
                "role": "只追音色筛选片段的人声音色目标。",
                "must_not": "不决定人声前后、不通过削 presence 间接改变融合。",
            },
            "bus_balance": {
                "role": "只决定全局前后关系。",
                "must_not": "不能独立把 active gap 补满；需要扣除 duck/section 预算。",
            },
            "accomp_duck": {
                "role": "只解决伴奏遮挡。",
                "must_not": "不能承担弱人声整体抬出任务。",
            },
            "section_balance": {
                "role": "只修段落级明显偏差。",
                "must_not": "触发大量窗口时继续改变整首融合关系。",
            },
            "spatial_fx": {
                "role": "匹配原曲人声 stem 的宽度、湿度、纵深。",
                "must_not": "不能为了质感把人声做宽/做湿到脱离伴奏。",
            },
        },
        "links": {
            "vocal_processing_context_version": vocal_processing_context.get("version"),
            "presence_policy": (vocal_processing_context.get("presence_band_policy") or {}).get("mode"),
        },
        "render_consumption": {
            "active": False,
            "policy": "当前只写入 plan 供审计/讨论；渲染脚本尚不消费此块，因此不会改变声音。",
        },
    }


def reference_presence_hf_policy(ref_features: dict[str, Any] | None) -> dict[str, Any]:
    """旧接口兼容：高频保护现在应优先读取 vocal_processing_context。"""
    ref_balance = active_balance_value(ref_features)
    mode = reference_presence_mode(ref_balance)
    policy = VOCAL_PRESENCE_POLICY[mode]
    if mode == "reference_vocal_forward_or_even":
        return {
            "mode": mode,
            "reference_vocal_minus_accomp_db": round(ref_balance, 2) if ref_balance is not None else None,
            "cut_caps_db": policy["hf_cut_caps_db"],
            "reason": policy["reason"],
        }
    if mode == "reference_vocal_slightly_back":
        return {
            "mode": mode,
            "reference_vocal_minus_accomp_db": round(ref_balance, 2),
            "cut_caps_db": policy["hf_cut_caps_db"],
            "reason": policy["reason"],
        }
    return {
        "mode": mode,
        "reference_vocal_minus_accomp_db": round(ref_balance, 2) if ref_balance is not None else None,
        "cut_caps_db": policy["hf_cut_caps_db"],
        "reason": policy["reason"],
    }


def high_frequency_safety(analysis: dict[str, Any], input_vocal: dict[str, Any]) -> dict[str, Any]:
    """判断高频 boost 是否安全；目前主要给旧逻辑兼容。"""
    ratios = analysis.get("ratios") or {}
    harsh_ratio = float(ratios.get("harsh") or 0.0)
    sib_ratio = float(ratios.get("sib") or 0.0)
    harsh_peak = float(analysis.get("peakiness_harsh") or 0.0)
    upper_peak = float(analysis.get("peakiness_upper") or 0.0)
    input_harsh = float(input_vocal.get("harsh") or 0.0)
    input_sib = float(input_vocal.get("sib") or 0.0)

    harsh_safe = (
        harsh_ratio <= VOCAL_HIGH_SAFE_RATIO_LIMITS["harsh"]
        and harsh_peak <= VOCAL_HIGH_SAFE_PEAK_LIMITS["harsh"]
        and input_harsh <= 0.0
    )
    sib_safe = (
        sib_ratio <= VOCAL_HIGH_SAFE_RATIO_LIMITS["sib"]
        and upper_peak <= VOCAL_HIGH_SAFE_PEAK_LIMITS["sib"]
        and input_sib <= -3.0
    )
    return {
        "safe": harsh_safe and sib_safe,
        "harsh_safe": harsh_safe,
        "sib_safe": sib_safe,
        "harsh_ratio": round(harsh_ratio, 4),
        "sib_ratio": round(sib_ratio, 4),
        "peakiness_harsh": round(harsh_peak, 3),
        "peakiness_upper": round(upper_peak, 3),
        "input_harsh_db": round(input_harsh, 2),
        "input_sib_db": round(input_sib, 2),
    }


def planned_cut_db(actions: list[dict[str, Any]] | None, band: str) -> float:
    """统计某个频段已经计划削掉多少，供统一预算裁剪使用。"""
    total = 0.0
    for action in actions or []:
        if action.get("band") != band:
            continue
        gain = action.get("gain_db")
        if isinstance(gain, (int, float)) and float(gain) < 0.0:
            total += abs(float(gain))
    return total


def vocal_high_boost_cap(
    band: str,
    template_id: str,
    delta: float,
    ref_vocal: dict[str, Any],
    input_vocal: dict[str, Any],
    analysis: dict[str, Any],
    safety: dict[str, Any],
) -> tuple[float, str | None]:
    """按模板和证据限制 upper/air boost；目前主要给旧逻辑兼容。"""
    if band not in {"upper", "air"}:
        return VOCAL_SOURCE_EQ_MAX_BOOST_DB, None
    if not safety.get("safe"):
        return 0.0, "harsh/sibilance evidence is not safe enough for high-frequency boost"

    upper_delta = float(ref_vocal.get("upper", 0.0)) - float(input_vocal.get("upper", 0.0))
    air_delta = float(ref_vocal.get("air", 0.0)) - float(input_vocal.get("air", 0.0))
    upper_ratio = float((analysis.get("ratios") or {}).get("upper") or 0.0)
    true_upper_deficit = upper_delta >= 1.8 or upper_ratio < 0.12
    true_air_deficit = air_delta >= 8.0 and upper_delta >= 1.0

    if band == "air":
        if not true_air_deficit:
            return 0.0, "14k air boost skipped: air is not independently deficient enough"
        return VOCAL_AIR_BOOST_MAX_DB, None

    if not true_upper_deficit:
        return 0.0, "upper boost skipped: upper band is not clearly deficient"
    cap = VOCAL_UPPER_BOOST_MAX_BY_TEMPLATE.get(template_id, 1.2)
    if template_id == "template_b" and upper_delta >= VOCAL_UPPER_BOOST_STRONG_DELTA_DB:
        cap = min(cap, 2.5)
    elif delta < VOCAL_UPPER_BOOST_STRONG_DELTA_DB:
        cap = min(cap, 1.5)
    return cap, None


def build_source_vocal_cleanup_eq(
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> dict[str, Any]:
    """基于干声自身特征的清理 EQ。

    只削掉干声自己超出的部分（闷、刺、齿音），判断基准是它自己的 mid。
    不使用原曲/参考人声的音色或响度比例。
    """
    input_vocal = (input_features or {}).get("vocal_tonal_balance") or {}
    input_active = ((input_features or {}).get("active_band_levels") or {}).get("vocal") or {}
    if not input_active:
        return {
            "enabled": False,
            "actions": [],
            "reason": "missing input active vocal band levels",
        }

    # 只看干声自身：不和参考人声音色比较，也不把干声往参考人声塑形。
    # 先用干声自己的 mid 做锚点，再只削掉明显超过“干净人声”预期形状的频段，
    # 也就是它自身的闷、刺、齿音问题。
    def norm(levels: dict[str, Any]) -> dict[str, float] | None:
        mid = levels.get("mid")
        if not isinstance(mid, (int, float)):
            return None
        return {b: float(v) - float(mid) for b, v in levels.items() if isinstance(v, (int, float))}

    input_n = norm(input_active)
    if input_n is None:
        return {
            "enabled": False,
            "actions": [],
            "reason": "active vocal band levels missing mid reference",
        }
    group_ratios = analysis.get("group_ratios") or {}
    ratios = analysis.get("ratios") or {}
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    preserve_missing_presence = presence_ratio <= 0.03 and body_to_presence >= 16.0

    ranked: list[tuple[float, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for band, rule in SOURCE_VOCAL_EQ_BANDS.items():
        if band not in input_n or band not in VOCAL_SELF_EQ_NEUTRAL_OFFSET_DB:
            continue
        if "cut" not in rule["actions"]:
            continue
        # 过量值表示该频段相对自身 mid 超过“干净人声预期值”多少 dB。
        excess = input_n[band] - VOCAL_SELF_EQ_NEUTRAL_OFFSET_DB[band]
        if excess < VOCAL_SELF_EQ_DEAD_BAND_DB:
            continue
        cut_cap = VOCAL_SELF_EQ_MAX_CUT_DB.get(band, VOCAL_SOURCE_EQ_MAX_CUT_DB)
        if preserve_missing_presence and band == "low" and float(ratios.get("low") or 0.0) >= 0.45:
            # presence 极低且 body 明显偏重时，听不清的主要原因是低频脏和伴奏遮挡；
            # 这里只加大低频清理，不碰本来就缺的高频。
            cut_cap = 7.0
        if preserve_missing_presence and band == "lowmid" and float(ratios.get("lowmid") or 0.0) >= 0.32:
            cut_cap = 5.5
        amount = clamp(excess * VOCAL_SELF_EQ_CUT_FRACTION, 0.5, cut_cap)
        ranked.append(
            (
                abs(excess),
                {
                    "band": band,
                    "type": "cut",
                    "freq_hz": rule["freq_hz"],
                    "q": rule["q"],
                    "gain_db": -round(amount, 2),
                    "source": "self_vocal_excess",
                    "reason": (
                        f"干声 {band} 相对自身 mid 超过干净人声基准 {excess:+.1f} dB；"
                        f"只削掉素材自身过量部分（不参考原曲音色）"
                    ),
                    "evidence": None,
                },
            )
        )
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    actions = [action for _, action in ranked[:VOCAL_SOURCE_EQ_MAX_ACTIONS]]
    return {
        "enabled": bool(actions),
        "mode": "post_template_pre_group_fx",
        "actions": actions,
        "skipped": skipped,
        "policy": (
            "仅做自驱动清理：按干声自身 mid 基准削掉闷、刺、齿音过量；"
            "不使用参考人声音色/响度比例塑形，只 cut，不做补偿性 boost"
        ),
    }


def build_reference_vocal_eq(
    ref_features: dict[str, Any],
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> dict[str, Any]:
    """兼容旧 plan 读取逻辑：实际仍走自驱动干声清理。"""
    _ = ref_features
    return build_source_vocal_cleanup_eq(input_features, analysis, template_id)


def timbre_boost_allowed(
    band: str,
    delta: float,
    analysis: dict[str, Any],
    current: dict[str, Any],
    target: dict[str, Any],
    safety: dict[str, Any],
) -> tuple[bool, str | None]:
    """判断音色筛选片段驱动的 boost 是否足够安全。"""
    min_delta = TIMBRE_BOOST_MIN_DELTA_DB.get(band, TIMBRE_EQ_DEAD_BAND_DB)
    if delta < min_delta:
        return False, f"boost delta {delta:.1f} dB is below timbre threshold {min_delta:.1f} dB"

    # air 很容易只是编码/分离残留，不单独追；必须 upper 也明显缺，并且常规高频安全通过。
    if band == "air":
        upper_delta = float(target.get("upper", 0.0)) - float(current.get("upper", 0.0))
        if upper_delta < 1.5:
            return False, "air boost skipped because upper band is not also clearly deficient"
        if not safety.get("safe"):
            return False, "air boost blocked by harsh/sibilance safety"

    peak_key = f"peakiness_{band}"
    if band in TIMBRE_BOOST_PEAK_LIMIT_DB:
        peak = float(analysis.get(peak_key) or 0.0)
        peak_limit = TIMBRE_BOOST_PEAK_LIMIT_DB[band]
        if peak > peak_limit:
            return False, f"{band} boost blocked because peakiness {peak:.1f} dB exceeds {peak_limit:.1f} dB"

    # harsh/sib 当前绝对电平已经不低时，不再按音色参考补，避免刺耳和齿音被放大。
    if band in TIMBRE_BOOST_INPUT_LEVEL_LIMIT_DB:
        current_db = float(current.get(band) or 0.0)
        level_limit = TIMBRE_BOOST_INPUT_LEVEL_LIMIT_DB[band]
        if current_db > level_limit:
            return False, f"{band} boost blocked because current level {current_db:.1f} dB is already high"

    return True, None


def envelope_budget_band(freq_hz: float) -> str:
    if freq_hz < 180.0:
        return "low"
    if freq_hz < 500.0:
        return "lowmid"
    if freq_hz < 1000.0:
        return "mid"
    if freq_hz < 4000.0:
        return "upper"
    if freq_hz < 8000.0:
        return "harsh"
    if freq_hz < 12000.0:
        return "sib"
    return "air"


def envelope_band_map(envelope: dict[str, Any]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for item in envelope.get("bands") or []:
        if not isinstance(item, dict):
            continue
        band_id = str(item.get("id") or "")
        if not band_id:
            continue
        freq = item.get("freq_hz")
        value = item.get("db")
        if isinstance(freq, (int, float)) and isinstance(value, (int, float)):
            out[band_id] = {"freq_hz": float(freq), "db": float(value)}
    return out


def build_timbre_envelope_actions(
    timbre_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    current_tone: dict[str, Any],
    target_tone: dict[str, Any],
    presence_policy: dict[str, Any],
    safety: dict[str, Any],
    processing_context: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """用细分频谱包络补足 8-band 音色匹配听感不明显的问题。"""
    target_env = envelope_band_map((timbre_features or {}).get("vocal_spectral_envelope") or {})
    current_env = envelope_band_map((input_features or {}).get("vocal_spectral_envelope") or {})
    if not target_env or not current_env:
        return [], [{
            "reason": "missing vocal_spectral_envelope for timbre reference or input vocal",
        }]

    pre_presence_scale = float(presence_policy.get("pre_timbre_cut_scale") or 1.0)
    pre_presence_caps = presence_policy.get("pre_timbre_cut_caps_db") or {}
    clarity_guard = (processing_context or {}).get("reference_clarity_guard") or {}
    clarity_protected = set(clarity_guard.get("protected_bands") or [])
    ranked: list[tuple[float, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for band_id, target_item in target_env.items():
        current_item = current_env.get(band_id)
        if not current_item:
            continue
        freq_hz = float(target_item["freq_hz"])
        delta = float(target_item["db"]) - float(current_item["db"])
        if abs(delta) < TIMBRE_ENVELOPE_DEAD_BAND_DB:
            continue
        budget_band = envelope_budget_band(freq_hz)

        if delta < 0.0:
            if budget_band in clarity_protected:
                skipped.append({
                    "envelope_band": band_id,
                    "budget_band": budget_band,
                    "freq_hz": round(freq_hz, 1),
                    "delta_db": round(delta, 2),
                    "reason": (
                        "原曲人声 stem 显示该清晰度频段已经比当前更亮；"
                        "跳过音色筛选片段驱动的高频 cut，避免把好干声做闷"
                    ),
                    "reference_clarity_guard": (clarity_guard.get("by_band") or {}).get(budget_band),
                })
                continue
            cap = TIMBRE_ENVELOPE_MAX_CUT_DB.get(budget_band, 0.6)
            amount = abs(delta) * TIMBRE_ENVELOPE_GAIN_FRACTION
            if budget_band in PRESENCE_BANDS:
                amount *= pre_presence_scale
                if isinstance(pre_presence_caps.get(budget_band), (int, float)):
                    cap = min(cap, float(pre_presence_caps[budget_band]))
            gain_db = -round(clamp(amount, TIMBRE_ENVELOPE_MIN_GAIN_DB, cap), 2)
            action_type = "cut"
        else:
            if budget_band not in TIMBRE_ENVELOPE_MAX_BOOST_DB:
                skipped.append({
                    "envelope_band": band_id,
                    "budget_band": budget_band,
                    "freq_hz": round(freq_hz, 1),
                    "delta_db": round(delta, 2),
                    "reason": "envelope boost disabled for this region",
                })
                continue
            allowed, block_reason = timbre_boost_allowed(
                budget_band,
                delta,
                analysis,
                current_tone,
                target_tone,
                safety,
            )
            if not allowed:
                skipped.append({
                    "envelope_band": band_id,
                    "budget_band": budget_band,
                    "freq_hz": round(freq_hz, 1),
                    "delta_db": round(delta, 2),
                    "reason": block_reason or "envelope boost blocked by timbre safety",
                })
                continue
            amount = delta * TIMBRE_ENVELOPE_GAIN_FRACTION
            gain_db = round(clamp(amount, TIMBRE_ENVELOPE_MIN_GAIN_DB, TIMBRE_ENVELOPE_MAX_BOOST_DB[budget_band]), 2)
            action_type = "boost"

        action = {
            "band": budget_band,
            "envelope_band": band_id,
            "type": action_type,
            "freq_hz": round(freq_hz, 1),
            "q": TIMBRE_ENVELOPE_Q,
            "gain_db": gain_db,
            "source": "timbre_reference_spectral_envelope",
            "reason": (
                f"音色筛选片段细分包络 {band_id} 与当前干声相差 {delta:+.1f} dB；"
                "只做少量宽峰修正，补足 8-band 粗匹配听感不明显的问题"
            ),
            "evidence": {
                "current_envelope_db": round(float(current_item["db"]), 2),
                "target_envelope_db": round(float(target_item["db"]), 2),
                "delta_db": round(delta, 2),
                "budget_band": budget_band,
                "gain_fraction": TIMBRE_ENVELOPE_GAIN_FRACTION,
                "cap_db": (
                    TIMBRE_ENVELOPE_MAX_CUT_DB.get(budget_band)
                    if action_type == "cut"
                    else TIMBRE_ENVELOPE_MAX_BOOST_DB.get(budget_band)
                ),
            },
        }
        ranked.append((abs(delta), action))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    actions: list[dict[str, Any]] = []
    used_budget_bands: set[str] = set()
    for _, action in ranked:
        budget_band = str((action.get("evidence") or {}).get("budget_band") or action.get("band") or "")
        if budget_band in used_budget_bands:
            continue
        actions.append(action)
        used_budget_bands.add(budget_band)
        if len(actions) >= TIMBRE_ENVELOPE_MAX_ACTIONS:
            break
    return actions, skipped


def build_timbre_reference_vocal_eq(
    timbre_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
    cleanup_actions: list[dict[str, Any]] | None = None,
    processing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """用音色筛选片段生成保守的人声音色匹配 EQ。

    这一步只看干声活动区的宽带音色形状，不参与响度、总线比例、
    伴奏 carve 或 master EQ，避免后续处理把“相似度”目标混进混音目标。
    """
    target = (timbre_features or {}).get("vocal_tonal_balance") or {}
    current = (input_features or {}).get("vocal_tonal_balance") or {}
    if not target or not current:
        return {
            "enabled": False,
            "actions": [],
            "reason": "missing timbre reference or input vocal tonal balance",
        }

    # 音色参考先于自清理决策：不再因为 cleanup 已命中同一频段而跳过。
    # 后置 cleanup / HF guard 仍会兜底，把泥、刺、齿音等风险控制住。
    _ = cleanup_actions
    safety = high_frequency_safety(analysis, current)
    presence_policy = (processing_context or {}).get("presence_band_policy") or {}
    clarity_guard = (processing_context or {}).get("reference_clarity_guard") or {}
    clarity_protected = set(clarity_guard.get("protected_bands") or [])
    pre_presence_scale = float(presence_policy.get("pre_timbre_cut_scale") or 1.0)
    pre_presence_caps = presence_policy.get("pre_timbre_cut_caps_db") or {}
    total_cut_budget = ((processing_context or {}).get("band_budget") or {}).get("max_total_cut_db") or {}
    ranked: list[tuple[float, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    for band, rule in SOURCE_VOCAL_EQ_BANDS.items():
        if band not in target or band not in current:
            continue
        delta = float(target[band]) - float(current[band])
        if abs(delta) < TIMBRE_EQ_DEAD_BAND_DB:
            continue

        if delta < 0.0:
            # 目标片段该频段更少时，只轻削一部分；这类动作比 boost 更安全。
            if "cut" not in rule["actions"]:
                continue
            if band in clarity_protected:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": (
                        "原曲人声 stem 显示该清晰度频段已经比当前更亮；"
                        "跳过音色筛选片段驱动的高频 cut，避免把好干声做闷"
                    ),
                    "reference_clarity_guard": (clarity_guard.get("by_band") or {}).get(band),
                })
                continue
            cap = TIMBRE_EQ_MAX_CUT_DB.get(band, VOCAL_SOURCE_EQ_MAX_CUT_DB)
            cut_fraction = TIMBRE_EQ_CUT_FRACTION
            if band in PRESENCE_BANDS:
                cut_fraction *= pre_presence_scale
                if isinstance(pre_presence_caps.get(band), (int, float)):
                    cap = min(cap, float(pre_presence_caps[band]))
            cleanup_cut = planned_cut_db(cleanup_actions, band)
            if isinstance(total_cut_budget.get(band), (int, float)):
                remaining = float(total_cut_budget[band]) - cleanup_cut
                if remaining <= 0.1:
                    skipped.append({
                        "band": band,
                        "delta_db": round(delta, 2),
                        "reason": (
                            f"{band} 已由 source cleanup 计划削减 {cleanup_cut:.1f} dB，"
                            "统一总预算已用完，跳过前置音色 cut"
                        ),
                    })
                    continue
                # 前置 timbre 只拿剩余额度的一小部分，避免后面清瑕疵没空间。
                cap = min(cap, remaining * 0.45)
            if cap < 0.25:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": f"{band} 剩余统一预算不足 0.25 dB，跳过前置音色 cut",
                })
                continue
            amount = clamp(abs(delta) * cut_fraction, 0.25, cap)
            action = {
                "band": band,
                "type": "cut",
                "freq_hz": rule["freq_hz"],
                "q": rule["q"],
                "gain_db": -round(amount, 2),
                "source": "timbre_reference_screened_clip",
                "reason": (
                    f"音色筛选片段 {band} 比当前干声低 {abs(delta):.1f} dB；"
                    "只做宽带轻削，避免后续链路改变音色相似度"
                ),
                "evidence": {
                    "current_db": round(float(current[band]), 2),
                    "target_db": round(float(target[band]), 2),
                    "delta_db": round(delta, 2),
                    "presence_policy": presence_policy.get("mode"),
                    "cut_cap_db": round(cap, 2),
                },
            }
        else:
            # boost 很容易把齿音、刺耳、分离噪声推出来，所以只允许少数中高频宽带轻补。
            if "boost" not in rule["actions"] or band not in TIMBRE_EQ_MAX_BOOST_DB:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": "boost disabled for this band to avoid unsafe tone shaping",
                })
                continue
            allowed, block_reason = timbre_boost_allowed(band, delta, analysis, current, target, safety)
            if not allowed:
                skipped.append({
                    "band": band,
                    "delta_db": round(delta, 2),
                    "reason": block_reason or "boost blocked by timbre safety",
                    "high_frequency_safety": safety if band in {"upper", "harsh", "sib", "air"} else None,
                })
                continue
            cap = TIMBRE_EQ_MAX_BOOST_DB[band]
            if band == "upper":
                cap = min(cap, VOCAL_UPPER_BOOST_MAX_BY_TEMPLATE.get(template_id, 1.0))
            amount = clamp(delta * TIMBRE_EQ_BOOST_FRACTION, 0.25, cap)
            action = {
                "band": band,
                "type": "boost",
                "freq_hz": rule["freq_hz"],
                "q": rule["q"],
                "gain_db": round(amount, 2),
                "source": "timbre_reference_screened_clip",
                "reason": (
                    f"音色筛选片段 {band} 比当前干声高 {delta:.1f} dB；"
                    "只补回一小部分，保留齿音/刺耳保护"
                ),
                "evidence": {
                    "current_db": round(float(current[band]), 2),
                    "target_db": round(float(target[band]), 2),
                    "delta_db": round(delta, 2),
                },
                "high_frequency_safety": safety if band in {"upper", "harsh", "sib", "air"} else None,
            }

        ranked.append((abs(delta), action))

    ranked.sort(key=lambda pair: pair[0], reverse=True)
    broad_actions = [action for _, action in ranked[:TIMBRE_EQ_MAX_ACTIONS]]
    envelope_actions, envelope_skipped = build_timbre_envelope_actions(
        timbre_features,
        input_features,
        analysis,
        current,
        target,
        presence_policy,
        safety,
        processing_context,
    )
    actions = [*broad_actions, *envelope_actions][:TIMBRE_EQ_TOTAL_MAX_ACTIONS]
    return {
        "enabled": bool(actions),
        "mode": "post_template_pre_group_fx",
        "actions": actions,
        "skipped": [*skipped, *envelope_skipped],
        "target_sources": (timbre_features or {}).get("sources") or {},
        "processing_context_version": (processing_context or {}).get("version"),
        "policy": (
            "用音色筛选片段做宽带、低幅度的人声音色匹配；"
            "先看 8-band 大方向，再用细分频谱包络补足可听差异；"
            "具体动作边界由统一 vocal_processing_context 约束"
        ),
    }


def build_accomp_carve_eq(
    ref_features: dict[str, Any],
    input_features: dict[str, Any] | None,
) -> dict[str, Any]:
    if input_features is None:
        return {
            "enabled": False,
            "actions": [],
            "reason": "missing input vocal/accompaniment features",
        }
    ref_gap = active_balance_value(ref_features)
    input_gap = active_balance_value(input_features)
    if ref_gap is None or input_gap is None:
        return {
            "enabled": False,
            "actions": [],
            "reason": "missing active vocal/accompaniment balance",
        }

    active_levels = input_features.get("active_band_levels") or {}
    vocal_levels = active_levels.get("vocal") or {}
    accomp_levels = active_levels.get("accomp") or {}
    ref_active_levels = ref_features.get("active_band_levels") or {}
    ref_vocal_levels = ref_active_levels.get("vocal") or {}
    ref_accomp_levels = ref_active_levels.get("accomp") or {}
    ref_vocal = ref_features.get("vocal_tonal_balance") or {}
    input_vocal = input_features.get("vocal_tonal_balance") or {}
    needed_lift = max(0.0, float(ref_gap) - float(input_gap))
    if needed_lift < 0.4:
        return {
            "enabled": False,
            "actions": [],
            "reference_active_gap_db": round(float(ref_gap), 2),
            "input_active_gap_db": round(float(input_gap), 2),
            "reason": "input vocal/accompaniment active gap is already close to the reference",
        }

    ranked: list[tuple[float, dict[str, Any]]] = []
    for band, rule in ACCOMP_CARVE_BANDS.items():
        if band not in vocal_levels or band not in accomp_levels:
            continue
        masking_db = float(accomp_levels[band]) - float(vocal_levels[band])
        ref_masking_db = None
        masking_excess = 0.0
        if band in ref_vocal_levels and band in ref_accomp_levels:
            ref_masking_db = float(ref_accomp_levels[band]) - float(ref_vocal_levels[band])
            masking_excess = masking_db - ref_masking_db
        elif masking_db > 0.0:
            # 兼容旧缓存：没有参考 masking 时，只把明显正 masking 当成过量。
            masking_excess = masking_db
        vocal_deficit = 0.0
        if band in ref_vocal and band in input_vocal:
            vocal_deficit = max(0.0, float(ref_vocal[band]) - float(input_vocal[band]))

        excess_over_deadband = max(0.0, masking_excess - ACCOMP_MASKING_EXCESS_DEAD_BAND_DB)
        if excess_over_deadband <= 0.0 and vocal_deficit < 1.5:
            continue
        pressure = (
            excess_over_deadband * 0.36
            + needed_lift * 0.24
            + vocal_deficit * 0.12
            + max(0.0, masking_db) * 0.04
        )
        amount = clamp(pressure * float(rule["weight"]), 0.5, ACCOMP_CARVE_MAX_CUT_DB)
        if amount < 0.5:
            continue
        priority = excess_over_deadband * 1.7 + needed_lift * 0.45 + vocal_deficit
        ranked.append(
            (
                priority,
                {
                    "band": band,
                    "region": ACCOMP_CARVE_REGION_BY_BAND.get(band, band),
                    "type": "cut",
                    "freq_hz": rule["freq_hz"],
                    "q": rule["q"],
                    "gain_db": -round(amount, 2),
                    "source": "reference_relative_masking_carve",
                    "reason": (
                        f"input active vocal gap {input_gap:+.1f} dB trails reference {ref_gap:+.1f} dB; "
                        f"current {band} masking {masking_db:+.1f} dB"
                        + (
                            f" vs reference {ref_masking_db:+.1f} dB"
                            if ref_masking_db is not None
                            else " without reference band masking"
                        )
                        + f" (excess {masking_excess:+.1f} dB)"
                    ),
                    "evidence": {
                        "current_masking_db": round(masking_db, 2),
                        "reference_masking_db": round(ref_masking_db, 2) if ref_masking_db is not None else None,
                        "masking_excess_db": round(masking_excess, 2),
                        "vocal_deficit_db": round(vocal_deficit, 2),
                        "needed_relative_vocal_lift_db": round(float(needed_lift), 2),
                    },
                },
            )
        )
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    actions: list[dict[str, Any]] = []
    used_regions: set[str] = set()
    for _, action in ranked:
        region = str(action.get("region") or action.get("band"))
        if region in used_regions:
            continue
        actions.append(action)
        used_regions.add(region)
        if len(actions) >= ACCOMP_CARVE_MAX_ACTIONS:
            break
    duck_reduction: dict[str, float] = {}
    duck_preserve: dict[str, float] = {}
    for action in actions:
        region = str(action.get("region") or action.get("band"))
        amount = abs(float(action.get("gain_db") or 0.0))
        duck_reduction[region] = round(max(duck_reduction.get(region, 0.0), amount), 2)
        evidence = action.get("evidence") or {}
        excess = float(evidence.get("masking_excess_db") or 0.0)
        if excess >= ACCOMP_MASKING_EXCESS_STRONG_DB:
            duck_preserve[region] = round(max(duck_preserve.get(region, 0.0), min(1.0, excess / 12.0)), 3)
    return {
        "enabled": bool(actions),
        "mode": "post_template_music_eq_pre_sum",
        "actions": actions,
        "duck_coordination": {
            "mode": "carve_reduces_duck",
            "regions": duck_reduction,
            "preserve_dynamic_duck": duck_preserve,
            "policy": (
                "one static carve per spectral problem region; matching dynamic duck bands are reduced, "
                "but strong reference-relative masking preserves more dynamic duck so a small carve does not "
                "pretend the masking problem is solved"
            ),
        },
        "reference_active_gap_db": round(float(ref_gap), 2),
        "input_active_gap_db": round(float(input_gap), 2),
        "needed_relative_vocal_lift_db": round(float(needed_lift), 2),
        "policy": "cut only; one carve per problem region, coordinated with vocal-aware ducking",
    }


def reverb_time_target(rt60_ms: float) -> float:
    if rt60_ms <= 0.0:
        return SPATIAL_BASELINE["rverb_time_s"]
    if rt60_ms < 5000.0:
        return 1.55
    if rt60_ms < 12000.0:
        return 2.10
    if rt60_ms < 18000.0:
        return 2.65
    return 3.20


def build_spatial_fx_plan(
    ref_features: dict[str, Any] | None,
    analysis: dict[str, Any] | None = None,
    effect_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """根据参考曲构建有上限的 vocal group 空间参数。"""
    if not ref_features:
        return {
            "enabled": False,
            "applied_to_render": False,
            "reason": "no_reference_features",
            "baseline": SPATIAL_BASELINE,
        }

    reverb = ref_features.get("reverb_proxy") or {}
    delay = ref_features.get("delay_proxy") or {}
    stem_quality = ref_features.get("vocal_stem_quality") or {}
    stem_spatial = ref_features.get("vocal_spatial_profile") or {}
    effect_spatial = (effect_context or {}).get("spatial") or {}
    spatial_decision = (effect_context or {}).get("spatial_decision") or {}
    spatial_mapping = spatial_decision.get("mapping") or {}
    reasons: list[str] = []
    center_led_reference = bool(effect_spatial.get("center_led", stem_spatial.get("near_mono_center_led")))
    group_ratios = (analysis or {}).get("group_ratios") or {}
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float((analysis or {}).get("body_to_presence") or 0.0)
    preserve_missing_presence = bool(
        effect_spatial.get("preserve_missing_presence")
        or (presence_ratio <= 0.03 and body_to_presence >= 16.0)
    )

    if bool(stem_quality.get("severe_leakage")):
        reasons.append("reference_vocal_stem_leakage_guard")

    valid_tail_count = int(reverb.get("valid_tail_count") or 0)
    if valid_tail_count < 8:
        reasons.append("too_few_stable_tail_events")

    tail_iqr = float(reverb.get("tail_iqr_db") or 0.0)
    reverb_conf = float(reverb.get("confidence") or 0.0)
    if tail_iqr > 10.0:
        reverb_conf *= 0.65
        reasons.append("tail_event_variance_penalty")

    active_inactive_gap = stem_quality.get("active_minus_inactive_db")
    if isinstance(active_inactive_gap, (int, float)) and float(active_inactive_gap) < 10.0:
        reverb_conf = min(reverb_conf, 0.70)

    if reverb_conf < 0.40:
        reasons.append("low_reverb_confidence")

    enabled = not any(reason in reasons for reason in (
        "reference_vocal_stem_leakage_guard",
        "too_few_stable_tail_events",
        "low_reverb_confidence",
    ))

    baseline = dict(SPATIAL_BASELINE)
    evidence = {
        "reverb_proxy": reverb,
        "delay_proxy": delay,
        "vocal_stem_quality": stem_quality,
        "vocal_spatial_profile": stem_spatial,
        "vocal_effect_target": effect_context,
        "spatial_decision": spatial_decision,
    }
    rt60_ms = float(reverb.get("est_rt60_ms") or 0.0)
    if center_led_reference or rt60_ms > 8000.0:
        # 1.1 先注释掉旧的“center-led / RT60 高就直接禁用 spatial_fx”硬 guard。
        # 这些情况仍然应该进入下面的有界映射：center-led 会自动降低 wet/width/delay，
        # RT60 proxy 不可信时也只通过上限收窄，而不是回退到 neutral stereo 或固定 rack。
        reasons.append("legacy_spatial_disable_guard_bypassed_in_1_1")
    if not enabled:
        return {
            "enabled": False,
            "applied_to_render": False,
            "reason": ",".join(reasons) if reasons else "disabled_by_policy",
            "baseline": baseline,
            "evidence": evidence,
        }

    tail_ratio = float(reverb.get("tail_to_onset_ratio_db") or -60.0)
    if bool(spatial_mapping.get("classic_faust_anchor")):
        # 1.1 空间链回到旧 Faust/Cubase rack 的稳定人格：
        # reference proxy 只能调白名单参数；0.1 的输入/输出和发送路径不改。
        # send level 可以按 reference 收窄/收干，但不能超过 0.1 预制上限，
        # 避免空间修正变成另一套隐形 bus balance。
        reasons.append("classic_faust_space_anchor")
        reasons.append("v0_1_io_send_path_lock")
        reasons.append("v0_1_effect_ceiling")
        reasons.append(f"spatial_guard_{spatial_decision.get('width_state', 'width_mapped')}")
        reasons.append(f"spatial_guard_{spatial_decision.get('clarity_risk', 'clarity_normal')}")
        if center_led_reference or rt60_ms > 8000.0:
            reasons.append("legacy_guard_replaced_by_classic_faust_anchor")

        rverb_send_pre_db = min(
            float(spatial_mapping.get("classic_rverb_send_pre_db", baseline["rverb_send_pre_db"])),
            baseline["rverb_send_pre_db"],
        )
        rverb = {
            "send_pre_db": round(clamp(
                rverb_send_pre_db,
                *SPATIAL_LIMITS["rverb_send_pre_db"],
            ), 3),
            "time_s": round(clamp(
                min(float(spatial_mapping.get("classic_rverb_time_s", baseline["rverb_time_s"])), baseline["rverb_time_s"]),
                *SPATIAL_LIMITS["rverb_time_s"],
            ), 3),
            "predelay_ms": round(clamp(
                min(float(spatial_mapping.get("classic_rverb_predelay_ms", baseline["rverb_predelay_ms"])), baseline["rverb_predelay_ms"]),
                *SPATIAL_LIMITS["rverb_predelay_ms"],
            ), 3),
            "early_ref_db": round(min(
                float(spatial_mapping.get("classic_rverb_early_ref_db", baseline["rverb_early_ref_db"])),
                baseline["rverb_early_ref_db"],
            ), 3),
            "damp": round(baseline["rverb_damp"], 3),
            "eq_hi_gain_db": round(clamp(
                min(
                    float(spatial_mapping.get("classic_rverb_eq_hi_gain_db", baseline["rverb_eq_hi_gain_db"])),
                    baseline["rverb_eq_hi_gain_db"],
                ),
                *SPATIAL_LIMITS["rverb_eq_hi_gain_db"],
            ), 3),
            "wet_delta_db": round(rverb_send_pre_db - baseline["rverb_send_pre_db"], 3),
            "confidence": round(reverb_conf, 3),
            "policy": "v0_1_send_path_locked_reference_params_with_ceiling",
        }

        output = {
            "side_trim_db": 0.0,
            "policy": "v0_1_output_path_locked_no_post_side_trim",
        }

        delay_conf = float(delay.get("confidence") or 0.0)
        supertap_send_pre_db = min(
            float(spatial_mapping.get("classic_supertap_send_pre_db", baseline["supertap_send_pre_db"])),
            baseline["supertap_send_pre_db"],
        )
        supertap_gain_db = min(
            float(spatial_mapping.get("classic_supertap_gain_db", baseline["supertap_gain_db"])),
            baseline["supertap_gain_db"],
        )
        supertap = {
            "send_pre_db": round(clamp(
                supertap_send_pre_db,
                *SPATIAL_LIMITS["supertap_send_pre_db"],
            ), 3),
            "gain_db": round(clamp(
                supertap_gain_db,
                *SPATIAL_LIMITS["supertap_gain_db"],
            ), 3),
            "feedback": round(clamp(
                min(
                    float(spatial_mapping.get("classic_supertap_feedback", baseline["supertap_feedback"])),
                    baseline["supertap_feedback"],
                ),
                *SPATIAL_LIMITS["supertap_feedback"],
            ), 3),
            "width": round(clamp(
                min(
                    float(spatial_mapping.get("classic_supertap_width", baseline["supertap_width"])),
                    baseline["supertap_width"],
                ),
                *SPATIAL_LIMITS["supertap_width"],
            ), 3),
            "color_hz": round(baseline["supertap_color_hz"], 1),
            "send_delta_db": round(supertap_send_pre_db - baseline["supertap_send_pre_db"], 3),
            "confidence": round(delay_conf, 3),
            "policy": "v0_1_send_path_locked_reference_params_with_ceiling",
        }

        shimmer = {
            "send_pre_db": round(baseline["shimmer_send_pre_db"], 3),
            "gain_db": round(baseline["shimmer_gain_db"], 3),
            "enabled": False,
            "confidence": 0.0,
            "policy": "hidden_by_default_first_rollout",
        }

        return {
            "enabled": True,
            "applied_to_render": True,
            "version": 2,
            "confidence": round(reverb_conf, 3),
            "baseline": baseline,
            "reverb": rverb,
            "output": output,
            "delay": supertap,
            "shimmer": shimmer,
            "limits": SPATIAL_LIMITS,
            "evidence": evidence,
            "guards": reasons,
            "policy": "v0_1_faust_io_send_path_locked_reference_params_with_ceiling",
        }

    wet_delta_target = clamp((tail_ratio + 12.0) / 12.0 * 4.0, 0.0, 4.0)
    wet_delta = wet_delta_target * reverb_conf
    time_target = float(spatial_mapping.get("time_target_s") or reverb_time_target(rt60_ms))
    predelay_target = clamp(12.0 + wet_delta_target * 4.0, 8.0, 28.0)
    if spatial_mapping:
        # 新空间契约直接控制 wet/time/predelay/early/delay：
        # center-led 只限制宽度，不再把纵深线索一并压没。
        wet_scale = float(spatial_mapping.get("wet_scale") or 1.0)
        wet_cap = float(spatial_mapping.get("wet_delta_cap_db") or 3.6)
        wet_delta = min(wet_delta * wet_scale, wet_cap)
        predelay_target = float(spatial_mapping.get("predelay_target_ms") or predelay_target)
        reasons.append(f"spatial_contract_{spatial_decision.get('depth_state', 'mapped')}")
        reasons.append(f"spatial_contract_{spatial_decision.get('delay_state', 'delay_mapped')}")
        if spatial_decision.get("clarity_risk") == "high":
            reasons.append("spatial_contract_clarity_guard")
    elif center_led_reference:
        wet_scale = float(effect_spatial.get("reverb_wet_scale") or 0.34)
        wet_delta = min(wet_delta * wet_scale, 1.35 * wet_scale)
        predelay_target = clamp(predelay_target + 4.0, 16.0, 32.0)
        reasons.append("center_led_reference_keep_depth_dry_front")
    if preserve_missing_presence and not spatial_mapping:
        wet_delta = min(wet_delta * 0.45, 0.75)
        time_target = min(time_target, 1.9)
        predelay_target = clamp(predelay_target + 2.0, 18.0, 32.0)
        reasons.append("missing_presence_keep_vocal_narrow_and_dry")
    reverb_eq_hi_gain = float(
        spatial_mapping.get(
            "reverb_eq_hi_gain_db",
            baseline["rverb_eq_hi_gain_db"] + reverb_conf * 0.30,
        )
    )
    if center_led_reference and not spatial_mapping:
        reverb_eq_hi_gain = min(reverb_eq_hi_gain, baseline["rverb_eq_hi_gain_db"] - 0.85)
    if preserve_missing_presence and not spatial_mapping:
        reverb_eq_hi_gain = min(reverb_eq_hi_gain, baseline["rverb_eq_hi_gain_db"] - 0.55)
    early_ref_db = float(
        spatial_mapping.get(
            "early_ref_db",
            baseline["rverb_early_ref_db"] - (1.5 if center_led_reference else 0.0),
        )
    )

    rverb = {
        "send_pre_db": round(clamp(
            baseline["rverb_send_pre_db"] + wet_delta,
            *SPATIAL_LIMITS["rverb_send_pre_db"],
        ), 3),
        "time_s": round(clamp(
            baseline["rverb_time_s"]
            + reverb_conf
            * float(spatial_mapping.get("time_scale") or effect_spatial.get("reverb_time_scale") or (0.50 if center_led_reference else 1.0))
            * (time_target - baseline["rverb_time_s"]),
            *SPATIAL_LIMITS["rverb_time_s"],
        ), 3),
        "predelay_ms": round(clamp(
            baseline["rverb_predelay_ms"] + reverb_conf * (predelay_target - baseline["rverb_predelay_ms"]),
            *SPATIAL_LIMITS["rverb_predelay_ms"],
        ), 3),
        "early_ref_db": round(early_ref_db, 3),
        "damp": round(baseline["rverb_damp"], 3),
        "eq_hi_gain_db": round(clamp(reverb_eq_hi_gain, *SPATIAL_LIMITS["rverb_eq_hi_gain_db"]), 3),
        "wet_delta_db": round(wet_delta, 3),
        "confidence": round(reverb_conf, 3),
    }

    output_side_trim_db = float(spatial_mapping.get("side_trim_db", effect_spatial.get("side_trim_db") or 0.0))
    output = {
        "side_trim_db": round(clamp(output_side_trim_db, *SPATIAL_LIMITS["output_side_trim_db"]), 3),
        "policy": "reference_vocal_center_led_side_trim",
    }

    delay_conf = float(delay.get("confidence") or 0.0)
    if spatial_mapping:
        delay_send_delta = float(spatial_mapping.get("delay_send_delta_db") or 0.0)
        feedback = float(spatial_mapping.get("delay_feedback") or baseline["supertap_feedback"])
        delay_width = float(spatial_mapping.get("delay_width") or baseline["supertap_width"])
        delay_policy = str(spatial_decision.get("delay_state") or "spatial_contract_delay")
    elif preserve_missing_presence:
        delay_send_delta = -7.0
        feedback = baseline["supertap_feedback"] * 0.45
        delay_width = 0.20
        delay_policy = "missing_presence_min_side_guard"
    elif center_led_reference:
        # 原曲人声接近居中时，delay 只能给纵深线索，不能制造明显侧向散开。
        delay_send_delta = -7.0
        feedback = baseline["supertap_feedback"] * 0.42
        delay_width = float(effect_spatial.get("delay_width_cap") or 0.28)
        delay_policy = "center_led_reference_delay_side_guard"
    elif delay_conf < 0.60:
        delay_send_delta = min(0.5, max(0.0, delay_conf * 0.8))
        feedback = baseline["supertap_feedback"]
        delay_width = baseline["supertap_width"]
        delay_policy = "low_confidence_light_send_only"
    else:
        delay_send_delta = min(2.5, delay_conf * 2.5)
        feedback = baseline["supertap_feedback"] + delay_conf * 0.05
        delay_width = baseline["supertap_width"]
        delay_policy = "bounded_reference_delay"
    supertap = {
        "send_pre_db": round(clamp(
            baseline["supertap_send_pre_db"] + delay_send_delta,
            *SPATIAL_LIMITS["supertap_send_pre_db"],
        ), 3),
        "gain_db": round(clamp(
            baseline["supertap_gain_db"] + delay_send_delta,
            *SPATIAL_LIMITS["supertap_gain_db"],
        ), 3),
        "feedback": round(clamp(feedback, *SPATIAL_LIMITS["supertap_feedback"]), 3),
        "width": round(clamp(delay_width, *SPATIAL_LIMITS["supertap_width"]), 3),
        "color_hz": round(baseline["supertap_color_hz"], 1),
        "send_delta_db": round(delay_send_delta, 3),
        "confidence": round(delay_conf, 3),
        "policy": delay_policy,
    }

    shimmer = {
        "send_pre_db": round(baseline["shimmer_send_pre_db"], 3),
        "gain_db": round(baseline["shimmer_gain_db"], 3),
        "enabled": False,
        "confidence": 0.0,
        "policy": "hidden_by_default_first_rollout",
    }

    return {
        "enabled": True,
        "applied_to_render": True,
        "version": 1,
        "confidence": round(reverb_conf, 3),
        "baseline": baseline,
        "reverb": rverb,
        "output": output,
        "delay": supertap,
        "shimmer": shimmer,
        "limits": SPATIAL_LIMITS,
        "evidence": evidence,
        "guards": reasons,
        "policy": "bounded_reference_mapping_confidence_blend_open_reference_only",
    }


def build_reference_overrides(
    ref_features: dict[str, Any],
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
    timbre_features: dict[str, Any] | None = None,
    processing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把参考/输入特征转成渲染器 overrides。"""
    effect_context = (processing_context or {}).get("vocal_effect_target") or build_vocal_effect_context(
        ref_features,
        input_features,
        analysis,
    )
    vocal_dynamics = build_vocal_dynamic_strategy(ref_features, input_features, effect_context=effect_context)
    overrides: dict[str, Any] = {
        "loudness_target": ref_features.get("loudness", {}),
        "reverb_observation": ref_features.get("reverb_proxy", {}),
        "vocal_effect_target": effect_context,
        "spatial_fx": build_spatial_fx_plan(ref_features, analysis, effect_context=effect_context),
        "reference_dynamics": ref_features.get("dynamics", {}),
        "vocal_dynamics": vocal_dynamics,
    }

    # 参考曲仍可用于响度、空间等非音色决策，但不能把成品拉向原曲 EQ 曲线。
    overrides["master_tilt_eq"] = {
        "enabled": False,
        "actions": [],
        "policy": "已禁用：不匹配原曲整体音色曲线，保留输入素材自身特点",
    }

    ref_balance = active_balance_value(ref_features)
    dry_input_balance = active_balance_value(input_features)
    ref_balance_block = (ref_features.get("vocal_accomp_balance") or {})
    bus = {
        "policy": "match_reference_stem_lufs_per_bus at render time",
        "balance_basis": "active_vocal_regions_preferred",
        "measurement_basis": "post_fx_vocal_group_active_regions",
    }
    if ref_balance_block.get("active_vocal_rms_db") is not None:
        bus["reference_active_vocal_rms_db"] = ref_balance_block["active_vocal_rms_db"]
    if ref_balance_block.get("active_accomp_rms_db") is not None:
        bus["reference_active_accomp_rms_db"] = ref_balance_block["active_accomp_rms_db"]
    if ref_balance is not None:
        bus["reference_vocal_minus_accomp_db"] = round(float(ref_balance), 2)
    if dry_input_balance is not None:
        bus["dry_input_vocal_minus_accomp_db"] = round(float(dry_input_balance), 2)
        bus["note"] = (
            "Dry input balance is diagnostic only. "
            "Render-time bus gains align post-FX vocal/accomp active-region RMS to the reference stems."
        )
    overrides["bus_balance"] = bus
    vocal_cleanup_eq = build_source_vocal_cleanup_eq(input_features, analysis, template_id)
    overrides["source_eq"] = {
        "vocal_eq": vocal_cleanup_eq,
        "timbre_vocal_eq": build_timbre_reference_vocal_eq(
            timbre_features,
            input_features,
            analysis,
            template_id,
            cleanup_actions=vocal_cleanup_eq.get("actions", []),
            processing_context=processing_context,
        ),
        "accomp_eq": {
            "enabled": False,
            "actions": [],
            "policy": "已禁用：基于参考曲的伴奏 carve 不属于保留素材特点的通用清理",
        },
    }
    overrides["vocal_processing_context"] = processing_context
    overrides["vocal_hf_guard"] = build_vocal_hf_guard(
        analysis,
        input_features,
        timbre_features,
        ref_features,
        processing_context=processing_context,
    )
    overrides["dry_vocal_strategy"] = build_dry_vocal_strategy(analysis, input_features, template_id)

    return overrides


def build_vocal_dynamic_strategy(
    ref_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    effect_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """比较输入干声和原曲人声 stem 的微动态差异。"""
    ref_dyn = (ref_features or {}).get("vocal_dynamics") or {}
    input_dyn = (input_features or {}).get("vocal_dynamics") or {}
    if not ref_dyn or not input_dyn:
        return {
            "enabled": False,
            "reason": "missing reference/input vocal dynamic profile",
            "policy": "只做诊断：缺少原曲或输入干声动态特征时不处理",
        }

    effect_gap = ((effect_context or {}).get("dynamics") or {}).get("gap") or {}

    def dyn_gap(key: str) -> float:
        if isinstance(effect_gap.get(key), (int, float)):
            return float(effect_gap[key])
        return float(ref_dyn.get(key) or 0.0) - float(input_dyn.get(key) or 0.0)

    range_gap = dyn_gap("frame_range_p90_p10_db")
    micro_gap = dyn_gap("micro_range_p95_p50_db")
    micro_p99_gap = dyn_gap("micro_range_p99_p50_db")
    level_gap = dyn_gap("active_rms_db")
    peak_gap = dyn_gap("peak_db")
    crest_gap = float(ref_dyn.get("crest_db") or 0.0) - float(input_dyn.get("crest_db") or 0.0)
    level_weak = level_gap >= 2.5 and peak_gap >= 1.8
    weak = (
        range_gap >= VOCAL_DYNAMIC_RANGE_WEAK_DB
        or micro_gap >= VOCAL_DYNAMIC_MICRO_WEAK_DB
        or micro_p99_gap >= VOCAL_DYNAMIC_MICRO_P99_WEAK_DB
        or level_weak
    )
    severity = max(
        0.0,
        range_gap / max(VOCAL_DYNAMIC_RANGE_WEAK_DB * 2.0, 1e-6),
        micro_gap / max(VOCAL_DYNAMIC_MICRO_WEAK_DB * 2.0, 1e-6),
        micro_p99_gap / max(VOCAL_DYNAMIC_MICRO_P99_WEAK_DB * 2.0, 1e-6),
        (level_gap - 2.0) / 5.0,
        (peak_gap - 1.5) / 4.0,
    )
    severity = clamp(severity, 0.0, 1.0)
    max_lift_db = clamp(0.85 + severity * 1.25, 0.0, VOCAL_DYNAMIC_MAX_LIFT_DB) if weak else 0.0
    max_cut_db = clamp(0.18 + severity * 0.37, 0.0, VOCAL_DYNAMIC_MAX_CUT_DB) if weak else 0.0
    contrast_amount = clamp(0.17 + severity * 0.21, 0.0, VOCAL_DYNAMIC_MAX_CONTRAST) if weak else 0.0
    return {
        "enabled": bool(weak),
        "mode": "light_vocal_dynamic_lift" if weak else "diagnostic_vocal_dynamic_flatness",
        "reference": ref_dyn,
        "input": input_dyn,
        "gap": {
            "frame_range_p90_p10_db": round(range_gap, 3),
            "micro_range_p95_p50_db": round(micro_gap, 3),
            "micro_range_p99_p50_db": round(micro_p99_gap, 3),
            "active_rms_db": round(level_gap, 3),
            "peak_db": round(peak_gap, 3),
            "crest_db": round(crest_gap, 3),
        },
        "thresholds": {
            "frame_range_weak_db": VOCAL_DYNAMIC_RANGE_WEAK_DB,
            "micro_range_weak_db": VOCAL_DYNAMIC_MICRO_WEAK_DB,
            "micro_range_p99_weak_db": VOCAL_DYNAMIC_MICRO_P99_WEAK_DB,
            "active_rms_weak_db": 2.5,
            "peak_weak_db": 1.8,
        },
        "triggered_by": [
            name for name, gap, threshold in (
                ("frame_range", range_gap, VOCAL_DYNAMIC_RANGE_WEAK_DB),
                ("micro_range", micro_gap, VOCAL_DYNAMIC_MICRO_WEAK_DB),
                ("micro_range_p99", micro_p99_gap, VOCAL_DYNAMIC_MICRO_P99_WEAK_DB),
                ("active_rms", level_gap, 2.5),
                ("peak", peak_gap, 1.8),
            )
            if gap >= threshold
        ],
        "processing": {
            "contrast_amount": round(contrast_amount, 3),
            "max_lift_db": round(max_lift_db, 3),
            "max_cut_db": round(max_cut_db, 3),
            "frame_ms": 50.0,
            "hop_ms": 25.0,
            "attack_ms": 35.0,
            "release_ms": 125.0,
            "peak_ceiling": 0.97,
            "severity": round(severity, 3),
            "hard_caps": {
                "contrast_amount": VOCAL_DYNAMIC_MAX_CONTRAST,
                "max_lift_db": VOCAL_DYNAMIC_MAX_LIFT_DB,
                "max_cut_db": VOCAL_DYNAMIC_MAX_CUT_DB,
            },
        },
        "policy": (
            "当输入人声短帧动态明显比原曲 stem 更平时，只做保守微动态对比；"
            "限制最大提升/回收并做峰值保护，避免把瑕疵源推炸。"
        ),
    }


def build_source_cleanup_overrides(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
    template_id: str,
    timbre_features: dict[str, Any] | None = None,
    ref_features: dict[str, Any] | None = None,
    processing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建通用清理块：即使没有加载参考曲也会使用。"""
    vocal_cleanup_eq = build_source_vocal_cleanup_eq(input_features, analysis, template_id)
    return {
        "source_eq": {
            "vocal_eq": vocal_cleanup_eq,
            "timbre_vocal_eq": build_timbre_reference_vocal_eq(
                timbre_features,
                input_features,
                analysis,
                template_id,
                cleanup_actions=vocal_cleanup_eq.get("actions", []),
                processing_context=processing_context,
            ),
            "accomp_eq": {
                "enabled": False,
                "actions": [],
                # 伴奏 carve 有时能提升人声可懂度，但如果按参考曲目标去挖，
                # 会改变伴奏/编曲原本的音色，所以通用清理阶段不做。
                "policy": "已禁用：通用素材清理不重新塑造伴奏音色",
            },
        },
        "vocal_processing_context": processing_context,
        "vocal_hf_guard": build_vocal_hf_guard(
            analysis,
            input_features,
            timbre_features,
            ref_features,
            processing_context=processing_context,
        ),
        "vocal_artifact_repair": build_vocal_artifact_repair(
            analysis,
            input_features,
            processing_context=processing_context,
        ),
        "dry_vocal_strategy": build_dry_vocal_strategy(analysis, input_features, template_id),
        "policy": (
            "通用且保留素材特点的清理：自驱动人声问题频段削减 + "
            "原生 Nyquist/高频保护；音色方向看筛选片段，处理边界看原曲人声/伴奏关系"
        ),
    }


def build_vocal_artifact_repair(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
    processing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """判断是否需要轻量修复生成/分离带来的毛刺和金属感。

    这里只做“修瑕疵”，不是换音色：触发条件来自干声自身的 peakiness 和
    原生采样率/Nyquist 墙；处理动作是 FFmpeg 自带的轻量去点击和轻度频谱降噪。
    """
    _ = input_features
    native_sr = analysis.get("native_sample_rate")
    peakiness = {
        "upper": float(analysis.get("peakiness_upper") or 0.0),
        "harsh": float(analysis.get("peakiness_harsh") or 0.0),
        "sib": float(analysis.get("peakiness_sib") or 0.0),
    }
    high_hits = [band for band, peak in peakiness.items() if peak >= HF_GUARD_PEAK_HARD_DB]
    max_peak = max(peakiness.values())
    low_native_sr = isinstance(native_sr, (int, float)) and float(native_sr) <= 24000.0
    electric_profile = len(high_hits) >= HF_GUARD_ELECTRIC_MIN_HITS
    group_ratios = analysis.get("group_ratios") or {}
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    # presence 极低且 body 明显偏重时，不是高频层太多，而是高频/咬字本来就少。
    # 这种声音不能继续收高频，只能靠伴奏让位和比例补偿来提升可懂度。
    preserve_missing_presence = presence_ratio <= 0.03 and body_to_presence >= 16.0

    actions: list[dict[str, Any]] = []
    reasons: list[str] = []
    severe_artifact = False
    presence_policy = (processing_context or {}).get("presence_band_policy") or {}
    repair_scale = float(presence_policy.get("repair_strength_scale") or 1.0)
    if preserve_missing_presence:
        reasons.append("presence 极低且 body_to_presence 很高；跳过高频 repair，避免越修越不清楚")
    elif electric_profile and max_peak >= 9.0:
        reasons.append(f"高频多个频段同时尖峰，最高 peakiness={max_peak:.1f} dB")
        if repair_scale < 1.0:
            reasons.append(
                f"统一决策层判定 {presence_policy.get('mode')}，repair 强度缩放为 {repair_scale:.2f}"
            )
        # 单个 upper 极端尖峰也会形成“爆音/电流感”，尤其低原生采样率干声。
        # severe 仍然只分层修高频，不动中低频主体，也不按歌名触发。
        severe_artifact = (max_peak >= 18.0 and len(high_hits) >= 2) or (max_peak >= 12.0 and len(high_hits) >= 3)
        actions.append({
            "type": "adeclick",
            "window": 40,
            "overlap": 75,
            "arorder": 4,
            "threshold": 2.0 if severe_artifact else 2.5,
            "burst": 2,
            "reason": "去掉短促毛刺/点击感，不改变持续音色",
        })
        actions.append({
            "type": "afftdn",
            "noise_reduction": round((6.0 if severe_artifact else 3.5) * repair_scale, 2),
            "noise_floor": -58 if severe_artifact else -56,
            "residual_floor": -45 if severe_artifact else -42,
            "adaptivity": 0.25 if severe_artifact else 0.35,
            "gain_smooth": 14 if severe_artifact else 10,
            "reason": "轻度平滑生成/分离的高频沙粒感",
        })
        if severe_artifact:
            reasons.append("判定为严重受损：upper/harsh/sib 三段同时尖且最高超过 9.5 dB")
            actions.append({
                "type": "afwtdn",
                "sigma": round(0.018 * repair_scale, 4),
                "levels": 8,
                "percent": round(35 * repair_scale, 1),
                "softness": 3.0,
                "samples": 8192,
                "reason": "严重受损时只在高频层加一点 wavelet 平滑，减少发毛/颗粒感",
            })
            actions.append({
                "type": "deesser",
                "intensity": round(0.22 * repair_scale, 3),
                "max_deessing": round(0.38 * repair_scale, 3),
                "frequency": 0.55,
                "reason": "严重受损时额外压齿音/金属边缘，强度保持保守",
            })
    elif electric_profile and low_native_sr:
        reasons.append("低原生采样率且高频多个频段同时偏尖")
        actions.append({
            "type": "adeclick",
            "window": 40,
            "overlap": 75,
            "arorder": 4,
            "threshold": 3.0,
            "burst": 2,
            "reason": "只做保守去点击，避免过度降噪吃掉人声细节",
        })

    return {
        "enabled": bool(actions),
        "mode": "split_high_repair" if severe_artifact else "inline_repair",
        "crossover_hz": 2600.0 if severe_artifact else None,
        "high_layer_gain_db": -1.2 if severe_artifact else 0.0,
        "actions": actions,
        "trigger": {
            "native_sample_rate": native_sr,
            "low_native_sample_rate": bool(low_native_sr),
            "electric_profile": electric_profile,
            "preserve_missing_presence": preserve_missing_presence,
            "high_peak_hits": high_hits,
            "peakiness": {key: round(value, 2) for key, value in peakiness.items()},
            "presence": round(presence_ratio, 4),
            "body_to_presence": round(body_to_presence, 3),
            "presence_policy": presence_policy.get("mode"),
            "repair_strength_scale": round(repair_scale, 3),
        },
        "reasons": reasons,
        "policy": "按统一决策层触发轻量 repair：干声瑕疵决定是否修，原曲人声站位决定修复强度。",
    }


def build_vocal_hf_guard(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
    timbre_features: dict[str, Any] | None = None,
    ref_features: dict[str, Any] | None = None,
    processing_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """低通重采样颗粒，并轻削分离/生成导致的“电”“金属”共振。

    依据干声原生采样率（Nyquist 墙）和各频段 peakiness 触发。
    输出动作会被 scripts/apply_vocal_plan_eq.py 当普通 EQ 滤波处理：
    一个高位 lowpass + 若干窄带 resonance cut，不新增 DSP 阶段。
    设计上保持保守：只 cut / lowpass，不 boost。
    """
    native_sr = analysis.get("native_sample_rate")
    nyquist = analysis.get("effective_nyquist_hz")
    if isinstance(native_sr, (int, float)) and native_sr > 0:
        native_nyquist = float(nyquist) if isinstance(nyquist, (int, float)) else float(native_sr) / 2.0
    else:
        native_nyquist = None

    actions: list[dict[str, Any]] = []
    tags: list[str] = []
    input_tone = (input_features or {}).get("vocal_tonal_balance") or {}
    timbre_tone = (timbre_features or {}).get("vocal_tonal_balance") or {}
    presence_policy = (processing_context or {}).get("presence_band_policy") or {}
    if presence_policy:
        reference_presence_policy = {
            "mode": presence_policy.get("mode"),
            "reference_vocal_minus_accomp_db": (
                (processing_context or {}).get("reference_balance") or {}
            ).get("vocal_minus_accomp_db"),
            "cut_caps_db": presence_policy.get("hf_cut_caps_db") or {},
            "reason": presence_policy.get("reason"),
        }
    else:
        reference_presence_policy = reference_presence_hf_policy(ref_features)
    reference_cut_caps = reference_presence_policy.get("cut_caps_db") or {}
    timbre_delta = {
        band: float(timbre_tone.get(band, 0.0)) - float(input_tone.get(band, 0.0))
        for band in HF_GUARD_ELECTRIC_BANDS
        if isinstance(timbre_tone.get(band), (int, float)) and isinstance(input_tone.get(band), (int, float))
    }
    clarity_guard = (processing_context or {}).get("reference_clarity_guard") or {}
    clarity_protected = set(clarity_guard.get("protected_bands") or [])
    peakiness = {
        "upper": float(analysis.get("peakiness_upper") or 0.0),
        "harsh": float(analysis.get("peakiness_harsh") or 0.0),
        "sib": float(analysis.get("peakiness_sib") or 0.0),
    }
    hits = [b for b in HF_GUARD_ELECTRIC_BANDS if peakiness[b] >= HF_GUARD_PEAK_HARD_DB]
    electric = len(hits) >= HF_GUARD_ELECTRIC_MIN_HITS

    # 1) 低通真实 Nyquist 墙以上的编码/重采样颗粒。
    lowpass_hz: float | None = None
    if native_nyquist is not None:
        for limit_hz, lp_hz in HF_GUARD_LOWPASS_BY_NYQUIST:
            if native_nyquist <= limit_hz + 1.0:
                lowpass_hz = lp_hz
                break
    # 不能只因为原生 24k/32k 就低通；好干声可能本来已经处理干净。
    # 当原曲 stem 明确需要保留 sib/air，且没有整体电/金属感时，低通会把清晰度做闷。
    lowpass_blocked_by_clarity = bool({"sib", "air"} & clarity_protected) and not electric
    if lowpass_hz is not None and not lowpass_blocked_by_clarity:
        tags.append("nyquist_grain_lowpass")
        actions.append({
            "band": "air",
            "type": "lowpass",
            "freq_hz": round(lowpass_hz, 1),
            "q": HF_GUARD_LOWPASS_Q,
            "source": "vocal_hf_guard",
            "reason": (
                f"native nyquist {native_nyquist:.0f} Hz -> low-pass resampling grain "
                f"above {lowpass_hz:.0f} Hz"
            ),
        })
    elif lowpass_hz is not None and lowpass_blocked_by_clarity:
        tags.append("clarity_guard_skip_nyquist_lowpass")

    # 2) 处理“电/金属感”分离噪声：upper/harsh/sib 整体都尖时，轻削窄带共振。
    group_ratios = analysis.get("group_ratios") or {}
    presence_ratio = float(group_ratios.get("presence") or 0.0)
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    preserve_missing_presence = presence_ratio <= 0.03 and body_to_presence >= 16.0
    if electric and not preserve_missing_presence:
        tags.append("electric_separation_noise")
    for band in HF_GUARD_ELECTRIC_BANDS:
        # presence 极低且 body 明显偏重时，不能再因为局部 peakiness 去削 upper/sib。
        if preserve_missing_presence:
            continue
        peak = peakiness[band]
        if peak < HF_GUARD_PEAK_HARD_DB:
            continue
        # 非整体电/金属感时，只轻削独立的 upper 硬峰。
        if not electric and band != "upper":
            continue
        if band in clarity_protected and not electric:
            tags.append(f"clarity_guard_skip_{band}_hf_cut")
            continue
        excess = peak - HF_GUARD_PEAK_HARD_DB
        cut = clamp(HF_GUARD_TAME_PER_PEAK_DB * (1.0 + excess), 0.5, HF_GUARD_TAME_MAX_CUT_DB)
        if timbre_delta.get(band, 0.0) >= 1.8:
            if not electric:
                tags.append(f"timbre_preserve_skip_{band}_hf_cut")
                continue
            # 整体电/金属感仍要兜底，但目标音色明确需要该频段时减半，避免抵消相似度。
            cut = min(cut, 0.8)
        if isinstance(reference_cut_caps.get(band), (int, float)):
            capped_cut = min(cut, float(reference_cut_caps[band]))
            if capped_cut < cut:
                tags.append(f"reference_presence_cap_{band}_hf_cut")
            cut = capped_cut
        actions.append({
            "band": band,
            "type": "cut",
            "freq_hz": HF_GUARD_TAME_FREQ_HZ[band],
            "q": HF_GUARD_TAME_Q,
            "gain_db": -round(cut, 2),
            "source": "vocal_hf_guard",
            "reason": (
                f"{band} peakiness {peak:.1f} dB 已超过 {HF_GUARD_PEAK_HARD_DB:.0f} dB；"
                f"轻削 {cut:.1f} dB 控制窄带共振"
            ),
        })

    return {
        "enabled": bool(actions),
        "actions": actions,
        "tags": tags,
        "native_sample_rate": native_sr,
        "effective_nyquist_hz": nyquist,
        "lowpass_hz": lowpass_hz,
        "electric_profile": electric,
        "preserve_missing_presence": preserve_missing_presence,
        "timbre_delta_db": {k: round(v, 2) for k, v in timbre_delta.items()},
        "reference_presence_policy": reference_presence_policy,
        "processing_context_version": (processing_context or {}).get("version"),
        "peakiness": {k: round(v, 2) for k, v in peakiness.items()},
        "policy": (
            "低通原生 Nyquist 墙以上的重采样颗粒，并轻削整体偏尖的高频段"
            "（AI 分离/生成的金属感噪声）。削减幅度同时受音色筛选片段和原曲人声/伴奏位置约束。"
        ),
    }


def build_vocal_sibilance_profile(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
) -> dict[str, Any]:
    """按单首干声 harsh/sib 能量和 crest 估计 de-esser 配置。"""
    bands = ((input_features or {}).get("active_band_levels") or {}).get("vocal") or {}
    sib_db = bands.get("sib")
    harsh_db = bands.get("harsh")
    crest_db = ((analysis or {}).get("dynamics") or {}).get("crest_db")

    ess_freq = 7500.0
    if isinstance(sib_db, (int, float)) and isinstance(harsh_db, (int, float)):
        delta = float(sib_db) - float(harsh_db)
        if delta >= 2.0:
            ess_freq = 8000.0
        elif delta <= -2.0:
            ess_freq = 6800.0

    thresh_db = -20.0
    candidates = [v for v in (sib_db, harsh_db) if isinstance(v, (int, float))]
    if candidates:
        thresh_db = clamp(max(candidates) - 5.0, -30.0, -12.0)

    range_db = 12.0
    if isinstance(crest_db, (int, float)) and float(crest_db) < 10.0:
        range_db = 8.0

    # 刺耳/齿音明显尖时加深 de-essing：旧固定阈值会让强齿音素材处理不足。
    # 阈值越低，触发越多；range 越宽，削减深度越大。
    sib_peak = float((analysis or {}).get("peakiness_sib") or 0.0)
    harsh_peak = float((analysis or {}).get("peakiness_harsh") or 0.0)
    peak_max = max(sib_peak, harsh_peak)
    peak_adapt_tag: str | None = None
    if peak_max >= 8.0:
        peak_adapt_tag = "peaky_hf_deepened"
        thresh_db = clamp(thresh_db - min(3.0, peak_max - 8.0 + 1.0), -30.0, -12.0)
        range_db = clamp(range_db + min(4.0, peak_max - 8.0 + 1.0), 8.0, 16.0)

    return {
        "ess_freq_hz": round(float(ess_freq), 1),
        "thresh_db": round(float(thresh_db), 2),
        "range_db": round(float(range_db), 2),
        "peak_adapt": peak_adapt_tag,
        "source": {
            "vocal_sib_db": sib_db,
            "vocal_harsh_db": harsh_db,
            "crest_db": crest_db,
            "peakiness_sib": round(sib_peak, 2),
            "peakiness_harsh": round(harsh_peak, 2),
        },
    }


def build_plan(
    analysis: dict[str, Any],
    fallback: str = "template_d",
    ref_features: dict[str, Any] | None = None,
    input_features: dict[str, Any] | None = None,
    timbre_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template_id, label = select_template(analysis, fallback=fallback)
    template = load_json(TEMPLATE_DIR / f"{template_id}.raw.json")

    common = load_json(TEMPLATE_DIR / "common_group_fx.raw.json")
    links = load_json(TEMPLATE_DIR / "preset_links.json")
    template_links = links.get(template_id, {})
    vocal_sibilance_profile = build_vocal_sibilance_profile(analysis, input_features)
    vocal_processing_context = build_vocal_processing_context(
        ref_features,
        input_features,
        timbre_features,
        analysis,
        template_id,
    )
    fusion_intent = build_fusion_intent(
        analysis,
        ref_features,
        template_id,
        vocal_processing_context,
    )

    reference_block: dict[str, Any] | None = None
    if ref_features is not None:
        reference_block = {
            "features": ref_features,
            "input_features": input_features,
            "timbre_features": timbre_features,
            "vocal_processing_context": vocal_processing_context,
            "fusion_intent": fusion_intent,
            "overrides": build_reference_overrides(
                ref_features,
                input_features,
                analysis,
                template_id,
                timbre_features=timbre_features,
                processing_context=vocal_processing_context,
            ),
        }

    if template_id == "template_d":
        plan = {
            "analysis": analysis,
            "classification_label": label,
            "selected_template": template_id,
            "selected_template_name": template.get("display_name"),
            "render_mode": "current_faust_default",
            "template": template,
            "timbre_features": timbre_features,
            # 顶层 source_cleanup 会优先于旧 reference overrides 被读取，
            # 因此 --no-reference 渲染也能得到同样的问题频段保护。
            "source_cleanup": build_source_cleanup_overrides(
                analysis,
                input_features,
                template_id,
                timbre_features=timbre_features,
                ref_features=ref_features,
                processing_context=vocal_processing_context,
            ),
            "vocal_processing_context": vocal_processing_context,
            "fusion_intent": fusion_intent,
            "vocal_sibilance_profile": vocal_sibilance_profile,
            "notes": [
                "Template D uses the older current project chain.",
                "A/B/C use scripts/render_template_mix.sh for plugin-order DSP approximations."
            ],
        }
        if reference_block is not None:
            plan["reference"] = reference_block
        return plan

    plan = {
        "analysis": analysis,
        "classification_label": label,
        "selected_template": template_id,
        "selected_template_name": template.get("display_name"),
        "render_mode": "template_dsp_approximation_chain",
        "timbre_features": timbre_features,
        # 把“保留素材特点的清理”独立放在 reference overrides 外面。
        # 渲染器会优先读取这里，避免以后新增参考曲特征时误改人声音色。
        "source_cleanup": build_source_cleanup_overrides(
            analysis,
            input_features,
            template_id,
            timbre_features=timbre_features,
            ref_features=ref_features,
            processing_context=vocal_processing_context,
        ),
        "vocal_processing_context": vocal_processing_context,
        "fusion_intent": fusion_intent,
        "vocal_sibilance_profile": vocal_sibilance_profile,
        "residual_vocal_eq": build_residual_vocal_eq(analysis, template_id, ref_features, input_features),
        "vocal_track": {
            **{k: v for k, v in template["vocal_track"].items() if k != "insert_chain"},
            "insert_chain": enrich_chain(
                template["vocal_track"].get("insert_chain", []),
                template_links.get("vocal_track", {}),
            ),
        },
        "accompaniment_track": {
            **{k: v for k, v in template["accompaniment_track"].items() if k != "insert_chain"},
            "insert_chain": enrich_chain(
                template["accompaniment_track"].get("insert_chain", []),
                template_links.get("accompaniment_track", {}),
            ),
        },
        "group_fx": common,
        "preset_links": template_links,
        "notes": [
            "This plan records the selected Cubase template and preset snapshots.",
            "Rendering uses template-specific Faust approximation chains. Exact proprietary parameter maps remain marked as uncertain where not decoded."
        ],
    }
    if reference_block is not None:
        plan["reference"] = reference_block
    return plan


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve mix template from analyzer JSON.")
    parser.add_argument("analysis_json", type=Path)
    parser.add_argument("--output", type=Path, default=Path("resolved_mix_plan.json"))
    parser.add_argument("--fallback", default="template_d")
    args = parser.parse_args()

    analysis = load_json(args.analysis_json)
    plan = build_plan(analysis, fallback=args.fallback)
    write_json(args.output, plan)
    print(json.dumps({
        "selected_template": plan["selected_template"],
        "classification_label": plan.get("classification_label"),
        "output": str(args.output),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
