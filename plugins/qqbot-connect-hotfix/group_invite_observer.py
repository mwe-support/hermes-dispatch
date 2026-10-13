"""Bounded private metadata capture for live inviter identity verification."""
import json
import os
import stat
import sys
from pathlib import Path
import time

EVENTS = {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "C2C_MESSAGE_CREATE",
          "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"}


def identifier(value):
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def metadata(adapter, payload):
    if not isinstance(payload, dict) or payload.get("op") != 0 or payload.get("t") not in EVENTS:
        return None
    data = payload.get("d")
    if not isinstance(data, dict):
        return None
    author = data.get("author")
    author = author if isinstance(author, dict) else {}
    return dict(observed_at=time.time(), event=payload["t"],
                seq=payload.get("s") if isinstance(payload.get("s"), int) else None,
                app_id=str(getattr(adapter, "_app_id", "")),
                event_id=identifier(payload.get("id")), timestamp=data.get("timestamp")
                if isinstance(data.get("timestamp"), (int, float, str)) else None,
                group_openid=identifier(data.get("group_openid")),
                op_member_openid=identifier(data.get("op_member_openid")),
                author={k: identifier(author.get(k)) for k in
                        ("id", "member_openid", "user_openid", "union_openid")},
                message_id=identifier(data.get("id")))


def capture(adapter, payload):
    row = metadata(adapter, payload)
    if row is None:
        return
    root = Path(os.getenv("HERMES_HOME", "~/.hermes")).expanduser()
    directory = root / "state"
    if directory.is_symlink():
        raise ValueError("symbolic observation directory")
    directory.mkdir(exist_ok=True, mode=0o700)
    path = directory / "qq-group-invite-trust-observe.jsonl"
    if path.is_symlink():
        raise ValueError("symbolic observation file")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or (sys.platform != "win32" and st.st_mode & 0o077) or
                (hasattr(os, "getuid") and st.st_uid != os.getuid())):
            raise ValueError("foreign observation file")
        if st.st_size >= 512 * 1024:
            return
        os.write(fd, (json.dumps(row, ensure_ascii=True) + "\n").encode())
    finally:
        os.close(fd)
