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


def build_residual_vocal_eq(analysis: dict[str, Any], template_id: str) -> dict[str, Any]:
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
MASTER_TILT_MAX_CUT_DB = 3.0
MASTER_TILT_MAX_BOOST_DB = 0.8
MASTER_TILT_MAX_ACTIONS = 4
BUS_BALANCE_MAX_MOVE_DB = 6.0
BUS_BALANCE_DEAD_BAND_DB = 0.6


def build_reference_overrides(
    ref_features: dict[str, Any],
    input_features: dict[str, Any] | None,
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

    ref_balance = (ref_features.get("vocal_accomp_balance") or {}).get("vocal_minus_accomp_db")
    input_balance = ((input_features or {}).get("vocal_accomp_balance") or {}).get("vocal_minus_accomp_db")
    bus = {
        "vocal_bus_gain_db": 0.0,
        "accomp_bus_gain_db": 0.0,
        "policy": "no positive gain on vocal or accompaniment buses",
    }
    if ref_balance is not None and input_balance is not None:
        delta = float(ref_balance) - float(input_balance)
        if abs(delta) >= BUS_BALANCE_DEAD_BAND_DB:
            move = clamp(abs(delta), 0.0, BUS_BALANCE_MAX_MOVE_DB)
            if delta > 0:
                bus["accomp_bus_gain_db"] = -round(move, 2)
                bus["reason"] = (
                    f"reference vocal is {ref_balance:+.1f} dB vs accomp; input is {input_balance:+.1f} dB. "
                    f"Cut accomp bus by {move:.1f} dB to lift vocal relatively."
                )
            else:
                bus["vocal_bus_gain_db"] = -round(move, 2)
                bus["reason"] = (
                    f"reference vocal is {ref_balance:+.1f} dB vs accomp; input is {input_balance:+.1f} dB. "
                    f"Cut vocal bus by {move:.1f} dB."
                )
            bus["reference_vocal_minus_accomp_db"] = round(float(ref_balance), 2)
            bus["input_vocal_minus_accomp_db"] = round(float(input_balance), 2)
    overrides["bus_balance"] = bus

    return overrides


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

    reference_block: dict[str, Any] | None = None
    if ref_features is not None:
        reference_block = {
            "features": ref_features,
            "input_features": input_features,
            "overrides": build_reference_overrides(ref_features, input_features),
        }

    if template_id == "template_d":
        plan = {
            "analysis": analysis,
            "classification_label": label,
            "selected_template": template_id,
            "selected_template_name": template.get("display_name"),
            "render_mode": "current_faust_default",
            "template": template,
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
        "residual_vocal_eq": build_residual_vocal_eq(analysis, template_id),
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
