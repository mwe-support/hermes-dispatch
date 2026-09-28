"""Offline integration test for QQ group receive and send paths.

Uses the real Hermes QQAdapter with synthetic gateway events and a fake REST
request method. No network connection or real QQ message is used.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio


def load_plugin_module():
    path = Path("/opt/data/plugins/qqbot-connect-hotfix/__init__.py")
    if not path.exists():
        path = Path(__file__).with_name("__init__.py")
    spec = importlib.util.spec_from_file_location(
        "qqbot_connect_hotfix_roundtrip_test",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = spec.name
    module.__path__ = [str(path.parent)]
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def main():
    mod = load_plugin_module()
    mod.register(None)

    from gateway.config import PlatformConfig
    from gateway.platforms.qqbot.adapter import QQAdapter

    adapter = QQAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "app_id": "bot-app",
                "client_secret": "test-only",
                "group_policy": "open",
                "markdown_support": False,
            },
        )
    )

    received = []

    async def capture_message(event):
        received.append(event)

    # Capture the fully constructed Hermes MessageEvent through the normal
    # BasePlatformAdapter callback, without invoking a gateway agent.
    adapter.set_message_handler(capture_message)

    await adapter._on_message(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "context-1",
            "content": "earlier group context",
            "group_openid": "group-test",
            "author": {"member_openid": "member-a"},
            "timestamp": "2026-07-20T12:00:00Z",
        },
    )
    assert received == []

    await adapter._on_message(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "mention-other",
            "content": "hello owner<@owner-openid>",
            "group_openid": "group-test",
            "author": {"member_openid": "member-a"},
            "mentions": [{"id": "owner-openid", "bot": False, "is_you": False}],
            "timestamp": "2026-07-20T12:00:00Z",
        },
    )
    assert received == []

    await adapter._on_message(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "mention-1",
            "content": "hello bot",
            "group_openid": "group-test",
            "author": {"member_openid": "member-b"},
            "mentions": [{"id": "bot-openid", "bot": True, "is_you": True}],
            "timestamp": "2026-07-20T12:00:01Z",
        },
    )
    tasks = list(adapter._session_tasks.values())
    if tasks:
        await anyio.sleep(0)
        for task in tasks:
            await task
    assert len(received) == 1
    event = received[0]
    assert event.source.chat_id == "group-test"
    assert event.source.chat_type == "group"
    assert event.source.user_id == "member-b"
    assert event.text == "hello bot"
    assert "earlier group context" in event.channel_context

    # QQ serializes real @ chips as <@id>, including after slash arguments.
    # Exercise adapter -> BasePlatformAdapter -> MessageEvent command parsing.
    cases = [
        ("<@bot-openid> /model gpt-6-luna --global", "model", "gpt-6-luna --global"),
        ("/model gpt-6-luna --global<@bot-openid>", "model", "gpt-6-luna --global"),
        ("/model <@bot-openid>", "model", ""),
        ("<@bot-openid> /help", "help", ""),
        ("/reasoning high <@bot-openid>", "reasoning", "high"),
        ("<@bot-openid> /stop", "stop", ""),
        ("<@bot-openid> /model user@provider <@other>", "model", "user@provider <@other>"),
        ("hello <@bot-openid>", None, "hello <@bot-openid>"),
    ]
    for index, (text, command, args) in enumerate(cases):
        raw = {
            "id": f"slash-{index}", "content": text, "group_openid": "group-test",
            "author": {"member_openid": "member-b"},
            "mentions": [{"id": "bot-openid", "bot": True, "is_you": True}],
        }
        await adapter._on_message("GROUP_AT_MESSAGE_CREATE", raw)
        await anyio.sleep(0)
        for task in list(adapter._session_tasks.values()):
            await task
        event = received[-1]
        assert event.get_command() == command, (text, event.text)
        assert event.get_command_args().strip() == args, (text, event.text)
        assert raw["content"] == text  # retain original transport evidence

    requests = []

    async def fake_api_request(method, path, body=None, **_kwargs):
        requests.append((method, path, body))
        return {"id": "sent-group-message"}

    adapter._api_request = fake_api_request
    adapter._chat_type_map["group-test"] = "group"
    adapter._running = True
    adapter._ws = SimpleNamespace(closed=False)

    result = await adapter.send("group-test", "offline group reply")
    assert result.success
    assert result.message_id == "sent-group-message"
    assert len(requests) == 1
    method, path, body = requests[0]
    assert method == "POST"
    assert path == "/v2/groups/group-test/messages"
    assert body["content"] == "offline group reply"

    print("group_slash_self_mentions=true")
    print("group_receive_event=true")
    print("group_context_injected=true")
    print("group_send_route=/v2/groups/group-test/messages")
    print("network_used=false")


anyio.run(main)
