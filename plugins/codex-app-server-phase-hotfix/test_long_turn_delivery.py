"""Real transport replay; no network, models or Gateway mutations."""
import importlib.util
import math
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('long_turn_check', Path(__file__).with_name('long_turn.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from agent.transports import codex_app_server_session as transport


def main():
    clock = [0.0]
    def event(at, kind, **extra):
        return at, {'method': 'item/completed', 'params': {'threadId': 'test-thread',
            'turnId': 'test-turn', 'item': {'id': str(at), 'type': kind, **extra}}}
    notes = [event(0, 'agentMessage', text='progress', phase='commentary'),
             event(1, 'commandExecution', command='true', aggregatedOutput='ok', exitCode=0, status='completed')]
    notes += [event(at, 'reasoning', summary=[]) for at in (26, 51, 81)]
    notes += [(92, None), event(120, 'agentMessage', text='done', phase='final_answer'),
              (121, {'method': 'turn/completed', 'params': {'threadId': 'test-thread',
                      'turn': {'id': 'test-turn', 'status': 'completed'}}})]
    class Client:
        def is_alive(self): return True
        def request(self, method, params, **kw): return {'turn': {'id': 'test-turn'}}
        def take_server_request(self, **kw): return None
        def take_notification(self, **kw):
            at, note = notes.pop(0)
            clock[0] = at
            return note
        def stderr_tail(self, *args): return []
    session = transport.CodexAppServerSession()
    session._client = Client()
    session._thread_id = 'test-thread'
    session.ensure_started = lambda: 'test-thread'
    wrapped = m.wrap_session_run_turn(transport.CodexAppServerSession.run_turn)
    with patch.object(transport.time, 'monotonic', side_effect=lambda: clock[0]):
        result = wrapped(session, 'synthetic task', turn_timeout=300)
    assert not result.interrupted and result.error is None and result.final_text == 'done', result.error

    # The shared runtime boundary must defeat Gateway's failed/already_sent checks.
    success = {'completed': True, 'final_response': 'done', 'messages': []}
    assert m.wrap_runtime_result(lambda *a, **kw: success)(object()) is success
    for failure in (
        {'error': 'transport stopped', 'completed': False},
        {'interrupted': True, 'completed': False, 'interrupt_message': 'queued user request'},
        {'partial': True, 'completed': False},
    ):
        raw = {**failure, 'final_response': 'progress', 'already_sent': True, 'messages': []}
        output = m.wrap_runtime_result(lambda *a, **kw: raw)(object())
        assert output['failed'] and output['partial'] and output['completed'] is False
        assert not output.get('already_sent') and output['final_response'] != 'progress'
        assert output['messages'] == []
        assert output['error'] and output['error'] in output['final_response']
        assert 'queued user request' not in output['final_response']

    deltas = []
    raw = {'error': 'worker exited', 'completed': False, 'final_response': 'progress'}
    output = m.wrap_runtime_result(lambda *a, **kw: raw)(SimpleNamespace(_fire_stream_delta=deltas.append))
    assert deltas == ['\n\n' + output['final_response']]

    # Existing callers may still explicitly choose a finite quiet timeout.
    forwarded = []
    def capture(self, user_input, **kw):
        forwarded.append(kw['post_tool_quiet_timeout'])
        self._on_event({'method': 'turn/completed'})
        return SimpleNamespace(interrupted=False, error=None)
    actor = SimpleNamespace(_on_event=None)
    m.wrap_session_run_turn(capture)(actor, 'test')
    m.wrap_session_run_turn(capture)(actor, 'test', post_tool_quiet_timeout=12)
    assert math.isinf(forwarded[0]) and forwarded[1] == 12
    print('post-tool >90s completion, failed/interrupted/partial delivery, successful result preservation PASS')


if __name__ == '__main__': main()
