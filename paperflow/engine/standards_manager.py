"""Academic Standards Registry & Preset Manager (2021-2026 Standards).

Built-in coverage of Chinese National Standards (GB), University Thesis Conventions,
and Major International Academic Association Guidelines released or actively enforced
between 2021 and 2026.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


ACADEMIC_STANDARDS: Dict[str, Dict[str, Any]] = {
    # -------------------------------------------------------------------------
    # 1. 参考文献国家标准
    # -------------------------------------------------------------------------
    "gbt_7714_2025": {
        "id": "gbt_7714_2025",
        "name": "GB/T 7714-2025《信息与文献 参考文献著录规则》",
        "category": "reference_gb",
        "status": "最新国家标准（2025发布，2026全面实施，替代2015版）",
        "year": "2025-2026",
        "official_url": "https://std.samr.gov.cn/gb/search/gbDetailed?id=4507EFE13D37CB6AE06397BE0A0A601F",
        "description": "国家标准化管理委员会最新发布的新一代著录国标。新增预印本等文献标识，支持全角标点，非联机文献取消访问日期，调整网络文献标点规则。",
        "citation_style": "numeric_brackets",  # [1]
        "csl_recommendation": "GB-T-7714-2025（顺序编码，双语）/（全角符号）",
        "key_features": [
            "新增预印本文献类型标识",
            "中文文献允许使用全角标点符号",
            "非联机印刷版文献不再要求著录访问日期[引用日期]",
            "作者超过3人时保留前3人后加'等'或'et al.'",
            "DOI、URL 著录格式统一与现代化规范",
        ],
    },
    "gbt_7714_2015": {
        "id": "gbt_7714_2015",
        "name": "GB/T 7714-2015《信息与文献 参考文献著录规则》",
        "category": "reference_gb",
        "status": "现行广泛沿用国标（多数高校毕业论文与学术期刊现行要求）",
        "year": "2015-2025",
        "description": "国内绝大多数高校和期刊过去十年及当前仍普遍要求的基准规范。正文采用顺序编码制 [1] 或著者-出版年制，半角标点。",
        "citation_style": "numeric_brackets",
        "csl_recommendation": "GB-T-7714-2015（顺序编码，双语）",
        "key_features": [
            "经典 14 类文献类型标识符（[M], [J], [C], [D], [R], [P], [S], [EB/OL] 等）",
            "半角英文标点加空格",
            "联机文献强制标注[引用日期]与获取路径",
        ],
    },

    # -------------------------------------------------------------------------
    # 2. 论文结构与编写国家标准
    # -------------------------------------------------------------------------
    "gbt_7713_2_2022": {
        "id": "gbt_7713_2_2022",
        "name": "GB/T 7713.2-2022《学术论文编写规则》",
        "category": "structure_gb",
        "status": "现行国家标准（2022年12月发布，2023年7月正式实施，替代GB 7713-87）",
        "year": "2022-2026",
        "description": "全面取代 1987 年老国标，确立了新时期我国学术论文的核心结构与规范要求，新增利益冲突声明、数据可用性声明、作者贡献声明等现代理念。",
        "required_sections": [
            "题名 (Title)",
            "作者署名与工作单位 (Authors & Affiliations)",
            "摘要与关键词 (Abstract & Keywords)",
            "引言 (Introduction)",
            "正文 (Body/Methodology/Results)",
            "结论或结语 (Conclusion)",
            "致谢 (Acknowledgements)",
            "利益冲突声明 (Conflict of Interest)",
            "数据可用性声明 (Data Availability Statement)",
            "参考文献 (References)",
            "附录 (Appendix, 如有)",
        ],
        "key_features": [
            "图表必须在正文中'先见文字，后见图表'（图随文走）",
            "明确三线表（顶底粗、栏目细、无坚线）规范要求",
            "标点符合 GB/T 15834，数字符合 GB/T 15835",
            "法定计量单位强制符合 GB 3100 ~ 3102",
        ],
    },
    "gbt_7713_1_2006": {
        "id": "gbt_7713_1_2006",
        "name": "GB/T 7713.1-2006《学位论文编写规则》",
        "category": "structure_gb",
        "status": "现行学位论文国家标准（推荐性）",
        "year": "2006-2026",
        "description": "中国学士、硕士、博士学位论文框架基石，明确前置部分、主体部分、参考文献、附录与结尾部分的顺序与编排准则。",
        "required_sections": [
            "前置部分：封面、扉页、原创性与版权声明、中文摘要、英文摘要、目录、符号说明",
            "主体部分：引言/绪论、正文章节、结论",
            "参考文献",
            "附录",
            "结尾部分：致谢、个人简历与在学期间发表学术论文/成果",
        ],
    },

    # -------------------------------------------------------------------------
    # 3. 国内学位论文与期刊排版范式标准 (2021-2026 常见高校与出版惯例)
    # -------------------------------------------------------------------------
    "chinese_thesis_standard": {
        "id": "chinese_thesis_standard",
        "name": "国内高校研究生/本科博硕学位论文通用排版惯例 (2021-2026 范式)",
        "category": "layout_preset",
        "status": "主流高校盲审与毕业设计最通用排版规范",
        "year": "2021-2026",
        "page_size": "A4 (21.0cm x 29.7cm)",
        "margins": {"top_cm": 3.0, "bottom_cm": 2.5, "left_cm": 3.0, "right_cm": 2.5},
        "typography": {
            "body_font_zh": "宋体",
            "body_font_en": "Times New Roman",
            "body_size_pt": 12.0,  # 小四
            "line_spacing": 1.5,
            "first_line_indent_chars": 2,
            "heading_1": {"font_zh": "黑体", "font_en": "Times New Roman", "size_pt": 16.0, "align": "center", "bold": True},  # 三号
            "heading_2": {"font_zh": "黑体", "font_en": "Times New Roman", "size_pt": 14.0, "align": "left", "bold": True},    # 四号
            "heading_3": {"font_zh": "黑体", "font_en": "Times New Roman", "size_pt": 12.0, "align": "left", "bold": True},    # 小四
            "table_caption": {"size_pt": 10.5, "align": "center", "position": "above"},  # 五号，表上
            "figure_caption": {"size_pt": 10.5, "align": "center", "position": "below"}, # 五号，图下
            "table_style": "three_line_table",  # 三线表
        },
    },
    "chinese_journal_standard": {
        "id": "chinese_journal_standard",
        "name": "中文核心期刊 / 科技期刊通用排版规范 (2021-2026 范式)",
        "category": "layout_preset",
        "status": "符合 CY/T 170-2019 / CY/T 174-2019 出版规范",
        "year": "2021-2026",
        "page_size": "A4 (21.0cm x 29.7cm)",
        "margins": {"top_cm": 2.5, "bottom_cm": 2.0, "left_cm": 2.0, "right_cm": 2.0},
        "typography": {
            "body_font_zh": "宋体",
            "body_font_en": "Times New Roman",
            "body_size_pt": 10.5,  # 五号字（期刊紧凑版式）
            "line_spacing": 1.25,
            "first_line_indent_chars": 2,
            "heading_1": {"font_zh": "黑体", "font_en": "Times New Roman", "size_pt": 14.0, "align": "left", "bold": True},
            "heading_2": {"font_zh": "黑体", "font_en": "Times New Roman", "size_pt": 12.0, "align": "left", "bold": True},
            "heading_3": {"font_zh": "宋体", "font_en": "Times New Roman", "size_pt": 10.5, "align": "left", "bold": True},
            "table_style": "three_line_table",
        },
    },

    # -------------------------------------------------------------------------
    # 4. 国际主流学术标准 (2021-2026 最新版)
    # -------------------------------------------------------------------------
    "ieee_conference_2024": {
        "id": "ieee_conference_2024",
        "name": "IEEE Transactions & Conference Template (2024-2026 Guidelines)",
        "category": "international",
        "status": "国际电气电子工程师学会最新投稿模板规范",
        "year": "2024-2026",
        "page_size": "US Letter (8.5 x 11 in) 或 A4",
        "columns": 2,  # 双栏
        "margins": {"top_cm": 1.9, "bottom_cm": 2.54, "left_cm": 1.57, "right_cm": 1.57},
        "typography": {
            "body_font_en": "Times New Roman",
            "body_size_pt": 10.0,
            "line_spacing": 1.0,
            "first_line_indent_chars": 2,
            "title_size_pt": 24.0,
            "heading_1": {"font_en": "Times New Roman", "size_pt": 10.0, "align": "center", "small_caps": True},
            "citation_style": "IEEE Numeric [1]",
        },
    },
    "acm_master_2024": {
        "id": "acm_master_2024",
        "name": "ACM Master Article Guidelines (2024-2026 Primary Template)",
        "category": "international",
        "status": "美国计算机学会 (ACM SIGCOMM/SIGKDD/CHI) 核心规范",
        "year": "2024-2026",
        "typography": {
            "body_font_en": "Libertine / Times New Roman",
            "body_size_pt": 9.5,
            "citation_style": "ACM [1] or Author-Year",
        },
        "key_features": [
            "强制 CCS Concepts 计算机分类法",
            "ACM Reference Format 格式块",
            "数据与代码开放声明要求",
        ],
    },
    "apa_7th_edition": {
        "id": "apa_7th_edition",
        "name": "APA 7th Edition (American Psychological Association 2020-2026)",
        "category": "international",
        "status": "全球社会科学、经管与心理学领域最通用规范",
        "year": "2020-2026",
        "page_size": "Letter 或 A4",
        "margins": {"all_cm": 2.54},  # 1 英寸
        "typography": {
            "body_font_en": "Times New Roman 12pt / Calibri 11pt",
            "line_spacing": 2.0,  # 双倍行距
            "first_line_indent_chars": 2,
            "citation_style": "Author-Date (e.g. Smith, 2023)",
        },
        "key_features": [
            "五级标准标题层级体系",
            "Running head 页眉规范",
            "文内作者年引用机制：3位以上作者直接使用 et al.",
        ],
    },
    "nature_springer_2024": {
        "id": "nature_springer_2024",
        "name": "Nature Portfolio & Springer Author Guidelines (2023-2026)",
        "category": "international",
        "status": "Nature 主刊及子刊最新论文编写指南",
        "year": "2023-2026",
        "key_features": [
            "Methods (方法论) 章节结构可置于文末独立板块",
            "强制 CRediT (Contributor Roles Taxonomy) 作者贡献分类声明",
            "Data Availability 与 Code Availability 独立声明",
            "Extended Data 图表与补充材料分层排版准则",
        ],
    },
}


def list_standards(category: str = "all") -> List[Dict[str, Any]]:
    """Return filtered academic standards list for 2021-2026."""
    results = []
    for std_id, item in ACADEMIC_STANDARDS.items():
        if category == "all" or item.get("category") == category:
            results.append(item)
    return results


def get_standard_by_id(std_id: str) -> Optional[Dict[str, Any]]:
    """Get detailed specification for a given academic standard ID."""
    return ACADEMIC_STANDARDS.get(std_id)
