"""SQLite-backed immutable event snapshots and hybrid message retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from .config import SnapshotConfig


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback normally, then release SQLite locks on Windows."""

    def __exit__(self, exc_type, exc, traceback):
        try:
            return super().__exit__(exc_type, exc, traceback)
        finally:
            self.close()


SCHEMA_VERSION = 1


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "value"):
        return value.value
    if hasattr(value, "__dict__"):
        return vars(value)
    return repr(value)


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "")


def _safe_scalar(value: Any) -> tuple[str, str] | None:
    if value is None:
        return "null", ""
    if isinstance(value, bool):
        return "boolean", "true" if value else "false"
    if isinstance(value, (int, float)):
        return "number", str(value)
    if isinstance(value, str):
        return "string", value
    return None


def flatten_scalars(value: Any, prefix: str = "") -> Iterable[tuple[str, str, str]]:
    """Yield stable JSON paths and scalar values for exact indexed lookup."""
    scalar = _safe_scalar(value)
    if scalar is not None:
        if prefix:
            yield prefix, scalar[0], scalar[1]
        return
    if isinstance(value, dict):
        for key in sorted(value, key=lambda item: str(item)):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from flatten_scalars(value[key], child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from flatten_scalars(item, child)


def estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for bounded context."""
    cjk = len(re.findall(r"[\u3400-\u9fff\uf900-\ufaff]", text))
    remainder = re.sub(r"[\u3400-\u9fff\uf900-\ufaff]", "", text)
    words = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", remainder))
    ascii_chars = len(re.findall(r"[A-Za-z0-9_]", remainder))
    return max(1, cjk + words + math.ceil(ascii_chars / 4))


def _ngrams(text: str, size: int = 2) -> set[str]:
    clean = "".join(str(text).casefold().split())
    if len(clean) <= size:
        return {clean} if clean else set()
    return {clean[i : i + size] for i in range(len(clean) - size + 1)}


def _fuzzy_score(query: str, text: str) -> float:
    if not query or not text:
        return 0.0
    qgrams = _ngrams(query)
    tgrams = _ngrams(text)
    jaccard = len(qgrams & tgrams) / max(1, len(qgrams | tgrams))
    sequence = SequenceMatcher(None, query.casefold(), text.casefold()).ratio()
    return 0.65 * jaccard + 0.35 * sequence


class SnapshotStore:
    def __init__(self, config: SnapshotConfig):
        self.config = config

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.config.db_path, timeout=30.0, factory=_ClosingConnection
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def initialize(self) -> None:
        for path in (
            self.config.root_dir,
            self.config.media_dir,
            self.config.restore_dir,
            self.config.db_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)
            try:
                path.chmod(0o700)
            except OSError:
                pass

        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    profile TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    event_type TEXT NOT NULL DEFAULT '',
                    message_id TEXT NOT NULL DEFAULT '',
                    chat_id TEXT NOT NULL DEFAULT '',
                    chat_type TEXT NOT NULL DEFAULT '',
                    sender_id TEXT NOT NULL DEFAULT '',
                    sender_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL DEFAULT '',
                    message_kind TEXT NOT NULL DEFAULT '',
                    reply_to_message_id TEXT NOT NULL DEFAULT '',
                    occurred_at TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    normalized_at TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS raw_events (
                    id INTEGER PRIMARY KEY,
                    message_snapshot_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL DEFAULT '',
                    captured_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    raw_sha256 TEXT NOT NULL,
                    UNIQUE(message_snapshot_id, event_type, raw_sha256)
                );

                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY,
                    message_snapshot_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    content_type TEXT NOT NULL DEFAULT '',
                    filename TEXT NOT NULL DEFAULT '',
                    remote_url TEXT NOT NULL DEFAULT '',
                    remote_id TEXT NOT NULL DEFAULT '',
                    declared_size INTEGER,
                    local_path TEXT NOT NULL DEFAULT '',
                    archive_path TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '',
                    byte_size INTEGER,
                    archive_status TEXT NOT NULL DEFAULT 'pending',
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(message_snapshot_id, ordinal)
                );

                CREATE TABLE IF NOT EXISTS message_values (
                    message_snapshot_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                    field_path TEXT NOT NULL,
                    value_type TEXT NOT NULL,
                    value_text TEXT NOT NULL,
                    PRIMARY KEY(message_snapshot_id, field_path, value_text)
                );

                CREATE INDEX IF NOT EXISTS idx_messages_message_id ON messages(message_id);
                CREATE INDEX IF NOT EXISTS idx_messages_chat_time ON messages(platform, chat_id, occurred_at, id);
                CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_id, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_messages_kind ON messages(message_kind, occurred_at);
                CREATE INDEX IF NOT EXISTS idx_attachments_filename ON attachments(filename);
                CREATE INDEX IF NOT EXISTS idx_attachments_remote_id ON attachments(remote_id);
                CREATE INDEX IF NOT EXISTS idx_attachments_remote_url ON attachments(remote_url);
                CREATE INDEX IF NOT EXISTS idx_attachments_sha256 ON attachments(sha256);
                CREATE INDEX IF NOT EXISTS idx_attachments_type ON attachments(content_type);
                CREATE INDEX IF NOT EXISTS idx_values_path_value ON message_values(field_path, value_text);
                CREATE INDEX IF NOT EXISTS idx_values_value ON message_values(value_text);
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(
                        snapshot_id UNINDEXED,
                        content,
                        attachment_text,
                        value_text,
                        tokenize='unicode61 remove_diacritics 2'
                    )
                    """
                )
                fts = "1"
            except sqlite3.OperationalError:
                fts = "0"
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES('fts5', ?)",
                (fts,),
            )
            self._backfill_embedded_cache_paths(conn)

        try:
            self.config.db_path.chmod(0o600)
        except OSError:
            pass

    def _has_fts(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT value FROM schema_metadata WHERE key='fts5'"
        ).fetchone()
        return bool(row and row[0] == "1")

    def _backfill_embedded_cache_paths(self, conn: sqlite3.Connection) -> None:
        """Upgrade older quote snapshots whose cache path lived only in text."""
        rows = conn.execute(
            """
            SELECT DISTINCT m.id, m.content
            FROM messages m
            JOIN attachments a ON a.message_snapshot_id=m.id
            WHERE a.archive_status='pending'
              AND (m.content LIKE '%[file:%' OR m.content LIKE '%[video:%')
            """
        ).fetchall()
        for row in rows:
            used: set[int] = set()
            changed = False
            for kind, display_name, local_path in _iter_embedded_local_paths(row["content"]):
                ordinal = self._match_attachment_ordinal(
                    conn,
                    int(row["id"]),
                    preferred_name=display_name,
                    content_type="video/" if kind == "video" else "",
                    used=used,
                )
                used.add(ordinal)
                self._archive_local_attachment(
                    conn,
                    snapshot_id=int(row["id"]),
                    ordinal=ordinal,
                    local_path=local_path,
                    content_type="",
                )
                changed = True
            if changed:
                self._refresh_fts(conn, int(row["id"]))

    @staticmethod
    def _raw_parts(platform: str, event_type: str, raw: dict[str, Any]) -> dict[str, str]:
        if platform == "whatsapp":
            content = str(raw.get("body") or "")
            attachments = list(_iter_raw_attachments(raw))
            return {
                "platform": platform,
                "event_type": event_type,
                "message_id": str(raw.get("messageId") or raw.get("id") or ""),
                "chat_id": str(raw.get("chatId") or ""),
                "chat_type": "group" if raw.get("isGroup") else "dm",
                "sender_id": str(raw.get("senderId") or ""),
                "sender_name": str(raw.get("senderName") or ""),
                "content": content,
                "message_kind": _message_kind(content, attachments),
                "reply_to_message_id": str(raw.get("quotedMessageId") or ""),
                "occurred_at": str(raw.get("timestamp") or ""),
            }
        author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
        message_id = str(raw.get("id") or raw.get("message_id") or "")
        chat_id = str(
            raw.get("group_openid")
            or raw.get("channel_id")
            or raw.get("guild_id")
            or author.get("user_openid")
            or raw.get("openid")
            or ""
        )
        sender_id = str(
            author.get("member_openid")
            or author.get("user_openid")
            or author.get("id")
            or ""
        )
        sender_name = str(author.get("username") or author.get("nick") or "")
        if raw.get("group_openid"):
            chat_type = "group"
        elif raw.get("channel_id"):
            chat_type = "guild"
        else:
            chat_type = "dm"
        content = str(raw.get("content") or "")
        attachments = list(_iter_raw_attachments(raw))
        kind = _message_kind(content, attachments)
        return {
            "platform": platform,
            "event_type": event_type,
            "message_id": message_id,
            "chat_id": chat_id,
            "chat_type": chat_type,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "content": content,
            "message_kind": kind,
            "reply_to_message_id": str(raw.get("reply_to_message_id") or ""),
            "occurred_at": str(raw.get("timestamp") or raw.get("event_ts") or ""),
        }

    def record_raw(
        self,
        *,
        profile: str,
        platform: str,
        event_type: str,
        raw: Any,
    ) -> int:
        payload = raw if isinstance(raw, dict) else {"value": raw}
        raw_json = canonical_json(payload)
        raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        parts = self._raw_parts(platform, event_type, payload)
        stable = parts["message_id"] or raw_sha
        dedupe_key = "\x1f".join((platform, profile, parts["chat_id"], stable))
        now = utc_now()

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO messages(
                    dedupe_key, profile, platform, event_type, message_id,
                    chat_id, chat_type, sender_id, sender_name, content,
                    message_kind, reply_to_message_id, occurred_at,
                    captured_at, updated_at, raw_json, raw_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    event_type=CASE WHEN excluded.event_type<>'' THEN excluded.event_type ELSE messages.event_type END,
                    content=CASE WHEN excluded.content<>'' THEN excluded.content ELSE messages.content END,
                    message_kind=CASE WHEN excluded.message_kind<>'' THEN excluded.message_kind ELSE messages.message_kind END,
                    sender_id=CASE WHEN excluded.sender_id<>'' THEN excluded.sender_id ELSE messages.sender_id END,
                    sender_name=CASE WHEN excluded.sender_name<>'' THEN excluded.sender_name ELSE messages.sender_name END,
                    occurred_at=CASE WHEN excluded.occurred_at<>'' THEN excluded.occurred_at ELSE messages.occurred_at END,
                    updated_at=excluded.updated_at,
                    raw_json=excluded.raw_json,
                    raw_sha256=excluded.raw_sha256
                """,
                (
                    dedupe_key,
                    profile,
                    platform,
                    parts["event_type"],
                    parts["message_id"],
                    parts["chat_id"],
                    parts["chat_type"],
                    parts["sender_id"],
                    parts["sender_name"],
                    parts["content"],
                    parts["message_kind"],
                    parts["reply_to_message_id"],
                    parts["occurred_at"],
                    now,
                    now,
                    raw_json,
                    raw_sha,
                ),
            )
            snapshot_id = int(
                conn.execute("SELECT id FROM messages WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO raw_events(
                    message_snapshot_id, event_type, captured_at, raw_json, raw_sha256
                ) VALUES(?,?,?,?,?)
                """,
                (snapshot_id, event_type, now, raw_json, raw_sha),
            )
            self._replace_values(conn, snapshot_id, payload)
            self._upsert_raw_attachments(
                conn,
                snapshot_id,
                payload,
                force_mirror=platform == "whatsapp",
            )
            self._refresh_fts(conn, snapshot_id)
            conn.commit()
        return snapshot_id

    def record_normalized(self, event: Any, profile: str = "default") -> int:
        source = getattr(event, "source", None)
        raw = getattr(event, "raw_message", None)
        payload = raw if isinstance(raw, dict) else {"raw": raw}
        platform = _enum_value(getattr(source, "platform", "unknown")) or "unknown"
        message_id = str(getattr(event, "message_id", "") or "")
        chat_id = str(getattr(source, "chat_id", "") or "")
        raw_json = canonical_json(payload)
        raw_sha = hashlib.sha256(raw_json.encode("utf-8")).hexdigest()
        stable = message_id or raw_sha
        dedupe_key = "\x1f".join((platform, profile, chat_id, stable))
        now = utc_now()
        occurred = getattr(event, "timestamp", None)
        occurred_at = _json_default(occurred) if occurred else ""
        message_kind = _enum_value(getattr(event, "message_type", ""))

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO messages(
                    dedupe_key, profile, platform, message_id, chat_id,
                    chat_type, sender_id, sender_name, content, message_kind,
                    reply_to_message_id, occurred_at, captured_at, updated_at,
                    normalized_at, raw_json, raw_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    chat_type=excluded.chat_type,
                    sender_id=excluded.sender_id,
                    sender_name=excluded.sender_name,
                    content=CASE WHEN excluded.content<>'' THEN excluded.content ELSE messages.content END,
                    message_kind=CASE WHEN excluded.message_kind<>'' THEN excluded.message_kind ELSE messages.message_kind END,
                    reply_to_message_id=excluded.reply_to_message_id,
                    occurred_at=CASE WHEN excluded.occurred_at<>'' THEN excluded.occurred_at ELSE messages.occurred_at END,
                    updated_at=excluded.updated_at,
                    normalized_at=excluded.normalized_at,
                    raw_json=excluded.raw_json,
                    raw_sha256=excluded.raw_sha256
                """,
                (
                    dedupe_key,
                    profile,
                    platform,
                    message_id,
                    chat_id,
                    str(getattr(source, "chat_type", "") or ""),
                    str(getattr(source, "user_id", "") or ""),
                    str(getattr(source, "user_name", "") or ""),
                    str(getattr(event, "text", "") or ""),
                    message_kind,
                    str(getattr(event, "reply_to_message_id", "") or ""),
                    str(occurred_at),
                    now,
                    now,
                    now,
                    raw_json,
                    raw_sha,
                ),
            )
            snapshot_id = int(
                conn.execute("SELECT id FROM messages WHERE dedupe_key=?", (dedupe_key,)).fetchone()[0]
            )
            self._replace_values(conn, snapshot_id, payload)
            media_urls = list(getattr(event, "media_urls", None) or [])
            media_types = list(getattr(event, "media_types", None) or [])
            used_ordinals: set[int] = set()
            for index, local_path in enumerate(media_urls):
                media_type = media_types[index] if index < len(media_types) else ""
                ordinal = self._match_attachment_ordinal(
                    conn,
                    snapshot_id,
                    preferred_name="",
                    content_type=str(media_type),
                    used=used_ordinals,
                )
                used_ordinals.add(ordinal)
                self._archive_local_attachment(
                    conn,
                    snapshot_id=snapshot_id,
                    ordinal=ordinal,
                    local_path=str(local_path),
                    content_type=str(media_type),
                    force_mirror=platform == "whatsapp",
                )
            for kind, display_name, local_path in _iter_embedded_local_paths(
                str(getattr(event, "text", "") or "")
            ):
                ordinal = self._match_attachment_ordinal(
                    conn,
                    snapshot_id,
                    preferred_name=display_name,
                    content_type="video/" if kind == "video" else "",
                    used=used_ordinals,
                )
                used_ordinals.add(ordinal)
                self._archive_local_attachment(
                    conn,
                    snapshot_id=snapshot_id,
                    ordinal=ordinal,
                    local_path=local_path,
                    content_type="",
                    force_mirror=platform == "whatsapp",
                )
            self._refresh_fts(conn, snapshot_id)
            conn.commit()
        return snapshot_id

    @staticmethod
    def _match_attachment_ordinal(
        conn: sqlite3.Connection,
        snapshot_id: int,
        *,
        preferred_name: str,
        content_type: str,
        used: set[int],
    ) -> int:
        rows = conn.execute(
            "SELECT ordinal, filename, content_type FROM attachments WHERE message_snapshot_id=? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        available = [row for row in rows if int(row["ordinal"]) not in used]
        if preferred_name:
            for row in available:
                if row["filename"] == preferred_name or Path(row["filename"]).name == Path(preferred_name).name:
                    return int(row["ordinal"])
        media_prefix = str(content_type or "").split("/", 1)[0]
        if media_prefix:
            for row in available:
                if str(row["content_type"] or "").startswith(media_prefix + "/"):
                    return int(row["ordinal"])
        if available:
            return int(available[0]["ordinal"])
        return max(used, default=-1) + 1

    def _replace_values(self, conn: sqlite3.Connection, snapshot_id: int, raw: Any) -> None:
        conn.execute("DELETE FROM message_values WHERE message_snapshot_id=?", (snapshot_id,))
        rows = [
            (snapshot_id, path[:512], value_type, value[:8192])
            for path, value_type, value in flatten_scalars(raw)
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO message_values(message_snapshot_id, field_path, value_type, value_text) VALUES(?,?,?,?)",
            rows,
        )

    def _upsert_raw_attachments(
        self,
        conn: sqlite3.Connection,
        snapshot_id: int,
        raw: Any,
        *,
        force_mirror: bool = False,
    ) -> None:
        for ordinal, attachment in enumerate(_iter_raw_attachments(raw)):
            local_path = str(attachment.get("local_path") or "")
            conn.execute(
                """
                INSERT INTO attachments(
                    message_snapshot_id, ordinal, content_type, filename,
                    remote_url, remote_id, declared_size, local_path, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(message_snapshot_id, ordinal) DO UPDATE SET
                    content_type=CASE WHEN excluded.content_type<>'' THEN excluded.content_type ELSE attachments.content_type END,
                    filename=CASE WHEN excluded.filename<>'' THEN excluded.filename ELSE attachments.filename END,
                    remote_url=CASE WHEN excluded.remote_url<>'' THEN excluded.remote_url ELSE attachments.remote_url END,
                    remote_id=CASE WHEN excluded.remote_id<>'' THEN excluded.remote_id ELSE attachments.remote_id END,
                    declared_size=COALESCE(excluded.declared_size, attachments.declared_size),
                    local_path=CASE WHEN excluded.local_path<>'' THEN excluded.local_path ELSE attachments.local_path END,
                    raw_json=excluded.raw_json
                """,
                (
                    snapshot_id,
                    ordinal,
                    str(attachment.get("content_type") or ""),
                    str(attachment.get("filename") or ""),
                    _normalized_url(str(attachment.get("url") or "")),
                    str(attachment.get("id") or attachment.get("file_id") or ""),
                    _optional_int(attachment.get("size") or attachment.get("file_size")),
                    local_path,
                    canonical_json(attachment),
                ),
            )
            if local_path:
                self._archive_local_attachment(
                    conn,
                    snapshot_id=snapshot_id,
                    ordinal=ordinal,
                    local_path=local_path,
                    content_type=str(attachment.get("content_type") or ""),
                    force_mirror=force_mirror,
                )

    def _archive_local_attachment(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot_id: int,
        ordinal: int,
        local_path: str,
        content_type: str,
        force_mirror: bool = False,
    ) -> None:
        path = Path(local_path)
        if not path.is_file():
            return
        sha, byte_size = _hash_file(path)
        archive_path = ""
        archive_status = "cached"
        if force_mirror or self.config.media_storage == "mirror":
            stored_path = self._write_file_blob(path, content_type, path.name, sha)
            archive_path = str(stored_path)
            archive_status = "archived"
        conn.execute(
            """
            INSERT INTO attachments(
                message_snapshot_id, ordinal, content_type, filename,
                local_path, archive_path, sha256, byte_size, archive_status
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(message_snapshot_id, ordinal) DO UPDATE SET
                content_type=CASE WHEN excluded.content_type<>'' THEN excluded.content_type ELSE attachments.content_type END,
                filename=CASE WHEN attachments.filename<>'' THEN attachments.filename ELSE excluded.filename END,
                local_path=excluded.local_path,
                archive_path=excluded.archive_path,
                sha256=excluded.sha256,
                byte_size=excluded.byte_size,
                archive_status=excluded.archive_status
            """,
            (
                snapshot_id,
                ordinal,
                content_type,
                path.name,
                str(path),
                archive_path,
                sha,
                byte_size,
                archive_status,
            ),
        )

    def archive_bytes(
        self,
        *,
        snapshot_id: int,
        ordinal: int,
        data: bytes,
        content_type: str = "",
        filename: str = "",
    ) -> dict[str, Any]:
        archive_path, sha = self._write_blob(data, content_type, filename)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO attachments(
                    message_snapshot_id, ordinal, content_type, filename,
                    archive_path, sha256, byte_size, archive_status
                ) VALUES(?,?,?,?,?,?,?, 'archived')
                ON CONFLICT(message_snapshot_id, ordinal) DO UPDATE SET
                    content_type=CASE WHEN excluded.content_type<>'' THEN excluded.content_type ELSE attachments.content_type END,
                    filename=CASE WHEN excluded.filename<>'' THEN excluded.filename ELSE attachments.filename END,
                    archive_path=excluded.archive_path,
                    sha256=excluded.sha256,
                    byte_size=excluded.byte_size,
                    archive_status='archived'
                """,
                (snapshot_id, ordinal, content_type, filename, str(archive_path), sha, len(data)),
            )
            self._refresh_fts(conn, snapshot_id)
            conn.commit()
        return {"archive_path": str(archive_path), "sha256": sha, "byte_size": len(data)}

    def mark_attachment_failed(self, snapshot_id: int, ordinal: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE attachments SET archive_status='failed' WHERE message_snapshot_id=? AND ordinal=? AND archive_status<>'archived'",
                (snapshot_id, ordinal),
            )

    def pending_attachments(self, snapshot_id: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ordinal, content_type, filename, remote_url
                FROM attachments
                WHERE message_snapshot_id=? AND archive_status<>'archived' AND remote_url<>''
                ORDER BY ordinal
                """,
                (snapshot_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _write_blob(self, data: bytes, content_type: str, filename: str) -> tuple[Path, str]:
        sha = hashlib.sha256(data).hexdigest()
        target = self._blob_target(sha, content_type, filename)
        if not target.exists():
            fd, tmp_name = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(tmp_name, 0o600)
                os.replace(tmp_name, target)
            finally:
                if os.path.exists(tmp_name):
                    os.unlink(tmp_name)
        return target, sha

    def _write_file_blob(
        self,
        source: Path,
        content_type: str,
        filename: str,
        sha: str,
    ) -> Path:
        """Mirror a Baileys cache file without loading the whole file in RAM."""
        target = self._blob_target(sha, content_type, filename)
        if target.exists():
            return target
        fd, tmp_name = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
        try:
            with source.open("rb") as reader, os.fdopen(fd, "wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return target

    def _blob_target(self, sha: str, content_type: str, filename: str) -> Path:
        ext = Path(filename).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,10}", ext):
            ext = mimetypes.guess_extension(content_type.split(";", 1)[0].strip()) or ""
        target_dir = self.config.media_dir / sha[:2] / sha[2:4]
        target_dir.mkdir(parents=True, exist_ok=True)
        return target_dir / f"{sha}{ext}"

    def _refresh_fts(self, conn: sqlite3.Connection, snapshot_id: int) -> None:
        if not self._has_fts(conn):
            return
        message = conn.execute("SELECT content FROM messages WHERE id=?", (snapshot_id,)).fetchone()
        if not message:
            return
        attachments = conn.execute(
            "SELECT filename, content_type, sha256 FROM attachments WHERE message_snapshot_id=? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        values = conn.execute(
            "SELECT field_path, value_text FROM message_values WHERE message_snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        attachment_text = " ".join(" ".join(filter(None, row)) for row in attachments)
        value_text = " ".join(
            f"{row['field_path']} {row['value_text']}"
            for row in values
            if _fts_value_allowed(row["field_path"], row["value_text"])
        )
        conn.execute("DELETE FROM message_fts WHERE snapshot_id=?", (snapshot_id,))
        conn.execute(
            "INSERT INTO message_fts(snapshot_id, content, attachment_text, value_text) VALUES(?,?,?,?)",
            (
                snapshot_id,
                _lexical_expansion(message["content"]),
                _lexical_expansion(attachment_text),
                _lexical_expansion(value_text),
            ),
        )

    def recent_context(
        self,
        *,
        platform: str,
        chat_id: str,
        exclude_snapshot_id: int | None = None,
    ) -> str:
        params: list[Any] = [platform, chat_id]
        exclude = ""
        if exclude_snapshot_id is not None:
            exclude = "AND m.id<>?"
            params.append(exclude_snapshot_id)
        params.append(self.config.context_messages)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT m.*, GROUP_CONCAT(
                    CASE
                        WHEN a.sha256<>'' THEN '[' || COALESCE(NULLIF(a.content_type,''),'file') || ':' || COALESCE(NULLIF(a.filename,''),a.sha256) || ' sha256=' || a.sha256 || ']'
                        ELSE '[' || COALESCE(NULLIF(a.content_type,''),'file') || ':' || COALESCE(NULLIF(a.filename,''),'attachment') || ']'
                    END,
                    ' '
                ) AS attachment_summary
                FROM messages m
                LEFT JOIN attachments a ON a.message_snapshot_id=m.id
                WHERE m.platform=? AND m.chat_id=? {exclude}
                GROUP BY m.id
                ORDER BY COALESCE(NULLIF(m.occurred_at,''), m.captured_at) DESC, m.id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()

        selected: list[str] = []
        tokens = estimate_tokens("[Recent durable message snapshots]")
        for row in rows:
            line = _context_line(row)
            cost = estimate_tokens(line)
            if selected and tokens + cost > self.config.context_tokens:
                break
            if not selected and cost > self.config.context_tokens:
                line = line[: max(128, self.config.context_tokens * 3)] + "..."
            selected.append(line)
            tokens += cost
        if not selected:
            return ""
        return "[Recent durable message snapshots]\n" + "\n".join(reversed(selected))

    def hybrid_search(self, query: str = "", **filters: Any) -> list[dict[str, Any]]:
        limit = max(1, min(int(filters.pop("limit", 10) or 10), 100))
        query = str(query or "").strip()
        with self._connect() as conn:
            where_sql, params = self._filter_sql(filters)
            if not query:
                rows = conn.execute(
                    f"SELECT m.* FROM messages m WHERE {where_sql} ORDER BY COALESCE(NULLIF(m.occurred_at,''),m.captured_at) DESC, m.id DESC LIMIT ?",
                    (*params, limit),
                ).fetchall()
                return [self._result_summary(conn, row, 1.0, ["structured"]) for row in rows]

            rankings: list[tuple[str, float, list[int]]] = []
            exact = conn.execute(
                f"""
                SELECT DISTINCT m.id FROM messages m
                WHERE {where_sql} AND (
                    m.message_id=? OR m.content=? OR
                    EXISTS(SELECT 1 FROM attachments ax WHERE ax.message_snapshot_id=m.id AND (ax.filename=? OR ax.sha256=?)) OR
                    EXISTS(SELECT 1 FROM message_values vx WHERE vx.message_snapshot_id=m.id AND vx.value_text=?)
                )
                ORDER BY m.id DESC LIMIT ?
                """,
                (*params, query, query, query, query, query, self.config.search_candidates),
            ).fetchall()
            rankings.append(("exact", 2.0, [int(row[0]) for row in exact]))

            if self._has_fts(conn):
                match = _fts_query(query)
                if match:
                    try:
                        bm25_rows = conn.execute(
                            f"""
                            SELECT m.id, bm25(message_fts, 0.0, 2.0, 1.4, 0.6) AS rank
                            FROM message_fts
                            JOIN messages m ON m.id=CAST(message_fts.snapshot_id AS INTEGER)
                            WHERE message_fts MATCH ? AND {where_sql}
                            ORDER BY rank ASC LIMIT ?
                            """,
                            (match, *params, self.config.search_candidates),
                        ).fetchall()
                        rankings.append(("bm25", 1.25, [int(row[0]) for row in bm25_rows]))
                    except sqlite3.OperationalError:
                        pass

            like = f"%{_escape_like(query.casefold())}%"
            substring_rows = conn.execute(
                f"""
                SELECT DISTINCT m.id FROM messages m
                WHERE {where_sql} AND (
                    lower(m.content) LIKE ? ESCAPE '\\' OR
                    EXISTS(SELECT 1 FROM attachments al WHERE al.message_snapshot_id=m.id AND lower(al.filename) LIKE ? ESCAPE '\\') OR
                    EXISTS(SELECT 1 FROM message_values vl WHERE vl.message_snapshot_id=m.id AND lower(vl.value_text) LIKE ? ESCAPE '\\')
                )
                ORDER BY m.id DESC LIMIT ?
                """,
                (*params, like, like, like, self.config.search_candidates),
            ).fetchall()
            rankings.append(("substring", 1.1, [int(row[0]) for row in substring_rows]))

            pool = conn.execute(
                f"""
                SELECT m.id, m.content,
                       COALESCE(GROUP_CONCAT(a.filename, ' '), '') AS attachment_text
                FROM messages m
                LEFT JOIN attachments a ON a.message_snapshot_id=m.id
                WHERE {where_sql}
                GROUP BY m.id
                ORDER BY m.id DESC LIMIT ?
                """,
                (*params, self.config.search_candidates),
            ).fetchall()
            fuzzy = sorted(
                (
                    (_fuzzy_score(query, f"{row['content']} {row['attachment_text']}"), int(row["id"]))
                    for row in pool
                ),
                reverse=True,
            )
            rankings.append(("fuzzy", 0.7, [snapshot_id for score, snapshot_id in fuzzy if score >= 0.12]))

            scores: dict[int, float] = {}
            sources: dict[int, list[str]] = {}
            rrf_k = 60.0
            for source, weight, ids in rankings:
                for rank, snapshot_id in enumerate(ids, start=1):
                    scores[snapshot_id] = scores.get(snapshot_id, 0.0) + weight / (rrf_k + rank)
                    sources.setdefault(snapshot_id, []).append(source)
            ordered = sorted(scores, key=lambda sid: (scores[sid], sid), reverse=True)[:limit]
            results = []
            for snapshot_id in ordered:
                row = conn.execute("SELECT * FROM messages WHERE id=?", (snapshot_id,)).fetchone()
                if row:
                    results.append(self._result_summary(conn, row, scores[snapshot_id], sources[snapshot_id]))
            return results

    @staticmethod
    def _filter_sql(filters: dict[str, Any]) -> tuple[str, list[Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        direct = {
            "snapshot_id": "m.id",
            "message_id": "m.message_id",
            "platform": "m.platform",
            "chat_id": "m.chat_id",
            "chat_type": "m.chat_type",
            "sender_id": "m.sender_id",
            "event_type": "m.event_type",
            "message_kind": "m.message_kind",
        }
        for name, column in direct.items():
            value = filters.get(name)
            if value not in (None, ""):
                clauses.append(f"{column}=?")
                params.append(value)
        if filters.get("from_time"):
            clauses.append("COALESCE(NULLIF(m.occurred_at,''),m.captured_at)>=?")
            params.append(filters["from_time"])
        if filters.get("to_time"):
            clauses.append("COALESCE(NULLIF(m.occurred_at,''),m.captured_at)<=?")
            params.append(filters["to_time"])
        attachment_filters = {
            "attachment_filename": "af.filename",
            "attachment_sha256": "af.sha256",
            "attachment_remote_id": "af.remote_id",
            "attachment_url": "af.remote_url",
            "media_type": "af.content_type",
        }
        for name, column in attachment_filters.items():
            value = filters.get(name)
            if value not in (None, ""):
                clauses.append(
                    f"EXISTS(SELECT 1 FROM attachments af WHERE af.message_snapshot_id=m.id AND {column}=?)"
                )
                params.append(value)
        field_path = filters.get("field_path")
        value = filters.get("value")
        if field_path not in (None, "") and value not in (None, ""):
            clauses.append(
                "EXISTS(SELECT 1 FROM message_values vf WHERE vf.message_snapshot_id=m.id AND vf.field_path=? AND vf.value_text=?)"
            )
            params.extend((field_path, str(value)))
        elif field_path not in (None, ""):
            clauses.append(
                "EXISTS(SELECT 1 FROM message_values vf WHERE vf.message_snapshot_id=m.id AND vf.field_path=?)"
            )
            params.append(field_path)
        elif value not in (None, ""):
            clauses.append(
                "EXISTS(SELECT 1 FROM message_values vf WHERE vf.message_snapshot_id=m.id AND vf.value_text=?)"
            )
            params.append(str(value))
        return " AND ".join(clauses), params

    def _result_summary(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        score: float,
        sources: list[str],
        include_links: bool = False,
    ) -> dict[str, Any]:
        attachments = conn.execute(
            """
            SELECT ordinal, content_type, filename, sha256, byte_size,
                   archive_status, archive_path, remote_url, remote_id, declared_size, local_path
            FROM attachments WHERE message_snapshot_id=? ORDER BY ordinal
            """,
            (row["id"],),
        ).fetchall()
        attachment_items = []
        for item in attachments:
            value = dict(item)
            if not include_links:
                value["remote_url_present"] = bool(value.pop("remote_url", ""))
                value.pop("local_path", None)
            attachment_items.append(value)
        return {
            "snapshot_id": int(row["id"]),
            "message_id": row["message_id"],
            "platform": row["platform"],
            "event_type": row["event_type"],
            "chat_id": row["chat_id"],
            "chat_type": row["chat_type"],
            "sender_id": row["sender_id"],
            "sender_name": row["sender_name"],
            "content": row["content"],
            "message_kind": row["message_kind"],
            "occurred_at": row["occurred_at"],
            "captured_at": row["captured_at"],
            "score": round(float(score), 8),
            "retrieval_sources": sources,
            "attachments": attachment_items,
        }

    def get_snapshot(self, identifier: str | int) -> dict[str, Any] | None:
        with self._connect() as conn:
            if str(identifier).isdigit():
                row = conn.execute("SELECT * FROM messages WHERE id=?", (int(identifier),)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM messages WHERE message_id=? ORDER BY id DESC LIMIT 1",
                    (str(identifier),),
                ).fetchone()
            if not row:
                return None
            result = self._result_summary(conn, row, 1.0, ["direct"], include_links=True)
            result["raw"] = json.loads(row["raw_json"])
            events = conn.execute(
                "SELECT event_type, captured_at, raw_sha256, raw_json FROM raw_events WHERE message_snapshot_id=? ORDER BY id",
                (row["id"],),
            ).fetchall()
            result["raw_events"] = [
                {
                    "event_type": item["event_type"],
                    "captured_at": item["captured_at"],
                    "raw_sha256": item["raw_sha256"],
                    "raw": json.loads(item["raw_json"]),
                }
                for item in events
            ]
            return result

    def restore_snapshot(self, identifier: str | int) -> dict[str, Any] | None:
        snapshot = self.get_snapshot(identifier)
        if not snapshot:
            return None
        target_dir = self.config.restore_dir / f"snapshot-{snapshot['snapshot_id']}"
        target_dir.mkdir(parents=True, exist_ok=True)
        restored = []
        links = []
        for item in snapshot["attachments"]:
            if item.get("remote_url"):
                links.append(
                    {
                        "url_present": True,
                        "remote_id": item.get("remote_id") or "",
                        "filename": item.get("filename") or "",
                        "content_type": item.get("content_type") or "",
                        "declared_size": item.get("declared_size"),
                    }
                )
            archive_path = Path(item.get("archive_path") or "")
            if not archive_path.is_file():
                continue
            name = _safe_filename(item.get("filename") or archive_path.name)
            destination = target_dir / f"{int(item['ordinal']):02d}-{name}"
            if destination.exists() and hashlib.sha256(destination.read_bytes()).hexdigest() != item["sha256"]:
                destination = target_dir / f"{int(item['ordinal']):02d}-{item['sha256'][:12]}-{name}"
            if not destination.exists():
                try:
                    os.link(archive_path, destination)
                except OSError:
                    shutil.copy2(archive_path, destination)
            restored.append(
                {
                    "path": str(destination),
                    "sha256": item["sha256"],
                    "content_type": item["content_type"],
                    "filename": item["filename"],
                }
            )
        manifest = dict(snapshot)
        manifest["restored_files"] = restored
        manifest_path = target_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(manifest_path, 0o600)
        return {
            "snapshot_id": snapshot["snapshot_id"],
            "message_id": snapshot["message_id"],
            "content": snapshot["content"],
            "manifest_path": str(manifest_path),
            "restored_files": restored,
            "remote_links": links,
        }

    def stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            messages = int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0])
            events = int(conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0])
            attachments = int(conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0])
            archived = int(
                conn.execute("SELECT COUNT(*) FROM attachments WHERE archive_status='archived'").fetchone()[0]
            )
            bytes_archived = int(
                conn.execute(
                    "SELECT COALESCE(SUM(byte_size),0) FROM attachments WHERE archive_status='archived'"
                ).fetchone()[0]
            )
            fts5 = self._has_fts(conn)
        return {
            "schema_version": SCHEMA_VERSION,
            "messages": messages,
            "raw_events": events,
            "attachments": attachments,
            "archived_attachments": archived,
            "archived_bytes": bytes_archived,
            "media_storage": self.config.media_storage,
            "fts5": fts5,
            "db_path": str(self.config.db_path),
        }


def _iter_raw_attachments(raw: Any) -> Iterable[dict[str, Any]]:
    if isinstance(raw, dict):
        attachments = raw.get("attachments")
        if isinstance(attachments, list):
            for attachment in attachments:
                if isinstance(attachment, dict):
                    yield attachment
        elements = raw.get("msg_elements")
        if isinstance(elements, list):
            for element in elements:
                if isinstance(element, dict):
                    yield from _iter_raw_attachments(element)
        media_urls = raw.get("mediaUrls")
        if isinstance(media_urls, list):
            for index, value in enumerate(media_urls):
                media_ref = str(value or "")
                if not media_ref:
                    continue
                is_remote = media_ref.startswith(("http://", "https://", "//"))
                filename = str(raw.get("fileName") or "")
                if not filename and not is_remote:
                    filename = Path(media_ref).name
                yield {
                    "id": str(raw.get("messageId") or ""),
                    "content_type": str(raw.get("mime") or raw.get("mediaType") or ""),
                    "filename": filename,
                    "url": media_ref if is_remote else "",
                    "local_path": "" if is_remote else media_ref,
                    "media_index": index,
                    "baileys_native_type": str(raw.get("nativeType") or ""),
                }
        if raw.get("hasMedia") and not media_urls:
            # Preserve the fact that Baileys observed media even when its CDN
            # download/reupload retry failed and no decrypted cache path exists.
            yield {
                "id": str(raw.get("messageId") or ""),
                "content_type": str(raw.get("mime") or raw.get("mediaType") or ""),
                "filename": str(raw.get("fileName") or ""),
                "baileys_native_type": str(raw.get("nativeType") or ""),
                "download_status": "unavailable_at_capture",
            }


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            byte_size += len(chunk)
    return digest.hexdigest(), byte_size


def _iter_embedded_local_paths(text: str) -> Iterable[tuple[str, str, str]]:
    """Extract adapter-generated non-image cache markers from normalized text."""
    pattern = re.compile(r"\[(file|video):\s*(.*?)\s+\(([^)\r\n]+)\)\]", re.IGNORECASE)
    for match in pattern.finditer(str(text or "")):
        candidate = Path(match.group(3).strip())
        if candidate.is_absolute() and candidate.is_file():
            yield match.group(1).lower(), match.group(2).strip(), str(candidate)


def _message_kind(content: str, attachments: list[dict[str, Any]]) -> str:
    kinds = []
    for attachment in attachments:
        content_type = str(attachment.get("content_type") or "").lower()
        if content_type.startswith("image/"):
            kind = "image"
        elif content_type.startswith("audio/") or content_type == "voice":
            kind = "audio"
        elif content_type.startswith("video/"):
            kind = "video"
        else:
            kind = "file"
        if kind not in kinds:
            kinds.append(kind)
    if not kinds:
        return "text" if content else "empty"
    if content:
        kinds.insert(0, "text")
    return "+".join(kinds)


def _normalized_url(url: str) -> str:
    return f"https:{url}" if url.startswith("//") else url


def _context_line(row: sqlite3.Row) -> str:
    timestamp = row["occurred_at"] or row["captured_at"]
    sender = row["sender_name"] or row["sender_id"] or "unknown"
    body = str(row["content"] or "").strip()
    attachment = str(row["attachment_summary"] or "").strip()
    if not body:
        body = attachment or f"[{row['message_kind'] or 'non-text message'}]"
    elif attachment:
        body = f"{body} {attachment}"
    return f"[{timestamp}] [{sender}] [snapshot:{row['id']}] {body}"


def _fts_query(query: str) -> str:
    expanded = _lexical_expansion(query)
    terms = re.findall(r"[\w\u3400-\u9fff\uf900-\ufaff-]+", expanded, flags=re.UNICODE)
    unique = []
    for term in terms:
        if term not in unique:
            unique.append(term)
    return " OR ".join(f'"{term.replace(chr(34), chr(34) * 2)}"' for term in unique[:24])


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _lexical_expansion(text: str) -> str:
    """Append CJK bi/tri-grams so FTS5/BM25 can recall unsegmented Chinese."""
    text = str(text or "")
    grams: list[str] = []
    for run in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]+", text):
        for size in (2, 3):
            if len(run) < size:
                continue
            grams.extend(run[index : index + size] for index in range(len(run) - size + 1))
    return text if not grams else f"{text} {' '.join(grams)}"


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^\w.()\[\] -]+", "_", Path(value).name, flags=re.UNICODE).strip(" .")
    return cleaned[:180] or "attachment"


def _fts_value_allowed(field_path: str, value: str) -> bool:
    """Keep signed URLs/secrets out of the redundant full-text index."""
    path = str(field_path).casefold()
    text = str(value)
    if len(text) > 1024 or text.startswith(("http://", "https://", "//")):
        return False
    return not any(marker in path for marker in ("url", "token", "secret", "authorization"))


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
