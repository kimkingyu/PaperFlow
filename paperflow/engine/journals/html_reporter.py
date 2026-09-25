"""Standalone HTML reporter for academic journal recommendations.

Produces self-contained, responsive, security-hardened HTML reports
with zero CDN / remote asset dependencies.
"""
from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import JournalError
from .identity import normalize_indexing


SENSITIVE_QUERY_KEYS = {
    "token", "access_token", "auth", "api_key", "apikey", "secret",
    "secretkey", "password", "pwd", "key", "code", "session", "session_id",
    "uuid", "ticket", "sig", "signature", "credential", "auth_token",
}


def clean_safe_url(url: Optional[str]) -> Optional[str]:
    """Clean and validate URL. Strictly allow only http/https without credentials or sensitive query keys."""
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except Exception:
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return None

    if not parts.hostname or not parts.netloc:
        return None

    if parts.username or parts.password:
        return None

    # Filter out sensitive credentials or session identifiers from query parameters
    if parts.query:
        try:
            query_pairs = parse_qsl(parts.query, keep_blank_values=True)
            safe_query_pairs = [
                (k, v) for k, v in query_pairs
                if k.lower() not in SENSITIVE_QUERY_KEYS
            ]
            new_query = urlencode(safe_query_pairs)
            parts = parts._replace(query=new_query)
        except Exception:
            return None

    return urlunsplit(parts)


def escape_txt(val: Any) -> str:
    """Safely escape text for HTML output."""
    if val is None:
        return ""
    return html.escape(str(val), quote=True)


def safe_dom_id(val: str) -> str:
    """Generate a valid, deterministic DOM id."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", str(val))
    return cleaned or "default"


def _safe_json_for_script(data: Any) -> str:
    """Serialize data into JSON with script tag escape protection."""
    serialized = json.dumps(data, ensure_ascii=False)
    return (
        serialized.replace("</", "<\\/")
        .replace("<!--", "<\\!--")
        .replace("<script", "<\\script")
    )


def _format_currency_amount(amount: Optional[float], currency: str) -> str:
    if amount is None:
        return "金额未知"
    curr_str = f" {currency}" if currency else ""
    if amount == 0:
        return f"0{curr_str}"
    return f"{amount:,.2f}{curr_str}"


def _parse_card_cost(cost: Any) -> Dict[str, Any]:
    """Extract and format fee info across all routes, distinguishing 0 from unknown."""
    if not isinstance(cost, dict):
        return {
            "type": "unknown",
            "cost_categories": ["unknown"],
            "badge_class": "badge-unknown",
            "summary": "费用未知 / 待核验",
            "route": "未知",
            "currency": "",
            "amount_str": "未知",
            "has_zero_apc": False,
            "has_paid": False,
            "quotes": [],
            "routes_summary": [],
            "needs_verification": True,
        }

    routes = cost.get("routes") or []
    selected = cost.get("selected") or {}

    # Inspect all available routes (hybrid journals frequently have 0 APC subscription and paid OA options)
    has_zero = False
    has_paid = False
    has_unknown = False

    check_targets = list(routes)
    if selected and selected not in check_targets:
        check_targets.append(selected)

    routes_summary = []
    for r in check_targets:
        if not isinstance(r, dict):
            continue
        r_route = r.get("route", "unknown")
        r_curr = r.get("currency", "")
        r_known = r.get("known_charges")
        r_est = r.get("estimated_total")

        if r_known == 0.0 or r_est == 0.0:
            has_zero = True
        elif (r_known is not None and r_known > 0) or (r_est is not None and r_est > 0):
            has_paid = True
        elif r_known is None and r_est is None:
            has_unknown = True

        amt = r_est if r_est is not None else r_known
        routes_summary.append({
            "route": r_route,
            "currency": r_curr,
            "amount": amt,
            "needs_verification": r.get("needs_verification", False),
        })

    # Determine cost filter categories
    cost_categories = []
    if has_zero:
        cost_categories.append("zero")
    if has_paid:
        cost_categories.append("paid")
    if not cost_categories:
        cost_categories.append("unknown")

    # Primary route information from selected (if available)
    sel_route = selected.get("route") or "unknown"
    sel_curr = selected.get("currency") or ""
    sel_known = selected.get("known_charges")
    sel_est = selected.get("estimated_total")
    sel_needs_verif = bool(selected.get("needs_verification", True))
    quotes = selected.get("quotes") or []

    route_map = {
        "open_access": "开放获取 (Open Access)",
        "subscription": "传统订阅 / 闭源 (Subscription)",
        "diamond": "钻石 OA (Diamond OA)",
        "unknown": "路线待核验",
    }
    route_zh = route_map.get(sel_route, sel_route)

    # Distinguish summary string
    if has_zero and has_paid:
        badge_class = "badge-info"
        summary = "含零 APC 路线（仅免出版版面费，非总费用全免）并提供付费 OA 选项"
        amt = sel_est if sel_est is not None else sel_known
        amt_fmt = _format_currency_amount(amt, sel_curr) if amt is not None else "付费可选"
        amount_str = f"含 0 APC 路线 / {amt_fmt}"
    elif has_zero:
        badge_class = "badge-success"
        summary = "含零 APC 路线（仅免出版版面费，非总费用全免；可能存在审稿/彩印/超页费）"
        amount_str = f"0 {sel_curr}".strip()
    elif has_paid:
        badge_class = "badge-info"
        amt = sel_est if sel_est is not None else sel_known
        amt_fmt = _format_currency_amount(amt, sel_curr)
        verif_note = "（含税费或附加费待核验）" if sel_needs_verif else "（已核验证据）"
        summary = f"原币预估：{amt_fmt} {verif_note}"
        amount_str = amt_fmt
    else:
        badge_class = "badge-warning"
        summary = "费用未知 / 待核验"
        amount_str = "未知"

    return {
        "type": " ".join(cost_categories),
        "cost_categories": cost_categories,
        "badge_class": badge_class,
        "summary": summary,
        "route": route_zh,
        "currency": sel_curr,
        "amount_str": amount_str,
        "has_zero_apc": has_zero,
        "has_paid": has_paid,
        "quotes": quotes,
        "routes_summary": routes_summary,
        "needs_verification": sel_needs_verif,
    }


def _parse_card_timing(timing: Any) -> Dict[str, Any]:
    """Extract review stage timing without confusing with final acceptance."""
    if not isinstance(timing, dict):
        return {
            "summary": "周期未知 / 待核验",
            "stage_zh": "待核验阶段",
            "days_str": "未知",
            "upper_days": None,
            "observations": [],
        }

    stage = timing.get("stage") or "first_decision"
    stage_map = {
        "first_decision": "初审周期（含编辑初筛，非外审/录用时间）",
        "peer_review": "同行评审周期（外审阶段，非最终录用时间）",
        "acceptance": "最终录用周期（历史统计参考）",
    }
    stage_zh = stage_map.get(stage, f"{stage} 阶段（非录用时间）")
    upper_days = timing.get("upper_days")
    observations = timing.get("observations") or []

    if upper_days is not None and upper_days >= 0:
        days_str = f"≤ {upper_days:.0f} 天 ({upper_days / 7:.1f} 周)"
        summary = f"{days_str} [{stage_zh}]"
    else:
        days_str = "未知"
        summary = f"周期未知 / 待核验 [{stage_zh}]"

    return {
        "summary": summary,
        "stage_zh": stage_zh,
        "days_str": days_str,
        "upper_days": upper_days,
        "observations": observations,
    }


def _parse_card_indexing(indexing: Any) -> Dict[str, Any]:
    """Detect indexing categories: sci, ei, dual, or unknown."""
    if not isinstance(indexing, list):
        idx_list = []
    else:
        idx_list = [str(x).strip() for x in indexing if str(x).strip()]

    idx_upper = {normalize_indexing(x) for x in idx_list}
    has_sci = "SCIE" in idx_upper
    has_ei = "EI" in idx_upper

    if has_sci and has_ei:
        cat = "dual"
        label = "SCI + EI 双检索"
        badge = "badge-primary"
    elif has_sci:
        cat = "sci"
        label = "SCIE / SCI"
        badge = "badge-primary"
    elif has_ei:
        cat = "ei"
        label = "EI 检索"
        badge = "badge-secondary"
    else:
        cat = "unknown"
        label = "其他 / 待核验收录"
        badge = "badge-muted"

    return {
        "category": cat,
        "label": label,
        "badge": badge,
        "raw_list": idx_list,
    }


def _parse_card_risk(risk: Any) -> Dict[str, Any]:
    """Parse multi-source risk radar results."""
    if not isinstance(risk, dict):
        return {
            "type": "warning",
            "label": "风险未核验",
            "badge_class": "badge-warning",
            "conclusion": "needs_verification",
            "checks": [],
        }

    conclusion = risk.get("conclusion") or "needs_verification"
    checks = risk.get("checks") or []

    if conclusion == "flagged":
        return {
            "type": "flagged",
            "label": "存在风险预警 / 规则拦截",
            "badge_class": "badge-danger",
            "conclusion": conclusion,
            "checks": checks,
        }
    elif conclusion == "needs_verification":
        return {
            "type": "warning",
            "label": "部分项目待核验 / 数据过期",
            "badge_class": "badge-warning",
            "conclusion": conclusion,
            "checks": checks,
        }
    else:
        return {
            "type": "ok",
            "label": "已核验源未见预警",
            "badge_class": "badge-success",
            "conclusion": conclusion,
            "checks": checks,
        }


def _parse_card_experiences(exp_list: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Parse experiences safely, flagging unknown sample size and needs_verification status."""
    if not isinstance(exp_list, list) or len(exp_list) == 0:
        return "none", []

    cleaned = []
    for e in exp_list:
        if not isinstance(e, dict):
            continue
        sample_size = e.get("sample_size")
        sample_size_str = f"{sample_size} 例" if sample_size is not None else "样本量未知"
        raw_url = e.get("url") or ""
        safe_link = clean_safe_url(raw_url)
        prov = e.get("provenance") or {}
        needs_verif = bool(e.get("needs_verification", False))

        cleaned.append({
            "field": e.get("field") or "",
            "topic": e.get("topic") or "",
            "summary": e.get("summary") or "（无详细摘要）",
            "sample_size_str": sample_size_str,
            "safe_url": safe_link,
            "data_year": prov.get("data_year") or "",
            "observed_at": prov.get("observed_at") or "",
            "needs_verification": needs_verif,
        })

    exp_type = "has" if cleaned else "none"
    return exp_type, cleaned


