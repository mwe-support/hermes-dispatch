"""Guard explicit QQ cron routes and optional per-job pins without core edits."""
from __future__ import annotations

import contextvars
import functools
import json
import logging
import os
from pathlib import Path
import re
import sys

from .channel_directory import lookup_channel_directory_type
from .cron_binding import binding_target, target as source_target

_TARGET = contextvars.ContextVar('qq_cron_delivery_target', default=None)
_MARKER = '_qq_cron_delivery_guard_wrapped'
logger = logging.getLogger(__name__)
_EXECUTION = contextvars.ContextVar('qq_cron_execution', default=False)
AUTO_ENV = 'HERMES_QQ_CRON_AUTO_DELIVERY'
_SEND_MARKER = '_qq_cron_manual_send_wrapped'


def _owns_qq_delivery():
    return _EXECUTION.get() or os.environ.get(AUTO_ENV) == '1'


def patch_cron_send_message(module):
    """Only the scheduler may send a QQ-auto-delivered job's output."""
    original = module._send_to_platform
    if getattr(original, _SEND_MARKER, False):
        return
    refusal = {'success': False, 'error': 'QQ cron delivery is automatic; extra QQ send_message '
               'is blocked. Put the intended content in the final response.'}

    @functools.wraps(original)
    async def send(platform, *args, **kwargs):
        if _owns_qq_delivery() and getattr(platform, 'value', platform) == 'qqbot':
            return dict(refusal)
        return await original(platform, *args, **kwargs)

    setattr(send, _SEND_MARKER, True)
    module._send_to_platform = send
    original_skip = getattr(module, '_maybe_skip_cron_duplicate_send', None)
    if original_skip is not None:
        @functools.wraps(original_skip)
        def skip(platform_name, *args, **kwargs):
            result = original_skip(platform_name, *args, **kwargs)
            if _owns_qq_delivery() and platform_name == 'qqbot':
                return result or dict(refusal)
            return result
        module._maybe_skip_cron_duplicate_send = skip


def _patch_execution_scope(scheduler):
    from tools.environments import local
    original_run = scheduler.run_job
    marker = '_qq_cron_execution_wrapped'
    if getattr(original_run, marker, False):
        return

    @functools.wraps(original_run)
    def run(job, *args, **kwargs):
        origin = job.get('origin') or {}
        deliver = scheduler._normalize_deliver_value(job.get('deliver', 'local')).strip()
        qq = bool(binding_target(job)) or (deliver != 'local' and (
            origin.get('platform') == 'qqbot' or any(
                part.strip().split(':', 1)[0].lower() == 'qqbot' for part in deliver.split(','))))
        token = _EXECUTION.set(qq or _EXECUTION.get())
        try:
            return original_run(job, *args, **kwargs)
        finally:
            _EXECUTION.reset(token)

    def scoped_environment(env):
        if _owns_qq_delivery():
            from cron import jobs
            bootstrap = str(Path(__file__).parent / 'cron_bootstrap')
            core = str(Path(jobs.__file__).resolve().parents[1])
            env.update({AUTO_ENV: '1', 'HERMES_QQ_CRON_BOOTSTRAP': bootstrap,
                        'HERMES_QQ_CRON_CORE': core})
            env['PYTHONPATH'] = os.pathsep.join([bootstrap, core, env.get('PYTHONPATH', '')])
        return env

    def environment(original):
        @functools.wraps(original)
        def build(*args, **kwargs):
            # Retain upstream credential scrubbing before adding policy context.
            return scoped_environment(original(*args, **kwargs))
        return build

    setattr(run, marker, True)
    replacements = [(original_run, run)] + [
        (getattr(local, name), environment(getattr(local, name)))
        for name in ('build_subprocess_env', 'hermes_subprocess_env')]
    original_python = getattr(scheduler, '_windows_cron_python_invocation', None)
    if original_python is not None:
        @functools.wraps(original_python)
        def python_invocation(*args, **kwargs):
            executable, overlay = original_python(*args, **kwargs)
            # Hermes applies this after build_subprocess_env on Windows uv
            # installs. Keep its venv/.pth overlay without losing the QQ hook.
            if overlay and _owns_qq_delivery():
                overlay = scoped_environment(dict(overlay))
            return executable, overlay
        replacements.append((original_python, python_invocation))
    for name, module in list(sys.modules.items()):
        if module is None or name.split('.')[0] not in {'cron', 'tools', 'agent', 'gateway', 'hermes_cli'}:
            continue
        for attr, value in list(vars(module).items()):
            for before, after in replacements:
                if value is before:
                    setattr(module, attr, after)


def _pinned_target(job, normalize):
    from hermes_constants import get_hermes_home

    deliver = normalize(job.get('deliver', 'local')).strip()
    bound = binding_target(job)
    if deliver == 'local' and bound is None:
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
    origin = job.get('origin') or {}
    if bound is None and isinstance(origin, dict) and origin.get('platform') == 'qqbot':
        # Legacy QQ jobs also use their durable origin, never the current home.
        bound = source_target(origin.get('chat_id'), origin.get('chat_type'))
    if bound is not None:
        pin = bound
        configured = pins.get(job.get('id'))
        if configured is not None and (
            not isinstance(configured, dict)
            or set(configured) != {'chat_id', 'chat_type'}
            or source_target(configured.get('chat_id'), configured.get('chat_type')) != bound
        ):
            raise ValueError('QQ delivery pin conflicts with the persisted conversation binding')
    elif job.get('id') in pins:
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
    elif deliver in {'qqbot', 'origin'} and (deliver == 'qqbot' or origin.get('platform') == 'qqbot'):
        raise ValueError('QQ cron delivery has no confirmed origin; refusing home fallback')
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

    patch_cron_send_message(send_message_tool)
    _patch_execution_scope(scheduler)

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
