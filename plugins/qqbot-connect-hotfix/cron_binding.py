"""Bind QQ-created cron jobs to their inbound conversation, before persistence."""
from __future__ import annotations

import contextvars
import functools
import inspect
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .channel_directory import lookup_channel_directory_type

BINDING = '_qq_delivery_binding'
CONTEXT_ENV = 'HERMES_QQ_CRON_CONTEXT'
_REQUEST = contextvars.ContextVar('qq_cron_request', default=None)
_CHILD_CONTEXT = contextvars.ContextVar('qq_cron_child_context', default='')
_MARKER = '_qq_cron_binding_wrapped'
_VERBS = r'投递到|发送到|推送到|发到|发给|send to|deliver to'


def _home():
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def target(chat_id, chat_type=None):
    if not isinstance(chat_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]+', chat_id):
        raise ValueError('QQ cron binding requires an exact conversation ID')
    chat_type = 'c2c' if chat_type == 'dm' else chat_type
    if not chat_type:
        chat_type = lookup_channel_directory_type(chat_id, paths=[_home() / 'channel_directory.json'])
    if chat_type not in {'group', 'c2c'}:
        raise ValueError('QQ cron binding requires a known group or private conversation type')
    return {'chat_id': chat_id, 'chat_type': chat_type}


def _requested_target(text, source):
    """Accept a delivery clause with an exact ID or unique directory name.

    Ambiguous prose is not authorization. Quoted examples, negatives and model
    tool arguments cannot supply the exception to the source binding.
    """
    text = re.sub(r'```.*?(?:```|\Z)', '', text or '', flags=re.S)
    clauses = re.split(r'[\n。！？!?；;,，]', text)
    destinations = []
    for clause in clauses:
        if clause.lstrip().startswith(('>', '"', "'", '“', '「')):
            continue
        match = re.fullmatch(
            r'\s*(?:请)?\s*(?:(?:把|将)?(?:结果|报告|消息|任务输出)\s*)?'
            rf'(?:{_VERBS})\s*[:：]?\s*(.+?)\s*',
            clause, flags=re.I)
        if match:
            destinations.append(match[1].strip('「」“”\"\' '))
        elif re.search(_VERBS, clause, re.I) and not re.search(r'不要|不许|禁止|别|do not|don.t', clause, re.I):
            raise ValueError('QQ cron binding: unclear delivery instruction; '
                             'put 投递到：<conversation name or qqbot:ID> in its own clause')
    if not destinations:
        return None
    if len(destinations) != 1:
        raise ValueError('QQ cron binding: specify exactly one delivery conversation')
    name = destinations[0]
    if name in {'当前会话', '本群', '本会话', '这里'}:
        return source
    try:
        data = json.loads((_home() / 'channel_directory.json').read_text())
    except FileNotFoundError:
        data = {}
    entries = data.get('platforms', {}).get('qqbot', [])
    native_id = name[6:] if name.lower().startswith('qqbot:') else name
    matches = [entry for entry in entries if isinstance(entry, dict)
               and (entry.get('id') == native_id or entry.get('name') == name)]
    unique = {str(entry.get('id')): entry for entry in matches}
    if native_id == source['chat_id']:
        return source
    if len(unique) != 1:
        raise ValueError('QQ cron binding: delivery conversation is unknown or ambiguous; '
                         'use 投递到：qqbot:<exact ID> or a unique known conversation name')
    entry = next(iter(unique.values()))
    return target(entry['id'], entry.get('type'))


def capture_request(*, event=None, **_):
    _REQUEST.set(None)
    source = getattr(event, 'source', None)
    platform = getattr(source, 'platform', None)
    if getattr(platform, 'value', platform) != 'qqbot':
        return
    try:
        origin = target(str(source.chat_id), str(source.chat_type))
        request = {'source': origin, 'message_id': str(getattr(source, 'message_id', '')
                                                    or getattr(event, 'message_id', '') or '')}
        try:
            request['requested'] = _requested_target(getattr(event, 'text', ''), origin)
        except (OSError, ValueError, TypeError) as exc:
            request['error'] = str(exc)
        _REQUEST.set(request)
    except (ValueError, TypeError):
        _REQUEST.set({'error': 'QQ cron binding: inbound conversation type is unavailable'})


def _current_request():
    from gateway.session_context import get_session_env
    pointer = os.environ.get(CONTEXT_ENV)
    if pointer:
        path = Path(pointer)
        root = (_home() / 'cron' / 'qq-context').resolve()
        if path.is_symlink() or path.resolve().parent != root:
            raise ValueError('QQ cron binding: invalid profile context path')
        try:
            request = json.loads(path.read_text())
            pid = request.get('gateway_pid')
            if not isinstance(pid, int) or pid <= 0:
                raise ValueError('missing Gateway owner')
            try:
                os.kill(pid, 0)
            except PermissionError:
                pass  # An existing owner can be invisible to a tool sandbox.
            return request
        except (OSError, ValueError) as exc:
            raise ValueError('QQ cron binding: creating QQ turn is no longer active') from exc
    if get_session_env('HERMES_CRON_SESSION') == '1':
        return None
    if get_session_env('HERMES_SESSION_PLATFORM') != 'qqbot':
        return None
    source = target(get_session_env('HERMES_SESSION_CHAT_ID'),
                    get_session_env('HERMES_SESSION_CHAT_TYPE'))
    request = _REQUEST.get()
    if request and request.get('source') == source:
        message_id = get_session_env('HERMES_SESSION_MESSAGE_ID')
        if message_id and request.get('message_id') == message_id:
            return request
    # Missing original text can never authorize another destination.
    return {'source': source, 'requested': None}


