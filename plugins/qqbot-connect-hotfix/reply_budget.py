"""Persist QQ reply reservations by authenticated inbound message, not by turn."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import hashlib
import json
import logging
from pathlib import Path
import re
import sqlite3
import time

PRIORITY = ContextVar('qq_reply_priority', default='ordinary')
_ANCHOR = ContextVar('qq_reply_anchor', default=None)
REQUEST = ContextVar('qq_reply_request', default=None)
_CHUNKS = ContextVar('qq_reply_chunks', default=None)
WINDOW = 3600
_PATH = re.compile(r'/v2/(groups|users)/([^/]+)/(messages|stream_messages)$')


class BudgetDenied(RuntimeError):
    pass


class ReplyExpired(BudgetDenied):
    pass


def is_budget_error(error):
    text = str(error or '')
    return ('qq_reply_budget_forbidden' in text or '40034128' in text
            or '被动回复时间或者次数超过限制' in text
            or 'QQ passive-reply window or budget is exhausted' in text)


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS anchors (
                    bot TEXT, msg TEXT, scene TEXT, chat TEXT, sender TEXT,
                    expires REAL, cap INTEGER, exhausted INTEGER DEFAULT 0,
                    next_seq INTEGER DEFAULT 1, PRIMARY KEY(bot,msg));
                CREATE TABLE IF NOT EXISTS requests (
                    bot TEXT, msg TEXT, key TEXT, seq INTEGER, priority TEXT,
                    status TEXT, response TEXT, stream TEXT,
                    PRIMARY KEY(bot,msg,key));
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def remember(self, bot, msg, scene, chat, sender, stamp):
        now = time.time()
        if not msg or scene not in ('group', 'c2c') or stamp > now + 5:
            return
        # Replay of the same inbound event never resets its quota or expiry.
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO anchors(bot,msg,scene,chat,sender,expires,cap) VALUES(?,?,?,?,?,?,?)',
                       (bot, msg, scene, chat, sender, stamp + WINDOW, 5 if scene == 'group' else 4))

    def reserve(self, bot, msg, route, chat, key, priority, stream_id=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            a = db.execute('SELECT * FROM anchors WHERE bot=? AND msg=?', (bot, msg)).fetchone()
            if not a or not ((route == 'groups' and a['scene'] == 'group' and a['chat'] == chat)
                             or (route == 'users' and (a['chat'] if a['scene'] == 'c2c' else a['sender']) == chat)):
                raise BudgetDenied('qq_reply_budget_forbidden: unknown or mismatched inbound message')
            if stream_id:
                r = db.execute("SELECT seq FROM requests WHERE bot=? AND msg=? AND stream=? AND status='sent' AND key LIKE 'stream:%'",
                               (bot, msg, stream_id)).fetchone()
                if not r:
                    raise BudgetDenied('qq_reply_budget_forbidden: unknown stream carrier')
                return dict(seq=r['seq'], update=True)
            existing = db.execute('SELECT * FROM requests WHERE bot=? AND msg=? AND key=?', (bot, msg, key)).fetchone()
            if existing:
                if existing['status'] == 'sent':
                    return dict(response=json.loads(existing['response']), seq=existing['seq'])
                # A lost ACK or another writer may already have spent this slot.
                raise BudgetDenied('qq_reply_budget_forbidden: prior send unconfirmed; retained for review')
            used = db.execute('SELECT count(*) FROM requests WHERE bot=? AND msg=?', (bot, msg)).fetchone()[0]
            progress = db.execute("SELECT count(*) FROM requests WHERE bot=? AND msg=? AND priority='progress'", (bot, msg)).fetchone()[0]
            reserve = 2 if a['scene'] == 'group' else 1
            cap = a['cap'] if priority == 'final' else a['cap'] - reserve
            if not a['exhausted'] and time.time() >= a['expires'] - 5:
                raise ReplyExpired('QQ msg_id expired: use the existing one-shot standalone fallback')
            if a['exhausted'] or used >= cap or (priority == 'progress' and progress):
                raise BudgetDenied('qq_reply_budget_forbidden: exhausted, reserved for final, or window closed')
            seq = a['next_seq']
            db.execute('UPDATE anchors SET next_seq=next_seq+1 WHERE bot=? AND msg=?', (bot, msg))
            db.execute("INSERT INTO requests VALUES(?,?,?,?,?,'reserved',NULL,NULL)", (bot, msg, key, seq, priority))
            return dict(seq=seq)

    def finish(self, bot, msg, key, response=None, rejected=False, exhausted=False):
        with self.connect() as db:
            if exhausted:
                db.execute('UPDATE anchors SET exhausted=1 WHERE bot=? AND msg=?', (bot, msg))
            if rejected:
                db.execute('DELETE FROM requests WHERE bot=? AND msg=? AND key=?', (bot, msg, key))
            else:
                db.execute('UPDATE requests SET status=?,response=?,stream=? WHERE bot=? AND msg=? AND key=?',
                           ('sent' if response is not None else 'uncertain',
                            json.dumps(response) if response is not None else None,
                            str(response.get('id') or '') if response else None, bot, msg, key))


def ledger(adapter):
    value = getattr(adapter, '_qq_reply_ledger', None)
    if value is None:
        from hermes_constants import get_hermes_home
        value = adapter._qq_reply_ledger = Ledger(get_hermes_home() / 'state/qq-reply-budget.sqlite3')
    return value


def patch_reply_budget(QQAdapter):
    original = QQAdapter._api_request
    if getattr(original, '_qq_reply_budget_wrapped', False):
        return
    handle = QQAdapter.handle_message
    send = QQAdapter.send
    chunk = QQAdapter._send_chunk

    @wraps(handle)
    async def inbound(self, event):
        source = getattr(event, 'source', None)
        stamp = getattr(event, 'timestamp', None)
        if (not getattr(event, 'internal', False) and stamp is not None
                and getattr(stamp, 'tzinfo', None) is not None):
            kind = getattr(source, 'chat_type', '')
            scene = 'c2c' if kind in ('dm', 'private', 'c2c') else kind
            await asyncio.to_thread(ledger(self).remember, str(self._app_id), str(event.message_id or ''),
                                    scene, str(source.chat_id), str(getattr(source, 'user_id', '') or ''), stamp.timestamp())
        return await handle(self, event)

    @wraps(send)
    async def scoped_send(self, chat_id, content, reply_to=None, metadata=None):
        meta = metadata or {}
        priority = 'final' if meta.get('notify') else 'progress' if meta.get('_interim_send') else PRIORITY.get()
        token = PRIORITY.set(priority)
        anchor_token = _ANCHOR.set((id(self), str(chat_id), reply_to))
        chunks_token = _CHUNKS.set({'seed': hashlib.sha256(str(content).encode()).hexdigest(), 'index': 0})
        try:
            return await send(self, chat_id, content, reply_to=reply_to, metadata=metadata)
        finally:
            _CHUNKS.reset(chunks_token)
            _ANCHOR.reset(anchor_token)
            PRIORITY.reset(token)

    @wraps(chunk)
    async def scoped_chunk(self, *args, **kwargs):
        parts = _CHUNKS.get()
        if parts is None:
            return await chunk(self, *args, **kwargs)
        identity = (parts['seed'], parts['index'])
        parts['index'] += 1
        token = REQUEST.set(identity)
        try:
            return await chunk(self, *args, **kwargs)
        finally:
            REQUEST.reset(token)

    @wraps(original)
    async def request(self, method, path, body=None, *args, **kwargs):
        match = _PATH.fullmatch(path)
        if method.upper() != 'POST' or not match or not isinstance(body, dict):
            return await original(self, method, path, body, *args, **kwargs)
        route, chat, endpoint = match.groups()
        wire = dict(body)
        anchor = _ANCHOR.get()
        from .outbound import _EXPIRED_REPLY_STANDALONE, is_expired_reply_error
        if (not wire.get('msg_id') and _EXPIRED_REPLY_STANDALONE.get() != chat
                and anchor and anchor[:2] == (id(self), chat) and anchor[2]):
            wire['msg_id'] = anchor[2]
        if not wire.get('msg_id'):
            return await original(self, method, path, wire, *args, **kwargs)
        identity = {k: v for k, v in wire.items() if k != 'msg_seq'}
        if REQUEST.get() is not None:
            identity['logical_request'] = REQUEST.get()
        # Updates of one native C2C carrier are not new replies.
        streaming = endpoint == 'stream_messages'
        if streaming:
            identity = {'stream_seq': body.get('msg_seq')}
        key = ('stream:' if streaming else 'message:') + hashlib.sha256(json.dumps([path, identity], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        bot, msg = str(self._app_id), str(wire['msg_id'])
        priority = 'progress' if 'input_notify' in body else PRIORITY.get()
        if streaming and body.get('input_state') == 10:
            priority = 'final'
        claim = await asyncio.to_thread(ledger(self).reserve, bot, msg, route, chat, key, priority,
                                        str(body['stream_msg_id']) if streaming and body.get('stream_msg_id') else None)
        if 'response' in claim:
            return claim['response']
        wire['msg_seq'] = claim['seq']
        try:
            response = await original(self, method, path, wire, *args, **kwargs)
            if (not claim.get('update') and 'input_notify' not in wire
                    and not (response.get('id') or response.get('stream_msg_id'))):
                raise RuntimeError('QQ response unconfirmed: missing message id')
        except Exception as exc:
            expired = is_expired_reply_error(exc)
            blocked = is_budget_error(exc) or expired
            # An explicit HTTP rejection did not accept this message; timeouts stay charged.
            rejected = bool(re.search(r'API error \[4\d\d\]', str(exc)))
            if not claim.get('update'):
                await asyncio.to_thread(ledger(self).finish, bot, msg, key, rejected=rejected, exhausted=blocked)
            if expired:
                raise ReplyExpired('QQ msg_id expired: use the existing one-shot standalone fallback') from exc
            if blocked:
                raise BudgetDenied('qq_reply_budget_forbidden: QQ rejected the passive reply allowance') from exc
            raise
        if not claim.get('update'):
            await asyncio.to_thread(ledger(self).finish, bot, msg, key, response=response)
        return response

    request._qq_reply_budget_wrapped = True
    QQAdapter._api_request = request
    QQAdapter.handle_message = inbound
    QQAdapter.send = scoped_send
    QQAdapter._send_chunk = scoped_chunk
    logging.getLogger(__name__).info('QQ per-message reply budget enabled: durable reservations and final-slot protection')
