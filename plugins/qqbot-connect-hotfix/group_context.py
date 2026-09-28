"""QQ full-group-message routing and context buffering."""

from __future__ import annotations

import logging
import os
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


def patch_group_message_create_event(QQAdapter):
    dispatch = QQAdapter._dispatch_payload
    on_message = QQAdapter._on_message
    if getattr(on_message, "_qqbot_group_message_create_wrapped", False):
        return

    def _dispatch_payload(self, payload: dict[str, Any]) -> None:
        if isinstance(payload, dict) and payload.get("op") == 0 and payload.get("t") == "GROUP_MESSAGE_CREATE":
            self._create_task(self._on_message("GROUP_MESSAGE_CREATE", payload.get("d")))
            return
        return dispatch(self, payload)

    async def _on_message(self, event_type: str, d: Any) -> None:
        if event_type == "GROUP_MESSAGE_CREATE":
            # This wrapper intentionally stops passive group events before
            # Hermes normalizes/routes them. Give persistent snapshot plugins
            # an explicit raw-event hook so capture does not depend on whether
            # that plugin was loaded before or after this compatibility layer.
            capture_raw = getattr(self, "_message_snapshot_capture_raw", None)
            if callable(capture_raw):
                try:
                    await capture_raw(event_type, d)
                except Exception:
                    logger.exception(
                        "qqbot-connect-hotfix: passive GROUP_MESSAGE_CREATE snapshot failed"
                    )
            context = group_context_block(self, d)
            if should_handle_group_message_create(self, d):
                routed = dict(d) if isinstance(d, dict) else d
                if isinstance(routed, dict) and context:
                    routed["_qqbot_channel_context"] = context
                result = await on_message(self, "GROUP_AT_MESSAGE_CREATE", routed)
                remember_group_message(self, d)
                return result
            remember_group_message(self, d)
            logger.debug("qqbot-connect-hotfix: buffered GROUP_MESSAGE_CREATE without bot mention marker")
            return
        if event_type == "GROUP_AT_MESSAGE_CREATE" and isinstance(d, dict):
            # QQ can label a message GROUP_AT_MESSAGE_CREATE when it mentions
            # *any* group member, not only this bot. Structured mentions are
            # authoritative: routing an ``is_you=false`` mention would wake
            # the agent merely because somebody addressed the group owner.
            if not group_at_mentions_this_bot(d):
                remember_group_message(self, d)
                logger.debug(
                    "qqbot-connect-hotfix: buffered GROUP_AT_MESSAGE_CREATE "
                    "whose structured mentions do not target this bot"
                )
                return
            context = group_context_block(self, d)
            if context:
                routed = dict(d)
                routed["_qqbot_channel_context"] = context
                return await on_message(self, event_type, routed)
        return await on_message(self, event_type, d)

    _dispatch_payload.__name__ = getattr(dispatch, "__name__", "_dispatch_payload")
    _dispatch_payload.__qualname__ = getattr(dispatch, "__qualname__", "QQAdapter._dispatch_payload")
    _dispatch_payload._qqbot_group_message_create_dispatch_wrapped = True
    _on_message.__name__ = getattr(on_message, "__name__", "_on_message")
    _on_message.__qualname__ = getattr(on_message, "__qualname__", "QQAdapter._on_message")
    _on_message._qqbot_group_message_create_wrapped = True
    QQAdapter._dispatch_payload = _dispatch_payload
    QQAdapter._on_message = _on_message
    logger.info("qqbot-connect-hotfix: patched QQAdapter GROUP_MESSAGE_CREATE routing")


def patch_group_channel_context(QQAdapter):
    original = QQAdapter.handle_message
    if getattr(original, "_qqbot_group_channel_context_wrapped", False):
        return

    async def handle_message(self, event):
        raw = getattr(event, "raw_message", None)
        if isinstance(raw, dict):
            # Full-group QQ payloads can retain the self @ chip before/after
            # a command. Strip only verified self mentions, before dispatch;
            # other mentions and ordinary conversation text remain intact.
            if getattr(getattr(event, "source", None), "chat_type", None) == "group":
                text = event.text or ""
                mentions = raw.get("mentions")
                for mention in mentions if isinstance(mentions, list) else []:
                    if isinstance(mention, dict) and mention.get("is_you") is True and mention.get("id"):
                        text = text.replace(f"<@{mention['id']}>", " ")
                if text.lstrip().startswith("/"):
                    event.text = text.strip()
            context = raw.pop("_qqbot_channel_context", None)
            if context and not getattr(event, "channel_context", None):
                event.channel_context = str(context)
        return await original(self, event)

    handle_message.__name__ = getattr(original, "__name__", "handle_message")
    handle_message.__qualname__ = getattr(original, "__qualname__", "QQAdapter.handle_message")
    handle_message._qqbot_group_channel_context_wrapped = True
    QQAdapter.handle_message = handle_message
    logger.info("qqbot-connect-hotfix: patched QQAdapter group channel context injection")


def should_handle_group_message_create(self, d: Any) -> bool:
    mode = os.getenv("QQBOT_GROUP_MESSAGE_CREATE_MODE", "mention").strip().lower()
    if mode in {"all", "open", "true", "1", "yes"}:
        return True
    if mode in {"off", "disabled", "false", "0", "no"} or not isinstance(d, dict):
        return False
    return group_at_mentions_this_bot(d, fallback=False)


def group_at_mentions_this_bot(d: Any, *, fallback: bool = True) -> bool:
    """Return QQ's structured self-mention decision.

    ``mentions[].is_you`` is present on current QQ group payloads and avoids
    confusing @owner/@member messages with @bot. Older GROUP_AT payloads may
    omit ``mentions`` entirely, so the GROUP_AT route keeps its historical
    behavior only in that absence. GROUP_MESSAGE_CREATE never uses that
    fallback because its purpose is passive full-group delivery.
    """
    if not isinstance(d, dict):
        return False
    mentions = d.get("mentions")
    if not isinstance(mentions, list):
        return fallback
    return any(
        isinstance(mention, dict) and mention.get("is_you") is True
        for mention in mentions
    )


