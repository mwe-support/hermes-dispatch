"""Codex final answer -> real Gateway/QQ upload, with HTTP replaced only."""
import asyncio
import base64
import importlib.util
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.platforms.qqbot.adapter import QQAdapter
from gateway.run import GatewayRunner
from gateway.session import SessionSource

spec = importlib.util.spec_from_file_location('file_delivery_test', Path(__file__).with_name('outbound.py'))
delivery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(delivery)


class RecordingQQ(QQAdapter):
    def __init__(self, chat_type='c2c'):
        super().__init__(PlatformConfig(typing_indicator=False, extra={
            'app_id': 'test-app', 'client_secret': 'test-secret',
            'markdown_support': False}))
        self._running = True
        self._ws = SimpleNamespace(closed=False)
        self._chat_type_map = {'test-chat': chat_type}
        self._http_client = SimpleNamespace(put=self.put)
        self.calls = []
        self.uploaded = []

    async def put(self, url, *, data, headers):
        assert url == 'https://upload.invalid/part'
        self.uploaded.append(data)
        return SimpleNamespace(status_code=200)

    async def _api_request(self, method, path, body=None, **kwargs):
        self.calls.append((path, body))
        if path.endswith('/upload_prepare'):
            return {'upload_id': 'test-upload', 'block_size': body['file_size'],
                    'parts': [{'part_index': 1, 'presigned_url': 'https://upload.invalid/part'}]}
        if path.endswith('/upload_part_finish'):
            return {}
        if path.endswith('/files'):
            if body.get('file_data'):
                self.uploaded.append(base64.b64decode(body['file_data']))
            return {'file_info': 'test-file-info'}
        assert path.endswith('/messages')
        if body['msg_type'] == 7:
            assert body['media'] == {'file_info': 'test-file-info'}
        else:
            assert body['msg_type'] == 0 and 'Sorry' not in body['content']
        return {'id': 'attachment-message'}


async def deliver(text, adapter):
    event = SimpleNamespace(source=SimpleNamespace(chat_id='test-chat', platform='qqbot'))
    await GatewayRunner._deliver_media_from_response(
        object.__new__(GatewayRunner), text, event, adapter, thread_metadata={})


