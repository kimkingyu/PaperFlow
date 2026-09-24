"""End-to-end MCP smoke test.

Spawns `python -m paperflow run` over stdio exactly like an MCP client
(NarraFork, Claude Desktop, Cursor) would, then:
1. lists the tools the server exposes,
2. calls get_active_word_doc (safe, read-only; never launches Word),
3. calls scan_anti_ai_flavor (pure text, no Office needed).

Usage:  .venv/Scripts/python.exe scripts/mcp_smoke_test.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent


def _text(result) -> str:
    return "\n".join(getattr(c, "text", str(c)) for c in result.content)


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "paperflow", "run"],
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONIOENCODING": "utf-8"},
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print(f"[tools] {len(names)} exposed:")
            for n in names:
                print(f"   - {n}")

            res = await session.call_tool("get_active_word_doc", {})
            print("\n[get_active_word_doc]")
            print(_text(res))

            res = await session.call_tool(
                "scan_anti_ai_flavor",
                {"text": "不可否认的是，本研究旨在深入探讨该问题。"},
            )
            payload = json.loads(_text(res))
            print("\n[scan_anti_ai_flavor] findings =", payload["total_findings"],
                  "| risk =", payload["ai_risk_score"])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
