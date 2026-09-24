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
    elif command in ("run", "serve"):
        from paperflow.server.mcp_server import run_server
        run_server()
    else:
        print("PaperFlow · AI 客户端与 Office/Word 实时连接桥梁")
        print("\n可用命令：")
        print("  python -m paperflow setup   # 一键自动为已安装的 AI 客户端注入连接配置")
        print("  python -m paperflow doctor  # 检查环境并探测桌面上打开的 Word/WPS")
        print("  python -m paperflow run     # 启动 MCP Server (stdio 模式)")


if __name__ == "__main__":
    main()
