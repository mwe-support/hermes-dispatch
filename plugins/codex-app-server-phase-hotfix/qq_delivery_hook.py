"""QQ-only developer context for Codex's native UserPromptSubmit hook.

The Gateway supplies origin through the app-server's process environment;
neither the prompt nor the workspace name is used to infer a chat channel.
This module also runs standalone, without importing Hermes or its plugins.
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import hashlib
import json
import os
from pathlib import Path
import sys
import time


PROFILE_ENV = "HERMES_DISPATCH_QQ_PROFILE"
CODEX_HOME_ENV = "HERMES_DISPATCH_QQ_CODEX_HOME"
_QQ_PROFILE = contextvars.ContextVar("dispatch_qq_profile", default="")
_MARKER = "_dispatch_qq_delivery_context_wrapped"
CONTRACT = '''This task is in a QQ chat with native file delivery. When the task calls for a reusable artifact such as a report, presentation, or code, create and verify the final file, then put MEDIA:"/absolute/path/to/file" on its own line in your final response for each requested output. A local path, Markdown link, or file citation alone is not the delivery contract. Attach only requested final outputs and requested examples; keep source material, secrets, and intermediate files out of delivery. For tasks that need only a conversational answer, answer normally. Describe files as created or verified; the channel reports actual delivery success or failure. If creation or verification fails, explain the failure instead of declaring completion.'''


def wrap_runtime_turn(original):
    @functools.wraps(original)
    def run(agent, *args, **kwargs):
        # Hermes constructs this key from SessionSource, not user text. QQ
        # guild/channel uploads are unsupported; enable only DM/group routes.
        key = str(getattr(agent, "_gateway_session_key", "") or "")
        parts = key.split(":", 4)
        is_qq = (
            len(parts) == 5 and parts[0] == "agent" and bool(parts[1])
            and parts[2] == "qqbot" and parts[3] in {"dm", "group"}
            and bool(parts[4]) and bool(getattr(agent, "session_id", None))
        )
        profile = str(Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()) if is_qq else ""
        token = _QQ_PROFILE.set(profile)
        try:
            return original(agent, *args, **kwargs)
        finally:
            _QQ_PROFILE.reset(token)

    setattr(run, _MARKER, True)
    return run


def wrap_client_init(original):
    @functools.wraps(original)
    def init(self, codex_bin="codex", codex_home=None, extra_args=None, env=None, **kwargs):
        child_env = dict(env or {})
        home = codex_home or child_env.get("CODEX_HOME") or os.environ.get("CODEX_HOME", "~/.codex")
        # Explicit empty values also clear inherited/stale origin flags for
        # non-QQ clients. Never mutate os.environ: turns can run concurrently.
        child_env[PROFILE_ENV] = _QQ_PROFILE.get()
        child_env[CODEX_HOME_ENV] = str(Path(home).expanduser().resolve()) if _QQ_PROFILE.get() else ""
        return original(self, codex_bin=codex_bin, codex_home=codex_home,
                        extra_args=extra_args, env=child_env, **kwargs)

    setattr(init, _MARKER, True)
    return init


def patch_qq_delivery_context():
    from agent import codex_runtime
    from agent.transports.codex_app_server import CodexAppServerClient

    if not getattr(codex_runtime.run_codex_app_server_turn, _MARKER, False):
        codex_runtime.run_codex_app_server_turn = wrap_runtime_turn(codex_runtime.run_codex_app_server_turn)
    if not getattr(CodexAppServerClient.__init__, _MARKER, False):
        CodexAppServerClient.__init__ = wrap_client_init(CodexAppServerClient.__init__)
    return "QQ DM/group origin scoped to each Codex subprocess"


def run_hook(event, hermes_home: Path, codex_home: Path, source_sha256: str):
    profile, home = str(hermes_home.resolve()), str(codex_home.resolve())
    if (event.get("hook_event_name") != "UserPromptSubmit"
            or os.environ.get(PROFILE_ENV) != profile
            or os.environ.get(CODEX_HOME_ENV) != home):
        return
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != source_sha256:
        raise ValueError("QQ delivery hook changed; reinstall and review its native hook definition")
    record = {
        "time": time.time(), "event": "UserPromptSubmit",
        "session_id": event.get("session_id"), "turn_id": event.get("turn_id"),
        "contract_sha256": hashlib.sha256(CONTRACT.encode()).hexdigest(),
        "source_sha256": source_sha256,
    }
    try:
        log = hermes_home / "logs" / "qq-delivery-hook.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + "\n")
    except OSError:
        # Audit failure should not withhold the delivery contract or stop work.
        print("QQ delivery hook: could not write audit record", file=sys.stderr)
    print(CONTRACT)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    args = parser.parse_args()
    event = json.load(sys.stdin)
    if not isinstance(event, dict):
        raise ValueError("hook input must be an object")
    run_hook(event, args.hermes_home.expanduser(), args.codex_home.expanduser(), args.source_sha256)


if __name__ == "__main__":
    main()