def _parse_card_metrics(metrics: Any) -> List[Dict[str, Any]]:
    """Format key metrics for display."""
    if not isinstance(metrics, list):
        return []
    formatted = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        name = m.get("name") or ""
        val = m.get("value")
        year = m.get("year")
        raw = m.get("raw") or ""
        unit = m.get("unit") or ""
        year_str = f" ({year})" if year else ""

        if name == "impact_factor":
            label = "影响因子 (IF)"
            val_str = f"{val:.2f}" if isinstance(val, (int, float)) else (raw or "未知")
            formatted.append({"label": label, "value": f"{val_str}{year_str}"})
        elif name == "annual_articles":
            label = "年发文量"
            val_str = f"{int(val):,} 篇" if isinstance(val, (int, float)) else (raw or "未知")
            formatted.append({"label": label, "value": f"{val_str}{year_str}"})
    return formatted


def _extract_all_cards(groups_data: Dict[str, Any], unclassified_data: List[Any]) -> List[Dict[str, Any]]:
    """Flatten all cards preserving order, metadata, and group assignments."""
    cards = []
    group_keys = ["efficiency", "balanced", "stretch"]
    for gk in group_keys:
        g = groups_data.get(gk) or {}
        for c in g.get("recommended") or []:
            if isinstance(c, dict):
                item = dict(c)
                item["_ui_group"] = gk
                item["_ui_tier"] = "recommended"
                cards.append(item)
        for c in g.get("provisional") or []:
            if isinstance(c, dict):
                item = dict(c)
                item["_ui_group"] = gk
                item["_ui_tier"] = "provisional"
                cards.append(item)

    for c in unclassified_data:
        if isinstance(c, dict):
            item = dict(c)
            item["_ui_group"] = "unclassified"
            item["_ui_tier"] = "provisional"
            cards.append(item)

    return cards


