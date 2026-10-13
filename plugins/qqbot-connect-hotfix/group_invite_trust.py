"""Profile-owned inviter policy and durable QQ group lifecycle decisions."""
import json
from contextlib import closing
import logging
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import time

logger = logging.getLogger(__name__)


def openid(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Fa-f0-9]{32}", value):
        return value.upper()
    return None


def private_file(path):
    if path.is_symlink():
        raise ValueError("symbolic trust file")
    if path.exists():
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or (sys.platform != "win32" and info.st_mode & 0o077) or
                (hasattr(os, "getuid") and info.st_uid != os.getuid())):
            raise ValueError("trust file must be private and owner-owned")


class Trust:
    def __init__(self, home, app_id, mode):
        self.home = Path(home).expanduser().resolve()
        self.app_id = str(app_id)
        self.mode = mode
        self.failed = False
        self.warned = False
        self.policy = self.home / "qq-group-invite-trust.json"
        self.db = self.home / "state/qq-group-invite-trust.sqlite3"

    def warn(self, exc):
        if not self.warned:
            logger.warning("QQ group trust unavailable: %s", type(exc).__name__)
            self.warned = True

    def inviters(self):
        private_file(self.policy)
        with self.policy.open() as stream:
            policy = json.load(stream)
        if policy.get("app_id") != self.app_id:
            raise ValueError("bot policy mismatch")
        values = policy.get("trusted_inviters")
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError("invalid inviter list")
        result = {openid(value) for value in values}
        if None in result:
            raise ValueError("invalid inviter OpenID")
        return result

    def connect(self):
        if self.db.parent.is_symlink():
            raise ValueError("symbolic trust directory")
        self.db.parent.mkdir(exist_ok=True, mode=0o700)
        private_file(self.db)
        # Create privately before SQLite opens it; existing state is never reset.
        fd = os.open(self.db, os.O_CREAT | os.O_APPEND | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        connection = sqlite3.connect(str(self.db), timeout=2)
        try:
            connection.execute("""CREATE TABLE IF NOT EXISTS groups (
                app_id TEXT NOT NULL, group_id TEXT NOT NULL, inviter TEXT,
                stamp REAL NOT NULL, removed INTEGER NOT NULL, approved INTEGER NOT NULL,
                PRIMARY KEY(app_id, group_id))""")
        except Exception:
            connection.close()
            raise
        return connection

    def lifecycle(self, event, data):
        try:
            group = openid(data.get("group_openid"))
            actor = openid(data.get("op_member_openid"))
            stamp = float(data.get("timestamp"))
            if not group or not 0 < stamp <= time.time() + 30:
                return
            try:
                inviters = self.inviters() if self.mode in {"observe", "enforce"} else set()
            except (OSError, ValueError, TypeError):
                inviters = set()
            removed = event == "GROUP_DEL_ROBOT"
            with closing(self.connect()) as connection, connection:
                previous = connection.execute(
                    "SELECT inviter,stamp,removed,approved FROM groups WHERE app_id=? AND group_id=?",
                    (self.app_id, group)).fetchone()
                if previous and (stamp < previous[1] or
                                 (stamp == previous[1] and (previous[2] or
                                  (not removed and actor == previous[0])))):
                    return
                connection.execute("INSERT OR REPLACE INTO groups VALUES (?,?,?,?,?,?)",
                                   (self.app_id, group, actor, stamp, int(removed),
                                    int(not removed and actor in inviters)))
        except Exception as exc:
            self.failed = True
            self.warn(exc)

    def allowed(self, group):
        try:
            if self.failed or self.mode not in {"observe", "enforce"}:
                return False
            group = openid(group)
            if not group:
                return False
            inviters = self.inviters()
            with closing(self.connect()) as connection, connection:
                row = connection.execute(
                    "SELECT inviter,removed,approved FROM groups WHERE app_id=? AND group_id=?",
                    (self.app_id, group)).fetchone()
            return bool(row and row[2] and not row[1] and row[0] in inviters)
        except Exception as exc:
            self.warn(exc)
            return False
