"""Native Hermes startup/adapter/auth integration; requires Hermes on PYTHONPATH."""
import asyncio
import builtins
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import patch

path = Path(__file__).with_name("__init__.py")
spec = importlib.util.spec_from_file_location("qq_invite_startup_test", path, submodule_search_locations=[str(path.parent)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
ACTOR, USER, GROUP, UNKNOWN = [c * 32 for c in "ABCD"]


async def main():
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, HERMES_HOME=directory, QQ_GROUP_INVITE_TRUST_MODE="enforce"):
        original_import = builtins.__import__
        def safe_import(name, *args, **kwargs):
            assert name != "gateway.run", "invitation feature caused discovery import deadlock"
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", safe_import):
            plugin.register(None)
        policy = Path(directory) / "qq-group-invite-trust.json"
        policy.write_text(json.dumps(dict(app_id="test-app", trusted_inviters=[ACTOR])))
        policy.chmod(0o600)
        from gateway.config import PlatformConfig, Platform
        from gateway.platforms.qqbot.adapter import QQAdapter
        adapter = QQAdapter(PlatformConfig(extra={"app_id": "test-app", "client_secret": "test-secret", "group_policy": "open"}))
        assert adapter._qq_invitation_trust.mode == "enforce", "first adapter missed guards"
        from gateway.run import GatewayRunner
        assert getattr(GatewayRunner._is_user_authorized, "_qq_invitation_authorization_guard", False)
        adapter._dispatch_payload(dict(op=0, t="GROUP_ADD_ROBOT", d=dict(
            group_openid=GROUP, op_member_openid=ACTOR, timestamp=time.time())))
        runner = object.__new__(GatewayRunner)
        runner._adapter_for_source = lambda source: adapter
        source = types.SimpleNamespace(platform=Platform.QQBOT, chat_type="group", chat_id=GROUP, user_id=USER)
        assert runner._is_user_authorized(source), "native Gateway denied a non-inviter member"
        source.chat_id = UNKNOWN
        assert not runner._is_user_authorized(source), "native Gateway accepted unknown group"
        await adapter._on_message("GROUP_AT_MESSAGE_CREATE", dict(id="message", group_openid=UNKNOWN, content="do not execute"))
        assert not adapter._last_msg_id, "denied intake changed native message state"
        try:
            await adapter._api_request("POST", f"/v2/groups/{UNKNOWN}/messages", {"content": "do not send"})
            raise AssertionError("denied group reached native HTTP path")
        except PermissionError:
            pass
        with patch.dict(os.environ, QQ_GROUP_INVITE_TRUST_MODE="observe"):
            observed = QQAdapter(PlatformConfig(extra={"app_id": "test-app", "client_secret": "test-secret", "group_policy": "open"}))
            assert observed._is_group_allowed(UNKNOWN, USER), "observation changed native group policy"

asyncio.run(main())
print("Native first-adapter invitation guard, Gateway all-member authorization, denial before HTTP, discovery safety: PASS")
