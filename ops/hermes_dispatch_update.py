#!/usr/bin/env python3
"""Cross-platform, profile-scoped updater for hermes-dispatch.

The updater is deliberately outside the Hermes Gateway process.  It fetches a
reviewed ref into a dedicated clone, validates a small release manifest, runs
the declared regression suite in an isolated HERMES_HOME, then installs only
changed plugins and managed settings.  A failed live update restores the exact
pre-update plugin/config snapshot and blocks that commit from automatic retry.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Sequence


DEFAULT_REMOTE = "https://github.com/mwe-support/hermes-dispatch.git"
DEFAULT_REF = "main"
MANIFEST_REL = Path("ops/release.json")
PLUGIN_NAME_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._-]{0,127}$")
PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
VERSION_RE = re.compile(r"(?:Hermes Agent v)?(\d+)\.(\d+)\.(\d+)")
MANAGED_CONFIG_KEYS = {
    "model.openai_runtime",
    "compression.codex_app_server_auto",
    "display.interim_assistant_messages",
    "display.streaming",
    "streaming.enabled",
    "streaming.transport",
    "display.platforms.qqbot.interim_assistant_messages",
    "display.platforms.qqbot.streaming",
    "display.platforms.qqbot.tool_progress",
    "agent.gateway_timeout",
    "agent.gateway_timeout_warning",
    "agent.restart_drain_timeout",
}
MANAGED_ENV_KEYS = {
    "HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS",
    "HERMES_CODEX_SESSION_PROJECTS_ENABLED",
    "HERMES_CODEX_SESSION_PROJECTS_BACKFILL",
    "HERMES_CODEX_APP_REGISTER_PROJECTS",
}


class UpdateError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def run(
    argv: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    env: Optional[dict[str, str]] = None,
    timeout: float = 120,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [str(item) for item in argv],
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout,
    )
    if check and result.returncode:
        command = " ".join(str(item) for item in argv[:4])
        detail = (result.stderr or result.stdout).strip()
        raise UpdateError(f"{command} failed ({result.returncode}): {detail[-2000:]}")
    return result


def resolve_executable(value: str, fallback: str) -> str:
    requested = value.strip() if value else ""
    if requested:
        path = shutil.which(requested) or requested
    else:
        path = shutil.which(fallback) or ""
    if not path or not Path(path).exists():
        raise UpdateError(f"required executable not found: {requested or fallback}")
    return str(Path(path).resolve())


def native_hermes_root(
    *,
    platform: Optional[str] = None,
    home: Optional[Path] = None,
    local_appdata: Optional[str] = None,
) -> Path:
    platform = platform or sys.platform
    home = home or Path.home()
    if platform == "win32":
        raw = local_appdata if local_appdata is not None else os.environ.get("LOCALAPPDATA", "")
        return (Path(raw) if raw else home / "AppData" / "Local") / "hermes"
    return home / ".hermes"


def profile_home(profile: str, **kwargs: Any) -> Path:
    if not PROFILE_RE.fullmatch(profile):
        raise UpdateError(f"invalid Hermes profile name: {profile!r}")
    root = native_hermes_root(**kwargs)
    return root if profile == "default" else root / "profiles" / profile


def is_link_or_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attrs & 0x400)


def assert_real_directory(path: Path, label: str, *, create: bool = False) -> None:
    if is_link_or_reparse(path):
        raise UpdateError(f"{label} must not be a symlink, junction, or reparse point: {path}")
    if path.exists() and not path.is_dir():
        raise UpdateError(f"{label} must be a directory: {path}")
    if create:
        path.mkdir(parents=True, exist_ok=True)
        if is_link_or_reparse(path):
            raise UpdateError(f"{label} became an unsafe link: {path}")


def atomic_write(path: Path, data: bytes, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(data)
    if mode is not None and os.name != "nt":
        tmp.chmod(mode)
    os.replace(tmp, path)


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"invalid JSON file {path}: {exc}") from exc


def write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        0o600,
    )


class FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0)
        if self.handle.read(1) == b"":
            self.handle.write(b"\0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self.handle.close()
            raise UpdateError(f"another updater is already running for {self.path.parent}") from exc
        return self

    def __exit__(self, *_: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()


def validate_manifest(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise UpdateError("release manifest schema must be 1")
    allowed = {
        "schema",
        "minimum_hermes",
        "plugins",
        "enable_plugins",
        "enable_tools",
        "config_set",
        "env_set_if_missing",
        "tests",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise UpdateError(f"unknown release manifest fields: {', '.join(unknown)}")
    minimum = str(raw.get("minimum_hermes") or "")
    if not VERSION_RE.fullmatch(minimum):
        raise UpdateError("minimum_hermes must be x.y.z")
    for field in ("plugins", "enable_plugins", "enable_tools", "tests"):
        if not isinstance(raw.get(field), list) or not all(isinstance(x, str) for x in raw[field]):
            raise UpdateError(f"release manifest {field} must be a string list")
    for name in raw["plugins"] + raw["enable_plugins"] + raw["enable_tools"]:
        if not PLUGIN_NAME_RE.fullmatch(name):
            raise UpdateError(f"invalid managed name in release manifest: {name!r}")
    if not set(raw["plugins"]) <= set(raw["enable_plugins"]):
        raise UpdateError("every managed plugin must also appear in enable_plugins")
    config_set = raw.get("config_set") or {}
    env_defaults = raw.get("env_set_if_missing") or {}
    if not isinstance(config_set, dict) or not set(config_set) <= MANAGED_CONFIG_KEYS:
        raise UpdateError("release manifest config_set contains an unmanaged key")
    if not isinstance(env_defaults, dict) or not set(env_defaults) <= MANAGED_ENV_KEYS:
        raise UpdateError("release manifest env_set_if_missing contains an unmanaged key")
    if not all(isinstance(v, (str, int, float, bool)) for v in config_set.values()):
        raise UpdateError("config_set values must be scalar")
    if not all(isinstance(v, str) and "\n" not in v for v in env_defaults.values()):
        raise UpdateError("env_set_if_missing values must be single-line strings")
    for item in raw["tests"]:
        path = Path(item)
        if path.is_absolute() or ".." in path.parts or path.suffix != ".py":
            raise UpdateError(f"unsafe test path in release manifest: {item!r}")
    return raw


def manifest_digest(manifest: dict[str, Any]) -> str:
    data = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


def tree_digest(root: Path) -> Optional[str]:
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if is_link_or_reparse(path):
            raise UpdateError(f"plugin tree contains an unsafe link: {path}")
        if path.is_file():
            digest.update(rel.as_posix().encode())
            digest.update(b"\0")
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return digest.hexdigest()


def installation_root(home: Path) -> Path:
    return home.parent.parent if home.parent.name == "profiles" else home


def hermes_python(home: Path) -> str:
    root = installation_root(home)
    candidates = [
        root / "hermes-agent" / "venv" / "bin" / "python",
        root / "hermes-agent" / ".venv" / "Scripts" / "python.exe",
        root / "hermes-agent" / "venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return sys.executable


def git_checkout(git: str, remote: str, ref: str, update_root: Path) -> tuple[Path, str]:
    repo = update_root / "repository"
    worktrees = update_root / "worktrees"
    update_root.mkdir(parents=True, exist_ok=True)
    if not (repo / ".git").exists():
        run([git, "clone", "--no-checkout", remote, repo], timeout=300)
    else:
        actual = run([git, "remote", "get-url", "origin"], cwd=repo).stdout.strip()
        if actual != remote:
            raise UpdateError(f"update clone origin mismatch: {actual!r} != {remote!r}")
    run([git, "fetch", "--prune", "origin", ref], cwd=repo, timeout=300)
    commit = run([git, "rev-parse", "FETCH_HEAD"], cwd=repo).stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise UpdateError(f"unexpected fetched commit: {commit!r}")
    checkout = worktrees / commit
    run([git, "worktree", "prune"], cwd=repo, check=False)
    if checkout.exists():
        run([git, "worktree", "remove", "--force", checkout], cwd=repo, check=False)
        shutil.rmtree(checkout, ignore_errors=True)
        run([git, "worktree", "prune"], cwd=repo, check=False)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    run([git, "worktree", "add", "--detach", checkout, commit], cwd=repo, timeout=120)
    return checkout, commit


def remove_checkout(git: str, update_root: Path, checkout: Path) -> None:
    repo = update_root / "repository"
    run([git, "worktree", "remove", "--force", checkout], cwd=repo, check=False)
    shutil.rmtree(checkout, ignore_errors=True)


def hermes_version(hermes: str, profile: str) -> tuple[int, int, int]:
    output = run([hermes, "-p", profile, "--version"], timeout=30).stdout
    match = VERSION_RE.search(output)
    if not match:
        raise UpdateError(f"could not parse Hermes version from: {output!r}")
    return tuple(int(part) for part in match.groups())


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise UpdateError(f"invalid version: {value!r}")
    return tuple(int(part) for part in match.groups())


def run_regressions(checkout: Path, home: Path, manifest: dict[str, Any], git: str) -> None:
    python = hermes_python(home)
    root = installation_root(home)
    tmp = Path(tempfile.mkdtemp(prefix="hermes-dispatch-update-test-"))
    try:
        env = os.environ.copy()
        env.update(
            HERMES_HOME=str(tmp),
            PYTHONPATH=str(root / "hermes-agent"),
            PYTHONDONTWRITEBYTECODE="1",
        )
        for relative in manifest["tests"]:
            path = checkout / relative
            if not path.is_file():
                raise UpdateError(f"declared regression does not exist: {relative}")
            run([python, path], cwd=checkout, env=env, timeout=300)
    finally:
        # ponytail: Windows SQLite handles may outlive a completed regression
        # subprocess briefly. Retry, then let the OS clean a locked temp tree
        # later instead of turning a passing suite into a deployment failure.
        if not remove_tree_with_retries(tmp):
            print(f"warning: deferred cleanup of locked regression directory: {tmp}", file=sys.stderr)
    run([git, "diff", "--check"], cwd=checkout, timeout=30)


def remove_tree_with_retries(path: Path, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            shutil.rmtree(path)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.25)


def read_gateway_state(home: Path) -> dict[str, Any]:
    raw = load_json(home / "gateway_state.json", {})
    return raw if isinstance(raw, dict) else {}


def process_alive(pid: Any) -> bool:
    try:
        number = int(pid)
    except (TypeError, ValueError):
        return False
    if number <= 0:
        return False
    if os.name == "nt":
        return windows_process_alive(number)
    try:
        os.kill(number, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def windows_process_alive(pid: int, kernel32: Any = None) -> bool:
    if kernel32 is None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        get_last_error = ctypes.get_last_error
    else:
        get_last_error = kernel32.get_last_error
    # SYNCHRONIZE is sufficient for a liveness probe and avoids tasklist/WMI,
    # which locked-down enterprise Windows policies commonly deny.
    handle = kernel32.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        return int(get_last_error()) == 5  # Access denied means the PID exists.
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) == 0x00000102
    finally:
        kernel32.CloseHandle(handle)


def gateway_running(state: dict[str, Any]) -> bool:
    if state.get("gateway_state") != "running":
        return False
    pid = state.get("pid")
    return process_alive(pid) if pid is not None else True


def qqbot_enabled(hermes: str, profile: str) -> bool:
    result = run(
        [hermes, "-p", profile, "config", "get", "platforms.qqbot.enabled"],
        check=False,
        timeout=30,
    )
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def apply_env_defaults(path: Path, values: dict[str, str]) -> list[str]:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    present: set[str] = set()
    for line in original.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        present.add(stripped.split("=", 1)[0].strip())
    missing = [key for key in values if key not in present]
    if not missing:
        return []
    suffix = "" if not original or original.endswith("\n") else "\n"
    addition = "".join(f"{key}={values[key]}\n" for key in missing)
    atomic_write(path, (original + suffix + addition).encode(), 0o600)
    return missing


def env_missing(path: Path, keys: Sequence[str]) -> list[str]:
    if not path.exists():
        return list(keys)
    present: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            present.add(stripped.split("=", 1)[0].strip())
    return [key for key in keys if key not in present]


def backup_live_state(home: Path, plugins: list[str], commit: str) -> tuple[Path, dict[str, Any]]:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_root = home / "update-backups"
    assert_real_directory(backup_root, "update backup root", create=True)
    backup = backup_root / f"{commit[:12]}-{stamp}"
    suffix = 1
    while backup.exists():
        backup = backup_root / f"{commit[:12]}-{stamp}-{suffix}"
        suffix += 1
    backup.mkdir()
    metadata: dict[str, Any] = {"files": {}, "plugins": {}}
    for name in ("config.yaml", ".env"):
        source = home / name
        metadata["files"][name] = source.exists()
        if source.exists():
            shutil.copy2(source, backup / name)
    for plugin in plugins:
        source = home / "plugins" / plugin
        metadata["plugins"][plugin] = source.is_dir()
        if source.is_dir():
            shutil.copytree(source, backup / "plugins" / plugin, copy_function=shutil.copy2)
    write_json(backup / "metadata.json", metadata)
    return backup, metadata


def preflight_active_plugins(home: Path, plugins: list[str]) -> None:
    root = home / "plugins"
    assert_real_directory(root, "plugin root", create=True)
    for plugin in plugins:
        target = root / plugin
        if is_link_or_reparse(target):
            raise UpdateError(f"active plugin target is unsafe: {target}")
        if target.exists() and not target.is_dir():
            raise UpdateError(f"active plugin target must be a directory: {target}")
        if target.is_dir():
            tree_digest(target)


def stage_plugins(checkout: Path, home: Path, plugins: list[str], commit: str) -> Path:
    stage_root = home / "update-staging"
    assert_real_directory(stage_root, "update staging root", create=True)
    stage = stage_root / commit
    if is_link_or_reparse(stage):
        raise UpdateError(f"update staging target is unsafe: {stage}")
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    for plugin in plugins:
        if not PLUGIN_NAME_RE.fullmatch(plugin):
            raise UpdateError(f"invalid plugin name: {plugin!r}")
        source = checkout / "plugins" / plugin
        if not (source / "plugin.yaml").is_file():
            raise UpdateError(f"invalid plugin source: {source}")
        if tree_digest(source) is None:
            raise UpdateError(f"empty plugin source: {source}")
        shutil.copytree(source, stage / plugin, copy_function=shutil.copy2)
    return stage


def replace_plugins(home: Path, stage: Path, plugins: list[str]) -> None:
    root = home / "plugins"
    preflight_active_plugins(home, plugins)
    for plugin in plugins:
        target = root / plugin
        incoming = stage / plugin
        if target.exists():
            shutil.rmtree(target)
        os.replace(incoming, target)


def restore_backup(home: Path, backup: Path, metadata: dict[str, Any]) -> None:
    for plugin, existed in metadata.get("plugins", {}).items():
        target = home / "plugins" / plugin
        if target.exists():
            shutil.rmtree(target)
        source = backup / "plugins" / plugin
        if existed:
            shutil.copytree(source, target, copy_function=shutil.copy2)
    for name, existed in metadata.get("files", {}).items():
        target = home / name
        source = backup / name
        if existed:
            atomic_write(target, source.read_bytes(), 0o600)
        elif target.exists():
            target.unlink()


def hermes_call(hermes: str, profile: str, *args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return run([hermes, "-p", profile, *args], timeout=timeout)


def expected_config_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def desired_state_drift(
    hermes: str,
    profile: str,
    home: Path,
    manifest: dict[str, Any],
) -> list[str]:
    drift: list[str] = []
    for key, expected in manifest["config_set"].items():
        result = run([hermes, "-p", profile, "config", "get", key], check=False, timeout=30)
        if result.returncode or result.stdout.strip() != expected_config_text(expected):
            drift.append(f"config:{key}")
    for key in env_missing(home / ".env", manifest["env_set_if_missing"]):
        drift.append(f"env:{key}")
    for plugin in manifest["enable_plugins"]:
        result = run([hermes, "-p", profile, "plugins", "show", plugin], check=False, timeout=30)
        if result.returncode or "Status: enabled" not in result.stdout:
            drift.append(f"plugin:{plugin}")
    tool_result = run(
        [hermes, "-p", profile, "tools", "list", "--platform", "qqbot"],
        check=False,
        timeout=60,
    )
    tools = tool_result.stdout if not tool_result.returncode else ""
    for tool in manifest["enable_tools"]:
        pattern = re.compile(rf"enabled\s+{re.escape(tool)}(?:\s|$)")
        if not pattern.search(tools):
            drift.append(f"tool:{tool}")
    return drift


def apply_managed_settings(
    hermes: str,
    profile: str,
    home: Path,
    manifest: dict[str, Any],
) -> None:
    for key, value in manifest["config_set"].items():
        hermes_call(hermes, profile, "config", "set", "--force", key, expected_config_text(value))
    apply_env_defaults(home / ".env", manifest["env_set_if_missing"])
    for plugin in manifest["enable_plugins"]:
        args = ["plugins", "enable", plugin]
        if plugin != "openai-codex":
            args.append("--no-allow-tool-override")
        hermes_call(hermes, profile, *args)
    for tool in manifest["enable_tools"]:
        hermes_call(hermes, profile, "tools", "enable", "--platform", "qqbot", tool)
    hermes_call(hermes, profile, "config", "check")


def wait_for_ready(log_path: Path, offset: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            size = log_path.stat().st_size
            if size < offset:
                offset = 0
            with log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                if any("Ready" in line for line in handle):
                    return True
        except FileNotFoundError:
            pass
        time.sleep(1)
    return False


def plugin_differences(checkout: Path, home: Path, plugins: list[str]) -> list[str]:
    return [
        plugin
        for plugin in plugins
        if tree_digest(checkout / "plugins" / plugin) != tree_digest(home / "plugins" / plugin)
    ]


def save_state(path: Path, state: dict[str, Any], **updates: Any) -> None:
    state.update(updates, updated_at=utc_now())
    write_json(path, state)


def perform_update(args: argparse.Namespace) -> dict[str, Any]:
    home = profile_home(args.profile)
    if not home.is_dir():
        raise UpdateError(f"Hermes profile does not exist: {home}")
    hermes = resolve_executable(args.hermes_cli, "hermes")
    git = resolve_executable(args.git_cli, "git")
    state_dir = home / "state"
    state_path = state_dir / "hermes-dispatch-update.json"
    update_root = home / "updates" / "hermes-dispatch"
    assert_real_directory(state_dir, "updater state directory", create=True)
    assert_real_directory(update_root, "updater repository root", create=True)
    state = load_json(state_path, {})
    if not isinstance(state, dict):
        raise UpdateError(f"invalid updater state: {state_path}")

    with FileLock(state_dir / "hermes-dispatch-update.lock"):
        checkout, commit = git_checkout(git, args.remote, args.ref, update_root)
        try:
            manifest = validate_manifest(load_json(checkout / MANIFEST_REL, None))
            digest = manifest_digest(manifest)
            if state.get("blocked_commit") == commit and not args.retry_blocked:
                return {"status": "blocked", "commit": commit, "reason": state.get("blocked_reason")}
            changed = plugin_differences(checkout, home, manifest["plugins"])
            manifest_changed = state.get("manifest_digest") != digest
            drift = desired_state_drift(hermes, args.profile, home, manifest)
            if not args.apply and state.get("last_tested_commit") == commit:
                return {
                    "status": "already-tested",
                    "commit": commit,
                    "plugins": changed,
                    "state_drift": drift,
                }
            if not changed and not manifest_changed and not drift:
                save_state(
                    state_path,
                    state,
                    applied_commit=commit,
                    last_seen_commit=commit,
                    last_result="no-live-change",
                )
                return {"status": "no-live-change", "commit": commit}

            gateway: dict[str, Any] = {}
            if args.apply:
                gateway = read_gateway_state(home)
                active = int(gateway.get("active_agents") or 0)
                if active:
                    save_state(
                        state_path,
                        state,
                        last_seen_commit=commit,
                        last_result="deferred-active-agents",
                        active_agents=active,
                    )
                    return {"status": "deferred", "commit": commit, "active_agents": active}

            current_version = hermes_version(hermes, args.profile)
            minimum = version_tuple(manifest["minimum_hermes"])
            if current_version < minimum:
                reason = (
                    f"Hermes {'.'.join(map(str, current_version))} is older than "
                    f"required {manifest['minimum_hermes']}"
                )
                save_state(
                    state_path,
                    state,
                    blocked_commit=commit,
                    blocked_reason=reason,
                    last_seen_commit=commit,
                    last_result="blocked_by_hermes_version",
                )
                return {"status": "blocked_by_hermes_version", "commit": commit, "reason": reason}

            try:
                run_regressions(checkout, home, manifest, git)
            except Exception as exc:
                reason = str(exc)
                save_state(
                    state_path,
                    state,
                    blocked_commit=commit,
                    blocked_reason=reason,
                    last_seen_commit=commit,
                    last_result="regression-failed",
                )
                raise UpdateError(f"regression suite failed; commit blocked: {reason}") from exc
            if not args.apply:
                save_state(
                    state_path,
                    state,
                    last_seen_commit=commit,
                    last_tested_commit=commit,
                    last_result="dry-run-passed",
                    dry_run_plugin_changes=changed,
                    dry_run_state_drift=drift,
                )
                return {
                    "status": "dry-run-passed",
                    "commit": commit,
                    "plugins": changed,
                    "state_drift": drift,
                }

            was_running = gateway_running(gateway)
            require_qq_ready = qqbot_enabled(hermes, args.profile)
            preflight_active_plugins(home, changed)
            backup, metadata = backup_live_state(home, changed, commit)
            stage = stage_plugins(checkout, home, changed, commit)
            log_path = home / "logs" / "gateway.log"
            log_offset = log_path.stat().st_size if log_path.exists() else 0
            stopped = False
            try:
                if was_running:
                    hermes_call(hermes, args.profile, "gateway", "stop", timeout=360)
                    stopped = True
                replace_plugins(home, stage, changed)
                apply_managed_settings(hermes, args.profile, home, manifest)
                for plugin in manifest["plugins"]:
                    expected = tree_digest(checkout / "plugins" / plugin)
                    actual = tree_digest(home / "plugins" / plugin)
                    if expected != actual:
                        raise UpdateError(f"installed plugin hash mismatch: {plugin}")
                if was_running:
                    hermes_call(hermes, args.profile, "gateway", "start", timeout=60)
                    stopped = False
                    if require_qq_ready and not wait_for_ready(log_path, log_offset, args.health_timeout):
                        raise UpdateError("Gateway restarted but QQ Ready was not observed")
                    hermes_call(hermes, args.profile, "gateway", "status", "--deep", timeout=60)
                save_state(
                    state_path,
                    state,
                    applied_commit=commit,
                    last_seen_commit=commit,
                    manifest_digest=digest,
                    last_tested_commit=commit,
                    last_result="updated",
                    last_backup=str(backup),
                    changed_plugins=changed,
                    blocked_commit=None,
                    blocked_reason=None,
                )
                shutil.rmtree(home / "update-staging" / commit, ignore_errors=True)
                return {"status": "updated", "commit": commit, "plugins": changed, "backup": str(backup)}
            except Exception as exc:
                rollback_errors: list[str] = []
                if was_running and not stopped:
                    try:
                        hermes_call(hermes, args.profile, "gateway", "stop", timeout=360)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"stop: {rollback_exc}")
                try:
                    restore_backup(home, backup, metadata)
                except Exception as rollback_exc:
                    rollback_errors.append(f"restore: {rollback_exc}")
                if was_running:
                    try:
                        hermes_call(hermes, args.profile, "gateway", "start", timeout=60)
                        if require_qq_ready and not wait_for_ready(log_path, log_offset, args.health_timeout):
                            rollback_errors.append("restored Gateway did not reach QQ Ready")
                    except Exception as rollback_exc:
                        rollback_errors.append(f"start: {rollback_exc}")
                reason = str(exc)
                if rollback_errors:
                    reason += "; rollback warnings: " + "; ".join(rollback_errors)
                save_state(
                    state_path,
                    state,
                    blocked_commit=commit,
                    blocked_reason=reason,
                    last_seen_commit=commit,
                    last_result="rolled-back",
                    last_backup=str(backup),
                )
                raise UpdateError(f"update failed and was rolled back: {reason}") from exc
        finally:
            remove_checkout(git, update_root, checkout)


def installed_script(home: Path) -> Path:
    suffix = ".py"
    return home / "bin" / f"hermes-dispatch-update{suffix}"


def scheduler_identity(profile: str) -> tuple[str, str]:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", profile)
    return f"com.mwe-support.hermes-dispatch-update.{safe}", f"Hermes_Dispatch_Update_{safe}"


def scheduler_arguments(args: argparse.Namespace, script: Path) -> list[str]:
    command = [
        str(Path(sys.executable).resolve()),
        str(script),
        "run",
        "--profile",
        args.profile,
        "--remote",
        args.remote,
        "--ref",
        args.ref,
        "--hermes-cli",
        resolve_executable(args.hermes_cli, "hermes"),
        "--git-cli",
        resolve_executable(args.git_cli, "git"),
        "--health-timeout",
        str(args.health_timeout),
    ]
    if args.apply:
        command.append("--apply")
    return command


def macos_plist_payload(
    label: str,
    command: list[str],
    home: Path,
    interval: int,
) -> dict[str, Any]:
    logs = home / "logs"
    return {
        "Label": label,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "StartInterval": int(interval),
        "WorkingDirectory": str(home),
        "StandardOutPath": str(logs / "dispatch-update.log"),
        "StandardErrorPath": str(logs / "dispatch-update.error.log"),
        "EnvironmentVariables": {"HERMES_HOME": str(home), "PYTHONUNBUFFERED": "1"},
    }


def windows_task_create_args(
    task: str,
    task_command: str,
    interval: int,
    principal: str,
) -> list[str]:
    return [
        "schtasks",
        "/Create",
        "/SC",
        "MINUTE",
        "/MO",
        str(max(1, int(interval) // 60)),
        "/TN",
        task,
        "/TR",
        task_command,
        "/RL",
        "LIMITED",
        "/RU",
        principal,
        "/IT",
        "/F",
    ]


def install_scheduler(args: argparse.Namespace) -> dict[str, Any]:
    home = profile_home(args.profile)
    if not home.is_dir():
        raise UpdateError(f"Hermes profile does not exist: {home}")
    script = installed_script(home)
    atomic_write(script, Path(__file__).read_bytes(), 0o700)
    command = scheduler_arguments(args, script)
    label, task = scheduler_identity(args.profile)
    (home / "logs").mkdir(parents=True, exist_ok=True)

    if sys.platform == "darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        payload = macos_plist_payload(label, command, home, args.interval)
        atomic_write(plist_path, plistlib.dumps(payload), 0o600)
        domain = f"gui/{os.getuid()}"
        run(["launchctl", "bootout", domain, plist_path], check=False, timeout=30)
        run(["launchctl", "bootstrap", domain, plist_path], timeout=30)
        return {"status": "installed", "adapter": "launchd", "path": str(plist_path), "apply": args.apply}

    if sys.platform == "win32":
        domain = os.environ.get("USERDOMAIN", "").strip()
        user = os.environ.get("USERNAME", "").strip() or getpass.getuser()
        principal = f"{domain}\\{user}" if domain else user
        wrapper = home / "bin" / "hermes-dispatch-update.cmd"
        wrapper_body = "@echo off\r\n" + subprocess.list2cmdline(command) + "\r\n"
        atomic_write(wrapper, wrapper_body.encode("utf-8"))
        task_command = subprocess.list2cmdline(["cmd.exe", "/d", "/c", str(wrapper)])
        if len(task_command) > 261:
            raise UpdateError(f"Windows Scheduled Task command exceeds 261 characters: {task_command}")
        run(windows_task_create_args(task, task_command, args.interval, principal), timeout=45)
        run(["schtasks", "/Run", "/TN", task], check=False, timeout=30)
        return {"status": "installed", "adapter": "scheduled-task", "path": task, "apply": args.apply}

    raise UpdateError("scheduler installation currently supports macOS and native Windows only")


def uninstall_scheduler(args: argparse.Namespace) -> dict[str, Any]:
    home = profile_home(args.profile)
    label, task = scheduler_identity(args.profile)
    if sys.platform == "darwin":
        plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        run(["launchctl", "bootout", f"gui/{os.getuid()}", plist_path], check=False, timeout=30)
        plist_path.unlink(missing_ok=True)
        return {"status": "uninstalled", "adapter": "launchd"}
    if sys.platform == "win32":
        run(["schtasks", "/Delete", "/TN", task, "/F"], check=False, timeout=30)
        return {"status": "uninstalled", "adapter": "scheduled-task"}
    raise UpdateError("scheduler removal currently supports macOS and native Windows only")


def scheduler_status(profile: str) -> dict[str, Any]:
    home = profile_home(profile)
    label, task = scheduler_identity(profile)
    state = load_json(home / "state" / "hermes-dispatch-update.json", {})
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
        probe = run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], check=False, timeout=15)
        installed = plist.exists()
        running = probe.returncode == 0
        adapter = "launchd"
    elif sys.platform == "win32":
        probe = run(["schtasks", "/Query", "/TN", task], check=False, timeout=15)
        installed = probe.returncode == 0
        running = installed
        adapter = "scheduled-task"
    else:
        installed = running = False
        adapter = "unsupported"
    return {
        "profile": profile,
        "home": str(home),
        "adapter": adapter,
        "installed": installed,
        "registered": running,
        "state": state,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    sub = root.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser, *, scheduling: bool = False) -> None:
        command.add_argument("--profile", default="default")
        command.add_argument("--remote", default=DEFAULT_REMOTE)
        command.add_argument("--ref", default=DEFAULT_REF)
        command.add_argument("--hermes-cli", default="")
        command.add_argument("--git-cli", default="")
        command.add_argument("--health-timeout", type=float, default=90)
        command.add_argument("--apply", action="store_true", help="mutate the profile; omission is dry-run")
        if scheduling:
            command.add_argument("--interval", type=int, default=1800)

    common(sub.add_parser("run", help="check and optionally apply one update"))
    run_parser = sub.choices["run"]
    run_parser.add_argument(
        "--retry-blocked",
        action="store_true",
        help="manually retry the currently blocked commit once",
    )
    common(sub.add_parser("install", help="install the platform scheduler"), scheduling=True)
    status = sub.add_parser("status", help="show scheduler and update state")
    status.add_argument("--profile", default="default")
    uninstall = sub.add_parser("uninstall", help="remove the platform scheduler")
    uninstall.add_argument("--profile", default="default")
    return root


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "run":
            result = perform_update(args)
        elif args.command == "install":
            result = install_scheduler(args)
        elif args.command == "status":
            result = scheduler_status(args.profile)
        else:
            result = uninstall_scheduler(args)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (UpdateError, OSError, subprocess.SubprocessError) as exc:
        print(f"hermes-dispatch-update: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
