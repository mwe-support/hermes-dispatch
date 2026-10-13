"""Guard QQ raw intake, normalized authorization and outbound REST calls."""
import functools
import os
from pathlib import Path

from .group_invite_observer import capture
from .group_invite_trust import Trust

GROUP_MESSAGES = {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}
LIFECYCLE = {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"}


def group_event(event, data):
    return (event in GROUP_MESSAGES or
            (event == "INTERACTION_CREATE" and isinstance(data, dict) and
             (data.get("group_openid") or data.get("chat_type") in (1, "1"))))


def trust(adapter):
    return adapter._qq_invitation_trust


def blocked(adapter, event, data):
    guard = trust(adapter)
    return (guard.mode not in {"disabled", "observe"} and group_event(event, data) and
            not guard.allowed(data.get("group_openid") if isinstance(data, dict) else None))


def install(adapter_class, authorization_class=None):
    if authorization_class is not None:
        install_authorization(authorization_class)
    if getattr(adapter_class.__init__, "_qq_invitation_guard", False):
        return
    original_init = adapter_class.__init__
    original_dispatch = adapter_class._dispatch_payload
    original_message = adapter_class._on_message
    original_handle = adapter_class.handle_message
    original_interaction = adapter_class._on_interaction
    original_group = adapter_class._is_group_allowed
    original_api = adapter_class._api_request

    @functools.wraps(original_init)
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._qq_invitation_trust = Trust(
            Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser(), self._app_id,
            os.getenv("QQ_GROUP_INVITE_TRUST_MODE", "disabled"))

    @functools.wraps(original_dispatch)
    def dispatch(self, payload):
        guard = trust(self)
        if isinstance(payload, dict) and payload.get("op") == 0:
            event, data = payload.get("t"), payload.get("d")
            if guard.mode == "observe":
                try:
                    capture(self, payload)
                except Exception:
                    pass
            if event in LIFECYCLE and isinstance(data, dict) and guard.mode != "disabled":
                guard.lifecycle(event, data)
                if guard.mode != "observe":
                    return None
            if blocked(self, event, data):
                return None
        return original_dispatch(self, payload)

    @functools.wraps(original_message)
    async def message(self, event, data):
        if not blocked(self, event, data):
            return await original_message(self, event, data)

    @functools.wraps(original_interaction)
    async def interaction(self, data):
        if not blocked(self, "INTERACTION_CREATE", data):
            return await original_interaction(self, data)

    @functools.wraps(original_handle)
    async def handle(self, event):
        source = getattr(event, "source", None)
        if (trust(self).mode not in {"disabled", "observe"} and
                getattr(source, "chat_type", None) == "group" and
                not trust(self).allowed(getattr(source, "chat_id", None))):
            return None
        return await original_handle(self, event)

    @functools.wraps(original_group)
    def group_allowed(self, group, user):
        guard = trust(self)
        return guard.allowed(group) if guard.mode not in {"disabled", "observe"} else original_group(self, group, user)

    @functools.wraps(original_api)
    async def api(self, method, path, *args, **kwargs):
        if (trust(self).mode not in {"disabled", "observe"} and str(path).startswith("/v2/groups/")
                and not trust(self).allowed(str(path).split("/")[3])):
            raise PermissionError("qq_group_not_authorized")
        return await original_api(self, method, path, *args, **kwargs)

    # Hook ordering is declared in the manifest: guards enclose snapshot/context wrappers.
    initialize._qq_invitation_guard = True
    adapter_class.__init__ = initialize
    adapter_class._dispatch_payload = dispatch
    adapter_class._on_message = message
    adapter_class._on_interaction = interaction
    adapter_class.handle_message = handle
    adapter_class._is_group_allowed = group_allowed
    adapter_class._api_request = api
    original_capture = getattr(adapter_class, "_message_snapshot_capture_raw", None)
    if callable(original_capture):
        @functools.wraps(original_capture)
        async def snapshot(self, event, data):
            if not blocked(self, event, data):
                return await original_capture(self, event, data)
        adapter_class._message_snapshot_capture_raw = snapshot


def install_authorization(authorization_class):
    original = authorization_class._is_user_authorized
    if getattr(original, "_qq_invitation_authorization_guard", False):
        return

    @functools.wraps(original)
    def authorized(self, source, *args, **kwargs):
        platform = getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None))
        if platform == "qqbot" and getattr(source, "chat_type", None) == "group":
            adapter = self._adapter_for_source(source)
            guard = getattr(adapter, "_qq_invitation_trust", None)
            if guard is not None and guard.mode not in {"disabled", "observe"}:
                return guard.allowed(source.chat_id)
            if guard is None and os.getenv("QQ_GROUP_INVITE_TRUST_MODE", "disabled") not in {"disabled", "observe"}:
                return False
        return original(self, source, *args, **kwargs)

    authorized._qq_invitation_authorization_guard = True
    authorization_class._is_user_authorized = authorized
