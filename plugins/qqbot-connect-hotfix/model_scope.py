"""Do not turn a successful QQ global model selection into a session pin."""
from __future__ import annotations

import contextvars
import functools

_SCOPE = contextvars.ContextVar('qq_global_model_scope', default=None)
_MARKER = '_qq_global_model_scope_wrapped'


async def _run_global(runner, source, call):
    source = runner._normalize_source_for_session_key(source)
    key = runner._session_key_for_source(source)
    scope = {'runner': runner, 'source': source, 'key': key, 'saved': False,
             'before': runner._session_model_overrides.get(key)}
    token = _SCOPE.set(scope)
    try:
        return await call()
    finally:
        try:
            if scope['saved']:
                # Clear durable state first: otherwise restart rehydrates the pin.
                await runner.async_session_store.set_model_override(key, None)
                runner._session_model_overrides.pop(key, None)
                getattr(runner, '_pending_model_notes', {}).pop(key, None)
                runner._evict_cached_agent(key)
        finally:
            _SCOPE.reset(token)


def patch_global_model_scope(GatewayRunner):
    from hermes_cli import config
    from hermes_cli.model_switch import parse_model_switch_args, resolve_persist_behavior

    original = GatewayRunner._handle_model_command
    if not getattr(original, _MARKER, False):
        @functools.wraps(original)
        async def model(self, event):
            request = parse_model_switch_args(event.get_command_args().strip())
            if (getattr(event.source.platform, 'value', event.source.platform) == 'qqbot'
                    and not request.errors and (request.target or request.explicit_provider)
                    and resolve_persist_behavior(request.is_global, request.is_session,
                        is_once=request.is_once, explicit_provider=request.explicit_provider)):
                return await _run_global(self, event.source, lambda: original(self, event))
            return await original(self, event)
        setattr(model, _MARKER, True)
        GatewayRunner._handle_model_command = model

    confirm_original = GatewayRunner._request_slash_confirm
    if not getattr(confirm_original, _MARKER, False):
        @functools.wraps(confirm_original)
        async def confirm(self, *args, **kwargs):
            scope = _SCOPE.get()
            if scope and kwargs.get('command') == 'model':
                handler = kwargs['handler']
                async def scoped_handler(choice):
                    return await _run_global(self, scope['source'], lambda: handler(choice))
                kwargs['handler'] = scoped_handler
            return await confirm_original(self, *args, **kwargs)
        setattr(confirm, _MARKER, True)
        GatewayRunner._request_slash_confirm = confirm

    save_original = config.save_config
    if not getattr(save_original, _MARKER, False):
        @functools.wraps(save_original)
        def save(cfg, *args, **kwargs):
            result = save_original(cfg, *args, **kwargs)
            scope = _SCOPE.get()
            if scope:
                override = scope['runner']._session_model_overrides.get(scope['key'])
                model_cfg = cfg.get('model', {})
                if (override and override is not scope['before'] and isinstance(model_cfg, dict)
                        and model_cfg.get('default') == override.get('model')
                        and model_cfg.get('provider') == override.get('provider')):
                    scope['saved'] = True
            return result
        setattr(save, _MARKER, True)
        config.save_config = save
