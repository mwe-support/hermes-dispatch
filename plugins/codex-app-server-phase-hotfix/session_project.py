"""Persist Hermes channel sessions as stable Codex projects and threads.

Hermes 0.20.0 keeps ``session_key -> AIAgent`` only in its process-local
agent cache.  ``CodexAppServerSession`` in turn keeps the Codex thread id only
on that AIAgent.  Rebuilding the agent therefore calls ``thread/start`` even
when the durable Hermes ``session_id`` has not changed.

This compatibility layer adds two durable mappings without changing Hermes'
own session database:

* one stable Codex project per channel ``session_key``;
* one current Codex thread per (Hermes ``session_id``, project path).

The stable Hermes session key becomes the default project's directory name. A
``/new`` or ``/reset`` rotates only ``session_id``, so the next turn starts a
new, session-id-named Codex thread inside the existing project. Process
restart/cache eviction with the same session id instead uses ``thread/resume``.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback normally, then release SQLite locks on Windows."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


logger = logging.getLogger(__name__)

ENABLED_ENV = "HERMES_CODEX_SESSION_PROJECTS_ENABLED"
ROOT_ENV = "HERMES_CODEX_SESSION_PROJECTS_ROOT"
ALIASES_ENV = "HERMES_CODEX_PROJECT_ALIASES"
ALLOWED_ROOTS_ENV = "HERMES_CODEX_PROJECT_ALLOWED_ROOTS"
ADMIN_USERS_ENV = "HERMES_CODEX_PROJECT_ADMIN_USERS"
BACKFILL_ENV = "HERMES_CODEX_SESSION_PROJECTS_BACKFILL"
REGISTER_APP_ENV = "HERMES_CODEX_APP_REGISTER_PROJECTS"
REGISTER_APP_CLI_ENV = "HERMES_CODEX_APP_CLI"
REGISTER_APP_TIMEOUT_ENV = "HERMES_CODEX_APP_REGISTER_TIMEOUT_SECONDS"
REGISTER_APP_RETRY_ENV = "HERMES_CODEX_APP_REGISTER_RETRY_SECONDS"

_RUNTIME_MARKER = "_codex_session_project_runtime_wrapped"
_INIT_MARKER = "_codex_session_project_init_wrapped"
_ENSURE_MARKER = "_codex_session_project_ensure_wrapped"
_CURRENT_BINDING: contextvars.ContextVar[Optional["SessionBinding"]] = (
    contextvars.ContextVar("codex_session_project_binding", default=None)
)
_CHANNEL_COMMAND_CONTEXT: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("codex_session_project_channel_command", default=None)
)
_THREAD_OWNER_LOCK = threading.Lock()
_THREAD_OWNERS: "weakref.WeakValueDictionary[str, Any]" = (
    weakref.WeakValueDictionary()
)


def _claim_thread_owner(thread_id: str, session: Any) -> None:
    with _THREAD_OWNER_LOCK:
        _THREAD_OWNERS[str(thread_id)] = session


def _retire_idle_thread_owner(thread_id: str, claimant: Any) -> bool:
    """Close a stale in-process writer before another Agent resumes it.

    A real active Hermes turn is never interrupted. Writers outside this
    Gateway process are deliberately untouched and remain protected by
    Codex's own single-writer error.
    """
    from .lifecycle import _has_active_turn

    key = str(thread_id)
    with _THREAD_OWNER_LOCK:
        owner = _THREAD_OWNERS.get(key)
        if owner is None or owner is claimant or _has_active_turn(owner):
            return False
        _THREAD_OWNERS.pop(key, None)
    try:
        owner.close()
    except Exception:
        logger.warning(
            "codex session project: failed to retire idle in-process owner "
            "for thread=%s",
            key[:8],
            exc_info=True,
        )
        return False
    logger.info(
        "codex session project: retired idle in-process owner before "
        "resuming thread=%s",
        key[:8],
    )
    return True


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()


def _project_root() -> Path:
    raw = os.environ.get(ROOT_ENV, "").strip()
    return (
        Path(raw).expanduser().resolve()
        if raw
        else (_hermes_home() / "codex-projects").resolve()
    )


def _safe_session_id(session_id: str) -> str:
    """Keep Hermes' readable id while rejecting path separators/control data."""
    value = str(session_id or "").strip()
    if not value or value in {".", ".."}:
        raise ValueError("Hermes session_id is required")
    if any(ch in value for ch in ("/", "\\", "\x00")):
        raise ValueError("Hermes session_id is not a safe project name")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Hermes session_id contains control characters")
    return value


def _session_key_project_name(session_key: str) -> str:
    """Return the exact logical project name after portable validation."""
    value = str(session_key or "").strip()
    if not value:
        raise ValueError("Hermes session_key is required")
    if value in {".", ".."} or "/" in value or "\\" in value or "\x00" in value:
        raise ValueError("Hermes session_key is not a safe project name")
    if any(ord(ch) < 32 for ch in value):
        raise ValueError("Hermes session_key contains control characters")
    return value


def _session_key_project_basename(
    session_key: str, *, platform: Optional[str] = None
) -> str:
    """Return a readable, deterministic filesystem basename.

    POSIX keeps ordinary Hermes keys verbatim. Windows replaces the forbidden
    ASCII colon with the compatible full-width colon while the exact key stays
    in SQLite and the project manifest.
    """
    value = _session_key_project_name(session_key)
    target_platform = platform or sys.platform
    if target_platform == "win32":
        value = value.replace(":", "：")
    if len(value.encode("utf-8")) > 255:
        raise ValueError("Hermes session_key exceeds the filesystem basename limit")
    return value


