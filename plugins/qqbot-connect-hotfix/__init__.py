"""Local QQBot adapter compatibility patches."""

from __future__ import annotations

import logging
import functools
import threading

from .channel_directory import (
    channel_directory_paths as _channel_directory_paths,
    lookup_channel_directory_type as _lookup_channel_directory_type,
    patch_channel_directory_chat_type as _patch_channel_directory_chat_type,
)
from .connect import patch_connect_signature as _patch_connect_signature
from .emoji import (
    describe_qq_face_only_message as _describe_qq_face_only_message,
    patch_emoji_only_group_mentions as _patch_emoji_only_group_mentions,
)
from .group_context import (
    contains_mention_marker as _contains_mention_marker,
    group_context_block as _group_context_block,
    group_context_limit as _group_context_limit,
    patch_group_channel_context as _patch_group_channel_context,
    patch_group_message_create_event as _patch_group_message_create_event,
    remember_group_message as _remember_group_message,
    should_handle_group_message_create as _should_handle_group_message_create,
)
from .group_config_interaction import (
    patch_group_config_interactions as _patch_group_config_interactions,
)
from .outbound import (
    is_expired_reply_error as _is_expired_reply_error,
    patch_expired_reply_fallback as _patch_expired_reply_fallback,
    patch_media_caption_retry as _patch_media_caption_retry,
    patch_output_file_delivery as _patch_output_file_delivery,
    patch_post_stream_media_failures as _patch_post_stream_media_failures,
    patch_plain_text_retry as _patch_plain_text_retry,
    send_plain_text as _send_plain_text,
    should_retry_plain_text as _should_retry_plain_text,
)
from .approval_owner import (
    patch_shared_group_approval_owners as _patch_shared_group_approval_owners,
    patch_shared_group_typed_approvals as _patch_shared_group_typed_approvals,
)
from .approval_choices import (
    patch_codex_approval_choices as _patch_codex_approval_choices,
)
from .streaming import (
    patch_qq_c2c_streaming as _patch_qq_c2c_streaming,
)

logger = logging.getLogger(__name__)


def register(ctx):
    try:
        from gateway.platforms.qqbot.adapter import QQAdapter
    except ImportError as exc:
        logger.warning("qqbot-connect-hotfix: could not import QQAdapter: %s", exc)
        return

    _patch_connect_signature(QQAdapter)
    _patch_group_config_interactions(QQAdapter)
    _patch_channel_directory_chat_type(QQAdapter)
    _patch_emoji_only_group_mentions(QQAdapter)
    _patch_group_message_create_event(QQAdapter)
    _patch_group_channel_context(QQAdapter)
    _patch_plain_text_retry(QQAdapter)
    _patch_expired_reply_fallback(QQAdapter)
    _patch_media_caption_retry(QQAdapter)
    _patch_output_file_delivery(QQAdapter)
    _defer_gateway_methods(QQAdapter)


def _defer_gateway_methods(QQAdapter):
    original = QQAdapter.__init__
    if getattr(original, "_qqbot_gateway_init_wrapped", False):
        return
    lock = threading.Lock()
    installed = False

    @functools.wraps(original)
    def init(self, *args, **kwargs):
        nonlocal installed
        # Background discovery holds Hermes' registry lock while the main
        # thread can own gateway.run's import lock. Adapter construction is
        # after that import and before the first event, so both locks are free.
        with lock:
            if not installed:
                _patch_gateway_methods(QQAdapter)
                installed = True
        original(self, *args, **kwargs)

    init._qqbot_gateway_init_wrapped = True
    QQAdapter.__init__ = init


def _patch_gateway_methods(QQAdapter):
    _patch_post_stream_media_failures(QQAdapter)
    streaming_status = _patch_qq_c2c_streaming(QQAdapter)
    logger.info("qqbot-connect-hotfix: %s", streaming_status)
    choices_status = _patch_codex_approval_choices(QQAdapter)
    logger.info("qqbot-connect-hotfix: %s", choices_status)
    approval_status = _patch_shared_group_approval_owners(QQAdapter)
    logger.info("qqbot-connect-hotfix: %s", approval_status)
    try:
        from gateway.slash_commands import GatewaySlashCommandsMixin

        typed_status = _patch_shared_group_typed_approvals(
            GatewaySlashCommandsMixin
        )
        logger.info("qqbot-connect-hotfix: %s", typed_status)
    except ImportError as exc:
        logger.warning(
            "qqbot-connect-hotfix: could not patch typed approvals: %s",
            exc,
        )