def contains_mention_marker(value: Any, app_id: str = "") -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in {"is_you", "at_bot", "mentioned_bot"} and item is True:
                return True
        for key in ("type", "elem_type", "element_type", "msg_type"):
            if str(value.get(key) or "").strip().lower() in {"at", "mention", "at_user"}:
                return True
        if any(str(value.get(k) or "").strip() == app_id for k in ("id", "user_id", "bot_id", "app_id")):
            return bool(app_id)
        return any(contains_mention_marker(v, app_id) for v in value.values())
    if isinstance(value, list):
        return any(contains_mention_marker(v, app_id) for v in value)
    return False


def remember_group_message(self, d: Any) -> None:
    line = _format_group_message_line(d)
    if not line or not isinstance(d, dict):
        return
    group_openid = str(d.get("group_openid") or "")
    if not group_openid:
        return
    buffers = getattr(self, "_qqbot_group_context_buffers", None)
    if buffers is None:
        buffers = {}
        self._qqbot_group_context_buffers = buffers
    maxlen = group_context_buffer_limit()
    buffer = buffers.get(group_openid)
    if buffer is None or getattr(buffer, "maxlen", None) != maxlen:
        buffer = deque(list(buffer or [])[-maxlen:], maxlen=maxlen)
        buffers[group_openid] = buffer
    buffer.append(line)


def group_context_block(self, d: Any) -> str:
    if not isinstance(d, dict):
        return ""
    group_openid = str(d.get("group_openid") or "")
    if not group_openid:
        return ""
    buffers = getattr(self, "_qqbot_group_context_buffers", {})
    lines = list(buffers.get(group_openid, []))
    return render_group_context(lines)


def render_group_context(lines: list[str]) -> str:
    if not lines:
        return ""
    max_messages = group_context_limit()
    plain = _context_block("[Recent group messages]", lines[-max_messages:])
    if len(lines) <= max_messages and len(plain) <= group_context_char_limit():
        return plain
    return compact_group_context(lines)


def compact_group_context(lines: list[str]) -> str:
    max_messages = group_context_limit()
    max_chars = group_context_char_limit()
    older = lines[:-max_messages]
    recent = lines[-max_messages:]
    summary = _compact_summary(older) if older else ""
    header = "[Recent group messages - compacted]"
    parts = [header]
    if summary:
        parts.append("[Compacted earlier messages]\n" + summary)
    parts.append("[Recent messages]")
    prefix = "\n".join(parts) + "\n"
    return prefix + "\n".join(_fit_recent_lines(recent, max(160, max_chars - len(prefix))))


def group_context_limit() -> int:
    return _bounded_int("QQBOT_GROUP_CONTEXT_MESSAGES", default=20, minimum=1, maximum=100)


def group_context_buffer_limit() -> int:
    return _bounded_int("QQBOT_GROUP_CONTEXT_BUFFER_MESSAGES", default=100, minimum=1, maximum=300)


def group_context_char_limit() -> int:
    return _bounded_int("QQBOT_GROUP_CONTEXT_CHARS", default=4000, minimum=500, maximum=12000)


def group_context_summary_chars() -> int:
    return _bounded_int("QQBOT_GROUP_CONTEXT_SUMMARY_CHARS", default=1200, minimum=200, maximum=4000)


def _format_group_message_line(d: Any) -> str:
    if not isinstance(d, dict):
        return ""
    content = str(d.get("content") or "").strip()
    if not content:
        return ""
    author = d.get("author") if isinstance(d.get("author"), dict) else {}
    member_openid = str(author.get("member_openid") or "member")
    timestamp = str(d.get("timestamp") or "").strip()
    prefix = f"[{member_openid}]"
    if timestamp:
        prefix = f"[{timestamp}] {prefix}"
    return f"{prefix} {content}"


def _context_block(header: str, lines: list[str]) -> str:
    return header + "\n" + "\n".join(lines)


def _compact_summary(lines: list[str]) -> str:
    participants = []
    for line in lines:
        marker = _participant_marker(line)
        if marker and marker not in participants:
            participants.append(marker)
    samples = [_clip_line(line, 160) for line in lines[-3:]]
    summary_lines = [f"- Earlier messages compacted: {len(lines)}"]
    if participants:
        summary_lines.append("- Participants: " + ", ".join(participants[:8]))
    if samples:
        summary_lines.append("- Latest earlier messages:")
        summary_lines.extend(f"  {sample}" for sample in samples)
    summary = "\n".join(summary_lines)
    limit = group_context_summary_chars()
    return summary if len(summary) <= limit else summary[: limit - 3] + "..."


def _fit_recent_lines(lines: list[str], budget: int) -> list[str]:
    selected = []
    used = 0
    for line in reversed(lines):
        clipped = _clip_line(line, 320)
        extra = len(clipped) + (1 if selected else 0)
        if selected and used + extra > budget:
            break
        selected.append(clipped)
        used += extra
    if not selected and lines:
        selected.append(_clip_line(lines[-1], max(40, budget - 3)))
    return list(reversed(selected))


def _participant_marker(line: str) -> str:
    start = line.find("[")
    end = line.find("]", start + 1)
    if start < 0 or end < 0:
        return ""
    marker = line[start + 1 : end].strip()
    return marker if marker and "T" not in marker[:12] else ""


def _clip_line(line: str, limit: int) -> str:
    clean = " ".join(str(line).split())
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def _bounded_int(name: str, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(maximum, value))
