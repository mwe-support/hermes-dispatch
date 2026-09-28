"""Real scheduler -> DeliveryRouter -> QQAdapter; replace only the QQ wire."""
import asyncio
import base64
import importlib.util
import json
import os
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cron import scheduler
from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.platforms.qqbot.adapter import QQAdapter


def load_plugin():
    path = Path(__file__).with_name('__init__.py')
    spec = importlib.util.spec_from_file_location('cron_qq_test_plugin', path,
                                                submodule_search_locations=[str(path.parent)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


from test_file_delivery import RecordingQQ as WireQQ


class RecordingQQ(WireQQ):
    def __init__(self, config):
        super().__init__()
        self._chat_type_map.clear()
        self.reject = False
        self.reject_media = False
        self.overlap = None
        self.arrivals = 0

    async def _api_request(self, method, path, body=None, **kwargs):
        if self.reject_media and body and body.get('msg_type') == 7:
            self.calls.append((path, body))
            raise RuntimeError('forbidden: media delivery denied')
        if self.reject:
            self.calls.append((path, body))
            raise RuntimeError('forbidden: target cannot receive proactive messages')
        if self.overlap is not None:
            self.arrivals += 1
            if self.arrivals == 2:
                self.overlap.set()
            await asyncio.wait_for(self.overlap.wait(), timeout=3)
        return await super()._api_request(method, path, body, **kwargs)


async def check_route(plugin, chat_type):
    canonical = 'c2c' if chat_type == 'dm' else chat_type
    opposite = 'c2c' if canonical == 'group' else 'group'
    endpoint = 'groups' if canonical == 'group' else 'users'
    target = 'target-chat'
    message_path = f'/v2/{endpoint}/{target}/messages'
    pin = {'chat_id': target, 'chat_type': chat_type}
    with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
        'HERMES_HOME': tmp, 'HERMES_SAFE_MODE': '1',
    }):
        home = Path(tmp)
        (home / 'cron').mkdir()
        routes = home / 'cron/delivery-targets.json'
        routes.write_text(json.dumps({'test-job': pin}))
        plugin.register(None)
        pconfig = PlatformConfig(enabled=True, typing_indicator=False,
            home_channel=HomeChannel(platform=Platform.QQBOT, chat_id='home-user', name='test-home'),
            extra={'app_id': 'test-app', 'client_secret': 'test-secret', 'markdown_support': False})
        config = GatewayConfig(platforms={Platform.QQBOT: pconfig})
        adapter = RecordingQQ(pconfig)
        loop = asyncio.get_running_loop()
        job = {'id': 'test-job', 'name': 'test', 'deliver': 'qqbot',
               'origin': {'platform': 'qqbot', 'chat_id': target, 'chat_type': chat_type}}
        with patch('gateway.config.load_gateway_config', return_value=config), \
             patch.object(scheduler, 'load_config', return_value={'cron': {'wrap_response': False}}), \
             patch.object(scheduler, '_get_home_target_chat_id', return_value='home-user'):
            async def send(selected=job, text='report', active=adapter):
                return await asyncio.to_thread(scheduler._deliver_result, selected, text,
                                               {Platform.QQBOT: active}, loop)

            original = scheduler._deliver_result
            plugin.register(None)
            assert scheduler._deliver_result is original, 'registration not idempotent'
            # Run the identical contract for groups, c2c and Hermes' dm alias.
            for setting in ('qqbot', 'origin', 'qqbot:' + target):
                for cached in (None, canonical, opposite):
                    fresh = RecordingQQ(pconfig)
                    if cached:
                        fresh._chat_type_map[target] = cached
                    error = await send({**job, 'deliver': setting}, active=fresh)
                    assert error is None, error
                    assert [path for path, _ in fresh.calls] == [message_path], fresh.calls
                    assert fresh._chat_type_map.get(target) == cached
            assert job['deliver'] == 'qqbot' and job['origin']['chat_id'] == target

            routes.unlink()
            # A QQ-created task must never inherit the configured home channel.
            origin_job = {**job, 'origin': {'platform': 'qqbot', 'chat_id': target,
                                           'chat_type': chat_type}}
            assert await send(origin_job) is None
            assert [path for path, _ in adapter.calls] == [message_path], adapter.calls
            adapter.calls.clear()

            # A model's extra send_message('qqbot') must not use home during
            # execution. Foreground QQ and non-QQ tools retain their behavior.
            from tools import send_message_tool
            token = plugin.cron_delivery._EXECUTION.set(True)
            try:
                rejected = await send_message_tool._send_to_platform(Platform.QQBOT, None, 'home-user', 'extra')
                assert not rejected['success'] and 'automatic' in rejected['error']
                skipped = send_message_tool._maybe_skip_cron_duplicate_send('qqbot', 'home-user', None)
                assert not skipped['success'] and 'automatic' in skipped['error']
                from agent.transports import codex_app_server
                env = codex_app_server.hermes_subprocess_env(inherit_credentials=True)
                assert env[plugin.cron_delivery.AUTO_ENV] == '1'
                probe = subprocess.run([sys.executable, '-c', '''import asyncio
from unittest.mock import patch
from gateway.config import Platform
from tools import send_message_tool
with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')):
    result = asyncio.run(send_message_tool._send_to_platform(Platform.QQBOT, None, 'synthetic', 'extra'))
assert not result['success'] and 'automatic' in result['error'], result
'''], env=env, capture_output=True, text=True, timeout=30)
                assert probe.returncode == 0, probe.stderr
            finally:
                plugin.cron_delivery._EXECUTION.reset(token)
            ordinary_calls = []
            async def ordinary_send(*args, **kwargs):
                ordinary_calls.append(args)
                return {'success': True}
            fake = SimpleNamespace(_send_to_platform=ordinary_send)
            plugin.cron_delivery.patch_cron_send_message(fake)
            token = plugin.cron_delivery._EXECUTION.set(True)
            try:
                assert not (await fake._send_to_platform(Platform.QQBOT, None, 'any', 'extra'))['success']
                assert (await fake._send_to_platform(Platform.TELEGRAM, None, 'any', 'normal'))['success']
            finally:
                plugin.cron_delivery._EXECUTION.reset(token)
            assert (await fake._send_to_platform(Platform.QQBOT, None, 'any', 'normal'))['success']
            assert len(ordinary_calls) == 2
            directory = home / 'channel_directory.json'
            directory.write_text(json.dumps({'platforms': {'qqbot': [
                {'id': target, 'name': 'native-target', 'type': chat_type}]}}))
            explicit = {**job, 'deliver': 'qqbot:' + target, 'origin': None}
            assert await send(explicit) is None
            assert [path for path, _ in adapter.calls] == [message_path], adapter.calls
            adapter.calls.clear()
            assert await send({**explicit, 'deliver': 'QQBOT:' + target}) is None
            assert [path for path, _ in adapter.calls] == [message_path]
            adapter.calls.clear()
            assert 'multi-target' in await send({**explicit, 'deliver': 'origin,qqbot:' + target})
            assert adapter.calls == []
            directory.unlink()
            assert 'chat type' in await send(explicit)
            assert adapter.calls == [], 'unknown type must not guess C2C'
            own_origin = {**explicit, 'origin': {'platform': 'qqbot', 'chat_id': target, 'chat_type': chat_type}}
            assert await send(own_origin) is None
            assert [path for path, _ in adapter.calls] == [message_path]
            adapter.calls.clear()
            routes.write_text(json.dumps({'test-job': pin}))

            # Contradicting explicit targets are errors, never silently rerouted.
            assert 'conflicts' in await send({**job, 'deliver': 'qqbot:another-group'})
            assert adapter.calls == []
            assert await send({**job, 'deliver': 'local'}) is None
            assert adapter.calls == []
            assert 'live QQ adapter' in scheduler._deliver_result(job, 'report')

            # Both native files and images: upload endpoints are NOT shared
            # between QQ group and private chat; assert every request and byte.
            output = home / 'report.txt'
            output.write_text('verified report')
            picture = home / 'pixel.png'
            picture.write_bytes(base64.b64decode(
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1sAAAAASUVORK5CYII='))
            for artifact in (output, picture):
                for prefix in ('report\n', ''):
                    assert await send(text=f'{prefix}MEDIA:"{artifact}"') is None
                    assert adapter.uploaded == [artifact.read_bytes()]
                    assert all(path.startswith(f'/v2/{endpoint}/{target}/') for path, _ in adapter.calls)
                    messages = [body for path, body in adapter.calls if path.endswith('/messages')]
                    assert [body['msg_type'] for body in messages] == ([0, 7] if prefix else [7])
                    adapter.calls.clear()
                    adapter.uploaded.clear()
            adapter.reject_media = True
            assert 'media delivery denied' in await send(text=f'report\nMEDIA:"{output}"')
            assert all(path.startswith(f'/v2/{endpoint}/{target}/') for path, _ in adapter.calls)
            adapter.reject_media = False
            adapter.calls.clear()
            adapter.uploaded.clear()

            # Rejected sends do not enter the upstream endpoint-guessing sender.
            adapter.reject = True
            assert 'forbidden' in await send()
            assert all(path == message_path for path, _ in adapter.calls)
            assert plugin.cron_delivery._TARGET.get() is None
            adapter.calls.clear()

            # Exercise the scheduler's real persisted delivery-error handling.
            from cron.jobs import create_job, get_job
            saved = create_job(prompt='test', schedule='0 * * * *', deliver='qqbot:' + target)
            routes.write_text(json.dumps({saved['id']: pin}))
            with patch.object(scheduler, 'run_job', return_value=(True, 'report', 'report', None)):
                assert await asyncio.to_thread(scheduler.run_one_job, saved,
                                               adapters={Platform.QQBOT: adapter}, loop=loop)
            stored = get_job(saved['id'])
            assert 'forbidden' in stored['last_delivery_error'], stored
            adapter.reject = False
            adapter.calls.clear()

            # A real no-agent script still gets the same delivery guard;
            # there is no Codex hook/event in this execution path.
            import builtins
            script = home / 'scripts' / 'cron-hook-probe.py'
            script.parent.mkdir(exist_ok=True)
            script.write_text('''import asyncio, sys
from unittest.mock import patch
from gateway.config import Platform
from tools import send_message_tool
with patch('socket.socket.connect', side_effect=AssertionError('network forbidden in probe')):
    result = asyncio.run(send_message_tool._send_to_platform(Platform.QQBOT, None, 'synthetic', 'extra'))
assert not result['success'] and 'automatic' in result['error'], result
assert 'run_agent' not in sys.modules
print('no-agent route probe; manual QQ send blocked')
''')
            scripted = create_job(prompt=None, schedule='0 * * * *', deliver='qqbot',
                                  script=script.name, no_agent=True)
            routes.write_text(json.dumps({scripted['id']: pin}))
            original_import = builtins.__import__
            def no_agent_import(name, *args, **kwargs):
                assert name != 'run_agent', 'no-agent delivery unexpectedly imported the model runtime'
                return original_import(name, *args, **kwargs)
            with patch('builtins.__import__', side_effect=no_agent_import):
                assert await asyncio.to_thread(scheduler.run_one_job, scripted,
                                               adapters={Platform.QQBOT: adapter}, loop=loop)
            finished = get_job(scripted['id'])
            assert finished['last_status'] == 'ok' and not finished['last_delivery_error'], finished
            assert [path for path, _ in adapter.calls] == [message_path]
            adapter.calls.clear()

            # Group and private jobs deliberately use the SAME opaque ID.
            # Force both requests in flight so a shared cache/type override
            # would misroute one of them; each ContextVar must retain its type.
            pins = {'test-job': pin, 'second-job': {'chat_id': target, 'chat_type': opposite}}
            routes.write_text(json.dumps(pins))
            adapter.overlap = asyncio.Event()
            second = {**job, 'id': 'second-job', 'origin': {
                'platform': 'qqbot', 'chat_id': target, 'chat_type': opposite}}
            assert await asyncio.gather(send(), send(second)) == [None, None]
            adapter.overlap = None
            assert sorted(path for path, _ in adapter.calls) == [
                f'/v2/groups/{target}/messages', f'/v2/users/{target}/messages']
            assert adapter._chat_type_map == {}
            adapter.calls.clear()
            await adapter.send('ordinary-user', 'normal')
            assert adapter.calls[0][0] == '/v2/users/ordinary-user/messages'
            adapter.calls.clear()

            # Even an intervening router that changes the target cannot send.
            from dataclasses import replace
            from gateway.delivery import DeliveryRouter
            route = DeliveryRouter._deliver_to_platform
            async def wrong_route(self, target, text, metadata):
                return await route(self, replace(target, chat_id='home-user'), text, metadata)
            with patch.object(DeliveryRouter, '_deliver_to_platform', wrong_route):
                assert 'destination mismatch' in await send()
            assert adapter.calls == []

            # Do not count Hermes' generated fallback UUID as a QQ receipt.
            with patch.object(adapter, '_api_request', new=AsyncMock(return_value={})):
                assert 'unconfirmed QQ API' in await send()
            for invalid_pin in ({'chat_id': '../escape', 'chat_type': 'group'},
                                {'chat_id': 'target-group', 'chat_type': 'unknown'},
                                {'chat_id': 'target-group'},
                                {'chat_id': 'target-group', 'chat_type': 'group', 'extra': True}):
                routes.write_text(json.dumps({'test-job': invalid_pin}))
                assert 'invalid pin' in await send()
            assert adapter.calls == []
            routes.write_text(json.dumps(pins))
            assert await send({**job, 'id': 'unconfigured'}) is None
            assert adapter.calls[0][0] == message_path
            adapter.calls.clear()
            assert 'refusing home' in await send({**job, 'id': 'unconfigured', 'origin': None})
            assert adapter.calls == []

            # A corrupt policy fails closed; another profile never uses it.
            routes.write_text('{invalid')
            assert 'invalid pin' in await send()
            assert await send({**job, 'deliver': 'local'}) is None
            assert adapter.calls == []
            from hermes_constants import set_hermes_home_override, reset_hermes_home_override
            other = home / 'other-profile'
            other.mkdir()
            token = set_hermes_home_override(other)
            try:
                resolved = plugin.cron_delivery._pinned_target(job, scheduler._normalize_deliver_value)
                assert resolved['chat_id'] == target and resolved['chat_type'] == canonical
                try:
                    plugin.cron_delivery._pinned_target(explicit, scheduler._normalize_deliver_value)
                except ValueError as exc:
                    assert 'chat type' in str(exc)
                else:
                    raise AssertionError('another profile must not inherit an explicit target type')
            finally:
                reset_hermes_home_override(token)
            routes.write_text(json.dumps(pins))

            # Upstream's dispatched-timeout branch assumes success; the guard
            # requires a confirmed receipt and prohibits a second transport.
            def unconfirmed(coro, loop):
                coro.close()
                return SimpleNamespace(result=lambda **kw: (_ for _ in ()).throw(TimeoutError()), cancel=lambda: False)
            with patch('agent.async_utils.safe_schedule_threadsafe', side_effect=unconfirmed):
                assert 'unconfirmed' in await send()
            assert adapter.calls == []
        print(f'cron {chat_type}: cold/correct/stale caches, route, media, failure persistence, group-private overlap, profiles, timeout: PASS')


async def main():
    plugin = load_plugin()
    for chat_type in sys.argv[1:] or ('group', 'c2c', 'dm'):
        await check_route(plugin, chat_type)


if __name__ == '__main__':
    asyncio.run(main())