async def main():
    delivery.patch_output_file_delivery(RecordingQQ)
    patched = RecordingQQ.extract_media
    delivery.patch_output_file_delivery(RecordingQQ)
    assert RecordingQQ.extract_media is patched
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp, '采购清单.txt')
        output.write_text('名称,数量\n测试物料,3\n')
        from gateway.platforms.base import BasePlatformAdapter
        def old_validate(path):
            return path
        def new_validate(path, session_key=""):
            assert session_key == 'test-session'
            return path
        for validator in (old_validate, new_validate):
            with patch.object(BasePlatformAdapter, 'validate_media_delivery_path', staticmethod(validator)):
                assert delivery._validate_output_path(str(output), 'test-session') == str(output)
        def broken_validate(path, session_key=""):
            raise TypeError('internal validator failure')
        with patch.object(BasePlatformAdapter, 'validate_media_delivery_path', staticmethod(broken_validate)):
            try:
                delivery._validate_output_path(str(output))
                assert False, 'internal validation errors must not bypass safety checks'
            except TypeError as exc:
                assert str(exc) == 'internal validator failure'
        # Existing explicit attachments must work on official 0.20.5 too.
        explicit = RecordingQQ()
        await deliver(f'MEDIA:"{output}"', explicit)
        assert explicit.uploaded == [output.read_bytes()], 'existing MEDIA attachment regressed'
        inline_media = RecordingQQ()
        await deliver(f'`MEDIA:{output}`', inline_media)
        assert inline_media.uploaded == [output.read_bytes()]
        for directive in (f'MEDIA:`{output}`', f'` MEDIA:{output} `', f'`media:{output}`'):
            existing = RecordingQQ()
            await deliver(directive, existing)
            assert existing.uploaded == [output.read_bytes()], directive
        final = f'已做好：[采购清单]({output})'
        agent = SimpleNamespace(_gateway_session_key='agent:main:qqbot:dm:test-chat')
        result = {'final_response': final}
        adapter = RecordingQQ()
        await deliver(result['final_response'], adapter)
        assert adapter.uploaded == [output.read_bytes()], 'local output never uploaded to QQ'
        assert sum(p.endswith('/messages') for p, _ in adapter.calls) == 1
        # The same finalized answer reaches both group and C2C native upload.
        group = RecordingQQ('group')
        await deliver(result['final_response'], group)
        assert group.uploaded == [output.read_bytes()]
        assert all('/v2/groups/' in path for path, _ in group.calls)

        # Non-streaming and streaming fallback use the adapter's full response
        # pipeline, which scans both MEDIA tags and remaining local links.
        for chat_type in ('c2c', 'group'):
            ordinary = RecordingQQ(chat_type)
            async def respond(event):
                return result['final_response']
            ordinary.set_message_handler(respond)
            event = MessageEvent(text='请生成清单', source=SessionSource(
                platform=Platform.QQBOT, chat_id='test-chat',
                chat_type='dm' if chat_type == 'c2c' else 'group',
                user_id='test-user'), message_id='incoming-test')
            await ordinary._process_message_background(event, agent._gateway_session_key)
            assert ordinary.uploaded == [output.read_bytes()], 'non-streaming upload duplicated or missing'
            sent = [body for path, body in ordinary.calls if path.endswith('/messages')]
            assert sum(body['msg_type'] == 7 for body in sent) == 1
            assert [body['content'] for body in sent if body['msg_type'] == 0] == ['已做好：采购清单.txt']

        # Full-path labels must not survive as a second bare-path attachment.
        result['final_response'] = f'[{output}]({output})'
        ordinary = RecordingQQ()
        ordinary.set_message_handler(respond)
        await ordinary._process_message_background(event, agent._gateway_session_key)
        assert ordinary.uploaded == [output.read_bytes()]
        nested = f'[:codex-file-citation{{path="{output}" purpose="output"}}]({output}) tail-marker'
        media, normalized = delivery._qq_output_files(nested, agent._gateway_session_key)
        assert 'tail-marker' in normalized and ':codex-file-citation' not in normalized
        assert media == [(str(output.resolve()), False)]
        null_tag = 'done MEDIA:/tmp/\x00.txt'
        assert delivery._qq_output_files(null_tag, agent._gateway_session_key) == ([], null_tag)

        key = agent._gateway_session_key
        spaced = Path(tmp, '测试 report.csv')
        spaced.write_text('a,b\n1,2\n')
        for link in (f'[file](<{spaced}>)', f'[file]({spaced.as_uri()})',
                     f'Created :codex-file-citation{{path="{spaced}" purpose="output"}}'):
            sent = RecordingQQ()
            await deliver(link, sent)
            assert sent.uploaded == [spaced.read_bytes()]
        media, dedup = delivery._qq_output_files(final + '\n' + final +
            f' :codex-file-citation{{path="{output}" purpose="output"}}', key)
        assert media == [(str(output.resolve()), False)]
        assert delivery._qq_output_files(dedup, key) == ([], dedup)
        for text in (f'`{final}`', f'```md\n{final}\n```', f'> {final}',
                     f'[source]({output}:12)', f'[source]({output}#L1)',
                     f'[missing]({tmp}/missing.pdf)', '[secret](/etc/passwd)',
                     '[remote](https://example.invalid/report.pdf)',
                     '[bad](http://[)', str(output),
                     f':codex-file-citation{{path="{output}" purpose="source"}}',
                     ':codex-file-citation{path="unclosed purpose="output"}',
                     f'`:codex-file-citation{{path="{output}" purpose="output"}}`'):
            assert delivery._qq_output_files(text, key) == ([], text), text
        secret_link = Path(tmp, 'secret.txt')
        secret_link.symlink_to('/etc/passwd')
        text = f'[secret]({secret_link})'
        assert delivery._qq_output_files(text, key) == ([], text)
        # The runtime/streamed text is untouched; only QQ's final extraction
        # accepts output references. Base extraction used by history is intact.
        from gateway.platforms.base import BasePlatformAdapter
        assert BasePlatformAdapter.extract_media(final)[0] == []
        assert final == f'已做好：[采购清单]({output})'

        # Use ASCII paths too: upstream ordinary delivery scans bare paths.
        sample = Path(tmp, 'example.txt')
        sample.write_text('example only')
        audio = Path(tmp, 'example.wav')
        audio.write_bytes(b'RIFF-test-audio')
        audio_ref = f'[audio]({audio})'
        assert RecordingQQ.extract_media('[[audio_as_voice]]\n' + audio_ref)[0] == [(str(audio.resolve()), True)]
        assert RecordingQQ.extract_media('~~~\n[[audio_as_voice]]\n~~~\n\n' + audio_ref)[0] == [(str(audio.resolve()), False)]
        # A generated attachment must not be reparsed into a trailing quote
        # (lazy continuation) or an unclosed fence. The ordering matters.
        for reference in (f'[download]({output})',
                          f':codex-file-citation{{path="{output}" purpose="output"}}'):
            for tail in (f'> [example]({sample})', f'  > [example]({sample})',
                         f'~~~md\n[example]({sample})', f'````md\n[example]({sample})',
                         f'~~~\n[example]({sample})\n~~~'):
                response = reference + '\n\n' + tail
                for chat_type in ('c2c', 'group'):
                    streamed = RecordingQQ(chat_type)
                    await deliver(response, streamed)
                    assert streamed.uploaded == [output.read_bytes()], (chat_type, response)
                    ordinary_tail = RecordingQQ(chat_type)
                    async def tail_response(_event):
                        return response
                    ordinary_tail.set_message_handler(tail_response)
                    await ordinary_tail._process_message_background(event, agent._gateway_session_key)
                    assert ordinary_tail.uploaded == [output.read_bytes()], (chat_type, response)
                    assert sum(b['msg_type'] == 7 for p, b in ordinary_tail.calls if p.endswith('/messages')) == 1
                    media, cleaned = ordinary_tail.extract_media(response)
                    assert len(media) == 1 and tail in cleaned
        for reference in (f'[sample]({sample})',
                          f':codex-file-citation{{path="{sample}" purpose="output"}}'):
            examples = [
                f'~~~md\n{reference}\n~~~', f'    {reference}', f'\t{reference}',
                f'  > {reference}', f'> example\n{reference}',
                f'````md\n```\n{reference}\n```\n````',
                f'~~~md\n{reference}', f'- example\n\n      {reference}',
                f'`` text ` {reference} ` text ``',
                f'`line one\n{reference}\nline three`',
                f'\r\n    {reference}\r\n',
                f'\\` text `{reference}`',
            ]
            for example in examples:
                assert delivery._qq_output_files(example) == ([], example), example
                for chat_type in ('c2c', 'group'):
                    protected = RecordingQQ(chat_type)
                    assert protected.extract_local_files(example) == ([], example), (example, protected.extract_local_files(example))
                    await deliver(example, protected)
                    assert protected.uploaded == [], example
                    async def example_response(_event):
                        return example
                    protected.set_message_handler(example_response)
                    await protected._process_message_background(event, agent._gateway_session_key)
                    assert protected.uploaded == [], example
                    bodies = [b['content'] for p, b in protected.calls if p.endswith('/messages')]
                    assert len(bodies) == 1  # QQ applies its normal text formatting.

        # A basename is data, not Markdown syntax. It must neither open a
        # fence that hides native MEDIA nor close one that exposes examples.
        for name in ('~~~report.txt', '```report.txt', '> report.txt', 'name ` code.txt'):
            odd = Path(tmp, name)
            odd.write_bytes(b'odd-name output')
            reference = f'[real]({odd.as_uri()})'
            for suffix, expected in (
                (f'\nMEDIA:"{output}"', [output.read_bytes(), odd.read_bytes()]),
                (f'\n\n~~~\nMEDIA:"{sample}"\n~~~', [odd.read_bytes()]),
                (f'\n\n~~~\n[example]({sample})\n~~~', [odd.read_bytes()]),
            ):
                response = reference + suffix
                for chat_type in ('c2c', 'group'):
                    streamed = RecordingQQ(chat_type)
                    await deliver(response, streamed)
                    assert streamed.uploaded == expected, (name, response)
                    ordinary_name = RecordingQQ(chat_type)
                    async def name_response(_event):
                        return response
                    ordinary_name.set_message_handler(name_response)
                    await ordinary_name._process_message_background(event, agent._gateway_session_key)
                    assert ordinary_name.uploaded == expected, (name, response)

        # An example and a real output in one answer must not hide the output
        # or erase the example while ordinary delivery scans remaining paths.
        mixed = f'~~~\n[sample]({sample})\n~~~\n\n{final}'
        for chat_type in ('c2c', 'group'):
            sent = RecordingQQ(chat_type)
            async def mixed_response(_event):
                return mixed
            sent.set_message_handler(mixed_response)
            await sent._process_message_background(event, agent._gateway_session_key)
            assert sent.uploaded == [output.read_bytes()]
            bodies = [b['content'] for p, b in sent.calls if p.endswith('/messages') and b['msg_type'] == 0]
            assert 'sample' in bodies[0]

        same_path = f'~~~\nMEDIA:{sample}\n~~~\n\n[download]({sample})'
        same_path_sent = RecordingQQ()
        await deliver(same_path, same_path_sent)
        assert same_path_sent.uploaded == [sample.read_bytes()]
        image_then_code = f'![picture](https://example.invalid/a.png)\n\n    [sample]({sample})'
        image_case = RecordingQQ()
        async def image_response(_event):
            return image_then_code
        image_case.set_message_handler(image_response)
        await image_case._process_message_background(event, agent._gateway_session_key)
        assert image_case.uploaded == [], 'removing an image exposed the indented example'

        from markdown_it import MarkdownIt
        code = f'    ```\n    {sample}\n    ```\n'
        before = MarkdownIt().parse(code)[0]
        after = MarkdownIt().parse(delivery._fence_indented_code(code))[0]
        assert before.type == 'code_block' and after.type == 'fence'
        assert before.content == after.content, 'code content changed during fencing'
        # Ordinary bare paths outside examples keep their existing behavior.
        bare = RecordingQQ()
        async def bare_response(_event):
            return str(sample)
        bare.set_message_handler(bare_response)
        await bare._process_message_background(event, agent._gateway_session_key)
        assert bare.uploaded == [sample.read_bytes()]

        class FailingQQ(RecordingQQ):
            async def _api_request(self, method, path, body=None, **kwargs):
                if path.endswith('/upload_prepare'):
                    raise RuntimeError('simulated upload failure')
                return await super()._api_request(method, path, body, **kwargs)

        # Reproduce the upstream post-stream gap: a failed SendResult was
        # ignored, so the user got no failure notice after the final text.
        failed_before = FailingQQ()
        directive = f'MEDIA:"{output}"'
        await deliver(directive, failed_before)
        assert not any(p.endswith('/messages') for p, _ in failed_before.calls)
        delivery.patch_post_stream_media_failures(RecordingQQ)
        patched_dispatch = GatewayRunner._deliver_media_from_response
        delivery.patch_post_stream_media_failures(RecordingQQ)
        assert GatewayRunner._deliver_media_from_response is patched_dispatch
        failed_after = FailingQQ()
        await deliver(directive, failed_after)
        notices = [b['content'] for p, b in failed_after.calls if p.endswith('/messages')]
        assert len(notices) == 1 and "Couldn't deliver" in notices[0]

        # Ordinary dispatch already notifies; the shared adapter patch must
        # not emit a second notice there or leak state from another stream.
        failed_ordinary = FailingQQ()
        async def failed_response(_event):
            return directive
        failed_ordinary.set_message_handler(failed_response)
        await failed_ordinary._process_message_background(event, agent._gateway_session_key)
        notices = [b['content'] for p, b in failed_ordinary.calls if p.endswith('/messages')]
        assert len(notices) == 1 and "Couldn't deliver" in notices[0]
        success_after = RecordingQQ()
        await deliver(directive, success_after)
        assert success_after.uploaded == [output.read_bytes()]
        assert sum(p.endswith('/messages') for p, _ in success_after.calls) == 1
        print('codex_final_to_qq_attachment=PASS')
        print('c2c_group_upload_bytes_and_file_info=PASS')
        print('nonstreaming_c2c_group_attachment_exactly_once=PASS')
        print('spaces_unicode_dedup_safe_paths_and_turn_scope=PASS')
        print('post_stream_failure_visible_once_and_ordinary_notice_not_duplicated=PASS')


if __name__ == '__main__':
    asyncio.run(main())
