"""SQLite planning tests use isolated temporary directories only."""
from __future__ import annotations

import json
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import paperflow.engine.planning.store as store_module
from paperflow.engine.planning.models import PlanningError, ResearchPlan
from paperflow.engine.planning.store import MAX_PLAN_BYTES, SCHEMA_VERSION, PlanningStore, default_data_dir


def plan_dict(title="研究项目", goal="研究目标"):
    return {
        "profile": {"title": title, "goal": goal},
        "questions": [{"id": "q1", "question": "能否改进基线"}],
        "experiments": [{"id": "exp1", "question_id": "q1", "title": "对照实验"}],
        "evidence": [{"id": "ev1", "kind": "note", "source_ref": "missing/data.txt", "summary": "提交者记录"}],
        "tasks": [{"id": "t1", "text": "准备实验", "completion_condition": "协议完成"}],
    }


@pytest.fixture
def store(tmp_path):
    return PlanningStore(str(tmp_path / "research"))


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(PlanningError) as error:
        function(*args, **kwargs)
    assert error.value.code == code
    return error.value


def test_default_directory_override_and_constructor_are_inert(tmp_path, monkeypatch):
    directory = tmp_path / "override" / "research"
    monkeypatch.setenv("PAPERFLOW_RESEARCH_HOME", str(directory))
    monkeypatch.setenv("PAPERFLOW_JOURNAL_HOME", str(tmp_path / "journals"))
    assert default_data_dir() == directory.resolve()
    instance = PlanningStore()
    assert instance.directory == directory.resolve()
    assert instance.path == directory.resolve() / "planning.sqlite3"
    assert not directory.exists()


