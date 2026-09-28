"""Real cold imports with isolated homes; run with Hermes on PYTHONPATH."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


PROBE = r'''
import sys
from unittest.mock import patch
if sys.argv[1] == "background":
    from hermes_cli.plugins import start_background_plugin_discovery
    start_background_plugin_discovery()
elif sys.argv[1] == "cron":
    import cron.scheduler
elif sys.argv[1] == "mcp":
    import mcp_serve
    from hermes_cli.plugins import discover_plugins
    discover_plugins()
import run_agent
from hermes_cli.plugins import discover_plugins, get_plugin_manager
from agent import codex_runtime
discover_plugins()
plugins = get_plugin_manager()._plugins
for name in ("codex-app-server-phase-hotfix", "qqbot-connect-hotfix", "message-snapshot-store"):
    assert name in plugins, name
    assert plugins[name].enabled and not plugins[name].error, (name, plugins[name].error)

# An actual AIAgent enters the real wrapper chain. Stop at session allocation:
# no credentials, model calls, Codex process, or QQ messages are needed.
from agent.transports.codex_app_server_session import CodexAppServerSession
agent = object.__new__(run_agent.AIAgent)
agent.session_cwd = __import__("os").environ["HERMES_HOME"]
with patch.object(CodexAppServerSession, "__init__", side_effect=RuntimeError("allocation reached")):
    try:
        codex_runtime.run_codex_app_server_turn(agent, user_message="test", original_user_message="test",
                                              messages=[], effective_task_id="startup-test")
    except RuntimeError as exc:
        assert str(exc) == "allocation reached", str(exc)
    else:
        raise AssertionError("did not reach session allocation")
assert getattr(run_agent.AIAgent.release_clients, "_codex_app_server_lifecycle_hotfix_wrapped", False)
# Cover idle cleanup, repeated cleanup, and active-turn protection.
from types import SimpleNamespace
from unittest.mock import Mock
idle = SimpleNamespace(_active_turn_id=None, close=Mock())
agent._codex_session = idle
agent.release_clients()
agent.release_clients()
idle.close.assert_called_once()
assert agent._codex_session is None
active = SimpleNamespace(_active_turn_id="active", close=Mock())
agent._codex_session = active
agent.release_clients()
active.close.assert_not_called()
assert agent._codex_session is active
agent._codex_session = None
print(sys.argv[1] + ": plugins fully loaded; lifecycle active before session allocation: PASS")
'''


def main():
    import hermes_cli
    root = Path(hermes_cli.__file__).resolve().parent.parent
    assert not (root / ".env").exists(), "Use a clean Hermes checkout without a project .env"
    for mode in sys.argv[1:] or ("cold", "background", "cron", "mcp"):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp, "hermes")
            home.mkdir()
            names = ("codex-app-server-phase-hotfix", "qqbot-connect-hotfix", "message-snapshot-store")
            for name in names:
                shutil.copytree(Path(__file__).resolve().parent.parent / name, home / "plugins" / name,
                                ignore=shutil.ignore_patterns("__pycache__"))
            (home / "config.yaml").write_text("plugins:\n  enabled:\n" + "".join(f"    - {n}\n" for n in names))
            env = {k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "WINDIR", "LANG"}}
            env.update(HOME=tmp, HERMES_HOME=str(home), CODEX_HOME=str(Path(tmp, "codex")),
                       PYTHONPATH=str(root), HERMES_SAFE_MODE="0",
                       HERMES_CODEX_SESSION_PROJECTS_BACKFILL="false", HERMES_CODEX_APP_REGISTER_PROJECTS="false")
            result = subprocess.run([sys.executable, "-c", PROBE, mode], env=env, cwd=root,
                                    capture_output=True, text=True, timeout=45)
            assert result.returncode == 0, result.stdout + result.stderr
            print(result.stdout.strip())


if __name__ == "__main__":
    main()
