"""GUI/native execution leases, using temporary SQLite stores and fake models only."""
from __future__ import annotations

import copy
import json
import threading
from types import SimpleNamespace

import pytest

from paperflow.engine.journals.models import JournalError, response
from paperflow.gui import paper_reading_loop as gui
from paperflow.gui.secrets import SecretStore


class FakeExecutionServices:
    def __init__(self):
        self.owners = {}
        self.calls = []
        self.lock = threading.RLock()
        self.fail_ensure = False

    def _record(self, method, project_id, owner):
        # A registry/SQLite inversion must not sneak into startup or cleanup.
        assert not gui._WORKER_LOCK._is_owned()
        self.calls.append((method, project_id, owner))

    def acquire_execution(self, project_id, owner):
        with self.lock:
            self._record("acquire", project_id, owner)
            if self.owners.get(project_id) not in (None, owner):
                raise JournalError("EXECUTION_BUSY", "busy")
            self.owners[project_id] = owner
            return {"project_id": project_id, "owner": owner}

    def ensure_execution(self, project_id, owner):
        with self.lock:
            self._record("ensure", project_id, owner)
            if self.fail_ensure or self.owners.get(project_id) != owner:
                raise JournalError("EXECUTION_LOST", "lost")

    def release_execution(self, project_id, owner):
        with self.lock:
            self._record("release", project_id, owner)
            if self.owners.get(project_id) == owner:
                self.owners.pop(project_id)


class FakeLoop:
    def __init__(self):
        self.project_id = "lease-test-project"
        self.loop_revision = 0
        self.project_revision = 1
        self.status = "idle"
        self.next_action = None
        self.effects = []
        self.usage = 0
        self.reserved = 0
        self.fail_control = False
        self.fail_step = False
        self.step_hook = None
        self.get_hook = None

    def get(self, project_id):
        if self.get_hook:
            self.get_hook()
        assert project_id == self.project_id
        return response({
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "loop_revision": self.loop_revision,
            "project": {"project_id": self.project_id, "revision": self.project_revision,
                        "profile": {"research_question": "A test question"}, "draft": {}},
            "loop": {"status": self.status, "stop_reason": None,
                     "next_action": copy.deepcopy(self.next_action)},
        })

    def control(self, project_id, action, *revisions, **kwargs):
        self.effects.append(("control", action))
        if self.fail_control:
            raise JournalError("CONTROL_FAILED", "control failed")
        self.loop_revision += 1
        if action in ("start", "resume"):
            if not self.next_action:
                self.next_action = {"action_id": "review-1", "kind": "review"}
            kind = self.next_action["kind"]
            self.status = "ready" if kind in ("search", "download", "read") else f"awaiting_{kind}"
        else:
            self.status = {"pause": "paused", "stop": "stopped"}[action]
        return self.get(project_id)

    def prepare_review(self, project_id):
        result = self.get(project_id)
        result["data"].update(context_fingerprint="fingerprint", review_schema={}, evidence_matrix=[])
        return result

    def reserve_model_call(self, project_id, action_id, *revisions):
        self.effects.append(("reserve", action_id))
        self.reserved += 1
        self.loop_revision += 1
        return response({"model_ticket": "ticket-1", "loop_revision": self.loop_revision})

    def settle_model_call(self, project_id, action_id, ticket, success):
        self.effects.append(("settle", success))
        assert self.reserved == 1
        self.reserved -= 1
        self.usage += 1
        self.loop_revision += 1
        return response({"settled": True, "loop_revision": self.loop_revision})

    def submit_review(self, project_id, review, fingerprint, *revisions, **kwargs):
        self.effects.append(("submit", review))
        self.status = "stopped"
        self.loop_revision += 1
        return self.get(project_id)

    def apply_feedback(self, project_id, action_id, feedback, *revisions, **kwargs):
        self.effects.append(("feedback", feedback))
        self.status = "stopped"
        self.loop_revision += 1
        return self.get(project_id)

    def step(self, project_id, action_id, *revisions):
        self.effects.append(("step", action_id))
        if self.step_hook:
            self.step_hook()
        if self.fail_step:
            raise JournalError("STEP_FAILED", "step failed")
        self.status = "stopped"
        self.loop_revision += 1
        return self.get(project_id)


def make_real_services(directory, core):
    from paperflow.application.services import ApplicationServices
    # Inject all literature/finder dependencies: no download, real model or Word/COM.
    return ApplicationServices(data_dir=directory, loop=core, writing=core,
                               literature=SimpleNamespace(get=core.get),
                               finder=SimpleNamespace(store=SimpleNamespace(directory=directory)))


