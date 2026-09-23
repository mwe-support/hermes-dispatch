"""Guard explicit QQ cron routes and optional per-job pins without core edits."""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import re

from .channel_directory import lookup_channel_directory_type

_TARGET = contextvars.ContextVar('qq_cron_delivery_target', default=None)
_MARKER = '_qq_cron_delivery_guard_wrapped'
logger = logging.getLogger(__name__)


def _pinned_target(job, normalize):
    from hermes_constants import get_hermes_home

    deliver = normalize(job.get('deliver', 'local')).strip()
    if deliver == 'local':
        return None
    path = get_hermes_home() / 'cron' / 'delivery-targets.json'
    try:
        pins = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        pins = {}
    if not isinstance(pins, dict):
        raise ValueError('delivery-targets.json must be an object keyed by job ID')
    parts = [part.strip() for part in deliver.split(',')]
    if len(parts) > 1 and any(part.lower().startswith('qqbot:') for part in parts):
        raise ValueError('explicit QQ multi-target delivery is unsupported; use separate jobs')
    if deliver.split(':', 1)[0].lower() == 'qqbot':
        deliver = 'qqbot' + deliver[len('qqbot'):]
    if job.get('id') in pins:
        pin = pins[job['id']]
    elif deliver.startswith('qqbot:'):
        chat_id = deliver.split(':', 1)[1]
        # Only this profile's durable directory/origin can supply the type;
        # never consult another profile or a transient adapter cache.
        origin = job.get('origin') or {}
        chat_type = lookup_channel_directory_type(
            chat_id, paths=[get_hermes_home() / 'channel_directory.json'])
        if not chat_type and isinstance(origin, dict) and origin.get('platform') == 'qqbot' and str(origin.get('chat_id')) == chat_id:
            chat_type = origin.get('chat_type')
        if not chat_type:
            raise ValueError('explicit QQ target has no durable chat type; configure delivery-targets.json')
        pin = {'chat_id': chat_id, 'chat_type': chat_type}
    else:
        return None
    if not isinstance(pin, dict) or set(pin) != {'chat_id', 'chat_type'}:
        raise ValueError('QQ delivery pin requires exactly chat_id and chat_type')
    chat_id = pin['chat_id']
    if not isinstance(chat_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', chat_id):
        raise ValueError('QQ delivery pin requires an exact native chat ID')
    # Hermes labels private sessions 'dm'; QQ calls the same route 'c2c'.
    chat_type = 'c2c' if pin['chat_type'] == 'dm' else pin['chat_type']
    if chat_type not in {'group', 'c2c', 'guild'}:
        raise ValueError('QQ delivery pin chat_type must be group, c2c, dm or guild')
    # ponytail: one QQ target per pinned job; split jobs for intentional fan-out.
    if deliver not in {'origin', 'qqbot', 'qqbot:' + chat_id}:
        raise ValueError('QQ delivery pin conflicts with the job deliver target')
    return {**pin, 'chat_type': chat_type, 'confirmed': 0}


def patch_cron_delivery(QQAdapter):
    from cron import scheduler
    from gateway.config import Platform
    from gateway.platforms.base import SendResult
    from tools import send_message_tool

    if getattr(scheduler._deliver_result, _MARKER, False):
        return
    original_deliver = scheduler._deliver_result
    original_targets = scheduler._resolve_delivery_targets
    original_standalone = send_message_tool._send_to_platform
    original_guess = QQAdapter._guess_chat_type

    @functools.wraps(original_deliver)
    def deliver(job, content, adapters=None, loop=None):
        try:
            target = _pinned_target(job, scheduler._normalize_deliver_value)
        except (OSError, ValueError, TypeError) as exc:
            return f'QQ cron delivery guard: invalid pin configuration ({exc})'
        if target is None:
            return original_deliver(job, content, adapters, loop)
        adapter = (adapters or {}).get(Platform.QQBOT)
        if not isinstance(adapter, QQAdapter) or loop is None or not loop.is_running():
            return 'QQ cron delivery guard: pinned target requires a live QQ adapter and Gateway loop'
        token = _TARGET.set(target)
        try:
            error = original_deliver({**job, 'deliver': 'qqbot:' + target['chat_id']},
                                     content, adapters, loop)
            error = error or target.get('error')
            if not error and not target['confirmed']:
                error = 'QQ cron delivery guard: no confirmed target receipt; delivery unconfirmed (no fallback)'
            if error:
                logger.warning('QQ cron delivery guard: job=%s delivery failed or unconfirmed: %s', job.get('id'), error)
            return error
        except Exception as exc:
            return f'QQ cron delivery guard: delivery failed ({exc})'
        finally:
            _TARGET.reset(token)

    @functools.wraps(original_targets)
    def targets(job):
        target = _TARGET.get()
        if target is None:
            return original_targets(job)
        return [{'platform': 'qqbot', 'chat_id': target['chat_id'],
                 'thread_id': None, '_resolved_from': 'explicit'}]

    @functools.wraps(original_standalone)
    async def standalone(*args, **kwargs):
        target = _TARGET.get()
        if target is not None:
            # The upstream standalone QQ sender guesses channel/C2C/group in
            # sequence. Never use it after a pinned send fails or times out.
            reason = target.get('error') or 'no confirmed live receipt'
            return {'success': False, 'error': f'QQ cron delivery guard: {reason}; standalone fallback disabled for pinned target'}
        return await original_standalone(*args, **kwargs)

    @functools.wraps(original_guess)
    def guess(self, chat_id):
        target = _TARGET.get()
        if target is not None:
            if str(chat_id) != target['chat_id']:
                raise ValueError('QQ cron delivery guard: refusing a different destination')
            return target['chat_type']
        return original_guess(self, chat_id)

    def wrap_send(original):
        @functools.wraps(original)
        async def send(self, chat_id, *args, **kwargs):
            target = _TARGET.get()
            if target is not None and str(chat_id) != target['chat_id']:
                target['error'] = 'destination mismatch'
                return SendResult(success=False, error='QQ cron delivery guard: destination mismatch', retryable=False)
            try:
                result = await original(self, chat_id, *args, **kwargs)
            except Exception as exc:
                if target is not None:
                    target['error'] = str(exc)
                raise
            if target is not None and not getattr(result, 'success', False):
                target['error'] = getattr(result, 'error', None) or 'unconfirmed QQ adapter result'
            if target is not None and getattr(result, 'success', False):
                raw = getattr(result, 'raw_response', None)
                if isinstance(raw, dict) and raw.get('id') and str(raw['id']) == str(getattr(result, 'message_id', None)):
                    target['confirmed'] += 1
                else:
                    target['error'] = 'QQ cron delivery guard: unconfirmed QQ API message receipt'
            return result
        return send

    setattr(deliver, _MARKER, True)
    scheduler._deliver_result = deliver
    scheduler._resolve_delivery_targets = targets
    send_message_tool._send_to_platform = standalone
    QQAdapter._guess_chat_type = guess
    QQAdapter.send = wrap_send(QQAdapter.send)
    QQAdapter._send_media = wrap_send(QQAdapter._send_media)
