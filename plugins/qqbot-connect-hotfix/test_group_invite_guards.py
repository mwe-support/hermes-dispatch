"""Run: python3 plugins/qqbot-connect-hotfix/test_group_invite_guards.py."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import patch

def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module

root = Path(__file__).resolve().parent
package("invite_test", root)
package("snapshot_test", root.parent / "message-snapshot-store")
package("context_test", root.parent / "qqbot-connect-hotfix")
guards = importlib.import_module("invite_test.group_invite_guards")
capture = importlib.import_module("snapshot_test.capture")
context = importlib.import_module("context_test.group_context")
ACTOR, USER, GROUP, UNKNOWN = [c * 32 for c in "ABCD"]


class Store:
    config = types.SimpleNamespace(media_storage="link")
    writes = 0
    def record_raw(self, **kwargs):
        self.writes += 1
        return self.writes


class Adapter:
    def __init__(self):
        self._app_id = "test-app"
        self.calls = []
        self.tasks = []
    def _dispatch_payload(self, payload):
        self.calls.append("raw")
    def _create_task(self, coro):
        self.tasks.append(coro)
    async def _on_message(self, event, data):
        self.calls.append("model")
    async def handle_message(self, event):
        self.calls.append("handled")
    async def _on_interaction(self, data):
        self.calls.append("interaction")
    def _is_group_allowed(self, group, user):
        return True
    async def _api_request(self, method, path, *args, **kwargs):
        self.calls.append("http")
        return {"ok": True}


class Gateway:
    def __init__(self, adapter):
        self.adapter = adapter
    def _adapter_for_source(self, source):
        return self.adapter
    def _is_user_authorized(self, source, **kwargs):
        return source.platform != "other"  # Simulates a permissive legacy/group/global policy.


async def main():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, HERMES_HOME=directory, QQ_GROUP_INVITE_TRUST_MODE="enforce"):
        home = Path(directory)
        policy = home / "qq-group-invite-trust.json"
        policy.write_text(json.dumps(dict(app_id="test-app", trusted_inviters=[ACTOR])))
        policy.chmod(0o600)
        store = Store()
        capture.patch_qq_snapshot_capture(Adapter, store)
        context.patch_group_message_create_event(Adapter)
        guards.install(Adapter, Gateway)
        initialize = Adapter.__init__
        guards.install(Adapter, Gateway)
        assert Adapter.__init__ is initialize
        adapter = Adapter()
        source = lambda group, user=USER: types.SimpleNamespace(platform="qqbot", chat_type="group", chat_id=group, user_id=user)
        gateway = Gateway(adapter)
        def raw(group, actor=USER):
            return dict(id="message", group_openid=group, author=dict(member_openid=actor), content="unauthorized private text")
        for kind in ["GROUP_MESSAGE_CREATE", "GROUP_AT_MESSAGE_CREATE"]:
            adapter._dispatch_payload(dict(op=0, t=kind, d=raw(UNKNOWN)))
            await adapter._on_message(kind, raw(UNKNOWN))
            await adapter._message_snapshot_capture_raw(kind, raw(UNKNOWN))
        assert not adapter.calls and not adapter.tasks and store.writes == 0
        assert not getattr(adapter, "_qqbot_group_context_buffers", {})
        assert not gateway._is_user_authorized(source(UNKNOWN)), "permissive native policy bypassed trust"
        assert not Gateway(None)._is_user_authorized(source(GROUP)), "missing transport failed open"
        await adapter.handle_message(types.SimpleNamespace(source=source(UNKNOWN)))
        assert store.writes == 0 and not adapter.calls
        adapter._dispatch_payload(dict(op=0, t="GROUP_ADD_ROBOT", d=dict(group_openid=GROUP, op_member_openid=ACTOR, timestamp=time.time())))
        assert adapter._is_group_allowed(GROUP, USER), "member was incorrectly required to be an inviter"
        assert gateway._is_user_authorized(source(GROUP, USER))
        assert gateway._is_user_authorized(source(GROUP, None))
        await adapter._on_message("GROUP_AT_MESSAGE_CREATE", dict(raw(GROUP), mentions=[dict(is_you=True)]))
        assert store.writes == 1 and "model" in adapter.calls
        await adapter._on_interaction(dict(chat_type=1, group_openid=UNKNOWN))
        await adapter._on_interaction(dict(chat_type=1))
        assert "interaction" not in adapter.calls
        await adapter._on_interaction(dict(chat_type=1, group_openid=GROUP))
        assert "interaction" in adapter.calls
        for suffix in ["messages", "files", "files/prepare"]:
            try:
                await adapter._api_request("POST", f"/v2/groups/{UNKNOWN}/{suffix}", {})
                raise AssertionError("blocked group reached REST")
            except PermissionError:
                pass
        assert "http" not in adapter.calls
        await adapter._api_request("POST", f"/v2/groups/{GROUP}/messages", {})
        await adapter._api_request("POST", f"/v2/users/{USER}/messages", {})
        assert adapter.calls.count("http") == 2
        assert gateway._is_user_authorized(types.SimpleNamespace(platform="qqbot", chat_type="dm"))
        assert not gateway._is_user_authorized(types.SimpleNamespace(platform="other", chat_type="group"))
        adapter._dispatch_payload(dict(op=0, t="GROUP_DEL_ROBOT", d=dict(group_openid=GROUP, op_member_openid=USER, timestamp=time.time()+1)))
        assert not gateway._is_user_authorized(source(GROUP))
        assert not adapter._is_group_allowed(GROUP, ACTOR)
        with patch.dict(os.environ, QQ_GROUP_INVITE_TRUST_MODE="observe"):
            observed = Adapter()
            observed._dispatch_payload(dict(op=0, t="GROUP_AT_MESSAGE_CREATE", d=raw(UNKNOWN)))
            assert observed.calls == ["raw"], "observation changed intake"
        with patch.dict(os.environ, QQ_GROUP_INVITE_TRUST_MODE="typo"):
            invalid = Adapter()
            invalid._dispatch_payload(dict(op=0, t="GROUP_AT_MESSAGE_CREATE", d=raw(GROUP)))
            assert not invalid.calls

asyncio.run(main())
print("Guard checks passed: real snapshot/context wrappers, all-member access, interactions, outbound, lifecycle, DM isolation")