def render_html_report(result: dict) -> str:
    """Render a standalone, security-hardened, responsive HTML report from recommendation output."""
    if not isinstance(result, dict):
        result = {}

    data = result.get("data") if isinstance(result.get("data"), dict) else result
    if not isinstance(data, dict):
        data = {}

    profile = data.get("profile") or {}
    groups = data.get("groups") or {}
    unclassified = data.get("unclassified") or []
    excluded = data.get("excluded") or []
    warnings = result.get("warnings") or data.get("warnings") or []
    narrative_guidance = data.get("narrative_guidance") or []
    coverage = result.get("coverage") or data.get("coverage") or {}
    stage = data.get("stage", "scored")
    evaluated_at = data.get("evaluated_at")

    # Research title
    profile_title = profile.get("title") or ""
    profile_summary = profile.get("summary") or ""
    research_theme = profile_title.strip() or profile_summary[:80].strip() or "学术期刊智能推荐报告"

    # Flatten cards
    all_cards = _extract_all_cards(groups, unclassified)

    # Display summary from engine
    display_summary = data.get("display_summary") or {}
    target_minimum = display_summary.get("target_minimum")
    shortfall = display_summary.get("shortfall", 0)

    # Calculate statistics (prefer display_summary when present, fallback to counted)
    count_recommended = display_summary.get("supported_recommendations", sum(1 for c in all_cards if c.get("_ui_tier") == "recommended"))
    count_provisional = display_summary.get("provisional_candidates", sum(1 for c in all_cards if c.get("_ui_tier") == "provisional"))
    count_unclassified = sum(1 for c in all_cards if c.get("_ui_group") == "unclassified")
    count_excluded = len(excluded)
    count_total = len(all_cards)

    # Time display
    time_str = evaluated_at or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # Prepare rendered HTML sections
    rendered_cards_html = []
    rendered_table_rows_html = []

    for idx, card in enumerate(all_cards, start=1):
        raw_journal_id = str(card.get("journal_id") or f"j-{idx}")
        safe_jid = safe_dom_id(raw_journal_id)
        journal_id = escape_txt(raw_journal_id)
        title = escape_txt(card.get("title") or "未命名期刊")
        issns = card.get("issns") or []
        issns_str = ", ".join(escape_txt(i) for i in issns) if issns else "无 ISSN"
        group_key = card.get("_ui_group") or "unclassified"
        tier = card.get("_ui_tier") or "provisional"

        # Parsing details
        cost_info = _parse_card_cost(card.get("cost"))
        timing_info = _parse_card_timing(card.get("timing"))
        idx_info = _parse_card_indexing(card.get("indexing"))
        risk_info = _parse_card_risk(card.get("risk"))
        exp_type, experiences = _parse_card_experiences(card.get("experiences"))
        metrics_list = _parse_card_metrics(card.get("metrics"))
        provenance = card.get("provenance") or {}
        metadata_observations = card.get("metadata_observations") or []
        total_exp_count = card.get("experience_count", len(experiences))
        homepage = clean_safe_url(card.get("homepage"))
        if homepage:
            homepage_html = (f'<a class="homepage-link" id="homepage-{safe_dom_id(raw_journal_id)}" '
                             f'href="{escape_txt(homepage)}" target="_blank" rel="noopener noreferrer" '
                             f'title="{escape_txt(homepage)}">官网 ↗</a>')
        else:
            homepage_html = '<span class="homepage-missing">官网待核验</span>'

        # Search index text
        search_terms = " ".join([
            str(card.get("title") or ""),
            raw_journal_id,
            " ".join(issns),
            " ".join(str(f) for f in card.get("fields") or []),
            idx_info["label"],
            cost_info["route"],
            risk_info["label"],
        ]).lower()

        # Score parsing
        score_obj = card.get("score")
        if isinstance(score_obj, dict):
            score_val = score_obj.get("value")
            score_str = f"{score_val:.1f}" if score_val is not None else "待评估"
            score_range = score_obj.get("range") or [score_val, score_val]
            range_str = f"[{score_range[0]:.1f}, {score_range[1]:.1f}]" if len(score_range) >= 2 and score_range[0] is not None and score_range[1] is not None else ""
            completeness = score_obj.get("evidence_completeness", 0)
            components = score_obj.get("components") or {}
        else:
            score_str = "待评估"
            range_str = ""
            completeness = 0
            components = {}

        # Missing & Gaps
        missing_items = card.get("missing") or []
        improvements = card.get("improvements") or []
        rationale = card.get("rationale") or ""
        special_tip = card.get("special_issue_tip") or ""

        # Eligibility badge
        if tier == "recommended":
            eligibility_badge = '<span class="badge badge-success">有证据推荐</span>'
            row_status_badge = '<span class="badge badge-success">有证据推荐</span>'
        else:
            eligibility_badge = '<span class="badge badge-warning">待核验候选</span>'
            row_status_badge = '<span class="badge badge-warning">待核验候选</span>'

        # Group label
        group_badge_labels = {
            "efficiency": '<span class="badge badge-efficiency">稳妥／效率档</span>',
            "balanced": '<span class="badge badge-balanced">均衡档</span>',
            "stretch": '<span class="badge badge-stretch">冲刺／顶刊档</span>',
            "unclassified": '<span class="badge badge-muted">未分类候选</span>',
        }
        group_badge = group_badge_labels.get(group_key, group_badge_labels["unclassified"])

        # Rankings & OA tags
        oa_mode = card.get("oa_mode") or "unknown"
        oa_labels = {
            "full": "完全开放获取 (OA)",
            "hybrid": "混合出版 (Hybrid)",
            "closed": "传统订阅 (Closed)",
            "diamond": "钻石免版面费 OA",
            "unknown": "OA模式待核验",
        }
        oa_badge = f'<span class="tag tag-oa">{escape_txt(oa_labels.get(oa_mode, oa_mode))}</span>'

        rankings = card.get("rankings") or []
        ranking_tags = []
        for r in rankings:
            if not isinstance(r, dict):
                continue
            sys_name = escape_txt((r.get("system") or "").upper())
            q = r.get("quartile")
            q_str = f"Q{q}" if q is not None else ""
            g = escape_txt(r.get("grade") or "")
            top_flag = r.get("top")
            top_str = " (Top)" if top_flag is True else ""
            tag_text = f"{sys_name} {q_str}{g}{top_str}".strip()
            if tag_text:
                ranking_tags.append(f'<span class="tag tag-rank">{escape_txt(tag_text)}</span>')
        rankings_html = " ".join(ranking_tags) if ranking_tags else '<span class="text-muted">分区待核验</span>'

        # Metrics badges
        metrics_html = " ".join(f'<span class="tag tag-metric">{escape_txt(m["label"])}: {escape_txt(m["value"])}</span>' for m in metrics_list)

        # Score breakdown html
        comp_items = []
        comp_labels = {"scope": "征稿范围", "manuscript": "稿件完成度", "goal": "投稿目标", "budget": "预算匹配", "speed": "审稿周期"}
        for ck, cinfo in components.items():
            if isinstance(cinfo, dict):
                c_score = cinfo.get("score")
                c_score_fmt = f"{c_score:.1f}" if c_score is not None else "待补"
                comp_items.append(f'<span class="score-pill">{escape_txt(comp_labels.get(ck, ck))}: {c_score_fmt}</span>')
        comp_html = " ".join(comp_items)

        # Experiences list html
        if experiences:
            exp_html_list = []
            for exp in experiences:
                url_markup = f'<a href="{escape_txt(exp["safe_url"])}" target="_blank" rel="noopener noreferrer" class="link-external">查看公开评价 ↗</a>' if exp["safe_url"] else ""
                verif_badge = '<span class="badge badge-warning text-xs">评价待核验 (时间较旧或未知)</span>' if exp["needs_verification"] else ""
                exp_html_list.append(
                    f'<li class="exp-item">'
                    f'<div class="exp-summary">{escape_txt(exp["summary"])} {verif_badge}</div>'
                    f'<div class="exp-meta text-muted">领域: {escape_txt(exp["field"] or "未标明")} | 主题: {escape_txt(exp["topic"] or "未标明")} | '
                    f'<span class="sample-size-tag">{escape_txt(exp["sample_size_str"])}</span> | 年份: {escape_txt(exp["data_year"] or "未标明")} {url_markup}</div>'
                    f'</li>'
                )
            exp_block_html = f'<ul class="exp-list">{"".join(exp_html_list)}</ul>'
        else:
            exp_block_html = '<div class="text-muted empty-inline">暂无公开投稿评价</div>'

        # Missing items html
        if missing_items:
            missing_html = '<ul class="missing-list">' + "".join(f'<li>{escape_txt(m)}</li>' for m in missing_items) + '</ul>'
        else:
            missing_html = '<div class="text-success text-sm">证据链完备，无待核验缺口</div>'

        # Improvements html
        if improvements:
            improv_html = '<ul class="improv-list">' + "".join(f'<li>{escape_txt(imp)}</li>' for imp in improvements) + '</ul>'
        else:
            improv_html = '<div class="text-muted text-sm">暂无具体修改补强建议</div>'

        # Evidence sources and Editorial snippet
        editorial_evidence = card.get("editorial_evidence") or []
        evidence_snippets = []
        for ee in editorial_evidence:
            if not isinstance(ee, dict):
                continue
            scope_sum = ee.get("scope_summary") or ""
            recent_p = ee.get("recent_papers") or []
            snippets_text = f"征稿范围摘要: {scope_sum[:250]}..." if len(scope_sum) > 250 else f"征稿范围摘要: {scope_sum}"
            papers_text = "近期范文: " + "; ".join(recent_p[:2]) if recent_p else ""
            evidence_snippets.append(f'<div class="evidence-snippet">{escape_txt(snippets_text)}<br><small>{escape_txt(papers_text)}</small></div>')
        evidence_block = "".join(evidence_snippets) if evidence_snippets else '<div class="text-muted text-sm">暂无附加征稿文本片段</div>'

        # Metadata observations html
        if metadata_observations:
            obs_items = []
            for obs in metadata_observations:
                if not isinstance(obs, dict):
                    continue
                obs_system = escape_txt(obs.get("system") or obs.get("source") or "元数据")
                obs_val = escape_txt(obs.get("value") or obs.get("status") or "")
                obs_note = escape_txt(obs.get("note") or obs.get("raw") or "")
                obs_items.append(f'<li><strong>{obs_system}</strong>: {obs_val} {f"({obs_note})" if obs_note else ""}</li>')
            metadata_block = f'<ul class="obs-list">{"".join(obs_items)}</ul>' if obs_items else '<div class="text-muted text-sm">无详细元数据记录</div>'
        else:
            metadata_block = '<div class="text-muted text-sm">暂无独立收录元数据观察记录</div>'

        # Special issue note
        special_tip_html = f'<div class="special-tip">💡 <strong>专刊投稿提示：</strong>{escape_txt(special_tip)}</div>' if special_tip else ""

        # Provenance info
        prov_source = escape_txt(provenance.get("source_id") or "本地证据库")
        prov_url = clean_safe_url(provenance.get("source_url"))
        prov_url_markup = f'<a href="{escape_txt(prov_url)}" target="_blank" rel="noopener noreferrer" class="link-external">官方链接 ↗</a>' if prov_url else ""
        prov_year = escape_txt(provenance.get("data_year") or "")
        prov_text = f"官方源: {prov_source} {f'({prov_year}年)' if prov_year else ''} {prov_url_markup}"

        # Construct Card HTML
        card_html = f"""
        <div class="journal-card filterable-item"
             id="card-{safe_jid}"
             data-search-text="{escape_txt(search_terms)}"
             data-group="{escape_txt(group_key)}"
             data-indexing="{escape_txt(idx_info['category'])}"
             data-cost="{escape_txt(cost_info['type'])}"
             data-experience="{escape_txt(exp_type)}"
             data-risk="{escape_txt(risk_info['type'])}">
            <div class="card-header">
                <div class="card-title-row">
                    <span class="card-index">#{idx}</span>
                    <h3 class="card-title">{title}</h3>
                    {homepage_html}
                </div>
                <div class="card-badges">
                    {group_badge}
                    {eligibility_badge}
                    <span class="badge {risk_info['badge_class']}">{escape_txt(risk_info['label'])}</span>
                </div>
            </div>

            <div class="card-subrow">
                <span class="issn-tag">ISSN: {issns_str}</span>
                <span class="badge {idx_info['badge']}">{escape_txt(idx_info['label'])}</span>
                {oa_badge}
                <div class="rankings-inline">{rankings_html}</div>
                {f'<div class="metrics-inline">{metrics_html}</div>' if metrics_html else ''}
            </div>

            <div class="score-dashboard">
                <div class="score-main">
                    <div class="score-label">推荐分 <span class="score-disclaimer">(非录用率)</span></div>
                    <div class="score-num">{score_str} <span class="score-range">{escape_txt(range_str)}</span></div>
                </div>
                <div class="score-completeness">
                    <div class="comp-label">证据完整度: <strong>{completeness:.0f}%</strong></div>
                    <div class="progress-bar-bg">
                        <div class="progress-bar-fill" style="width: {completeness:.0f}%;"></div>
                    </div>
                </div>
                <div class="score-breakdown">
                    {comp_html}
                </div>
            </div>

            <div class="details-grid">
                <div class="detail-box">
                    <div class="detail-title">💰 费用与路线说明</div>
                    <div class="detail-content">
                        <div><strong>路线：</strong>{escape_txt(cost_info['route'])}</div>
                        <div class="cost-summary {cost_info['badge_class']}">{escape_txt(cost_info['summary'])}</div>
                    </div>
                </div>
                <div class="detail-box">
                    <div class="detail-title">⏱️ 审稿周期指标</div>
                    <div class="detail-content">
                        <div><strong>阶段：</strong>{escape_txt(timing_info['stage_zh'])}</div>
                        <div class="timing-summary">{escape_txt(timing_info['summary'])}</div>
                    </div>
                </div>
            </div>

            {special_tip_html}

            <div class="assessment-section">
                {f'<div class="rationale-block"><strong>🎯 适配理由：</strong>{escape_txt(rationale)}</div>' if rationale else ''}
                
                <div class="card-toggle-group">
                    <details class="report-details" id="details-missing-{safe_jid}">
                        <summary>⚠️ 待核验事项与缺口 ({len(missing_items)})</summary>
                        {missing_html}
                    </details>
                    
                    <details class="report-details" id="details-improv-{safe_jid}">
                        <summary>🛠️ 补强建议与修改建议 ({len(improvements)})</summary>
                        {improv_html}
                    </details>
                    
                    <details class="report-details" id="details-exp-{safe_jid}">
                        <summary>💬 真实投稿评价 (共 {total_exp_count} 条，展示 {len(experiences)} 条)</summary>
                        {exp_block_html}
                    </details>

                    <details class="report-details" id="details-evidence-{safe_jid}">
                        <summary>📑 征稿依据与范围摘录</summary>
                        <div class="evidence-prov-meta text-xs text-muted mb-2">{prov_text}</div>
                        {evidence_block}
                    </details>

                    <details class="report-details" id="details-metadata-{safe_jid}">
                        <summary>🔍 收录元数据观察与检索证据 ({len(metadata_observations)})</summary>
                        {metadata_block}
                    </details>
                </div>
            </div>
        </div>
        """
        rendered_cards_html.append(card_html)

        # Construct Table Row HTML
        table_row_html = f"""
        <tr class="filterable-item"
            id="row-{safe_jid}"
            data-search-text="{escape_txt(search_terms)}"
            data-group="{escape_txt(group_key)}"
            data-indexing="{escape_txt(idx_info['category'])}"
            data-cost="{escape_txt(cost_info['type'])}"
            data-experience="{escape_txt(exp_type)}"
            data-risk="{escape_txt(risk_info['type'])}">
            <td class="col-num">#{idx}</td>
            <td class="col-title">
                <strong>{title}</strong>
                <div class="text-muted text-sm">{issns_str}</div>
                <div class="text-sm">{homepage_html.replace('id="homepage-', 'id="row-homepage-')}</div>
            </td>
            <td class="col-group">{group_badge}</td>
            <td class="col-score">
                <strong>{score_str}</strong>
                <div class="text-muted text-xs">完整度 {completeness:.0f}%</div>
            </td>
            <td class="col-indexing">
                <span class="badge {idx_info['badge']}">{escape_txt(idx_info['label'])}</span>
                <div class="text-xs mt-1">{rankings_html}</div>
            </td>
            <td class="col-cost">
                <div class="text-sm font-semibold">{escape_txt(cost_info['amount_str'])}</div>
                <div class="text-muted text-xs">{escape_txt(cost_info['route'])}</div>
            </td>
            <td class="col-timing">
                <div class="text-sm">{escape_txt(timing_info['days_str'])}</div>
                <div class="text-muted text-xs">{escape_txt(timing_info['stage_zh'])}</div>
            </td>
            <td class="col-risk">
                <span class="badge {risk_info['badge_class']}">{escape_txt(risk_info['label'])}</span>
            </td>
            <td class="col-exp">
                <span class="badge {'badge-info' if exp_type == 'has' else 'badge-muted'}">
                    {'有评价 (' + str(total_exp_count) + ')' if exp_type == 'has' else '无评价'}
                </span>
            </td>
            <td class="col-status">{row_status_badge}</td>
        </tr>
        """
        rendered_table_rows_html.append(table_row_html)

    # Narrative guidance section
    rendered_guidance_html = ""
    if narrative_guidance or warnings:
        guidance_items = "".join(f'<li class="guidance-item">{escape_txt(g)}</li>' for g in narrative_guidance)
        warnings_items = "".join(f'<li class="warning-item">⚠️ {escape_txt(w)}</li>' for w in warnings)
        rendered_guidance_html = f"""
        <div class="narrative-card">
            <h3 class="narrative-title">🧭 学术常识排雷与叙事策略指导</h3>
            {f'<ul class="guidance-list">{guidance_items}</ul>' if guidance_items else ''}
            {f'<ul class="warnings-list">{warnings_items}</ul>' if warnings_items else ''}
        </div>
        """

    # Excluded journals section
    rendered_excluded_html = ""
    if excluded:
        excluded_rows = []
        for ex in excluded:
            ex_title = escape_txt(ex.get("title") or "未命名期刊")
            ex_reasons = "; ".join(escape_txt(r) for r in ex.get("reasons") or [])
            excluded_rows.append(f'<tr><td><strong>{ex_title}</strong></td><td class="text-danger">{ex_reasons}</td></tr>')
        rendered_excluded_html = f"""
        <details class="excluded-details" id="details-excluded-list">
            <summary class="excluded-summary">🚫 查看已排除/不合规期刊名单 ({len(excluded)} 本)</summary>
            <div class="excluded-body">
                <p class="text-muted text-sm">以下期刊因不满足硬性过滤条件、预算超限或风险拦截而被排除在候选集之外：</p>
                <table class="excluded-table">
                    <thead><tr><th>期刊标题</th><th>排除原因</th></tr></thead>
                    <tbody>{"".join(excluded_rows)}</tbody>
                </table>
            </div>
        </details>
        """

    # Shortfall notification block if shortfall > 0
    shortfall_block = ""
    if shortfall > 0 and target_minimum:
        shortfall_block = f"""
        <div class="stat-card card-shortfall" id="stat-shortfall">
            <div class="stat-title">目标候选缺口</div>
            <div class="stat-value text-warning" id="stat-count-shortfall">{shortfall}</div>
            <div class="stat-desc">目标最少 {target_minimum} 本，保留真实缺口，绝不盲目凑数</div>
        </div>
        """

    # Assemble Full HTML Document
    html_output = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{escape_txt(research_theme)} - 学术期刊推荐报告</title>