@pytest.fixture(params=["fake", "sqlite"])
def env(request, tmp_path, monkeypatch):
    core = FakeLoop()
    leases = (FakeExecutionServices() if request.param == "fake"
              else make_real_services(tmp_path, core))
    store = SecretStore()
    store.configure("openai_compatible", "https://model.invalid/v1", "fake-model",
                    "lease-test-key", remember=False)
    store.consent(True)
    chats = []

    def chat(*args):
        chats.append(args)
        return json.dumps({"decision": "stop"})

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    workers = []
    original_start = gui.LoopWorker.start

    def record_start(worker):
        workers.append(worker)
        original_start(worker)

    monkeypatch.setattr(gui.LoopWorker, "start", record_start)
    yield SimpleNamespace(core=core, leases=leases, store=store, chats=chats, workers=workers)
    for worker in workers:
        worker.request_stop()
        if worker.thread and worker.thread.ident:
            worker.thread.join(3)
            assert not worker.thread.is_alive()


def start(env, function=gui.start_auto_loop):
    return function(env.core, env.store, env.core.project_id, env.core.loop_revision,
                    env.core.project_revision, consent=True, execution_services=env.leases)


def ready(env, kind="review"):
    env.core.status = "ready" if kind in ("search", "download", "read") else f"awaiting_{kind}"
    material = {}
    if kind == "assessment":
        material["candidates"] = [{"paper_id": "paper-1", "metadata_sha256": "sha-1", "paper": {}}]
    env.core.next_action = {"action_id": "action-1", "kind": kind, "material": material}


def assert_busy(leases, project_id, owner="agent:test-native"):
    with pytest.raises(JournalError) as exc:
        leases.acquire_execution(project_id, owner)
    assert exc.value.code == "EXECUTION_BUSY"


def assert_released(env):
    owner = "agent:after-gui"
    env.leases.acquire_execution(env.core.project_id, owner)
    env.leases.release_execution(env.core.project_id, owner)


def join_worker(env):
    worker = env.workers[-1]
    worker.thread.join(3)
    assert not worker.thread.is_alive()
    assert not worker.is_running
    assert gui.get_loop_worker(env.core.project_id) is not worker
    return worker


@pytest.mark.parametrize("function", [gui.start_auto_loop, gui.resume_loop, gui.single_step])
def test_native_owner_rejects_gui_before_any_effect(env, function):
    ready(env)
    before = env.core.get(env.core.project_id)
    env.leases.acquire_execution(env.core.project_id, "agent:existing")
    try:
        with pytest.raises(JournalError) as exc:
            start(env, function)
        assert exc.value.code == "EXECUTION_BUSY"
        assert env.core.get(env.core.project_id) == before
        assert env.core.effects == []
        assert env.chats == []
        assert not env.workers
        assert_busy(env.leases, env.core.project_id)
    finally:
        env.leases.release_execution(env.core.project_id, "agent:existing")


@pytest.mark.parametrize("function", [gui.start_auto_loop, gui.single_step])
def test_gui_holds_lease_through_model_settlement_and_apply(env, monkeypatch, function):
    ready(env)

    def chat(*args):
        assert_busy(env.leases, env.core.project_id)
        env.chats.append(args)
        return '{"decision":"stop"}'

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    for method in ("settle_model_call", "submit_review"):
        original = getattr(env.core, method)

        def guarded(*args, _original=original, **kwargs):
            assert_busy(env.leases, env.core.project_id)
            return _original(*args, **kwargs)

        monkeypatch.setattr(env.core, method, guarded)
    start(env, function)
    if function is gui.start_auto_loop:
        assert join_worker(env).last_error is None
    assert len(env.chats) == env.core.usage == 1
    assert env.core.reserved == 0
    assert any(effect[0] == "submit" for effect in env.core.effects)
    assert_released(env)
    if isinstance(env.leases, FakeExecutionServices):
        gui_calls = [c for c in env.leases.calls if c[2].startswith("gui:")]
        assert len([c for c in gui_calls if c[0] == "acquire"]) == 1
        assert len([c for c in gui_calls if c[0] == "release"]) == 1
        assert len([c for c in gui_calls if c[0] == "ensure"]) >= 4
        owner = gui_calls[0][2]
        assert len(owner) == 36 and len({c[2] for c in gui_calls}) == 1


@pytest.mark.parametrize("kind", ["search", "download", "read"])
def test_single_step_holds_lease_during_actual_step(env, kind):
    ready(env, kind)
    env.core.step_hook = lambda: assert_busy(env.leases, env.core.project_id)
    start(env, gui.single_step)
    assert env.core.effects == [("step", "action-1")]
    assert env.chats == []
    assert_released(env)


