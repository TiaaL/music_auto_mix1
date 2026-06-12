#!/usr/bin/env python3
"""Create a resolved mix plan from spectral template analyzer JSON."""

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
    """Keep one corrective move per band/type, using the stronger absolute gain."""
    for index, existing in enumerate(actions):
        if existing.get("band") == action.get("band") and existing.get("type") == action.get("type"):
            if abs(float(action.get("gain_db", 0.0))) > abs(float(existing.get("gain_db", 0.0))):
                actions[index] = action
            return
    actions.append(action)


def classification_hits(analysis: dict[str, Any]) -> list[dict[str, Any]]:
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
    """EQ actions from spectral target-curve deviations not covered by the selected template."""
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
    """EQ actions from ratio bands outside neutral range that are not covered by the template."""
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


def filter_residual_high_boosts(
    actions: list[dict[str, Any]],
    ref_features: dict[str, Any] | None,
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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

    # Source 2: spectral deviation from target curve (uncovered bands only)
    for action in spectral_deviation_residual_actions(analysis, coverage, config):
        append_residual_action(actions, action)

    # Source 3: ratio bands outside neutral range (uncovered bands only)
    for action in ratio_excess_residual_actions(analysis, coverage, config):
        append_residual_action(actions, action)

    max_actions = int(config.get("max_actions", 4))
    actions = (actions + covered_strong_actions)[:max_actions]
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

SOURCE_VOCAL_EQ_BANDS = {
    "low":    {"freq_hz": 130.0,   "q": 0.8, "actions": ("cut",)},
    "lowmid": {"freq_hz": 320.0,   "q": 0.9, "actions": ("cut", "boost")},
    "mid":    {"freq_hz": 800.0,   "q": 0.8, "actions": ("cut", "boost")},
    "upper":  {"freq_hz": 2800.0,  "q": 0.9, "actions": ("cut", "boost")},
    "harsh":  {"freq_hz": 6200.0,  "q": 1.2, "actions": ("cut",)},
    "sib":    {"freq_hz": 9500.0,  "q": 1.4, "actions": ("cut",)},
    "air":    {"freq_hz": 14000.0, "q": 0.7, "actions": ("cut", "boost")},
}

ACCOMP_CARVE_BANDS = {
    "lowmid": {"freq_hz": 320.0,  "q": 0.9, "weight": 0.7},
    "mid":    {"freq_hz": 800.0,  "q": 0.8, "weight": 0.9},
    "upper":  {"freq_hz": 2800.0, "q": 0.9, "weight": 1.0},
    "harsh":  {"freq_hz": 6200.0, "q": 1.2, "weight": 0.6},
}

VOCAL_SOURCE_EQ_DEAD_BAND_DB = 1.2
VOCAL_SOURCE_EQ_MAX_CUT_DB = 2.5
VOCAL_SOURCE_EQ_MAX_BOOST_DB = 1.5
VOCAL_SOURCE_EQ_MAX_ACTIONS = 4
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
ACCOMP_CARVE_REGION_BY_BAND = {
    "lowmid": "body",
    "mid": "presence",
    "upper": "presence",
    "harsh": "presence",
}


def build_dry_vocal_strategy(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
    template_id: str,
) -> dict[str, Any]:
    ratios = analysis.get("ratios") or {}
    group_ratios = analysis.get("group_ratios") or {}
    body_to_presence = float(analysis.get("body_to_presence") or 0.0)
    classification = analysis.get("classification") or {}
    tags: list[str] = []
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
        duck_profile["body_extra_db"] += 0.25
        duck_profile["presence_extra_db"] += 0.45
    if lowmid_ratio >= 0.70 or body_to_presence >= 12.0:
        tags.append("presence_masked_by_body")
        duck_profile["presence_extra_db"] += 0.35
    if body_ratio >= 0.85 and presence_ratio <= 0.08:
        tags.append("dark_or_muffled_dry_vocal")
        duck_profile["air_extra_db"] += 0.20
    if float(ratios.get("low") or 0.0) >= 0.06 or float(ratios.get("sub") or 0.0) >= 0.004:
        tags.append("dry_vocal_low_pressure")
        duck_profile["low_extra_db"] += 0.25
    if upper_peak >= 12.0 or harsh_peak >= 8.0:
        tags.append("peaky_presence")
        duck_profile["presence_extra_db"] = max(0.0, duck_profile["presence_extra_db"] - 0.20)

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
        "ratios": {
            "lowmid": round(lowmid_ratio, 4),
            "body": round(body_ratio, 4),
            "presence": round(presence_ratio, 4),
            "body_to_presence": round(body_to_presence, 3),
        },
        "duck_profile": {key: round(value, 3) for key, value in duck_profile.items()},
        "policy": (
            "Use dry vocal spectral shape to decide where accompaniment yields; "
            "overall vocal/accomp gain still follows the reference active ratio conservatively."
        ),
    }


