"""CLI commands for paper format audit and normalization."""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from paperflow.engine.formatter import IssueSeverity, PaperFormatAuditor, PaperFormatNormalizer
from paperflow.engine.word_live_bridge import WordNotAvailableError, live_bridge


def run_audit(args: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="PaperFlow 论文格式合规审查与体检器")
    parser.add_argument("file_path", nargs="?", default="", help="可选：要审查的 docx 文件路径。若为空则自动审查桌面打开的 Word")
    parser.add_argument("--standard", default="chinese_thesis_standard", help="学术标准 ID (默认: chinese_thesis_standard)")
    parser.add_argument("--comments", action="store_true", help="活跃 Word 模式下在违规处自动插入原生审阅批注")
    parsed = parser.parse_args(args)

    print("=" * 65)
    print("   PaperFlow · 学术论文格式规范审查报告")
    print("=" * 65)

    try:
        if parsed.file_path:
            print(f"[*] 审查离线文档: {parsed.file_path}")
            report = PaperFormatAuditor.audit_docx(parsed.file_path, standard_id=parsed.standard)
        else:
            print("[*] 正在连接桌面 Word/WPS 活跃文档...")
            report = live_bridge.audit_document(standard_id=parsed.standard, add_comments=parsed.comments)

        score = report.get("format_score", 0)
        summary = report.get("summary", {})
        issues = report.get("issues", [])

        print(f"\n[+] 参照规范: {report.get('standard_name', parsed.standard)}")
        print(f"[+] 排版合规评分: {score} / 100")
        print(f"[+] 统计概览: {summary.get('total_paragraphs', 0)} 段落 | {summary.get('total_tables', 0)} 表格")
        print(f"    - 严重问题 (Error):   {summary.get('errors', 0)}")
        print(f"    - 警告问题 (Warning): {summary.get('warnings', 0)}")
        print(f"    - 优化建议 (Info):    {summary.get('suggestions', 0)}")

        if not issues:
            print("\n[√] 完美！未检测到任何格式缺陷，完全符合学术排版规范。")
            return 0

        print("\n" + "-" * 65)
        print("   问题明细清单")
        print("-" * 65)
        for idx, item in enumerate(issues, start=1):
            sev = item["severity"].upper()
            tag = "[严重]" if sev == "ERROR" else ("[警告]" if sev == "WARNING" else "[建议]")
            print(f"\n{idx}. {tag} [{item['rule_id']}] 位于 {item['location']}")
            print(f"   说明: {item['message']}")
            if item.get("snippet"):
                print(f"   片段: \"{item['snippet']}\"")
            if item.get("suggestion"):
                print(f"   建议: {item['suggestion']}")

        if report.get("comments_injected"):
            print(f"\n[√] 已在 Word 文档对应违规位置成功插入 {report['comments_injected']} 条审阅批注气泡。")

        print("\n提示：可使用 'python -m paperflow fix' 命令进行一键自动规范化修复。")
        return 1 if summary.get("errors", 0) > 0 else 0

    except WordNotAvailableError as e:
        print(f"\n[!] 连接失败: {e}")
        return 2
    except Exception as e:
        print(f"\n[!] 审查发生异常: {e}")
        return 3


def run_fix(args: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="PaperFlow 论文格式一键规范化与自愈工具")
    parser.add_argument("file_path", nargs="?", default="", help="可选：要修复的 docx 文件路径。若为空则修复桌面打开的 Word")
    parser.add_argument("--output", default="", help="离线模式时指定修复后的另存路径 (默认覆盖原文件)")
    parser.add_argument("--standard", default="chinese_thesis_standard", help="学术标准 ID (默认: chinese_thesis_standard)")
    parsed = parser.parse_args(args)

    print("=" * 65)
    print("   PaperFlow · 学术论文格式一键规范化")
    print("=" * 65)

    try:
        if parsed.file_path:
            print(f"[*] 正在规范化文件: {parsed.file_path}")
            res = PaperFormatNormalizer.normalize_docx(
                input_path=parsed.file_path,
                output_path=parsed.output if parsed.output else None,
                standard_id=parsed.standard,
            )
            print(f"[√] 修复完成！保存至: {res['saved_path']}")
        else:
            print("[*] 正在规范化当前桌面 Word/WPS 活跃文档...")
            res = live_bridge.normalize_document(standard_id=parsed.standard)
            print(f"[√] 活跃文档 '{res.get('document_name')}' 规范化完成并已保存！")

        print(f"\n[+] 本次共执行了 {res.get('total_changes', 0)} 项修复操作：")
        for idx, chg in enumerate(res.get("changes_applied", []), start=1):
            print(f"    {idx}. {chg}")

        print("\n[√] 排版修复完成，建议重新运行 'python -m paperflow audit' 复检。")
        return 0

    except WordNotAvailableError as e:
        print(f"\n[!] 连接失败: {e}")
        return 2
    except Exception as e:
        print(f"\n[!] 修复发生异常: {e}")
        return 3
