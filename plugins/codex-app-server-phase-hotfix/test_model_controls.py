"""Hermes channel slash commands -> resolved Agent -> real Codex RPC assembly."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import yaml


def main():
    with tempfile.TemporaryDirectory(prefix='hermes-model-controls-') as tmp, patch.dict(os.environ, {
        'HERMES_HOME': tmp, 'CODEX_HOME': str(Path(tmp, 'codex')),
        'HERMES_CODEX_SESSION_PROJECTS_BACKFILL': 'false',
        'HERMES_CODEX_APP_REGISTER_PROJECTS': 'false',
        'HERMES_CODEX_SESSION_PROJECTS_ENABLED': 'true',
    }):
        config_path = Path(tmp, 'config.yaml')
        config_path.write_text(yaml.safe_dump({
            'model': {'default': 'gpt-5.6-sol', 'provider': 'openai-codex',
                      'openai_runtime': 'codex_app_server'},
            'agent': {'reasoning_effort': 'medium'},
        }))
        codex = Path(tmp, 'codex'); codex.mkdir()
        codex_config = codex / 'config.toml'
        codex_config.write_text('model = "gpt-6-astra"\nmodel_reasoning_effort = "high"\n')
        codex_before = codex_config.read_bytes()

        import gateway.run as gateway_run
        from gateway.config import Platform
        from gateway.platforms.base import MessageEvent
        from gateway.session import SessionSource
        from agent import codex_runtime
        from agent.transports import codex_app_server_session as sessions
        from agent.transports.codex_app_server import CodexAppServerClient
        from hermes_cli.model_switch import ModelSwitchResult

        class RecordingClient(CodexAppServerClient):
            instances = []
            def __init__(self, **kwargs):
                self._next_id = 1
                self._pending = {}
                self._pending_lock = threading.Lock()
                self._notifications = queue.Queue()
                self._server_requests = queue.Queue()
                self._closed = self._initialized = False
                self.sent = []
                self.hold_turn = False
                self.turn_started = threading.Event()
                self.instances.append(self)

            def _send(self, message):
                self.sent.append(message)
                if 'id' not in message:
                    return
                method, params = message['method'], message['params']
                result = {}
                if method == 'thread/start':
                    result = {'thread': {'id': f'test-thread-{len(self.instances)}'}}
                elif method == 'thread/resume':
                    result = {'thread': {'id': params['threadId']}}
                elif method == 'turn/start':
                    tid = f'test-turn-{message["id"]}'
                    result = {'turn': {'id': tid}}
                    self.turn_started.set()
                    if not self.hold_turn:
                        self._notifications.put({'method': 'item/completed', 'params': {
                            'threadId': params['threadId'], 'turnId': tid,
                            'item': {'type': 'agentMessage', 'id': tid + '-message',
                                     'phase': 'final_answer', 'text': 'ok'}}})
                        self._notifications.put({'method': 'turn/completed', 'params': {
                            'threadId': params['threadId'], 'turn': {'id': tid, 'status': 'completed'}}})
                self._pending.pop(message['id']).queue.put({'result': result})

            def is_alive(self):
                return not self._closed

            def stderr_tail(self, n=20):
                return []

            def close(self, timeout=3):
                self._closed = True

        plugin_path = Path(__file__).with_name('__init__.py')
        spec = importlib.util.spec_from_file_location('model_controls_test_plugin', plugin_path,
                    submodule_search_locations=[str(plugin_path.parent)])
        plugin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = plugin
        spec.loader.exec_module(plugin)
        plugin.register(SimpleNamespace(register_tool=Mock(), register_hook=Mock(), register_command=Mock()))
        request = CodexAppServerClient.request
        runtime = codex_runtime.run_codex_app_server_turn
        plugin.register(SimpleNamespace(register_tool=Mock(), register_hook=Mock(), register_command=Mock()))
        assert CodexAppServerClient.request is request
        assert codex_runtime.run_codex_app_server_turn is runtime

        def make_runner():
            r = object.__new__(gateway_run.GatewayRunner)
            r.adapters = {}; r._voice_mode = {}; r._session_model_overrides = {}
            r._session_reasoning_overrides = {}; r._running_agents = {}
            r._session_db = None
            r.session_store = SimpleNamespace()
            r._async_session_store = SimpleNamespace(_store=r.session_store, set_model_override=AsyncMock())
            return r

        def event(platform, text):
            return MessageEvent(text=text, source=SessionSource(
                platform=platform, chat_id=platform.value + '-test', chat_type='dm', user_id='test-user'))

        def switched(**kwargs):
            return ModelSwitchResult(success=True, new_model=kwargs['raw_input'],
                target_provider='openai-codex', provider_label='OpenAI Codex',
                api_key='test-only', base_url='https://chatgpt.com/backend-api/codex',
                api_mode='codex_app_server', is_global=kwargs.get('is_global', False))

        def resolved_agent(runner, source, existing=None):
            key = runner._session_key_for_source(source)
            cfg = yaml.safe_load(config_path.read_text())
            model, _ = runner._apply_session_model_override(key, cfg['model']['default'], {})
            reasoning = runner._resolve_session_reasoning_config(source=source, model=model)
            agent = existing or SimpleNamespace(_gateway_session_key=key, session_id='test-session',
                session_cwd=tmp, _codex_session=None, _skill_nudge_interval=0,
                _session_db=None, session_api_calls=0, context_compressor=None)
            agent.model, agent.reasoning_config = model, reasoning
            return agent

        def run(agent):
            result = codex_runtime.run_codex_app_server_turn(agent, user_message='hello',
                original_user_message='hello', messages=[], effective_task_id='test-task')
            assert result['completed'], result
            sent = agent._codex_session._client.sent
            turn = next(m['params'] for m in reversed(sent) if m.get('method') == 'turn/start')
            expected = agent.reasoning_config or {}
            effort = 'none' if expected.get('enabled') is False else expected.get('effort', 'medium')
            assert turn.get('model') == agent.model, turn
            assert turn.get('effort') == effort, turn
            return result['codex_thread_id']

        async def channels():
            runner = make_runner()
            agents = []
            for platform in (Platform.QQBOT, Platform.WHATSAPP):
                source = event(platform, '').source
                assert '/model' in await runner._handle_help_command(event(platform, '/help'))
                initial_config = config_path.read_bytes()
                reply = await runner._handle_model_command(event(platform, '/model gpt-5.6-luna --session'))
                assert 'gpt-5.6-luna' in reply
                assert 'gpt-5.6-luna' in await runner._handle_model_command(event(platform, '/model'))
                assert config_path.read_bytes() == initial_config
                await runner._handle_reasoning_command(event(platform, '/reasoning high'))
                agent = resolved_agent(runner, source)
                thread = run(agent)
                await runner._handle_model_command(event(platform, '/model gpt-5.6-sol --session'))
                await runner._handle_reasoning_command(event(platform, '/reasoning low'))
                assert run(resolved_agent(runner, source, agent)) == thread
                await runner._handle_reasoning_command(event(platform, '/reasoning off'))
                assert run(resolved_agent(runner, source, agent)) == thread
                assert agent.reasoning_config['effort'] == 'low'  # off hides display only
                await runner._handle_reasoning_command(event(platform, '/reasoning reset'))
                assert run(resolved_agent(runner, source, agent)) == thread
                assert agent.reasoning_config['effort'] == 'medium'
                await runner._handle_reasoning_command(event(platform, '/reasoning none'))
                assert run(resolved_agent(runner, source, agent)) == thread
                # Rebuild the Agent like a cache eviction/restart, then resume its thread.
                agent._codex_session.close()
                agent = resolved_agent(runner, source)
                assert run(agent) == thread
                resume = next(m['params'] for m in agent._codex_session._client.sent
                              if m.get('method') == 'thread/resume')
                assert resume.get('model') == agent.model
                await runner._handle_model_command(event(platform, '/model gpt-5.6-luna --once'))
                run(resolved_agent(runner, source, agent))
                runner._restore_pending_one_turn_model_override(runner._session_key_for_source(source))
                run(resolved_agent(runner, source, agent))
                assert agent.model == 'gpt-5.6-sol'
                agents.append(agent)
            # Two simultaneous channels must keep their own selected settings.
            agents[0].model, agents[0].reasoning_config = 'gpt-5.6-luna', {'effort': 'low'}
            agents[1].model, agents[1].reasoning_config = 'gpt-5.6-sol', {'effort': 'high'}
            barrier = threading.Barrier(2)
            original_turn = sessions.CodexAppServerSession.run_turn
            def overlap(self, *args, **kwargs):
                barrier.wait(timeout=5)
                return original_turn(self, *args, **kwargs)
            with patch.object(sessions.CodexAppServerSession, 'run_turn', overlap), ThreadPoolExecutor(2) as pool:
                assert all(pool.map(run, agents))

            # --global persists through Hermes; the shared Codex config is untouched.
            await runner._handle_model_command(event(Platform.QQBOT, '/model gpt-5.6-luna --global'))
            await runner._handle_reasoning_command(event(Platform.QQBOT, '/reasoning xhigh --global'))
            saved = yaml.safe_load(config_path.read_text())
            assert saved['model']['default'] == 'gpt-5.6-luna'
            assert saved['agent']['reasoning_effort'] == 'xhigh'
            run(resolved_agent(runner, event(Platform.QQBOT, '').source, agents[0]))
            run(resolved_agent(runner, event(Platform.WHATSAPP, '').source, agents[1]))
            assert agents[1].model == 'gpt-5.6-sol'  # its session override still wins

            # /stop selects only the caller's run; the real session loop emits turn/interrupt.
            for platform, agent in zip((Platform.QQBOT, Platform.WHATSAPP), agents):
                source = event(platform, '/stop').source
                key = runner._session_key_for_source(source)
                runner._running_agents[key] = agent
                runner._async_session_store.get_or_create_session = AsyncMock(
                    return_value=SimpleNamespace(session_key=key))
                async def interrupt(session_key, source, **kwargs):
                    runner._running_agents.pop(session_key)._codex_session.request_interrupt()
                runner._interrupt_and_clear_session = interrupt
                client = agent._codex_session._client
                client.turn_started.clear(); client.hold_turn = True
                with ThreadPoolExecutor(1) as pool:
                    future = pool.submit(codex_runtime.run_codex_app_server_turn, agent,
                        user_message='wait', original_user_message='wait', messages=[], effective_task_id='stop-test')
                    assert await asyncio.to_thread(client.turn_started.wait, 5)
                    await runner._handle_stop_command(event(platform, '/stop'))
                    result = future.result(timeout=5)
                assert not result['completed'] and result['partial']
                stop = next(m['params'] for m in reversed(client.sent) if m.get('method') == 'turn/interrupt')
                assert set(stop) == {'threadId', 'turnId'}
                client.hold_turn = False
            # Unscoped client calls, help and unrelated RPCs retain their original behavior.
            client = RecordingClient()
            params = {'threadId': 'direct', 'input': []}
            client.request('turn/start', params)
            assert client.sent[-1]['params'] == params and 'model' not in params
            def explicit(agent):
                client.request('turn/start', {**params, 'model': 'native-model', 'effort': 'low'})
            plugin.model_controls.wrap_runtime_turn(explicit)(agents[0])
            assert client.sent[-1]['params']['model'] == 'native-model'
            assert client.sent[-1]['params']['effort'] == 'low'
            def failed(agent):
                raise RuntimeError('test failure')
            try:
                plugin.model_controls.wrap_runtime_turn(failed)(agents[0])
            except RuntimeError:
                pass
            client.request('thread/start', {})
            assert client.sent[-1]['params'] == {}  # context cleared after failure
            assert codex_config.read_bytes() == codex_before

        with patch.object(gateway_run, '_hermes_home', Path(tmp)), \
             patch('hermes_cli.model_switch.switch_model', side_effect=switched), \
             patch('hermes_cli.model_switch.list_authenticated_providers', return_value=[]), \
             patch('agent.models_dev.fetch_models_dev', return_value={}), \
             patch('hermes_cli.model_selection_guards.combined_selection_warning', return_value=None), \
             patch('hermes_cli.context_switch_guard.enrich_model_switch_warnings_for_gateway'), \
             patch.object(sessions, 'CodexAppServerClient', RecordingClient):
            asyncio.run(channels())
        print('QQ/WhatsApp /help /model /reasoning /stop; Codex wire, cache/resume/once/global/isolation: PASS')


if __name__ == '__main__':
    main()
