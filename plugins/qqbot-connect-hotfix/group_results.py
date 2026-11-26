"""Durable, acknowledgement-based delivery of QQ group task results.

QQ replies have a short lifetime. Never fall back to an unauthorized proactive
send after a long task; retain final items and use the next authenticated group
mention as a fresh reply anchor. Ambiguous sends are retained for manual review.
"""
from __future__ import annotations

import asyncio
import contextvars
from contextlib import contextmanager
import functools
import hashlib
import inspect
import json
import logging
import shutil
import sqlite3
import time

from .reply_budget import PRIORITY, REQUEST, is_budget_error
from pathlib import Path

logger = logging.getLogger(__name__)
_TURN = contextvars.ContextVar("qq_group_result_turn", default=None)
_REPLAY = contextvars.ContextVar("qq_group_result_replay", default=False)
_MARKER = "_qq_group_results_wrapped"
_COMMANDS = {"领取结果", "取结果", "补发结果", "/results"}
WINDOW = 3595  # Current group window is 60 minutes; retain a send margin.


def _result(success=False, error=None, message_id=None):
    from gateway.platforms.base import SendResult
    return SendResult(success=success, error=error, message_id=message_id, retryable=False)


def _denied(error):
    text = str(error or "").lower()
    if "unconfirmed" in text:
        return False
    return is_budget_error(error) or any(x in text for x in ("主动消息失败", "无权限", "msg_id已过期", "msgid已经过期", "reply_window_closed")) or (
        ("msg_id" in text or "msgid" in text) and ("过期" in text or "expired" in text)
    )


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = self.root / "results.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS results (
                id TEXT PRIMARY KEY, chat TEXT NOT NULL, origin TEXT NOT NULL,
                kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                created REAL NOT NULL, ack TEXT, error TEXT)""")
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, chat, origin, kind, payload):
        payload = dict(payload)
        digest = None
        if kind not in {"text", "remote_image"}:
            source = Path(payload["path"])
            if not source.is_file():
                raise FileNotFoundError("Attachment source is unavailable; delivery was not attempted")
            content_hash = hashlib.sha256()
            with source.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    content_hash.update(chunk)
            digest = content_hash.hexdigest()
        identity = [chat, origin, kind, payload]
        if digest is not None:
            identity.append(digest)
        key = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if digest is not None:
            folder = self.root / "files" / key
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / source.name
            if not target.exists():
                temporary = folder / (source.name + ".partial")
                shutil.copyfile(source, temporary)
                temporary.replace(target)
            payload["path"] = str(target)
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO results VALUES (?,?,?,?,?,'pending',?,NULL,NULL)",
                       (key, chat, origin, kind, json.dumps(payload, ensure_ascii=False), time.time()))
        return key

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM results WHERE id=?", (key,)).fetchone()
        return dict(row) if row else None

    def claim(self, key):
        with self.connect() as db:
            return db.execute("UPDATE results SET status='sending' WHERE id=? AND status='pending'", (key,)).rowcount == 1

    def finish(self, key, status, ack=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE results SET status=?,ack=?,error=? WHERE id=?", (status, ack, error, key))

    def pending(self, chat, limit=3):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM results WHERE chat=? AND status='pending' ORDER BY created,rowid LIMIT ?", (chat, limit))]

    def counts(self, chat):
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute(
                "SELECT status,count(*) FROM results WHERE chat=? GROUP BY status", (chat,))}


def _store(adapter):
    store = getattr(adapter, "_qq_group_results_store", None)
    if store is None:
        from hermes_constants import get_hermes_home
        store = adapter._qq_group_results_store = Store(get_hermes_home() / "state" / "qq-group-results")
    return store


def _anchor(adapter, chat):
    anchor, stamp = (getattr(adapter, "_qq_group_reply_seen", {}) or {}).get(chat, (None, None))
    if anchor and stamp is not None and -5 <= time.time() - stamp < WINDOW:
        return str(anchor)
    return None


def _scope(adapter, chat, metadata, reply_to=None):
    turn = _TURN.get()
    return bool(not _REPLAY.get() and adapter._guess_chat_type(chat) == "group"
                and not (metadata or {}).get("_interim_send")
                and ((turn and turn["chat"] == chat and not turn["command"]) or reply_to))


async def _deliver(adapter, row, anchor, priority="final"):
    store = _store(adapter)
    if row["status"] == "delivered":
        return _result(True, message_id=row["ack"])
    if not anchor:
        return _result(error="qq_group_result_pending: waiting for a fresh group mention")
    if not await asyncio.to_thread(store.claim, row["id"]):
        current = await asyncio.to_thread(store.get, row["id"])
        return _result(current["status"] == "delivered", error=None if current["status"] == "delivered" else "qq_group_result_unconfirmed", message_id=current["ack"])
    token = _REPLAY.set(True)
    priority_token = PRIORITY.set(priority)
    request_token = REQUEST.set(row['id'])
    try:
        payload = json.loads(row["payload"])
        if row["kind"] == "text":
            result = await adapter._send_group_text(row["chat"], payload["text"], reply_to=anchor)
        else:
            path_key = {"document": "file_path", "image": "image_path", "remote_image": "image_url", "video": "video_path", "voice": "audio_path"}[row["kind"]]
            payload[path_key] = payload.pop("url" if row["kind"] == "remote_image" else "path")
            result = await adapter._qq_group_results_original_media[row["kind"]](
                adapter, chat_id=row["chat"], **payload, reply_to=anchor)
    except Exception as exc:
        error = str(exc)
        await asyncio.to_thread(store.finish, row["id"], "pending" if _denied(error) else "uncertain", None, error[:800])
        return _result(error="qq_group_result_pending" if _denied(error) else "qq_group_result_unconfirmed")
    finally:
        REQUEST.reset(request_token)
        PRIORITY.reset(priority_token)
        _REPLAY.reset(token)
    if result.success:
        await asyncio.to_thread(store.finish, row["id"], "delivered", result.message_id, None)
    else:
        await asyncio.to_thread(store.finish, row["id"], "pending" if _denied(result.error) else "uncertain", None, str(result.error or "")[:800])
    if not result.success:
        return _result(error="qq_group_result_pending" if _denied(result.error) else "qq_group_result_unconfirmed")
    return result


async def _flush(adapter, chat, anchor):
    locks = getattr(adapter, "_qq_group_results_locks", None)
    if locks is None:
        locks = adapter._qq_group_results_locks = {}
    async with locks.setdefault(chat, asyncio.Lock()):
        rows = await asyncio.to_thread(_store(adapter).pending, chat, 3)
        for row in rows:
            result = await _deliver(adapter, row, anchor)
            if not result.success:
                break
        return len(rows)


def patch_group_results(QQAdapter):
    if getattr(QQAdapter._process_message_background, _MARKER, False):
        return
    original_process = QQAdapter._process_message_background
    original_send = QQAdapter.send
    original_retry = QQAdapter._send_with_retry
    original_notice = QQAdapter._notify_media_delivery_failure
    original_set_handler = QQAdapter.set_message_handler
    media_methods = {"document": ("send_document", "file_path"), "image": ("send_image_file", "image_path"),
                     "remote_image": ("send_image", "image_url"),
                     "video": ("send_video", "video_path"), "voice": ("send_voice", "audio_path")}
    QQAdapter._qq_group_results_original_media = {}

    @functools.wraps(original_set_handler)
    def set_handler(self, handler):
        if handler is None:
            return original_set_handler(self, handler)
        @functools.wraps(handler)
        async def dispatch(event):
            turn = _TURN.get()
            if turn and turn['chat'] == str(event.source.chat_id) and 'recovery_notice' in turn:
                return turn['recovery_notice']
            return await handler(event)
        return original_set_handler(self, dispatch)
    QQAdapter.set_message_handler = set_handler

    @functools.wraps(original_process)
    async def process(self, event, session_key):
        # Gateway dispatch checks authorization later; require its bound check before replay.
        group = getattr(event.source, "chat_type", None) == "group"
        chat = str(event.source.chat_id)
        authorized = self._is_sender_authorized(getattr(event.source, 'user_id', None), 'group', chat) is True
        token = _TURN.set({"chat": chat, "origin": str(event.message_id or ""),
                           "command": bool(event.is_command())} if group and authorized else None)
        try:
            stamp = getattr(event, "timestamp", None)
            fresh = (group and authorized and bool(event.message_id) and not getattr(event, "internal", False)
                     and getattr(stamp, "tzinfo", None) is not None
                     and -5 <= time.time() - stamp.timestamp() < WINDOW)
            recovery = str(event.text or "").strip() in _COMMANDS
            try:
                if fresh:
                    anchor = str(event.message_id or "")
                    await _flush(self, chat, anchor)
                    if recovery:
                        counts = await asyncio.to_thread(_store(self).counts, chat)
                        remaining = counts.get("pending", 0)
                        uncertain = counts.get("uncertain", 0) + counts.get("sending", 0)
                        notice = f"待交付结果还剩 {remaining} 项。" if remaining else "当前没有待补发项。"
                        if remaining:
                            notice += "请再次 @我发送“领取结果”。"
                        if uncertain:
                            notice += f"另有 {uncertain} 项回执不明确，已保留供核对，未自动重发。"
                        _TURN.get().update(command=True, recovery_notice=notice)
                    elif not event.is_command():
                        await self.send(chat, "已收到。未显示的结果可 @我发送“领取结果”，无需重做任务。",
                                        reply_to=anchor, metadata={"_interim_send": True})
            except Exception:
                logger.exception("QQ result preparation failed; preserving native session cleanup")
                if fresh and recovery:
                    _TURN.get().update(command=True, recovery_notice="暂时无法读取待交付结果，未重新执行原任务，请稍后重试。")
            result = await original_process(self, event, session_key)
            if fresh and not event.is_command() and not recovery:
                try:
                    await _flush(self, chat, anchor)
                except Exception:
                    logger.exception("QQ pending-result flush failed; retained for retry")
            return result
        finally:
            _TURN.reset(token)
    setattr(process, _MARKER, True)
    QQAdapter._process_message_background = process

    @functools.wraps(original_send)
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        turn = _TURN.get()
        if turn and turn["chat"] == chat_id and not reply_to:
            reply_to = turn["origin"]
        if not _scope(self, chat_id, metadata, reply_to):
            # Avoid repeated unauthorized Working/commentary sends after expiry.
            turn = _TURN.get()
            if not _REPLAY.get() and turn and turn["chat"] == chat_id and not _anchor(self, chat_id):
                return _result(error="qq_group_reply_window_closed")
            meta = metadata or {}
            priority = PRIORITY.set('final' if meta.get('notify') else 'progress' if meta.get('_interim_send') else PRIORITY.get())
            try:
                return await original_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)
            finally:
                PRIORITY.reset(priority)
        store = _store(self)
        chunks = self.truncate_message(self.format_message(content), self.MAX_MESSAGE_LENGTH)
        result = _result(True)
        # Persist every chunk before beginning network I/O.
        origin = str(reply_to or (_TURN.get() or {}).get("origin") or "")
        keys = [await asyncio.to_thread(store.enqueue, chat_id, origin, "text", {"text": chunk, "part": index}) for index, chunk in enumerate(chunks)]
        for key in keys:
            row = await asyncio.to_thread(store.get, key)
            result = await _deliver(self, row, reply_to or _anchor(self, chat_id),
                                    "final" if (metadata or {}).get("notify") else "ordinary")
            if not result.success:
                break
        return result
    QQAdapter.send = send

    @functools.wraps(original_retry)
    async def retry(self, chat_id, content, reply_to=None, metadata=None, **kwargs):
        if _scope(self, chat_id, metadata, reply_to):
            return await self.send(chat_id, content, reply_to=reply_to, metadata=metadata)
        return await original_retry(self, chat_id, content, reply_to=reply_to, metadata=metadata, **kwargs)
    QQAdapter._send_with_retry = retry

    for kind, (name, path_key) in media_methods.items():
        original = getattr(QQAdapter, name)
        QQAdapter._qq_group_results_original_media[kind] = original

        def make_media(original, kind, path_key):
            signature = inspect.signature(original)
            @functools.wraps(original)
            async def media(self, *args, **kwargs):
                bound = signature.bind(self, *args, **kwargs)
                chat = bound.arguments.get("chat_id")
                metadata = kwargs.get("metadata")
                reply_to = bound.arguments.get("reply_to")
                if not _scope(self, chat, metadata, reply_to):
                    return await original(self, *args, **kwargs)
                payload = {"url" if kind == "remote_image" else "path": str(bound.arguments[path_key])}
                # Persist the method's named argument, rather than an arbitrary kwargs set.
                for field in ("caption", "file_name"):
                    if bound.arguments.get(field) is not None:
                        payload[field] = bound.arguments[field]
                store = _store(self)
                key = await asyncio.to_thread(store.enqueue, chat, str(reply_to or (_TURN.get() or {}).get("origin") or ""), kind, payload)
                row = await asyncio.to_thread(store.get, key)
                result = await _deliver(self, row, reply_to or _anchor(self, chat),
                                        "final" if (metadata or {}).get("notify") else "ordinary")
                turn = _TURN.get()
                if not result.success and turn and turn['chat'] == chat:
                    turn.setdefault('retained_media', set()).add(str(bound.arguments[path_key]))
                return result
            return media
        setattr(QQAdapter, name, make_media(original, kind, path_key))

    @functools.wraps(original_notice)
    async def notice(self, chat_id, media_path, *args, **kwargs):
        turn = _TURN.get()
        if turn and turn['chat'] == chat_id and str(media_path) in turn.get('retained_media', set()):
            return  # Only suppress the notice when this exact attachment was retained.
        return await original_notice(self, chat_id, media_path, *args, **kwargs)
    QQAdapter._notify_media_delivery_failure = notice
    logger.info("QQ group result outbox enabled; expired finals await a fresh group mention")