def active_balance_value(features: dict[str, Any] | None) -> float | None:
    balance = (features or {}).get("vocal_accomp_balance") or {}
    active = balance.get("active_vocal_minus_accomp_db")
    if active is not None:
        return float(active)
    value = balance.get("vocal_minus_accomp_db")
    return float(value) if value is not None else None


def high_frequency_safety(analysis: dict[str, Any], input_vocal: dict[str, Any]) -> dict[str, Any]:
    """Return conservative evidence for whether upper/air boosts are safe."""
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


def vocal_high_boost_cap(
    band: str,
    template_id: str,
    delta: float,
    ref_vocal: dict[str, Any],
    input_vocal: dict[str, Any],
    analysis: dict[str, Any],
    safety: dict[str, Any],
) -> tuple[float, str | None]:
    """Template/evidence-gated caps for rendered upper/air boosts."""
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


def build_reference_vocal_eq(
    ref_features: dict[str, Any],
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> dict[str, Any]:
    ref_vocal = ref_features.get("vocal_tonal_balance") or {}
    input_vocal = (input_features or {}).get("vocal_tonal_balance") or {}
    if not ref_vocal or not input_vocal:
        return {
            "enabled": False,
            "actions": [],
            "reason": "missing reference/input vocal tonal features",
        }

    ranked: list[tuple[float, dict[str, Any]]] = []
    skipped: list[dict[str, Any]] = []
    high_safety = high_frequency_safety(analysis, input_vocal)
    for band, rule in SOURCE_VOCAL_EQ_BANDS.items():
        if band not in ref_vocal or band not in input_vocal:
            continue
        delta = float(ref_vocal[band]) - float(input_vocal[band])
        if abs(delta) < VOCAL_SOURCE_EQ_DEAD_BAND_DB:
            continue
        if delta > 0:
            if "boost" not in rule["actions"]:
                continue
            cap, skip_reason = vocal_high_boost_cap(
                band, template_id, delta, ref_vocal, input_vocal, analysis, high_safety
            )
            if cap <= 0.0:
                skipped.append({
                    "band": band,
                    "type": "boost",
                    "delta_db": round(delta, 2),
                    "reason": skip_reason or "boost cap is zero",
                    "safety": high_safety if band in {"upper", "air"} else None,
                })
                continue
            amount = clamp(delta * 0.45, 0.5, cap)
            action_type = "boost"
            gain = round(amount, 2)
        else:
            if "cut" not in rule["actions"]:
                continue
            amount = clamp(abs(delta) * 0.55, 0.5, VOCAL_SOURCE_EQ_MAX_CUT_DB)
            action_type = "cut"
            gain = -round(amount, 2)
        ranked.append(
            (
                abs(delta),
                {
                    "band": band,
                    "type": action_type,
                    "freq_hz": rule["freq_hz"],
                    "q": rule["q"],
                    "gain_db": gain,
                    "source": "reference_vocal_tonal_delta",
                    "reason": (
                        f"reference vocal {band} {ref_vocal[band]:+.1f} dB vs input vocal "
                        f"{input_vocal[band]:+.1f} dB (delta {delta:+.1f} dB)"
                    ),
                    "evidence": (
                        {"high_frequency_safety": high_safety}
                        if band in {"upper", "air"} and action_type == "boost"
                        else None
                    ),
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
            "match current dry vocal tonal shape toward the reference, but gate upper/air boosts by "
            "template and harsh/sibilance safety evidence; 14k air is conservative"
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
        if masking_db < -8.0 and needed_lift < 2.0:
            continue
        vocal_deficit = 0.0
        if band in ref_vocal and band in input_vocal:
            vocal_deficit = max(0.0, float(ref_vocal[band]) - float(input_vocal[band]))
        pressure = (masking_db + 8.0) * 0.18 + needed_lift * 0.30 + vocal_deficit * 0.15
        amount = clamp(pressure * float(rule["weight"]), 0.5, ACCOMP_CARVE_MAX_CUT_DB)
        if amount < 0.5:
            continue
        priority = masking_db + needed_lift + vocal_deficit
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
                    "source": "active_vocal_masking_carve",
                    "reason": (
                        f"input active vocal gap {input_gap:+.1f} dB trails reference {ref_gap:+.1f} dB; "
                        f"accomp-vocal masking in {band} is {masking_db:+.1f} dB"
                    ),
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
    for action in actions:
        region = str(action.get("region") or action.get("band"))
        amount = abs(float(action.get("gain_db") or 0.0))
        duck_reduction[region] = round(max(duck_reduction.get(region, 0.0), amount), 2)
    return {
        "enabled": bool(actions),
        "mode": "post_template_music_eq_pre_sum",
        "actions": actions,
        "duck_coordination": {
            "mode": "carve_reduces_duck",
            "regions": duck_reduction,
            "policy": (
                "one static carve per spectral problem region; matching dynamic duck bands are reduced "
                "so accompaniment is not carved and ducked hard for the same issue"
            ),
        },
        "reference_active_gap_db": round(float(ref_gap), 2),
        "input_active_gap_db": round(float(input_gap), 2),
        "needed_relative_vocal_lift_db": round(float(needed_lift), 2),
        "policy": "cut only; one carve per problem region, coordinated with vocal-aware ducking",
    }


def build_reference_overrides(
    ref_features: dict[str, Any],
    input_features: dict[str, Any] | None,
    analysis: dict[str, Any],
    template_id: str,
) -> dict[str, Any]:
    """Translate reference + input features into renderer parameter overrides.

    Memory-rule clamps applied here:
      - bus gain <= 0 (no positive gain on vocals or accompaniment)
      - reverb is observation-only in v1 (clean/small preset still owned by Faust binary)
    """
    overrides: dict[str, Any] = {
        "loudness_target": ref_features.get("loudness", {}),
        "reverb_observation": ref_features.get("reverb_proxy", {}),
        "reference_dynamics": ref_features.get("dynamics", {}),
    }

    actions: list[dict[str, Any]] = []
    ref_tonal = ref_features.get("tonal_balance") or {}
    input_tonal = (input_features or {}).get("tonal_balance") or {}
    if ref_tonal and input_tonal:
        ranked: list[tuple[float, dict[str, Any]]] = []
        for band, rule in MASTER_TILT_BANDS.items():
            if band not in ref_tonal or band not in input_tonal:
                continue
            delta = float(ref_tonal[band]) - float(input_tonal[band])
            if abs(delta) < MASTER_TILT_DEAD_BAND_DB:
                continue
            if delta > 0:
                if "boost" not in rule["actions"]:
                    continue
                gain = round(min(delta, MASTER_TILT_MAX_BOOST_DB), 2)
                action_type = "boost"
            else:
                if "cut" not in rule["actions"]:
                    continue
                gain = round(-min(-delta, MASTER_TILT_MAX_CUT_DB), 2)
                action_type = "cut"
            ranked.append(
                (
                    abs(delta),
                    {
                        "band": band,
                        "type": action_type,
                        "freq_hz": rule["freq_hz"],
                        "q": rule["q"],
                        "gain_db": gain,
                        "source": "reference_tonal_delta",
                        "reason": (
                            f"reference {band} {ref_tonal[band]:+.1f} dB vs input mix {input_tonal[band]:+.1f} dB "
                            f"(delta {delta:+.1f} dB)"
                        ),
                    },
                )
            )
        ranked.sort(key=lambda pair: pair[0], reverse=True)
        actions = [action for _, action in ranked[:MASTER_TILT_MAX_ACTIONS]]
    overrides["master_tilt_eq"] = {
        "enabled": bool(actions),
        "actions": actions,
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
    overrides["source_eq"] = {
        "vocal_eq": build_reference_vocal_eq(ref_features, input_features, analysis, template_id),
        "accomp_eq": build_accomp_carve_eq(ref_features, input_features),
    }
    overrides["dry_vocal_strategy"] = build_dry_vocal_strategy(analysis, input_features, template_id)

    return overrides


def build_vocal_sibilance_profile(
    analysis: dict[str, Any],
    input_features: dict[str, Any] | None,
) -> dict[str, Any]:
    """Derive per-song de-esser params from vocal harsh/sib band energy + crest.

    Output is consumed by scripts/build_rdeesser_dyn.sh to compile a song-specific
    rdeesser_dyn binary. Heuristic only — kept conservative to avoid changing
    perceived volume balance.
    """
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

    return {
        "ess_freq_hz": round(float(ess_freq), 1),
        "thresh_db": round(float(thresh_db), 2),
        "range_db": round(float(range_db), 2),
        "source": {
            "vocal_sib_db": sib_db,
            "vocal_harsh_db": harsh_db,
            "crest_db": crest_db,
        },
    }


def build_plan(
    analysis: dict[str, Any],
    fallback: str = "template_d",
    ref_features: dict[str, Any] | None = None,
    input_features: dict[str, Any] | None = None,
) -> dict[str, Any]:
    template_id, label = select_template(analysis, fallback=fallback)
    template = load_json(TEMPLATE_DIR / f"{template_id}.raw.json")

    common = load_json(TEMPLATE_DIR / "common_group_fx.raw.json")
    links = load_json(TEMPLATE_DIR / "preset_links.json")
    template_links = links.get(template_id, {})
    vocal_sibilance_profile = build_vocal_sibilance_profile(analysis, input_features)

    reference_block: dict[str, Any] | None = None
    if ref_features is not None:
        reference_block = {
            "features": ref_features,
            "input_features": input_features,
            "overrides": build_reference_overrides(ref_features, input_features, analysis, template_id),
        }

    if template_id == "template_d":
        plan = {
            "analysis": analysis,
            "classification_label": label,
            "selected_template": template_id,
            "selected_template_name": template.get("display_name"),
            "render_mode": "current_faust_default",
            "template": template,
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
