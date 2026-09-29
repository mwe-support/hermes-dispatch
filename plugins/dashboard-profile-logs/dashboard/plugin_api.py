"""Use the native log reader inside Hermes' validated, task-local profile scope."""

from typing import Optional

from fastapi import APIRouter

router = APIRouter()


@router.get("/logs")
async def get_logs(
    profile: Optional[str] = None,
    file: str = "agent",
    lines: int = 100,
    level: Optional[str] = None,
    component: Optional[str] = None,
    search: Optional[str] = None,
):
    # Lazy import avoids the Dashboard's plugin-discovery import cycle.
    from hermes_cli.web_server import _config_profile_scope, get_logs as native_logs

    with _config_profile_scope(profile):
        result = await native_logs(file, lines, level, component, search)
    return {**result, "profile": profile or "current"}
