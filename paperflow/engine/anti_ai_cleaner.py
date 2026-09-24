"""Academic Anti-AI Detection and De-flavoring Engine.

Detects high-frequency AI-generated markers, rigid sentence patterns,
and robotic buzzwords in academic papers (both Chinese & English),
and provides actionable academic rewrites based on human scholar writing habits:
- Dynamic active/passive voice rebalancing
- Eliminating formulaic transitions (moreover, furthermore, delve into, testament to)
- Breaking mechanical paragraph length symmetry
- Injecting realistic scholar assertions & nuanced qualifications
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

# High-frequency AI cliches in Chinese academic writing
ZH_AI_PATTERNS = [
    (r"不可否认的是", "删除此过渡词，直接陈述事实或核心论据"),
    (r"深入探讨|深入剖析|深入研究", "替换为具体动作：'定量评估'、'系统分析'、'实证检验'、'形式化推导'"),
    (r"总而言之|综上所述|总的来看", "段落内部避免总结套话，段尾直接给出收敛结论"),
    (r"为.*?注入了强劲动力|赋予了全新内涵", "去除修辞抒情，使用中立学术动词：'显著降低了...'、'提升了...达X%'"),
    (r"宛如|犹如|犹如春风化雨", "学术论文严禁使用文学抒情比喻"),
    (r"值得注意的是", "直接给出观察结果，如：'实验表明...' 或 '数据反映出...'"),
    (r"在当今.*?的背景下|随着.*?的飞速发展", "开门见山陈述研究对象与痛点，不要以空泛时代背景开头"),
    (r"本研究旨在", "主动语态改写为：'本文提出...'、'本文设计并实现了...'"),
    (r"不仅.*?而且.*?更重要的是", "打破连续三重递进句式，拆分为因果逻辑句"),
]

# High-frequency AI cliches in English academic writing
EN_AI_PATTERNS = [
    (r"\bdelve into\b", "replace with 'investigate', 'analyze', 'examine', or 'evaluate'"),
    (r"\ba testament to\b", "replace with 'demonstrates that', 'indicates', or 'provides evidence for'"),
    (r"\bit is worth noting that\b", "delete filler; state the observation directly: 'Notably,' or 'We observe that'"),
    (r"\bvital role|crucial role|pivotal role\b", "replace with concrete function: 'serves as the primary mechanism for'"),
    (r"\btapestry\b", "strictly avoid literary metaphors; state the system architecture directly"),
    (r"\bintertwined\b", "replace with 'correlated', 'coupled', or 'interdependent'"),
    (r"\bIn conclusion,\s*it can be seen that\b", "summarize with quantitative findings instead of formulaic recap"),
    (r"\bfostering\b", "replace with 'enabling', 'facilitating', or 'accelerating'"),
    (r"\bseamlessly integrate[sd]?\b", "replace with 'integrate with low overhead' or state exact protocol"),
]


@dataclass
class AIFinding:
    """An identified AI cliché or stylistic defect."""
    matched_text: str
    suggestion: str
    rule_type: str


class AntiAICleaner:
    """Academic anti-AI detector and text de-flavoring assistant."""

    def __init__(self) -> None:
        self.zh_rules = [(re.compile(p, re.IGNORECASE), s) for p, s in ZH_AI_PATTERNS]
        self.en_rules = [(re.compile(p, re.IGNORECASE), s) for p, s in EN_AI_PATTERNS]

    def analyze(self, text: str) -> Dict[str, Any]:
        """Scan text and identify AI-generated patterns and stylistic risks."""
        findings: List[Dict[str, str]] = []
        is_english = len(re.findall(r"[A-Za-z]", text)) > len(re.findall(r"[\u4e00-\u9fa5]", text))

        rules = self.en_rules if is_english else self.zh_rules

        for regex, suggestion in rules:
            for match in regex.finditer(text):
                findings.append({
                    "matched": match.group(0),
                    "suggestion": suggestion,
                    "position": f"char {match.start()}-{match.end()}",
                })

        # Check consecutive transition adverbs (e.g. Furthermore, Moreover)
        consecutive_moreover = len(re.findall(r"\b(Moreover|Furthermore|Additionally)\b", text, re.IGNORECASE))
        if consecutive_moreover >= 3:
            findings.append({
                "matched": f"{consecutive_moreover} consecutive transition markers",
                "suggestion": "Excessive repetitive transition words detected. Diversify sentence flow or use causal clauses.",
                "position": "document-wide",
            })

        # Calculate a rough "AI Flavor Index" (0-100)
        word_count = len(text.split()) if is_english else len(text)
        risk_score = min(100, int((len(findings) * 25) / max(1, word_count / 150)))

        return {
            "is_english": is_english,
            "total_findings": len(findings),
            "ai_risk_score": risk_score,
            "level": "高风险 (明显的AI生成感)" if risk_score > 60 else ("中度风险" if risk_score > 25 else "低风险 (符合人类学者习惯)"),
            "findings": findings,
            "guidelines": [
                "1. 偏好主动语态 ('本文证明了...' 优于 '被证明了...')",
                "2. 打破句式对称性：长短句交替，避免整齐划一的并列排比句",
                "3. 剔除文学化抒情和空洞套话，代之以具体实验指标与归因分析",
                "4. 保持学术断言的适度自信与严谨限定词 (e.g., 'under high-load scenarios')",
            ],
        }


anti_ai_engine = AntiAICleaner()
