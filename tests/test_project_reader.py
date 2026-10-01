"""Tests for local/GitHub project reader security boundaries."""
import os
from pathlib import Path

import pytest

from paperflow.engine.journals.models import JournalError
from paperflow.engine.journals.project_reader import read_local_project, read_github_project


def test_read_local_project_success(tmp_path: Path):
    proj = tmp_path / "my_project"
    proj.mkdir()
    (proj / "README.md").write_text("# Hello\n\nThis is my paper project.", encoding="utf-8")
    (proj / "docs").mkdir()
    (proj / "docs" / "spec.md").write_text("Technical details here.", encoding="utf-8")
    (proj / ".env").write_text("SECRET=123", encoding="utf-8")

    result = read_local_project(str(proj))
    data = result["data"]
    assert data["project_name"] == "my_project"
    assert "Hello" in data["text"]
    assert "Technical details here." in data["text"]
    assert "SECRET" not in data["text"]
    assert len(data["extracted_files"]) == 2


def test_read_local_project_blocks_forbidden_paths(tmp_path: Path):
    # System paths
    for bad in ["C:\\Windows", "C:\\", str(Path.home())]:
        with pytest.raises(JournalError) as exc:
            read_local_project(bad)
        assert exc.value.code in ("FORBIDDEN_PATH", "NETWORK_PATH_REJECTED", "FILE_NOT_FOUND")


def test_read_local_project_no_docs(tmp_path: Path):
    proj = tmp_path / "empty_project"
    proj.mkdir()
    (proj / "src.py").write_text("print('hello')", encoding="utf-8")

    with pytest.raises(JournalError) as exc:
        read_local_project(str(proj))
    assert exc.value.code == "EMPTY_INPUT"


def test_read_github_project_validation():
    with pytest.raises(JournalError):
        read_github_project("")
    with pytest.raises(JournalError):
        read_github_project("not-a-repo")
    with pytest.raises(JournalError):
        read_github_project("https://evil.com/fake/repo")

    # Valid formats
    from paperflow.engine.journals.project_reader import _parse_github_repo
    assert _parse_github_repo("owner/repo") == "owner/repo"
    assert _parse_github_repo("https://github.com/owner/repo/") == "owner/repo"
    assert _parse_github_repo("git@github.com:owner/repo.git") == "owner/repo"