@pytest.mark.parametrize("cancel", ["pause", "stop", "shutdown"])
def test_cancelled_worker_settles_late_call_before_release(env, monkeypatch, cancel):
    entered, finish = threading.Event(), threading.Event()

    def chat(*args):
        env.chats.append(args)
        entered.set()
        assert finish.wait(3)
        return '{"decision":"stop"}'

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    start(env)
    try:
        assert entered.wait(3)
        assert_busy(env.leases, env.core.project_id)
        if cancel == "shutdown":
            gui.shutdown_all()
        else:
            getattr(gui, f"{cancel}_loop")(env.core, env.core.project_id)
        # A cancellation cannot release while the in-flight request is unsettled.
        assert_busy(env.leases, env.core.project_id)
    finally:
        finish.set()
    join_worker(env)
    assert env.core.usage == 1 and env.core.reserved == 0
    assert not any(effect[0] in ("submit", "feedback") for effect in env.core.effects)
    assert_released(env)


@pytest.mark.parametrize("function", [gui.start_auto_loop, gui.single_step])
def test_model_exception_settles_and_releases(env, monkeypatch, function):
    ready(env)

    def chat(*args):
        assert_busy(env.leases, env.core.project_id)
        raise RuntimeError("fake model failed")

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    if function is gui.single_step:
        with pytest.raises(RuntimeError, match="fake model failed"):
            start(env, function)
    else:
        start(env, function)
        assert "fake model failed" in join_worker(env).last_error
    assert env.core.usage == 1 and env.core.reserved == 0
    assert not any(effect[0] in ("submit", "feedback") for effect in env.core.effects)
    assert_released(env)


@pytest.mark.parametrize("mutation", ["consent", "config"])
@pytest.mark.parametrize("function", [gui.start_auto_loop, gui.single_step])
@pytest.mark.parametrize("kind", ["review", "assessment", "interpretation", "revision"])
def test_changed_authorization_discards_late_result_without_refund(env, monkeypatch, mutation, function, kind):
    ready(env, kind)

    def chat(*args):
        if mutation == "consent":
            env.store.consent(False)
        else:
            env.store.config.model = "changed-model"
        return '{"decision":"stop"}'

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    if function is gui.single_step:
        with pytest.raises(JournalError) as exc:
            start(env, function)
        assert exc.value.code == ("CONSENT_REQUIRED" if mutation == "consent" else "CONFIG_CHANGED")
    else:
        start(env, function)
        assert join_worker(env).last_error
    assert env.core.usage == 1 and env.core.reserved == 0
    assert not any(effect[0] in ("submit", "feedback") for effect in env.core.effects)
    assert_released(env)


@pytest.mark.parametrize("failure", ["control", "constructor", "thread"])
def test_initialization_failures_release_and_remove_worker(env, monkeypatch, failure):
    if failure == "control":
        env.core.fail_control = True
    elif failure == "constructor":
        def fail_init(*args, **kwargs):
            raise RuntimeError("constructor failed")
        monkeypatch.setattr(gui.LoopWorker, "__init__", fail_init)
    else:
        def fail_start(*args, **kwargs):
            raise RuntimeError("thread failed")
        monkeypatch.setattr(threading.Thread, "start", fail_start)
    with pytest.raises((JournalError, RuntimeError)):
        start(env)
    assert gui.get_loop_worker(env.core.project_id) is None
    assert env.chats == []
    assert_released(env)


def test_single_step_exception_releases(env):
    ready(env, "download")
    env.core.fail_step = True
    with pytest.raises(JournalError) as exc:
        start(env, gui.single_step)
    assert exc.value.code == "STEP_FAILED"
    assert_released(env)


def test_lease_lost_does_not_pause_another_executor(tmp_path, monkeypatch):
    core = FakeLoop()
    leases = FakeExecutionServices()
    store = SecretStore()
    store.configure("openai_compatible", "https://model.invalid/v1", "fake", "key", remember=False)
    store.consent(True)
    owner = "gui:test"
    leases.acquire_execution(core.project_id, owner)
    worker = gui.LoopWorker(core, store, core.project_id, execution_services=leases, owner=owner)
    core.status = "awaiting_review"
    leases.owners[core.project_id] = "agent:new-owner"
    monkeypatch.setattr(gui.model_scoring, "_chat", lambda *args: pytest.fail("model must not run"))
    worker._run()
    assert worker.last_error
    assert core.effects == []
    assert leases.owners[core.project_id] == "agent:new-owner"