def _register_app_enabled() -> bool:
    return os.environ.get(REGISTER_APP_ENV, "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _backfill_enabled() -> bool:
    return os.environ.get(BACKFILL_ENV, "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _register_app_timeout() -> float:
    raw = os.environ.get(REGISTER_APP_TIMEOUT_ENV, "30").strip()
    try:
        value = float(raw)
    except ValueError:
        return 30.0
    return min(max(value, 1.0), 120.0)


def _register_app_retry_seconds() -> float:
    raw = os.environ.get(REGISTER_APP_RETRY_ENV, "300").strip()
    try:
        value = float(raw)
    except ValueError:
        return 300.0
    return min(max(value, 0.0), 86400.0)


def _codex_app_command(
    cli: str,
    project_path: str,
    *,
    platform: Optional[str] = None,
    user_id: Optional[int] = None,
) -> list[str]:
    """Build a Desktop-session-aware command while keeping CLI semantics."""
    target_platform = platform or sys.platform
    base = [cli, "app", project_path]
    if target_platform == "darwin" and Path("/bin/launchctl").is_file():
        uid = os.getuid() if user_id is None else user_id
        return ["/bin/launchctl", "asuser", str(uid), *base]
    return base


def _now() -> float:
    return time.time()


@dataclass(frozen=True)
class SessionBinding:
    session_key: str
    session_id: str
    project_name: str
    project_path: str
    binding_mode: str
    thread_id: Optional[str] = None


class SessionProjectStore:
    """Small WAL-backed mapping store safe for gateway/MCP subprocesses."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = (
            Path(db_path).expanduser().resolve()
            if db_path is not None
            else (_hermes_home() / "state" / "codex-session-projects.sqlite3")
        )
        self._init_lock = threading.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(
            str(self.db_path), timeout=15.0, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._initialize(conn)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        return conn

    def _initialize(self, conn: sqlite3.Connection) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS channel_projects (
                    session_key TEXT PRIMARY KEY,
                    default_project_name TEXT NOT NULL,
                    default_project_path TEXT NOT NULL UNIQUE,
                    active_project_name TEXT NOT NULL,
                    active_project_path TEXT NOT NULL,
                    binding_mode TEXT NOT NULL DEFAULT 'auto',
                    created_from_session_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS session_threads (
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    thread_name TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (session_key, session_id, project_path)
                );

                CREATE INDEX IF NOT EXISTS idx_session_threads_route
                    ON session_threads(session_key, session_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS thread_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    project_path TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    replaced_at REAL NOT NULL,
                    reason TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS codex_app_project_registrations (
                    project_path TEXT PRIMARY KEY,
                    codex_cli TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_attempt_at REAL NOT NULL,
                    registered_at REAL,
                    error TEXT
                );
                """
            )
            conn.commit()
            self._initialized = True

    @staticmethod
    def _project_manifest(project_path: Path) -> Path:
        return project_path / ".hermes-dispatch.json"

    def _scaffold_default_project(
        self, project_path: Path, session_key: str, first_session_id: str
    ) -> None:
        project_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        manifest_path = self._project_manifest(project_path)
        session_key_hash = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        manifest = {
            "schema_version": 2,
            "project_name": session_key,
            "created_from_hermes_session_id": first_session_id,
            "channel_session_key_sha256": session_key_hash,
        }
        if manifest_path.exists():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(
                    f"refusing to reuse project with unreadable manifest: {project_path}"
                ) from exc
            if existing.get("channel_session_key_sha256") != session_key_hash:
                raise RuntimeError(
                    f"project directory already belongs to another channel: {project_path}"
                )
        else:
            self._write_exclusive(
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )

        agents_path = project_path / "AGENTS.md"
        if not agents_path.exists():
            self._write_exclusive(
                agents_path,
                """# Hermes Session Project

This project groups Codex threads belonging to one Hermes channel session.

- Each Hermes `session_id` is a separate Codex thread named with that id.
- Read `PROJECT_MEMORY.md` when prior project context may matter.
- Use Hermes session/message search tools for detailed historical recovery;
  do not inject the full history into every ordinary turn.
- Store only durable, reusable project facts in `PROJECT_MEMORY.md` and never
  write credentials, tokens, private identifiers, or raw chat dumps there.
""",
            )

        memory_path = project_path / "PROJECT_MEMORY.md"
        if not memory_path.exists():
            self._write_exclusive(
                memory_path,
                """# Project Memory

Durable facts shared by Codex threads in this Hermes session project.
""",
            )

    def _rewrite_project_manifest(
        self, project_path: Path, session_key: str, first_session_id: str
    ) -> None:
        manifest_path = self._project_manifest(project_path)
        session_key_hash = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError(
                f"refusing to migrate project with unreadable manifest: {project_path}"
            ) from exc
        if existing.get("channel_session_key_sha256") != session_key_hash:
            raise RuntimeError(
                f"project directory belongs to another channel: {project_path}"
            )
        updated = dict(existing)
        updated.update(
            {
                "schema_version": 2,
                "project_name": session_key,
                "created_from_hermes_session_id": (
                    existing.get("created_from_hermes_session_id") or first_session_id
                ),
                "channel_session_key_sha256": session_key_hash,
            }
        )
        if updated == existing:
            return
        temp_path = manifest_path.with_name(
            f".{manifest_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        self._write_exclusive(
            temp_path,
            json.dumps(updated, ensure_ascii=False, indent=2) + "\n",
        )
        os.replace(temp_path, manifest_path)

    def _migrate_default_project_name(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> sqlite3.Row:
        """Rename a legacy first-session-id project to its exact session key."""
        session_key = str(row["session_key"])
        desired_name = _session_key_project_name(session_key)
        desired_basename = _session_key_project_basename(session_key)
        old_path = Path(row["default_project_path"]).expanduser().resolve()
        desired_path = (old_path.parent / desired_basename).resolve()
        if (
            row["default_project_name"] == desired_name
            and old_path == desired_path
        ):
            return row

        active_was_default = Path(row["active_project_path"]).expanduser().resolve() == old_path
        moved = False
        if old_path != desired_path:
            if old_path.exists() and desired_path.exists():
                raise RuntimeError(
                    "refusing to merge legacy and session-key project directories: "
                    f"{old_path} -> {desired_path}"
                )
            if old_path.exists():
                old_path.rename(desired_path)
                moved = True
            elif not desired_path.exists():
                raise RuntimeError(
                    f"legacy Codex project directory is missing: {old_path}"
                )

        try:
            self._rewrite_project_manifest(
                desired_path,
                session_key,
                str(row["created_from_session_id"]),
            )
            conn.execute(
                """
                UPDATE channel_projects
                SET default_project_name = ?, default_project_path = ?,
                    active_project_name = CASE WHEN active_project_path = ?
                        THEN ? ELSE active_project_name END,
                    active_project_path = CASE WHEN active_project_path = ?
                        THEN ? ELSE active_project_path END,
                    updated_at = ?
                WHERE session_key = ?
                """,
                (
                    desired_name,
                    str(desired_path),
                    str(old_path),
                    desired_name,
                    str(old_path),
                    str(desired_path),
                    _now(),
                    session_key,
                ),
            )
            conn.execute(
                """UPDATE session_threads SET project_path = ?
                WHERE session_key = ? AND project_path = ?""",
                (str(desired_path), session_key, str(old_path)),
            )
            conn.execute(
                """UPDATE thread_history SET project_path = ?
                WHERE session_key = ? AND project_path = ?""",
                (str(desired_path), session_key, str(old_path)),
            )
        except Exception:
            if moved and desired_path.exists() and not old_path.exists():
                desired_path.rename(old_path)
            raise

        migrated = conn.execute(
            "SELECT * FROM channel_projects WHERE session_key = ?",
            (session_key,),
        ).fetchone()
        assert migrated is not None
        logger.info(
            "codex session project: migrated project name to session_key "
            "path=%s active_default=%s",
            desired_path,
            active_was_default,
        )
        return migrated

    @staticmethod
    def _write_exclusive(path: Path, content: str) -> None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise

    def ensure_binding(self, session_key: str, session_id: str) -> SessionBinding:
        session_key = str(session_key or "").strip()
        session_id = _safe_session_id(session_id)
        if not session_key:
            raise ValueError("Hermes session_key is required")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM channel_projects WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                project_name = _session_key_project_name(session_key)
                project_basename = _session_key_project_basename(session_key)
                project_path = (_project_root() / project_basename).resolve()
                self._scaffold_default_project(project_path, session_key, session_id)
                now = _now()
                conn.execute(
                    """
                    INSERT INTO channel_projects (
                        session_key, default_project_name, default_project_path,
                        active_project_name, active_project_path, binding_mode,
                        created_from_session_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'auto', ?, ?, ?)
                    """,
                    (
                        session_key,
                        project_name,
                        str(project_path),
                        project_name,
                        str(project_path),
                        session_id,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM channel_projects WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
            assert row is not None
            row = self._migrate_default_project_name(conn, row)
            thread = conn.execute(
                """
                SELECT thread_id FROM session_threads
                WHERE session_key = ? AND session_id = ? AND project_path = ?
                """,
                (session_key, session_id, row["active_project_path"]),
            ).fetchone()
            conn.commit()
        return SessionBinding(
            session_key=session_key,
            session_id=session_id,
            project_name=row["active_project_name"],
            project_path=row["active_project_path"],
            binding_mode=row["binding_mode"],
            thread_id=thread["thread_id"] if thread else None,
        )

    def get_binding(self, session_key: str, session_id: str) -> Optional[SessionBinding]:
        try:
            session_id = _safe_session_id(session_id)
        except ValueError:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM channel_projects WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                return None
            thread = conn.execute(
                """
                SELECT thread_id FROM session_threads
                WHERE session_key = ? AND session_id = ? AND project_path = ?
                """,
                (session_key, session_id, row["active_project_path"]),
            ).fetchone()
        return SessionBinding(
            session_key=session_key,
            session_id=session_id,
            project_name=row["active_project_name"],
            project_path=row["active_project_path"],
            binding_mode=row["binding_mode"],
            thread_id=thread["thread_id"] if thread else None,
        )

    def record_thread(self, binding: SessionBinding, thread_id: str) -> None:
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            return
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT thread_id FROM session_threads
                WHERE session_key = ? AND session_id = ? AND project_path = ?
                """,
                (binding.session_key, binding.session_id, binding.project_path),
            ).fetchone()
            if previous and previous["thread_id"] != thread_id:
                conn.execute(
                    """
                    INSERT INTO thread_history (
                        session_key, session_id, project_path, thread_id,
                        replaced_at, reason
                    ) VALUES (?, ?, ?, ?, ?, 'resume-missing-or-replaced')
                    """,
                    (
                        binding.session_key,
                        binding.session_id,
                        binding.project_path,
                        previous["thread_id"],
                        _now(),
                    ),
                )
            now = _now()
            conn.execute(
                """
                INSERT INTO session_threads (
                    session_key, session_id, project_path, thread_id,
                    thread_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_key, session_id, project_path) DO UPDATE SET
                    thread_id = excluded.thread_id,
                    thread_name = excluded.thread_name,
                    updated_at = excluded.updated_at
                """,
                (
                    binding.session_key,
                    binding.session_id,
                    binding.project_path,
                    thread_id,
                    binding.session_id,
                    now,
                    now,
                ),
            )
            conn.commit()

    def clear_thread(self, binding: SessionBinding, reason: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                """
                SELECT thread_id FROM session_threads
                WHERE session_key = ? AND session_id = ? AND project_path = ?
                """,
                (binding.session_key, binding.session_id, binding.project_path),
            ).fetchone()
            if previous:
                conn.execute(
                    """
                    INSERT INTO thread_history (
                        session_key, session_id, project_path, thread_id,
                        replaced_at, reason
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        binding.session_key,
                        binding.session_id,
                        binding.project_path,
                        previous["thread_id"],
                        _now(),
                        reason,
                    ),
                )
                conn.execute(
                    """DELETE FROM session_threads
                    WHERE session_key = ? AND session_id = ? AND project_path = ?""",
                    (binding.session_key, binding.session_id, binding.project_path),
                )
            conn.commit()

    def bind_project(
        self,
        session_key: str,
        session_id: str,
        project_name: str,
        project_path: Path,
    ) -> SessionBinding:
        """Move the channel's active project and carry the current thread id.

        Carrying the thread lets the next turn call ``thread/resume`` with the
        new cwd, preserving the user's current Codex context while moving the
        thread into the explicitly selected project.
        """
        current = self.ensure_binding(session_key, session_id)
        target = Path(project_path).expanduser().resolve()
        if not target.is_dir():
            raise ValueError(f"Codex project directory does not exist: {target}")
        name = str(project_name or target.name).strip() or target.name
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE channel_projects
                SET active_project_name = ?, active_project_path = ?,
                    binding_mode = 'user', updated_at = ?
                WHERE session_key = ?
                """,
                (name, str(target), _now(), session_key),
            )
            if current.thread_id:
                now = _now()
                conn.execute(
                    """
                    INSERT INTO session_threads (
                        session_key, session_id, project_path, thread_id,
                        thread_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_key, session_id, project_path) DO UPDATE SET
                        thread_id = excluded.thread_id,
                        thread_name = excluded.thread_name,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_key,
                        session_id,
                        str(target),
                        current.thread_id,
                        session_id,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return self.ensure_binding(session_key, session_id)

    def restore_default(self, session_key: str, session_id: str) -> SessionBinding:
        current = self.ensure_binding(session_key, session_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM channel_projects WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            assert row is not None
            conn.execute(
                """
                UPDATE channel_projects
                SET active_project_name = default_project_name,
                    active_project_path = default_project_path,
                    binding_mode = 'auto', updated_at = ?
                WHERE session_key = ?
                """,
                (_now(), session_key),
            )
            if current.thread_id:
                now = _now()
                conn.execute(
                    """
                    INSERT INTO session_threads (
                        session_key, session_id, project_path, thread_id,
                        thread_name, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_key, session_id, project_path) DO UPDATE SET
                        thread_id = excluded.thread_id,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_key,
                        session_id,
                        row["default_project_path"],
                        current.thread_id,
                        session_id,
                        now,
                        now,
                    ),
                )
            conn.commit()
        return self.ensure_binding(session_key, session_id)

    def list_threads(self, session_key: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, project_path, thread_id, thread_name,
                       created_at, updated_at
                FROM session_threads WHERE session_key = ?
                ORDER BY updated_at DESC
                """,
                (session_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def app_registration_status(self, project_path: str) -> Optional[dict[str, Any]]:
        path = str(Path(project_path).expanduser().resolve())
        with self._connect() as conn:
            row = conn.execute(
                """SELECT codex_cli, status, last_attempt_at, registered_at, error
                FROM codex_app_project_registrations WHERE project_path = ?""",
                (path,),
            ).fetchone()
        return dict(row) if row else None

    def record_app_registration(
        self,
        project_path: str,
        codex_cli: str,
        *,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        path = str(Path(project_path).expanduser().resolve())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO codex_app_project_registrations (
                    project_path, codex_cli, status, last_attempt_at,
                    registered_at, error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(project_path) DO UPDATE SET
                    codex_cli = excluded.codex_cli,
                    status = excluded.status,
                    last_attempt_at = excluded.last_attempt_at,
                    registered_at = CASE WHEN excluded.status = 'registered'
                        THEN excluded.registered_at
                        ELSE codex_app_project_registrations.registered_at END,
                    error = excluded.error
                """,
                (
                    path,
                    codex_cli,
                    "registered" if success else "failed",
                    now,
                    now if success else None,
                    error,
                ),
            )
            conn.commit()

    def record_app_registration_attempt(
        self, project_path: str, codex_cli: str
    ) -> None:
        path = str(Path(project_path).expanduser().resolve())
        now = _now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO codex_app_project_registrations (
                    project_path, codex_cli, status, last_attempt_at,
                    registered_at, error
                ) VALUES (?, ?, 'pending', ?, NULL, NULL)
                ON CONFLICT(project_path) DO UPDATE SET
                    codex_cli = excluded.codex_cli,
                    status = 'pending',
                    last_attempt_at = excluded.last_attempt_at,
                    error = NULL
                """,
                (path, codex_cli, now),
            )
            conn.commit()


_STORE: Optional[SessionProjectStore] = None
_STORE_LOCK = threading.Lock()
_APP_REGISTRATION_INFLIGHT: set[str] = set()
_APP_REGISTRATION_LOCK = threading.Lock()


def get_store() -> SessionProjectStore:
    global _STORE
    desired = (_hermes_home() / "state" / "codex-session-projects.sqlite3").resolve()
    with _STORE_LOCK:
        if _STORE is None or _STORE.db_path != desired:
            _STORE = SessionProjectStore(desired)
        return _STORE


def backfill_existing_session_projects() -> str:
    """Scaffold mappings for routes that predate this compatibility layer.

    Hermes' own ``sessions.json`` remains authoritative for the current
    session id of every QQ, WhatsApp, or other Gateway route. Backfill creates
    only the deterministic project/mapping. The corresponding Codex thread is
    started lazily by the route's next real turn, avoiding empty threads and a
    race with inbound messages during Gateway startup.
    """
    if not _backfill_enabled():
        return "legacy session backfill disabled"
    sessions_path = _hermes_home() / "sessions" / "sessions.json"
    if not sessions_path.is_file():
        return "no legacy session registry"
    try:
        payload = json.loads(sessions_path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning(
            "codex session project: could not read legacy session registry: %s",
            exc,
        )
        return "legacy session registry unreadable"
    if not isinstance(payload, dict):
        return "legacy session registry has unexpected shape"

    created = existing = skipped = 0
    store = get_store()
    for route_key, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        session_key = str(entry.get("session_key") or route_key or "").strip()
        session_id = str(entry.get("session_id") or "").strip()
        if not session_key or not session_id:
            skipped += 1
            continue
        before = store.get_binding(session_key, session_id)
        try:
            store.ensure_binding(session_key, session_id)
        except Exception:
            skipped += 1
            logger.warning(
                "codex session project: legacy route backfill failed key_hash=%s",
                hashlib.sha256(session_key.encode("utf-8")).hexdigest(),
                exc_info=True,
            )
            continue
        if before is None:
            created += 1
        else:
            existing += 1
    logger.info(
        "codex session project: legacy route backfill created=%d existing=%d skipped=%d",
        created,
        existing,
        skipped,
    )
    return f"legacy projects backfilled={created}, existing={existing}, skipped={skipped}"


def _extract_thread_id(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    thread = payload.get("thread") or {}
    if not isinstance(thread, dict):
        thread = {}
    value = (
        thread.get("id")
        or thread.get("sessionId")
        or payload.get("sessionId")
        or payload.get("threadId")
    )
    return str(value) if value else None


def _resume_is_missing(exc: Exception) -> bool:
    text = str(getattr(exc, "message", exc) or "").lower()
    if "method not found" in text:
        return False
    if any(
        hint in text
        for hint in (
            "thread not found",
            "unknown thread",
            "no rollout found",
            "rollout not found",
        )
    ):
        return True
    return (
        ("thread" in text or "rollout" in text)
        and "does not exist" in text
    )


def _set_thread_name_best_effort(session: Any, binding: SessionBinding) -> None:
    try:
        session._client.request(
            "thread/name/set",
            {"threadId": session._thread_id, "name": binding.session_id},
            timeout=10,
        )
    except Exception:
        logger.debug(
            "codex session project: thread/name/set unavailable for %s",
            binding.session_id,
            exc_info=True,
        )


def _run_codex_app_registration(binding: SessionBinding, cli: str) -> None:
    """Run one detached registration worker without holding an Agent turn."""
    store = get_store()
    try:
        completed = subprocess.run(
            _codex_app_command(cli, binding.project_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_register_app_timeout(),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"codex app exited {completed.returncode}")
        store.record_app_registration(binding.project_path, cli, success=True)
        logger.info(
            "codex session project: registered Codex App project=%s path=%s",
            binding.project_name,
            binding.project_path,
        )
    except Exception as exc:
        error = str(exc)[:2000]
        try:
            store.record_app_registration(
                binding.project_path, cli, success=False, error=error
            )
        except Exception:
            logger.debug(
                "codex session project: could not persist App registration failure",
                exc_info=True,
            )
        logger.warning(
            "codex session project: Codex App registration failed for %s: %s",
            binding.project_path,
            error,
        )
    finally:
        with _APP_REGISTRATION_LOCK:
            _APP_REGISTRATION_INFLIGHT.discard(binding.project_path)


def _register_project_with_codex_app_best_effort(
    binding: SessionBinding, codex_bin: Any
) -> None:
    """Schedule Codex Desktop registration without delaying the Agent turn.

    This is opt-in because the command may launch/focus the Desktop app and is
    not appropriate inside a headless container. Success is persisted so later
    turns and Gateway restarts do not relaunch the app for the same path.
    """
    if not _register_app_enabled():
        return
    store = get_store()
    previous = store.app_registration_status(binding.project_path)
    if previous and previous.get("status") == "registered":
        return
    if previous and previous.get("status") in {"pending", "failed"}:
        elapsed = _now() - float(previous.get("last_attempt_at") or 0)
        if elapsed < _register_app_retry_seconds():
            return

    configured = os.environ.get(REGISTER_APP_CLI_ENV, "").strip()
    cli = configured or str(codex_bin or "codex").strip() or "codex"
    with _APP_REGISTRATION_LOCK:
        if binding.project_path in _APP_REGISTRATION_INFLIGHT:
            return
        _APP_REGISTRATION_INFLIGHT.add(binding.project_path)
    try:
        store.record_app_registration_attempt(binding.project_path, cli)
        worker = threading.Thread(
            target=_run_codex_app_registration,
            args=(binding, cli),
            name=f"codex-app-register-{hashlib.sha256(binding.project_path.encode()).hexdigest()[:8]}",
            daemon=True,
        )
        worker.start()
        logger.info(
            "codex session project: scheduled Codex App registration project=%s",
            binding.project_name,
        )
    except Exception as exc:
        with _APP_REGISTRATION_LOCK:
            _APP_REGISTRATION_INFLIGHT.discard(binding.project_path)
        logger.warning(
            "codex session project: could not schedule Codex App registration for %s: %s",
            binding.project_path,
            exc,
        )


def wrap_session_init(original: Callable) -> Callable:
    @functools.wraps(original)
    def init(self, *args, **kwargs):
        binding = _CURRENT_BINDING.get()
        if binding is not None:
            kwargs["cwd"] = binding.project_path
        original(self, *args, **kwargs)
        if binding is not None:
            self._dispatch_session_binding = binding
            self._dispatch_resume_thread_id = binding.thread_id

    setattr(init, _INIT_MARKER, True)
    init._codex_session_project_original = original
    return init


def wrap_ensure_started(original: Callable) -> Callable:
    """Replace 0.20.0's start-only handshake with resume-or-start."""

    @functools.wraps(original)
    def ensure_started(self):
        binding = getattr(self, "_dispatch_session_binding", None)
        if binding is None:
            return original(self)
        if self._thread_id is not None:
            return self._thread_id

        if self._client is None:
            self._client = self._client_factory(
                codex_bin=self._codex_bin, codex_home=self._codex_home
            )
        from agent.transports import codex_app_server_session as session_module

        self._client.initialize(
            client_name="hermes",
            client_title="Hermes Agent",
            client_version=session_module._get_hermes_version(),
        )

        thread_id = None
        resume_id = getattr(self, "_dispatch_resume_thread_id", None)
        if resume_id:
            _retire_idle_thread_owner(str(resume_id), self)
            try:
                resumed = self._client.request(
                    "thread/resume",
                    {"threadId": resume_id, "cwd": binding.project_path},
                    timeout=15,
                )
                thread_id = _extract_thread_id(resumed) or str(resume_id)
                logger.info(
                    "codex session project: resumed thread=%s session=%s project=%s",
                    thread_id[:8],
                    binding.session_id,
                    binding.project_name,
                )
            except Exception as exc:
                if not _resume_is_missing(exc):
                    raise
                logger.warning(
                    "codex session project: mapped thread %s is missing; "
                    "starting a replacement in project %s",
                    str(resume_id)[:8],
                    binding.project_name,
                )
                get_store().clear_thread(binding, "codex-thread-missing")

        if thread_id is None:
            started = self._client.request(
                "thread/start", {"cwd": binding.project_path}, timeout=15
            )
            thread_id = _extract_thread_id(started)
            if not thread_id:
                from agent.transports.codex_app_server import CodexAppServerError

                raise CodexAppServerError(
                    code=-32603,
                    message=(
                        "codex thread/start returned no thread id "
                        f"(payload keys: {sorted(started.keys())})"
                    ),
                )
            logger.info(
                "codex session project: started thread=%s session=%s project=%s cwd=%s",
                thread_id[:8],
                binding.session_id,
                binding.project_name,
                binding.project_path,
            )

        self._thread_id = thread_id
        self._dispatch_resume_thread_id = thread_id
        _claim_thread_owner(thread_id, self)
        get_store().record_thread(binding, thread_id)
        _set_thread_name_best_effort(self, binding)
        _register_project_with_codex_app_best_effort(binding, self._codex_bin)
        return thread_id

    setattr(ensure_started, _ENSURE_MARKER, True)
    ensure_started._codex_session_project_original = original
    return ensure_started


def wrap_codex_runtime_turn(original: Callable) -> Callable:
    """Bind the current AIAgent to its durable project before session init."""

    @functools.wraps(original)
    def run(agent, *args, **kwargs):
        if not _enabled():
            return original(agent, *args, **kwargs)
        session_key = str(getattr(agent, "_gateway_session_key", "") or "").strip()
        session_id = str(getattr(agent, "session_id", "") or "").strip()
        if not session_key or not session_id:
            return original(agent, *args, **kwargs)

        store = get_store()
        binding = store.ensure_binding(session_key, session_id)
        live = getattr(agent, "_codex_session", None)
        live_binding = getattr(live, "_dispatch_session_binding", None)
        if live is not None and (
            live_binding is None
            or live_binding.session_id != binding.session_id
            or live_binding.project_path != binding.project_path
        ):
            try:
                live.close()
            finally:
                agent._codex_session = None

        token = _CURRENT_BINDING.set(binding)
        try:
            result = original(agent, *args, **kwargs)
        finally:
            _CURRENT_BINDING.reset(token)

        if isinstance(result, dict) and result.get("codex_thread_id"):
            store.record_thread(binding, str(result["codex_thread_id"]))

        # A project-binding tool may have changed the mapping from the MCP
        # subprocess during this very turn. Retire only this session's client
        # after its result is complete; the next message resumes the same
        # thread with the new cwd.
        latest = store.get_binding(session_key, session_id)
        if latest is not None and latest.project_path != binding.project_path:
            live = getattr(agent, "_codex_session", None)
            try:
                if live is not None:
                    live.close()
            finally:
                agent._codex_session = None
        return result

    setattr(run, _RUNTIME_MARKER, True)
    run._codex_session_project_original = original
    return run


def _runtime_has_native_mapping(runtime_fn: Callable, ensure_fn: Callable) -> bool:
    try:
        runtime_source = inspect.getsource(runtime_fn)
        ensure_source = inspect.getsource(ensure_fn)
    except (OSError, TypeError):
        return False
    return (
        "codex_thread_id" in runtime_source
        and "thread/resume" in ensure_source
        and "gateway_session_key" in ensure_source
    )


def patch_codex_session_projects() -> str:
    """Patch the installed Hermes runtime once, skipping an upstream equivalent."""
    if not _enabled():
        return "disabled by environment"

    from agent import codex_runtime
    from agent.transports.codex_app_server_session import CodexAppServerSession

    runtime_fn = codex_runtime.run_codex_app_server_turn
    ensure_fn = CodexAppServerSession.ensure_started
    if _runtime_has_native_mapping(runtime_fn, ensure_fn):
        return "upstream already persists channel project/thread mappings; skipped"

    statuses = []
    if getattr(CodexAppServerSession.__init__, _INIT_MARKER, False):
        statuses.append("init already patched")
    else:
        CodexAppServerSession.__init__ = wrap_session_init(
            CodexAppServerSession.__init__
        )
        statuses.append("project cwd patched")

    if getattr(CodexAppServerSession.ensure_started, _ENSURE_MARKER, False):
        statuses.append("resume already patched")
    else:
        CodexAppServerSession.ensure_started = wrap_ensure_started(
            CodexAppServerSession.ensure_started
        )
        statuses.append("thread resume patched")

    if getattr(codex_runtime.run_codex_app_server_turn, _RUNTIME_MARKER, False):
        statuses.append("runtime already patched")
    else:
        codex_runtime.run_codex_app_server_turn = wrap_codex_runtime_turn(
            codex_runtime.run_codex_app_server_turn
        )
        statuses.append("session mapping patched")
    statuses.append(backfill_existing_session_projects())
    return ", ".join(statuses)


def _aliases() -> dict[str, str]:
    raw = os.environ.get(ALIASES_ENV, "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; ignoring project aliases", ALIASES_ENV)
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        str(name).strip(): str(path).strip()
        for name, path in value.items()
        if str(name).strip() and str(path).strip()
    }


def _allowed_roots() -> list[Path]:
    raw = os.environ.get(ALLOWED_ROOTS_ENV, "").strip()
    roots = []
    for value in raw.split(os.pathsep) if raw else []:
        value = value.strip()
        if value:
            roots.append(Path(value).expanduser().resolve())
    # Alias targets are explicit operator configuration and therefore allowed.
    for value in _aliases().values():
        path = Path(value).expanduser().resolve()
        if path not in roots:
            roots.append(path)
    return roots


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_project_target(value: str) -> tuple[str, Path]:
    target = str(value or "").strip()
    if not target:
        raise ValueError("project alias or path is required")
    aliases = _aliases()
    if target in aliases:
        path = Path(aliases[target]).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"configured project does not exist: {target}")
        return target, path

    path = Path(target).expanduser().resolve()
    roots = _allowed_roots()
    if not roots or not any(_within(path, root) for root in roots):
        raise PermissionError(
            "project path is outside HERMES_CODEX_PROJECT_ALLOWED_ROOTS; "
            "configure an alias or allowed root first"
        )
    if not path.is_dir():
        raise ValueError(f"Codex project directory does not exist: {path}")
    return path.name, path


def _current_identity() -> tuple[str, str, str]:
    session_key = session_id = user_id = ""
    try:
        from gateway.session_context import get_session_env

        session_key = get_session_env("HERMES_SESSION_KEY", "")
        session_id = get_session_env("HERMES_SESSION_ID", "")
        user_id = get_session_env("HERMES_SESSION_USER_ID", "")
    except Exception:
        session_key = os.environ.get("HERMES_SESSION_KEY", "")
        session_id = os.environ.get("HERMES_SESSION_ID", "")
        user_id = os.environ.get("HERMES_SESSION_USER_ID", "")

    # Hermes 0.20.0 clears the gateway session ContextVars at the start of
    # every inbound message, then dispatches plugin slash commands before its
    # normal Agent path calls _set_session_env().  The channel-neutral
    # pre-dispatch hook below captures the same routing identity for that
    # narrow window.  Normal tool calls continue to use the official Hermes
    # session context above.
    command_context = _CHANNEL_COMMAND_CONTEXT.get()
    if command_context:
        # For a plugin slash command the current inbound Gateway route is
        # authoritative. At this pre-Agent stage Hermes' ContextVars are
        # _UNSET, so get_session_env() falls back to process-global
        # os.environ. A prior Agent can leave its old session_id there after
        # /new, and concurrent channels can leave another route entirely.
        # Never merge that stale process fallback into the captured command
        # route. Normal Agent tool calls have no command_context and continue
        # using the official task-local values above.
        session_key = str(command_context.get("session_key") or "")
        session_id = str(command_context.get("session_id") or "")
        user_id = str(command_context.get("user_id") or "")
    return session_key, session_id, user_id


def _capture_channel_command_context(
    *, event: Any = None, gateway: Any = None, session_store: Any = None, **_: Any
) -> None:
    """Capture a plugin slash command's route before Agent context is bound.

    This hook uses Hermes' own platform-neutral session-key resolver, so QQ,
    WhatsApp and every other Gateway adapter receive identical semantics.
    It performs no writes and does not bypass Hermes authorization.
    """
    _CHANNEL_COMMAND_CONTEXT.set(None)
    if event is None or gateway is None:
        return None
    try:
        command = str(event.get_command() or "").replace("_", "-").lower()
    except Exception:
        return None
    if command != "codex-project":
        return None
    source = getattr(event, "source", None)
    if source is None:
        return None
    try:
        session_key = gateway._session_key_for_source(source)
    except Exception:
        logger.debug("Could not resolve channel session key for /codex-project", exc_info=True)
        return None

    store = session_store or getattr(gateway, "session_store", None)
    entry = None
    if store is not None:
        try:
            # This read occurs on the Gateway event-loop task before command
            # dispatch. SessionStore owns the mapping and replaces entries
            # atomically, so a single reference read is sufficient here.
            entry = (getattr(store, "_entries", {}) or {}).get(session_key)
        except Exception:
            entry = None
    _CHANNEL_COMMAND_CONTEXT.set(
        {
            "session_key": session_key,
            "session_id": str(getattr(entry, "session_id", "") or ""),
            "user_id": str(getattr(source, "user_id", "") or ""),
            "source": source,
            "gateway": gateway,
        }
    )
    return None


async def _ensure_channel_command_session() -> None:
    """Create the normal Hermes route if /codex-project is the first message."""
    command_context = _CHANNEL_COMMAND_CONTEXT.get()
    if not command_context or command_context.get("session_id"):
        return
    gateway = command_context.get("gateway")
    source = command_context.get("source")
    if gateway is None or source is None:
        return
    async_store = getattr(gateway, "async_session_store", None)
    if async_store is None:
        return
    # The handler is reached only after the Gateway's existing authorization
    # checks. Use the same async store path as a normal channel message.
    entry = await async_store.get_or_create_session(source)
    updated = dict(command_context)
    updated["session_id"] = str(getattr(entry, "session_id", "") or "")
    _CHANNEL_COMMAND_CONTEXT.set(updated)


def _admin_allowed(user_id: str) -> bool:
    configured = {
        value.strip()
        for value in os.environ.get(ADMIN_USERS_ENV, "").split(",")
        if value.strip()
    }
    return bool(configured) and ("*" in configured or user_id in configured)


def _json_result(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _public_binding(
    binding: SessionBinding, *, reveal_path: bool = False
) -> dict[str, Any]:
    """Return management metadata without exposing a raw channel identifier."""
    result = {
        "session_key_sha256": hashlib.sha256(
            binding.session_key.encode("utf-8")
        ).hexdigest(),
        "session_id": binding.session_id,
        "project_name": binding.project_name,
        "binding_mode": binding.binding_mode,
        "thread_id": binding.thread_id,
    }
    if reveal_path:
        result["project_path"] = binding.project_path
    return result


def register_session_project_interfaces(ctx: Any) -> None:
    """Expose prompt-callable status/bind/list operations and a slash command."""
    schema = {
        "name": "codex_session_project",
        "description": (
            "Inspect or change the Codex project associated with the current "
            "Hermes channel session. Use bind/default only after an explicit "
            "human request; project changes require an authorized user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "list_threads", "bind", "default"],
                },
                "project": {
                    "type": "string",
                    "description": "Configured project alias or allowed absolute path.",
                },
            },
            "required": ["action"],
        },
    }

    def handler(args: dict, **_: Any) -> str:
        session_key, session_id, user_id = _current_identity()
        if not session_key or not session_id:
            return _json_result({"error": "no active Hermes channel session"})
        store = get_store()
        action = str((args or {}).get("action") or "status").strip().lower()
        try:
            binding = store.ensure_binding(session_key, session_id)
            is_admin = _admin_allowed(user_id)
            if action == "status":
                registration = store.app_registration_status(binding.project_path)
                return _json_result(
                    {
                        "binding": _public_binding(binding, reveal_path=is_admin),
                        "codex_app_registration": (
                            registration
                            if is_admin and registration is not None
                            else {
                                "enabled": _register_app_enabled(),
                                "status": (
                                    registration.get("status")
                                    if registration is not None
                                    else "not_attempted"
                                ),
                            }
                        ),
                    }
                )
            if action == "list_threads":
                threads = store.list_threads(session_key)
                if not is_admin:
                    threads = [
                        {key: value for key, value in row.items() if key != "project_path"}
                        for row in threads
                    ]
                return _json_result({"threads": threads})
            if action in {"bind", "default"} and not is_admin:
                return _json_result(
                    {
                        "error": (
                            "project changes are restricted; add this user to "
                            f"{ADMIN_USERS_ENV}"
                        )
                    }
                )
            if action == "default":
                updated = store.restore_default(session_key, session_id)
            elif action == "bind":
                name, path = resolve_project_target(
                    str((args or {}).get("project") or "")
                )
                updated = store.bind_project(
                    session_key, session_id, name, path
                )
            else:
                return _json_result({"error": f"unknown action: {action}"})
            return _json_result(
                {
                    "binding": _public_binding(updated, reveal_path=True),
                    "effective": "next Codex turn",
                }
            )
        except Exception as exc:
            return _json_result({"error": str(exc)})

    ctx.register_tool(
        name="codex_session_project",
        toolset="codex_session_project",
        schema=schema,
        handler=handler,
        description=schema["description"],
        emoji="🗂️",
    )

    ctx.register_hook("pre_gateway_dispatch", _capture_channel_command_context)

    async def command(raw_args: str) -> str:
        await _ensure_channel_command_session()
        parts = str(raw_args or "").strip().split(maxsplit=1)
        action = parts[0].lower() if parts else "status"
        project = parts[1] if len(parts) > 1 else ""
        return handler({"action": action, "project": project})

    ctx.register_command(
        "codex-project",
        command,
        description="Inspect or bind the current Hermes session's Codex project.",
        args_hint="status|list_threads|bind <alias-or-path>|default",
    )
