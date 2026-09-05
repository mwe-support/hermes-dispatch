"""QQ outbound send compatibility patches."""

from __future__ import annotations

import functools
import contextvars
import inspect
import logging
import re
import shlex
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit
import uuid
from typing import Any, Awaitable, Callable, Dict

logger = logging.getLogger(__name__)

_EXPIRED_REPLY_WRAPPER = "_qqbot_expired_reply_fallback_wrapped"
_POST_STREAM_DELIVERY = contextvars.ContextVar("qq_post_stream_delivery", default=None)


def is_expired_reply_error(error: object) -> bool:
    """Return whether QQ rejected an expired reply message anchor.

    QQ currently reports Chinese errors such as ``msg_id已过期``.  Keep
    conservative English aliases as gateways and SDK layers sometimes render
    the same field as ``message_id`` or ``message id``.
    """

    text = str(error or "").lower()
    has_reply_id = any(
        marker in text for marker in ("msg_id", "message_id", "message id")
    )
    has_expiry = any(
        marker in text for marker in ("expired", "expire", "expiration", "过期")
    )
    return has_reply_id and has_expiry


async def _send_with_expired_reply_fallback(
    send: Callable[[object], Awaitable[Any]],
    *,
    reply_to: object,
    log_tag: str,
    send_kind: str,
):
    """Try the referenced send, then retry once without the expired anchor."""

    try:
        result = await send(reply_to)
    except Exception as exc:
        if not reply_to or not is_expired_reply_error(exc):
            raise
        logger.warning(
            "qqbot-connect-hotfix: %s reply anchor expired for %s; "
            "retrying once as a standalone message: %s",
            send_kind,
            log_tag,
            exc,
        )
        try:
            return await send(None)
        except Exception as fallback_exc:
            raise RuntimeError(
                "QQ standalone fallback failed after expired reply anchor; "
                f"fallback={fallback_exc}; original={exc}"
            ) from fallback_exc

    if (
        reply_to
        and not getattr(result, "success", True)
        and is_expired_reply_error(getattr(result, "error", None))
    ):
        logger.warning(
            "qqbot-connect-hotfix: %s reply anchor expired for %s; "
            "retrying once as a standalone message: %s",
            send_kind,
            log_tag,
            getattr(result, "error", ""),
        )
        return await send(None)
    return result


def patch_expired_reply_fallback(QQAdapter):
    """Retry QQ text/keyboard sends once without an expired ``reply_to``.

    Wrapping the three low-level text senders covers normal chunk delivery and
    ``send_with_keyboard`` without duplicating either caller.  The keyboard
    object is passed through unchanged on the standalone retry.
    """

    patched = []

    for method_name, send_kind in (
        ("_send_c2c_text", "C2C text/keyboard"),
        ("_send_group_text", "group text/keyboard"),
    ):
        original = getattr(QQAdapter, method_name, None)
        if original is None or getattr(original, _EXPIRED_REPLY_WRAPPER, False):
            continue

        def make_text_wrapper(original_method, kind):
            @functools.wraps(original_method)
            async def wrapped(
                self,
                target_id: str,
                content: str,
                reply_to=None,
                keyboard=None,
            ):
                async def send(anchor):
                    return await original_method(
                        self,
                        target_id,
                        content,
                        anchor,
                        keyboard,
                    )

                return await _send_with_expired_reply_fallback(
                    send,
                    reply_to=reply_to,
                    log_tag=str(target_id),
                    send_kind=kind,
                )

            setattr(wrapped, _EXPIRED_REPLY_WRAPPER, True)
            return wrapped

        setattr(QQAdapter, method_name, make_text_wrapper(original, send_kind))
        patched.append(method_name)

    original_guild = getattr(QQAdapter, "_send_guild_text", None)
    if original_guild is not None and not getattr(
        original_guild, _EXPIRED_REPLY_WRAPPER, False
    ):
        @functools.wraps(original_guild)
        async def _send_guild_text(
            self,
            channel_id: str,
            content: str,
            reply_to=None,
        ):
            async def send(anchor):
                return await original_guild(self, channel_id, content, anchor)

            return await _send_with_expired_reply_fallback(
                send,
                reply_to=reply_to,
                log_tag=str(channel_id),
                send_kind="guild text",
            )

        setattr(_send_guild_text, _EXPIRED_REPLY_WRAPPER, True)
        QQAdapter._send_guild_text = _send_guild_text
        patched.append("_send_guild_text")

    if patched:
        logger.info(
            "qqbot-connect-hotfix: patched expired reply fallback for %s",
            ", ".join(patched),
        )


