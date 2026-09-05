import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import anyio


def load_plugin_module():
    path = Path(__file__).with_name("__init__.py")
    if not path.exists():
        path = Path("/opt/data/plugins/qqbot-connect-hotfix/__init__.py")
    spec = importlib.util.spec_from_file_location(
        "qqbot_connect_hotfix_test",
        path,
        submodule_search_locations=[str(path.parent)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "qqbot_connect_hotfix_test"
    mod.__path__ = [str(path.parent)]
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_plugin_module()

temp_dir = tempfile.TemporaryDirectory()
channel_directory_path = Path(temp_dir.name) / "channel_directory.json"
channel_directory_path.write_text(
    json.dumps(
        {
            "platforms": {
                "qqbot": [
                    {"id": "B279C1A461933B21DAFEE3263B8854A6", "type": "group"},
                ],
            },
        },
    ),
    encoding="utf-8",
)
os.environ["HERMES_CHANNEL_DIRECTORY"] = str(channel_directory_path)


class DummyAdapter:
    MAX_MESSAGE_LENGTH = 4000

    def __init__(self):
        self.calls = []

    def _next_msg_seq(self, key):
        return 42

    async def _api_request(self, method, path, body):
        self.calls.append((method, path, body))
        return {"id": "ok"}


class DummyInteractionAdapter:
    def __init__(self):
        self.acks = []
        self.delegated = []

    async def _acknowledge_interaction(self, interaction_id, code=0):
        self.acks.append((interaction_id, code, None))

    async def _on_interaction(self, raw):
        self.delegated.append(raw)


class DummyHttpResponse:
    status_code = 204
    text = ""


class DummyHttpClient:
    def __init__(self):
        self.calls = []

    async def put(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return DummyHttpResponse()


class DummyAckAdapter:
    def __init__(self):
        self._http_client = DummyHttpClient()

    async def _ensure_token(self):
        return "test-token"

    async def _acknowledge_interaction(self, interaction_id, code=0):
        raise AssertionError("the wrapped data-aware ACK must bypass the original method")

    async def _on_interaction(self, raw):
        return None


class DummyDispatchAdapter:
    def __init__(self):
        self.created = []
        self.events = []
        self._app_id = "123456"

    def _create_task(self, coro):
        self.created.append(coro)
        return coro

    def _dispatch_payload(self, payload):
        self.events.append(("dispatch", payload))

    async def _on_message(self, event_type, d):
        self.events.append((event_type, d))


class DummyEvent:
    def __init__(self, raw_message):
        self.raw_message = raw_message
        self.channel_context = None


class DummyHandleAdapter:
    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)
        return None


class DummyApprovalAdapter:
    _APPROVAL_BUTTON_TO_CHOICE = {
        "allow-once": "once",
        "allow-session": "session",
        "allow-always": "always",
        "deny": "deny",
    }

    def __init__(self):
        self.sent = []
        self.delegated = []

    @staticmethod
    def _parse_gateway_session_key(session_key):
        parts = str(session_key).split(":")
        if len(parts) < 5 or parts[:3] != ["agent", "main", "qqbot"]:
            return None
        parsed = {
            "platform": parts[2],
            "chat_type": parts[3],
            "chat_id": parts[4],
        }
        if len(parts) > 5:
            parsed["user_id"] = parts[5]
        return parsed

    async def send_exec_approval(
        self,
        chat_id,
        command,
        session_key,
        description="dangerous command",
        metadata=None,
        allow_permanent=True,
        smart_denied=False,
    ):
        self.sent.append(
            (
                chat_id,
                command,
                session_key,
                description,
                metadata,
                allow_permanent,
                smart_denied,
            )
        )
        return SimpleNamespace(success=True)

    async def _default_interaction_dispatch(self, event):
        self.delegated.append(event)


class DummyCurrentApprovalAdapter(DummyApprovalAdapter):
    """Model the 0.19-era adapter contract with explicit session scope."""

    async def send_exec_approval(
        self,
        chat_id,
        command,
        session_key,
        description="dangerous command",
        metadata=None,
        allow_permanent=True,
        allow_session=True,
        smart_denied=False,
    ):
        self.sent.append(
            (
                chat_id,
                command,
                session_key,
                description,
                metadata,
                allow_permanent,
                allow_session,
                smart_denied,
            )
        )
        return SimpleNamespace(success=True)


class DummyChoiceAdapter:
    _APPROVAL_BUTTON_TO_CHOICE = {
        "allow-once": "once",
        "allow-always": "always",
        "deny": "deny",
    }

    def __init__(self):
        self.sent = []
        self.delegated = []

    async def send_with_keyboard(self, chat_id, text, keyboard, reply_to=None):
        self.sent.append((chat_id, text, keyboard.to_dict(), reply_to))
        return SimpleNamespace(success=True)

    async def send_approval_request(self, chat_id, req, reply_to=None):
        self.delegated.append((chat_id, req, reply_to))
        return SimpleNamespace(success=True)


class DummySlashCommands:
    def __init__(self):
        self.handled = []

    @staticmethod
    def _session_key_for_source(source):
        return source.session_key

    async def _handle_approve_command(self, event):
        self.handled.append(("approve", event.source.user_id))
        return "approved"

    async def _handle_deny_command(self, event):
        self.handled.append(("deny", event.source.user_id))
        return "denied"


async def main():
    mod.register(None)
    from gateway.config import PlatformConfig
    from gateway.platforms.qqbot.adapter import QQAdapter

    # Gateway-dependent patches activate during the real adapter lifecycle,
    # before its first message. __new__ alone skips that initialization.
    adapter = QQAdapter(PlatformConfig(extra={
        "app_id": "test-app", "client_secret": "test-secret",
    }))
    from gateway.platforms.qqbot.keyboards import (
        ApprovalRequest,
        parse_approval_button_data,
    )

    adapter._chat_type_map = {}
    assert adapter._guess_chat_type("B279C1A461933B21DAFEE3263B8854A6") == "group"
    assert parse_approval_button_data(
        "approve:agent:main:qqbot:c2c:user:allow-session"
    ) == ("agent:main:qqbot:c2c:user", "allow-session")
    assert QQAdapter._APPROVAL_BUTTON_TO_CHOICE["allow-session"] == "session"
    face_message = '<faceType=1,faceId="333",ext="x"><faceType=1,faceId="333">'
    normalized = QQAdapter._strip_at_mention(face_message)
    assert normalized == "用户在群里 @ 了你，并发送了 2 个 QQ 表情。请根据上下文做简短回应。"
    mixed = QQAdapter._strip_at_mention('@Momo 你好 <faceType=1,faceId="333">')
    assert mixed == "你好 <faceType=1,faceId=\"333\">"

    mod._patch_group_config_interactions(DummyInteractionAdapter)
    interaction_adapter = DummyInteractionAdapter()

    async def capture_ack(interaction_id, code=0, data=None):
        interaction_adapter.acks.append((interaction_id, code, data))

    interaction_adapter._acknowledge_interaction = capture_ack
    await interaction_adapter._on_interaction(
        {"id": "cfg-query", "group_openid": "group-openid", "data": {"type": 2001}}
    )
    assert interaction_adapter.acks[0][0:2] == ("cfg-query", 0)
    assert interaction_adapter.acks[0][2]["claw_cfg"]["require_mention"] == "always"
    assert interaction_adapter.acks[0][2]["claw_cfg"]["claw_type"] == "openclaw"
    await interaction_adapter._on_interaction(
        {
            "id": "cfg-update",
            "group_openid": "group-openid",
            "data": {"type": 2002, "resolved": {"claw_cfg": {"require_mention": "mention"}}},
        }
    )
    assert interaction_adapter.acks[1][2]["claw_cfg"]["require_mention"] == "mention"
    await interaction_adapter._on_interaction({"id": "button", "data": {"type": 11}})
    assert interaction_adapter.delegated == [{"id": "button", "data": {"type": 11}}]

    mod._patch_group_config_interactions(DummyAckAdapter)
    ack_adapter = DummyAckAdapter()
    claw_cfg = {
        "claw_cfg": {
            "channel_type": "qqbot",
            "claw_type": "openclaw",
            "require_mention": "always",
        }
    }
    await ack_adapter._acknowledge_interaction("interaction-wire", 0, claw_cfg)
    ack_path, ack_options = ack_adapter._http_client.calls[0]
    assert ack_path.endswith("/interactions/interaction-wire")
    assert ack_options["json"] == {"code": 0, "data": claw_cfg}
    assert ack_options["headers"]["Authorization"] == "QQBot test-token"

    directory_type = mod._lookup_channel_directory_type("B279C1A461933B21DAFEE3263B8854A6")
    assert directory_type == "group", directory_type

    mod._patch_group_message_create_event(DummyDispatchAdapter)
    dispatch_adapter = DummyDispatchAdapter()
    await dispatch_adapter._on_message(
        "GROUP_MESSAGE_CREATE",
        {
            "id": "msg-0",
            "content": "普通上下文",
            "group_openid": "group-openid",
            "author": {"member_openid": "member-a"},
        },
    )
    assert dispatch_adapter.events == []
    dispatch_adapter._dispatch_payload(
        {
            "op": 0,
            "t": "GROUP_MESSAGE_CREATE",
            "d": {
                "id": "msg-1",
                "content": "<@bot-openid> hello",
                "group_openid": "group-openid",
                "author": {"member_openid": "member-b"},
                "mentions": [{"id": "bot-openid", "bot": True, "is_you": True}],
            },
        }
    )
    await dispatch_adapter.created[0]
    assert dispatch_adapter.events[0][0] == "GROUP_AT_MESSAGE_CREATE"
    assert "普通上下文" in dispatch_adapter.events[0][1]["_qqbot_channel_context"]

    ignored_adapter = DummyDispatchAdapter()
    await ignored_adapter._on_message("GROUP_MESSAGE_CREATE", {"id": "msg-2", "content": "hello"})
    assert ignored_adapter.events == []

    other_mention_adapter = DummyDispatchAdapter()
    await other_mention_adapter._on_message(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "msg-other-at",
            "content": "owner test<@owner-openid>",
            "group_openid": "group-openid",
            "author": {"member_openid": "member-c"},
            "mentions": [{"id": "owner-openid", "bot": False, "is_you": False}],
        },
    )
    assert other_mention_adapter.events == []

    old_env = {
        key: os.environ.get(key)
        for key in (
            "QQBOT_GROUP_CONTEXT_MESSAGES",
            "QQBOT_GROUP_CONTEXT_CHARS",
            "QQBOT_GROUP_CONTEXT_SUMMARY_CHARS",
        )
    }
    os.environ["QQBOT_GROUP_CONTEXT_MESSAGES"] = "3"
    os.environ["QQBOT_GROUP_CONTEXT_CHARS"] = "500"
    os.environ["QQBOT_GROUP_CONTEXT_SUMMARY_CHARS"] = "200"
    try:
        compact_adapter = DummyDispatchAdapter()
        for idx in range(6):
            await compact_adapter._on_message(
                "GROUP_MESSAGE_CREATE",
                {
                    "id": f"msg-c{idx}",
                    "content": f"普通上下文{idx}",
                    "group_openid": "compact-group",
                    "author": {"member_openid": f"member-{idx % 2}"},
                },
            )
        await compact_adapter._on_message(
            "GROUP_MESSAGE_CREATE",
            {
                "id": "msg-c-at",
                "content": "<@bot-openid> hello",
                "group_openid": "compact-group",
                "author": {"member_openid": "member-at"},
                "mentions": [{"id": "bot-openid", "bot": True, "is_you": True}],
            },
        )
        compact_context = compact_adapter.events[0][1]["_qqbot_channel_context"]
        assert "[Recent group messages - compacted]" in compact_context
        assert "Earlier messages compacted: 3" in compact_context
        assert "普通上下文3" in compact_context
        assert "普通上下文5" in compact_context
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    mod._patch_group_channel_context(DummyHandleAdapter)
    handle_adapter = DummyHandleAdapter()
    event = DummyEvent({"_qqbot_channel_context": "[Recent group messages]\n[member-a] 普通上下文"})
    await handle_adapter.handle_message(event)
    assert event.channel_context == "[Recent group messages]\n[member-a] 普通上下文"

    # Codex records exact request scopes on the existing Gateway queue. QQ must
    # render every scope and return the missing allow-session vocabulary.
    choice_status = mod._patch_codex_approval_choices(DummyChoiceAdapter)
    choice_status_again = mod._patch_codex_approval_choices(DummyChoiceAdapter)
    assert "approval choices sender patched" in choice_status
    assert "already patched" in choice_status_again

    from tools import approval as approval_tools

    choice_session = "agent:main:qqbot:c2c:user-openid"
    choice_entry = approval_tools._ApprovalEntry(
        {
            "command": "curl https://example.invalid",
            "codex_approval_choices": ["once", "session", "always", "deny"],
        }
    )
    with approval_tools._lock:
        approval_tools._gateway_queues[choice_session] = [choice_entry]
    choice_adapter = DummyChoiceAdapter()
    try:
        choice_result = await choice_adapter.send_approval_request(
            "user-openid",
            ApprovalRequest(
                session_key=choice_session,
                title="Execute this command?",
                command_preview="curl https://example.invalid",
            ),
            reply_to="message-id",
        )
    finally:
        with approval_tools._lock:
            approval_tools._gateway_queues.pop(choice_session, None)
    assert choice_result.success
    assert choice_adapter.delegated == []
    rows = choice_adapter.sent[0][2]["content"]["rows"]
    buttons = [button for row in rows for button in row["buttons"]]
    labels = [button["render_data"]["label"] for button in buttons]
    assert labels == [
        "✅ 本次允许",
        "🕒 会话允许",
        "⭐ 始终允许同类",
        "❌ 拒绝",
    ]
    assert buttons[1]["action"]["data"].endswith(":allow-session")
    assert len(rows) == 2

    group_token = "qq-approval-v1.test-token"
    choice_adapter._qq_shared_approval_tokens = {
        group_token: {"session_key": choice_session}
    }
    with approval_tools._lock:
        approval_tools._gateway_queues[choice_session] = [choice_entry]
    try:
        await choice_adapter.send_approval_request(
            "group-openid",
            ApprovalRequest(
                session_key=group_token,
                title="Execute this command?",
            ),
        )
    finally:
        with approval_tools._lock:
            approval_tools._gateway_queues.pop(choice_session, None)
    group_rows = choice_adapter.sent[1][2]["content"]["rows"]
    group_buttons = [
        button for row in group_rows for button in row["buttons"]
    ]
    assert group_buttons[1]["action"]["data"] == (
        f"approve:{group_token}:allow-session"
    )

    # Shared group sessions omit user_id from the session key. Bind the button
    # to the initiating user's ContextVar via an opaque, single-use token.
    approval_patch_status = mod._patch_shared_group_approval_owners(
        DummyApprovalAdapter
    )
    approval_patch_status_again = mod._patch_shared_group_approval_owners(
        DummyApprovalAdapter
    )
    assert "approval sender patched" in approval_patch_status
    assert "already patched" in approval_patch_status_again

    # Newer Gateway code passes allow_session even when the installed adapter
    # is still the legacy implementation. The wrapper must accept and safely
    # omit it for that old original method.
    legacy_direct = DummyApprovalAdapter()
    legacy_result = await legacy_direct.send_exec_approval(
        "user-openid",
        "echo legacy",
        "agent:main:qqbot:c2c:user-openid",
        allow_session=False,
    )
    assert legacy_result.success
    assert legacy_direct.sent[0][2] == "agent:main:qqbot:c2c:user-openid"

    # Current adapters implement allow_session and must receive its exact
    # value rather than having the compatibility wrapper discard it.
    current_status = mod._patch_shared_group_approval_owners(
        DummyCurrentApprovalAdapter
    )
    assert "approval sender patched" in current_status
    current_direct = DummyCurrentApprovalAdapter()
    current_result = await current_direct.send_exec_approval(
        "user-openid",
        "echo current",
        "agent:main:qqbot:c2c:user-openid",
        allow_permanent=False,
        allow_session=False,
    )
    assert current_result.success
    assert current_direct.sent[0][5:8] == (False, False, False)

    from gateway.session_context import clear_session_vars, set_session_vars

    approval_adapter = DummyApprovalAdapter()
    shared_session = "agent:main:qqbot:group:group-openid"
    session_tokens = set_session_vars(
        platform="qqbot",
        chat_id="group-openid",
        user_id="requester-openid",
        session_key=shared_session,
    )
    try:
        approval_result = await approval_adapter.send_exec_approval(
            "group-openid",
            "computer_use app=com.apple.Notes",
            shared_session,
            "Allow Computer Use to use Notes?",
            allow_session=True,
        )
    finally:
        clear_session_vars(session_tokens)
    assert approval_result.success
    public_token = approval_adapter.sent[0][2]
    assert public_token.startswith("qq-approval-v1.")
    assert shared_session not in public_token

    import tools.approval

    resolved = []
    original_resolve = tools.approval.resolve_gateway_approval
    tools.approval.resolve_gateway_approval = (
        lambda session_key, choice: resolved.append((session_key, choice)) or 1
    )
    try:
        await approval_adapter._default_interaction_dispatch(
            SimpleNamespace(
                button_data=f"approve:{public_token}:allow-once",
                operator_openid="other-member",
                group_openid="group-openid",
                guild_id="",
            )
        )
        assert resolved == []
        await approval_adapter._default_interaction_dispatch(
            SimpleNamespace(
                button_data=f"approve:{public_token}:allow-once",
                operator_openid="requester-openid",
                group_openid="group-openid",
                guild_id="",
            )
        )
        assert resolved == [(shared_session, "once")]
        # The nonce is consumed: a stale/double click cannot approve anything.
        await approval_adapter._default_interaction_dispatch(
            SimpleNamespace(
                button_data=f"approve:{public_token}:allow-once",
                operator_openid="requester-openid",
                group_openid="group-openid",
                guild_id="",
            )
        )
        assert resolved == [(shared_session, "once")]

        session_tokens = set_session_vars(
            platform="qqbot",
            chat_id="group-openid",
            user_id="requester-openid",
            session_key=shared_session,
        )
        try:
            await approval_adapter.send_exec_approval(
                "group-openid",
                "python -m pytest",
                shared_session,
                "Allow this command for the session?",
            )
        finally:
            clear_session_vars(session_tokens)
        session_public_token = approval_adapter.sent[1][2]
        await approval_adapter._default_interaction_dispatch(
            SimpleNamespace(
                button_data=(
                    f"approve:{session_public_token}:allow-session"
                ),
                operator_openid="requester-openid",
                group_openid="group-openid",
                guild_id="",
            )
        )
        assert resolved == [
            (shared_session, "once"),
            (shared_session, "session"),
        ]
    finally:
        tools.approval.resolve_gateway_approval = original_resolve

    typed_status = mod._patch_shared_group_typed_approvals(DummySlashCommands)
    typed_status_again = mod._patch_shared_group_typed_approvals(
        DummySlashCommands
    )
    assert "_handle_approve_command patched" in typed_status
    assert "already patched" in typed_status_again

    # Create a new pending approval because the button test consumed the first
    # token and its associated requester record.
    session_tokens = set_session_vars(
        platform="qqbot",
        chat_id="group-openid",
        user_id="requester-openid",
        session_key=shared_session,
    )
    try:
        await approval_adapter.send_exec_approval(
            "group-openid",
            "computer_use app=com.apple.Notes",
            shared_session,
        )
    finally:
        clear_session_vars(session_tokens)

    slash_commands = DummySlashCommands()
    intruder_event = SimpleNamespace(
        source=SimpleNamespace(
            platform="qqbot",
            user_id="other-member",
            session_key=shared_session,
        )
    )
    owner_event = SimpleNamespace(
        source=SimpleNamespace(
            platform="qqbot",
            user_id="requester-openid",
            session_key=shared_session,
        )
    )
    intruder_reply = await slash_commands._handle_approve_command(intruder_event)
    assert "Only the member" in intruder_reply
    assert slash_commands.handled == []
    assert await slash_commands._handle_approve_command(owner_event) == "approved"
    assert slash_commands.handled == [("approve", "requester-openid")]
    # Once consumed, even the prior owner cannot use a typed command to affect
    # a later or unrelated pending queue item.
    owner_reply = await slash_commands._handle_deny_command(owner_event)
    assert "Only the member" in owner_reply

    dummy = DummyAdapter()
    result = await mod._send_plain_text(
        dummy,
        "group",
        "B279C1A461933B21DAFEE3263B8854A6",
        "**hello**",
    )
    assert result.success
    method, request_path, body = dummy.calls[0]
    assert method == "POST"
    assert request_path == "/v2/groups/B279C1A461933B21DAFEE3263B8854A6/messages"
    assert body["msg_type"] == 0
    assert body["content"] == "**hello**"
    print("adapter_guess_B279=group")
    print("emoji_only_normalized=2")
    print("group_config_interaction_ack=true")
    print("directory_type_B279=group")
    print("group_message_create_context=recent")
    print("approval_session_choice=true")
    print("approval_complete_keyboard=true")
    print("approval_session_button_roundtrip=true")
    print("shared_group_approval_owner_bound=true")
    print("shared_group_approval_nonce_single_use=true")
    print("shared_group_typed_approval_owner_bound=true")
    print("plain_path=" + request_path)
    print("plain_msg_type=0")


anyio.run(main)
