"""Persistent Codex app-server compatibility patches."""

from __future__ import annotations

import logging

from .phase_filter import patch_codex_app_server_event_bridge
from .image_delivery import patch_codex_image_delivery
from .approval_bridge import patch_codex_gateway_approvals
from .long_turn import patch_codex_app_server_turn_timeout
from .lifecycle import patch_codex_agent_soft_eviction
from .qq_delivery_hook import patch_qq_delivery_context
from .session_project import (
    patch_codex_session_projects,
    register_session_project_interfaces,
)

logger = logging.getLogger(__name__)


def register(ctx):
    """Install Codex app-server gateway compatibility patches."""
    phase_status = patch_codex_app_server_event_bridge()
    image_status = patch_codex_image_delivery()
    approval_status = patch_codex_gateway_approvals()
    long_turn_status = patch_codex_app_server_turn_timeout()
    lifecycle_status = patch_codex_agent_soft_eviction()
    session_project_status = patch_codex_session_projects()
    qq_delivery_status = patch_qq_delivery_context()
    register_session_project_interfaces(ctx)
    logger.info("codex-app-server-phase-hotfix: %s", phase_status)
    logger.info("codex-app-server-phase-hotfix: image delivery %s", image_status)
    logger.info("codex-app-server-phase-hotfix: approvals %s", approval_status)
    logger.info("codex-app-server-phase-hotfix: long turns %s", long_turn_status)
    logger.info("codex-app-server-phase-hotfix: QQ delivery %s", qq_delivery_status)
    logger.info(
        "codex-app-server-phase-hotfix: Agent lifecycle %s", lifecycle_status
    )
    logger.info(
        "codex-app-server-phase-hotfix: session projects %s",
        session_project_status,
    )