def test_default_directory_localappdata(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERFLOW_RESEARCH_HOME", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    expected = tmp_path / "localappdata" / "PaperFlow" / "research"
    assert default_data_dir() == expected
    assert PlanningStore().directory == expected
    assert not expected.exists()


def test_default_directory_home_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("PAPERFLOW_RESEARCH_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    expected = tmp_path / "home" / ".paperflow" / "research"
    assert default_data_dir() == expected
    assert not expected.exists()


def test_empty_reads_and_missing_save_never_connect_or_create_directory(store, monkeypatch):
    def fail_connect(*args, **kwargs):
        pytest.fail("空库读取或缺失项目保存不得连接/创建 SQLite")

    monkeypatch.setattr(store_module.sqlite3, "connect", fail_connect)
    assert store.list_projects() == {"projects": [], "total": 0, "limit": 20, "offset": 0}
    assert store.list_projects(limit=1, offset=2) == {"projects": [], "total": 0, "limit": 1, "offset": 2}
    assert_error("PROJECT_NOT_FOUND", store.get, "absent")
    assert_error("PROJECT_NOT_FOUND", store.get, "absent", revision=1)
    assert_error("PROJECT_NOT_FOUND", store.save, "absent", plan_dict(), expected_revision=1)
    assert not store.directory.exists()


def test_schema_version_and_initial_snapshot_keys_and_persistence(store):
    assert SCHEMA_VERSION == 1
    assert MAX_PLAN_BYTES == 2 * 1024 * 1024
    snapshot = store.create(plan_dict(), change_note=" 创建研究项目 ")
    assert re.fullmatch(r"rp_[0-9a-f]{32}", snapshot["project_id"])
    assert snapshot["revision"] == 1
    assert snapshot["created_at"] == snapshot["updated_at"]
    assert snapshot["created_at"].endswith("+00:00")
    assert snapshot["change_note"] == "创建研究项目"
    assert set(snapshot) == {"project_id", "revision", "created_at", "updated_at", "change_note", "plan"}
    assert snapshot["plan"] == ResearchPlan.model_validate(plan_dict()).model_dump(mode="json")
    reopened = PlanningStore(str(store.directory))
    assert reopened.get(snapshot["project_id"]) == snapshot
    assert reopened.get(snapshot["project_id"], revision=1) == snapshot
    assert list(store.directory.iterdir()) == [store.path]
    with sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 1


def test_dates_and_payload_are_json_values_and_input_is_unchanged(store):
    data = plan_dict(" 标题 ")
    data["profile"].update(start_date=date(2026, 9, 30), target_date=date(2026, 12, 31))
    data["tasks"][0]["due_date"] = date(2026, 10, 2)
    original = deepcopy(data)
    snapshot = store.create(data)
    assert data == original
    assert snapshot["plan"]["profile"]["start_date"] == "2026-09-30"
    assert snapshot["plan"]["tasks"][0]["due_date"] == "2026-10-02"
    assert json.loads(json.dumps(snapshot, allow_nan=False)) == snapshot
    assert store.get(snapshot["project_id"]) == snapshot


def test_modifying_inputs_and_returned_results_never_changes_database(store):
    data = plan_dict()
    snapshot = store.create(data)
    identifier = snapshot["project_id"]
    expected = deepcopy(snapshot)
    data["profile"]["title"] = "修改输入"
    snapshot["plan"]["profile"]["title"] = "修改返回值"
    snapshot["plan"]["tasks"].clear()
    result = store.get(identifier)
    assert result == expected
    result["plan"]["evidence"].clear()
    assert store.get(identifier) == expected
    listing = store.list_projects()
    listing["projects"][0]["title"] = "修改分页结果"
    assert store.list_projects()["projects"][0]["title"] == expected["plan"]["profile"]["title"]


def test_save_revision_history_never_overwrites_old_snapshots(store, monkeypatch):
    timestamps = iter(["2026-09-30T00:00:00+00:00", "2026-09-30T01:00:00+00:00", "2026-09-30T02:00:00+00:00"])
    monkeypatch.setattr(store_module, "utc_now", lambda: next(timestamps))
    first = store.create(plan_dict("版本1"))
    identifier = first["project_id"]
    second = store.save(identifier, plan_dict("版本2"), expected_revision=1)
    third = store.save(identifier, plan_dict("版本3"), expected_revision=2, change_note=" 第三版本 ")
    assert [first["revision"], second["revision"], third["revision"]] == [1, 2, 3]
    assert first["created_at"] == second["created_at"] == third["created_at"]
    assert first["updated_at"] < second["updated_at"] < third["updated_at"]
    assert third["change_note"] == "第三版本"
    assert store.get(identifier) == third
    assert store.get(identifier, revision=1) == first
    assert store.get(identifier, revision=2) == second
    assert store.get(identifier, revision=3) == third
    assert store.list_projects()["projects"][0]["revision"] == 3
    assert store.list_projects()["projects"][0]["title"] == "版本3"
    with sqlite3.connect(store.path.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3


def test_stale_revision_conflict_preserves_database_and_old_snapshots(store):
    first = store.create(plan_dict("版本1"))
    identifier = first["project_id"]
    second = store.save(identifier, plan_dict("版本2"), expected_revision=1)
    before = store.path.read_bytes()
    error = assert_error("REVISION_CONFLICT", store.save, identifier, plan_dict("过期修改"), expected_revision=1)
    assert identifier not in str(error)
    assert store.path.read_bytes() == before
    assert store.get(identifier) == second
    assert store.get(identifier, revision=1) == first
    assert_error("REVISION_NOT_FOUND", store.get, identifier, revision=3)


def test_two_stores_concurrent_save_has_exactly_one_winner(store):
    first = store.create(plan_dict())
    stores = [PlanningStore(str(store.directory)), PlanningStore(str(store.directory))]
    barrier = Barrier(2)

    def save(index):
        barrier.wait(timeout=10)
        try:
            return stores[index].save(first["project_id"], plan_dict(f"线程{index}"), expected_revision=1)
        except PlanningError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, range(2)))
    successes = [result for result in results if isinstance(result, dict)]
    assert len(successes) == 1
    assert results.count("REVISION_CONFLICT") == 1
    assert store.get(first["project_id"]) == successes[0]
    assert successes[0]["revision"] == 2
    assert store.get(first["project_id"], revision=1) == first
    assert_error("REVISION_NOT_FOUND", store.get, first["project_id"], revision=3)


def test_concurrent_initial_creates_publish_without_overwrite(store, monkeypatch):
    real_link = store_module.os.link
    barrier = Barrier(2)

    def synchronized_link(source, target):
        barrier.wait(timeout=10)
        return real_link(source, target)

    monkeypatch.setattr(store_module.os, "link", synchronized_link)
    stores = [PlanningStore(str(store.directory)), PlanningStore(str(store.directory))]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(stores[index].create, plan_dict(f"初始项目{index}")) for index in range(2)]
        snapshots = [future.result(timeout=15) for future in futures]
    assert len({snapshot["project_id"] for snapshot in snapshots}) == 2
    assert store.list_projects()["total"] == 2
    for snapshot in snapshots:
        assert store.get(snapshot["project_id"]) == snapshot
    assert list(store.directory.iterdir()) == [store.path]