def test_sqlite_same_directory_instances_exclude_both_directions(tmp_path, monkeypatch):
    core = FakeLoop()
    first = make_real_services(tmp_path, core)
    second = make_real_services(tmp_path, core)
    first.acquire_execution(core.project_id, "agent:first-instance")
    assert_busy(second, core.project_id, "gui:other-instance")
    first.release_execution(core.project_id, "agent:first-instance")
    store = SecretStore()
    store.configure("openai_compatible", "https://model.invalid/v1", "fake", "key", remember=False)
    store.consent(True)
    core.status = "awaiting_review"
    core.next_action = {"action_id": "review-1", "kind": "review"}

    def chat(*args):
        assert_busy(second, core.project_id, "agent:second-instance")
        return '{"decision":"stop"}'

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    gui.single_step(core, store, core.project_id, 0, 1, consent=True, execution_services=first)
    second.acquire_execution(core.project_id, "agent:second-instance")
    second.release_execution(core.project_id, "agent:second-instance")


def test_single_step_renews_immediately_before_step(tmp_path):
    core = FakeLoop()
    leases = FakeExecutionServices()
    store = SecretStore()
    core.status = "ready"
    core.next_action = {"action_id": "read-1", "kind": "read"}
    gets = 0

    def expire_before_step():
        nonlocal gets
        gets += 1
        if gets == 3:
            leases.fail_ensure = True

    core.get_hook = expire_before_step
    with pytest.raises(JournalError) as exc:
        gui.single_step(core, store, core.project_id, 0, 1, execution_services=leases)
    assert exc.value.code == "EXECUTION_LOST"
    assert core.effects == []
    assert leases.owners == {}


@pytest.mark.parametrize("function", [gui.start_auto_loop, gui.single_step])
def test_lost_lease_after_actual_model_call_is_settled_not_applied(env, monkeypatch, function):
    ready(env)
    holder = "agent:replacement"

    def chat(*args):
        if isinstance(env.leases, FakeExecutionServices):
            owner = env.leases.owners[env.core.project_id]
        else:
            with env.leases.workspaces.connect() as db:
                row = db.execute("SELECT owner FROM execution_leases WHERE project_id=?",
                                 (env.core.project_id,)).fetchone()
                owner = row["owner"]
        env.leases.release_execution(env.core.project_id, owner)
        env.leases.acquire_execution(env.core.project_id, holder)
        return '{"decision":"stop"}'

    monkeypatch.setattr(gui.model_scoring, "_chat", chat)
    try:
        if function is gui.single_step:
            with pytest.raises(JournalError) as exc:
                start(env, function)
            assert exc.value.code == "EXECUTION_LOST"
        else:
            start(env, function)
            assert join_worker(env).last_error
        assert env.core.usage == 1 and env.core.reserved == 0
        assert not any(effect[0] in ("submit", "feedback") for effect in env.core.effects)
        assert not any(effect == ("control", "pause") for effect in env.core.effects)
        assert_busy(env.leases, env.core.project_id)
    finally:
        env.leases.release_execution(env.core.project_id, holder)


def test_single_step_rechecks_cas_after_acquiring(env, monkeypatch):
    ready(env)
    original_acquire = env.leases.acquire_execution

    def commit_during_acquire(project_id, owner):
        result = original_acquire(project_id, owner)
        env.core.loop_revision += 1
        return result

    monkeypatch.setattr(env.leases, "acquire_execution", commit_during_acquire)
    with pytest.raises(JournalError) as exc:
        start(env, gui.single_step)
    assert exc.value.code == "REVISION_CONFLICT"
    assert env.chats == [] and env.core.effects == []
    monkeypatch.setattr(env.leases, "acquire_execution", original_acquire)
    assert_released(env)


@pytest.mark.parametrize("function", [gui.start_auto_loop, gui.single_step])
def test_lease_key_comes_from_verified_service_state(env, monkeypatch, function):
    ready(env)
    original_get = env.core.get

    def aliased_get(project_id):
        return original_get(env.core.project_id)

    monkeypatch.setattr(env.core, "get", aliased_get)
    env.leases.acquire_execution(env.core.project_id, "agent:canonical")
    try:
        with pytest.raises(JournalError) as exc:
            function(env.core, env.store, "untrusted-alias", 0, 1,
                     consent=True, execution_services=env.leases)
        assert exc.value.code == "EXECUTION_BUSY"
        assert env.chats == [] and env.core.effects == []
    finally:
        env.leases.release_execution(env.core.project_id, "agent:canonical")


def test_fake_error_envelope_cannot_control_loop(env, monkeypatch):
    monkeypatch.setattr(env.leases, "acquire_execution", lambda *args: {
        "status": "error", "error_code": "EXECUTION_BUSY", "message": "busy"})
    with pytest.raises(JournalError) as exc:
        start(env)
    assert exc.value.code == "EXECUTION_BUSY"
    assert env.chats == [] and env.core.effects == []
