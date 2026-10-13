"""Durable trust, revocation and replay checks; no external dependencies."""
import json
import os
from pathlib import Path
import tempfile
import time
from group_invite_trust import Trust

ACTOR, STRANGER, GROUP = "A" * 32, "B" * 32, "C" * 32


def policy(home, actors, app="test-app"):
    path = home / "qq-group-invite-trust.json"
    path.write_text(json.dumps(dict(app_id=app, trusted_inviters=actors)))
    path.chmod(0o600)


with tempfile.TemporaryDirectory() as directory:
    home = Path(directory)
    policy(home, [ACTOR])
    guard = Trust(home, "test-app", "enforce")
    now = time.time() - 10
    def event(kind, actor=ACTOR, stamp=now):
        guard.lifecycle(kind, dict(group_openid=GROUP, op_member_openid=actor, timestamp=stamp))
    assert not guard.allowed(GROUP)
    event("GROUP_ADD_ROBOT")
    assert guard.allowed(GROUP)
    assert Trust(home, "test-app", "enforce").allowed(GROUP), "restart lost authorization"
    assert not Trust(home, "other-app", "enforce").allowed(GROUP), "wrong bot inherited trust"
    assert not Trust(home, "test-app", "typo").allowed(GROUP)
    event("GROUP_DEL_ROBOT", STRANGER, now + 1)
    assert not guard.allowed(GROUP)
    event("GROUP_ADD_ROBOT")
    event("GROUP_ADD_ROBOT", stamp=now + 1)
    assert not guard.allowed(GROUP), "stale/tied ADD restored removed group"
    event("GROUP_ADD_ROBOT", stamp=now + 2)
    assert guard.allowed(GROUP)
    event("GROUP_ADD_ROBOT", STRANGER, now + 3)
    assert not guard.allowed(GROUP), "untrusted re-invitation reused prior trust"
    event("GROUP_ADD_ROBOT", stamp=now + 4)
    event("GROUP_ADD_ROBOT", None, now + 4)
    assert not guard.allowed(GROUP), "missing inviter retained old trusted membership"
    event("GROUP_ADD_ROBOT", stamp=now + 4.5)
    policy(home, [])
    assert not guard.allowed(GROUP), "revoked inviter retained group authorization"
    policy(home, [ACTOR])
    assert guard.allowed(GROUP)
    (home / "qq-group-invite-trust.json").write_text("invalid JSON")
    assert not guard.allowed(GROUP)
    event("GROUP_DEL_ROBOT", STRANGER, now + 5)
    policy(home, [ACTOR])
    assert not guard.allowed(GROUP), "policy failure prevented recording removal"
    event("GROUP_ADD_ROBOT", stamp=now + 6)
    assert guard.allowed(GROUP)
    (home / "qq-group-invite-trust.json").chmod(0o644)
    if os.name != "nt":
        assert not guard.allowed(GROUP), "publicly writable/readable policy accepted"
    policy(home, ["*"])
    assert not guard.allowed(GROUP)
    policy(home, [ACTOR])
    event("GROUP_DEL_ROBOT", stamp=time.time()+1000)
    assert guard.allowed(GROUP), "invalid future event poisoned current state"
    if os.name != "nt":
        assert guard.db.stat().st_mode & 0o077 == 0
    guard.db.unlink()
    guard.db.symlink_to(home / "untouched")
    assert not guard.allowed(GROUP) and not (home / "untouched").exists()
print("Trust checks passed: persistence, bot scope, lifecycle order, revocation, corrupt/private policy, symlinks")