def test_get_and_list_use_real_read_only_connections(store, monkeypatch):
    snapshot = store.create(plan_dict())
    real_connect = store_module.sqlite3.connect
    connections = []

    def connect(database, **kwargs):
        assert database.endswith("?mode=ro")
        assert kwargs["uri"] is True
        connection = real_connect(database, **kwargs)
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE projects SET title='不可写入'")
        connections.append(database)
        return connection

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    before = store.path.read_bytes()
    assert store.get(snapshot["project_id"]) == snapshot
    assert store.list_projects()["total"] == 1
    assert len(connections) == 2
    assert store.path.read_bytes() == before
    assert list(store.directory.iterdir()) == [store.path]


def test_save_opens_existing_database_mode_rw_not_create(store, monkeypatch):
    snapshot = store.create(plan_dict())
    real_connect = store_module.sqlite3.connect
    seen = []

    def connect(database, **kwargs):
        seen.append(database)
        return real_connect(database, **kwargs)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    store.save(snapshot["project_id"], plan_dict(), expected_revision=1)
    assert len(seen) == 1
    assert seen[0].endswith("?mode=rw")


def test_missing_project_and_missing_revision_are_distinct(store):
    snapshot = store.create(plan_dict())
    before = store.path.read_bytes()
    assert_error("PROJECT_NOT_FOUND", store.get, "absent")
    assert_error("PROJECT_NOT_FOUND", store.get, "absent", revision=1)
    assert_error("PROJECT_NOT_FOUND", store.save, "absent", plan_dict(), expected_revision=1)
    assert_error("REVISION_NOT_FOUND", store.get, snapshot["project_id"], revision=2)
    assert store.path.read_bytes() == before


