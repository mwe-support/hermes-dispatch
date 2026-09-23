"""Resolve QQ chat types from the persisted Hermes channel directory."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def patch_channel_directory_chat_type(QQAdapter):
    original = QQAdapter._guess_chat_type
    if getattr(original, "_qqbot_channel_directory_wrapped", False):
        return

    def _guess_chat_type(self, chat_id: str) -> str:
        chat_id = str(chat_id)
        if chat_id in self._chat_type_map:
            return self._chat_type_map[chat_id]

        directory_type = lookup_channel_directory_type(chat_id)
        if directory_type:
            self._chat_type_map[chat_id] = directory_type
            logger.info(
                "qqbot-connect-hotfix: resolved %s as %s from channel_directory.json",
                chat_id,
                directory_type,
            )
            return directory_type

        return original(self, chat_id)

    _guess_chat_type.__name__ = getattr(original, "__name__", "_guess_chat_type")
    _guess_chat_type.__qualname__ = getattr(original, "__qualname__", "QQAdapter._guess_chat_type")
    _guess_chat_type.__doc__ = getattr(original, "__doc__", None)
    _guess_chat_type._qqbot_channel_directory_wrapped = True
    QQAdapter._guess_chat_type = _guess_chat_type
    logger.info("qqbot-connect-hotfix: patched QQAdapter._guess_chat_type with channel directory lookup")


def lookup_channel_directory_type(chat_id: str, *, paths=None) -> str | None:
    for path in channel_directory_paths() if paths is None else paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("qqbot-connect-hotfix: could not read %s: %s", path, exc)
            continue

        platforms = data.get("platforms") if isinstance(data, dict) else None
        entries = platforms.get("qqbot") if isinstance(platforms, dict) else None
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("id") or "") != chat_id:
                continue
            entry_type = str(entry.get("type") or "").strip().lower()
            if entry_type == "group":
                return "group"
            if entry_type in {"dm", "c2c", "user", "private"}:
                return "c2c"
            if entry_type in {"guild", "channel"}:
                return "guild"
    return None


def channel_directory_paths():
    seen = set()
    candidates = [
        os.getenv("HERMES_CHANNEL_DIRECTORY"),
        "/opt/data/channel_directory.json",
        str(Path.home() / "channel_directory.json"),
        str(Path.home() / ".hermes" / "channel_directory.json"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        yield path