def patch_plain_text_retry(QQAdapter):
    original_c2c = QQAdapter._send_c2c_text
    original_group = QQAdapter._send_group_text
    if getattr(original_c2c, "_qqbot_plain_text_retry_wrapped", False):
        return

    async def _send_c2c_text(self, openid: str, content: str, reply_to=None, keyboard=None):
        try:
            return await original_c2c(self, openid, content, reply_to, keyboard)
        except RuntimeError as exc:
            if should_retry_plain_text(self, exc):
                logger.warning(
                    "qqbot-connect-hotfix: markdown C2C send failed for %s; retrying plain text: %s",
                    openid,
                    exc,
                )
                return await send_plain_text(self, "c2c", openid, content, reply_to, keyboard)
            raise

    async def _send_group_text(self, group_openid: str, content: str, reply_to=None, keyboard=None):
        try:
            return await original_group(self, group_openid, content, reply_to, keyboard)
        except RuntimeError as exc:
            if should_retry_plain_text(self, exc):
                logger.warning(
                    "qqbot-connect-hotfix: markdown group send failed for %s; retrying plain text: %s",
                    group_openid,
                    exc,
                )
                return await send_plain_text(self, "group", group_openid, content, reply_to, keyboard)
            raise

    _send_c2c_text.__name__ = getattr(original_c2c, "__name__", "_send_c2c_text")
    _send_c2c_text.__qualname__ = getattr(original_c2c, "__qualname__", "QQAdapter._send_c2c_text")
    _send_c2c_text._qqbot_plain_text_retry_wrapped = True
    _send_group_text.__name__ = getattr(original_group, "__name__", "_send_group_text")
    _send_group_text.__qualname__ = getattr(original_group, "__qualname__", "QQAdapter._send_group_text")
    _send_group_text._qqbot_plain_text_retry_wrapped = True
    QQAdapter._send_c2c_text = _send_c2c_text
    QQAdapter._send_group_text = _send_group_text
    logger.info("qqbot-connect-hotfix: patched QQAdapter text send with plain-text retry")


def should_retry_plain_text(self, exc: Exception) -> bool:
    if not getattr(self, "_markdown_support", False):
        return False
    text = str(exc).lower()
    return "invalid request" in text or "markdown" in text


async def send_plain_text(self, target_type: str, target_id: str, content: str, reply_to=None, keyboard=None):
    from gateway.platforms.base import SendResult
    from gateway.platforms.qqbot.constants import MSG_TYPE_TEXT

    msg_seq = self._next_msg_seq(reply_to or target_id)
    body: Dict[str, Any] = {
        "content": content[: self.MAX_MESSAGE_LENGTH],
        "msg_type": MSG_TYPE_TEXT,
        "msg_seq": msg_seq,
    }
    if reply_to:
        body["msg_id"] = reply_to
        body["message_reference"] = {"message_id": reply_to}
    if keyboard is not None:
        body["keyboard"] = keyboard.to_dict()

    path = f"/v2/users/{target_id}/messages" if target_type == "c2c" else f"/v2/groups/{target_id}/messages"
    data = await self._api_request("POST", path, body)
    return SendResult(
        success=True,
        message_id=str(data.get("id", uuid.uuid4().hex[:12])),
        raw_response=data,
    )


def patch_media_caption_retry(QQAdapter):
    original = QQAdapter._send_media
    if getattr(original, "_qqbot_media_caption_retry_wrapped", False):
        return

    async def _send_media(self, chat_id: str, media_source: str, file_type: int, kind: str, caption=None, reply_to=None, file_name=None):
        result = await original(self, chat_id, media_source, file_type, kind, caption, reply_to, file_name=file_name)
        if result.success or not caption:
            return result
        if "invalid request" not in str(result.error or "").lower():
            return result
        logger.warning(
            "qqbot-connect-hotfix: media send with caption failed for %s; retrying without caption: %s",
            chat_id,
            result.error,
        )
        return await original(self, chat_id, media_source, file_type, kind, None, reply_to, file_name=file_name)

    _send_media.__name__ = getattr(original, "__name__", "_send_media")
    _send_media.__qualname__ = getattr(original, "__qualname__", "QQAdapter._send_media")
    _send_media.__doc__ = getattr(original, "__doc__", None)
    _send_media._qqbot_media_caption_retry_wrapped = True
    QQAdapter._send_media = _send_media
    logger.info("qqbot-connect-hotfix: patched QQAdapter media send with caption retry")