def _select(deliver, request, existing=None):
    if request and request.get('error'):
        raise ValueError(request['error'])
    selected = (request or {}).get('requested') or existing or request['source']
    selected = target(selected['chat_id'], selected['chat_type'])
    value = str(deliver or '').strip()
    if value not in {'', 'origin', 'qqbot', 'local', 'qqbot:' + selected['chat_id']}:
        raise ValueError('QQ cron binding: destination was not explicitly requested by the user; '
                         'use a delivery clause in the original message')
    return selected


def binding_target(job):
    origin = job.get('origin') or {}
    binding = origin.get(BINDING) if isinstance(origin, dict) else None
    if binding is None:
        return None
    if not isinstance(binding, dict) or binding.get('version') != 1:
        raise ValueError('QQ cron binding: invalid persisted binding')
    return target(binding.get('chat_id'), binding.get('chat_type'))


def _bound_origin(origin, selected, source):
    return {**(origin or {}), 'platform': 'qqbot', **source,
            BINDING: {'version': 1, **selected}}


def patch_job_binding():
    from cron import jobs
    if getattr(jobs.create_job, _MARKER, False):
        return
    original_create, original_update = jobs.create_job, jobs.update_job
    signature = inspect.signature(original_create)

    @functools.wraps(original_create)
    def create(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        values = bound.arguments
        request = _current_request()
        origin = values.get('origin') or {}
        if not isinstance(origin, dict):
            raise ValueError('QQ cron binding: origin must be an object')
        if request is None and origin.get('platform') == 'qqbot':
            request = {'source': target(origin.get('chat_id'), origin.get('chat_type'))}
        if request:
            selected = _select(values.get('deliver'), request)
            values['origin'] = _bound_origin(origin, selected, request['source'])
            values['deliver'] = 'qqbot:' + selected['chat_id']
        return original_create(*bound.args, **bound.kwargs)

    @functools.wraps(original_update)
    def update(job_id, updates):
        # Hermes' re-entrant jobs lock keeps the binding read + update atomic.
        with jobs._jobs_lock():
            job = jobs.get_job(job_id)
            if job is None:
                return original_update(job_id, updates)
            origin = job.get('origin') or {}
            selected = binding_target(job)
            if selected is None and origin.get('platform') == 'qqbot':
                selected = target(origin.get('chat_id'), origin.get('chat_type'))
            if selected is None:
                return original_update(job_id, updates)
            values = dict(updates)
            request = _current_request()
            selected = _select(values.get('deliver'), request if 'deliver' in values else None, selected)
            # Changing schedules/prompts or the editing chat must not rebind.
            values['origin'] = _bound_origin(origin, selected,
                                             target(origin['chat_id'], origin.get('chat_type')))
            values['deliver'] = 'qqbot:' + selected['chat_id']
            return original_update(job_id, values)

    setattr(create, _MARKER, True)
    # Repair aliases imported before discovery as well as future imports.
    for name, module in list(sys.modules.items()):
        if name.split('.')[0] not in {'cron', 'tools', 'hermes_cli', 'gateway'} or module is None:
            continue
        for attr, value in list(vars(module).items()):
            if value is original_create:
                setattr(module, attr, create)
            elif value is original_update:
                setattr(module, attr, update)


def patch_codex_context():
    """Carry the authenticated turn into Codex's persistent tool subprocesses."""
    from agent import codex_runtime
    from agent.transports.codex_app_server import CodexAppServerClient
    if getattr(codex_runtime.run_codex_app_server_turn, _MARKER, False):
        return
    original_run = codex_runtime.run_codex_app_server_turn
    original_init = CodexAppServerClient.__init__

    @functools.wraps(original_run)
    def run(agent, *args, **kwargs):
        request = _current_request()
        path = None
        if request:
            root = _home() / 'cron' / 'qq-context'
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not getattr(agent, '_qq_cron_context_path', None):
                agent._qq_cron_context_path = root / (uuid.uuid4().hex + '.json')
            path = agent._qq_cron_context_path
            temporary = path.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as handle:
                os.chmod(temporary, 0o600)
                json.dump({**request, 'gateway_pid': os.getpid()}, handle)
            temporary.replace(path)
        token = _CHILD_CONTEXT.set(str(path) if path else '')
        try:
            return original_run(agent, *args, **kwargs)
        finally:
            _CHILD_CONTEXT.reset(token)
            if path:
                path.unlink(missing_ok=True)

    @functools.wraps(original_init)
    def init(self, *args, env=None, **kwargs):
        child_env = {**(env or {}), CONTEXT_ENV: _CHILD_CONTEXT.get()}
        if _CHILD_CONTEXT.get():
            from cron import jobs
            core = str(Path(jobs.__file__).resolve().parents[1])
            child_env['HERMES_QQ_CRON_BOOTSTRAP'] = str(Path(__file__).parent / 'cron_bootstrap')
            child_env['PYTHONPATH'] = os.pathsep.join([
                child_env['HERMES_QQ_CRON_BOOTSTRAP'], core,
                child_env.get('PYTHONPATH', os.environ.get('PYTHONPATH', '')),
            ])
        return original_init(self, *args, env=child_env, **kwargs)

    setattr(run, _MARKER, True)
    codex_runtime.run_codex_app_server_turn = run
    CodexAppServerClient.__init__ = init


def register_binding(ctx):
    patch_job_binding()
    patch_codex_context()
    if ctx is not None and hasattr(ctx, 'register_hook'):
        ctx.register_hook('pre_gateway_dispatch', capture_request)
