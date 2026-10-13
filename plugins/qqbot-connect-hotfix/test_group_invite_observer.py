"""Run with Python; requires only the standard library."""
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from group_invite_observer import capture


class Adapter:
    _app_id = "test-app"

    def __init__(self):
        self.received = []

    def _dispatch_payload(self, payload):
        self.received.append(payload)
        return "forwarded"


with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, HERMES_HOME=directory):
    adapter = Adapter()
    add = dict(op=0, t="GROUP_ADD_ROBOT", s=1,
               d=dict(group_openid="test-group", op_member_openid="test-inviter", timestamp=1,
                      content="SECRET", token="SECRET"))
    capture(adapter, add)
    capture(adapter, dict(op=0, t="C2C_MESSAGE_CREATE", d=dict(
        id="message", content="PRIVATE", author=dict(user_openid="test-user", username="PRIVATE"))))
    path = Path(directory) / "state/qq-group-invite-trust-observe.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 2 and rows[0]["op_member_openid"] == "test-inviter"
    assert rows[1]["author"]["user_openid"] == "test-user"
    assert "SECRET" not in path.read_text() and "PRIVATE" not in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    for invalid in [None, {"op": 0, "t": "READY"}, {"op": 0, "t": "GROUP_ADD_ROBOT", "d": []}]:
        capture(adapter, invalid)
    assert len(path.read_text().splitlines()) == 2
    path.unlink()
    path.symlink_to(Path(directory) / "untouched")
    try:
        capture(adapter, add)
        raise AssertionError("symbolic output accepted")
    except ValueError:
        pass
    assert not (Path(directory) / "untouched").exists()
print("Observer checks passed: private metadata, redaction, malformed input, symlinks")
