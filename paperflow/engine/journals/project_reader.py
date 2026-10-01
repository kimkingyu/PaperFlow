"""Security-hardened project repository (local folder or GitHub) text extractor for journal recommendation."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from .models import JournalError, response

MAX_FILE_BYTES = 32 * 1024
MAX_TOTAL_CHARS = 60000

EXCLUDE_DIRS = {".git", ".svn", ".hg", ".venv", "venv", "ENV", "env", "node_modules", "__pycache__", "dist", "build", ".idea", ".vscode", ".narrafork", "attached"}
EXCLUDE_FILE_PATTERNS = {
    r"^\.env", r"credential", r"secret", r"token", r"password", r"private", r"key$", r"\.key$", r"\.pem$", r"\.p12$", r"\.pfx$", r"\.sqlite$", r"\.db$"
}

PRIORITY_FILES = [
    "readme.md", "readme-zh.md", "readme_zh.md", "readme.txt", "readme.markdown",
    "article.md", "paper.md", "summary.md", "abstract.md"
]
SECONDARY_DIRS = ["docs", "documentation", "benchmarks", "evaluations", "research"]
ALLOWED_EXTENSIONS = {".md", ".txt", ".rst", ".markdown"}


def _is_sensitive_file(filename: str) -> bool:
    name = filename.lower()
    for pat in EXCLUDE_FILE_PATTERNS:
        if re.search(pat, name):
            return True
    return False


def _is_safe_dir(dir_path: str) -> Path:
    if not dir_path or not isinstance(dir_path, str):
        raise JournalError("INVALID_PATH", "目录路径不能为空")
    stripped = dir_path.strip()
    if stripped.startswith(("\\\\", "//")):
        raise JournalError("NETWORK_PATH_REJECTED", "拒绝访问网络共享或 UNC 路径")

    p = Path(stripped).expanduser().resolve()
    if not p.is_dir():
        raise JournalError("FILE_NOT_FOUND", f"指定的本地目录不存在或不是文件夹: {p}")

    # Block system dirs & drive roots
    root_str = str(p)
    if len(root_str) <= 3: # e.g. C:\, D:\
        raise JournalError("FORBIDDEN_PATH", "不允许直接扫描硬盘根目录")
    lower = root_str.lower().replace("\\", "/").rstrip("/")
    home = str(Path.home()).lower().replace("\\", "/").rstrip("/")
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower().replace("\\", "/").rstrip("/")
    forbidden_exact = {home, windir, "c:/program files", "c:/program files (x86)", "c:/documents and settings"}
    forbidden_suffix = ("/.ssh", "/.aws", "/.gnupg")
    if lower in forbidden_exact or any(lower.endswith(suffix) for suffix in forbidden_suffix):
        raise JournalError("FORBIDDEN_PATH", f"拒绝扫描系统或敏感目录: {p}")

    return p


def _collect_files(root: Path) -> List[Dict[str, Any]]:
    collected = []
    readme_files = []
    secondary_files = []
    meta_files = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in EXCLUDE_DIRS and not d.startswith(".")]
        p_dir = Path(dirpath)

        for f in filenames:
            if f.startswith(".") or _is_sensitive_file(f):
                continue

            ext = f[f.rfind("."):].lower() if "." in f else ""
            full_path = p_dir / f
            rel_path = str(full_path.relative_to(root)).replace("\\", "/")

            item = {"abs": full_path, "rel": rel_path, "name": f}

            if f.lower() in PRIORITY_FILES:
                readme_files.append(item)
            elif any(f"/{d}/" in f"/{rel_path.lower()}" or rel_path.lower().startswith(f"{d}/") for d in SECONDARY_DIRS):
                if ext in ALLOWED_EXTENSIONS:
                    secondary_files.append(item)
            elif ext in ALLOWED_EXTENSIONS and "/" not in rel_path:
                meta_files.append(item) # root level markdown/txt

    readme_files.sort(key=lambda x: x["name"].lower())
    secondary_files.sort(key=lambda x: x["rel"].lower())

    # Priority: readmes first, then secondary docs, then other root files
    collected.extend(readme_files)
    collected.extend(secondary_files[:10]) # cap secondary docs
    if not collected:
        collected.extend(meta_files[:5])

    return collected


def _generate_tree(root: Path, max_depth: int = 2, max_lines: int = 40) -> str:
    lines = []
    count = 0

    def walk(d: Path, prefix: str, depth: int):
        nonlocal count
        if depth > max_depth or count >= max_lines:
            return
        try:
            entries = sorted([e for e in d.iterdir() if e.is_dir() or (e.is_file() and not _is_sensitive_file(e.name))])
            dirs = [e for e in entries if e.is_dir() and e.name.lower() not in EXCLUDE_DIRS and not e.name.startswith(".")]
            files = [e for e in entries if e.is_file() and e.name.lower() in PRIORITY_FILES]

            children = dirs + files
            for i, child in enumerate(children):
                if count >= max_lines: break
                is_last = i == len(children) - 1
                connector = "└── " if is_last else "├── "
                lines.append(f"{prefix}{connector}{child.name}{'/' if child.is_dir() else ''}")
                count += 1
                if child.is_dir():
                    walk(child, prefix + ("    " if is_last else "│   "), depth + 1)
        except PermissionError:
            pass

    walk(root, "", 1)
    return "\n".join(lines)


def read_local_project(dir_path: str, max_chars: int = MAX_TOTAL_CHARS) -> Dict[str, Any]:
    root = _is_safe_dir(dir_path)
    files = _collect_files(root)

    if not files:
        raise JournalError("EMPTY_INPUT", "在该目录下找不到 README 或合法的 Markdown/TXT 说明文档")

    text_parts = []
    extracted = []
    current_chars = 0

    for item in files:
        if current_chars >= max_chars:
            break
        try:
            with open(item["abs"], "rb") as f:
                raw = f.read(MAX_FILE_BYTES + 1)
            content = raw.decode("utf-8", errors="replace")

            allowed = min(MAX_FILE_BYTES, max_chars - current_chars)
            if len(content) > allowed:
                content = content[:allowed]

            text_parts.append(f"\n\n===== 文档: {item['rel']} =====\n\n{content.strip()}")
            extracted.append({"path": item["rel"], "chars": len(content)})
            current_chars += len(content)
        except OSError:
            pass

    tree = _generate_tree(root)

    final_text = f"===== 项目目录结构 =====\n{tree}\n\n" + "".join(text_parts)
    if len(final_text) > max_chars:
        final_text = final_text[:max_chars] + "\n\n...(由于字数限制，内容已截断)"

    return response({
        "project_name": root.name,
        "source_type": "local_directory",
        "location": str(root),
        "extracted_files": extracted,
        "directory_tree": tree,
        "text": final_text,
        "char_count": len(final_text)
    })


def _parse_github_repo(query: str) -> str:
    q = query.strip()
    if not q:
        raise JournalError("INVALID_INPUT", "仓库名不能为空")
    m = re.search(r"github\.com[:/]([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git)?/?$", q)
    if m:
        repo = m.group(1)
    else:
        repo = q
    repo = repo.strip("/")
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise JournalError("INVALID_INPUT", "无效的 GitHub 仓库格式，应为 owner/repo 或 https://github.com/owner/repo")
    return repo


def read_github_project(repo_query: str, max_chars: int = MAX_TOTAL_CHARS) -> Dict[str, Any]:
    repo = _parse_github_repo(repo_query)

    # 1. Try gh CLI
    try:
        res = subprocess.run(["gh", "repo", "view", repo, "--json", "name,description,url"], capture_output=True, text=True, timeout=15)
        if res.returncode != 0:
            raise JournalError("SOURCE_UNAVAILABLE", f"GitHub 仓库查询失败: {res.stderr.strip() or res.stdout.strip()}")
        info = json.loads(res.stdout)
        name = info["name"]
        url = info["url"]

        # Fetch readme
        res_readme = subprocess.run(["gh", "api", f"repos/{repo}/readme", "--jq", ".content"], capture_output=True, text=True, timeout=15)
        content = ""
        if res_readme.returncode == 0:
            import base64
            content = base64.b64decode(res_readme.stdout.strip()).decode("utf-8", errors="replace")

        # Fetch docs list and readme of docs if possible (limit to a few files to avoid abuse)
        docs_content = ""
        res_tree = subprocess.run(["gh", "api", f"repos/{repo}/git/trees/main?recursive=1", "--jq", ".tree[] | select(.path | startswith(\"docs/\") and endswith(\".md\")) | .path"], capture_output=True, text=True, timeout=15)
        if res_tree.returncode != 0:
             res_tree = subprocess.run(["gh", "api", f"repos/{repo}/git/trees/master?recursive=1", "--jq", ".tree[] | select(.path | startswith(\"docs/\") and endswith(\".md\")) | .path"], capture_output=True, text=True, timeout=15)

        if res_tree.returncode == 0:
            doc_paths = res_tree.stdout.strip().split("\n")[:5]
            extracted_docs = []
            for dp in doc_paths:
                if not dp.strip(): continue
                if _is_sensitive_file(dp): continue
                res_doc = subprocess.run(["gh", "api", f"repos/{repo}/contents/{dp}", "--jq", ".content"], capture_output=True, text=True, timeout=15)
                if res_doc.returncode == 0:
                    doc_text = base64.b64decode(res_doc.stdout.strip()).decode("utf-8", errors="replace")
                    extracted_docs.append({"path": dp, "chars": len(doc_text)})
                    docs_content += f"\n\n===== 文档: {dp} =====\n\n{doc_text.strip()}\n"
        else:
            extracted_docs = []

        full_text = f"# {name}\n\n{content.strip()}\n{docs_content}"
        if len(full_text) > max_chars:
            full_text = full_text[:max_chars] + "\n\n...(由于字数限制，内容已截断)"

        return response({
            "project_name": name,
            "source_type": "github_repository",
            "location": url,
            "extracted_files": [{"path": "README.md", "chars": len(content)}] + extracted_docs,
            "directory_tree": "(GitHub 仓库无法直接生成完整目录树，仅提取了文档)",
            "text": full_text,
            "char_count": len(full_text)
        })

    except subprocess.TimeoutExpired:
        raise JournalError("SOURCE_UNAVAILABLE", "GitHub 查询超时")
    except Exception as e:
        if isinstance(e, JournalError):
            raise e
        raise JournalError("INTERNAL_ERROR", f"读取 GitHub 仓库时发生错误: {str(e)}")
