"""Carry Hermes' resolved slash-command settings into native Codex requests."""

from __future__ import annotations

import contextvars
import functools


_OPTIONS = contextvars.ContextVar("dispatch_codex_model_options", default=None)
_MARKER = "_dispatch_codex_model_controls_wrapped"


def wrap_runtime_turn(original):
    @functools.wraps(original)
    def run(agent, *args, **kwargs):
        # Gateway already resolves /model, --once and /reasoning per session.
        # Reading the Agent also preserves channel/provider override priority.
        model = getattr(agent, "model", None)
        options = None
        if isinstance(model, str) and model.strip():
            reasoning = getattr(agent, "reasoning_config", None) or {}
            options = {
                "model": model.strip(),
                "effort": ("none" if reasoning.get("enabled") is False
                           else reasoning.get("effort") or "medium"),
            }
        token = _OPTIONS.set(options)
        try:
            return original(agent, *args, **kwargs)
        finally:
            _OPTIONS.reset(token)

    setattr(run, _MARKER, True)
    return run


def wrap_request(original):
    @functools.wraps(original)
    def request(self, method, params=None, *args, **kwargs):
        options = _OPTIONS.get()
        if options is not None and method in {"thread/start", "thread/resume", "turn/start"}:
            defaults = {"model": options["model"]}
            if method == "turn/start":
                # Codex retains overrides on the thread. Send the effective
                # effort every turn, including after /reasoning reset or --once.
                defaults["effort"] = options["effort"]
            # A newer upstream's explicit request fields remain authoritative.
            params = {**defaults, **(params or {})}
        return original(self, method, params, *args, **kwargs)

    setattr(request, _MARKER, True)
    return request


def patch_codex_model_controls():
    from agent import codex_runtime
    from agent.transports.codex_app_server import CodexAppServerClient

    if not getattr(codex_runtime.run_codex_app_server_turn, _MARKER, False):
        codex_runtime.run_codex_app_server_turn = wrap_runtime_turn(codex_runtime.run_codex_app_server_turn)
    if not getattr(CodexAppServerClient.request, _MARKER, False):
        CodexAppServerClient.request = wrap_request(CodexAppServerClient.request)
    return "Hermes model/reasoning settings scoped to each Codex turn"
