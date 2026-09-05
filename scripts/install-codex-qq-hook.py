#!/usr/bin/env python3
"""Manage only dispatch's QQ hook in one explicit profile/CODEX_HOME pair.

Run after install-plugins.sh, with the target profile's Python interpreter.
This installer never modifies Codex credentials, config.toml, or hook trust.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import stat
import sys
import tempfile


def regular_path(path: Path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"expected a regular file, not a shared link: {path}")


def write_bytes(path: Path, data: bytes | None):
    if data is None:
        path.unlink(missing_ok=True)
        return
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode()


def manage(hermes_home: Path, codex_home: Path, *, remove=False, python=sys.executable):
    profile, home = hermes_home.expanduser().resolve(), codex_home.expanduser().resolve()
    if not profile.is_dir() or not home.is_dir():
        raise ValueError("both the Hermes profile and authenticated CODEX_HOME must already exist")
    hooks_path = home / "hooks.json"
    state_dir = home / ".hermes-dispatch"
    if state_dir.is_symlink() or (state_dir.exists() and not state_dir.is_dir()):
        raise ValueError("dispatch hook state directory must not be a shared link")
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state_path = state_dir / "qq-delivery-hook.json"
    lock_path = state_dir / "qq-delivery-hook.lock"
    for path in (hooks_path, state_path, lock_path):
        regular_path(path)
    # Serialize our installers even when two profiles accidentally target the
    # same CODEX_HOME. Its persisted owner below prevents cross-profile writes.
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _manage_locked(profile, home, hooks_path, state_path, remove, python)


def _manage_locked(profile, home, hooks_path, state_path, remove, python):
    for path in (hooks_path, state_path):
        regular_path(path)
    old_hooks = hooks_path.read_bytes() if hooks_path.exists() else None
    old_state = state_path.read_bytes() if state_path.exists() else None
    data = json.loads(old_hooks) if old_hooks is not None else {}
    state = json.loads(old_state) if old_state is not None else None
    if state is not None and not isinstance(state, dict):
        raise ValueError("dispatch hook ownership state must be an object")
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("hooks.json must contain a hooks object")
    groups = data.get("hooks", {}).get("UserPromptSubmit", [])
    if not isinstance(groups, list):
        raise ValueError("UserPromptSubmit must be a list")
    if state is not None:
        if state.get("hermes_home") != str(profile) or state.get("codex_home") != str(home):
            raise ValueError("this CODEX_HOME hook is owned by a different Hermes profile")
        if groups.count(state["entry"]) != 1:
            raise ValueError("managed hook was changed outside dispatch; leaving hooks.json untouched")
    if remove and state is None:
        return "not installed"

    updated = copy.deepcopy(data)
    entries = updated.setdefault("hooks", {}).setdefault("UserPromptSubmit", [])
    owned_index = entries.index(state["entry"]) if state is not None else len(entries)
    if remove:
        if owned_index == len(entries) - 1:
            entries.pop()
        else:
            # Other hook trust keys include matcher/handler indices. An empty
            # group removes ours without shifting hooks added after it.
            entries[owned_index] = {"hooks": []}
        if not entries and not state["had_event"]:
            del updated["hooks"]["UserPromptSubmit"]
        if not updated["hooks"] and not state["had_hooks"]:
            del updated["hooks"]
        next_hooks = None if not updated and not state["had_file"] else json_bytes(updated)
        next_state = None
    else:
        script = profile / "plugins/codex-app-server-phase-hotfix/qq_delivery_hook.py"
        regular_path(script)
        digest = hashlib.sha256(script.read_bytes()).hexdigest()
        interpreter = Path(python).expanduser().resolve()
        if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
            raise ValueError("--python must name an existing executable path")
        command = shlex.join([str(interpreter), str(script), "--source-sha256", digest,
                              "--hermes-home", str(profile), "--codex-home", str(home)])
        entry = {"hooks": [{"type": "command", "command": command, "timeout": 5,
                             "statusMessage": f"QQ file delivery ({digest[:12]})"}]}
        if state is not None and state["entry"] == entry:
            return "already installed (review trust with Codex /hooks)"
        if state is None:
            entries.append(entry)
        else:
            entries[owned_index] = entry
        next_state = json_bytes({
            "hermes_home": str(profile), "codex_home": str(home), "entry": entry,
            "had_file": state["had_file"] if state else old_hooks is not None,
            "had_hooks": state["had_hooks"] if state else "hooks" in data,
            "had_event": state["had_event"] if state else "UserPromptSubmit" in data.get("hooks", {}),
        })
        next_hooks = json_bytes(updated)

    backup_root = profile / "plugin-backups"
    if backup_root.is_symlink():
        raise ValueError("plugin backup root must not be a shared link")
    backup_root.mkdir(exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix="codex-qq-hook-", dir=backup_root))
    for name, content in [("hooks.json", old_hooks), ("state.json", old_state)]:
        if content is not None:
            write_bytes(backup / name, content)
    try:
        write_bytes(hooks_path, next_hooks)
        write_bytes(state_path, next_state)
    except Exception:
        write_bytes(hooks_path, old_hooks)
        write_bytes(state_path, old_state)
        raise
    return f"{'removed' if remove else 'installed'} QQ hook; backup: {backup}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hermes-home", type=Path, required=True)
    parser.add_argument("--codex-home", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable, help="interpreter available to this Codex instance")
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    try:
        print(manage(args.hermes_home, args.codex_home, remove=args.remove, python=args.python))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"QQ hook configuration unchanged or rolled back: {exc}\n")
    if not args.remove:
        print("Review and trust this hook with /hooks in the target CODEX_HOME before using it.")


if __name__ == "__main__":
    main()
