"""Prefer proactive group messages, retaining bounded passive fallback."""
import asyncio
from functools import wraps
import logging
import re
import time

from .outbound import _EXPIRED_REPLY_STANDALONE, _recent_group_reply_anchor
from .reply_budget import _ANCHOR, BudgetDenied, ledger

_PATH = re.compile(r'/v2/groups/([^/]+)/messages$')


class GroupDeliveryError(RuntimeError):
    """Stop native retries after a definitive rejection or an ambiguous send."""


def _terminal(error):
    text = str(error)
    rejected = 'unconfirmed' not in text.lower() and (
        isinstance(error, BudgetDenied) or bool(re.search(r'API error \[4\d\d\]', text)))
    state = 'rejected' if rejected else 'unconfirmed'
    return GroupDeliveryError(f'qq_group_delivery_forbidden: {state}: {text}')


def _permission_denied(error):
    text = str(error).lower()
    return ('40034105' in text or ('主动消息' in text and '权限' in text)
            or ('proactive' in text and any(x in text for x in ('permission', 'not allowed', 'forbidden'))))


def patch_group_delivery(adapter_class):
    original = adapter_class._api_request
    if getattr(original, '_qq_group_delivery_wrapped', False):
        return

    async def permission(self, chat):
        states = getattr(self, '_qq_group_send_states', None)
        if states is None:
            states = self._qq_group_send_states = {}
            self._qq_group_state_locks = {}
        async with self._qq_group_state_locks.setdefault(chat, asyncio.Lock()):
            value, until = states.get(chat, (None, 0))
            if time.monotonic() >= until:
                try:
                    result = await original(self, 'GET', f'/v2/groups/{chat}/bot_state')
                    value = result.get('allow_proactive_msg')
                    value = value if isinstance(value, bool) else None
                except PermissionError:
                    raise
                except Exception:
                    value = None  # A missing read permission does not deny the send API.
                states[chat] = (value, time.monotonic() + 60)
            return value

    @wraps(original)
    async def request(self, method, path, body=None, *args, **kwargs):
        match = _PATH.fullmatch(path)
        # Event replies have their own contract; C2C streaming is unchanged.
        if method.upper() != 'POST' or not match or not isinstance(body, dict) or body.get('event_id'):
            return await original(self, method, path, body, *args, **kwargs)
        chat = match[1]
        bot = str(self._app_id)
        scoped = _ANCHOR.get()
        anchor = body.get('msg_id') or (scoped[2] if scoped and scoped[:2] == (id(self), chat) else None)
        store = ledger(self)
        try:
            fresh = bool(anchor) and await asyncio.to_thread(store.group_anchor, bot, str(anchor), chat)
        except BudgetDenied as exc:
            raise _terminal(exc) from exc

        if await permission(self, chat) is not False:
            wire = {k: v for k, v in body.items() if k not in ('msg_id', 'event_id', 'message_reference')}
            while delay := await asyncio.to_thread(store.group_send_delay, bot, chat):
                await asyncio.sleep(delay)
            token = _EXPIRED_REPLY_STANDALONE.set(chat)
            try:
                response = await original(self, method, path, wire, *args, **kwargs)
                if not response.get('id'):
                    raise RuntimeError('QQ response unconfirmed: missing message id')
                return response
            except PermissionError:
                raise
            except Exception as exc:
                if not _permission_denied(exc):
                    raise _terminal(exc) from exc
                self._qq_group_send_states[chat] = (False, time.monotonic() + 60)
            finally:
                _EXPIRED_REPLY_STANDALONE.reset(token)

        if not fresh:
            anchor = _recent_group_reply_anchor(self, chat)
            try:
                fresh = bool(anchor) and await asyncio.to_thread(store.group_anchor, bot, anchor, chat)
            except BudgetDenied as exc:
                raise _terminal(exc) from exc
        if not fresh:
            raise _terminal(BudgetDenied('qq_reply_budget_forbidden: no authenticated group msg_id within 60 minutes'))
        wire = dict(body, msg_id=anchor)
        if 'message_reference' in wire:
            wire['message_reference'] = {'message_id': anchor}
        try:
            return await original(self, method, path, wire, *args, **kwargs)
        except PermissionError:
            raise
        except Exception as exc:
            raise _terminal(exc) from exc

    request._qq_group_delivery_wrapped = True
    adapter_class._api_request = request
    logging.getLogger(__name__).info('QQ group proactive-first delivery enabled; passive fallback window is 60 minutes')
