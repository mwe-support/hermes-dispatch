"""Close idle Codex subprocesses when Hermes soft-evicts an Agent."""

from __future__ import annotations

import functools
import inspect
import logging
import threading
from collections.abc import Callable

logger = logging.getLogger(__name__)

_PATCH_MARKER = "_codex_app_server_lifecycle_hotfix_wrapped"
_RUNTIME_MARKER = "_codex_lifecycle_runtime_wrapped"
_PATCH_LOCK = threading.Lock()


def _has_active_turn(session: object) -> bool:
    """Read the session's active-turn flag under its lock when available."""
    lock = getattr(session, "_active_turn_lock", None)
    if lock is None:
        return bool(getattr(session, "_active_turn_id", None))
    with lock:
        return bool(getattr(session, "_active_turn_id", None))


def wrap_release_clients(original: Callable) -> Callable:
    """Extend Hermes soft cleanup to release an idle Codex thread writer.

    ``AIAgent.close()`` already owns the hard-cleanup path.  Hermes 0.20.5's
    ``release_clients()`` omits ``_codex_session``, however, so an Agent cache
    eviction can leave its app-server subprocess holding the thread while the
    replacement Agent immediately tries ``thread/resume``.
    """

    @functools.wraps(original)
    def release_clients(self, *args, **kwargs):
        session = getattr(self, "_codex_session", None)
        if session is not None and not _has_active_turn(session):
            # Clear the owner reference before close, matching AIAgent.close().
            # This stays idempotent even when close itself raises.
            self._codex_session = None
            try:
                session.close()
                logger.info(
                    "codex-app-server-phase-hotfix: closed idle Codex session "
                    "during Agent soft eviction"
                )
            except Exception:
                logger.warning(
                    "codex-app-server-phase-hotfix: idle Codex session close "
                    "failed during Agent soft eviction",
                    exc_info=True,
                )
        return original(self, *args, **kwargs)

    setattr(release_clients, _PATCH_MARKER, True)
    release_clients._codex_app_server_lifecycle_original = original
    return release_clients


def _upstream_closes_codex_session(release_clients: Callable) -> bool:
    try:
        source = inspect.getsource(release_clients)
    except (OSError, TypeError):
        return False
    return "_codex_session" in source and ".close()" in source


def _patch_agent_class(AIAgent) -> str:
    """Called only with an initialized class, before its first Codex session."""
    current = AIAgent.release_clients
    if getattr(current, _PATCH_MARKER, False):
        return "already patched"
    if _upstream_closes_codex_session(current):
        return "upstream already closes idle Codex sessions; skipped"
    AIAgent.release_clients = wrap_release_clients(current)
    return "idle Codex session soft-eviction patched"


def patch_codex_agent_soft_eviction() -> str:
    """Defer class patching until runtime entry; never import run_agent here."""
    from agent import codex_runtime

    original = codex_runtime.run_codex_app_server_turn
    if getattr(original, _RUNTIME_MARKER, False):
        return "already patched"

    @functools.wraps(original)
    def run(agent, *args, **kwargs):
        # Plugin discovery may hold the registry lock while run_agent imports
        # model_tools on another thread. At turn entry the class is complete,
        # and no Codex session has been allocated yet (cron/CLI/Gateway alike).
        with _PATCH_LOCK:
            _patch_agent_class(type(agent))
        return original(agent, *args, **kwargs)

    setattr(run, _RUNTIME_MARKER, True)
    codex_runtime.run_codex_app_server_turn = run
    return "idle Codex session soft-eviction patched at first Codex turn"