<style>
/* CSS Reset & System Typography */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    background-color: #f6f8fb;
    color: #1e293b;
    line-height: 1.6;
    padding: 24px;
}}
.container {{
    max-width: 1280px;
    margin: 0 auto;
}}

/* Header */
.report-header {{
    background: #ffffff;
    border-radius: 12px;
    padding: 28px 32px;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
    border: 1px solid #e2e8f0;
    margin-bottom: 20px;
}}
.header-badge-row {{
    display: flex;
    gap: 8px;
    align-items: center;
    margin-bottom: 12px;
    flex-wrap: wrap;
}}
.report-title {{
    font-size: 26px;
    font-weight: 700;
    color: #0f172a;
    line-height: 1.3;
    margin-bottom: 8px;
}}
.report-subtitle {{
    font-size: 14px;
    color: #64748b;
}}

/* Compliance Banner */
.compliance-banner {{
    background-color: #eff6ff;
    border-left: 4px solid #2563eb;
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 20px;
    font-size: 13.5px;
    color: #1e40af;
    line-height: 1.6;
}}
.compliance-banner strong {{
    color: #1d4ed8;
}}

/* Stats Dashboard */
.stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 20px;
}}
.stat-card {{
    background: #ffffff;
    border-radius: 10px;
    padding: 20px;
    border: 1px solid #e2e8f0;
    box-shadow: 0 1px 4px rgba(0,0,0,0.03);
    position: relative;
    overflow: hidden;
}}
.stat-card.card-rec {{ border-top: 4px solid #10b981; }}
.stat-card.card-prov {{ border-top: 4px solid #f59e0b; }}
.stat-card.card-unclass {{ border-top: 4px solid #6b7280; }}
.stat-card.card-excl {{ border-top: 4px solid #ef4444; }}
.stat-card.card-shortfall {{ border-top: 4px solid #f97316; }}
.stat-title {{
    font-size: 13px;
    color: #64748b;
    font-weight: 600;
    margin-bottom: 6px;
    text-transform: uppercase;
}}
.stat-value {{
    font-size: 32px;
    font-weight: 800;
    color: #0f172a;
    line-height: 1;
    margin-bottom: 4px;
}}
.stat-desc {{
    font-size: 12px;
    color: #94a3b8;
}}

/* Filter Toolbar */
.toolbar-card {{
    background: #ffffff;
    border-radius: 12px;
    padding: 20px 24px;
    border: 1px solid #e2e8f0;
    box-shadow: 0 2px 8px rgba(0,0,0,0.03);
    margin-bottom: 24px;
}}
.toolbar-top {{
    display: flex;
    gap: 16px;
    align-items: center;
    flex-wrap: wrap;
    margin-bottom: 16px;
}}
.search-wrapper {{
    flex: 1;
    min-width: 260px;
    position: relative;
}}
.search-input {{
    width: 100%;
    padding: 10px 14px;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
}}
.search-input:focus {{
    border-color: #2563eb;
    box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.1);
}}
.filters-row {{
    display: flex;
    gap: 12px;
    flex-wrap: wrap;
    align-items: center;
}}
.filter-group {{
    display: flex;
    flex-direction: column;
    gap: 4px;
}}
.filter-label {{
    font-size: 11px;
    font-weight: 700;
    color: #475569;
    text-transform: uppercase;
}}
.filter-select {{
    padding: 8px 12px;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    font-size: 13px;
    background-color: #ffffff;
    color: #334155;
    outline: none;
    cursor: pointer;
}}
.filter-select:focus {{
    border-color: #2563eb;
}}
.btn-reset {{
    padding: 8px 16px;
    background: #f1f5f9;
    color: #334155;
    border: 1px solid #cbd5e1;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.2s;
    align-self: flex-end;
}}
.btn-reset:hover {{
    background: #e2e8f0;
}}
.toolbar-footer {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-top: 1px solid #f1f5f9;
    padding-top: 12px;
    font-size: 13px;
    color: #64748b;
}}
.filter-note {{
    font-size: 12px;
    color: #94a3b8;
}}

/* View Mode Switcher */
.view-tabs {{
    display: flex;
    gap: 8px;
    margin-bottom: 20px;
}}
.view-tab-btn {{
    padding: 8px 16px;
    border: 1px solid #cbd5e1;
    background: #ffffff;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    color: #475569;
    cursor: pointer;
}}
.view-tab-btn.active {{
    background: #2563eb;
    color: #ffffff;
    border-color: #2563eb;
}}

/* Cards Layout */
.cards-container {{
    display: flex;
    flex-direction: column;
    gap: 20px;
    margin-bottom: 30px;
}}
.journal-card {{
    background: #ffffff;
    border-radius: 12px;
    border: 1px solid #e2e8f0;
    padding: 24px;
    box-shadow: 0 2px 6px rgba(0,0,0,0.03);
    transition: box-shadow 0.2s, border-color 0.2s;
}}
.journal-card:hover {{
    box-shadow: 0 4px 14px rgba(0,0,0,0.06);
    border-color: #cbd5e1;
}}
.card-header {{
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 16px;
    margin-bottom: 12px;
    flex-wrap: wrap;
}}
.card-title-row {{
    display: flex;
    align-items: center;
    gap: 10px;
    flex: 1;
}}
.card-index {{
    background: #f1f5f9;
    color: #475569;
    font-weight: 700;
    font-size: 12px;
    padding: 2px 8px;
    border-radius: 4px;
}}
.card-title {{
    font-size: 19px;
    font-weight: 700;
    color: #0f172a;
}}
.homepage-link {{
    display: inline-block;
    font-size: 12px;
    font-weight: 600;
    color: #1d4ed8;
    border: 1px solid #bfdbfe;
    background: #eff6ff;
    padding: 2px 8px;
    border-radius: 999px;
    text-decoration: none;
    white-space: nowrap;
}}
.homepage-link:hover {{ background: #dbeafe; }}
.homepage-missing {{
    font-size: 12px;
    color: #94a3b8;
    white-space: nowrap;
}}
.card-badges {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}}
.card-subrow {{
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
    font-size: 13px;
}}
.issn-tag {{
    color: #64748b;
    font-family: ui-monospace, SFMono-Regular, monospace;
    font-size: 12px;
}}
.rankings-inline, .metrics-inline {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}}

/* Badges & Tags */
.badge {{
    display: inline-block;
    padding: 3px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
    line-height: 1.3;
}}
.badge-success {{ background: #ecfdf5; color: #065f46; border: 1px solid #a7f3d0; }}
.badge-warning {{ background: #fffbeb; color: #92400e; border: 1px solid #fde68a; }}
.badge-danger {{ background: #fef2f2; color: #991b1b; border: 1px solid #fecaca; }}
.badge-info {{ background: #eff6ff; color: #1e40af; border: 1px solid #bfdbfe; }}
.badge-primary {{ background: #f0fdf4; color: #15803d; border: 1px solid #bbf7d0; }}
.badge-secondary {{ background: #f8fafc; color: #334155; border: 1px solid #cbd5e1; }}
.badge-muted {{ background: #f1f5f9; color: #64748b; border: 1px solid #e2e8f0; }}

.badge-efficiency {{ background: #ecfeff; color: #0e7490; border: 1px solid #a5f3fc; }}
.badge-balanced {{ background: #f0fdf4; color: #166534; border: 1px solid #bbf7d0; }}
.badge-stretch {{ background: #faf5ff; color: #6b21a8; border: 1px solid #e9d5ff; }}

.tag {{
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 600;
}}
.tag-oa {{ background: #e0e7ff; color: #3730a3; }}
.tag-rank {{ background: #fef3c7; color: #92400e; }}
.tag-metric {{ background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0; }}

/* Score Dashboard Inside Card */
.score-dashboard {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 16px;
    display: flex;
    align-items: center;
    gap: 24px;
    margin-bottom: 16px;
    flex-wrap: wrap;
}}
.score-main {{
    min-width: 140px;
}}
.score-label {{
    font-size: 12px;
    color: #64748b;
    font-weight: 600;
}}
.score-disclaimer {{
    font-size: 11px;
    color: #ef4444;
    font-weight: normal;
}}
.score-num {{
    font-size: 28px;
    font-weight: 800;
    color: #0f172a;
    line-height: 1.1;
}}
.score-range {{
    font-size: 13px;
    font-weight: normal;
    color: #64748b;
}}
.score-completeness {{
    flex: 1;
    min-width: 180px;
}}
.comp-label {{
    font-size: 12px;
    color: #475569;
    margin-bottom: 4px;
}}
.progress-bar-bg {{
    height: 8px;
    background: #e2e8f0;
    border-radius: 4px;
    overflow: hidden;
}}
.progress-bar-fill {{
    height: 100%;
    background: #2563eb;
    border-radius: 4px;
}}
.score-breakdown {{
    display: flex;
    gap: 6px;
    flex-wrap: wrap;
}}
.score-pill {{
    background: #ffffff;
    border: 1px solid #cbd5e1;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    color: #475569;
}}

/* Details Grid */
.details-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-bottom: 14px;
}}
@media (max-width: 768px) {{
    .details-grid {{ grid-template-columns: 1fr; }}
}}
.detail-box {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 12px 14px;
    font-size: 13px;
}}
.detail-title {{
    font-weight: 700;
    color: #334155;
    margin-bottom: 6px;
    font-size: 12.5px;
}}
.cost-summary, .timing-summary {{
    margin-top: 4px;
    font-size: 12px;
    padding: 4px 6px;
    border-radius: 4px;
}}

/* Special Issue Tip */
.special-tip {{
    background: #fffbeb;
    border: 1px solid #fef3c7;
    border-left: 3px solid #f59e0b;
    border-radius: 6px;
    padding: 10px 14px;
    font-size: 12.5px;
    color: #78350f;
    margin-bottom: 14px;
}}

/* Assessment Details & Toggles */
.assessment-section {{
    margin-top: 10px;
}}
.rationale-block {{
    font-size: 13px;
    color: #334155;
    background: #f8fafc;
    border-left: 3px solid #64748b;
    padding: 8px 12px;
    border-radius: 4px;
    margin-bottom: 12px;
}}
.card-toggle-group {{
    display: flex;
    flex-direction: column;
    gap: 8px;
}}
.report-details {{
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 13px;
}}
.report-details summary {{
    cursor: pointer;
    font-weight: 600;
    color: #334155;
    outline: none;
}}
.missing-list, .improv-list, .obs-list {{
    margin: 8px 0 4px 20px;
    color: #475569;
    font-size: 12.5px;
}}
.missing-list li {{ color: #b45309; }}
.improv-list li {{ color: #2563eb; }}

.exp-list {{
    list-style: none;
    margin-top: 8px;
    display: flex;
    flex-direction: column;
    gap: 8px;
}}
.exp-item {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    border-radius: 6px;
    padding: 8px 12px;
}}
.exp-summary {{
    font-size: 13px;
    color: #1e293b;
    margin-bottom: 4px;
}}
.exp-meta {{
    font-size: 11px;
    color: #64748b;
}}
.sample-size-tag {{
    color: #0369a1;
    font-weight: 600;
}}
.link-external {{
    color: #2563eb;
    text-decoration: none;
    font-weight: 600;
    margin-left: 6px;
}}
.link-external:hover {{ text-decoration: underline; }}
.evidence-snippet {{
    background: #ffffff;
    border: 1px solid #e2e8f0;
    padding: 8px;
    border-radius: 4px;
    margin-top: 6px;
    font-size: 12px;
    color: #475569;
}}

/* Comparison Table View */
.table-wrapper {{
    background: #ffffff;
    border-radius: 12px;
    border: 1px solid #e2e8f0;
    box-shadow: 0 2px 6px rgba(0,0,0,0.03);
    overflow-x: auto;
    margin-bottom: 30px;
}}
.comparison-table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 13px;
    text-align: left;
    min-width: 900px;
}}
.comparison-table th {{
    background: #f8fafc;
    color: #475569;
    font-weight: 700;
    padding: 12px 14px;
    border-bottom: 2px solid #e2e8f0;
    white-space: nowrap;
}}
.comparison-table td {{
    padding: 12px 14px;
    border-bottom: 1px solid #f1f5f9;
    vertical-align: middle;
}}
.comparison-table tr:hover td {{
    background-color: #f8fafc;
}}

/* Guidance & Narrative Card */
.narrative-card {{
    background: #ffffff;
    border-radius: 10px;
    border: 1px solid #e2e8f0;
    padding: 20px 24px;
    margin-bottom: 24px;
}}
.narrative-title {{
    font-size: 16px;
    font-weight: 700;
    color: #0f172a;
    margin-bottom: 12px;
}}
.guidance-list, .warnings-list {{
    margin-left: 20px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: 13.5px;
}}
.guidance-item {{ color: #1e3a8a; }}
.warning-item {{ color: #991b1b; list-style: none; margin-left: -20px; }}

/* Excluded Details */
.excluded-details {{
    background: #ffffff;
    border-radius: 10px;
    border: 1px solid #e2e8f0;
    padding: 16px 20px;
    margin-bottom: 30px;
}}
.excluded-summary {{
    font-weight: 700;
    color: #64748b;
    cursor: pointer;
}}
.excluded-body {{
    margin-top: 14px;
}}
.excluded-table {{
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
    font-size: 12.5px;
}}
.excluded-table th, .excluded-table td {{
    padding: 8px 12px;
    border: 1px solid #e2e8f0;
}}
.excluded-table th {{ background: #f8fafc; }}

/* Empty States & Utility Classes */
.is-hidden {{ display: none !important; }}
.empty-state {{
    background: #ffffff;
    border-radius: 10px;
    border: 2px dashed #cbd5e1;
    padding: 40px;
    text-align: center;
    color: #64748b;
    margin-bottom: 30px;
}}
.empty-state h4 {{ font-size: 16px; color: #334155; margin-bottom: 6px; }}
.text-muted {{ color: #64748b; }}
.text-success {{ color: #16a34a; }}
.text-warning {{ color: #d97706; }}
.text-danger {{ color: #dc2626; }}
.text-sm {{ font-size: 12.5px; }}
.text-xs {{ font-size: 11px; }}
.mt-1 {{ margin-top: 4px; }}
.mb-2 {{ margin-bottom: 8px; }}
.font-semibold {{ font-weight: 600; }}

/* Print Rules */
@media print {{
    body {{ background: #fff; padding: 0; }}
    .toolbar-card, .btn-reset, .view-tabs {{ display: none !important; }}
    .journal-card {{ break-inside: avoid; border: 1px solid #ccc; box-shadow: none; margin-bottom: 20px; }}
    .report-details[open] {{ break-inside: avoid; }}
}}
</style>
</head>
<body>
<div class="container">

    <!-- Report Header -->
    <header class="report-header">
        <div class="header-badge-row">
            <span class="badge badge-info" id="badge-mode">模式：{escape_txt(data.get("mode", "manuscript"))}</span>
            <span class="badge badge-secondary" id="badge-rubric">评估基准：{escape_txt(data.get("rubric_version", "journal-fit-v1"))}</span>
            <span class="badge badge-muted" id="badge-total">候选总数：<strong id="header-total-count">{count_total}</strong></span>
        </div>
        <h1 class="report-title" id="report-title">{escape_txt(research_theme)}</h1>
        <p class="report-subtitle" id="report-subtitle">独立单文件学术期刊推荐报告 · 评估时间：<span id="evaluated-at-timestamp">{escape_txt(time_str)}</span></p>
    </header>

    <!-- Strict Academic Compliance Banner -->
    <div class="compliance-banner" id="compliance-banner">
        <strong>⚠️ 学术合规与评级边界特别声明：</strong><br>
        1. <strong>非录用率保证：</strong>推荐分仅代表所提供稿件证据与期刊公开征稿范围的匹配保守下界，绝非期刊录用概率，亦非期刊学术声誉评级。<br>
        2. <strong>策略标签边界：</strong>“稳妥／效率档”为用户投稿策略分类，并非期刊官方质量认证或“水刊”背书。<br>
        3. <strong>费用边界：</strong>“含零 APC 路线”仅表示该刊存在免收论文出版费的发表路线，绝不代表审稿、加急、彩印、超页等总费用全免。<br>
        4. <strong>审稿周期：</strong>周期指标基于期刊历史统计阶段（如初审可能含编辑初筛），绝不能解读为外审通过或录用周期。
    </div>

    <!-- Stats Dashboard -->
    <div class="stats-grid" id="stats-dashboard">
        <div class="stat-card card-rec" id="stat-recommended">
            <div class="stat-title">有证据推荐</div>
            <div class="stat-value" id="stat-count-recommended">{count_recommended}</div>
            <div class="stat-desc">硬性要求满足且证据链完备</div>
        </div>
        <div class="stat-card card-prov" id="stat-provisional">
            <div class="stat-title">待核验候选</div>
            <div class="stat-value" id="stat-count-provisional">{count_provisional}</div>
            <div class="stat-desc">存在缺失项、冲突或待人工核实</div>
        </div>
        <div class="stat-card card-unclass" id="stat-unclassified">
            <div class="stat-title">未分类候选</div>
            <div class="stat-value" id="stat-count-unclassified">{count_unclassified}</div>
            <div class="stat-desc">匹配主题但定位依据不足</div>
        </div>
        <div class="stat-card card-excl" id="stat-excluded">
            <div class="stat-title">已排除期刊</div>
            <div class="stat-value" id="stat-count-excluded">{count_excluded}</div>
            <div class="stat-desc">硬性规则拦截或风险不符合</div>
        </div>
        {shortfall_block}
    </div>

    <!-- Narrative Guidance & Common-Sense Red Lines -->
    {rendered_guidance_html}

    <!-- Interactive Filter Toolbar -->
    <div class="toolbar-card" id="toolbar-card">
        <div class="toolbar-top">
            <div class="search-wrapper">
                <input type="text" id="search-input" class="search-input" placeholder="🔍 全文搜索期刊名称、ISSN、研究方向、出版商..." autocomplete="off">
            </div>
            <button type="button" id="btn-reset" class="btn-reset">↺ 重置所有筛选</button>
        </div>
        <div class="filters-row">
            <div class="filter-group">
                <label class="filter-label" for="filter-indexing">收录检索</label>
                <select id="filter-indexing" class="filter-select">
                    <option value="all">全部收录类型</option>
                    <option value="sci">SCI / SCIE</option>
                    <option value="ei">EI 检索</option>
                    <option value="dual">SCI + EI 双检索</option>
                    <option value="unknown">其他 / 待核验</option>
                </select>
            </div>
            <div class="filter-group">
                <label class="filter-label" for="filter-group">推荐策略档位</label>
                <select id="filter-group" class="filter-select">
                    <option value="all">全部档位</option>
                    <option value="efficiency">稳妥／效率档 (用户策略)</option>
                    <option value="balanced">均衡档</option>
                    <option value="stretch">冲刺／顶刊档</option>
                    <option value="unclassified">未分类候选</option>
                </select>
            </div>
            <div class="filter-group">
                <label class="filter-label" for="filter-cost">费用路线类型</label>
                <select id="filter-cost" class="filter-select">
                    <option value="all">全部费用状态</option>
                    <option value="zero">含零 APC 路线 (非总费用全免)</option>
                    <option value="paid">含付费版面费路线</option>
                    <option value="unknown">费用未知 / 待核验</option>
                </select>
            </div>
            <div class="filter-group">
                <label class="filter-label" for="filter-experience">公开投稿评价</label>
                <select id="filter-experience" class="filter-select">
                    <option value="all">全部评价状态</option>
                    <option value="has">有公开投稿评价</option>
                    <option value="none">暂无公开评价</option>
                </select>
            </div>
            <div class="filter-group">
                <label class="filter-label" for="filter-risk">风险雷达状态</label>
                <select id="filter-risk" class="filter-select">
                    <option value="all">全部风险状态</option>
                    <option value="ok">已核验无预警</option>
                    <option value="warning">待核验 / 冲突 / 过期</option>
                    <option value="flagged">存在预警 / 规则拦截</option>
                </select>
            </div>
        </div>
        <div class="toolbar-footer">
            <span class="filter-status">当前显示 <strong id="match-count">{count_total}</strong> / 共 <span id="total-count">{count_total}</span> 本候选期刊</span>
            <span class="filter-note">💡 卡片区与横向对比表严格同步联动筛选</span>
        </div>
    </div>

    <!-- View Mode Switcher -->
    <div class="view-tabs" id="view-tabs">
        <button type="button" class="view-tab-btn active" id="tab-cards" onclick="switchView('cards')">📑 分档卡片视图</button>
        <button type="button" class="view-tab-btn" id="tab-table" onclick="switchView('table')">📊 横向对比表格</button>
    </div>

    <!-- Empty State Notification -->
    <div id="empty-search-state" class="empty-state is-hidden">
        <h4>未找到符合当前筛选条件的期刊</h4>
        <p>请尝试清空关键词、放宽检索收录要求或点击上方“重置所有筛选”按钮。</p>
    </div>

    <!-- Cards View -->
    <main id="cards-view" class="cards-container">
        {"".join(rendered_cards_html) if rendered_cards_html else '<div class="empty-state" id="empty-cards-state"><h4>暂无推荐期刊</h4><p>当前候选池为空或全部被硬性规则拦截。</p></div>'}
    </main>

    <!-- Table View -->
    <div id="table-view" class="table-wrapper is-hidden">
        <table class="comparison-table" id="comparison-table">
            <thead>
                <tr>
                    <th>#</th>
                    <th>期刊名称 & ISSN</th>
                    <th>推荐档位</th>
                    <th>推荐分 (非录用率)</th>
                    <th>收录 & 分区</th>
                    <th>出版路线 & 费用</th>
                    <th>审稿周期 (阶段)</th>
                    <th>风险雷达</th>
                    <th>投稿评价</th>
                    <th>资格状态</th>
                </tr>
            </thead>
            <tbody>
                {"".join(rendered_table_rows_html) if rendered_table_rows_html else '<tr><td colspan="10" class="text-center text-muted">暂无候选记录</td></tr>'}
            </tbody>
        </table>
    </div>

    <!-- Excluded Section -->
    {rendered_excluded_html}

</div>

<script>
// Zero-external-dependency, innerHTML-free interactive filtering
(function() {{
    const searchInput = document.getElementById('search-input');
    const filterIndexing = document.getElementById('filter-indexing');
    const filterGroup = document.getElementById('filter-group');
    const filterCost = document.getElementById('filter-cost');
    const filterExp = document.getElementById('filter-experience');
    const filterRisk = document.getElementById('filter-risk');
    const btnReset = document.getElementById('btn-reset');
    const matchCountEl = document.getElementById('match-count');
    const emptyStateEl = document.getElementById('empty-search-state');

    function applyFilters() {{
        const query = (searchInput.value || '').trim().toLowerCase();
        const indexingVal = filterIndexing.value;
        const groupVal = filterGroup.value;
        const costVal = filterCost.value;
        const expVal = filterExp.value;
        const riskVal = filterRisk.value;

        const items = document.querySelectorAll('.filterable-item');
        let visibleCards = 0;

        items.forEach(function(el) {{
            const searchTxt = (el.getAttribute('data-search-text') || '').toLowerCase();
            const elGroup = el.getAttribute('data-group') || '';
            const elIndexing = el.getAttribute('data-indexing') || '';
            const elCost = el.getAttribute('data-cost') || '';
            const elExp = el.getAttribute('data-experience') || '';
            const elRisk = el.getAttribute('data-risk') || '';

            let matched = true;

            // Search query matching
            if (query && searchTxt.indexOf(query) === -1) {{
                matched = false;
            }}

            // Indexing matching
            if (matched && indexingVal !== 'all') {{
                if (indexingVal === 'sci' && elIndexing !== 'sci' && elIndexing !== 'dual') matched = false;
                else if (indexingVal === 'ei' && elIndexing !== 'ei' && elIndexing !== 'dual') matched = false;
                else if (indexingVal === 'dual' && elIndexing !== 'dual') matched = false;
                else if (indexingVal === 'unknown' && elIndexing !== 'unknown') matched = false;
            }}

            // Group matching
            if (matched && groupVal !== 'all') {{
                if (elGroup !== groupVal) matched = false;
            }}

            // Cost matching across all routes (support multi-token like "zero paid")
            if (matched && costVal !== 'all') {{
                const costTokens = elCost.split(/\\s+/);
                if (costTokens.indexOf(costVal) === -1) {{
                    matched = false;
                }}
            }}

            // Experience matching
            if (matched && expVal !== 'all') {{
                if (elExp !== expVal) matched = false;
            }}

            // Risk matching
            if (matched && riskVal !== 'all') {{
                if (elRisk !== riskVal) matched = false;
            }}

            if (matched) {{
                el.classList.remove('is-hidden');
                if (el.classList.contains('journal-card')) {{
                    visibleCards++;
                }}
            }} else {{
                el.classList.add('is-hidden');
            }}
        }});

        if (matchCountEl) {{
            matchCountEl.textContent = String(visibleCards);
        }}

        if (emptyStateEl) {{
            if (visibleCards === 0) {{
                emptyStateEl.classList.remove('is-hidden');
            }} else {{
                emptyStateEl.classList.add('is-hidden');
            }}
        }}
    }}

    function resetFilters() {{
        searchInput.value = '';
        filterIndexing.value = 'all';
        filterGroup.value = 'all';
        filterCost.value = 'all';
        filterExp.value = 'all';
        filterRisk.value = 'all';
        applyFilters();
    }}

    if (searchInput) searchInput.addEventListener('input', applyFilters);
    if (filterIndexing) filterIndexing.addEventListener('change', applyFilters);
    if (filterGroup) filterGroup.addEventListener('change', applyFilters);
    if (filterCost) filterCost.addEventListener('change', applyFilters);
    if (filterExp) filterExp.addEventListener('change', applyFilters);
    if (filterRisk) filterRisk.addEventListener('change', applyFilters);
    if (btnReset) btnReset.addEventListener('click', resetFilters);

    window.switchView = function(view) {{
        const cardsView = document.getElementById('cards-view');
        const tableView = document.getElementById('table-view');
        const tabCards = document.getElementById('tab-cards');
        const tabTable = document.getElementById('tab-table');

        if (view === 'cards') {{
            if (cardsView) cardsView.classList.remove('is-hidden');
            if (tableView) tableView.classList.add('is-hidden');
            if (tabCards) tabCards.classList.add('active');
            if (tabTable) tabTable.classList.remove('active');
        }} else {{
            if (cardsView) cardsView.classList.add('is-hidden');
            if (tableView) tableView.classList.remove('is-hidden');
            if (tabCards) tabCards.classList.remove('active');
            if (tabTable) tabTable.classList.add('active');
        }}
    }};
}})();
</script>
</body>
</html>
"""
    return html_output


def write_html_report(result: dict, output_path: str, overwrite: bool = False) -> str:
    """Write rendered HTML report to specified file path. Returns absolute file path."""
    if not output_path or not isinstance(output_path, str):
        raise JournalError("INVALID_PATH", "输出路径必须为非空字符串")

    clean_path = output_path.strip()
    lower_path = clean_path.lower()
    if not (lower_path.endswith(".html") or lower_path.endswith(".htm")):
        raise JournalError("INVALID_FILE_TYPE", "输出路径扩展名必须为 .html 或 .htm")

    abs_path = os.path.abspath(clean_path)

    if os.path.exists(abs_path) and not overwrite:
        raise JournalError("FILE_EXISTS", f"目标报告文件已存在且未开启覆盖 (overwrite=False): {abs_path}")

    html_content = render_html_report(result)

    try:
        parent_dir = os.path.dirname(abs_path)
        if parent_dir:
            os.makedirs(parent_dir, exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(html_content)
    except (IOError, OSError, PermissionError) as e:
        raise JournalError("IO_ERROR", f"写入报告文件失败: {e}") from e

    return abs_path
