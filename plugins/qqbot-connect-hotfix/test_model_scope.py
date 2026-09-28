"""Real QQ /model persistence: global callers keep inheriting later defaults."""
import asyncio
import importlib.util
import os
from pathlib import Path
import tempfile
from unittest.mock import AsyncMock, patch
import yaml


def main():
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'HERMES_HOME': tmp}):
        import gateway.run as gw
        from gateway.config import GatewayConfig, Platform
        from gateway.session import SessionStore, SessionSource
        from gateway.platforms.base import MessageEvent
        from hermes_cli import config, model_switch
        path = Path(tmp, 'config.yaml')
        path.write_text(yaml.safe_dump({'model': {'default': 'model-old', 'provider': 'openai-codex'}}))
        candidate = Path(__file__).with_name('model_scope.py')
        if candidate.exists():
            spec = importlib.util.spec_from_file_location('scope_test_patch', candidate)
            mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
            mod.patch_global_model_scope(gw.GatewayRunner)
        stores = []
        def runner():
            r = object.__new__(gw.GatewayRunner)
            r.config = GatewayConfig(); r.adapters = {}; r._voice_mode = {}
            r._session_model_overrides = {}; r._running_agents = {}; r._session_db = None
            r.session_store = SessionStore(Path(tmp, 'sessions'), r.config)
            stores.append(r.session_store)
            r._async_session_store = gw.AsyncSessionStore(r.session_store)
            return r
        def event(chat, text):
            return MessageEvent(text=text, source=SessionSource(platform=Platform.QQBOT,
                chat_id=chat, chat_type='dm', user_id='test-user'))
        def switch(**kw):
            return model_switch.ModelSwitchResult(success=True, new_model=kw['raw_input'],
                target_provider='openai-codex', api_key='test-only',
                api_mode='codex_app_server', is_global=kw['is_global'])
        async def scenario():
            r = runner()
            a,b,pinned = [event(x,'') for x in ['a','b','pinned']]
            for e in [a,b,pinned]:r.session_store.get_or_create_session(e.source)
            async def command(e,text):
                e.text=text
                return await r._handle_model_command(e)
            def resolved(e):
                return r._resolve_session_agent_runtime(source=e.source)[0]
            await command(pinned,'/model model-pinned --session')
            await command(a,'/model model-a --global')
            await command(b,'/model model-b --global')
            assert resolved(a)=='model-b', f'old --global caller stayed pinned: {resolved(a)}'
            assert resolved(b)=='model-b'
            assert r._session_key_for_source(a.source) not in r._pending_model_notes
            assert resolved(pinned)=='model-pinned', 'explicit session selection must remain'
            r=runner()
            assert resolved(a)=='model-b', 'global inheritance must survive restart'
            assert resolved(pinned)=='model-pinned'
            # A failed config write must keep the native session fallback.
            with patch.object(config,'save_config',side_effect=OSError('synthetic write failure')):
                await command(a,'/model model-unsaved --global')
            assert resolved(a)=='model-unsaved'
            assert yaml.safe_load(path.read_text())['model']['default']=='model-b'
            # A deferred confirmation must retain the global scope, including cancellation.
            saved={}
            async def confirm(self,**kw):saved['handler']=kw['handler'];return 'confirm'
            # Patch the original confirmation seam before re-installing the plugin wrapper.
            with patch.object(gw.GatewayRunner,'_request_slash_confirm',confirm):
                if candidate.exists():mod.patch_global_model_scope(gw.GatewayRunner)
                with patch('hermes_cli.model_selection_guards.combined_selection_warning',
                           return_value=type('Warning',(),{'title':'test','message':'test'})()):
                    await command(a,'/model model-confirmed --global')
                    await saved['handler']('cancel')
                    assert resolved(a)=='model-unsaved'
                    await command(a,'/model model-confirmed --global')
                    await saved['handler']('once')
                assert resolved(a)=='model-confirmed'
                assert r.session_store.get_model_override(r._session_key_for_source(a.source)) is None
                await command(b,'/model model-final --global')
                assert resolved(a)=='model-final'
            print('global inheritance, restart, explicit session, save failure, deferred approval: PASS')
        with patch.object(gw,'_hermes_home',Path(tmp)), \
             patch.object(model_switch,'switch_model',side_effect=switch), \
             patch.object(model_switch,'resolve_display_context_length_async',AsyncMock(return_value=None)), \
             patch('hermes_cli.context_switch_guard.enrich_model_switch_warnings_for_gateway'), \
             patch('hermes_cli.model_selection_guards.combined_selection_warning',return_value=None), \
             patch.object(gw,'_resolve_runtime_agent_kwargs',return_value={'provider':'openai-codex','api_key':'test-only'}), \
             patch.object(gw,'_resolve_runtime_agent_kwargs_for_provider',return_value={'provider':'openai-codex','api_key':'test-only'}):
            try:
                asyncio.run(scenario())
            finally:
                for store in stores:
                    close = getattr(store, 'close_all_db_handles', None)
                    if close:
                        close()
                    elif getattr(store, '_db', None):
                        store._db.close()

if __name__=='__main__':main()