def _validate_output_path(path: str, session_key: str = ""):
    from gateway.platforms.base import BasePlatformAdapter

    validate = BasePlatformAdapter.validate_media_delivery_path
    # Official 0.20.5 accepts only path; newer releases also translate paths
    # using session_key. Do not catch TypeError from inside path validation.
    if "session_key" in inspect.signature(validate).parameters:
        return validate(path, session_key=session_key)
    return validate(path)


def _qq_example_spans(text: str):
    # markdown-it-py already ships with Hermes' required Rich dependency.
    # Its source maps cover nested/lazy quotes and all CommonMark code blocks.
    from markdown_it import MarkdownIt
    from markdown_it.rules_inline import backtick

    offsets = [0] + [m.end() for m in re.finditer(r"\r\n?|\n", text)] + [len(text)]
    parser = MarkdownIt()
    tokens = parser.parse(text)
    spans = [(offsets[t.map[0]], offsets[t.map[1]])
             for t in tokens
             if t.type in {"fence", "code_block", "blockquote_open", "html_block"} and t.map]
    # Ask the existing inline rule for its actual source span. This respects
    # escaped openers, literal closers, HTML/link syntax and block boundaries.
    block_start = 0
    def capture_code(state, silent):
        start, count = state.pos, len(state.tokens)
        matched = backtick(state, silent)
        if not silent and len(state.tokens) > count and state.tokens[-1].type == "code_inline":
            spans.append((block_start + start, block_start + state.pos))
        return matched
    parser.inline.ruler.at("backticks", capture_code)
    for token in tokens:
        if token.type == "inline" and token.map:
            block_start, end = offsets[token.map[0]], offsets[token.map[1]]
            # Preserve character offsets through the parser's CR normalization.
            source = text[block_start:end].replace("\r\n", " \n").replace("\r", "\n")
            parser.parseInline(source)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _fence_indented_code(text: str):
    from markdown_it import MarkdownIt

    tokens = MarkdownIt().parse(text)
    offsets = [0] + [m.end() for m in re.finditer(r"\r\n?|\n", text)] + [len(text)]
    # An earlier image may disappear before Hermes strips the body. Protect
    # every top-level indented block, while retaining nested list structure.
    for token in reversed(tokens):
        if token.type == "code_block" and token.level == 0:
            start, end = offsets[token.map[0]], offsets[token.map[1]]
            fence = "`" * max(3, 1 + max((len(m[0]) for m in re.finditer(r"`+", token.content)), default=0))
            text = text[:start] + fence + "\n" + token.content + fence + "\n" + text[end:]
    return text


def _qq_output_files(text: str, session_key: str = ""):
    """Return validated output attachments separately from their display text."""
    from gateway.platforms.base import BasePlatformAdapter

    visible = text
    for start, end in _qq_example_spans(text):
        visible = visible[:start] + " " * (end - start) + visible[end:]
    existing, _ = _extract_outside_examples(BasePlatformAdapter.extract_media, text)
    seen = {
        safe for path, _voice in existing
        if (safe := _validate_output_path(path, session_key))
    }
    media = []
    links = []
    # ponytail: explicit output citations and inline download links only;
    # bare paths may be inspected source, not delivery intent.
    candidates = [
        (m.start(), m.end(), m.group(2).strip("<>"))
        for m in re.finditer(r"!?\[([^\]\n]*)\]\((<[^>\n]+>|[^\s()]+)\)", visible)
    ]
    for match in re.finditer(r":codex-file-citation\{([^{}\n]*)\}", visible):
        if any(start <= match.start() < end for start, end, _target in candidates):
            continue  # Do not replace nested citations a second time.
        try:
            attrs = dict(token.split("=", 1) for token in shlex.split(match.group(1)))
        except ValueError:
            continue
        if attrs.get("purpose") == "output" and attrs.get("path"):
            path = attrs["path"]
            candidates.append((match.start(), match.end(), quote(path, safe="/~")))
    for start, end, target in sorted(candidates):
        try:
            parsed = urlsplit(target)
        except ValueError:
            continue
        if parsed.scheme not in {"", "file"} or parsed.netloc or parsed.query or parsed.fragment:
            continue
        path = unquote(parsed.path)
        if not path.startswith(("/", "~/")) or re.search(r":\d+(?::\d+)?$", path):
            continue  # Source citations are not attachments.
        safe = _validate_output_path(path, session_key)
        if not safe or any(c in safe for c in '\n\r"'):
            continue
        tag = f'MEDIA:"{safe}"'
        if "[[audio_as_voice]]" in visible:
            tag = "[[audio_as_voice]] " + tag
        extracted, _ = BasePlatformAdapter.extract_media(tag)
        if not extracted:
            continue
        # Remove the local target too: non-streaming Hermes also scans bare
        # paths and would otherwise send the same file a second time.
        label = Path(safe).name
        # The native file card retains the exact name. Do not put filenames
        # containing Markdown syntax back into text that will be parsed again.
        links.append((start, end, label if re.fullmatch(r"[\w.-]+", label) else "attachment"))
        if safe not in seen:
            seen.add(safe)
            media.extend(extracted)
    for start, end, label in reversed(links):
        text = text[:start] + label + text[end:]
    return media, text


