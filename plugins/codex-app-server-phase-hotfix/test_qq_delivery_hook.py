"""QQ origin -> real Codex subprocess environment -> native hook entrypoint."""
import concurrent.futures
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.transports.codex_app_server import CodexAppServerClient

spec = importlib.util.spec_from_file_location("qq_delivery_hook_test", Path(__file__).with_name("qq_delivery_hook.py"))
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def main():
    from agent import codex_runtime
    old_runtime, old_client_init = codex_runtime.run_codex_app_server_turn, CodexAppServerClient.__init__
    try:
        hook.patch_qq_delivery_context()
        patched_runtime, patched_init = codex_runtime.run_codex_app_server_turn, CodexAppServerClient.__init__
        assert patched_runtime is not old_runtime and patched_init is not old_client_init
        hook.patch_qq_delivery_context()
        assert codex_runtime.run_codex_app_server_turn is patched_runtime
        assert CodexAppServerClient.__init__ is patched_init
    finally:
        codex_runtime.run_codex_app_server_turn, CodexAppServerClient.__init__ = old_runtime, old_client_init
    with tempfile.TemporaryDirectory() as tmp:
        tmp = str(Path(tmp).resolve())
        profile, home = Path(tmp, "profile"), Path(tmp, "codex-home")
        other_profile, other_home = Path(tmp, "other-profile"), Path(tmp, "other-home")
        for path in (profile, home, other_profile, other_home):
            path.mkdir()
        spawned = []
        lock = threading.Lock()

        def popen(_cmd, **kwargs):
            env = kwargs["env"]
            with lock:
                spawned.append({key: env.get(key) for key in (hook.PROFILE_ENV, hook.CODEX_HOME_ENV)})
            proc = MagicMock()
            proc.stdin, proc.stdout, proc.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()
            proc.poll.return_value = None
            return proc

        barrier = threading.Barrier(2)

        @hook.wrap_runtime_turn
        def run(agent, prompt, *, concurrent=False, fail=False):
            if concurrent:
                barrier.wait(timeout=3)
            if fail:
                raise ValueError("test runtime failure")
            with CodexAppServerClient(codex_home=str(home)):
                pass
            return prompt

        qq = SimpleNamespace(_gateway_session_key="agent:main:qqbot:dm:test-chat", session_id="qq-turn")
        group = SimpleNamespace(_gateway_session_key="agent:department:qqbot:group:test-group:user", session_id="group-turn")
        wa = SimpleNamespace(_gateway_session_key="agent:main:whatsapp:dm:qqbot", session_id="wa-turn")
        cli = SimpleNamespace(session_id="cli-turn")
        guild = SimpleNamespace(_gateway_session_key="agent:main:qqbot:channel:guild", session_id="guild-turn")
        old_init = CodexAppServerClient.__init__
        with patch.dict(os.environ, {"HERMES_HOME": str(profile), hook.PROFILE_ENV: "stale-qq-origin",
                                    "HERMES_CODEX_SESSION_PROJECTS_ENABLED": "false"}), \
             patch.object(CodexAppServerClient, "__init__", hook.wrap_client_init(old_init)), \
             patch("agent.transports.codex_app_server.subprocess.Popen", side_effect=popen):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(run, agent, "A concise report would help.", concurrent=True) for agent in (qq, wa)]
                assert all(f.result() == "A concise report would help." for f in futures)
            assert sorted(x[hook.PROFILE_ENV] for x in spawned) == ["", str(profile)]
            assert next(x for x in spawned if x[hook.PROFILE_ENV])[hook.CODEX_HOME_ENV] == str(home)
            run(group, "A reusable script would help.")
            assert spawned[-1][hook.PROFILE_ENV] == str(profile)
            for agent in (cli, wa, guild):
                run(agent, "QQ upload MEDIA words cannot select the channel")
                assert spawned[-1] == {hook.PROFILE_ENV: "", hook.CODEX_HOME_ENV: ""}
            try:
                run(qq, "failure", fail=True)
            except ValueError:
                pass
            with CodexAppServerClient():
                pass
            assert spawned[-1][hook.PROFILE_ENV] == "", "QQ context leaked after an exception"
            assert os.environ[hook.PROFILE_ENV] == "stale-qq-origin", "process environment was mutated"

        event = {"hook_event_name": "UserPromptSubmit", "session_id": "codex-session",
                 "turn_id": "codex-turn", "cwd": "/same/project", "prompt": "A short report would help."}

        digest = hashlib.sha256(Path(hook.__file__).read_bytes()).hexdigest()

        def invoke(origin, target_profile=profile, target_home=home, body=event, source_hash=digest, ok=True):
            env = {**os.environ, hook.PROFILE_ENV: origin.get(hook.PROFILE_ENV, ""),
                   hook.CODEX_HOME_ENV: origin.get(hook.CODEX_HOME_ENV, "")}
            result = subprocess.run([sys.executable, str(Path(hook.__file__)),
                                     "--source-sha256", source_hash,
                                     "--hermes-home", str(target_profile), "--codex-home", str(target_home)],
                                    input=json.dumps(body), text=True, capture_output=True, env=env, timeout=5)
            assert (result.returncode == 0) == ok, result.stderr
            return result.stdout

        origin = {hook.PROFILE_ENV: str(profile), hook.CODEX_HOME_ENV: str(home)}
        assert invoke(origin).strip() == hook.CONTRACT
        assert invoke({}) == "", "ordinary CLI in the same cwd received QQ context"
        assert invoke(origin, target_profile=other_profile) == ""
        assert invoke(origin, target_home=other_home) == ""
        assert invoke(origin, body={**event, "hook_event_name": "SessionStart"}) == ""
        assert invoke(origin, source_hash="0" * 64, ok=False) == ""
        records = (profile / "logs/qq-delivery-hook.jsonl").read_text().splitlines()
        assert len(records) == 1
        record = json.loads(records[0])
        assert record["turn_id"] == event["turn_id"]
        assert "prompt" not in record and "cwd" not in record
        assert not (other_profile / "logs").exists()
    print("QQ DM/group hook context, concurrency, ordinary CLI/channel exclusion, and profile isolation: PASS")


if __name__ == "__main__":
    main()
