"""PaperFlow CLI entrypoint."""

from __future__ import annotations

import sys


def main() -> None:
    args = sys.argv[1:]
    command = args[0] if args else "run"

    if command in ("setup", "--setup"):
        from paperflow.cli.setup_mcp import run_setup
        force = "--all" in args
        run_setup(force_all=force)
    elif command in ("doctor", "--doctor"):
        from paperflow.cli.doctor import run_doctor
        run_doctor()
    elif command in ("audit", "check", "--check"):
        from paperflow.cli.formatter_cli import run_audit
        sys.exit(run_audit(args[1:]))
    elif command in ("fix", "format", "normalize", "--fix"):
        from paperflow.cli.formatter_cli import run_fix
        sys.exit(run_fix(args[1:]))
    elif command in ("journal", "journals"):
        from paperflow.cli.journal_cli import run_journal_cli
        sys.exit(run_journal_cli(args[1:]))
    elif command in ("paper", "papers"):
        from paperflow.cli.paper_cli import run_paper_cli
        sys.exit(run_paper_cli(args[1:]))
    elif command in ("web", "gui", "studio"):
        from paperflow.gui.server import run_gui
        sys.exit(run_gui(args[1:]))
    elif command in ("run", "serve"):
        from paperflow.server.mcp_server import run_server
        run_server()
    else:
        print("PaperFlow · 独立科研 Agent 工作台与 MCP 写作工具服务")
        print("\n可用命令：")
        print("  python -m paperflow audit   # 论文格式审查体检 (支持桌面活动 Word 或 docx 文件)")
        print("  python -m paperflow fix     # 论文格式一键规范化与自愈修复")
        print("  python -m paperflow journal # 期刊本地选刊、风险预警与审稿跟踪")
        print("  python -m paperflow paper   # 真实文献检索、合法全文、本地分页阅读与有证据解读卡")
        print("  python -m paperflow web     # 打开独立科研 Agent 与完整网页工作台（gui/studio 为别名）")
        print("  python -m paperflow setup   # 一键自动为已安装的 AI 客户端注入连接配置")
        print("  python -m paperflow doctor  # 检查环境并探测桌面上打开的 Word/WPS")
        print("  python -m paperflow run     # 启动 MCP Server (stdio 模式)")


if __name__ == "__main__":
    main()