def patch_output_file_delivery(QQAdapter):
    """Expand QQ final-delivery syntax without changing streamed text."""
    original = QQAdapter.extract_media
    if getattr(original, "_qqbot_output_files_wrapped", False):
        return

    @functools.wraps(original)
    def extract_media(content):
        content = _fence_indented_code(content)
        media, _ = _extract_outside_examples(original, content)
        outputs, content = _qq_output_files(content)
        _, cleaned = _extract_outside_examples(original, content)
        # Generated output attachments are already parsed and validated. Never
        # insert directives into model text: a trailing quote or open fence can
        # absorb them when the text is parsed again.
        return media + outputs, cleaned

    extract_media._qqbot_output_files_wrapped = True
    QQAdapter.extract_media = staticmethod(extract_media)

    original_local = QQAdapter.extract_local_files

    @functools.wraps(original_local)
    def extract_local_files(content):
        return _extract_outside_examples(original_local, content)

    QQAdapter.extract_local_files = staticmethod(extract_local_files)


def _extract_outside_examples(extract, content):
    # Hide examples through both extraction stages: upstream MEDIA cleanup
    # strips leading whitespace, which otherwise turns indented code into a
    # bare path before ordinary delivery's second scan. Restore display text.
    protected = {}
    from gateway.platforms.base import BasePlatformAdapter
    for start, end in reversed(_qq_example_spans(content)):
        example = content[start:end]
        if example.startswith("`") and "\n" not in example:
            native_example = example
            if re.search(r"MEDIA:\s*$", content[:start]):
                native_example = "MEDIA:" + example
            if BasePlatformAdapter.extract_media(native_example)[0]:
                continue  # Preserve inline MEDIA syntax recognized by Hermes.
        marker = uuid.uuid4().hex
        protected[marker] = example
        content = content[:start] + marker + content[end:]
    paths, cleaned = extract(content)
    for marker, example in protected.items():
        cleaned = cleaned.replace(marker, example)
    return paths, cleaned


def patch_post_stream_media_failures(QQAdapter):
    """Reuse Hermes' ordinary failure notice for QQ post-stream attachments."""
    from gateway.run import GatewayRunner

    original_media = QQAdapter._send_media
    if not getattr(original_media, "_qqbot_post_stream_failure_wrapped", False):
        @functools.wraps(original_media)
        async def send_media(self, chat_id, media_source, *args, **kwargs):
            result = await original_media(self, chat_id, media_source, *args, **kwargs)
            context = _POST_STREAM_DELIVERY.get()
            if context is not None and context[0] is self and not result.success:
                await self._notify_media_delivery_failure(
                    chat_id, media_source, metadata=context[1],
                )
            return result

        send_media._qqbot_post_stream_failure_wrapped = True
        QQAdapter._send_media = send_media

    original_dispatch = GatewayRunner._deliver_media_from_response
    if getattr(original_dispatch, "_qqbot_post_stream_failure_wrapped", False):
        return

    @functools.wraps(original_dispatch)
    async def deliver(self, response, event, adapter, thread_metadata=None):
        platform = getattr(event.source.platform, "value", event.source.platform)
        if platform != "qqbot":
            return await original_dispatch(self, response, event, adapter, thread_metadata)
        metadata = (dict(thread_metadata) if thread_metadata is not None else
                    self._thread_metadata_for_source(event.source, self._reply_anchor_for_event(event)))
        token = _POST_STREAM_DELIVERY.set((adapter, metadata))
        try:
            return await original_dispatch(self, response, event, adapter, metadata)
        finally:
            _POST_STREAM_DELIVERY.reset(token)

    deliver._qqbot_post_stream_failure_wrapped = True
    GatewayRunner._deliver_media_from_response = deliver