def test_save_does_not_initialize_existing_empty_sqlite(tmp_path):
    instance = PlanningStore(str(tmp_path))
    with sqlite3.connect(instance.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    before = instance.path.read_bytes()
    assert_error("SCHEMA_CHANGED", instance.save, "absent", plan_dict(), expected_revision=1)
    assert_error("SCHEMA_CHANGED", instance.list_projects)
    assert instance.path.read_bytes() == before
    with sqlite3.connect(instance.path.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


@pytest.mark.parametrize("version", [0, 1, 2, 100])
def test_foreign_or_unsupported_schema_is_never_modified(tmp_path, version):
    instance = PlanningStore(str(tmp_path))
    with sqlite3.connect(instance.path) as connection:
        connection.execute("CREATE TABLE foreign_data(value TEXT NOT NULL)")
        connection.execute("INSERT INTO foreign_data VALUES ('PRIVATE_SECRET')")
        connection.execute(f"PRAGMA user_version={version}")
    before = instance.path.read_bytes()
    for method, args, kwargs in [
        (instance.create, (plan_dict(),), {}),
        (instance.get, ("absent",), {}),
        (instance.save, ("absent", plan_dict()), {"expected_revision": 1}),
        (instance.list_projects, (), {}),
    ]:
        error = assert_error("SCHEMA_CHANGED", method, *args, **kwargs)
        assert "PRIVATE_SECRET" not in str(error)
        assert str(instance.path) not in str(error)
        assert instance.path.read_bytes() == before


def test_matching_table_names_with_foreign_columns_are_rejected(tmp_path):
    instance = PlanningStore(str(tmp_path))
    with sqlite3.connect(instance.path) as connection:
        connection.execute("CREATE TABLE projects(unrelated TEXT)")
        connection.execute("CREATE TABLE snapshots(unrelated TEXT)")
        connection.execute("PRAGMA user_version=1")
    before = instance.path.read_bytes()
    assert_error("SCHEMA_CHANGED", instance.create, plan_dict())
    assert instance.path.read_bytes() == before


def test_other_journal_database_is_not_touched(tmp_path):
    journals = tmp_path / "journals.sqlite3"
    with sqlite3.connect(journals) as connection:
        connection.execute("CREATE TABLE journal_note(value TEXT)")
        connection.execute("INSERT INTO journal_note VALUES ('must preserve')")
    before = journals.read_bytes()
    instance = PlanningStore(str(tmp_path))
    snapshot = instance.create(plan_dict())
    instance.save(snapshot["project_id"], plan_dict("更新"), expected_revision=1)
    instance.get(snapshot["project_id"])
    instance.list_projects()
    assert journals.read_bytes() == before


def test_first_create_failure_rolls_back_schema_and_cleans_temporary_database(store, monkeypatch):
    def fail_insert(connection, snapshot, serialized):
        assert connection.in_transaction
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        raise sqlite3.IntegrityError("PRIVATE_SECRET insert failure")

    monkeypatch.setattr(PlanningStore, "_insert_snapshot", staticmethod(fail_insert))
    error = assert_error("STORAGE_ERROR", store.create, plan_dict())
    assert "PRIVATE_SECRET" not in str(error)
    assert not store.path.exists()
    assert list(store.directory.iterdir()) == []
    assert store.list_projects()["total"] == 0


def test_first_create_failed_schema_initialization_is_atomic(store, monkeypatch):
    def fail_initialize(connection):
        connection.execute("CREATE TABLE partial_schema(value TEXT)")
        connection.execute("PRAGMA user_version=1")
        raise sqlite3.OperationalError("PRIVATE_SECRET schema failure")

    monkeypatch.setattr(PlanningStore, "_initialize", staticmethod(fail_initialize))
    assert_error("STORAGE_ERROR", store.create, plan_dict())
    assert not store.path.exists()
    assert list(store.directory.iterdir()) == []


def test_create_failure_on_preexisting_empty_database_preserves_schema_zero(tmp_path, monkeypatch):
    instance = PlanningStore(str(tmp_path))
    with sqlite3.connect(instance.path):
        pass
    before = instance.path.read_bytes()

    def fail_insert(connection, snapshot, serialized):
        raise sqlite3.IntegrityError("insert failure")

    monkeypatch.setattr(PlanningStore, "_insert_snapshot", staticmethod(fail_insert))
    assert_error("STORAGE_ERROR", instance.create, plan_dict())
    assert instance.path.read_bytes() == before
    with sqlite3.connect(instance.path.as_uri() + "?mode=ro", uri=True) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == []


def test_snapshot_and_latest_revision_rollback_in_same_transaction(store, monkeypatch):
    first = store.create(plan_dict("初始"))
    original_insert = PlanningStore._insert_snapshot
    before = store.path.read_bytes()

    def fail_after_partial_write(connection, snapshot, serialized):
        original_insert(connection, snapshot, serialized)
        connection.execute(
            "UPDATE projects SET latest_revision=?, title=? WHERE project_id=?",
            (snapshot["revision"], "半完成变更", snapshot["project_id"]),
        )
        raise sqlite3.OperationalError("PRIVATE_SECRET failure after snapshot")

    monkeypatch.setattr(PlanningStore, "_insert_snapshot", staticmethod(fail_after_partial_write))
    error = assert_error("STORAGE_ERROR", store.save, first["project_id"], plan_dict("新版本"), expected_revision=1)
    assert "PRIVATE_SECRET" not in str(error)
    assert store.path.read_bytes() == before
    assert store.get(first["project_id"]) == first
    assert store.list_projects()["projects"][0]["title"] == "初始"
    assert_error("REVISION_NOT_FOUND", store.get, first["project_id"], revision=2)


def test_second_project_failed_create_rolls_back_project_and_snapshot(store, monkeypatch):
    first = store.create(plan_dict("已有项目"))
    before = store.path.read_bytes()

    def fail_insert(connection, snapshot, serialized):
        raise sqlite3.IntegrityError("snapshot failure")

    monkeypatch.setattr(PlanningStore, "_insert_snapshot", staticmethod(fail_insert))
    assert_error("STORAGE_ERROR", store.create, plan_dict("新增失败"))
    assert store.path.read_bytes() == before
    assert store.list_projects()["total"] == 1
    assert store.get(first["project_id"]) == first


def test_commit_failure_never_publishes_initial_database(store, monkeypatch):
    real_connect = store_module.sqlite3.connect

    class FailingCommit(sqlite3.Connection):
        def commit(self):
            raise sqlite3.OperationalError("PRIVATE_SECRET commit failure")

    def connect(*args, **kwargs):
        return real_connect(*args, **kwargs, factory=FailingCommit)

    monkeypatch.setattr(store_module.sqlite3, "connect", connect)
    error = assert_error("STORAGE_ERROR", store.create, plan_dict())
    assert "PRIVATE_SECRET" not in str(error)
    assert not store.path.exists()
    assert list(store.directory.iterdir()) == []


def test_sqlite_connection_failure_is_wrapped_without_paths_or_secrets(store, monkeypatch):
    snapshot = store.create(plan_dict())

    def fail_connect(*args, **kwargs):
        raise sqlite3.OperationalError("PRIVATE_SECRET " + str(store.path))

    monkeypatch.setattr(store_module.sqlite3, "connect", fail_connect)
    for method, args, kwargs in [
        (store.create, (plan_dict(),), {}),
        (store.get, (snapshot["project_id"],), {}),
        (store.save, (snapshot["project_id"], plan_dict()), {"expected_revision": 1}),
        (store.list_projects, (), {}),
    ]:
        error = assert_error("STORAGE_ERROR", method, *args, **kwargs)
        assert "PRIVATE_SECRET" not in str(error)
        assert str(store.path) not in str(error)


def test_publication_failure_is_safe_and_cleans_staged_file(store, monkeypatch):
    def fail_publish(*args, **kwargs):
        raise PermissionError("PRIVATE_SECRET " + str(store.path))

    monkeypatch.setattr(store_module.os, "link", fail_publish)
    error = assert_error("STORAGE_ERROR", store.create, plan_dict())
    assert "PRIVATE_SECRET" not in str(error)
    assert not store.path.exists()
    assert list(store.directory.iterdir()) == []


def test_validation_failure_happens_before_connect_or_mkdir(store, monkeypatch):
    def fail_connect(*args, **kwargs):
        pytest.fail("输入校验失败时不得触碰数据库")

    monkeypatch.setattr(store_module.sqlite3, "connect", fail_connect)
    invalid = plan_dict()
    invalid["profile"]["title"] = " "
    with pytest.raises(ValidationError):
        store.create(invalid)
    with pytest.raises(ValidationError):
        store.save("absent", invalid, expected_revision=1)
    assert not store.directory.exists()


def test_invalid_plan_save_and_create_do_not_modify_existing_database(store):
    first = store.create(plan_dict())
    invalid = plan_dict()
    invalid["tasks"][0]["status"] = "done"
    before = store.path.read_bytes()
    with pytest.raises(ValidationError):
        store.create(invalid)
    with pytest.raises(ValidationError):
        store.save(first["project_id"], invalid, expected_revision=1)
    assert store.path.read_bytes() == before
    assert store.get(first["project_id"]) == first


@pytest.mark.parametrize("note", [None, True, 1, "", " \n\t ", "a" * 1001])
def test_change_note_validation_is_bounded_nonempty_and_inert(store, note):
    assert_error("INVALID_CHANGE_NOTE", store.create, plan_dict(), change_note=note)
    assert_error("INVALID_CHANGE_NOTE", store.save, "absent", plan_dict(), expected_revision=1, change_note=note)
    assert not store.directory.exists()


def test_change_note_at_limit_and_strip(store):
    first = store.create(plan_dict(), change_note=" " + "a" * 1000 + " ")
    assert first["change_note"] == "a" * 1000
    second = store.save(first["project_id"], plan_dict(), expected_revision=1, change_note=" " + "b" * 1000 + " ")
    assert second["change_note"] == "b" * 1000
    assert store.get(first["project_id"]) == second


@pytest.mark.parametrize("revision", [True, False, 0, -1, 1.0, "1", 1 << 63, float("nan")])
def test_revision_inputs_reject_bool_noninteger_and_out_of_bounds(store, revision):
    assert_error("INVALID_REVISION", store.get, "absent", revision=revision)
    assert_error("INVALID_REVISION", store.save, "absent", plan_dict(), expected_revision=revision)
    assert not store.directory.exists()


def test_expected_revision_is_required_and_none_rejected(store):
    with pytest.raises(TypeError):
        store.save("absent", plan_dict())
    assert_error("INVALID_REVISION", store.save, "absent", plan_dict(), expected_revision=None)
    assert not store.directory.exists()


@pytest.mark.parametrize("identifier", [None, True, 1, "", "  ", "a" * 129])
def test_project_id_validation_has_safe_errors(store, identifier):
    assert_error("INVALID_PROJECT_ID", store.get, identifier)
    assert_error("INVALID_PROJECT_ID", store.save, identifier, plan_dict(), expected_revision=1)
    assert not store.directory.exists()


@pytest.mark.parametrize("kwargs", [
    {"limit": True}, {"limit": False}, {"limit": 0}, {"limit": -1}, {"limit": 101},
    {"limit": 1.0}, {"limit": "20"}, {"offset": True}, {"offset": False},
    {"offset": -1}, {"offset": 1.0}, {"offset": "0"}, {"offset": 1 << 63},
])
def test_pagination_validation_is_inert(store, kwargs):
    assert_error("INVALID_PAGINATION", store.list_projects, **kwargs)
    assert not store.directory.exists()


def test_pagination_is_sorted_by_updated_at_then_project_id(store, monkeypatch):
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-09-30T00:00:00+00:00")
    initial = [store.create(plan_dict(f"项目{index}", f"目标{index}")) for index in range(5)]
    expected_ids = sorted(snapshot["project_id"] for snapshot in initial)
    page = store.list_projects(limit=2, offset=1)
    assert page["total"] == 5
    assert page["limit"] == 2 and page["offset"] == 1
    assert [project["project_id"] for project in page["projects"]] == expected_ids[1:3]
    assert set(page["projects"][0]) == {"project_id", "title", "goal", "revision", "created_at", "updated_at"}
    assert store.list_projects(limit=100)["total"] == 5
    assert store.list_projects(offset=5)["projects"] == []
    monkeypatch.setattr(store_module, "utc_now", lambda: "2026-09-30T01:00:00+00:00")
    chosen = initial[0]
    store.save(chosen["project_id"], plan_dict("最近更新", "新目标"), expected_revision=1)
    latest = store.list_projects(limit=1)["projects"][0]
    assert latest["project_id"] == chosen["project_id"]
    assert latest["title"] == "最近更新" and latest["goal"] == "新目标" and latest["revision"] == 2
    assert latest["created_at"] == chosen["created_at"]


def test_sql_parameterization_and_missing_ids_do_not_execute_user_sql(store):
    snapshot = store.create(plan_dict("标题'; DROP TABLE projects; --", "含SQL引号的目标"))
    before = store.path.read_bytes()
    malicious_id = "' OR 1=1; DROP TABLE snapshots; --"
    assert_error("PROJECT_NOT_FOUND", store.get, malicious_id)
    assert_error("PROJECT_NOT_FOUND", store.save, malicious_id, plan_dict(), expected_revision=1)
    assert store.path.read_bytes() == before
    assert store.get(snapshot["project_id"]) == snapshot
    assert store.list_projects()["total"] == 1


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_json_nonfinite_input_is_rejected_before_writes(store, number):
    data = plan_dict()
    data["profile"]["start_date"] = number
    with pytest.raises(ValidationError):
        store.create(data)
    assert not store.directory.exists()


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_json_serializer_itself_rejects_nonfinite_numbers(store, monkeypatch, number):
    fake = SimpleNamespace(model_dump=lambda **kwargs: {"profile": {"title": "标题", "goal": "目标"}, "number": number})
    monkeypatch.setattr(store_module.ResearchPlan, "model_validate", lambda value: fake)
    assert_error("INVALID_PLAN_JSON", store.create, {})
    assert not store.directory.exists()


def test_maximum_payload_uses_utf8_bytes_not_character_count(store, monkeypatch):
    data = plan_dict("中文标题")
    serialized = json.dumps(
        ResearchPlan.model_validate(data).model_dump(mode="json"),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    exact_bytes = len(serialized.encode("utf-8"))
    assert exact_bytes > len(serialized)
    monkeypatch.setattr(store_module, "MAX_PLAN_BYTES", exact_bytes)
    snapshot = store.create(data)
    assert store.get(snapshot["project_id"]) == snapshot
    before = store.path.read_bytes()
    monkeypatch.setattr(store_module, "MAX_PLAN_BYTES", exact_bytes - 1)
    assert_error("PLAN_TOO_LARGE", store.create, data)
    assert_error("PLAN_TOO_LARGE", store.save, snapshot["project_id"], data, expected_revision=1)
    assert store.path.read_bytes() == before


def test_real_two_mib_limit_rejects_valid_large_plan_before_directory_creation(store):
    data = {
        "profile": {"title": "标题", "goal": "目标"},
        "questions": [{"id": f"q{index}", "question": "研" * 4000} for index in range(200)],
    }
    ResearchPlan.model_validate(data)
    assert_error("PLAN_TOO_LARGE", store.create, data)
    assert not store.directory.exists()


@pytest.mark.parametrize("bad_json", [
    '{"profile":{"title":"标题","goal":"目标"},"x":NaN}',
    '{"profile":{"title":"标题","goal":"目标"},"x":Infinity}',
    '{"profile":{"title":"标题","goal":"目标"},"x":-Infinity}',
    '["PRIVATE_SECRET"]', '{"profile":{"title":"PRIVATE_SECRET"}}', '{PRIVATE_SECRET',
])
def test_corrupt_or_nonfinite_saved_json_has_safe_errors(store, bad_json):
    snapshot = store.create(plan_dict())
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE snapshots SET plan_json=? WHERE project_id=? AND revision=?",
            (bad_json, snapshot["project_id"], 1),
        )
    before = store.path.read_bytes()
    error = assert_error("INVALID_PLAN_JSON", store.get, snapshot["project_id"])
    assert "PRIVATE_SECRET" not in str(error)
    assert store.path.read_bytes() == before


def test_oversized_saved_json_is_not_returned(store, monkeypatch):
    snapshot = store.create(plan_dict())
    monkeypatch.setattr(store_module, "MAX_PLAN_BYTES", 1)
    assert_error("INVALID_PLAN_JSON", store.get, snapshot["project_id"])


def test_credentials_validation_has_no_secret_and_no_file_output(store):
    data = plan_dict()
    data["evidence"][0]["source_ref"] = "https://user:PRIVATE_SECRET@example.com/file"
    with pytest.raises(ValidationError) as error:
        store.create(data)
    assert "PRIVATE_SECRET" not in str(error.value)
    assert not store.directory.exists()


def test_done_task_artifact_is_not_opened_and_does_not_change_experiment_or_question(store):
    data = plan_dict()
    data["tasks"][0].update(status="done", experiment_id="exp1", artifact_refs=["absent/imaginary-result.csv"])
    snapshot = store.create(data)
    plan = store.get(snapshot["project_id"])["plan"]
    assert plan["tasks"][0]["status"] == "done"
    assert plan["experiments"][0]["status"] == "planned"
    assert plan["questions"][0]["status"] == "proposed"
    assert not (store.directory / "absent").exists()


def test_snapshot_duplicate_uuid_failure_cannot_overwrite_project(store, monkeypatch):
    monkeypatch.setattr(store_module, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    first = store.create(plan_dict("原项目"))
    before = store.path.read_bytes()
    assert_error("STORAGE_ERROR", store.create, plan_dict("不能覆盖"))
    assert store.path.read_bytes() == before
    assert store.get(first["project_id"]) == first
    assert store.list_projects()["total"] == 1
