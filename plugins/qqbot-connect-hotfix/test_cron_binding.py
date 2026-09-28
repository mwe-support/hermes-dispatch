"""Persist real cron jobs from inbound QQ turns, including Codex child tools."""
import importlib.util
import asyncio
from contextvars import copy_context
import json
import os
from pathlib import Path
import subprocess
import shutil
import shlex
import sys
import tempfile
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch


def load_plugin():
    path = Path(__file__).with_name('__init__.py')
    spec = importlib.util.spec_from_file_location('qq_binding_test', path,
                                                 submodule_search_locations=[str(path.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main(home):
    from cron import jobs, scheduler
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools import cronjob_tools  # Alias exists before plugin discovery.
    from agent import codex_runtime
    from agent.transports.codex_app_server import CodexAppServerClient

    plugin = load_plugin()
    envs = []
    children = []
    launchers = home / 'launcher-bin'
    launchers.mkdir()
    launcher = launchers / 'hermes'
    original_launcher = ('#!/bin/sh\nunset PYTHONPATH\nunset PYTHONHOME\nexec '
                         + shlex.quote(sys.executable) + ' '
                         + shlex.quote(str(Path(jobs.__file__).resolve().parents[1] / 'hermes')) + ' "$@"\n')
    launcher.write_text(original_launcher)
    launcher.chmod(0o700)
    installer = Path(__file__).resolve().parents[2] / 'scripts/install-qq-cron-bootstrap.py'
    child_code = '''
import json
from cron import jobs
print(json.dumps(jobs.create_job(prompt='test', schedule='30m', deliver='qqbot')))
'''

    def child(env):
        result = subprocess.run([sys.executable, '-c', child_code], env=env,
            cwd=Path(__file__).parent, text=True, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def client_init(self, *args, env=None, **kwargs):
        envs.append({**os.environ, **env, 'PATH': str(launchers) + os.pathsep + os.environ['PATH']})

    def runtime(agent, **kwargs):
        if kwargs.get('no_tools'):
            return {'reply': 'ordinary chat remains available'}
        if not getattr(agent, 'child_env', None):
            client = object.__new__(CodexAppServerClient)
            CodexAppServerClient.__init__(client)
            agent.child_env = envs[-1]
        created = child(agent.child_env)
        result = subprocess.run([sys.executable, '-m', 'hermes_cli.main', 'cron', 'create',
                                 '30m', 'cli-test', '--deliver', 'qqbot', '--repeat', '1'],
            env=agent.child_env, text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        cli_job = [j for j in jobs.load_jobs() if j['prompt'] == 'cli-test'][-1]
        assert cli_job['deliver'] == created['deliver']
        assert binding.binding_target(cli_job) == binding.binding_target(created)
        if os.name != 'nt':  # generated POSIX shell wrapper; native CLI above runs everywhere
            launcher = shutil.which('hermes', path=agent.child_env['PATH'])
            assert Path(launcher).parent == launchers
            installed = subprocess.run([sys.executable, str(installer), '--home', str(home), '--launcher', launcher],
                capture_output=True, text=True, timeout=30)
            assert installed.returncode == 0, installed.stderr
            assert Path(launcher).stat().st_mode & 0o777 == 0o700
            wrapped = subprocess.run([launcher, 'cron', 'create', '30m', 'launcher-test',
                                      '--deliver', 'qqbot', '--repeat', '1'],
                env=agent.child_env, text=True, capture_output=True, timeout=30)
            assert wrapped.returncode == 0, wrapped.stdout + wrapped.stderr
            launcher_job = [j for j in jobs.load_jobs() if j['prompt'] == 'launcher-test'][-1]
            assert binding.binding_target(launcher_job) == binding.binding_target(created)
        custom = home / 'python-customization'
        custom.mkdir(exist_ok=True)
        (custom / 'sitecustomize.py').write_text("import os\nos.environ['QQ_BOOTSTRAP_CHAIN_TEST']='preserved'\n")
        probe = subprocess.run([sys.executable, '-c',
            "import sys,os; assert 'cron.jobs' not in sys.modules; "
            "assert os.environ['QQ_BOOTSTRAP_CHAIN_TEST']=='preserved'"],
            env={**agent.child_env, 'PYTHONPATH': agent.child_env['PYTHONPATH'] + os.pathsep + str(custom)},
            capture_output=True, text=True, timeout=30)
        assert probe.returncode == 0, probe.stderr
        other = home / 'other-profile'
        other.mkdir(exist_ok=True)
        try:
            child({**agent.child_env, 'HERMES_HOME': str(other)})
        except RuntimeError as exc:
            assert 'invalid profile context path' in str(exc)
        else:
            raise AssertionError('another profile consumed the QQ context')
        assert not (other / 'cron/jobs.json').exists()
        children.append(created)
        return created

    with patch.object(codex_runtime, 'run_codex_app_server_turn', runtime), \
         patch.object(CodexAppServerClient, '__init__', client_init):
        plugin.register(None)
        binding = plugin.cron_binding
        # Owner validation must never signal the Gateway, especially on Windows.
        pointer = home / 'cron' / 'qq-context' / 'owner-test.json'
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({'gateway_pid': os.getpid(), 'source': {'chat_id': 'test'}}))
        with patch.dict(os.environ, {binding.CONTEXT_ENV: str(pointer)}), patch(
            'gateway.status._pid_exists', return_value=True
        ), patch.object(binding.os, 'kill', side_effect=AssertionError('owner probe sent a signal')):
            assert binding._current_request()['gateway_pid'] == os.getpid()
        pointer.unlink()
        assert cronjob_tools.update_job is jobs.update_job
        assert getattr(jobs.create_job, '_qq_cron_binding_wrapped', False)
        (home / 'channel_directory.json').write_text(json.dumps({'platforms': {'qqbot': [
            {'id': 'other-group', 'type': 'group', 'name': '运营群'},
            {'id': 'other-user', 'type': 'c2c', 'name': '指定私聊'},
        ]}}))

        @contextmanager
        def incoming(chat='source-group', kind='group', text='每天生成报告', missing_message_id=False):
            source = SimpleNamespace(platform='qqbot', chat_id=chat, chat_type=kind,
                                     message_id='' if missing_message_id else 'msg-' + chat)
            token = binding._REQUEST.set(None)
            binding.capture_request(event=SimpleNamespace(source=source, text=text))
            tokens = set_session_vars(platform='qqbot', chat_id=chat, chat_type=kind,
                                      message_id=source.message_id, cron_session='')
            try:
                yield
            finally:
                clear_session_vars(tokens)
                binding._REQUEST.reset(token)

        def create(**kwargs):
            return jobs.create_job(prompt='test', schedule='30m', **kwargs)

        def must_reject(call):
            before = len(jobs.load_jobs())
            try:
                call()
            except (ValueError, RuntimeError):
                pass
            else:
                raise AssertionError('unauthorized target accepted')
            assert len(jobs.load_jobs()) == before

        for kind in ('group', 'c2c', 'dm'):
            with incoming(chat='source-' + kind, kind=kind):
                for deliver in (None, 'origin', 'qqbot', 'local'):
                    job = create(deliver=deliver, origin={'platform': 'qqbot',
                                 'chat_id': 'fake-origin', 'chat_type': 'group'})
                    persisted = jobs.get_job(job['id'])
                    expected = {'chat_id': 'source-' + kind,
                                'chat_type': 'c2c' if kind == 'dm' else kind}
                    assert persisted['deliver'] == 'qqbot:source-' + kind
                    assert binding.binding_target(persisted) == expected
                    assert persisted['origin']['chat_id'] == expected['chat_id']
                    assert plugin.cron_delivery._pinned_target(persisted,
                               scheduler._normalize_deliver_value)['chat_id'] == expected['chat_id']
                must_reject(lambda: create(deliver='qqbot:other-group'))
                must_reject(lambda: create(deliver='all'))

        for text in ('提到运营群而已', '不要发送到：运营群', '> 发送到：运营群',
                     '```\n发送到：运营群\n```', '```\n发送到：运营群'):
            with incoming(text=text):
                must_reject(lambda: create(deliver='qqbot:other-group'))
                assert create()['deliver'] == 'qqbot:source-group'

        for text, destination in (('每天生成报告，发送到：运营群', 'other-group'),
                                   ('投递到：qqbot:other-user', 'other-user')):
            with incoming(text=text):
                job = create(deliver='qqbot')
                assert job['deliver'] == 'qqbot:' + destination
                assert job['origin']['chat_id'] == 'source-group'
                must_reject(lambda: create(deliver='qqbot:unrequested'))

        with incoming(text='发送到：运营群', missing_message_id=True):
            assert create()['deliver'] == 'qqbot:other-group', 'QQ may omit the session message ID'

        inherited = []
        class Gateway:
            async def _handle_message(self, event):
                if not getattr(event, 'internal', False):
                    binding.capture_request(event=event)
                tokens = set_session_vars(platform='qqbot', chat_id='source-group',
                                          chat_type='group', message_id='', cron_session='')
                try:
                    inherited.append(copy_context())
                    return await asyncio.to_thread(create)
                finally:
                    clear_session_vars(tokens)
        binding.patch_gateway_scope(Gateway)
        event = SimpleNamespace(source=SimpleNamespace(platform='qqbot', chat_id='source-group',
                                chat_type='group', message_id=''), text='发送到：运营群')
        assert asyncio.run(Gateway()._handle_message(event))['deliver'] == 'qqbot:other-group'
        must_reject(lambda: inherited[0].run(create))
        event.internal = True
        must_reject(lambda: asyncio.run(Gateway()._handle_message(event)))

        with incoming(text='发送到：不存在的群'):
            must_reject(create)
            ordinary = SimpleNamespace()
            assert codex_runtime.run_codex_app_server_turn(ordinary, no_tools=True)['reply']
            assert not ordinary._qq_cron_context_path.exists()
        with incoming(text='每天生成报告并发送到运营群'):
            must_reject(create)  # Must ask for a clear target, not silently use origin.
        with incoming(text='发送到：运营群\n发给：指定私聊'):
            must_reject(create)

        with incoming():
            job = create()
        with incoming(chat='different-editor', text='发送到：运营群'):
            updated = jobs.update_job(job['id'], {'name': 'new', 'origin': None})
            assert binding.binding_target(updated)['chat_id'] == 'source-group'
            updated = jobs.update_job(job['id'], {'deliver': 'qqbot:other-group'})
            assert binding.binding_target(updated)['chat_id'] == 'other-group'
            assert updated['origin']['chat_id'] == 'source-group'
        must_reject(lambda: jobs.update_job(job['id'], {'deliver': 'qqbot:other-user'}))
        assert jobs.update_job(job['id'], {'deliver': 'qqbot'})['deliver'] == 'qqbot:other-group'
        tampered = {**updated, 'deliver': 'qqbot:home-user'}
        must_reject(lambda: plugin.cron_delivery._pinned_target(tampered,
                                        scheduler._normalize_deliver_value))

        for old_route in ('local', 'qqbot:other-group'):
            legacy = jobs.create_job.__wrapped__(prompt='legacy', schedule='30m', deliver=old_route,
                origin={'platform': 'qqbot', 'chat_id': 'source-group', 'chat_type': 'group'})
            changed = jobs.update_job(legacy['id'], {'name': 'renamed', 'enabled': False, 'origin': None})
            assert changed['deliver'] == old_route and changed['origin'] == legacy['origin']
            assert binding.binding_target(changed) is None
            must_reject(lambda: jobs.update_job(legacy['id'], {'deliver': 'qqbot'}))
            with incoming(text='发送到：运营群'):
                changed = jobs.update_job(legacy['id'], {'deliver': 'qqbot'})
                assert binding.binding_target(changed)['chat_id'] == 'other-group'
        local_legacy = jobs.create_job.__wrapped__(prompt='legacy local', schedule='30m', deliver='local',
            origin={'platform': 'qqbot', 'chat_id': 'unknown-old-origin'})
        assert jobs.update_job(local_legacy['id'], {'enabled': False})['deliver'] == 'local'

        def concurrent(n):
            with incoming(chat=f'concurrent-{n}', kind='group' if n % 2 else 'dm'):
                return create()
        with ThreadPoolExecutor(max_workers=4) as pool:
            parallel = list(pool.map(concurrent, range(8)))
        assert [j['deliver'] for j in parallel] == [f'qqbot:concurrent-{n}' for n in range(8)]

        agent = SimpleNamespace()
        with incoming(text='发送到：运营群'):
            assert codex_runtime.run_codex_app_server_turn(agent)['deliver'] == 'qqbot:other-group'
        assert not agent._qq_cron_context_path.exists()
        must_reject(lambda: child(agent.child_env))
        with incoming():
            assert codex_runtime.run_codex_app_server_turn(agent)['deliver'] == 'qqbot:source-group'
        assert len(envs) == 1, 'test must reuse the persistent Codex child environment'
        assert not agent._qq_cron_context_path.exists()
        assert not list((home / 'cron' / 'qq-context').glob('*.json'))
        if os.name != 'nt':
            removed = subprocess.run([sys.executable, str(installer), '--home', str(home),
                                      '--launcher', str(launcher), '--remove'],
                capture_output=True, text=True, timeout=30)
            assert removed.returncode == 0, removed.stderr
            assert launcher.read_text() == original_launcher

        # Native cron tool (not only low-level storage) sees the same binding.
        with incoming(chat='tool-private', kind='dm'):
            result = json.loads(cronjob_tools.cronjob(action='create', prompt='test',
                                                     schedule='30m', deliver='qqbot'))
            assert result['success'], result
        assert any(j['deliver'] == 'qqbot:tool-private' for j in jobs.load_jobs())
    print('QQ create/update/native tool/concurrency/Codex child/explicit user override: PASS')


if __name__ == '__main__':
    with tempfile.TemporaryDirectory() as temporary:
        with patch.dict(os.environ, {'HERMES_HOME': temporary, 'HERMES_SAFE_MODE': '1',
                                     'HERMES_QQ_CRON_CONTEXT': ''}):
            main(Path(temporary))
