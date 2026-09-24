"""One-Click MCP Auto-Connect Setup Tool.

Automatically detects installed AI clients:
- Tencent WorkBuddy / CodeBuddy
- Qwen (通义千问桌面端 / Qwen Agent / Qoder)
- Claude Desktop
- Cursor
- Cherry Studio
- Cline / Roo Code (VS Code Extensions)

And safely injects the PaperFlow Word-Bridge MCP server configuration
so users can connect their AI clients to Microsoft Word / WPS with ZERO manual copy-pasting.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def get_current_python_exe() -> str:
    """Return the absolute path of the current python interpreter."""
    return str(Path(sys.executable).resolve())


def get_mcp_server_config() -> Dict[str, Any]:
    """Generate the standard MCP configuration block for PaperFlow."""
    python_exe = get_current_python_exe()
    project_root = str(Path(__file__).resolve().parent.parent.parent)

    return {
        "command": python_exe,
        "args": ["-m", "paperflow.server.mcp_server"],
        "env": {
            "PYTHONPATH": project_root,
            "PYTHONIOENCODING": "utf-8",
        },
    }


CLIENT_TARGETS = [
    {
        "name": "Claude Desktop",
        "description": "Anthropic 官方 Claude 桌面客户端",
        "paths": [
            Path(os.environ.get("APPDATA", "")) / "Claude" / "claude_desktop_config.json",
            Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
        ],
        "key_path": ["mcpServers"],
    },
    {
        "name": "Cursor",
        "description": "Cursor AI 编辑器",
        "paths": [
            Path.home() / ".cursor" / "mcp.json",
            Path(os.environ.get("USERPROFILE", "")) / ".cursor" / "mcp.json",
        ],
        "key_path": ["mcpServers"],
    },
    {
        "name": "Tencent WorkBuddy / CodeBuddy",
        "description": "腾讯 WorkBuddy 企业级 AI 助手",
        "paths": [
            Path(os.environ.get("APPDATA", "")) / "Tencent" / "WorkBuddy" / "mcp_config.json",
            Path.home() / ".workbuddy" / "mcp.json",
            Path.home() / ".codebuddy" / "mcp.json",
        ],
        "key_path": ["mcpServers"],
    },
    {
        "name": "Qwen (通义千问桌面端 / Qoder)",
        "description": "阿里通义千问 Qwen / Qoder 客户端",
        "paths": [
            Path(os.environ.get("APPDATA", "")) / "Qwen" / "mcp.json",
            Path.home() / ".qwen" / "mcp.json",
            Path.home() / ".qoder" / "mcp.json",
        ],
        "key_path": ["mcpServers"],
    },
    {
        "name": "Cherry Studio",
        "description": "开源多模型桌面客户端 Cherry Studio",
        "paths": [
            Path(os.environ.get("APPDATA", "")) / "CherryStudio" / "mcp_servers.json",
            Path.home() / ".cherry-studio" / "mcp.json",
        ],
        "key_path": ["mcpServers"],
    },
    {
        "name": "Cline (VS Code 扩展)",
        "description": "VS Code 自动化编程扩展 Cline",
        "paths": [
            Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json",
        ],
        "key_path": ["mcpServers"],
    },
    {
        "name": "Roo Code (VS Code 扩展)",
        "description": "VS Code 自动化编程扩展 Roo Code",
        "paths": [
            Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage" / "rooveterinaryinc.roo-cline" / "settings" / "cline_mcp_settings.json",
        ],
        "key_path": ["mcpServers"],
    },
]


def inject_mcp_config(file_path: Path, server_name: str, config: Dict[str, Any]) -> bool:
    """Safely inject the MCP server configuration into a target JSON file.

    - Never overwrites a config file that cannot be parsed (raises instead).
    - Writes a .bak backup of the original file before modifying it.
    """
    data: Dict[str, Any] = {}
    if file_path.exists():
        raw = file_path.read_text(encoding="utf-8")
        if raw.strip():
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(
                    f"配置文件不是合法 JSON，为避免破坏原配置已跳过：{e}"
                ) from e
            if not isinstance(data, dict):
                raise RuntimeError("配置文件顶层不是 JSON 对象，已跳过。")
        backup = file_path.with_name(file_path.name + ".bak")
        backup.write_text(raw, encoding="utf-8")

    if "mcpServers" not in data or not isinstance(data["mcpServers"], dict):
        data["mcpServers"] = {}

    data["mcpServers"][server_name] = config

    file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    return True


def run_setup(force_all: bool = False) -> None:
    """Scan and configure all detected AI clients."""
    server_config = get_mcp_server_config()
    server_name = "paperflow"

    print("=" * 65)
    print("   PaperFlow · AI 客户端与 Office/Word 自动连接向导")
    print("=" * 65)
    print(f"[*] 当前 Python 环境: {get_current_python_exe()}")
    print("[*] 正在扫描本机已安装的大模型 AI 客户端...\n")

    configured_count = 0

    for client in CLIENT_TARGETS:
        client_name = client["name"]
        paths = client["paths"]
        matched_path: Optional[Path] = None

        for p in paths:
            # If the config file exists, or if its parent app directory exists
            if p.exists() or p.parent.exists() or force_all:
                matched_path = p
                break

        if matched_path:
            try:
                inject_mcp_config(matched_path, server_name, server_config)
                print(f"  [√] 成功连接: {client_name}")
                print(f"      配置文件: {matched_path}")
                configured_count += 1
            except Exception as e:
                print(f"  [X] 写入失败: {client_name} ({e})")
        else:
            print(f"  [-] 未检测到: {client_name} (跳过)")

    print("\n" + "=" * 65)
    if configured_count > 0:
        print(f"🎉 全部配置完成！已为 {configured_count} 个 AI 客户端注入 PaperFlow 连接通道。")
        print("📌 使用方法：")
        print("   1. 重启你的 AI 客户端（如 WorkBuddy、通义千问、Cursor 等）；")
        print("   2. 在桌面打开你的 Word 或 WPS 论文文件；")
        print("   3. 在 AI 客户端聊天框输入：“连接当前 Word 论文并列出章节”，即可开启实时写作交互！")
    else:
        print("⚠️ 未自动匹配到常用客户端目录。你可以手动复制下方 JSON 配置：\n")
        manual_json = json.dumps({"mcpServers": {server_name: server_config}}, ensure_ascii=False, indent=2)
        print(manual_json)
    print("=" * 65)


if __name__ == "__main__":
    run_setup()
