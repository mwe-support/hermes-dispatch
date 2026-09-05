import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio

from gateway.platforms.base import BasePlatformAdapter
from gateway.config import Platform, StreamingConfig
import gateway.run as gateway_run
from gateway.run import GatewayRunner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def load_plugin_module():
    path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location(
        "qqbot_connect_streaming_test",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = spec.name
    mod.__path__ = [str(path.parent)]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_plugin_module()
streaming_mod = sys.modules[mod.__name__ + ".streaming"]


def hermes_version_tuple():
    return streaming_mod._hermes_version_tuple()


class DummyAdapter:
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self):
        self._app_id = "test-app"
        self._markdown_support = True
        self._last_msg_id = {}
        self.api_calls = []
        self.successful_api_calls = []
        self.normal_sends = []
        self.normal_send_attempts = []
        self.normal_send_entered = None
        self.normal_send_concurrent_entered = None
        self.normal_send_second_attempt_entered = None
        self.normal_send_release = None
        self.fail_normal_attempts = 0
        self.raise_normal_attempts = 0
        self.normal_send_inflight = 0
        self.normal_send_peak = 0
        self.typing_calls = []
        self.stream_counter = 0
        self.fail_next_stream = False
        self.fail_stream_attempts = 0
        self.fail_seal_attempts = 0
        self.fail_tail_open_attempts = 0
        self.accept_then_expire_stream = False
        self.accepted_terminal_calls = []
        self.accept_then_timeout_stream = False
        self.accepted_timeout_calls = []
        self.timeout_reconciliation_complete = False
        self.timeout_first_stream_attempts = 0
        self.fail_reply_budget_on_new_stream = False
        self.native_stream_now = 0.0
        self._qq_native_stream_clock = lambda: self.native_stream_now

    def _guess_chat_type(self, chat_id):
        chat_id = str(chat_id)
        if chat_id.startswith("group-"):
            return "group"
        if chat_id.startswith("guild-dm-"):
            return "dm"
        return "c2c"

    def _next_msg_seq(self, key):
        return 73

    async def _api_request(self, method, path, body):
        call = (method, path, dict(body))
        self.api_calls.append(call)
        if self.accept_then_expire_stream and body["index"] > 0:
            if not self.accepted_terminal_calls:
                self.accepted_terminal_calls.append(call)
                raise RuntimeError("同一流式消息发送超过时间限制")
            raise RuntimeError("请求参数index需要递增")
        if self.accept_then_timeout_stream and body["index"] > 0:
            if not self.accepted_timeout_calls:
                self.accepted_timeout_calls.append(call)
                raise TimeoutError("response lost after QQ accepted the frame")
            if not self.timeout_reconciliation_complete:
                self.timeout_reconciliation_complete = True
                raise RuntimeError("请求参数index需要递增")
        if body["index"] == 0 and self.timeout_first_stream_attempts:
            self.timeout_first_stream_attempts -= 1
            raise TimeoutError("first stream response lost")
        if (
            self.fail_reply_budget_on_new_stream
            and body["index"] == 0
            and self.stream_counter > 0
        ):
            raise RuntimeError("回复消息失败，被动回复时间或者次数超过限制")
        if self.fail_next_stream:
            self.fail_next_stream = False
            raise RuntimeError("stream unavailable")
        if self.fail_stream_attempts and body["input_state"] == 1:
            self.fail_stream_attempts -= 1
            raise RuntimeError("stream rate limited")
        if (
            body["index"] == 0
            and len(body["content_raw"]) == 100
            and self.fail_tail_open_attempts
        ):
            self.fail_tail_open_attempts -= 1
            raise RuntimeError("tail open unavailable")
        if body["input_state"] == 10 and self.fail_seal_attempts:
            self.fail_seal_attempts -= 1
            raise RuntimeError("seal unavailable")
        self.stream_counter += 1
        if body["index"] == 0:
            data = {"id": f"stream-{self.stream_counter}"}
        else:
            data = {"id": body["stream_msg_id"]}
        self.successful_api_calls.append(call)
        return data

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        attempt = (chat_id, content, reply_to, metadata)
        self.normal_send_attempts.append(attempt)
        self.normal_send_inflight += 1
        self.normal_send_peak = max(
            self.normal_send_peak,
            self.normal_send_inflight,
        )
        try:
            if self.normal_send_entered is not None:
                self.normal_send_entered.set()
            if (
                len(self.normal_send_attempts) >= 2
                and self.normal_send_second_attempt_entered is not None
            ):
                self.normal_send_second_attempt_entered.set()
            if (
                self.normal_send_inflight >= 2
                and self.normal_send_concurrent_entered is not None
            ):
                self.normal_send_concurrent_entered.set()
            if self.normal_send_release is not None:
                await self.normal_send_release.wait()
            if self.raise_normal_attempts:
                self.raise_normal_attempts -= 1
                raise RuntimeError("normal send raised")
            if self.fail_normal_attempts:
                self.fail_normal_attempts -= 1
                return SimpleNamespace(
                    success=False,
                    message_id=None,
                    error="normal send unavailable",
                )
            self.normal_sends.append(attempt)
            return SimpleNamespace(
                success=True,
                message_id="normal-message",
                error=None,
            )
        finally:
            self.normal_send_inflight -= 1

    async def send_typing(self, chat_id, metadata=None):
        self.typing_calls.append((chat_id, metadata))


class GatewayDummyAdapter(DummyAdapter, BasePlatformAdapter):
    """Real consumer-compatible adapter while retaining the wire fake."""


GatewayDummyAdapter.__abstractmethods__ = frozenset()
GatewayDummyAdapter.SUPPORTS_MESSAGE_EDITING = False


def assert_exact_final_ownership(adapter, target):
    """Every final character has exactly one successful visible owner."""

    sealed = [
        call[2]["content_raw"]
        for call in adapter.successful_api_calls
        if call[2]["input_state"] == 10
    ]
    streams, _anchors = streaming_mod._stream_maps(adapter)
    visible_open = [
        state.last_content
        for state in streams.values()
        if state.stream_msg_id and not state.sealed
    ]
    ordinary = [item[1] for item in adapter.normal_sends]
    actual = "".join(sealed + visible_open + ordinary)
    assert actual == target, (actual, target)


async def wait_for_final_claim_users(
    adapter,
    chat_id,
    reply_to,
    expected_users,
):
    """Wait until every intended concurrent caller has registered its claim."""

    key = (str(chat_id), str(reply_to))
    with anyio.fail_after(1):
        while True:
            broker = getattr(
                adapter,
                "_qq_native_c2c_final_delivery_broker",
                None,
            )
            if broker is not None and broker.registered_for(key) == expected_users:
                return
            await anyio.sleep(0)


