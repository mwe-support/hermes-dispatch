import importlib.util
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import anyio


def load_plugin_module():
    path = Path("/opt/data/plugins/qqbot-connect-hotfix/__init__.py")
    if not path.exists():
        path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location(
        "qqbot_connect_expired_reply_test",
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


class DummyAdapter:
    def __init__(self):
        self.calls = []

    async def _send_c2c_text(
        self, target_id, content, reply_to=None, keyboard=None
    ):
        self.calls.append(("c2c", target_id, content, reply_to, keyboard))
        if reply_to:
            raise RuntimeError("回复消息msg_id已过期")
        return SimpleNamespace(success=True, message_id="c2c-standalone")

    async def _send_group_text(
        self, target_id, content, reply_to=None, keyboard=None
    ):
        self.calls.append(("group", target_id, content, reply_to, keyboard))
        if reply_to:
            raise RuntimeError("QQ Bot API error: message_id expired")
        return SimpleNamespace(success=True, message_id="group-standalone")

    async def _send_guild_text(self, target_id, content, reply_to=None):
        self.calls.append(("guild", target_id, content, reply_to, None))
        if reply_to:
            raise RuntimeError("QQ Bot API error: message id expiration")
        return SimpleNamespace(success=True, message_id="guild-standalone")


class UnrelatedErrorAdapter(DummyAdapter):
    async def _send_group_text(
        self, target_id, content, reply_to=None, keyboard=None
    ):
        self.calls.append(("group", target_id, content, reply_to, keyboard))
        raise RuntimeError("QQ Bot API error: forbidden")


class FallbackFailureAdapter(DummyAdapter):
    async def _send_group_text(
        self, target_id, content, reply_to=None, keyboard=None
    ):
        self.calls.append(("group", target_id, content, reply_to, keyboard))
        if reply_to:
            raise RuntimeError("msg_id expired")
        raise RuntimeError("standalone rate limited")


class WindowsPassiveRetryAdapter(DummyAdapter):
    """Reproduce the Windows proactive-to-passive wrapper inside register()."""

    def __init__(self):
        super().__init__()
        self._last_msg_id = {"group-4": "expired-4"}

    async def _native_group_text(self, target_id, content, reply_to=None, keyboard=None):
        self.calls.append(("group", target_id, content, reply_to, keyboard))
        if reply_to:
            raise RuntimeError("回复消息msg_id已过期")
        raise RuntimeError("主动消息失败, 无权限")

    async def _send_group_text(self, target_id, content, reply_to=None, keyboard=None):
        try:
            return await self._native_group_text(target_id, content, reply_to, keyboard)
        except RuntimeError as exc:
            if reply_to or "主动消息失败" not in str(exc):
                raise
            cached = self._last_msg_id.get(target_id)
            if cached:
                return await self._native_group_text(target_id, content, cached, keyboard)
            raise


class ComposedGroupAdapter:
    def __init__(self):
        self.calls = []
        self._last_msg_id = {}
        self._markdown_support = False
        self.inject_newer = False
        self.result_error = False

    async def handle_message(self, event):
        self._last_msg_id[event.source.chat_id] = event.message_id

    async def _send_c2c_text(self, target_id, content, reply_to=None, keyboard=None):
        return SimpleNamespace(success=True)

    async def _send_group_text(self, target_id, content, reply_to=None, keyboard=None):
        self.calls.append((content, reply_to, keyboard))
        if reply_to == "expired":
            if self.inject_newer:
                self._last_msg_id[target_id] = "newer"
                self._qq_group_reply_seen[target_id] = ("newer", time.time())
            if self.result_error:
                return SimpleNamespace(success=False, error="回复消息msg_id已过期")
            raise RuntimeError("回复消息msg_id已过期")
        if reply_to in {"recent", "newer"}:
            return SimpleNamespace(success=True)
        raise RuntimeError("主动消息失败, 无权限")


async def main():
    mod._patch_expired_reply_fallback(DummyAdapter)
    # Registration is idempotent and must not add a second retry layer.
    mod._patch_expired_reply_fallback(DummyAdapter)

    keyboard = object()
    adapter = DummyAdapter()
    result = await adapter._send_group_text(
        "group-1", "approve?", "expired-1", keyboard
    )
    assert result.success
    assert adapter.calls == [
        ("group", "group-1", "approve?", "expired-1", keyboard),
        ("group", "group-1", "approve?", None, keyboard),
    ]

    c2c = DummyAdapter()
    result = await c2c._send_c2c_text("user-1", "done", "expired-2")
    assert result.success
    assert [call[3] for call in c2c.calls] == ["expired-2", None]

    guild = DummyAdapter()
    result = await guild._send_guild_text("channel-1", "done", "expired-3")
    assert result.success
    assert [call[3] for call in guild.calls] == ["expired-3", None]

    mod._patch_expired_reply_fallback(UnrelatedErrorAdapter)
    unrelated = UnrelatedErrorAdapter()
    try:
        await unrelated._send_group_text("group-2", "done", "anchor")
    except RuntimeError as exc:
        assert "forbidden" in str(exc)
    else:
        raise AssertionError("unrelated errors must not trigger standalone retry")
    assert len(unrelated.calls) == 1

    mod._patch_expired_reply_fallback(FallbackFailureAdapter)
    failed = FallbackFailureAdapter()
    try:
        await failed._send_group_text("group-3", "done", "expired-4")
    except RuntimeError as exc:
        text = str(exc)
        assert "standalone rate limited" in text
        assert "msg_id expired" in text
    else:
        raise AssertionError("fallback failure must preserve both diagnostics")

    # Match register(): the Windows passive retry is the inner sender, then
    # plain-text compatibility and expired-reply fallback wrap it in order.
    mod._patch_plain_text_retry(WindowsPassiveRetryAdapter)
    mod._patch_expired_reply_fallback(WindowsPassiveRetryAdapter)
    windows = WindowsPassiveRetryAdapter()
    try:
        await windows._send_group_text("group-4", "final", "expired-4", keyboard)
    except RuntimeError as exc:
        assert "主动消息失败, 无权限" in str(exc), str(exc)
    else:
        raise AssertionError("proactive denial must remain visible")
    assert [call[3] for call in windows.calls] == ["expired-4", None], windows.calls

    # Exercise the actual registration order and the QQ event timestamp.
    mod._patch_group_reply_timestamps(ComposedGroupAdapter)
    mod._patch_plain_text_retry(ComposedGroupAdapter)
    mod._patch_expired_reply_fallback(ComposedGroupAdapter)

    async def inbound(adapter, anchor, age=0):
        event = SimpleNamespace(
            source=SimpleNamespace(chat_type="group", chat_id="group"),
            message_id=anchor,
            timestamp=datetime.now(timezone.utc) - timedelta(seconds=age),
        )
        await adapter.handle_message(event)

    recent = ComposedGroupAdapter()
    await inbound(recent, "recent")
    assert (await recent._send_group_text("group", "progress", keyboard=keyboard)).success
    assert recent.calls == [("progress", None, keyboard), ("progress", "recent", keyboard)]

    stale = ComposedGroupAdapter()
    await inbound(stale, "recent", age=301)
    try:
        await stale._send_group_text("group", "final")
    except RuntimeError as exc:
        assert "主动消息失败" in str(exc)
    else:
        raise AssertionError("an expired passive window must not be retried")
    assert stale.calls == [("final", None, None)]

    for result_error in (False, True):
        expired = ComposedGroupAdapter()
        expired.result_error = result_error
        await inbound(expired, "expired")
        try:
            await expired._send_group_text("group", "final", "expired", keyboard)
        except RuntimeError as exc:
            assert "主动消息失败" in str(exc), str(exc)
        else:
            raise AssertionError("standalone denial must remain visible")
        assert [call[1] for call in expired.calls] == ["expired", None]
        assert all(call[2] is keyboard for call in expired.calls)
        assert "group" not in expired._last_msg_id

    newer = ComposedGroupAdapter()
    newer.inject_newer = True
    await inbound(newer, "expired")
    try:
        await newer._send_group_text("group", "final", "expired")
    except RuntimeError as exc:
        assert "主动消息失败" in str(exc)
    else:
        raise AssertionError("expired fallback must stay standalone")
    assert [call[1] for call in newer.calls] == ["expired", None]
    assert newer._last_msg_id["group"] == "newer"

    for message in (
        "回复消息msg_id已过期",
        "msg_id expired",
        "message_id has expired",
        "message id expiration",
    ):
        assert mod._is_expired_reply_error(message), message
    for message in ("msg_id missing", "message id invalid", "request expired"):
        assert not mod._is_expired_reply_error(message), message

    print("expired_reply_group_fallback=ok")
    print("expired_reply_c2c_fallback=ok")
    print("expired_reply_guild_fallback=ok")
    print("expired_reply_keyboard_preserved=ok")
    print("expired_reply_diagnostics=ok")


anyio.run(main)
