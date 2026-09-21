"""Regression checks for the persistent Codex app-server phase hotfix."""

from __future__ import annotations

import base64
import asyncio
import hashlib
import importlib.util
import json
import math
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def load_plugin_module():
    # Exercise the checkout under test.  The /opt/data fallback keeps this
    # script usable when it is copied into a minimal container by itself.
    path = Path(__file__).with_name("__init__.py")
    if not path.exists():
        path = Path("/opt/data/plugins/codex-app-server-phase-hotfix/__init__.py")
    spec = importlib.util.spec_from_file_location(
        "codex_app_server_phase_hotfix_test",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = spec.name
    module.__path__ = [str(path.parent)]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = load_plugin_module()


def completed(text: str, phase=None):
    item = {"type": "agentMessage", "id": text, "text": text}
    if phase is not None:
        item["phase"] = phase
    return {"method": "item/completed", "params": {"item": item}}


def main():
    # Soft Agent eviction must release an idle Codex app-server writer, while
    # leaving a genuinely active turn untouched.
    lifecycle_calls = []

    class LifecycleSession:
        def __init__(self, active_turn_id=None):
            self._active_turn_id = active_turn_id
            self._active_turn_lock = threading.Lock()
            self.closed = False

        def close(self):
            self.closed = True

    wrapped_release = mod.lifecycle.wrap_release_clients(
        lambda agent: lifecycle_calls.append(agent)
    )
    idle_agent = SimpleNamespace(_codex_session=LifecycleSession())
    wrapped_release(idle_agent)
    assert idle_agent._codex_session is None
    assert lifecycle_calls == [idle_agent]

    active_session = LifecycleSession("turn-live")
    active_agent = SimpleNamespace(_codex_session=active_session)
    wrapped_release(active_agent)
    assert active_agent._codex_session is active_session
    assert active_session.closed is False

    # Session projects: a channel routing key owns one project named exactly
    # after its session_key. /new or /reset produces a new session/thread in
    # the same project; process restart with the same id resumes the thread.
    old_session_project_env = {
        key: os.environ.get(key)
        for key in (
            "HERMES_HOME",
            mod.session_project.ROOT_ENV,
            mod.session_project.ALIASES_ENV,
            mod.session_project.ALLOWED_ROOTS_ENV,
            mod.session_project.ADMIN_USERS_ENV,
            mod.session_project.BACKFILL_ENV,
            mod.session_project.REGISTER_APP_ENV,
            mod.session_project.REGISTER_APP_CLI_ENV,
            mod.session_project.REGISTER_APP_TIMEOUT_ENV,
            mod.session_project.REGISTER_APP_RETRY_ENV,
            "HERMES_SESSION_KEY",
            "HERMES_SESSION_ID",
            "HERMES_SESSION_USER_ID",
        )
    }
    with tempfile.TemporaryDirectory() as session_tmp:
        os.environ["HERMES_HOME"] = session_tmp
        os.environ[mod.session_project.REGISTER_APP_ENV] = "false"
        backfill_key = "agent:main:qqbot:group:old-route"
        backfill_session = "20260701_010203_backfill"
        sessions_dir = Path(session_tmp, "sessions")
        sessions_dir.mkdir()
        Path(sessions_dir, "sessions.json").write_text(
            json.dumps(
                {
                    "_README": "ignored metadata",
                    backfill_key: {
                        "session_key": backfill_key,
                        "session_id": backfill_session,
                    },
                }
            ),
            encoding="utf-8",
        )
        backfill_status = mod.session_project.backfill_existing_session_projects()
        assert "backfilled=1" in backfill_status
        store = mod.session_project.get_store()
        backfilled = store.get_binding(backfill_key, backfill_session)
        assert backfilled is not None
        assert Path(backfilled.project_path).is_dir()
        assert backfilled.thread_id is None
        key = "agent:main:qqbot:dm:test-user"
        session_a = "20260825_100000_aaaaaaaa"
        session_b = "20260825_110000_bbbbbbbb"
        binding_a = store.ensure_binding(key, session_a)
        assert binding_a.project_name == key
        assert Path(binding_a.project_path).name == mod.session_project._session_key_project_basename(key)
        assert Path(binding_a.project_path, "AGENTS.md").is_file()
        assert Path(binding_a.project_path, "PROJECT_MEMORY.md").is_file()
        manifest = json.loads(
            Path(binding_a.project_path, ".hermes-dispatch.json").read_text()
        )
        assert manifest["project_name"] == key
        assert manifest["schema_version"] == 2
        assert mod.session_project._session_key_project_basename(
            key, platform="win32"
        ) == "agent：main：qqbot：dm：test-user"
        assert mod.session_project._session_key_project_basename(
            key, platform="linux"
        ) == key

        # Existing 1.6.x databases and folders used the first session_id as
        # the project name. Migrate them in place, preserving every thread id
        # while changing the shared project path to the exact session_key.
        legacy_key = "agent:main:whatsapp:dm:legacy-user"
        legacy_session = "20260825_090000_legacy00"
        legacy_thread = "thread-legacy"
        legacy_path = Path(session_tmp, "codex-projects", legacy_session)
        store._scaffold_default_project(legacy_path, legacy_key, legacy_session)
        with store._connect() as conn:
            now = mod.session_project._now()
            conn.execute(
                """
                INSERT INTO channel_projects (
                    session_key, default_project_name, default_project_path,
                    active_project_name, active_project_path, binding_mode,
                    created_from_session_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'auto', ?, ?, ?)
                """,
                (
                    legacy_key,
                    legacy_session,
                    str(legacy_path.resolve()),
                    legacy_session,
                    str(legacy_path.resolve()),
                    legacy_session,
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO session_threads (
                    session_key, session_id, project_path, thread_id,
                    thread_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    legacy_key,
                    legacy_session,
                    str(legacy_path.resolve()),
                    legacy_thread,
                    legacy_session,
                    now,
                    now,
                ),
            )
            conn.commit()
        migrated = store.ensure_binding(legacy_key, legacy_session)
        migrated_path = Path(
            session_tmp,
            "codex-projects",
            mod.session_project._session_key_project_basename(legacy_key),
        ).resolve()
        assert migrated.project_name == legacy_key
        assert Path(migrated.project_path) == migrated_path
        assert migrated.thread_id == legacy_thread
        assert migrated_path.is_dir()
        assert not legacy_path.exists()
        migrated_manifest = json.loads(
            Path(migrated.project_path, ".hermes-dispatch.json").read_text()
        )
        assert migrated_manifest["schema_version"] == 2
        assert migrated_manifest["project_name"] == legacy_key

        store.record_thread(binding_a, "thread-a")
        resumed_a = store.ensure_binding(key, session_a)
        assert resumed_a.thread_id == "thread-a"

        binding_b = store.ensure_binding(key, session_b)
        assert binding_b.project_path == binding_a.project_path
        assert binding_b.thread_id is None
        store.record_thread(binding_b, "thread-b")
        assert [row["session_id"] for row in store.list_threads(key)] == [
            session_b,
            session_a,
        ]

        class FakeClient:
            def __init__(self, resume_payload=None, resume_error=None):
                self.resume_payload = resume_payload
                self.resume_error = resume_error
                self.calls = []

            def initialize(self, **kwargs):
                self.calls.append(("initialize", kwargs))

            def request(self, method, params, timeout):
                self.calls.append((method, params, timeout))
                if method == "thread/resume":
                    if self.resume_error is not None:
                        raise self.resume_error
                    return self.resume_payload or {
                        "thread": {"id": params["threadId"]}
                    }
                if method == "thread/start":
                    return {"thread": {"id": "thread-new"}}
                if method == "thread/name/set":
                    return {}
                raise AssertionError(method)

        class FakeCodexSession:
            def __init__(self, binding, client):
                self._dispatch_session_binding = binding
                self._dispatch_resume_thread_id = binding.thread_id
                self._thread_id = None
                self._client = client
                self._client_factory = None
                self._codex_bin = "codex"
                self._codex_home = None
                self._active_turn_id = None
                self._active_turn_lock = threading.Lock()
                self.closed = False

            def close(self):
                self.closed = True
                self._thread_id = None

        # A stale Agent may still own the persisted thread after Hermes has
        # removed it from the cache. The resume wrapper closes that known idle
        # in-process owner synchronously before asking Codex to resume.
        stale_owner = LifecycleSession()
        mod.session_project._claim_thread_owner("thread-a", stale_owner)

        wrapped_ensure = mod.session_project.wrap_ensure_started(
            lambda self: "upstream-start"
        )
        backfill_client = FakeClient()
        backfill_session_obj = FakeCodexSession(backfilled, backfill_client)
        assert wrapped_ensure(backfill_session_obj) == "thread-new"
        assert any(call[0] == "thread/start" for call in backfill_client.calls)
        assert (
            store.ensure_binding(backfill_key, backfill_session).thread_id
            == "thread-new"
        )

        resume_client = FakeClient()
        resumed_session = FakeCodexSession(resumed_a, resume_client)
        assert wrapped_ensure(resumed_session) == "thread-a"
        assert stale_owner.closed is True
        assert any(call[0] == "thread/resume" for call in resume_client.calls)
        assert not any(call[0] == "thread/start" for call in resume_client.calls)
        name_call = next(
            call for call in resume_client.calls if call[0] == "thread/name/set"
        )
        assert name_call[1]["name"] == session_a

        unsupported_client = FakeClient(
            resume_error=RuntimeError("JSON-RPC method not found: thread/resume")
        )
        unsupported_session = FakeCodexSession(resumed_a, unsupported_client)
        try:
            wrapped_ensure(unsupported_session)
        except RuntimeError as exc:
            assert "method not found" in str(exc)
        else:
            raise AssertionError("unsupported resume must not create a duplicate thread")
        assert not any(
            call[0] == "thread/start" for call in unsupported_client.calls
        )

        missing_client = FakeClient(
            resume_error=RuntimeError("thread not found: thread-a")
        )
        missing_session = FakeCodexSession(resumed_a, missing_client)
        assert wrapped_ensure(missing_session) == "thread-new"
        assert any(call[0] == "thread/start" for call in missing_client.calls)
        assert store.ensure_binding(key, session_a).thread_id == "thread-new"

        start_client = FakeClient()
        new_session = FakeCodexSession(binding_b, start_client)
        # Clear the row recorded above to model the first turn after /new.
        store.clear_thread(binding_b, "test-new-session")
        binding_b = store.ensure_binding(key, session_b)
        new_session._dispatch_session_binding = binding_b
        new_session._dispatch_resume_thread_id = None
        assert wrapped_ensure(new_session) == "thread-new"
        assert any(call[0] == "thread/start" for call in start_client.calls)
        assert store.ensure_binding(key, session_b).thread_id == "thread-new"

        # Desktop visibility uses the Codex CLI's supported, cross-platform
        # `codex app <path>` entrypoint. It is opt-in and recorded after one
        # success so later turns/restarts do not repeatedly focus the app.
        os.environ[mod.session_project.REGISTER_APP_ENV] = "true"
        completed_app = SimpleNamespace(returncode=0)

        class ImmediateThread:
            def __init__(self, *, target, args, **kwargs):
                self.target = target
                self.args = args

            def start(self):
                self.target(*self.args)

        with patch.object(
            mod.session_project.subprocess, "run", return_value=completed_app
        ) as run_app, patch.object(
            mod.session_project.threading, "Thread", ImmediateThread
        ):
            mod.session_project._register_project_with_codex_app_best_effort(
                binding_a, "/opt/codex/bin/codex"
            )
            mod.session_project._register_project_with_codex_app_best_effort(
                binding_a, "/opt/codex/bin/codex"
            )
        assert run_app.call_count == 1
        assert run_app.call_args.args[0] == mod.session_project._codex_app_command(
            "/opt/codex/bin/codex", binding_a.project_path
        )
        assert store.app_registration_status(binding_a.project_path)["status"] == (
            "registered"
        )
        assert mod.session_project._codex_app_command(
            "/opt/codex/bin/codex",
            binding_a.project_path,
            platform="linux",
        ) == ["/opt/codex/bin/codex", "app", binding_a.project_path]
        assert mod.session_project._codex_app_command(
            r"C:\\Codex\\codex.exe",
            binding_a.project_path,
            platform="win32",
        ) == [r"C:\\Codex\\codex.exe", "app", binding_a.project_path]
        assert mod.session_project._codex_app_command(
            "/opt/codex/bin/codex",
            binding_a.project_path,
            platform="darwin",
            user_id=501,
        ) == [
            "/bin/launchctl",
            "asuser",
            "501",
            "/opt/codex/bin/codex",
            "app",
            binding_a.project_path,
        ]
        os.environ[mod.session_project.REGISTER_APP_ENV] = "false"

        # A prompt-callable project bind is admin-gated, resolves only an
        # operator-configured alias/allowed path, and carries the current
        # thread so the next turn resumes it with the new cwd.
        external = Path(session_tmp, "department-finance")
        external.mkdir()
        os.environ[mod.session_project.ALIASES_ENV] = json.dumps(
            {"finance": str(external)}
        )
        os.environ[mod.session_project.ADMIN_USERS_ENV] = "owner-openid"
        os.environ["HERMES_SESSION_KEY"] = key
        os.environ["HERMES_SESSION_ID"] = session_b
        os.environ["HERMES_SESSION_USER_ID"] = "owner-openid"

        class FakeContext:
            def __init__(self):
                self.tools = {}
                self.commands = {}
                self.hooks = {}

            def register_tool(self, **kwargs):
                self.tools[kwargs["name"]] = kwargs["handler"]

            def register_command(self, name, handler, **kwargs):
                self.commands[name] = handler

            def register_hook(self, name, handler):
                self.hooks[name] = handler

        fake_ctx = FakeContext()
        mod.session_project.register_session_project_interfaces(fake_ctx)
        project_tool = fake_ctx.tools["codex_session_project"]
        bound = json.loads(project_tool({"action": "bind", "project": "finance"}))
        assert bound["binding"]["project_path"] == str(external.resolve())
        assert bound["binding"]["thread_id"] == "thread-new"
        assert bound["effective"] == "next Codex turn"
        restored = json.loads(project_tool({"action": "default"}))
        assert restored["binding"]["project_path"] == binding_a.project_path
        assert restored["binding"]["thread_id"] == "thread-new"

        os.environ["HERMES_SESSION_USER_ID"] = "not-owner"
        denied = json.loads(project_tool({"action": "bind", "project": "finance"}))
        assert "restricted" in denied["error"]

        # Hermes dispatches plugin slash commands before it binds the normal
        # Agent session ContextVars. The pre-dispatch hook must resolve the
        # same channel-neutral route for both QQ and WhatsApp rather than
        # relying on process-global environment variables.
        # Reproduce Hermes 0.20.0's pre-Agent slash-command window: the
        # task-local variables are not bound yet and get_session_env() can
        # fall back to process-global values left by the previous Agent.
        os.environ["HERMES_SESSION_KEY"] = "agent:main:qqbot:dm:stale-user"
        os.environ["HERMES_SESSION_ID"] = "stale-before-new"
        os.environ["HERMES_SESSION_USER_ID"] = "stale-owner"

        class FakeGatewayStore:
            def __init__(self, entries):
                self._entries = entries

        class FakeGateway:
            def __init__(self, routes):
                self.routes = routes
                self.session_store = FakeGatewayStore(
                    {
                        route_key: SimpleNamespace(session_id=session_value)
                        for route_key, session_value in routes.values()
                    }
                )

            def _session_key_for_source(self, source):
                return self.routes[source.platform][0]

        routes = {
            "qqbot": ("agent:main:qqbot:dm:qq-user", "qq-session"),
            "whatsapp": (
                "agent:main:whatsapp:dm:wa-user",
                "whatsapp-session",
            ),
        }
        fake_gateway = FakeGateway(routes)
        pre_dispatch = fake_ctx.hooks["pre_gateway_dispatch"]
        for platform, (route_key, route_session_id) in routes.items():
            source = SimpleNamespace(platform=platform, user_id=f"{platform}-owner")
            event = SimpleNamespace(
                source=source,
                get_command=lambda: "codex-project",
            )
            pre_dispatch(
                event=event,
                gateway=fake_gateway,
                session_store=fake_gateway.session_store,
            )
            command_result = json.loads(
                asyncio.run(fake_ctx.commands["codex-project"]("status"))
            )
            assert command_result["binding"]["session_id"] == route_session_id
            assert command_result["binding"]["session_key_sha256"] == hashlib.sha256(
                route_key.encode("utf-8")
            ).hexdigest()

        qq_key, _qq_old_session = routes["qqbot"]
        qq_new_session = "qq-session-after-new"
        fake_gateway.session_store._entries[qq_key] = SimpleNamespace(
            session_id=qq_new_session
        )
        qq_source = SimpleNamespace(platform="qqbot", user_id="qqbot-owner")
        pre_dispatch(
            event=SimpleNamespace(
                source=qq_source,
                get_command=lambda: "codex-project",
            ),
            gateway=fake_gateway,
            session_store=fake_gateway.session_store,
        )
        after_new = json.loads(
            asyncio.run(fake_ctx.commands["codex-project"]("status"))
        )
        assert after_new["binding"]["session_id"] == qq_new_session
        assert after_new["binding"]["project_name"] == qq_key
        assert after_new["binding"]["thread_id"] is None

        # A binding change observed after a completed turn retires only the
        # current Agent's app-server client. Another session remains untouched.
        class LiveSession:
            def __init__(self, binding):
                self._dispatch_session_binding = binding
                self.closed = False

            def close(self):
                self.closed = True

        isolated_other = LiveSession(binding_a)
        switching_agent = SimpleNamespace(
            _gateway_session_key=key,
            session_id=session_b,
            _codex_session=None,
        )

        def switch_during_turn(agent, *args, **kwargs):
            active = store.ensure_binding(key, session_b)
            agent._codex_session = LiveSession(active)
            store.bind_project(key, session_b, "finance", external)
            return {"codex_thread_id": active.thread_id, "completed": True}

        wrapped_runtime = mod.session_project.wrap_codex_runtime_turn(
            switch_during_turn
        )
        result = wrapped_runtime(switching_agent)
        assert result["completed"] is True
        assert switching_agent._codex_session is None
        assert isolated_other.closed is False

    for key, value in old_session_project_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    # Long turns: 0 means no Codex wall-clock deadline. The wrapper keeps all
    # completion and callback state on the individual session/call.
    assert math.isinf(mod.long_turn.configured_turn_timeout("0"))
    assert math.isinf(mod.long_turn.configured_turn_timeout("invalid"))
    assert mod.long_turn.configured_turn_timeout("7200") == 7200

    class TurnResult:
        def __init__(self, text):
            self.final_text = text
            self.turn_id = f"turn-{text}"
            self.interrupted = False
            self.error = None
            self.should_retire = False

    concurrent_barrier = threading.Barrier(2)

    def isolated_original(
        session,
        user_input,
        *,
        turn_timeout,
        notification_poll_timeout,
        post_tool_quiet_timeout,
    ):
        session.received_timeout = turn_timeout
        concurrent_barrier.wait(timeout=2)
        session._on_event(
            {
                "method": "turn/completed",
                "params": {"turn": {"id": f"turn-{user_input}"}},
            }
        )
        return TurnResult(user_input)

    isolated_wrapper = mod.long_turn.wrap_session_run_turn(isolated_original)

    class IsolatedSession:
        def __init__(self, name):
            self.name = name
            self.events = []
            self._on_event = self.events.append
            self.interrupts = []

        def _issue_interrupt(self, turn_id):
            self.interrupts.append(turn_id)

    sessions = [IsolatedSession("alpha"), IsolatedSession("beta")]
    results = {}

    def exercise(session, timeout):
        results[session.name] = isolated_wrapper(
            session,
            session.name,
            turn_timeout=timeout,
        )

    threads = [
        threading.Thread(target=exercise, args=(sessions[0], 31)),
        threading.Thread(target=exercise, args=(sessions[1], 47)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert sessions[0].received_timeout == 31
    assert sessions[1].received_timeout == 47
    assert results["alpha"].final_text == "alpha"
    assert results["beta"].final_text == "beta"
    assert "alpha" not in str(sessions[1].events)
    assert "beta" not in str(sessions[0].events)
    assert not sessions[0].interrupts and not sessions[1].interrupts

    # If an operator deliberately restores a finite timeout, an assistant
    # commentary item at the deadline must not be accepted as final success.
    def deadline_original(
        session,
        user_input,
        *,
        turn_timeout,
        notification_poll_timeout,
        post_tool_quiet_timeout,
    ):
        time.sleep(turn_timeout)
        return TurnResult("still working")

    import time

    deadline_session = IsolatedSession("deadline")
    deadline_result = mod.long_turn.wrap_session_run_turn(deadline_original)(
        deadline_session,
        "work",
        turn_timeout=0.03,
        notification_poll_timeout=0.001,
    )
    assert deadline_result.interrupted is True
    assert deadline_result.should_retire is True
    assert "without turn/completed" in deadline_result.error
    assert deadline_session.interrupts == ["turn-still working"]

    delegated = []

    def original_factory(_agent):
        return delegated.append

    wrapped = mod.phase_filter.wrap_event_bridge_factory(original_factory)
    bridge = wrapped(object())

    bridge(completed("working", "commentary"))
    assert [n["params"]["item"]["text"] for n in delegated] == ["working"]

    bridge(completed("done", "final_answer"))
    assert [n["params"]["item"]["text"] for n in delegated] == ["working"]

    bridge(completed("legacy-final"))
    bridge({"method": "turn/completed", "params": {"turn": {"status": "completed"}}})
    assert "legacy-final" not in str(delegated)

    bridge(completed("legacy-commentary"))
    bridge(
        {
            "method": "item/started",
            "params": {"item": {"type": "commandExecution", "id": "cmd-1"}},
        }
    )
    assert any(
        n.get("params", {}).get("item", {}).get("text") == "legacy-commentary"
        for n in delegated
    )

    status = mod.phase_filter.patch_codex_app_server_event_bridge()
    status_again = mod.phase_filter.patch_codex_app_server_event_bridge()
    assert status in {
        "patched Codex agentMessage phase routing",
        "upstream already filters final_answer; skipped",
    }
    assert status_again in {
        "already patched",
        "upstream already filters final_answer; skipped",
    }
    original_backfill = os.environ.get(mod.session_project.BACKFILL_ENV)
    os.environ[mod.session_project.BACKFILL_ENV] = "false"
    session_project_status = mod.session_project.patch_codex_session_projects()
    session_project_status_again = mod.session_project.patch_codex_session_projects()
    if original_backfill is None:
        os.environ.pop(mod.session_project.BACKFILL_ENV, None)
    else:
        os.environ[mod.session_project.BACKFILL_ENV] = original_backfill
    assert "patched" in session_project_status or "skipped" in session_project_status
    assert "already patched" in session_project_status_again or "skipped" in session_project_status_again

    old_home = os.environ.get("HERMES_HOME")
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["HERMES_HOME"] = tmp
        image_status = mod.image_delivery.patch_codex_image_delivery()
        image_status_again = mod.image_delivery.patch_codex_image_delivery()

        from agent.transports.codex_event_projector import CodexEventProjector

        tiny_png = base64.b64encode(
            b"\x89PNG\r\n\x1a\n" + b"codex-image-test"
        ).decode("ascii")
        projection = CodexEventProjector().project(
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "imageGeneration",
                        "id": "exec-image-test",
                        "status": "completed",
                        "revisedPrompt": "test image",
                        "result": tiny_png,
                    }
                },
            }
        )
        assert len(projection.messages) == 2
        tool_call = projection.messages[0]["tool_calls"][0]
        assert tool_call["function"]["name"] == "image_generate"
        tool_payload = json.loads(projection.messages[1]["content"])
        image_path = Path(tool_payload["image"])
        assert tool_payload["success"] is True
        assert image_path.is_file()
        assert image_path.parent == (Path(tmp) / "cache" / "images").resolve()

        def empty_image_turn(_agent, **kwargs):
            kwargs["messages"].extend(projection.messages)
            return {
                "final_response": "",
                "messages": kwargs["messages"],
                "api_calls": 1,
                "completed": True,
            }

        wrapped_turn = mod.image_delivery.wrap_codex_runtime_turn(empty_image_turn)
        history = [{"role": "user", "content": "generate"}]
        recovered = wrapped_turn(object(), messages=history)
        assert recovered["final_response"] == f"MEDIA:{image_path.resolve()}"

        assert "projector patched" in image_status
        assert "already patched" in image_status_again

    # Current Codex app-server permissions protocol: only the granted subset
    # is returned. An empty profile is a denial; persistence is session-only.
    requested = {
        "network": {"enabled": True, "ignored": "not-on-wire"},
        "fileSystem": {
            "write": ["/tmp/export"],
            "entries": [
                {
                    "access": "write",
                    "path": {"type": "path", "path": "/tmp/export"},
                }
            ],
            "ignored": ["/etc"],
        },
        "unknown": {"enabled": True},
    }
    once_response = mod.approval_bridge.permission_response(requested, "once")
    assert once_response["scope"] == "turn"
    assert once_response["permissions"]["network"] == {"enabled": True}
    assert once_response["permissions"]["fileSystem"]["write"] == ["/tmp/export"]
    assert "ignored" not in once_response["permissions"]["fileSystem"]
    assert "unknown" not in once_response["permissions"]
    assert mod.approval_bridge.permission_response(requested, "always") == {
        "permissions": once_response["permissions"],
        "scope": "session",
    }
    assert mod.approval_bridge.permission_response(requested, "deny") == {
        "permissions": {},
        "scope": "turn",
    }

    permission_responses = []

    class PermissionClient:
        def respond(self, request_id, result):
            permission_responses.append((request_id, result))

    permission_callback_calls = []

    class PermissionSession:
        _client = PermissionClient()
        _approval_callback = staticmethod(
            lambda *args, **kwargs: permission_callback_calls.append((args, kwargs))
            or "once"
        )

    delegated_requests = []
    permission_handler = mod.approval_bridge.wrap_server_request_handler(
        lambda _self, request: delegated_requests.append(request)
    )
    permission_handler(
        PermissionSession(),
        {
            "id": 61,
            "method": "item/permissions/requestApproval",
            "params": {
                "cwd": "/workspace",
                "reason": "download a dependency",
                "permissions": {"network": {"enabled": True}},
            },
        },
    )
    assert permission_responses == [
        (61, {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
    ]
    assert permission_callback_calls[0][1]["allow_permanent"] is False
    assert permission_callback_calls[0][1]["approval_choices"] == [
        "once",
        "session",
        "deny",
    ]
    assert delegated_requests == []

    execpolicy_params = {
        "command": "curl https://example.invalid",
        "cwd": "/workspace",
        "proposedExecpolicyAmendment": ["prefix_rule", "curl"],
    }
    assert mod.approval_bridge.command_approval_choices(execpolicy_params) == [
        "once",
        "session",
        "always",
        "deny",
    ]
    assert mod.approval_bridge.command_response(execpolicy_params, "session") == {
        "decision": "acceptForSession"
    }
    assert mod.approval_bridge.command_response(execpolicy_params, "always") == {
        "decision": {
            "acceptWithExecpolicyAmendment": {
                "execpolicy_amendment": ["prefix_rule", "curl"]
            }
        }
    }
    assert mod.approval_bridge.command_response(
        {
            "proposedNetworkPolicyAmendments": [
                {"host": "example.invalid", "action": "allow"}
            ]
        },
        "always",
    ) == {
        "decision": {
            "applyNetworkPolicyAmendment": {
                "network_policy_amendment": {
                    "host": "example.invalid",
                    "action": "allow",
                }
            }
        }
    }
    assert mod.approval_bridge.file_change_response("session") == {
        "decision": "acceptForSession"
    }

    # Computer Use asks through MCP elicitation rather than the standalone
    # permissions method. Only the bundled connector identity is intercepted.
    computer_use_request = {
        "id": 63,
        "method": "mcpServer/elicitation/request",
        "params": {
            "serverName": "node-repl",
            "threadId": "thread-1",
            "turnId": "turn-1",
            "mode": "openai/form",
            "message": 'Allow Computer Use to use "Notes"?',
            "requestedSchema": {},
            "_meta": {
                "connector_id": "computer-use",
                "connector_name": "Computer Use",
                "persist": ["session", "always"],
                "riskLevel": "medium",
                "tool_params": {"app": "com.apple.Notes"},
                "tool_params_display": [
                    {"name": "app", "display_name": "App", "value": "Notes"}
                ],
            },
        },
    }
    permission_handler(PermissionSession(), computer_use_request)
    assert permission_responses[-1] == (
        63,
        {"action": "accept", "content": None, "_meta": None},
    )
    cua_args, cua_kwargs = permission_callback_calls[-1]
    assert cua_args[0] == "computer_use app=com.apple.Notes"
    assert 'Allow Computer Use to use "Notes"?' in cua_args[1]
    assert cua_kwargs["allow_permanent"] is True
    assert cua_kwargs["approval_choices"] == [
        "once",
        "session",
        "always",
        "deny",
    ]

    assert mod.approval_bridge.computer_use_elicitation_response("always") == {
        "action": "accept",
        "content": None,
        "_meta": {"persist": "always"},
    }
    assert mod.approval_bridge.computer_use_elicitation_response("deny") == {
        "action": "decline",
        "content": None,
        "_meta": None,
    }

    unrelated_elicitation = {
        "id": 64,
        "method": "mcpServer/elicitation/request",
        "params": {
            "serverName": "third-party-mcp",
            "threadId": "thread-1",
            "mode": "form",
            "message": "Provide a value",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }
    permission_handler(PermissionSession(), unrelated_elicitation)
    assert delegated_requests[-1] == unrelated_elicitation

    # Exercise the same private queue contract Hermes' own command guards use.
    notified = []
    approval_module = SimpleNamespace(
        _lock=threading.Lock(),
        _gateway_notify_cbs={"qq-session": notified.append},
        get_current_session_key=lambda default="": "qq-session",
        _await_gateway_decision=lambda session_key, notify, data, surface: (
            notify(data) or {"resolved": True, "choice": "once"}
        ),
    )
    gateway_choice = mod.approval_bridge._gateway_approval_choice(
        "curl https://example.invalid/file",
        "Codex requests network access",
        approval_choices=["once", "session", "deny"],
        _approval_module=approval_module,
    )
    assert gateway_choice == "once"
    assert notified and notified[0]["allow_permanent"] is False
    assert notified[0]["codex_approval_choices"] == ["once", "session", "deny"]

    # Integrate with the installed Hermes queue, not only the isolated fake.
    from tools import approval as real_approval

    real_session_key = "codex-approval-hotfix-regression"
    real_notified = []
    real_token = real_approval.set_current_session_key(real_session_key)

    def resolve_real_queue(data):
        real_notified.append(data)
        assert real_approval.resolve_gateway_approval(real_session_key, "once") == 1

    real_approval.register_gateway_notify(real_session_key, resolve_real_queue)
    try:
        assert mod.approval_bridge._gateway_approval_choice(
            "request_permissions network",
            "Codex requests network access",
            approval_choices=["once", "session", "deny"],
        ) == "once"
        assert real_notified[0]["pattern_key"] == "codex_app_server:approval"
        assert real_notified[0]["codex_approval_choices"] == [
            "once",
            "session",
            "deny",
        ]
    finally:
        real_approval.unregister_gateway_notify(real_session_key)
        real_approval.reset_current_session_key(real_token)

    injected_callbacks = []

    def capture_init(_self, **kwargs):
        injected_callbacks.append(kwargs.get("approval_callback"))

    mod.approval_bridge.wrap_session_init(capture_init)(object(), approval_callback=None)
    assert injected_callbacks == [mod.approval_bridge._gateway_approval_choice]

    approval_status = mod.approval_bridge.patch_codex_gateway_approvals()
    approval_status_again = mod.approval_bridge.patch_codex_gateway_approvals()
    assert "gateway callback patched" in approval_status
    assert "command/file approval choices patched" in approval_status
    assert "permission requests patched" in approval_status
    assert "Computer Use elicitations patched" in approval_status
    assert "already patched" in approval_status_again

    long_turn_status = mod.long_turn.patch_codex_app_server_turn_timeout()
    long_turn_status_again = mod.long_turn.patch_codex_app_server_turn_timeout()
    assert "patched Codex turn wall deadline" in long_turn_status
    assert long_turn_status_again == "already patched"

    lifecycle_status = mod.lifecycle.patch_codex_agent_soft_eviction()
    lifecycle_status_again = mod.lifecycle.patch_codex_agent_soft_eviction()
    assert "soft-eviction patched" in lifecycle_status
    assert lifecycle_status_again == "already patched"

    # Verify all four live session request methods after the class patch.
    from agent.transports.codex_app_server_session import (
        CodexAppServerSession,
        _ServerRequestRouting,
    )

    bridged_responses = []

    class BridgedClient:
        def respond(self, request_id, result):
            bridged_responses.append((request_id, result))

    live_session = object.__new__(CodexAppServerSession)
    live_session._client = BridgedClient()
    live_session._routing = _ServerRequestRouting()
    live_session._cwd = "/workspace"
    live_session._pending_file_changes = {"file-1": "change README.md"}
    live_session._approval_callback = lambda *_args, **_kwargs: "once"
    live_session._handle_server_request(
        {
            "id": 71,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "curl https://example.invalid", "cwd": "/workspace"},
        }
    )
    live_session._handle_server_request(
        {
            "id": 72,
            "method": "item/fileChange/requestApproval",
            "params": {"itemId": "file-1", "reason": "update documentation"},
        }
    )
    live_session._handle_server_request(
        {
            "id": 73,
            "method": "item/permissions/requestApproval",
            "params": {
                "cwd": "/workspace",
                "permissions": {"network": {"enabled": True}},
            },
        }
    )
    live_session._handle_server_request(
        {
            "id": 74,
            "method": "mcpServer/elicitation/request",
            "params": computer_use_request["params"],
        }
    )
    assert bridged_responses == [
        (71, {"decision": "accept"}),
        (72, {"decision": "accept"}),
        (73, {"permissions": {"network": {"enabled": True}}, "scope": "turn"}),
        (74, {"action": "accept", "content": None, "_meta": None}),
    ]

    scoped_responses = []
    live_session._client = SimpleNamespace(
        respond=lambda request_id, result: scoped_responses.append(
            (request_id, result)
        )
    )
    live_session._approval_callback = lambda *_args, **_kwargs: "session"
    live_session._handle_server_request(
        {
            "id": 75,
            "method": "item/commandExecution/requestApproval",
            "params": {"command": "pwd", "cwd": "/workspace"},
        }
    )
    live_session._handle_server_request(
        {
            "id": 76,
            "method": "item/fileChange/requestApproval",
            "params": {"itemId": "file-1"},
        }
    )
    assert scoped_responses == [
        (75, {"decision": "acceptForSession"}),
        (76, {"decision": "acceptForSession"}),
    ]

    if old_home is None:
        os.environ.pop("HERMES_HOME", None)
    else:
        os.environ["HERMES_HOME"] = old_home

    print("commentary_forwarded=true")
    print("final_answer_suppressed=true")
    print("unknown_terminal_suppressed=true")
    print("image_generation_materialized=true")
    print("empty_image_final_recovered=true")
    print("gateway_approval_callback=true")
    print("gateway_real_queue_roundtrip=true")
    print("complete_command_file_choices=true")
    print("persistent_command_amendment=true")
    print("permission_subset_response=true")
    print("permission_deny_empty_subset=true")
    print("computer_use_elicitation_bridge=true")
    print("unrelated_elicitation_delegated=true")
    print("long_turn_unlimited=true")
    print("long_turn_deadline_terminal_guard=true")
    print("long_turn_session_isolation=true")
    print("idle_codex_writer_soft_eviction=true")
    print("active_codex_writer_preserved=true")
    print("session_project_stable_route=true")
    print("session_thread_resume=true")
    print("session_project_admin_gate=true")
    print("session_project_switch_isolation=true")
    print("session_project_channel_command_context=true")
    print("session_project_new_command_rotation=true")
    print("session_key_project_name_migration=true")
    print("codex_app_cross_platform_registration=true")
    print(f"install_status={status}")
    print(f"session_project_install_status={session_project_status}")


if __name__ == "__main__":
    main()