async def main():
    status = mod._patch_qq_c2c_streaming(DummyAdapter)
    if hermes_version_tuple() < (0, 20, 5):
        assert status.startswith(
            "QQ C2C native streaming disabled: requires Hermes >=0.20.5"
        )
        assert not hasattr(DummyAdapter, "send_draft")
        assert not getattr(
            GatewayRunner._build_stream_consumer_config,
            streaming_mod._RUNNER_PATCHED,
            False,
        )
        legacy = DummyAdapter()
        legacy._last_msg_id["user-disabled"] = "disabled-1"
        await legacy.send_typing("user-disabled")
        await legacy.send_typing("user-disabled")
        assert len(legacy.typing_calls) == 2
        print("qq_c2c_hermes_0_20_0_fail_closed=ok")
        print("qq_c2c_disabled_typing_unchanged=ok")
        return

    assert status == "QQ C2C native streaming patched"
    assert mod._patch_qq_c2c_streaming(DummyAdapter).endswith("already patched")

    # Only an exact stable release version may enable the patch. Pre-release
    # suffixes fail closed even when their numeric core is 0.20.5.
    import hermes_cli

    original_version = hermes_cli.__version__
    try:
        for candidate in ("0.20.5rc1", "0.20.5.dev0", "0.20.5+local"):
            hermes_cli.__version__ = candidate
            assert streaming_mod._hermes_version_tuple() == ()
            assert not streaming_mod._hermes_streaming_supported()
        hermes_cli.__version__ = "0.20.5"
        assert streaming_mod._hermes_version_tuple() == (0, 20, 5)
        assert streaming_mod._hermes_streaming_supported()
    finally:
        hermes_cli.__version__ = original_version

    adapter = DummyAdapter()
    assert not adapter.supports_draft_streaming(chat_type="dm", chat_id="user-1")
    assert not adapter.supports_draft_streaming(
        chat_type="group", chat_id="group-1"
    )
    assert adapter.stream_is_message_for_chat("user-1")
    assert not adapter.stream_is_message_for_chat("group-1")
    assert not adapter.stream_is_message_for_chat("guild-dm-1")
    rejected_group = await adapter.send_draft(
        "group-1", 999, "不应发送", {"reply_to_message_id": "group-msg"}
    )
    assert not rejected_group.success
    rejected_guild_dm = await adapter.send_draft(
        "guild-dm-1",
        998,
        "不应发送",
        {"reply_to_message_id": "guild-dm-msg"},
    )
    assert not rejected_guild_dm.success
    assert not adapter.api_calls

    metadata = {"reply_to_message_id": "inbound-1"}
    first = await adapter.send_draft("user-1", 1001, "正在读取", metadata)
    second = await adapter.send_draft("user-1", 1001, "正在读取知识库", metadata)
    assert adapter.supports_draft_streaming(chat_type="dm", chat_id="user-1")
    assert first.success and second.success
    assert len(adapter.api_calls) == 2
    first_body = adapter.api_calls[0][2]
    second_body = adapter.api_calls[1][2]
    assert first_body == {
        "input_mode": "replace",
        "input_state": 1,
        "index": 0,
        "content_type": "markdown",
        "content_raw": "正在读取",
        "msg_id": "inbound-1",
        "msg_seq": 73,
    }
    assert second_body["index"] == 1
    assert second_body["stream_msg_id"].startswith("stream-")
    assert second_body["content_raw"] == "正在读取知识库"

    final = await adapter.send(
        "user-1",
        "最终答案",
        reply_to="inbound-1",
        metadata={"notify": True, "reply_to_message_id": "inbound-1"},
    )
    assert final.success
    assert final.message_id.startswith("stream-")
    assert adapter.api_calls[-1][2]["input_state"] == 10
    assert (
        adapter.api_calls[-1][2]["content_raw"]
        == "正在读取知识库\n最终答案"
    )
    assert not adapter.normal_sends

    # QQ expires one C2C native carrier after roughly ten minutes even when
    # its content stays below MAX_MESSAGE_LENGTH.  Rollover must therefore be
    # driven by stream age as well as size: keep the first carrier before the
    # safety boundary, then seal it and open index 0 on a new carrier once the
    # fake monotonic clock crosses the boundary.
    age_rollover = DummyAdapter()
    age_rollover._qq_native_stream_max_age_seconds = 480.0
    age_metadata = {"reply_to_message_id": "inbound-age-rollover"}
    await age_rollover.send_draft(
        "user-age-rollover",
        1002,
        "phase one",
        age_metadata,
    )
    age_rollover.native_stream_now = 479.0
    await age_rollover.send_draft(
        "user-age-rollover",
        1002,
        "phase one plus",
        age_metadata,
    )
    assert [call[2]["input_state"] for call in age_rollover.api_calls] == [1, 1]
    age_rollover.native_stream_now = 481.0
    await age_rollover.send_draft(
        "user-age-rollover",
        1002,
        "phase one plus tail",
        age_metadata,
    )
    age_bodies = [call[2] for call in age_rollover.api_calls]
    assert [body["input_state"] for body in age_bodies] == [1, 1, 10, 1]
    assert [body["index"] for body in age_bodies] == [0, 1, 2, 0]
    assert age_bodies[2]["content_raw"] == "phase one plus"
    assert age_bodies[3]["content_raw"] == " tail"
    assert "stream_msg_id" not in age_bodies[3]

    # Expiry must not depend on another draft callback. Open one carrier, let
    # the independently scheduled fake timer reach 480 seconds while the turn
    # stays silent, and require the old carrier to seal before any new delta.
    silent_expiry = DummyAdapter()
    silent_expiry._qq_native_stream_max_age_seconds = 480.0
    expiry_waiting = anyio.Event()
    expiry_release = anyio.Event()

    async def release_silent_expiry(delay):
        assert delay == 480.0
        expiry_waiting.set()
        await expiry_release.wait()
        silent_expiry.native_stream_now += delay

    silent_expiry._qq_native_stream_sleep = release_silent_expiry
    silent_metadata = {"reply_to_message_id": "inbound-silent-expiry"}
    await silent_expiry.send_draft(
        "user-silent-expiry",
        1003,
        "silent phase",
        silent_metadata,
    )
    with anyio.fail_after(1):
        await expiry_waiting.wait()
    assert len(silent_expiry.api_calls) == 1
    expiry_release.set()
    with anyio.fail_after(1):
        while len(silent_expiry.api_calls) < 2:
            await anyio.sleep(0)
    silent_bodies = [call[2] for call in silent_expiry.api_calls]
    assert [body["input_state"] for body in silent_bodies] == [1, 10]
    assert [body["index"] for body in silent_bodies] == [0, 1]
    await silent_expiry.send_draft(
        "user-silent-expiry",
        1003,
        "silent phase resumed",
        silent_metadata,
    )
    silent_bodies = [call[2] for call in silent_expiry.api_calls]
    assert [body["index"] for body in silent_bodies] == [0, 1, 0]
    assert silent_bodies[-1]["content_raw"] == " resumed"
    assert "stream_msg_id" not in silent_bodies[-1]

    # A deadline rollover may leave the final fully owned by a sealed head,
    # including an empty final callback. Completing it must not open a carrier.
    for final_text in ("committed head", ""):
        committed_final = DummyAdapter()
        committed_metadata = {"reply_to_message_id": "inbound-committed-final"}
        await committed_final.send_draft(
            "user-committed-final", 1004, "committed head", committed_metadata,
        )
        committed_final.native_stream_now = 481.0
        result = await committed_final.send(
            "user-committed-final", final_text,
            metadata={"notify": True, **committed_metadata},
        )
        assert result.success and result.message_id == "stream-1"
        assert [call[2]["input_state"] for call in committed_final.api_calls] == [1, 10]
        assert not committed_final.normal_sends
        assert not streaming_mod._stream_maps(committed_final)[0]
        assert_exact_final_ownership(committed_final, "committed head")
    print("qq_c2c_committed_final_without_active_carrier=ok")

    # Completing a turn before its deadline must cancel the independent timer;
    # releasing the old sleeper afterwards cannot emit a late seal.
    cancelled_expiry = DummyAdapter()
    cancelled_waiting = anyio.Event()
    cancelled_release = anyio.Event()
    cancelled_observed = anyio.Event()

    async def observe_cancelled_expiry(delay):
        assert delay == 480.0
        cancelled_waiting.set()
        try:
            await cancelled_release.wait()
        finally:
            cancelled_observed.set()

    cancelled_expiry._qq_native_stream_sleep = observe_cancelled_expiry
    cancelled_metadata = {"reply_to_message_id": "inbound-cancelled-expiry"}
    await cancelled_expiry.send_draft(
        "user-cancelled-expiry",
        1012,
        "progress",
        cancelled_metadata,
    )
    with anyio.fail_after(1):
        await cancelled_waiting.wait()
    cancelled_final = await cancelled_expiry.send(
        "user-cancelled-expiry",
        "progress\nFINAL",
        reply_to="inbound-cancelled-expiry",
        metadata={"notify": True, **cancelled_metadata},
    )
    assert cancelled_final.success
    with anyio.fail_after(1):
        await cancelled_observed.wait()
    calls_after_cancelled_final = len(cancelled_expiry.api_calls)
    cancelled_release.set()
    await anyio.sleep(0)
    assert len(cancelled_expiry.api_calls) == calls_after_cancelled_final

    # QQ can consume and display a continuation frame while returning the
    # terminal stream-lifetime error.  The next request would then receive
    # "index needs to increment".  Treat that carrier as deliberately retired:
    # no later draft or seal may touch it, and final delivery owns only the
    # suffix beyond the accepted terminal frame.
    lifetime_terminal = DummyAdapter()
    lifetime_metadata = {"reply_to_message_id": "inbound-lifetime-terminal"}
    await lifetime_terminal.send_draft(
        "user-lifetime-terminal",
        1004,
        "progress",
        lifetime_metadata,
    )
    lifetime_terminal.accept_then_expire_stream = True
    accepted_terminal_text = "progress accepted before expiry"
    await lifetime_terminal.send_draft(
        "user-lifetime-terminal",
        1004,
        accepted_terminal_text,
        lifetime_metadata,
    )
    calls_after_terminal = len(lifetime_terminal.api_calls)
    await lifetime_terminal.send_draft(
        "user-lifetime-terminal",
        1004,
        accepted_terminal_text + " ignored late draft",
        lifetime_metadata,
    )
    assert len(lifetime_terminal.api_calls) == calls_after_terminal
    lifetime_final_text = accepted_terminal_text + "\nFINAL"
    lifetime_final = await lifetime_terminal.send(
        "user-lifetime-terminal",
        lifetime_final_text,
        reply_to="inbound-lifetime-terminal",
        metadata={"notify": True, **lifetime_metadata},
    )
    assert lifetime_final.success
    assert len(lifetime_terminal.api_calls) == calls_after_terminal
    assert [item[1] for item in lifetime_terminal.normal_sends] == ["\nFINAL"]
    assert accepted_terminal_text + lifetime_terminal.normal_sends[0][1] == lifetime_final_text
    lifetime_repeat = await lifetime_terminal.send(
        "user-lifetime-terminal",
        lifetime_final_text,
        reply_to="inbound-lifetime-terminal",
        metadata={"notify": True, **lifetime_metadata},
    )
    assert lifetime_repeat.success
    assert len(lifetime_terminal.normal_sends) == 1

    # A transport timeout is ambiguous: QQ may have accepted and displayed the
    # submitted frame even though the client received no response. Reconcile
    # that exact index at most once. If QQ says the index must advance, promote
    # the timed-out body to acknowledged ownership, continue at the next index,
    # and never enter a stale-index retry loop.
    ambiguous_timeout = DummyAdapter()
    ambiguous_metadata = {
        "reply_to_message_id": "inbound-ambiguous-timeout"
    }
    await ambiguous_timeout.send_draft(
        "user-ambiguous-timeout",
        1008,
        "progress",
        ambiguous_metadata,
    )
    ambiguous_timeout.accept_then_timeout_stream = True
    timed_out_body = "progress accepted before timeout"
    await ambiguous_timeout.send_draft(
        "user-ambiguous-timeout",
        1008,
        timed_out_body,
        ambiguous_metadata,
    )
    ambiguous_timeout.native_stream_now = 0.2
    latest_body = timed_out_body + " plus latest"
    await ambiguous_timeout.send_draft(
        "user-ambiguous-timeout",
        1008,
        latest_body,
        ambiguous_metadata,
    )
    ambiguous_bodies = [call[2] for call in ambiguous_timeout.api_calls]
    assert [body["index"] for body in ambiguous_bodies] == [0, 1, 1, 2]
    assert ambiguous_bodies[-1]["content_raw"] == latest_body
    assert [
        call[2]["index"] for call in ambiguous_timeout.successful_api_calls
    ] == [0, 2]
    ambiguous_final = await ambiguous_timeout.send(
        "user-ambiguous-timeout",
        latest_body + "\nFINAL",
        reply_to="inbound-ambiguous-timeout",
        metadata={"notify": True, **ambiguous_metadata},
    )
    assert ambiguous_final.success
    assert not ambiguous_timeout.normal_sends

    # If the lost response belongs to a seal, the bounded reconciliation that
    # receives "index must advance" has already proved that exact seal frame
    # was consumed. Do not emit a second seal at the next index.
    ambiguous_seal = DummyAdapter()
    ambiguous_seal_metadata = {
        "reply_to_message_id": "inbound-ambiguous-seal"
    }
    await ambiguous_seal.send_draft(
        "user-ambiguous-seal",
        1010,
        "progress",
        ambiguous_seal_metadata,
    )
    ambiguous_seal.accept_then_timeout_stream = True
    ambiguous_seal_result = await ambiguous_seal.abandon_open_draft(
        "user-ambiguous-seal",
        "progress",
        ambiguous_seal_metadata,
    )
    assert ambiguous_seal_result.success
    ambiguous_seal_bodies = [call[2] for call in ambiguous_seal.api_calls]
    assert [body["index"] for body in ambiguous_seal_bodies] == [0, 1, 1]
    assert [body["input_state"] for body in ambiguous_seal_bodies] == [1, 10, 10]

    # If even the one reconciliation of an ambiguous index-0 open loses its
    # response, the carrier id is unknowable. Retire it locally, never retry a
    # third time, and preserve the newest coalesced body in one ordinary final
    # instead of assuming the uncertain open was visible.
    ambiguous_open = DummyAdapter()
    ambiguous_open.timeout_first_stream_attempts = 2
    ambiguous_open_metadata = {
        "reply_to_message_id": "inbound-ambiguous-open"
    }
    await ambiguous_open.send_draft(
        "user-ambiguous-open",
        1011,
        "opening progress",
        ambiguous_open_metadata,
    )
    ambiguous_open.native_stream_now = 0.2
    latest_ambiguous_open = "opening progress plus latest"
    await ambiguous_open.send_draft(
        "user-ambiguous-open",
        1011,
        latest_ambiguous_open,
        ambiguous_open_metadata,
    )
    calls_after_ambiguous_open = len(ambiguous_open.api_calls)
    await ambiguous_open.send_draft(
        "user-ambiguous-open",
        1011,
        latest_ambiguous_open + " ignored late draft",
        ambiguous_open_metadata,
    )
    assert calls_after_ambiguous_open == 2
    assert len(ambiguous_open.api_calls) == calls_after_ambiguous_open
    ambiguous_open_final = await ambiguous_open.send(
        "user-ambiguous-open",
        "FINAL",
        reply_to="inbound-ambiguous-open",
        metadata={"notify": True, **ambiguous_open_metadata},
    )
    assert ambiguous_open_final.success
    assert [item[1] for item in ambiguous_open.normal_sends] == [
        latest_ambiguous_open + "\nFINAL"
    ]

    # Ordinary non-terminal failures must not turn every following delta into
    # another request. Each carrier uses bounded cooldowns and coalesces all
    # cumulative updates in that window into the latest body.
    cooled_frames = DummyAdapter()
    cooled_frames._qq_native_stream_frame_retry_delays = (10.0, 20.0)
    cooled_metadata = {"reply_to_message_id": "inbound-cooled-frames"}
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress",
        cooled_metadata,
    )
    cooled_frames.fail_stream_attempts = 2
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress one",
        cooled_metadata,
    )
    cooled_frames.native_stream_now = 1.0
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress two",
        cooled_metadata,
    )
    cooled_frames.native_stream_now = 9.0
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress three",
        cooled_metadata,
    )
    assert len(cooled_frames.api_calls) == 2
    cooled_frames.native_stream_now = 10.0
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress three",
        cooled_metadata,
    )
    cooled_frames.native_stream_now = 11.0
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress four",
        cooled_metadata,
    )
    cooled_frames.native_stream_now = 29.0
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress five",
        cooled_metadata,
    )
    assert len(cooled_frames.api_calls) == 3
    cooled_frames.native_stream_now = 30.0
    await cooled_frames.send_draft(
        "user-cooled-frames",
        1009,
        "progress five",
        cooled_metadata,
    )
    cooled_bodies = [call[2] for call in cooled_frames.api_calls]
    assert [body["index"] for body in cooled_bodies] == [0, 1, 1, 1]
    assert [body["content_raw"] for body in cooled_bodies] == [
        "progress",
        "progress one",
        "progress three",
        "progress five",
    ]

    # Once QQ declares the inbound passive-reply window exhausted, neither a
    # new native carrier nor an ordinary retry can make that anchor writable.
    # Retire the replacement carrier immediately and suppress all later draft
    # requests instead of applying the ordinary cooldown forever.
    reply_budget_terminal = DummyAdapter()
    reply_budget_terminal._qq_native_stream_max_age_seconds = 480.0
    reply_budget_metadata = {
        "reply_to_message_id": "inbound-reply-budget-terminal"
    }
    await reply_budget_terminal.send_draft(
        "user-reply-budget-terminal",
        1014,
        "progress",
        reply_budget_metadata,
    )
    reply_budget_terminal.native_stream_now = 481.0
    reply_budget_terminal.fail_reply_budget_on_new_stream = True
    await reply_budget_terminal.send_draft(
        "user-reply-budget-terminal",
        1014,
        "progress after rollover",
        reply_budget_metadata,
    )
    calls_after_reply_budget = len(reply_budget_terminal.api_calls)
    reply_budget_terminal.native_stream_now = 600.0
    await reply_budget_terminal.send_draft(
        "user-reply-budget-terminal",
        1014,
        "progress ignored after terminal budget",
        reply_budget_metadata,
    )
    await reply_budget_terminal.send_draft(
        "user-reply-budget-terminal",
        1014,
        "another ignored delta",
        reply_budget_metadata,
    )
    assert calls_after_reply_budget == 3
    assert len(reply_budget_terminal.api_calls) == calls_after_reply_budget
    terminal_final = await reply_budget_terminal.send(
        "user-reply-budget-terminal",
        "final cannot use an exhausted inbound anchor",
        metadata={"notify": True, **reply_budget_metadata},
    )
    assert not terminal_final.success
    assert not reply_budget_terminal.normal_send_attempts
    assert len(reply_budget_terminal.api_calls) == calls_after_reply_budget

    # A final may arrive while the carrier is still cooling down. The latest
    # coalesced cumulative body must remain the lossless base for that final.
    cooled_final = DummyAdapter()
    cooled_final._qq_native_stream_frame_retry_delays = (10.0,)
    cooled_final_metadata = {"reply_to_message_id": "inbound-cooled-final"}
    await cooled_final.send_draft(
        "user-cooled-final",
        1013,
        "progress",
        cooled_final_metadata,
    )
    cooled_final.fail_stream_attempts = 1
    await cooled_final.send_draft(
        "user-cooled-final",
        1013,
        "progress one",
        cooled_final_metadata,
    )
    cooled_final.native_stream_now = 1.0
    latest_cooled_final = "progress one plus latest"
    await cooled_final.send_draft(
        "user-cooled-final",
        1013,
        latest_cooled_final,
        cooled_final_metadata,
    )
    cooled_final_result = await cooled_final.send(
        "user-cooled-final",
        "FINAL",
        reply_to="inbound-cooled-final",
        metadata={"notify": True, **cooled_final_metadata},
    )
    assert cooled_final_result.success
    assert_exact_final_ownership(
        cooled_final,
        latest_cooled_final + "\nFINAL",
    )

    # The terminal lifetime response can also arrive on the final cumulative
    # replace itself.  That accepted frame already owns the whole final, so the
    # completion path must tombstone it without a seal or ordinary duplicate.
    lifetime_during_final = DummyAdapter()
    lifetime_final_metadata = {
        "reply_to_message_id": "inbound-lifetime-during-final"
    }
    await lifetime_during_final.send_draft(
        "user-lifetime-during-final",
        1005,
        "progress",
        lifetime_final_metadata,
    )
    lifetime_during_final.accept_then_expire_stream = True
    accepted_whole_final = "progress\nFINAL"
    lifetime_during_final_result = await lifetime_during_final.send(
        "user-lifetime-during-final",
        accepted_whole_final,
        reply_to="inbound-lifetime-during-final",
        metadata={"notify": True, **lifetime_final_metadata},
    )
    assert lifetime_during_final_result.success
    assert len(lifetime_during_final.api_calls) == 2
    assert not lifetime_during_final.normal_sends
    lifetime_during_final_repeat = await lifetime_during_final.send(
        "user-lifetime-during-final",
        accepted_whole_final,
        reply_to="inbound-lifetime-during-final",
        metadata={"notify": True, **lifetime_final_metadata},
    )
    assert lifetime_during_final_repeat.success
    assert len(lifetime_during_final.api_calls) == 2

    # The same terminal response may arrive while proactive age rollover seals
    # the old carrier.  One seal attempt retires it; no stale seal/index retry
    # is allowed, and the eventual final owns only the unseen suffix.
    lifetime_during_rollover = DummyAdapter()
    lifetime_during_rollover._qq_native_stream_max_age_seconds = 480.0
    lifetime_rollover_metadata = {
        "reply_to_message_id": "inbound-lifetime-during-rollover"
    }
    await lifetime_during_rollover.send_draft(
        "user-lifetime-during-rollover",
        1006,
        "phase one",
        lifetime_rollover_metadata,
    )
    lifetime_during_rollover.accept_then_expire_stream = True
    lifetime_during_rollover.native_stream_now = 481.0
    await lifetime_during_rollover.send_draft(
        "user-lifetime-during-rollover",
        1006,
        "phase one late draft",
        lifetime_rollover_metadata,
    )
    assert len(lifetime_during_rollover.api_calls) == 2
    assert lifetime_during_rollover.api_calls[-1][2]["input_state"] == 10
    lifetime_rollover_final = await lifetime_during_rollover.send(
        "user-lifetime-during-rollover",
        "phase one\nFINAL",
        reply_to="inbound-lifetime-during-rollover",
        metadata={"notify": True, **lifetime_rollover_metadata},
    )
    assert lifetime_rollover_final.success
    assert len(lifetime_during_rollover.api_calls) == 2
    assert [item[1] for item in lifetime_during_rollover.normal_sends] == [
        "\nFINAL"
    ]

    # Cancellation also terminalizes retired state locally without touching the
    # expired carrier.  A later real final remains eligible for one full normal
    # delivery because cancellation did not claim it as delivered.
    lifetime_cancelled = DummyAdapter()
    lifetime_cancelled_metadata = {
        "reply_to_message_id": "inbound-lifetime-cancelled"
    }
    await lifetime_cancelled.send_draft(
        "user-lifetime-cancelled",
        1007,
        "progress",
        lifetime_cancelled_metadata,
    )
    lifetime_cancelled.accept_then_expire_stream = True
    await lifetime_cancelled.send_draft(
        "user-lifetime-cancelled",
        1007,
        "progress accepted",
        lifetime_cancelled_metadata,
    )
    lifetime_cancelled_call_count = len(lifetime_cancelled.api_calls)
    lifetime_cancelled_close = await lifetime_cancelled.abandon_open_draft(
        "user-lifetime-cancelled",
        "progress accepted",
        lifetime_cancelled_metadata,
    )
    assert lifetime_cancelled_close.success
    assert len(lifetime_cancelled.api_calls) == lifetime_cancelled_call_count
    lifetime_cancelled_final = await lifetime_cancelled.send(
        "user-lifetime-cancelled",
        "FULL FINAL",
        reply_to="inbound-lifetime-cancelled",
        metadata={"notify": True, **lifetime_cancelled_metadata},
    )
    assert lifetime_cancelled_final.success
    assert [item[1] for item in lifetime_cancelled.normal_sends] == ["FULL FINAL"]

    # Hermes can stream user-visible commentary before it produces the short
    # turn-final answer. QQ replace mode forbids removing an already-delivered
    # prefix, so sealing must retain the cumulative draft instead of replacing
    # it with the shorter final-only string.
    prefixed = DummyAdapter()
    prefixed_metadata = {"reply_to_message_id": "inbound-prefix"}
    await prefixed.send_draft(
        "user-prefix",
        1003,
        "开始只读检查。",
        prefixed_metadata,
    )
    await prefixed.send_draft(
        "user-prefix",
        1003,
        "开始只读检查。\n最终答案",
        prefixed_metadata,
    )
    prefixed_final = await prefixed.send(
        "user-prefix",
        "最终答案",
        reply_to="inbound-prefix",
        metadata={
            "notify": True,
            "reply_to_message_id": "inbound-prefix",
        },
    )
    assert prefixed_final.success
    assert prefixed.api_calls[-1][2]["input_state"] == 10
    assert (
        prefixed.api_calls[-1][2]["content_raw"]
        == "开始只读检查。\n最终答案"
    )
    assert not prefixed.normal_sends

    # Hermes' completed commentary callback is an ordinary ``_interim_send``.
    # When the exact item is already the token-bounded terminal payload of the
    # same anchored native stream, that second carrier must be acknowledged
    # without creating another QQ bubble.
    interim_owned = DummyAdapter()
    interim_owned_metadata = {
        "reply_to_message_id": "inbound-interim-owned"
    }
    await interim_owned.send_draft(
        "user-interim-owned",
        1004,
        "第一段\nSTATUS",
        interim_owned_metadata,
    )
    owned_interim = await interim_owned.send(
        "user-interim-owned",
        "STATUS",
        reply_to="inbound-interim-owned",
        metadata={"_interim_send": True, **interim_owned_metadata},
    )
    assert owned_interim.success
    assert owned_interim.raw_response["qq_stream_owned_interim"] is True
    assert not interim_owned.normal_sends

    # A nonterminal occurrence is not ownership evidence; neither are a
    # word-internal suffix, a different inbound anchor, or a non-interim send.
    interim_nonterminal = DummyAdapter()
    await interim_nonterminal.send_draft(
        "user-interim-nonterminal",
        1005,
        "STATUS\n继续处理",
        {"reply_to_message_id": "inbound-interim-nonterminal"},
    )
    await interim_nonterminal.send(
        "user-interim-nonterminal",
        "STATUS",
        reply_to="inbound-interim-nonterminal",
        metadata={
            "_interim_send": True,
            "reply_to_message_id": "inbound-interim-nonterminal",
        },
    )
    assert interim_nonterminal.normal_sends[-1][1] == "STATUS"

    for word_draft_id, word_prefix in ((1006, "NOT"), (1007, "NOT_")):
        interim_word_suffix = DummyAdapter()
        word_anchor = f"inbound-interim-word-suffix-{word_draft_id}"
        await interim_word_suffix.send_draft(
            "user-interim-word-suffix",
            word_draft_id,
            f"{word_prefix}STATUS",
            {"reply_to_message_id": word_anchor},
        )
        await interim_word_suffix.send(
            "user-interim-word-suffix",
            "STATUS",
            reply_to=word_anchor,
            metadata={
                "_interim_send": True,
                "reply_to_message_id": word_anchor,
            },
        )
        assert interim_word_suffix.normal_sends[-1][1] == "STATUS"

    # Unicode punctuation is a real message boundary. Completed commentary
    # following either Western or CJK punctuation is already owned by the
    # native stream and must not create an ordinary duplicate bubble.
    for draft_id, punctuation in enumerate((",", "，", "—"), start=1020):
        interim_punctuation = DummyAdapter()
        punctuation_anchor = f"inbound-interim-punctuation-{draft_id}"
        await interim_punctuation.send_draft(
            f"user-interim-punctuation-{draft_id}",
            draft_id,
            f"阶段一{punctuation}STATUS",
            {"reply_to_message_id": punctuation_anchor},
        )
        punctuation_result = await interim_punctuation.send(
            f"user-interim-punctuation-{draft_id}",
            "STATUS",
            reply_to=punctuation_anchor,
            metadata={
                "_interim_send": True,
                "reply_to_message_id": punctuation_anchor,
            },
        )
        assert punctuation_result.success
        assert not interim_punctuation.normal_sends

    await interim_owned.send(
        "user-interim-owned",
        "STATUS",
        reply_to="different-anchor",
        metadata={
            "_interim_send": True,
            "reply_to_message_id": "different-anchor",
        },
    )
    await interim_owned.send(
        "user-interim-owned",
        "STATUS",
        reply_to="inbound-interim-owned",
        metadata={"non_conversational": True},
    )
    assert [item[1] for item in interim_owned.normal_sends] == [
        "STATUS",
        "STATUS",
    ]

    # The ownership check spans already-sealed rollover heads plus the open
    # tail, so a completed commentary at the 4000-character boundary does not
    # fall back to an oversized duplicate ordinary message.
    interim_overflow = DummyAdapter()
    interim_overflow_text = "X" * 4000 + "\nSTATUS"
    await interim_overflow.send_draft(
        "user-interim-overflow",
        1007,
        interim_overflow_text,
        {"reply_to_message_id": "inbound-interim-overflow"},
    )
    overflow_interim = await interim_overflow.send(
        "user-interim-overflow",
        interim_overflow_text,
        reply_to="inbound-interim-overflow",
        metadata={
            "_interim_send": True,
            "reply_to_message_id": "inbound-interim-overflow",
        },
    )
    assert overflow_interim.success
    assert not interim_overflow.normal_sends

    # Hermes currently omits the reply anchor from _send_commentary. A unique
    # same-chat terminal owner is safe to recover; two matching concurrent
    # streams are ambiguous and must not be guessed.
    interim_ambiguous = DummyAdapter()
    for draft_id, anchor in (
        (1008, "inbound-interim-a"),
        (1009, "inbound-interim-b"),
    ):
        await interim_ambiguous.send_draft(
            "user-interim-ambiguous",
            draft_id,
            "SAME STATUS",
            {"reply_to_message_id": anchor},
        )
    ambiguous_interim = await interim_ambiguous.send(
        "user-interim-ambiguous",
        "SAME STATUS",
        metadata={"_interim_send": True},
    )
    assert ambiguous_interim.success
    assert interim_ambiguous.normal_sends[-1][1] == "SAME STATUS"

    interim_unowned = DummyAdapter()
    unowned_interim = await interim_unowned.send(
        "user-interim-unowned",
        "NO STREAM",
        metadata={"_interim_send": True},
    )
    assert unowned_interim.success
    assert interim_unowned.normal_sends[-1][1] == "NO STREAM"

    # A non-final message must never seal or hijack the open stream.
    await adapter.send_draft(
        "user-2",
        1002,
        "处理中",
        {"reply_to_message_id": "inbound-2"},
    )
    ordinary = await adapter.send(
        "user-2",
        "审批提示",
        reply_to="inbound-2",
        metadata={"non_conversational": True},
    )
    assert ordinary.success
    assert adapter.normal_sends[-1][1] == "审批提示"
    await adapter.send(
        "user-2",
        "完成",
        reply_to="inbound-2",
        metadata={"notify": True, "reply_to_message_id": "inbound-2"},
    )

    # Two simultaneous DMs keep independent stream ids and indices.
    await adapter.send_draft(
        "user-a", 2001, "A1", {"reply_to_message_id": "msg-a"}
    )
    await adapter.send_draft(
        "user-b", 2002, "B1", {"reply_to_message_id": "msg-b"}
    )
    await adapter.send_draft(
        "user-a", 2001, "A2", {"reply_to_message_id": "msg-a"}
    )
    bodies = [call[2] for call in adapter.api_calls[-3:]]
    assert [body["index"] for body in bodies] == [0, 0, 1]
    assert [body["msg_id"] for body in bodies] == ["msg-a", "msg-b", "msg-a"]
    assert "stream_msg_id" not in bodies[0]
    assert "stream_msg_id" not in bodies[1]
    assert bodies[2]["stream_msg_id"].startswith("stream-")

    # Draft ids are unique only within one chat at the public adapter seam.
    # Two private chats may therefore use the same id concurrently without
    # sharing, rejecting, or replacing each other's native stream state.
    same_draft_id = DummyAdapter()
    same_a = await same_draft_id.send_draft(
        "user-same-draft-a",
        2003,
        "A",
        {"reply_to_message_id": "msg-same-draft-a"},
    )
    same_b = await same_draft_id.send_draft(
        "user-same-draft-b",
        2003,
        "B",
        {"reply_to_message_id": "msg-same-draft-b"},
    )
    assert same_a.success and same_b.success
    assert [call[2]["msg_id"] for call in same_draft_id.api_calls] == [
        "msg-same-draft-a",
        "msg-same-draft-b",
    ]
    assert [call[2]["content_raw"] for call in same_draft_id.api_calls] == [
        "A",
        "B",
    ]

    # Cancelling a turn seals the visible stream instead of leaving it live.
    abandoned = await adapter.abandon_open_draft(
        "user-a",
        "任务已停止",
        {"reply_to_message_id": "msg-a"},
    )
    assert abandoned.success
    assert adapter.api_calls[-1][2]["input_state"] == 10

    # A failed first frame stays on the native lane so Hermes cannot emit an
    # uneditable partial. The turn-final wrapper then falls back to exactly
    # one original normal message when no stream ever opened.
    fallback = DummyAdapter()
    fallback.fail_next_stream = True
    failed = await fallback.send_draft(
        "user-f", 3001, "处理中", {"reply_to_message_id": "msg-f"}
    )
    assert failed.success
    normal = await fallback.send(
        "user-f",
        "最终回退",
        reply_to="msg-f",
        metadata={"notify": True, "reply_to_message_id": "msg-f"},
    )
    assert normal.success
    assert fallback.normal_sends[-1][1] == "最终回退"

    # A transient seal error retries the same acknowledged index and closes
    # the stream without emitting an ordinary duplicate final.
    seal_retry = DummyAdapter()
    await seal_retry.send_draft(
        "user-seal-retry",
        3101,
        "处理中",
        {"reply_to_message_id": "msg-seal-retry"},
    )
    seal_retry.fail_seal_attempts = 1
    retried = await seal_retry.send(
        "user-seal-retry",
        "最终答案",
        reply_to="msg-seal-retry",
        metadata={"notify": True, "reply_to_message_id": "msg-seal-retry"},
    )
    assert retried.success
    assert not seal_retry.normal_sends
    retry_streams, _retry_anchors = streaming_mod._stream_maps(seal_retry)
    assert not retry_streams

    # After one bounded close round fails, retry the already-visible composed
    # final in place. Do not emit an ordinary duplicate.
    seal_degrade = DummyAdapter()
    await seal_degrade.send_draft(
        "user-seal-degrade",
        3102,
        "处理中",
        {"reply_to_message_id": "msg-seal-degrade"},
    )
    seal_degrade.fail_seal_attempts = len(streaming_mod._SEAL_RETRY_DELAYS)
    degraded = await seal_degrade.send(
        "user-seal-degrade",
        "最终回退",
        reply_to="msg-seal-degrade",
        metadata={
            "notify": True,
            "reply_to_message_id": "msg-seal-degrade",
        },
    )
    assert degraded.success
    assert not seal_degrade.normal_sends
    degrade_streams, _degrade_anchors = streaming_mod._stream_maps(seal_degrade)
    assert not degrade_streams
    assert seal_degrade.api_calls[-1][2]["input_state"] == 10
    assert seal_degrade.api_calls[-1][2]["content_raw"] == "处理中\n最终回退"
    assert_exact_final_ownership(seal_degrade, "处理中\n最终回退")

    # If both close rounds remain unavailable, the complete visible stream is
    # still a single owner and stays addressable for an explicit later retry.
    seal_recover = DummyAdapter()
    await seal_recover.send_draft(
        "user-seal-recover",
        3103,
        "处理中",
        {"reply_to_message_id": "msg-seal-recover"},
    )
    seal_recover.fail_seal_attempts = len(streaming_mod._SEAL_RETRY_DELAYS) * 2
    recovered_fallback = await seal_recover.send(
        "user-seal-recover",
        "最终回退",
        reply_to="msg-seal-recover",
        metadata={
            "notify": True,
            "reply_to_message_id": "msg-seal-recover",
        },
    )
    assert recovered_fallback.success
    assert not seal_recover.normal_sends
    recover_streams, _recover_anchors = streaming_mod._stream_maps(seal_recover)
    assert ("user-seal-recover", 3103) in recover_streams
    assert (
        recover_streams[("user-seal-recover", 3103)].close_pending_final_content
        == "处理中\n最终回退"
    )
    assert_exact_final_ownership(seal_recover, "处理中\n最终回退")
    calls_before_close_pending_late_frame = len(seal_recover.api_calls)
    ignored_close_pending_late_frame = await seal_recover.send_draft(
        "user-seal-recover",
        3103,
        "处理中\n最终回退\nLATE",
        {"reply_to_message_id": "msg-seal-recover"},
    )
    assert ignored_close_pending_late_frame.success
    assert (
        ignored_close_pending_late_frame.raw_response[
            "qq_visible_final_owned"
        ]
        is True
    )
    assert len(seal_recover.api_calls) == calls_before_close_pending_late_frame
    streams_before_changed_draft = dict(recover_streams)
    anchors_before_changed_draft = dict(_recover_anchors)
    ignored_changed_draft = await seal_recover.send_draft(
        "user-seal-recover",
        3199,
        "处理中\n最终回退\nCHANGED-DRAFT-LATE",
        {"reply_to_message_id": "msg-seal-recover"},
    )
    assert ignored_changed_draft.success
    assert ignored_changed_draft.raw_response["qq_visible_final_owned"] is True
    assert len(seal_recover.api_calls) == calls_before_close_pending_late_frame
    assert recover_streams == streams_before_changed_draft
    assert _recover_anchors == anchors_before_changed_draft

    # The anchor guard is turn-scoped, not a chat-wide serialization rule.
    # Another inbound anchor in the same private chat can still stream.
    independent_anchor = "msg-seal-recover-independent"
    independent_draft = await seal_recover.send_draft(
        "user-seal-recover",
        3200,
        "INDEPENDENT",
        {"reply_to_message_id": independent_anchor},
    )
    assert independent_draft.success
    assert len(seal_recover.api_calls) == calls_before_close_pending_late_frame + 1
    assert ("user-seal-recover", 3200) in recover_streams
    assert _recover_anchors[("user-seal-recover", independent_anchor)] == (
        "user-seal-recover",
        3200,
    )
    independent_closed = await seal_recover.abandon_open_draft(
        "user-seal-recover",
        "INDEPENDENT",
        {"reply_to_message_id": independent_anchor},
    )
    assert independent_closed.success
    assert ("user-seal-recover", 3200) not in recover_streams
    closed_after_failure = await seal_recover.abandon_open_draft(
        "user-seal-recover",
        "最终回退",
        {"reply_to_message_id": "msg-seal-recover"},
    )
    assert closed_after_failure.success
    assert ("user-seal-recover", 3103) not in recover_streams
    assert [
        call[2]["content_raw"]
        for call in seal_recover.successful_api_calls
        if (
            call[2]["input_state"] == 10
            and call[2]["content_raw"] == "处理中\n最终回退"
        )
    ] == ["处理中\n最终回退"]
    # Abandon is an out-of-band completion path. Even if the separate
    # per-chat tombstone is later evicted, the broker's bounded replay result
    # must keep a repeated same-anchor final from opening another carrier.
    completed_owner_limit = streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
    streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = 1
    try:
        streaming_mod._remember_turn_tombstone(
            seal_recover,
            streaming_mod._QQC2CStream(
                chat_id="user-seal-recover",
                draft_id=9100,
                reply_to="evict-seal-recover-owner",
                msg_seq=9100,
            ),
            final_payload="independent",
            final_content="independent",
        )
        replayed_after_close = await seal_recover.send(
            "user-seal-recover",
            "最终回退",
            reply_to="msg-seal-recover",
            metadata={
                "notify": True,
                "reply_to_message_id": "msg-seal-recover",
            },
        )
        assert replayed_after_close.success
    finally:
        streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = completed_owner_limit
    assert not seal_recover.normal_sends
    assert ("user-seal-recover", 3103) not in recover_streams

    # A normal abandonment has no complete turn-final identity. It may retain
    # content-aware tombstone evidence, but must not publish an anchor-wide
    # broker replay result that would swallow a later, different final.
    partial_abandon = DummyAdapter()
    partial_anchor = "msg-partial-abandon"
    await partial_abandon.send_draft(
        "user-partial-abandon",
        3104,
        "部分结果",
        {"reply_to_message_id": partial_anchor},
    )
    partial_closed = await partial_abandon.abandon_open_draft(
        "user-partial-abandon",
        "部分结果",
        {"reply_to_message_id": partial_anchor},
    )
    assert partial_closed.success
    partial_broker = streaming_mod._final_delivery_broker(partial_abandon)
    assert partial_broker.stats().completed == 0
    delivered_after_partial_abandon = await partial_abandon.send(
        "user-partial-abandon",
        "真正最终",
        reply_to=partial_anchor,
        metadata={
            "notify": True,
            "reply_to_message_id": partial_anchor,
        },
    )
    assert delivered_after_partial_abandon.success
    assert [item[1] for item in partial_abandon.normal_sends] == ["真正最终"]

    # Capacity pressure never discards an opened stream. The extra turn stays
    # final-only, while both existing streams remain sealable.
    capacity = DummyAdapter()
    previous_capacity = streaming_mod._MAX_OPEN_STREAMS
    streaming_mod._MAX_OPEN_STREAMS = 2
    try:
        await capacity.send_draft(
            "user-cap-a", 3201, "A", {"reply_to_message_id": "msg-cap-a"}
        )
        await capacity.send_draft(
            "user-cap-b", 3202, "B", {"reply_to_message_id": "msg-cap-b"}
        )
        before_extra = len(capacity.api_calls)
        extra = await capacity.send_draft(
            "user-cap-c", 3203, "C", {"reply_to_message_id": "msg-cap-c"}
        )
        assert extra.success
        assert len(capacity.api_calls) == before_extra
        capacity_streams, _capacity_anchors = streaming_mod._stream_maps(capacity)
        assert set(capacity_streams) == {
            ("user-cap-a", 3201),
            ("user-cap-b", 3202),
        }
        capacity_final = await capacity.send(
            "user-cap-c",
            "C final",
            reply_to="msg-cap-c",
            metadata={"notify": True, "reply_to_message_id": "msg-cap-c"},
        )
        assert capacity_final.success
        assert capacity.normal_sends[-1][1] == "C final"
        assert set(capacity_streams) == {
            ("user-cap-a", 3201),
            ("user-cap-b", 3202),
        }
        capacity_final_repeat = await capacity.send(
            "user-cap-c",
            "C final",
            reply_to="msg-cap-c",
            metadata={"notify": True, "reply_to_message_id": "msg-cap-c"},
        )
        assert capacity_final_repeat.success
        assert [item[1] for item in capacity.normal_sends] == ["C final"]
        await capacity.abandon_open_draft(
            "user-cap-a", "A", {"reply_to_message_id": "msg-cap-a"}
        )
        await capacity.abandon_open_draft(
            "user-cap-b", "B", {"reply_to_message_id": "msg-cap-b"}
        )
        await capacity.send_draft(
            "user-cap-c", 3203, "C final", {"reply_to_message_id": "msg-cap-c"}
        )
        assert ("user-cap-c", 3203) not in capacity_streams
    finally:
        streaming_mod._MAX_OPEN_STREAMS = previous_capacity

    # Capacity-final-only identities also need a total chat bound. Once the
    # pending-chat LRU exceeds that bound, the oldest identity may expire, but
    # the newest pending chat must remain acknowledged without a QQ frame.
    pending_chat_limit_existed = hasattr(
        streaming_mod,
        "_MAX_FINAL_ONLY_PENDING_CHATS",
    )
    pending_chat_limit = getattr(
        streaming_mod,
        "_MAX_FINAL_ONLY_PENDING_CHATS",
        None,
    )
    previous_capacity = streaming_mod._MAX_OPEN_STREAMS
    streaming_mod._MAX_FINAL_ONLY_PENDING_CHATS = 2
    streaming_mod._MAX_OPEN_STREAMS = 1
    try:
        bounded_pending_chats = DummyAdapter()
        await bounded_pending_chats.send_draft(
            "user-pending-registry-blocker",
            3210,
            "blocker",
            {"reply_to_message_id": "msg-pending-registry-blocker"},
        )
        for offset in range(3):
            await bounded_pending_chats.send_draft(
                f"user-pending-registry-{offset}",
                3211 + offset,
                f"pending {offset}",
                {"reply_to_message_id": f"msg-pending-registry-{offset}"},
            )
        await bounded_pending_chats.abandon_open_draft(
            "user-pending-registry-blocker",
            "blocker",
            {"reply_to_message_id": "msg-pending-registry-blocker"},
        )

        before_oldest_pending = len(bounded_pending_chats.api_calls)
        await bounded_pending_chats.send_draft(
            "user-pending-registry-0",
            3211,
            "pending 0",
            {"reply_to_message_id": "msg-pending-registry-0"},
        )
        assert len(bounded_pending_chats.api_calls) == before_oldest_pending + 1

        before_newest_pending = len(bounded_pending_chats.api_calls)
        newest_pending = await bounded_pending_chats.send_draft(
            "user-pending-registry-2",
            3213,
            "pending 2",
            {"reply_to_message_id": "msg-pending-registry-2"},
        )
        assert newest_pending.success
        assert newest_pending.raw_response["qq_final_only_pending"] is True
        assert len(bounded_pending_chats.api_calls) == before_newest_pending
    finally:
        streaming_mod._MAX_OPEN_STREAMS = previous_capacity
        if pending_chat_limit_existed:
            streaming_mod._MAX_FINAL_ONLY_PENDING_CHATS = pending_chat_limit
        else:
            del streaming_mod._MAX_FINAL_ONLY_PENDING_CHATS

    # A capacity-degraded turn still has a lifecycle identity. Successful
    # abandonment must complete that identity so a delayed draft callback
    # cannot re-arm it after native capacity becomes available again.
    abandoned_pending = DummyAdapter()
    previous_capacity = streaming_mod._MAX_OPEN_STREAMS
    streaming_mod._MAX_OPEN_STREAMS = 1
    try:
        await abandoned_pending.send_draft(
            "user-pending-blocker",
            3204,
            "blocker",
            {"reply_to_message_id": "msg-pending-blocker"},
        )
        pending_draft = await abandoned_pending.send_draft(
            "user-pending-abandon",
            3205,
            "pending",
            {"reply_to_message_id": "msg-pending-abandon"},
        )
        assert pending_draft.success
        pending_abandon = await abandoned_pending.abandon_open_draft(
            "user-pending-abandon",
            "pending",
            {"reply_to_message_id": "msg-pending-abandon"},
        )
        assert pending_abandon.success
        await abandoned_pending.abandon_open_draft(
            "user-pending-blocker",
            "blocker",
            {"reply_to_message_id": "msg-pending-blocker"},
        )
        before_late_pending = len(abandoned_pending.api_calls)
        late_pending = await abandoned_pending.send_draft(
            "user-pending-abandon",
            3205,
            "pending",
            {"reply_to_message_id": "msg-pending-abandon"},
        )
        assert late_pending.success
        assert len(abandoned_pending.api_calls) == before_late_pending
        delivered_after_abandon = await abandoned_pending.send(
            "user-pending-abandon",
            "pending",
            reply_to="msg-pending-abandon",
            metadata={
                "notify": True,
                "reply_to_message_id": "msg-pending-abandon",
            },
        )
        assert delivered_after_abandon.success
        assert [item[1] for item in abandoned_pending.normal_sends] == [
            "pending"
        ]
        repeated_after_abandon = await abandoned_pending.send(
            "user-pending-abandon",
            "pending",
            reply_to="msg-pending-abandon",
            metadata={
                "notify": True,
                "reply_to_message_id": "msg-pending-abandon",
            },
        )
        assert repeated_after_abandon.success
        assert [item[1] for item in abandoned_pending.normal_sends] == [
            "pending"
        ]
    finally:
        streaming_mod._MAX_OPEN_STREAMS = previous_capacity

    # Concurrent turn-final callbacks share one keyed single-flight. A cancelled
    # capacity turn and a still-pending capacity turn must each expose exactly
    # one successful ordinary QQ message, while both callers complete.
    previous_capacity = streaming_mod._MAX_OPEN_STREAMS
    streaming_mod._MAX_OPEN_STREAMS = 1
    try:
        for abandoned, suffix in ((True, "cancelled"), (False, "pending")):
            concurrent_final = DummyAdapter()
            blocker_chat = f"user-concurrent-blocker-{suffix}"
            blocker_anchor = f"msg-concurrent-blocker-{suffix}"
            final_chat = f"user-concurrent-{suffix}"
            final_anchor = f"msg-concurrent-{suffix}"
            await concurrent_final.send_draft(
                blocker_chat,
                3250,
                "blocker",
                {"reply_to_message_id": blocker_anchor},
            )
            await concurrent_final.send_draft(
                final_chat,
                3251,
                "final",
                {"reply_to_message_id": final_anchor},
            )
            if abandoned:
                await concurrent_final.abandon_open_draft(
                    final_chat,
                    "final",
                    {"reply_to_message_id": final_anchor},
                )
            concurrent_final.normal_send_entered = anyio.Event()
            concurrent_final.normal_send_release = anyio.Event()
            results = {}

            async def send_concurrent_final(call_id):
                results[call_id] = await concurrent_final.send(
                    final_chat,
                    "final",
                    reply_to=final_anchor,
                    metadata={
                        "notify": True,
                        "reply_to_message_id": final_anchor,
                    },
                )

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(send_concurrent_final, "first")
                task_group.start_soon(send_concurrent_final, "second")
                task_group.start_soon(send_concurrent_final, "third")
                await concurrent_final.normal_send_entered.wait()
                await wait_for_final_claim_users(
                    concurrent_final,
                    final_chat,
                    final_anchor,
                    3,
                )
                concurrent_final.normal_send_release.set()

            assert results["first"].success
            assert results["second"].success
            assert results["third"].success
            assert concurrent_final.normal_send_peak == 1
            assert (
                concurrent_final._qq_native_c2c_final_delivery_broker
                .stats()
                .active
                == 0
            )
            assert [item[1] for item in concurrent_final.normal_sends] == [
                "final"
            ]
            repeated_concurrent_final = await concurrent_final.send(
                final_chat,
                "final",
                reply_to=final_anchor,
                metadata={
                    "notify": True,
                    "reply_to_message_id": final_anchor,
                },
            )
            assert repeated_concurrent_final.success
            assert [item[1] for item in concurrent_final.normal_sends] == [
                "final"
            ]
            await concurrent_final.abandon_open_draft(
                blocker_chat,
                "blocker",
                {"reply_to_message_id": blocker_anchor},
            )

        # Active-stream failure, unseen-suffix delivery and ownership
        # promotion are one broker transaction. Same-key final callbacks must
        # not reach the ordinary QQ boundary twice while the first is blocked.
        active_fallback = DummyAdapter()
        active_anchor = "msg-active-final-claim"
        await active_fallback.send_draft(
            "user-active-final-claim",
            3254,
            "progress",
            {"reply_to_message_id": active_anchor},
        )
        active_fallback.fail_next_stream = True
        active_fallback.normal_send_entered = anyio.Event()
        active_fallback.normal_send_release = anyio.Event()
        active_results = []
        completed_owner_limit = streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
        remember_tombstone = streaming_mod._remember_turn_tombstone

        def remember_then_evict(adapter, state, **kwargs):
            remember_tombstone(adapter, state, **kwargs)
            if adapter is active_fallback and state.reply_to == active_anchor:
                remember_tombstone(
                    adapter,
                    streaming_mod._QQC2CStream(
                        chat_id=state.chat_id,
                        draft_id=9999,
                        reply_to="independent-owner-eviction",
                        msg_seq=99,
                    ),
                    final_payload="independent",
                    final_content="independent",
                )

        async def send_active_fallback():
            active_results.append(
                await active_fallback.send(
                    "user-active-final-claim",
                    "FINAL",
                    reply_to=active_anchor,
                    metadata={
                        "notify": True,
                        "reply_to_message_id": active_anchor,
                    },
                )
            )

        streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = 1
        streaming_mod._remember_turn_tombstone = remember_then_evict
        try:
            async with anyio.create_task_group() as task_group:
                for _ in range(3):
                    task_group.start_soon(send_active_fallback)
                await active_fallback.normal_send_entered.wait()
                await wait_for_final_claim_users(
                    active_fallback,
                    "user-active-final-claim",
                    active_anchor,
                    3,
                )
                active_fallback.normal_send_release.set()
        finally:
            streaming_mod._remember_turn_tombstone = remember_tombstone
            streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = completed_owner_limit

        assert all(result.success for result in active_results)
        assert active_fallback.normal_send_peak == 1
        assert [item[1] for item in active_fallback.normal_sends] == [
            "\nFINAL"
        ]
        assert list(
            active_fallback._qq_native_c2c_completed_owners[
                "user-active-final-claim"
            ]
        ) == [("independent-owner-eviction", 9999)]
        assert (
            active_fallback._qq_native_c2c_final_delivery_broker.stats().active
            == 0
        )

        # Hermes cancellation cleanup can race a shielded final attempt. The
        # abandon path must join the same anchor transaction; otherwise it can
        # seal the complete native final while the already-started ordinary
        # unseen-suffix request is still blocked, creating two visible owners.
        abandon_race = DummyAdapter()
        abandon_race_anchor = "msg-abandon-final-race"
        abandon_race_target = "progress\nFINAL"
        await abandon_race.send_draft(
            "user-abandon-final-race",
            3255,
            "progress",
            {"reply_to_message_id": abandon_race_anchor},
        )
        abandon_race.fail_next_stream = True
        abandon_race.normal_send_entered = anyio.Event()
        abandon_race.normal_send_release = anyio.Event()
        abandon_race_results = {}

        async def send_abandon_race_final():
            abandon_race_results["final"] = await abandon_race.send(
                "user-abandon-final-race",
                "FINAL",
                reply_to=abandon_race_anchor,
                metadata={
                    "notify": True,
                    "reply_to_message_id": abandon_race_anchor,
                },
            )

        async def abandon_racing_final():
            abandon_race_results["abandon"] = (
                await abandon_race.abandon_open_draft(
                    "user-abandon-final-race",
                    abandon_race_target,
                    {"reply_to_message_id": abandon_race_anchor},
                )
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_abandon_race_final)
            await abandon_race.normal_send_entered.wait()
            calls_before_abandon = len(abandon_race.api_calls)
            task_group.start_soon(abandon_racing_final)
            await anyio.sleep(0)
            await anyio.sleep(0)
            calls_while_final_blocked = len(abandon_race.api_calls)
            abandon_race.normal_send_release.set()

        assert calls_while_final_blocked == calls_before_abandon
        assert abandon_race_results["final"].success
        assert abandon_race_results["abandon"].success
        assert [item[1] for item in abandon_race.normal_sends] == ["\nFINAL"]
        assert_exact_final_ownership(abandon_race, abandon_race_target)

        # The reverse order is also one lifecycle transaction. Cancellation
        # may seal the complete cumulative target before the short notify=True
        # callback enters the broker. Once that native seal succeeds, the
        # terminal payload already has a visible owner and must not be sent as
        # a second ordinary bubble.
        abandon_first = DummyAdapter()
        abandon_first_chat = "user-abandon-first"
        abandon_first_anchor = "msg-abandon-first"
        abandon_first_target = "progress\nFINAL"
        await abandon_first.send_draft(
            abandon_first_chat,
            3256,
            "progress",
            {"reply_to_message_id": abandon_first_anchor},
        )
        seal_entered = anyio.Event()
        seal_release = anyio.Event()
        base_api_request = abandon_first._api_request

        async def gated_seal_request(method, path, body):
            if (
                body["input_state"] == 10
                and body["content_raw"] == abandon_first_target
            ):
                seal_entered.set()
                await seal_release.wait()
            return await base_api_request(method, path, body)

        abandon_first._api_request = gated_seal_request
        abandon_first_results = {}
        completed_owner_limit = streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
        remember_tombstone = streaming_mod._remember_turn_tombstone

        def remember_abandon_then_evict(adapter, state, **kwargs):
            owner = remember_tombstone(adapter, state, **kwargs)
            if (
                adapter is abandon_first
                and state.reply_to == abandon_first_anchor
                and kwargs.get("final_delivered", True)
            ):
                remember_tombstone(
                    adapter,
                    streaming_mod._QQC2CStream(
                        chat_id=state.chat_id,
                        draft_id=9998,
                        reply_to="independent-abandon-eviction",
                        msg_seq=98,
                    ),
                    final_payload="independent",
                    final_content="independent",
                )
            return owner

        async def abandon_before_final():
            abandon_first_results["abandon"] = (
                await abandon_first.abandon_open_draft(
                    abandon_first_chat,
                    abandon_first_target,
                    {"reply_to_message_id": abandon_first_anchor},
                )
            )

        async def send_after_abandon_started():
            abandon_first_results["final"] = await abandon_first.send(
                abandon_first_chat,
                "FINAL",
                reply_to=abandon_first_anchor,
                metadata={
                    "notify": True,
                    "reply_to_message_id": abandon_first_anchor,
                },
            )

        async def send_late_after_abandon_started():
            abandon_first_results["late"] = await abandon_first.send_draft(
                abandon_first_chat,
                3259,
                "LATE",
                {"reply_to_message_id": abandon_first_anchor},
            )

        streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = 1
        streaming_mod._remember_turn_tombstone = remember_abandon_then_evict
        try:
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(abandon_before_final)
                await seal_entered.wait()
                task_group.start_soon(send_late_after_abandon_started)
                task_group.start_soon(send_after_abandon_started)
                await wait_for_final_claim_users(
                    abandon_first,
                    abandon_first_chat,
                    abandon_first_anchor,
                    3,
                )
                assert abandon_first.normal_send_attempts == []
                seal_release.set()
        finally:
            streaming_mod._remember_turn_tombstone = remember_tombstone
            streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = completed_owner_limit

        assert abandon_first_results["abandon"].success
        assert abandon_first_results["late"].success
        assert abandon_first_results["final"].success
        assert abandon_first.normal_sends == []
        assert_exact_final_ownership(abandon_first, abandon_first_target)
        assert (
            abandon_first._qq_native_c2c_final_delivery_broker.stats().active
            == 0
        )
        assert (
            abandon_first._qq_native_c2c_final_delivery_broker
            .transient_completion_for(
                (abandon_first_chat, abandon_first_anchor)
            )
            is None
        )

        async def assert_abandon_leading_boundary(
            *,
            label,
            payload,
            expected_owned,
        ):
            adapter = DummyAdapter()
            chat_id = f"user-abandon-leading-{label}"
            anchor = f"msg-abandon-leading-{label}"
            target = f"progress{payload}"
            await adapter.send_draft(
                chat_id,
                3258,
                "progress",
                {"reply_to_message_id": anchor},
            )
            closed = await adapter.abandon_open_draft(
                chat_id,
                target,
                {"reply_to_message_id": anchor},
            )
            final = await adapter.send(
                chat_id,
                payload,
                reply_to=anchor,
                metadata={
                    "notify": True,
                    "reply_to_message_id": anchor,
                },
            )
            assert closed.success
            assert final.success
            if expected_owned:
                assert adapter.normal_sends == []
                assert_exact_final_ownership(adapter, target)
            else:
                assert [item[1] for item in adapter.normal_sends] == [
                    payload
                ]

        for label, payload in (
            ("newline", "\nFINAL"),
            ("ascii-comma", ",FINAL"),
            ("chinese-comma", "，FINAL"),
        ):
            await assert_abandon_leading_boundary(
                label=label,
                payload=payload,
                expected_owned=True,
            )
        await assert_abandon_leading_boundary(
            label="connector",
            payload="_FINAL",
            expected_owned=False,
        )

        abandon_word_overlap = DummyAdapter()
        await abandon_word_overlap.send_draft(
            "user-abandon-word-overlap",
            3257,
            "progressNOTFINAL",
            {"reply_to_message_id": "msg-abandon-word-overlap"},
        )
        word_seal_entered = anyio.Event()
        word_seal_release = anyio.Event()
        word_base_api_request = abandon_word_overlap._api_request

        async def gated_word_seal_request(method, path, body):
            if body["input_state"] == 10:
                word_seal_entered.set()
                await word_seal_release.wait()
            return await word_base_api_request(method, path, body)

        abandon_word_overlap._api_request = gated_word_seal_request
        word_overlap_results = {}

        async def abandon_word_internal():
            word_overlap_results["abandon"] = (
                await abandon_word_overlap.abandon_open_draft(
                    "user-abandon-word-overlap",
                    "progressNOTFINAL",
                    {"reply_to_message_id": "msg-abandon-word-overlap"},
                )
            )

        async def send_word_internal_final():
            word_overlap_results["final"] = await abandon_word_overlap.send(
                "user-abandon-word-overlap",
                "FINAL",
                reply_to="msg-abandon-word-overlap",
                metadata={
                    "notify": True,
                    "reply_to_message_id": "msg-abandon-word-overlap",
                },
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(abandon_word_internal)
            await word_seal_entered.wait()
            task_group.start_soon(send_word_internal_final)
            await wait_for_final_claim_users(
                abandon_word_overlap,
                "user-abandon-word-overlap",
                "msg-abandon-word-overlap",
                2,
            )
            word_seal_release.set()

        assert word_overlap_results["abandon"].success
        assert word_overlap_results["final"].success
        assert [item[1] for item in abandon_word_overlap.normal_sends] == [
            "FINAL"
        ]

        # A late stream callback can arrive while the same-anchor final owns
        # an external ordinary send but has not yet published its tombstone or
        # broker completion. Both the original draft id and a changed draft id
        # must wait for that final flight, then observe completion instead of
        # opening/replacing another native carrier. A different anchor remains
        # independent and may progress while the first final is blocked.
        async def assert_late_frame_waits_for_final(
            *,
            label,
            late_draft_id,
            late_content,
            fallback_anchor=False,
        ):
            adapter = DummyAdapter()
            chat_id = f"user-late-frame-{label}"
            anchor = f"msg-late-frame-{label}"
            target = "progress\nFINAL"
            initial_draft_id = 3260
            await adapter.send_draft(
                chat_id,
                initial_draft_id,
                "progress",
                {"reply_to_message_id": anchor},
            )
            if fallback_anchor:
                adapter._last_msg_id[chat_id] = anchor
            adapter.fail_next_stream = True
            adapter.normal_send_entered = anyio.Event()
            adapter.normal_send_release = anyio.Event()
            results = {}

            async def send_final():
                results["final"] = await adapter.send(
                    chat_id,
                    "FINAL",
                    reply_to=anchor,
                    metadata={
                        "notify": True,
                        "reply_to_message_id": anchor,
                    },
                )

            async def send_late_frame():
                results["late"] = await adapter.send_draft(
                    chat_id,
                    late_draft_id,
                    late_content,
                    None if fallback_anchor else {
                        "reply_to_message_id": anchor
                    },
                )

            async with anyio.create_task_group() as task_group:
                task_group.start_soon(send_final)
                await adapter.normal_send_entered.wait()
                calls_before_late = len(adapter.api_calls)
                task_group.start_soon(send_late_frame)
                await wait_for_final_claim_users(
                    adapter,
                    chat_id,
                    anchor,
                    2,
                )
                assert len(adapter.api_calls) == calls_before_late
                if fallback_anchor:
                    adapter._last_msg_id[chat_id] = f"{anchor}-new-inbound"

                independent = await adapter.send_draft(
                    chat_id,
                    3261,
                    "independent",
                    {"reply_to_message_id": f"{anchor}-independent"},
                )
                assert independent.success
                # Capacity may route the independent anchor to final-only,
                # but the call must return before the blocked final releases.
                assert len(adapter.api_calls) == calls_before_late
                adapter.normal_send_release.set()

            assert results["final"].success
            assert results["late"].success
            assert [item[1] for item in adapter.normal_sends] == ["\nFINAL"]
            streams, anchors = streaming_mod._stream_maps(adapter)
            assert (chat_id, anchor) not in anchors
            assert (chat_id, late_draft_id) not in streams
            sealed_final = [
                call[2]["content_raw"]
                for call in adapter.successful_api_calls
                if call[2]["input_state"] == 10
            ]
            assert "".join(sealed_final + ["\nFINAL"]) == target

        await assert_late_frame_waits_for_final(
            label="same-draft",
            late_draft_id=3260,
            late_content="progress\nFINAL\nLATE",
        )
        await assert_late_frame_waits_for_final(
            label="changed-draft",
            late_draft_id=3262,
            late_content="LATE",
        )
        await assert_late_frame_waits_for_final(
            label="fallback-anchor",
            late_draft_id=3263,
            late_content="LATE",
            fallback_anchor=True,
        )

        # Missing reply identity is not a stable turn key. Two unanchored
        # finals in one private chat must remain two real deliveries; caching
        # `(chat_id, "")` would replay TURN-A and silently swallow TURN-B.
        unanchored_finals = DummyAdapter()
        first_unanchored = await unanchored_finals.send(
            "user-unanchored-finals",
            "TURN-A",
            metadata={"notify": True},
        )
        second_unanchored = await unanchored_finals.send(
            "user-unanchored-finals",
            "TURN-B",
            metadata={"notify": True},
        )
        assert first_unanchored.success
        assert second_unanchored.success
        assert [item[1] for item in unanchored_finals.normal_sends] == [
            "TURN-A",
            "TURN-B",
        ]

        unanchored_parallel = DummyAdapter()
        unanchored_parallel.normal_send_concurrent_entered = anyio.Event()
        unanchored_parallel.normal_send_release = anyio.Event()
        unanchored_parallel_results = []

        async def send_unanchored_parallel(content):
            unanchored_parallel_results.append(
                await unanchored_parallel.send(
                    "user-unanchored-parallel",
                    content,
                    metadata={"notify": True},
                )
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_unanchored_parallel, "PARALLEL-A")
            task_group.start_soon(send_unanchored_parallel, "PARALLEL-B")
            with anyio.fail_after(1):
                await unanchored_parallel.normal_send_concurrent_entered.wait()
            unanchored_parallel.normal_send_release.set()

        assert all(result.success for result in unanchored_parallel_results)
        assert sorted(
            item[1] for item in unanchored_parallel.normal_sends
        ) == ["PARALLEL-A", "PARALLEL-B"]

        failed_claim = DummyAdapter()
        await failed_claim.send_draft(
            "user-failed-claim-blocker",
            3252,
            "blocker",
            {"reply_to_message_id": "msg-failed-claim-blocker"},
        )
        await failed_claim.send_draft(
            "user-failed-claim",
            3253,
            "final",
            {"reply_to_message_id": "msg-failed-claim"},
        )
        await failed_claim.abandon_open_draft(
            "user-failed-claim",
            "final",
            {"reply_to_message_id": "msg-failed-claim"},
        )
        failed_claim.fail_normal_attempts = 1
        failed_claim.normal_send_entered = anyio.Event()
        failed_claim.normal_send_release = anyio.Event()
        failed_results = {}

        async def send_failed_claim(call_id):
            failed_results[call_id] = await failed_claim.send(
                "user-failed-claim",
                "final",
                reply_to="msg-failed-claim",
                metadata={
                    "notify": True,
                    "reply_to_message_id": "msg-failed-claim",
                },
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_failed_claim, "first")
            task_group.start_soon(send_failed_claim, "second")
            await failed_claim.normal_send_entered.wait()
            await wait_for_final_claim_users(
                failed_claim,
                "user-failed-claim",
                "msg-failed-claim",
                2,
            )
            failed_claim.normal_send_release.set()

        assert sorted(result.success for result in failed_results.values()) == [
            False,
            True,
        ]
        assert failed_claim.normal_send_peak == 1
        assert (
            failed_claim._qq_native_c2c_final_delivery_broker.stats().active
            == 0
        )
        assert [item[1] for item in failed_claim.normal_sends] == ["final"]
        replay_after_retry = await failed_claim.send(
            "user-failed-claim",
            "final",
            reply_to="msg-failed-claim",
            metadata={
                "notify": True,
                "reply_to_message_id": "msg-failed-claim",
            },
        )
        assert replay_after_retry.success
        assert [item[1] for item in failed_claim.normal_sends] == ["final"]

        independent_claims = DummyAdapter()
        await independent_claims.send_draft(
            "user-independent-claim-blocker",
            3260,
            "blocker",
            {"reply_to_message_id": "msg-independent-claim-blocker"},
        )
        for draft_id, anchor in (
            (3261, "msg-independent-claim-a"),
            (3262, "msg-independent-claim-b"),
        ):
            await independent_claims.send_draft(
                "user-independent-claim",
                draft_id,
                f"final-{draft_id}",
                {"reply_to_message_id": anchor},
            )
        independent_claims.normal_send_concurrent_entered = anyio.Event()
        independent_claims.normal_send_release = anyio.Event()
        independent_results = {}

        async def send_independent_claim(call_id, anchor):
            independent_results[call_id] = await independent_claims.send(
                "user-independent-claim",
                call_id,
                reply_to=anchor,
                metadata={
                    "notify": True,
                    "reply_to_message_id": anchor,
                },
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(
                send_independent_claim,
                "final-3261",
                "msg-independent-claim-a",
            )
            task_group.start_soon(
                send_independent_claim,
                "final-3262",
                "msg-independent-claim-b",
            )
            await independent_claims.normal_send_concurrent_entered.wait()
            independent_claims.normal_send_release.set()

        assert all(result.success for result in independent_results.values())
        assert independent_claims.normal_send_peak == 2
        assert (
            independent_claims._qq_native_c2c_final_delivery_broker
            .stats()
            .active
            == 0
        )
        assert sorted(item[1] for item in independent_claims.normal_sends) == [
            "final-3261",
            "final-3262",
        ]
        await independent_claims.abandon_open_draft(
            "user-independent-claim-blocker",
            "blocker",
            {"reply_to_message_id": "msg-independent-claim-blocker"},
        )

        cancelled_waiter = DummyAdapter()
        await cancelled_waiter.send_draft(
            "user-cancelled-waiter-blocker",
            3270,
            "blocker",
            {"reply_to_message_id": "msg-cancelled-waiter-blocker"},
        )
        await cancelled_waiter.send_draft(
            "user-cancelled-waiter",
            3271,
            "final",
            {"reply_to_message_id": "msg-cancelled-waiter"},
        )
        await cancelled_waiter.abandon_open_draft(
            "user-cancelled-waiter",
            "final",
            {"reply_to_message_id": "msg-cancelled-waiter"},
        )
        cancelled_waiter.normal_send_entered = anyio.Event()
        cancelled_waiter.normal_send_release = anyio.Event()
        waiter_scope_ready = anyio.Event()
        waiter_cancelled = anyio.Event()
        waiter_scope_holder = {}
        leader_results = []

        async def send_claim_leader():
            leader_results.append(
                await cancelled_waiter.send(
                    "user-cancelled-waiter",
                    "final",
                    reply_to="msg-cancelled-waiter",
                    metadata={
                        "notify": True,
                        "reply_to_message_id": "msg-cancelled-waiter",
                    },
                )
            )

        async def send_then_cancel_waiter():
            with anyio.CancelScope() as cancel_scope:
                waiter_scope_holder["scope"] = cancel_scope
                waiter_scope_ready.set()
                await cancelled_waiter.send(
                    "user-cancelled-waiter",
                    "final",
                    reply_to="msg-cancelled-waiter",
                    metadata={
                        "notify": True,
                        "reply_to_message_id": "msg-cancelled-waiter",
                    },
                )
            waiter_cancelled.set()

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_claim_leader)
            await cancelled_waiter.normal_send_entered.wait()
            task_group.start_soon(send_then_cancel_waiter)
            await waiter_scope_ready.wait()
            await wait_for_final_claim_users(
                cancelled_waiter,
                "user-cancelled-waiter",
                "msg-cancelled-waiter",
                2,
            )
            waiter_scope_holder["scope"].cancel()
            await waiter_cancelled.wait()
            cancelled_waiter.normal_send_release.set()

        assert leader_results[0].success
        assert [item[1] for item in cancelled_waiter.normal_sends] == ["final"]
        assert (
            cancelled_waiter._qq_native_c2c_final_delivery_broker
            .stats()
            .active
            == 0
        )
        replay_after_waiter_cancel = await cancelled_waiter.send(
            "user-cancelled-waiter",
            "final",
            reply_to="msg-cancelled-waiter",
            metadata={
                "notify": True,
                "reply_to_message_id": "msg-cancelled-waiter",
            },
        )
        assert replay_after_waiter_cancel.success
        assert [item[1] for item in cancelled_waiter.normal_sends] == ["final"]
        await cancelled_waiter.abandon_open_draft(
            "user-cancelled-waiter-blocker",
            "blocker",
            {"reply_to_message_id": "msg-cancelled-waiter-blocker"},
        )

        cancelled_holder = DummyAdapter()
        await cancelled_holder.send_draft(
            "user-cancelled-holder-blocker",
            3272,
            "blocker",
            {"reply_to_message_id": "msg-cancelled-holder-blocker"},
        )
        await cancelled_holder.send_draft(
            "user-cancelled-holder",
            3273,
            "final",
            {"reply_to_message_id": "msg-cancelled-holder"},
        )
        await cancelled_holder.abandon_open_draft(
            "user-cancelled-holder",
            "final",
            {"reply_to_message_id": "msg-cancelled-holder"},
        )
        cancelled_holder.normal_send_entered = anyio.Event()
        cancelled_holder.normal_send_release = anyio.Event()
        holder_scope_ready = anyio.Event()
        holder_cancelled = anyio.Event()
        holder_scope_holder = {}
        holder_waiter_results = []

        async def send_then_cancel_holder():
            with anyio.CancelScope() as cancel_scope:
                holder_scope_holder["scope"] = cancel_scope
                holder_scope_ready.set()
                await cancelled_holder.send(
                    "user-cancelled-holder",
                    "final",
                    reply_to="msg-cancelled-holder",
                    metadata={
                        "notify": True,
                        "reply_to_message_id": "msg-cancelled-holder",
                    },
                )
            holder_cancelled.set()

        async def send_after_holder_cancel():
            holder_waiter_results.append(
                await cancelled_holder.send(
                    "user-cancelled-holder",
                    "final",
                    reply_to="msg-cancelled-holder",
                    metadata={
                        "notify": True,
                        "reply_to_message_id": "msg-cancelled-holder",
                    },
                )
            )

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_then_cancel_holder)
            await holder_scope_ready.wait()
            await cancelled_holder.normal_send_entered.wait()
            task_group.start_soon(send_after_holder_cancel)
            await wait_for_final_claim_users(
                cancelled_holder,
                "user-cancelled-holder",
                "msg-cancelled-holder",
                2,
            )
            holder_scope_holder["scope"].cancel()
            await holder_cancelled.wait()
            cancelled_holder.normal_send_release.set()

        assert holder_waiter_results[0].success
        assert cancelled_holder.normal_send_peak == 1
        assert len(cancelled_holder.normal_send_attempts) == 1
        assert [item[1] for item in cancelled_holder.normal_sends] == ["final"]
        assert (
            cancelled_holder._qq_native_c2c_final_delivery_broker
            .stats()
            .active
            == 0
        )
        await cancelled_holder.abandon_open_draft(
            "user-cancelled-holder-blocker",
            "blocker",
            {"reply_to_message_id": "msg-cancelled-holder-blocker"},
        )

        raised_claim = DummyAdapter()
        await raised_claim.send_draft(
            "user-raised-claim-blocker",
            3274,
            "blocker",
            {"reply_to_message_id": "msg-raised-claim-blocker"},
        )
        await raised_claim.send_draft(
            "user-raised-claim",
            3275,
            "final",
            {"reply_to_message_id": "msg-raised-claim"},
        )
        await raised_claim.abandon_open_draft(
            "user-raised-claim",
            "final",
            {"reply_to_message_id": "msg-raised-claim"},
        )
        raised_claim.raise_normal_attempts = 1
        raised_claim.normal_send_entered = anyio.Event()
        raised_claim.normal_send_release = anyio.Event()
        raised_results = {}

        async def send_raised_claim(call_id):
            try:
                raised_results[call_id] = await raised_claim.send(
                    "user-raised-claim",
                    "final",
                    reply_to="msg-raised-claim",
                    metadata={
                        "notify": True,
                        "reply_to_message_id": "msg-raised-claim",
                    },
                )
            except RuntimeError as exc:
                raised_results[call_id] = str(exc)

        async with anyio.create_task_group() as task_group:
            task_group.start_soon(send_raised_claim, "first")
            task_group.start_soon(send_raised_claim, "second")
            await raised_claim.normal_send_entered.wait()
            await wait_for_final_claim_users(
                raised_claim,
                "user-raised-claim",
                "msg-raised-claim",
                2,
            )
            raised_claim.normal_send_release.set()

        assert sorted(
            "success" if getattr(result, "success", False) else result
            for result in raised_results.values()
        ) == ["normal send raised", "success"]
        assert raised_claim.normal_send_peak == 1
        assert [item[1] for item in raised_claim.normal_sends] == ["final"]
        assert (
            raised_claim._qq_native_c2c_final_delivery_broker.stats().active
            == 0
        )
        replay_after_raise = await raised_claim.send(
            "user-raised-claim",
            "final",
            reply_to="msg-raised-claim",
            metadata={
                "notify": True,
                "reply_to_message_id": "msg-raised-claim",
            },
        )
        assert replay_after_raise.success
        assert [item[1] for item in raised_claim.normal_sends] == ["final"]
        await raised_claim.abandon_open_draft(
            "user-raised-claim-blocker",
            "blocker",
            {"reply_to_message_id": "msg-raised-claim-blocker"},
        )
    finally:
        streaming_mod._MAX_OPEN_STREAMS = previous_capacity

    # With streaming disabled/no native lane, preserve upstream periodic
    # typing behavior exactly.
    disabled_typing = DummyAdapter()
    disabled_typing._last_msg_id["user-disabled"] = "typing-disabled"
    await disabled_typing.send_typing("user-disabled")
    await disabled_typing.send_typing("user-disabled")
    await disabled_typing.send_typing("user-disabled")
    assert len(disabled_typing.typing_calls) == 3

    # An active native lane is bounded to one passive input_notify per inbound
    # msg_id so the final retains its passive-reply budget.
    typing = DummyAdapter()
    typing._last_msg_id["user-t"] = "typing-1"
    await typing.send_draft(
        "user-t", 3301, "处理中", {"reply_to_message_id": "typing-1"}
    )
    await typing.send_typing("user-t")
    await typing.send_typing("user-t")
    await typing.send_typing("user-t")
    assert len(typing.typing_calls) == 1
    typing._last_msg_id["user-t"] = "typing-2"
    await typing.send_typing("user-t")
    assert len(typing.typing_calls) == 2

    # Hermes' in-process runner normally rejects non-editable adapters before
    # the consumer can probe native draft support. The hotfix bypasses that
    # legacy gate only for QQ C2C; group chats retain the rejection.
    gate_adapter = GatewayDummyAdapter()
    runner = object.__new__(GatewayRunner)
    scfg = StreamingConfig(enabled=True, transport="auto")
    original_config_loader = gateway_run._load_gateway_config
    try:
        gateway_run._load_gateway_config = lambda: {
            "display": {
                "platforms": {
                    "qqbot": {
                        "streaming": True,
                        "interim_assistant_messages": True,
                    }
                }
            }
        }
        c2c_cfg, _pause = runner._build_stream_consumer_config(
            SimpleNamespace(
                platform=Platform.QQBOT,
                chat_id="user-gate",
                chat_type="dm",
            ),
            scfg,
            gate_adapter,
            on_missing_cursor="raise",
        )
        assert c2c_cfg.transport == "auto"
        assert c2c_cfg.cursor == ""
        assert gate_adapter.supports_draft_streaming(
            chat_type="dm", chat_id="user-gate"
        )
        try:
            runner._build_stream_consumer_config(
                SimpleNamespace(
                    platform=Platform.QQBOT,
                    chat_id="group-gate",
                    chat_type="group",
                ),
                scfg,
                gate_adapter,
                on_missing_cursor="raise",
            )
        except RuntimeError as exc:
            assert "non-editable platform" in str(exc)
        else:
            raise AssertionError("QQ group unexpectedly bypassed edit-only gate")

        # QQ guild direct messages also use source.chat_type="dm", but the
        # adapter's authoritative route is "dm", not "c2c".
        try:
            runner._build_stream_consumer_config(
                SimpleNamespace(
                    platform=Platform.QQBOT,
                    chat_id="guild-dm-gate",
                    chat_type="dm",
                ),
                scfg,
                gate_adapter,
                on_missing_cursor="raise",
            )
        except RuntimeError as exc:
            assert "non-editable platform" in str(exc)
        else:
            raise AssertionError("QQ guild DM unexpectedly entered C2C lane")
        assert "guild-dm-gate" not in streaming_mod._native_lane_chats(
            gate_adapter
        )

        # Real in-process Runner combination: interim messages alone can cause
        # this builder call while both global and QQ streaming are false. The
        # native lane must remain disabled and upstream's non-editable gate
        # must reject the consumer.
        gateway_run._load_gateway_config = lambda: {
            "display": {
                "interim_assistant_messages": True,
                "platforms": {
                    "qqbot": {
                        "streaming": False,
                        "interim_assistant_messages": True,
                    }
                },
            }
        }
        disabled_gate_adapter = GatewayDummyAdapter()
        disabled_scfg = StreamingConfig(enabled=False, transport="auto")
        try:
            runner._build_stream_consumer_config(
                SimpleNamespace(
                    platform=Platform.QQBOT,
                    chat_id="user-interim-only",
                    chat_type="dm",
                ),
                disabled_scfg,
                disabled_gate_adapter,
                on_missing_cursor="raise",
            )
        except RuntimeError as exc:
            assert "non-editable platform" in str(exc)
        else:
            raise AssertionError("interim-only QQ unexpectedly opened native lane")
        assert not streaming_mod._native_lane_chats(disabled_gate_adapter)
        assert not disabled_gate_adapter.supports_draft_streaming(
            chat_type="dm", chat_id="user-interim-only"
        )

        # A platform-level opt-out also wins when top-level streaming remains
        # enabled. This exercises the complete resolved-setting precedence.
        try:
            runner._build_stream_consumer_config(
                SimpleNamespace(
                    platform=Platform.QQBOT,
                    chat_id="user-platform-opt-out",
                    chat_type="dm",
                ),
                StreamingConfig(enabled=True, transport="auto"),
                disabled_gate_adapter,
                on_missing_cursor="raise",
            )
        except RuntimeError as exc:
            assert "non-editable platform" in str(exc)
        else:
            raise AssertionError("QQ platform streaming opt-out was ignored")
        assert "user-platform-opt-out" not in streaming_mod._native_lane_chats(
            disabled_gate_adapter
        )

        # A live enabled -> disabled transition must revoke a lane selected
        # for an earlier turn on the same adapter.
        gateway_run._load_gateway_config = lambda: {
            "display": {"platforms": {"qqbot": {"streaming": True}}}
        }
        toggle_adapter = GatewayDummyAdapter()
        toggle_source = SimpleNamespace(
            platform=Platform.QQBOT,
            chat_id="toggle-user",
            chat_type="dm",
        )
        runner._build_stream_consumer_config(
            toggle_source,
            StreamingConfig(enabled=True, transport="auto"),
            toggle_adapter,
            on_missing_cursor="raise",
        )
        assert toggle_adapter.supports_draft_streaming(
            chat_type="dm", chat_id="toggle-user"
        )
        gateway_run._load_gateway_config = lambda: {
            "display": {"platforms": {"qqbot": {"streaming": False}}}
        }
        try:
            runner._build_stream_consumer_config(
                toggle_source,
                StreamingConfig(enabled=True, transport="auto"),
                toggle_adapter,
                on_missing_cursor="raise",
            )
        except RuntimeError as exc:
            assert "non-editable platform" in str(exc)
        else:
            raise AssertionError("disabled QQ lane unexpectedly stayed active")
        assert "toggle-user" not in streaming_mod._native_lane_chats(toggle_adapter)
        assert not toggle_adapter.supports_draft_streaming(
            chat_type="dm", chat_id="toggle-user"
        )
        toggle_adapter._last_msg_id["toggle-user"] = "toggle-typing"
        await toggle_adapter.send_typing("toggle-user")
        await toggle_adapter.send_typing("toggle-user")
        assert len(toggle_adapter.typing_calls) == 2

        # Revoking the lane must not discard an already-visible stream. Its
        # map entry keeps the passive-reply budget protected until close.
        gateway_run._load_gateway_config = lambda: {
            "display": {"platforms": {"qqbot": {"streaming": True}}}
        }
        open_toggle_adapter = GatewayDummyAdapter()
        open_toggle_source = SimpleNamespace(
            platform=Platform.QQBOT,
            chat_id="toggle-open-user",
            chat_type="dm",
        )
        runner._build_stream_consumer_config(
            open_toggle_source,
            StreamingConfig(enabled=True, transport="auto"),
            open_toggle_adapter,
            on_missing_cursor="raise",
        )
        await open_toggle_adapter.send_draft(
            "toggle-open-user",
            3401,
            "处理中",
            {"reply_to_message_id": "toggle-open-msg"},
        )
        gateway_run._load_gateway_config = lambda: {
            "display": {"platforms": {"qqbot": {"streaming": False}}}
        }
        try:
            runner._build_stream_consumer_config(
                open_toggle_source,
                StreamingConfig(enabled=True, transport="auto"),
                open_toggle_adapter,
                on_missing_cursor="raise",
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("open stream config disable bypassed gate")
        assert "toggle-open-user" not in streaming_mod._native_lane_chats(
            open_toggle_adapter
        )
        assert not open_toggle_adapter.supports_draft_streaming(
            chat_type="dm", chat_id="toggle-open-user"
        )
        assert streaming_mod._typing_budget_applies(
            open_toggle_adapter, "toggle-open-user"
        )
        closed_toggle = await open_toggle_adapter.abandon_open_draft(
            "toggle-open-user",
            "处理中",
            {"reply_to_message_id": "toggle-open-msg"},
        )
        assert closed_toggle.success

        # Lane selection is an LRU registry, not an adapter-lifetime history.
        # It may retain a currently open stream past the nominal bound, but
        # must evict inactive chats and converge after that stream closes.
        previous_lane_limit = getattr(
            streaming_mod,
            "_MAX_NATIVE_LANE_CHATS",
            1024,
        )
        streaming_mod._MAX_NATIVE_LANE_CHATS = 2
        try:
            gateway_run._load_gateway_config = lambda: {
                "display": {"platforms": {"qqbot": {"streaming": True}}}
            }
            lane_adapter = GatewayDummyAdapter()
            for lane_chat in ("lane-active", "lane-idle-1", "lane-idle-2"):
                runner._build_stream_consumer_config(
                    SimpleNamespace(
                        platform=Platform.QQBOT,
                        chat_id=lane_chat,
                        chat_type="dm",
                    ),
                    StreamingConfig(enabled=True, transport="auto"),
                    lane_adapter,
                    on_missing_cursor="raise",
                )
                if lane_chat == "lane-active":
                    opened_lane = await lane_adapter.send_draft(
                        lane_chat,
                        3451,
                        "处理中",
                        {"reply_to_message_id": "lane-active-msg"},
                    )
                    assert opened_lane.success
            lane_chats = streaming_mod._native_lane_chats(lane_adapter)
            assert len(lane_chats) == 2
            assert "lane-active" in lane_chats
            assert "lane-idle-2" in lane_chats
            assert "lane-idle-1" not in lane_chats

            closed_lane = await lane_adapter.abandon_open_draft(
                "lane-active",
                "处理中",
                {"reply_to_message_id": "lane-active-msg"},
            )
            assert closed_lane.success
            runner._build_stream_consumer_config(
                SimpleNamespace(
                    platform=Platform.QQBOT,
                    chat_id="lane-idle-3",
                    chat_type="dm",
                ),
                StreamingConfig(enabled=True, transport="auto"),
                lane_adapter,
                on_missing_cursor="raise",
            )
            lane_chats = streaming_mod._native_lane_chats(lane_adapter)
            assert len(lane_chats) == 2
            assert "lane-active" not in lane_chats
            assert "lane-idle-2" in lane_chats
            assert "lane-idle-3" in lane_chats
        finally:
            streaming_mod._MAX_NATIVE_LANE_CHATS = previous_lane_limit

        disabled_gate_adapter._last_msg_id["user-interim-only"] = "typing-off"
        await disabled_gate_adapter.send_typing("user-interim-only")
        await disabled_gate_adapter.send_typing("user-interim-only")
        assert len(disabled_gate_adapter.typing_calls) == 2
    finally:
        gateway_run._load_gateway_config = original_config_loader

    # Exercise the actual Hermes native-draft consumer contract. The QQ
    # stream itself is the message: cumulative frames stay on one stream and
    # the consumer's notify=True final send becomes exactly one seal frame,
    # never a second ordinary QQ message.
    integrated = GatewayDummyAdapter()
    streaming_mod._mark_native_lane(integrated, "user-integrated")
    cfg = StreamConsumerConfig(
        transport="auto",
        chat_type="dm",
        edit_interval=0.01,
        buffer_threshold=1,
        cursor="",
    )
    consumer = GatewayStreamConsumer(
        integrated,
        "user-integrated",
        cfg,
        initial_reply_to_id="inbound-integrated",
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(consumer.run)
        consumer.on_delta("阶段一")
        await anyio.sleep(0.05)
        consumer.on_segment_break()
        await anyio.sleep(0.05)
        consumer.on_delta("，阶段二")
        await anyio.sleep(0.05)
        consumer.finish("阶段一，阶段二，完成")

    stream_bodies = [call[2] for call in integrated.api_calls]
    assert len(stream_bodies) >= 2
    assert [body["input_state"] for body in stream_bodies].count(10) == 1
    assert stream_bodies[-1]["content_raw"] == "阶段一，阶段二，完成"
    assert not integrated.normal_sends
    assert consumer.final_response_sent is True
    assert consumer.delivered_final_matches("阶段一，阶段二，完成") is True

    # Codex app-server emits live deltas for a commentary item and then emits
    # the completed phase=commentary item through Hermes' interim callback.
    # Hermes marks that second carrier as ``_interim_send`` even though the
    # exact text is already visible in the native QQ stream. The connector
    # must keep the stream as the sole message owner instead of posting an
    # identical ordinary QQ bubble beside it.
    commentary_duplicate = GatewayDummyAdapter()
    streaming_mod._mark_native_lane(
        commentary_duplicate,
        "user-commentary-duplicate",
    )
    commentary_consumer = GatewayStreamConsumer(
        commentary_duplicate,
        "user-commentary-duplicate",
        cfg,
        initial_reply_to_id="inbound-commentary-duplicate",
    )
    commentary_text = "处理即将完成\nPR187_TERMINAL_ONCE"
    async with anyio.create_task_group() as tg:
        tg.start_soon(commentary_consumer.run)
        commentary_consumer.on_delta(commentary_text)
        await anyio.sleep(0.05)
        commentary_consumer.on_commentary(commentary_text)
        await anyio.sleep(0.05)
        commentary_consumer.finish("PR187_TERMINAL_ONCE")

    commentary_bodies = [call[2] for call in commentary_duplicate.api_calls]
    assert [body["input_state"] for body in commentary_bodies].count(10) == 1
    assert commentary_bodies[-1]["content_raw"] == commentary_text
    assert not commentary_duplicate.normal_sends, (
        commentary_bodies,
        commentary_duplicate.normal_sends,
    )
    assert commentary_consumer.final_response_sent is True
    assert commentary_consumer.delivered_final_matches(
        "PR187_TERMINAL_ONCE"
    ) is True

    # Consecutive Codex commentary items can be concatenated by the consumer
    # without a textual token boundary. The completed-item callback is still
    # the same consumer-owned segment and must not create one ordinary bubble
    # per minute beside the growing native carrier.
    commentary_sequence = GatewayDummyAdapter()
    streaming_mod._mark_native_lane(
        commentary_sequence,
        "user-commentary-sequence",
    )
    commentary_sequence_consumer = GatewayStreamConsumer(
        commentary_sequence,
        "user-commentary-sequence",
        cfg,
        initial_reply_to_id="inbound-commentary-sequence",
    )
    commentary_sequence_final = "QQ_AGE_FINAL"
    async with anyio.create_task_group() as tg:
        tg.start_soon(commentary_sequence_consumer.run)
        commentary_sequence_consumer.on_delta("QQ_AGE_START")
        await anyio.sleep(0.05)
        commentary_sequence_consumer.on_commentary("QQ_AGE_START")
        await anyio.sleep(0.05)
        commentary_sequence_consumer.on_delta("QQ_AGE_STEP_1")
        await anyio.sleep(0.05)
        commentary_sequence_consumer.on_commentary("QQ_AGE_STEP_1")
        await anyio.sleep(0.05)
        # Codex streams the final answer as agentMessage deltas before Hermes
        # supplies the same authoritative final_response to finish(). There is
        # no guaranteed whitespace between the last commentary and final.
        commentary_sequence_consumer.on_delta(commentary_sequence_final)
        await anyio.sleep(0.05)
        commentary_sequence_consumer.finish(commentary_sequence_final)

    assert not commentary_sequence.normal_sends, (
        commentary_sequence.api_calls,
        commentary_sequence.normal_sends,
    )
    assert_exact_final_ownership(
        commentary_sequence,
        "QQ_AGE_STARTQQ_AGE_STEP_1" + commentary_sequence_final,
    )

    # An authoritative final callback is not proof of delta ownership. This
    # uses the real consumer (not direct adapter.send) so the final context is
    # active even though FINAL was never streamed as its own segment.
    for completed_commentary in (False, True):
        independent_final = GatewayDummyAdapter()
        streaming_mod._mark_native_lane(independent_final, "user-independent-final")
        independent_consumer = GatewayStreamConsumer(
            independent_final,
            "user-independent-final",
            cfg,
            initial_reply_to_id="inbound-independent-final",
        )
        async with anyio.create_task_group() as tg:
            tg.start_soon(independent_consumer.run)
            independent_consumer.on_delta("status NOTFINAL")
            await anyio.sleep(0.05)
            if completed_commentary:
                independent_consumer.on_commentary("status NOTFINAL")
                await anyio.sleep(0.05)
            independent_consumer.finish("FINAL")
        assert_exact_final_ownership(independent_final, "status NOTFINAL\nFINAL")
        assert not independent_final.normal_sends
        assert independent_consumer.final_response_sent is True
        assert independent_consumer.delivered_final_matches("FINAL") is True

    # finish() may drain the final tail and adopt the authoritative payload in
    # the same tick, before the last cumulative draft reaches QQ. Provenance
    # must preserve the visible prefix and add only the undisplayed tail.
    partial_final = GatewayDummyAdapter()
    streaming_mod._mark_native_lane(partial_final, "user-partial-final")
    partial_consumer = GatewayStreamConsumer(
        partial_final, "user-partial-final", cfg,
        initial_reply_to_id="inbound-partial-final",
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(partial_consumer.run)
        partial_consumer.on_delta("PROGRESS")
        partial_consumer.on_commentary("PROGRESS")
        await anyio.sleep(0.05)
        partial_consumer.on_delta("FI")
        await anyio.sleep(0.05)
        partial_consumer.on_delta("NAL")
        partial_consumer.finish("FINAL")
    assert_exact_final_ownership(partial_final, "PROGRESSFINAL")
    assert not partial_final.normal_sends

    # Hermes may append a verifier/footer after streaming the answer. The
    # explicit final segment, not a suffix guess, lets that extension keep the
    # streamed portion exactly once.
    augmented_final = GatewayDummyAdapter()
    streaming_mod._mark_native_lane(augmented_final, "user-augmented-final")
    augmented_consumer = GatewayStreamConsumer(
        augmented_final, "user-augmented-final", cfg,
        initial_reply_to_id="inbound-augmented-final",
    )
    async with anyio.create_task_group() as tg:
        tg.start_soon(augmented_consumer.run)
        augmented_consumer.on_delta("PROGRESS")
        augmented_consumer.on_commentary("PROGRESS")
        await anyio.sleep(0.05)
        augmented_consumer.on_delta("FINAL")
        await anyio.sleep(0.05)
        augmented_consumer.finish("FINAL\nverified")
    assert_exact_final_ownership(augmented_final, "PROGRESSFINAL\nverified")

    # Boundary resets, filtered deltas, and an authoritative rewrite must not
    # leak/invent provenance. Drive only public consumer callbacks; the QQ API
    # sink below is the external visibility boundary.
    provenance_cases = (
        ("tool-break", "status NOTFINAL", "break", (), "FINAL", "status NOTFINAL\nFINAL"),
        ("filtered", "PROGRESS\n", "commentary", ("<think>hidden</think>FI", "NAL"),
         "FINAL", "PROGRESS\nFINAL"),
        ("rewritten", "PROGRESS", "commentary", ("DRAFT",),
         "FINAL", "PROGRESSDRAFT\nFINAL"),
        ("no-commentary", "", "none", ("FI", "NAL"), "FINAL", "FINAL"),
    )
    for label, progress, boundary, deltas, final_text, expected in provenance_cases:
        case_adapter = GatewayDummyAdapter()
        chat = "user-provenance-" + label
        streaming_mod._mark_native_lane(case_adapter, chat)
        case_consumer = GatewayStreamConsumer(
            case_adapter, chat, cfg, initial_reply_to_id="inbound-" + label,
        )
        async with anyio.create_task_group() as tg:
            tg.start_soon(case_consumer.run)
            if progress:
                case_consumer.on_delta(progress)
                await anyio.sleep(0.05)
            if boundary == "commentary":
                case_consumer.on_commentary(progress)
            elif boundary == "break":
                case_consumer.on_segment_break()
            await anyio.sleep(0.05)
            for delta in deltas:
                case_consumer.on_delta(delta)
                await anyio.sleep(0.05)
            case_consumer.finish(final_text)
        assert_exact_final_ownership(case_adapter, expected)
        assert not case_adapter.normal_sends
        assert case_consumer.final_response_sent is True

    # Combine the live failure's boundaries: an age rollover, consecutive
    # commentary without whitespace, a >9,000-character final streamed as
    # deltas, and the same authoritative final passed to finish(). The final
    # must remain one logical owner and fit inside four QQ carriers rather
    # than being appended a second time until the passive-reply budget fails.
    long_final_boundary = GatewayDummyAdapter()
    long_final_boundary._qq_native_stream_max_age_seconds = 480.0
    streaming_mod._mark_native_lane(
        long_final_boundary,
        "user-long-final-boundary",
    )
    long_final_consumer = GatewayStreamConsumer(
        long_final_boundary,
        "user-long-final-boundary",
        cfg,
        initial_reply_to_id="inbound-long-final-boundary",
    )
    long_final_text = "QQ_LONG_FINAL_BEGIN" + ("X" * 9200) + "QQ_LONG_FINAL_OK"
    async with anyio.create_task_group() as tg:
        tg.start_soon(long_final_consumer.run)
        long_final_consumer.on_delta("QQ_LONG_PROGRESS_1")
        await anyio.sleep(0.05)
        long_final_consumer.on_commentary("QQ_LONG_PROGRESS_1")
        await anyio.sleep(0.05)
        long_final_boundary.native_stream_now = 481.0
        long_final_consumer.on_delta("QQ_LONG_PROGRESS_2")
        await anyio.sleep(0.05)
        long_final_consumer.on_commentary("QQ_LONG_PROGRESS_2")
        await anyio.sleep(0.05)
        long_final_consumer.on_delta(long_final_text)
        await anyio.sleep(0.05)
        long_final_consumer.finish(long_final_text)

    long_final_target = (
        "QQ_LONG_PROGRESS_1QQ_LONG_PROGRESS_2" + long_final_text
    )
    assert_exact_final_ownership(long_final_boundary, long_final_target)
    long_final_bodies = [call[2] for call in long_final_boundary.api_calls]
    assert sum(body["index"] == 0 for body in long_final_bodies) == 4
    assert all(len(body["content_raw"]) <= 4000 for body in long_final_bodies)
    assert "".join(
        body["content_raw"]
        for body in long_final_bodies
        if body["input_state"] == 10
    ).count("QQ_LONG_FINAL_OK") == 1
    assert not long_final_boundary.normal_sends
    assert long_final_consumer.final_response_sent is True
    assert long_final_consumer.delivered_final_matches(long_final_text) is True

    # A response beyond one QQ message must roll over as complete native
    # stream chunks. Generic Hermes overflow would emit an ordinary head and
    # then reuse the draft id with a shorter tail, violating replace-prefix.
    overflow = GatewayDummyAdapter()
    streaming_mod._mark_native_lane(overflow, "user-overflow")
    overflow_cfg = StreamConsumerConfig(
        transport="auto",
        chat_type="dm",
        edit_interval=0.01,
        buffer_threshold=1,
        cursor="",
    )
    overflow_consumer = GatewayStreamConsumer(
        overflow,
        "user-overflow",
        overflow_cfg,
        initial_reply_to_id="inbound-overflow",
    )
    overflow_final = "A" * 2000 + "B" * 2100 + "C" * 300
    async with anyio.create_task_group() as tg:
        tg.start_soon(overflow_consumer.run)
        overflow_consumer.on_delta("A" * 2000)
        await anyio.sleep(0.05)
        overflow_consumer.on_delta("B" * 2100)
        await anyio.sleep(0.05)
        overflow_consumer.on_delta("C" * 300)
        await anyio.sleep(0.05)
        overflow_consumer.finish(overflow_final)

    overflow_bodies = [call[2] for call in overflow.api_calls]
    assert not overflow.normal_sends
    assert sum(body["index"] == 0 for body in overflow_bodies) == 2
    sealed_chunks = [
        body["content_raw"]
        for body in overflow_bodies
        if body["input_state"] == 10
    ]
    assert len(sealed_chunks) == 2
    assert "".join(sealed_chunks) == overflow_final
    active_prefix = ""
    for body in overflow_bodies:
        if body["index"] == 0:
            active_prefix = ""
        assert body["content_raw"].startswith(active_prefix)
        active_prefix = body["content_raw"]
    assert overflow_consumer.final_response_sent is True
    assert overflow_consumer.delivered_final_matches(overflow_final) is True

    # The authoritative final can be the first payload that crosses the QQ
    # limit. It must roll over even when no committed overflow prefix exists
    # yet, rather than truncating the seal at 4000 characters.
    final_growth = DummyAdapter()
    final_growth_metadata = {"reply_to_message_id": "inbound-final-growth"}
    await final_growth.send_draft(
        "user-final-growth",
        5101,
        "D" * 3900,
        final_growth_metadata,
    )
    final_growth_text = "D" * 4100
    final_growth_result = await final_growth.send(
        "user-final-growth",
        final_growth_text,
        reply_to="inbound-final-growth",
        metadata={"notify": True, **final_growth_metadata},
    )
    assert final_growth_result.success
    assert not final_growth.normal_sends
    final_growth_seals = [
        call[2]["content_raw"]
        for call in final_growth.api_calls
        if call[2]["input_state"] == 10
    ]
    assert "".join(final_growth_seals) == final_growth_text
    assert_exact_final_ownership(final_growth, final_growth_text)
    final_growth_repeat = await final_growth.send(
        "user-final-growth",
        final_growth_text,
        reply_to="inbound-final-growth",
        metadata={"notify": True, **final_growth_metadata},
    )
    assert final_growth_repeat.success
    await final_growth.send_draft(
        "user-final-growth",
        5101,
        final_growth_text,
        final_growth_metadata,
    )
    final_growth_streams, _final_growth_anchors = streaming_mod._stream_maps(
        final_growth
    )
    assert not final_growth.normal_sends
    assert ("user-final-growth", 5101) not in final_growth_streams

    # A full 4000-character commentary followed by an independent short final
    # must roll into another native message instead of silently dropping the
    # final at the old seal-body cap.
    independent_full = DummyAdapter()
    independent_full_metadata = {
        "reply_to_message_id": "inbound-independent-full"
    }
    independent_full_draft = "G" * 4000
    independent_full_target = independent_full_draft + "\nFINAL"
    await independent_full.send_draft(
        "user-independent-full",
        5103,
        independent_full_draft,
        independent_full_metadata,
    )
    independent_full_result = await independent_full.send(
        "user-independent-full",
        "FINAL",
        reply_to="inbound-independent-full",
        metadata={"notify": True, **independent_full_metadata},
    )
    assert independent_full_result.success
    assert not independent_full.normal_sends
    assert_exact_final_ownership(independent_full, independent_full_target)

    # The same composition is lossless when an independent final is larger
    # than the remaining capacity in a partially filled commentary stream.
    independent_growth = DummyAdapter()
    independent_growth_metadata = {
        "reply_to_message_id": "inbound-independent-growth"
    }
    independent_growth_draft = "H" * 3900
    independent_growth_final = "I" * 200
    independent_growth_target = (
        independent_growth_draft + "\n" + independent_growth_final
    )
    await independent_growth.send_draft(
        "user-independent-growth",
        5104,
        independent_growth_draft,
        independent_growth_metadata,
    )
    independent_growth_result = await independent_growth.send(
        "user-independent-growth",
        independent_growth_final,
        reply_to="inbound-independent-growth",
        metadata={"notify": True, **independent_growth_metadata},
    )
    assert independent_growth_result.success
    assert not independent_growth.normal_sends
    assert_exact_final_ownership(independent_growth, independent_growth_target)

    # If every rollover-head seal retry fails, the old 3900-character stream
    # remains the visible owner and the ordinary fallback receives only the
    # 201-character unseen suffix. A recovered close must not absorb that
    # suffix and duplicate it.
    head_seal_failure = DummyAdapter()
    head_seal_failure_metadata = {
        "reply_to_message_id": "inbound-head-seal-failure"
    }
    head_seal_failure_draft = "J" * 3900
    head_seal_failure_final = "K" * 200
    head_seal_failure_target = (
        head_seal_failure_draft + "\n" + head_seal_failure_final
    )
    await head_seal_failure.send_draft(
        "user-head-seal-failure",
        5105,
        head_seal_failure_draft,
        head_seal_failure_metadata,
    )
    head_seal_failure.fail_seal_attempts = len(
        streaming_mod._SEAL_RETRY_DELAYS
    )
    head_seal_failure_result = await head_seal_failure.send(
        "user-head-seal-failure",
        head_seal_failure_final,
        reply_to="inbound-head-seal-failure",
        metadata={"notify": True, **head_seal_failure_metadata},
    )
    assert head_seal_failure_result.success
    assert [len(item[1]) for item in head_seal_failure.normal_sends] == [201]
    assert head_seal_failure.normal_sends[0][1] == (
        "\n" + head_seal_failure_final
    )
    assert_exact_final_ownership(head_seal_failure, head_seal_failure_target)

    # If the head was sealed but the new tail stream cannot open, the ordinary
    # fallback owns only the uncommitted suffix. Sending the complete final
    # would duplicate the already-visible 4000-character head.
    tail_failure = DummyAdapter()
    tail_failure.fail_tail_open_attempts = 2
    tail_failure_metadata = {"reply_to_message_id": "inbound-tail-failure"}
    tail_failure_text = "E" * 2000 + "F" * 2100
    await tail_failure.send_draft(
        "user-tail-failure",
        5102,
        "E" * 2000,
        tail_failure_metadata,
    )
    await tail_failure.send_draft(
        "user-tail-failure",
        5102,
        tail_failure_text,
        tail_failure_metadata,
    )
    tail_failure_result = await tail_failure.send(
        "user-tail-failure",
        tail_failure_text,
        reply_to="inbound-tail-failure",
        metadata={"notify": True, **tail_failure_metadata},
    )
    assert tail_failure_result.success
    assert [len(item[1]) for item in tail_failure.normal_sends] == [100]
    assert tail_failure.normal_sends[-1][1] == tail_failure_text[4000:]
    tail_failure_streams, _tail_failure_anchors = streaming_mod._stream_maps(
        tail_failure
    )
    assert not tail_failure_streams
    assert_exact_final_ownership(tail_failure, tail_failure_text)

    # Once a rollover tail is visible, a failed first close round must retry
    # that same tail rather than sending it again through the ordinary API.
    tail_seal_failure = DummyAdapter()
    tail_seal_failure_metadata = {
        "reply_to_message_id": "inbound-tail-seal-failure"
    }
    tail_seal_failure_text = "L" * 4100
    await tail_seal_failure.send_draft(
        "user-tail-seal-failure",
        5106,
        "L" * 3900,
        tail_seal_failure_metadata,
    )
    await tail_seal_failure.send_draft(
        "user-tail-seal-failure",
        5106,
        tail_seal_failure_text,
        tail_seal_failure_metadata,
    )
    tail_seal_failure.fail_seal_attempts = len(
        streaming_mod._SEAL_RETRY_DELAYS
    )
    tail_seal_failure_result = await tail_seal_failure.send(
        "user-tail-seal-failure",
        tail_seal_failure_text,
        reply_to="inbound-tail-seal-failure",
        metadata={"notify": True, **tail_seal_failure_metadata},
    )
    assert tail_seal_failure_result.success
    assert not tail_seal_failure.normal_sends
    tail_seal_streams, _tail_seal_anchors = streaming_mod._stream_maps(
        tail_seal_failure
    )
    assert not tail_seal_streams
    assert_exact_final_ownership(tail_seal_failure, tail_seal_failure_text)

    # A successful ordinary suffix fallback remains an immutable owner even
    # when every immediate recovery seal fails. A later abandon/close must
    # seal only the native text that was already visible, not absorb the
    # ordinary-owned suffix and display the final twice.
    delayed_close = DummyAdapter()
    delayed_close_metadata = {
        "reply_to_message_id": "inbound-delayed-close"
    }
    delayed_close_target = "处理中\nFINAL"
    await delayed_close.send_draft(
        "user-delayed-close",
        5107,
        "处理中",
        delayed_close_metadata,
    )
    delayed_close.fail_next_stream = True
    delayed_close.fail_seal_attempts = len(
        streaming_mod._SEAL_RETRY_DELAYS
    )
    delayed_close_result = await delayed_close.send(
        "user-delayed-close",
        "FINAL",
        reply_to="inbound-delayed-close",
        metadata={"notify": True, **delayed_close_metadata},
    )
    assert delayed_close_result.success
    assert [item[1] for item in delayed_close.normal_sends] == ["\nFINAL"]
    delayed_close_streams, _delayed_close_anchors = streaming_mod._stream_maps(
        delayed_close
    )
    assert ("user-delayed-close", 5107) in delayed_close_streams
    assert_exact_final_ownership(delayed_close, delayed_close_target)
    delayed_broker = delayed_close._qq_native_c2c_final_delivery_broker
    assert delayed_broker.stats().completed == 0
    completed_owner_limit = streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
    streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = 1
    try:
        # Prove abandon completion does not depend on the earlier ordinary
        # fallback tombstone surviving an independent-anchor eviction.
        for draft_id, anchor in (
            (9101, "evict-before-abandon"),
            (9102, "evict-after-abandon"),
        ):
            streaming_mod._remember_turn_tombstone(
                delayed_close,
                streaming_mod._QQC2CStream(
                    chat_id="user-delayed-close",
                    draft_id=draft_id,
                    reply_to=anchor,
                    msg_seq=draft_id,
                ),
                final_payload=anchor,
                final_content=anchor,
            )
            if anchor == "evict-before-abandon":
                delayed_closed = await delayed_close.abandon_open_draft(
                    "user-delayed-close",
                    delayed_close_target,
                    delayed_close_metadata,
                )
                assert delayed_closed.success
                assert delayed_broker.stats().completed == 1
                assert (
                    "user-delayed-close",
                    5107,
                ) not in delayed_close_streams

        assert list(
            delayed_close._qq_native_c2c_completed_owners[
                "user-delayed-close"
            ]
        ) == [("evict-after-abandon", 9102)]
        replayed_after_abandon = await delayed_close.send(
            "user-delayed-close",
            "FINAL",
            reply_to="inbound-delayed-close",
            metadata={"notify": True, **delayed_close_metadata},
        )
        assert replayed_after_abandon.success
    finally:
        streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = completed_owner_limit
    assert [item[1] for item in delayed_close.normal_sends] == ["\nFINAL"]
    assert_exact_final_ownership(delayed_close, delayed_close_target)

    # Once an ordinary fallback owns the suffix, late draft frames from the
    # completed consumer cannot put that immutable text back into the native
    # message. They are stale lifecycle events and must be harmless.
    late_frame = DummyAdapter()
    late_frame_metadata = {
        "reply_to_message_id": "inbound-late-frame"
    }
    late_frame_target = "处理中\nFINAL"
    await late_frame.send_draft(
        "user-late-frame",
        5113,
        "处理中",
        late_frame_metadata,
    )
    late_frame.fail_next_stream = True
    late_frame.fail_seal_attempts = len(streaming_mod._SEAL_RETRY_DELAYS)
    late_frame_result = await late_frame.send(
        "user-late-frame",
        "FINAL",
        reply_to="inbound-late-frame",
        metadata={"notify": True, **late_frame_metadata},
    )
    assert late_frame_result.success
    await late_frame.send_draft(
        "user-late-frame",
        5113,
        late_frame_target,
        late_frame_metadata,
    )
    assert_exact_final_ownership(late_frame, late_frame_target)
    late_frame_closed = await late_frame.abandon_open_draft(
        "user-late-frame",
        late_frame_target,
        late_frame_metadata,
    )
    assert late_frame_closed.success
    assert_exact_final_ownership(late_frame, late_frame_target)

    # A retried turn-final callback may close the retained native stream, but
    # it must not deliver or absorb the ordinary-owned suffix a second time.
    retried_final = DummyAdapter()
    retried_final_metadata = {
        "reply_to_message_id": "inbound-retried-final"
    }
    retried_final_target = "处理中\nFINAL"
    await retried_final.send_draft(
        "user-retried-final",
        5114,
        "处理中",
        retried_final_metadata,
    )
    retried_final.fail_next_stream = True
    retried_final.fail_seal_attempts = len(streaming_mod._SEAL_RETRY_DELAYS)
    first_final = await retried_final.send(
        "user-retried-final",
        "FINAL",
        reply_to="inbound-retried-final",
        metadata={"notify": True, **retried_final_metadata},
    )
    assert first_final.success
    stream_key = ("user-retried-final", 5114)
    assert stream_key in retried_final._qq_native_c2c_streams
    api_calls_before_retry = len(retried_final.api_calls)
    second_final = await retried_final.send(
        "user-retried-final",
        "FINAL",
        reply_to="inbound-retried-final",
        metadata={"notify": True, **retried_final_metadata},
    )
    assert second_final.success
    assert len(retried_final.api_calls) > api_calls_before_retry
    assert retried_final.api_calls[-1][2]["input_state"] == 10
    assert stream_key not in retried_final._qq_native_c2c_streams
    assert [item[1] for item in retried_final.normal_sends] == ["\nFINAL"]
    assert_exact_final_ownership(retried_final, retried_final_target)

    # A recovery close can succeed immediately after the ordinary fallback.
    # Removing the active stream must leave bounded completed-turn ownership:
    # repeated final callbacks and late frames for the same full turn identity
    # are stale, while a new inbound anchor may safely reuse the draft id.
    completed_owner = DummyAdapter()
    completed_owner_metadata = {
        "reply_to_message_id": "inbound-completed-owner"
    }
    completed_owner_target = "处理中\nFINAL"
    await completed_owner.send_draft(
        "user-completed-owner",
        5116,
        "处理中",
        completed_owner_metadata,
    )
    completed_owner.fail_next_stream = True
    completed_owner_result = await completed_owner.send(
        "user-completed-owner",
        "FINAL",
        reply_to="inbound-completed-owner",
        metadata={"notify": True, **completed_owner_metadata},
    )
    assert completed_owner_result.success
    completed_owner_streams, _completed_owner_anchors = (
        streaming_mod._stream_maps(completed_owner)
    )
    assert not completed_owner_streams, completed_owner_streams
    assert [item[1] for item in completed_owner.normal_sends] == ["\nFINAL"]

    repeated_completed_final = await completed_owner.send(
        "user-completed-owner",
        "FINAL",
        reply_to="inbound-completed-owner",
        metadata={"notify": True, **completed_owner_metadata},
    )
    assert repeated_completed_final.success
    await completed_owner.send_draft(
        "user-completed-owner",
        5116,
        completed_owner_target,
        completed_owner_metadata,
    )
    assert not completed_owner_streams
    assert [item[1] for item in completed_owner.normal_sends] == ["\nFINAL"]
    assert_exact_final_ownership(completed_owner, completed_owner_target)

    reused_anchor = "inbound-completed-owner-new-turn"
    reused_draft = await completed_owner.send_draft(
        "user-completed-owner",
        5116,
        "新任务",
        {"reply_to_message_id": reused_anchor},
    )
    assert reused_draft.success
    assert ("user-completed-owner", 5116) in completed_owner_streams
    assert (
        completed_owner_streams[("user-completed-owner", 5116)].reply_to
        == reused_anchor
    )

    # A successful all-native completion also needs a completed-turn owner.
    # Once the seal removes the active state, repeated finals and late frames
    # for the same full identity remain stale lifecycle events.
    native_completed = DummyAdapter()
    native_completed_metadata = {
        "reply_to_message_id": "inbound-native-completed"
    }
    native_completed_target = "全部原生完成"
    await native_completed.send_draft(
        "user-native-completed",
        5118,
        native_completed_target,
        native_completed_metadata,
    )
    native_completed_result = await native_completed.send(
        "user-native-completed",
        native_completed_target,
        reply_to="inbound-native-completed",
        metadata={"notify": True, **native_completed_metadata},
    )
    assert native_completed_result.success
    native_completed_streams, _native_completed_anchors = (
        streaming_mod._stream_maps(native_completed)
    )
    assert not native_completed_streams

    native_completed_repeat = await native_completed.send(
        "user-native-completed",
        native_completed_target,
        reply_to="inbound-native-completed",
        metadata={"notify": True, **native_completed_metadata},
    )
    assert native_completed_repeat.success
    await native_completed.send_draft(
        "user-native-completed",
        5118,
        native_completed_target,
        native_completed_metadata,
    )
    assert not native_completed_streams
    native_calls_before_changed_draft = len(native_completed.api_calls)
    native_changed_draft_late = await native_completed.send_draft(
        "user-native-completed",
        8118,
        f"{native_completed_target}\nLATE",
        native_completed_metadata,
    )
    assert native_changed_draft_late.success
    assert len(native_completed.api_calls) == native_calls_before_changed_draft
    assert not native_completed_streams
    assert not native_completed.normal_sends
    assert_exact_final_ownership(native_completed, native_completed_target)

    # If the first native frame never opens, the single ordinary final is the
    # completed turn's owner. Removing the placeholder must not permit a final
    # retry or late draft to create a second carrier.
    final_only_completed = DummyAdapter()
    final_only_completed_metadata = {
        "reply_to_message_id": "inbound-final-only-completed"
    }
    final_only_completed.fail_next_stream = True
    await final_only_completed.send_draft(
        "user-final-only-completed",
        5119,
        "不可见草稿",
        final_only_completed_metadata,
    )
    final_only_result = await final_only_completed.send(
        "user-final-only-completed",
        "普通最终答复",
        reply_to="inbound-final-only-completed",
        metadata={"notify": True, **final_only_completed_metadata},
    )
    assert final_only_result.success
    final_only_streams, _final_only_anchors = streaming_mod._stream_maps(
        final_only_completed
    )
    assert not final_only_streams

    final_only_repeat = await final_only_completed.send(
        "user-final-only-completed",
        "普通最终答复",
        reply_to="inbound-final-only-completed",
        metadata={"notify": True, **final_only_completed_metadata},
    )
    assert final_only_repeat.success
    await final_only_completed.send_draft(
        "user-final-only-completed",
        5119,
        "不可见草稿\n普通最终答复",
        final_only_completed_metadata,
    )
    assert not final_only_streams
    assert [item[1] for item in final_only_completed.normal_sends] == [
        "普通最终答复"
    ]

    # A previously sealed rollover head can itself become the authoritative
    # final after a tail-open failure. Completing that no-active-stream state
    # must retain ownership just like an active native seal.
    committed_only = DummyAdapter()
    committed_only_metadata = {
        "reply_to_message_id": "inbound-committed-only"
    }
    committed_only_head = "C" * 4000
    committed_only.fail_tail_open_attempts = 1
    await committed_only.send_draft(
        "user-committed-only",
        5120,
        committed_only_head + ("T" * 100),
        committed_only_metadata,
    )
    committed_only_result = await committed_only.send(
        "user-committed-only",
        committed_only_head,
        reply_to="inbound-committed-only",
        metadata={"notify": True, **committed_only_metadata},
    )
    assert committed_only_result.success
    committed_only_streams, _committed_only_anchors = streaming_mod._stream_maps(
        committed_only
    )
    assert not committed_only_streams

    committed_only_repeat = await committed_only.send(
        "user-committed-only",
        committed_only_head,
        reply_to="inbound-committed-only",
        metadata={"notify": True, **committed_only_metadata},
    )
    assert committed_only_repeat.success
    await committed_only.send_draft(
        "user-committed-only",
        5120,
        committed_only_head,
        committed_only_metadata,
    )
    assert not committed_only_streams
    assert not committed_only.normal_sends

    # Completed ownership is deliberately bounded. Once the oldest identity
    # is evicted, a replay can open normally; the newest retained identity
    # must still reject its stale late frame.
    bounded_owners = DummyAdapter()
    first_bounded_draft = 5200
    completed_owner_limit = streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
    completed_result_limit = streaming_mod._MAX_FINAL_DELIVERY_RESULTS
    streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = 2
    streaming_mod._MAX_FINAL_DELIVERY_RESULTS = 2
    try:
        for offset in range(streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT + 1):
            bounded_draft_id = first_bounded_draft + offset
            bounded_anchor = f"inbound-bounded-owner-{offset}"
            bounded_metadata = {"reply_to_message_id": bounded_anchor}
            await bounded_owners.send_draft(
                "user-bounded-owner",
                bounded_draft_id,
                "处理中",
                bounded_metadata,
            )
            bounded_owners.fail_next_stream = True
            bounded_result = await bounded_owners.send(
                "user-bounded-owner",
                "FINAL",
                reply_to=bounded_anchor,
                metadata={"notify": True, **bounded_metadata},
            )
            assert bounded_result.success

        await bounded_owners.send_draft(
            "user-bounded-owner",
            first_bounded_draft,
            "处理中\nFINAL",
            {"reply_to_message_id": "inbound-bounded-owner-0"},
        )
        newest_bounded_draft = (
            first_bounded_draft + streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
        )
        await bounded_owners.send_draft(
            "user-bounded-owner",
            newest_bounded_draft,
            "处理中\nFINAL",
            {
                "reply_to_message_id": (
                    "inbound-bounded-owner-"
                    f"{streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT}"
                )
            },
        )
        bounded_streams, _bounded_anchors = streaming_mod._stream_maps(
            bounded_owners
        )
        assert ("user-bounded-owner", first_bounded_draft) in bounded_streams
        assert (
            "user-bounded-owner",
            newest_bounded_draft,
        ) not in bounded_streams
    finally:
        streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = completed_owner_limit
        streaming_mod._MAX_FINAL_DELIVERY_RESULTS = completed_result_limit

    # Per-chat quotas must not leave the outer chat registry unbounded. The
    # least-recently-used completed chat may expire once the total chat bound
    # is exceeded, while the newest chat keeps replay protection.
    completed_chat_limit_existed = hasattr(
        streaming_mod,
        "_MAX_COMPLETED_OWNER_CHATS",
    )
    completed_chat_limit = getattr(
        streaming_mod,
        "_MAX_COMPLETED_OWNER_CHATS",
        None,
    )
    completed_result_limit = streaming_mod._MAX_FINAL_DELIVERY_RESULTS
    streaming_mod._MAX_COMPLETED_OWNER_CHATS = 2
    streaming_mod._MAX_FINAL_DELIVERY_RESULTS = 2
    try:
        bounded_owner_chats = DummyAdapter()
        for offset in range(3):
            chat_id = f"user-owner-registry-{offset}"
            draft_id = 5400 + offset
            anchor = f"inbound-owner-registry-{offset}"
            payload = f"owner final {offset}"
            await bounded_owner_chats.send_draft(
                chat_id,
                draft_id,
                payload,
                {"reply_to_message_id": anchor},
            )
            await bounded_owner_chats.send(
                chat_id,
                payload,
                reply_to=anchor,
                metadata={"notify": True, "reply_to_message_id": anchor},
            )

        before_oldest_owner = len(bounded_owner_chats.api_calls)
        await bounded_owner_chats.send_draft(
            "user-owner-registry-0",
            5400,
            "owner final 0",
            {"reply_to_message_id": "inbound-owner-registry-0"},
        )
        assert len(bounded_owner_chats.api_calls) == before_oldest_owner + 1

        before_newest_owner = len(bounded_owner_chats.api_calls)
        await bounded_owner_chats.send_draft(
            "user-owner-registry-2",
            5402,
            "owner final 2",
            {"reply_to_message_id": "inbound-owner-registry-2"},
        )
        assert len(bounded_owner_chats.api_calls) == before_newest_owner
    finally:
        if completed_chat_limit_existed:
            streaming_mod._MAX_COMPLETED_OWNER_CHATS = completed_chat_limit
        else:
            del streaming_mod._MAX_COMPLETED_OWNER_CHATS
        streaming_mod._MAX_FINAL_DELIVERY_RESULTS = completed_result_limit

    # Capacity is isolated per private chat. Heavy completion traffic in chat
    # B must not evict chat A's replay protection.
    cross_chat_owners = DummyAdapter()
    completed_owner_limit = streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT
    streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = 2
    try:
        await cross_chat_owners.send_draft(
            "user-owner-a",
            5300,
            "A final",
            {"reply_to_message_id": "inbound-owner-a"},
        )
        await cross_chat_owners.send(
            "user-owner-a",
            "A final",
            reply_to="inbound-owner-a",
            metadata={"notify": True, "reply_to_message_id": "inbound-owner-a"},
        )
        for offset in range(3):
            await cross_chat_owners.send_draft(
                "user-owner-b",
                5301 + offset,
                f"B final {offset}",
                {"reply_to_message_id": f"inbound-owner-b-{offset}"},
            )
            await cross_chat_owners.send(
                "user-owner-b",
                f"B final {offset}",
                reply_to=f"inbound-owner-b-{offset}",
                metadata={
                    "notify": True,
                    "reply_to_message_id": f"inbound-owner-b-{offset}",
                },
            )

        replay_a = await cross_chat_owners.send(
            "user-owner-a",
            "A final",
            reply_to="inbound-owner-a",
            metadata={"notify": True, "reply_to_message_id": "inbound-owner-a"},
        )
        assert replay_a.success
        await cross_chat_owners.send_draft(
            "user-owner-a",
            5300,
            "A final",
            {"reply_to_message_id": "inbound-owner-a"},
        )
        cross_chat_streams, _cross_chat_anchors = streaming_mod._stream_maps(
            cross_chat_owners
        )
        assert not cross_chat_owners.normal_sends
        assert ("user-owner-a", 5300) not in cross_chat_streams
    finally:
        streaming_mod._MAX_COMPLETED_OWNERS_PER_CHAT = completed_owner_limit

    # Independent finals are payloads, not substring searches. An earlier,
    # non-terminal occurrence of the same value does not own the final.
    repeated_final = DummyAdapter()
    repeated_final_metadata = {
        "reply_to_message_id": "inbound-repeated-final"
    }
    repeated_final_draft = "progress FINAL details"
    repeated_final_target = repeated_final_draft + "\nFINAL"
    await repeated_final.send_draft(
        "user-repeated-final",
        5108,
        repeated_final_draft,
        repeated_final_metadata,
    )
    repeated_final_result = await repeated_final.send(
        "user-repeated-final",
        "FINAL",
        reply_to="inbound-repeated-final",
        metadata={"notify": True, **repeated_final_metadata},
    )
    assert repeated_final_result.success
    assert_exact_final_ownership(repeated_final, repeated_final_target)

    # A true terminal copy already has one visible owner and must not be
    # appended again merely because the final callback repeats it.
    terminal_final = DummyAdapter()
    terminal_final_metadata = {
        "reply_to_message_id": "inbound-terminal-final"
    }
    terminal_final_target = "progress\nFINAL"
    await terminal_final.send_draft(
        "user-terminal-final",
        5109,
        terminal_final_target,
        terminal_final_metadata,
    )
    terminal_final_result = await terminal_final.send(
        "user-terminal-final",
        "FINAL",
        reply_to="inbound-terminal-final",
        metadata={"notify": True, **terminal_final_metadata},
    )
    assert terminal_final_result.success
    assert_exact_final_ownership(terminal_final, terminal_final_target)

    # The same Unicode punctuation boundary applies to the turn-final path:
    # the already-visible terminal payload remains the sole owner.
    punctuation_final = DummyAdapter()
    punctuation_final_metadata = {
        "reply_to_message_id": "inbound-punctuation-final"
    }
    punctuation_final_target = "阶段一，FINAL"
    await punctuation_final.send_draft(
        "user-punctuation-final",
        5117,
        punctuation_final_target,
        punctuation_final_metadata,
    )
    punctuation_final_result = await punctuation_final.send(
        "user-punctuation-final",
        "FINAL",
        reply_to="inbound-punctuation-final",
        metadata={"notify": True, **punctuation_final_metadata},
    )
    assert punctuation_final_result.success
    assert_exact_final_ownership(punctuation_final, punctuation_final_target)

    # Coincidental partial overlap and a matching substring inside a larger
    # terminal word are not ownership. Preserve the complete independent
    # final behind a message boundary in both cases.
    for draft_id, draft_text, final_text in (
        (5110, "status F", "FINAL"),
        (5111, "status NOTFINAL", "FINAL"),
    ):
        overlap = DummyAdapter()
        overlap_metadata = {
            "reply_to_message_id": f"inbound-overlap-{draft_id}"
        }
        overlap_target = draft_text + "\n" + final_text
        await overlap.send_draft(
            f"user-overlap-{draft_id}",
            draft_id,
            draft_text,
            overlap_metadata,
        )
        overlap_result = await overlap.send(
            f"user-overlap-{draft_id}",
            final_text,
            reply_to=overlap_metadata["reply_to_message_id"],
            metadata={"notify": True, **overlap_metadata},
        )
        assert overlap_result.success
        assert_exact_final_ownership(overlap, overlap_target)

    # A caller-supplied message boundary is authoritative; composition must
    # not insert a second newline before an independent final that already
    # starts with one.
    leading_boundary = DummyAdapter()
    leading_boundary_metadata = {
        "reply_to_message_id": "inbound-leading-boundary"
    }
    await leading_boundary.send_draft(
        "user-leading-boundary",
        5115,
        "progress",
        leading_boundary_metadata,
    )
    leading_boundary_result = await leading_boundary.send(
        "user-leading-boundary",
        "\nFINAL",
        reply_to="inbound-leading-boundary",
        metadata={"notify": True, **leading_boundary_metadata},
    )
    assert leading_boundary_result.success
    assert_exact_final_ownership(leading_boundary, "progress\nFINAL")

    # A cumulative authoritative final remains a replace, not an independent
    # append, when it explicitly extends the complete visible draft.
    cumulative_final = DummyAdapter()
    cumulative_final_metadata = {
        "reply_to_message_id": "inbound-cumulative-final"
    }
    await cumulative_final.send_draft(
        "user-cumulative-final",
        5112,
        "progress ",
        cumulative_final_metadata,
    )
    cumulative_final_result = await cumulative_final.send(
        "user-cumulative-final",
        "progress complete",
        reply_to="inbound-cumulative-final",
        metadata={"notify": True, **cumulative_final_metadata},
    )
    assert cumulative_final_result.success
    assert_exact_final_ownership(cumulative_final, "progress complete")

    # A stale/cancelled consumer can close the same visible stream through
    # Hermes' real three-argument abandon_open_draft contract.
    cancelled = GatewayDummyAdapter()
    await cancelled.send_draft(
        "user-cancel",
        5001,
        "部分结果",
        {"reply_to_message_id": "inbound-cancel"},
    )
    closed = await cancelled.abandon_open_draft(
        "user-cancel",
        "部分结果",
        {"reply_to_message_id": "inbound-cancel"},
    )
    assert closed.success
    assert cancelled.api_calls[-1][2]["input_state"] == 10
    cancelled_repeat = await cancelled.send(
        "user-cancel",
        "部分结果",
        reply_to="inbound-cancel",
        metadata={"notify": True, "reply_to_message_id": "inbound-cancel"},
    )
    assert cancelled_repeat.success
    await cancelled.send_draft(
        "user-cancel",
        5001,
        "部分结果",
        {"reply_to_message_id": "inbound-cancel"},
    )
    cancelled_streams, _cancelled_anchors = streaming_mod._stream_maps(cancelled)
    assert not cancelled.normal_sends
    assert ("user-cancel", 5001) not in cancelled_streams

    print("qq_c2c_stream_open_continue_seal=ok")
    print("qq_c2c_stream_age_rollover=ok")
    print("qq_c2c_stream_silent_expiry_rollover=ok")
    print("qq_c2c_stream_expiry_task_cancelled=ok")
    print("qq_c2c_stream_lifetime_terminal_retirement=ok")
    print("qq_c2c_stream_ambiguous_timeout_reconciled=ok")
    print("qq_c2c_stream_ambiguous_seal_single_frame=ok")
    print("qq_c2c_stream_ambiguous_open_lossless_fallback=ok")
    print("qq_c2c_stream_frame_cooldown_coalescing=ok")
    print("qq_c2c_stream_reply_budget_terminal_retirement=ok")
    print("qq_c2c_stream_cooled_final_lossless=ok")
    print("qq_c2c_stream_seal_preserves_prefix=ok")
    print("qq_c2c_stream_parallel_dm_isolation=ok")
    print("qq_c2c_same_draft_id_cross_chat_isolation=ok")
    print("qq_c2c_stream_nonfinal_send_isolation=ok")
    print("qq_c2c_streamed_interim_carrier_dedup=ok")
    print("qq_c2c_interim_ownership_boundaries=ok")
    print("qq_c2c_unicode_punctuation_ownership=ok")
    print("qq_c2c_stream_abandon_close=ok")
    print("qq_c2c_stream_fallback=ok")
    print("qq_c2c_stream_seal_retry=ok")
    print("qq_c2c_stream_safe_final_fallback_close=ok")
    print("qq_c2c_stream_seal_state_retained=ok")
    print("qq_c2c_stream_capacity_preserves_opened=ok")
    print("qq_c2c_capacity_pending_abandon_replay_dedup=ok")
    print("qq_c2c_concurrent_final_single_delivery=ok")
    print("qq_c2c_active_fallback_single_flight=ok")
    print("qq_c2c_active_fallback_tombstone_eviction_safe=ok")
    print("qq_c2c_abandon_final_flight_coordination=ok")
    print("qq_c2c_abandon_first_terminal_owner=ok")
    print("qq_c2c_abandon_first_claim_context_eviction_safe=ok")
    print("qq_c2c_abandon_leading_boundary_owned=ok")
    print("qq_c2c_active_final_late_frame_coordination=ok")
    print("qq_c2c_completed_anchor_changed_draft_guard=ok")
    print("qq_c2c_unanchored_finals_independent=ok")
    print("qq_c2c_failed_final_claim_retry=ok")
    print("qq_c2c_independent_final_claims_parallel=ok")
    print("qq_c2c_cancelled_final_waiter_cleanup=ok")
    print("qq_c2c_cancelled_final_holder_handoff=ok")
    print("qq_c2c_raised_final_claim_handoff=ok")
    print("qq_c2c_final_only_pending_chat_registry_bounded=ok")
    print("qq_c2c_disabled_typing_unchanged=ok")
    print("qq_c2c_interim_only_runner_stays_disabled=ok")
    print("qq_c2c_prerelease_version_fail_closed=ok")
    print("qq_c2c_typing_budget=ok")
    print("qq_c2c_gateway_stream_gate=ok")
    print("qq_c2c_gateway_stream_consumer=ok")
    print("qq_c2c_streamed_commentary_single_carrier=ok")
    print("qq_c2c_consecutive_commentary_single_carrier=ok")
    print("qq_c2c_long_final_boundary_single_owner=ok")
    print("qq_c2c_consumer_independent_suffix_final=ok")
    print("qq_c2c_consumer_partial_final_same_tick=ok")
    print("qq_c2c_consumer_augmented_final=ok")
    print("qq_c2c_consumer_delta_provenance_boundaries=ok")
    print("qq_c2c_guild_dm_rejected=ok")
    print("qq_c2c_runtime_disable_revokes_lane=ok")
    print("qq_c2c_native_lane_registry_bounded=ok")
    print("qq_c2c_overflow_rollover=ok")
    print("qq_c2c_final_first_overflow_rollover=ok")
    print("qq_c2c_independent_final_full_rollover=ok")
    print("qq_c2c_independent_final_growth_rollover=ok")
    print("qq_c2c_head_seal_failure_suffix_ownership=ok")
    print("qq_c2c_tail_open_failure_suffix_fallback=ok")
    print("qq_c2c_tail_seal_failure_no_duplicate=ok")
    print("qq_c2c_delayed_close_ordinary_ownership=ok")
    print("qq_c2c_ordinary_owned_late_frame_ignored=ok")
    print("qq_c2c_ordinary_owned_final_retry=ok")
    print("qq_c2c_completed_owner_replay_dedup=ok")
    print("qq_c2c_native_completed_owner_replay_dedup=ok")
    print("qq_c2c_final_only_completed_owner_replay_dedup=ok")
    print("qq_c2c_committed_only_completed_owner_replay_dedup=ok")
    print("qq_c2c_completed_owner_anchor_isolation=ok")
    print("qq_c2c_completed_owner_bounded=ok")
    print("qq_c2c_completed_owner_cross_chat_isolation=ok")
    print("qq_c2c_completed_owner_chat_registry_bounded=ok")
    print("qq_c2c_nonterminal_repeated_final=ok")
    print("qq_c2c_terminal_final_single_owner=ok")
    print("qq_c2c_partial_overlap_not_ownership=ok")
    print("qq_c2c_leading_boundary_not_duplicated=ok")
    print("qq_c2c_cumulative_final_replace=ok")
    print("qq_c2c_abandoned_completed_owner_replay_dedup=ok")


if __name__ == "__main__":
    anyio.run(main)
