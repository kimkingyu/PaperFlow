"""Environment and Connectivity Diagnostics tool for PaperFlow."""

from __future__ import annotations

import sys
from paperflow.engine.word_live_bridge import live_bridge


def run_doctor() -> None:
    """Check python, win32com, Word/WPS status."""
    print("=" * 60)
    print("   PaperFlow 环境自检与 Office 连接探测器")
    print("=" * 60)
    print(f"1. Python 版本: {sys.version.split()[0]} ({'OK' if sys.version_info >= (3, 9) else '需升级'})")

    # Check pywin32
    try:
        import win32com.client
        print("2. Windows COM 支持 (pywin32): 已就绪 (OK)")
    except ImportError:
        print("2. Windows COM 支持 (pywin32): 未安装 (请运行 pip install pywin32)")

    # Check live Word/WPS instance
    print("3. 探测桌面上打开的 Office 软件 (Word / WPS)...")
    ok, msg = live_bridge.connect()
    if ok:
        print(f"   [√] {msg}")
        try:
            info = live_bridge.get_document_info()
            print(f"   [i] 当前活跃文档: {info['document_name']}")
            print(f"   [i] 段落数: {info['paragraph_count']} | 字数: {info['word_count']}")
        except Exception as e:
            print(f"   [!] 获取文档信息异常: {e}")
    else:
        print(f"   [!] 提示: {msg}")
        print("   (提示：请在桌面上打开任意 Word 文档以支持实时写入与批注交互)")

    print("=" * 60)


if __name__ == "__main__":
    run_doctor()
