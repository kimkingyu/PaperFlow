"""Read-only probe: is Word / WPS installed, running, and reachable over COM?

This script never launches Word or WPS. It only:
1. checks registry for Word / WPS COM ProgIDs and which executable serves them,
2. lists running WINWORD.EXE / wps.exe processes,
3. attaches to an already-running instance via GetActiveObject.
"""

from __future__ import annotations

import subprocess
import winreg

PROGIDS = ["Word.Application", "KWps.Application", "Wps.Application"]
PROCESS_NAMES = ["WINWORD.EXE", "wps.exe"]


def _read_default(root, path: str) -> str | None:
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, "")
            return value
    except OSError:
        return None


def server_exe(progid: str) -> str | None:
    """Return the LocalServer32 command line serving this ProgID, or None."""
    clsid = _read_default(winreg.HKEY_CLASSES_ROOT, f"{progid}\\CLSID")
    if not clsid:
        return None
    for view in ("CLSID", "WOW6432Node\\CLSID"):
        exe = _read_default(winreg.HKEY_CLASSES_ROOT, f"{view}\\{clsid}\\LocalServer32")
        if exe:
            return exe
    return f"(CLSID {clsid}, no LocalServer32)"


def running(image: str) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
        capture_output=True,
        text=True,
        errors="ignore",
    ).stdout
    return image.lower() in out.lower()


def main() -> None:
    print("== COM ProgID -> server executable ==")
    for pid in PROGIDS:
        print(f"  {pid:<20} {server_exe(pid) or 'not registered'}")

    print("== Process running ==")
    for name in PROCESS_NAMES:
        print(f"  {name:<20} {'yes' if running(name) else 'no'}")

    print("== Attach to running instance (GetActiveObject, no launch) ==")
    try:
        import win32com.client
    except ImportError:
        print("  pywin32 not installed")
        return

    for pid in PROGIDS:
        try:
            app = win32com.client.GetActiveObject(pid)
        except Exception as e:  # noqa: BLE001
            print(f"  {pid:<20} not attachable ({type(e).__name__})")
            continue
        try:
            count = app.Documents.Count
        except Exception:  # noqa: BLE001
            count = "?"
        print(f"  {pid:<20} attached, open documents = {count}")


if __name__ == "__main__":
    main()
