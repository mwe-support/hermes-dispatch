#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


MODULE_PATH = Path(__file__).with_name("hermes_dispatch_update.py")
SPEC = importlib.util.spec_from_file_location("hermes_dispatch_update", MODULE_PATH)
assert SPEC and SPEC.loader
ops = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ops
SPEC.loader.exec_module(ops)


def write_plugin(root: Path, name: str, version: str, content: str) -> Path:
    plugin = root / "plugins" / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(f"name: {name}\nversion: {version}\n")
    (plugin / "runtime.py").write_text(content)
    return plugin


def make_fixture_repo(base: Path) -> Path:
    source = base / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", source], check=True)
    subprocess.run(["git", "-C", source, "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", source, "config", "user.name", "Test"], check=True)
    write_plugin(source, "sample", "2", "new")
    test = source / "ops/smoke.py"
    test.parent.mkdir(exist_ok=True)
    test.write_text("print('smoke=ok')\n")
    manifest = {
        "schema": 1,
        "minimum_hermes": "0.20.5",
        "plugins": ["sample"],
        "enable_plugins": ["sample"],
        "enable_tools": [],
        "config_set": {},
        "env_set_if_missing": {},
        "tests": ["ops/smoke.py"],
    }
    (source / "ops/release.json").write_text(json.dumps(manifest))
    subprocess.run(["git", "-C", source, "add", "."], check=True)
    subprocess.run(["git", "-C", source, "commit", "-qm", "fixture"], check=True)
    return source


class UpdaterTests(unittest.TestCase):
    def test_native_profile_paths(self) -> None:
        home = Path("/Users/team")
        self.assertEqual(
            ops.profile_home("default", platform="darwin", home=home),
            home / ".hermes",
        )
        self.assertEqual(
            ops.profile_home("sales", platform="darwin", home=home),
            home / ".hermes" / "profiles" / "sales",
        )
        self.assertEqual(
            ops.profile_home(
                "default",
                platform="win32",
                home=Path("C:/Users/team"),
                local_appdata="C:/Users/team/AppData/Local",
            ),
            Path("C:/Users/team/AppData/Local/hermes"),
        )
        with self.assertRaises(ops.UpdateError):
            ops.profile_home("../escape", platform="darwin", home=home)

    def test_named_profile_uses_shared_hermes_install(self) -> None:
        root = Path("/Users/team/.hermes")
        profile = root / "profiles" / "sales"
        self.assertEqual(ops.installation_root(root), root)
        self.assertEqual(ops.installation_root(profile), root)

    def test_manifest_rejects_unmanaged_and_unsafe_fields(self) -> None:
        manifest = {
            "schema": 1,
            "minimum_hermes": "0.20.5",
            "plugins": ["qqbot-connect-hotfix"],
            "enable_plugins": ["qqbot-connect-hotfix"],
            "enable_tools": ["message_snapshot"],
            "config_set": {"model.openai_runtime": "codex_app_server"},
            "env_set_if_missing": {"HERMES_CODEX_SESSION_PROJECTS_ENABLED": "true"},
            "tests": ["plugins/test_hotfix.py"],
        }
        self.assertEqual(ops.validate_manifest(manifest), manifest)
        poisoned = dict(manifest, config_set={"platforms.qqbot.secret": "leak"})
        with self.assertRaises(ops.UpdateError):
            ops.validate_manifest(poisoned)
        unsafe_test = dict(manifest, tests=["../steal.py"])
        with self.assertRaises(ops.UpdateError):
            ops.validate_manifest(unsafe_test)

    def test_env_defaults_preserve_existing_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# keep\nA=operator\n")
            changed = ops.apply_env_defaults(path, {"A": "default", "B": "new"})
            self.assertEqual(changed, ["B"])
            self.assertEqual(path.read_text(), "# keep\nA=operator\nB=new\n")
            self.assertEqual(ops.apply_env_defaults(path, {"B": "other"}), [])

    def test_tree_digest_ignores_bytecode_and_rejects_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "plugin"
            root.mkdir()
            (root / "plugin.yaml").write_text("name: p\nversion: 1\n")
            before = ops.tree_digest(root)
            cache = root / "__pycache__"
            cache.mkdir()
            (cache / "x.pyc").write_bytes(b"ignored")
            self.assertEqual(before, ops.tree_digest(root))
            if hasattr(os, "symlink"):
                link = root / "escape"
                try:
                    link.symlink_to(root / "plugin.yaml")
                except OSError:
                    return
                with self.assertRaises(ops.UpdateError):
                    ops.tree_digest(root)

    def test_plugin_replace_and_exact_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            checkout = base / "checkout"
            home = base / "profile"
            old = write_plugin(home, "sample", "1", "old")
            write_plugin(checkout, "sample", "2", "new")
            backup, metadata = ops.backup_live_state(home, ["sample"], "a" * 40)
            stage = ops.stage_plugins(checkout, home, ["sample"], "a" * 40)
            ops.replace_plugins(home, stage, ["sample"])
            self.assertEqual((home / "plugins/sample/runtime.py").read_text(), "new")
            ops.restore_backup(home, backup, metadata)
            self.assertEqual((home / "plugins/sample/runtime.py").read_text(), "old")
            self.assertTrue(old.parent.is_dir())

    def test_active_plugin_link_is_rejected_before_backup(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home = base / "profile"
            outside = write_plugin(base / "outside", "sample", "1", "secret")
            (home / "plugins").mkdir(parents=True)
            try:
                (home / "plugins/sample").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation unavailable")
            with self.assertRaises(ops.UpdateError):
                ops.preflight_active_plugins(home, ["sample"])
            self.assertEqual((outside / "runtime.py").read_text(), "secret")

    def test_gateway_running_requires_a_live_pid(self) -> None:
        self.assertTrue(ops.gateway_running({"gateway_state": "running", "pid": os.getpid()}))
        self.assertFalse(ops.gateway_running({"gateway_state": "stopped", "pid": os.getpid()}))

    def test_desired_state_detects_disabled_plugin(self) -> None:
        manifest = {
            "config_set": {},
            "env_set_if_missing": {},
            "enable_plugins": ["sample"],
            "enable_tools": ["message_snapshot"],
        }
        disabled = subprocess.CompletedProcess([], 0, "Status: disabled\n", "")
        tools = subprocess.CompletedProcess([], 0, "✓ enabled  message_snapshot\n", "")
        with patch.object(ops, "run", side_effect=[disabled, tools]):
            drift = ops.desired_state_drift("hermes", "default", Path("/tmp/missing"), manifest)
        self.assertEqual(drift, ["plugin:sample"])

    def test_dry_run_fetches_and_tests_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_fixture_repo(base)
            home = base / "home"
            home.mkdir()
            args = argparse.Namespace(
                profile="default",
                remote=str(source),
                ref="main",
                hermes_cli=sys.executable,
                git_cli=shutil_which_git(),
                health_timeout=1,
                apply=False,
                retry_blocked=False,
            )
            with patch.object(ops, "profile_home", return_value=home), patch.object(
                ops, "hermes_version", return_value=(0, 20, 5)
            ), patch.object(
                ops, "desired_state_drift", return_value=[]
            ):
                result = ops.perform_update(args)
            self.assertEqual(result["status"], "dry-run-passed")
            self.assertEqual(result["plugins"], ["sample"])
            self.assertFalse((home / "plugins/sample").exists())
            state = json.loads((home / "state/hermes-dispatch-update.json").read_text())
            self.assertEqual(state["last_result"], "dry-run-passed")

    def test_failed_apply_restores_plugin_and_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_fixture_repo(base)
            home = base / "home"
            home.mkdir()
            write_plugin(home, "sample", "1", "old")
            args = argparse.Namespace(
                profile="default",
                remote=str(source),
                ref="main",
                hermes_cli=sys.executable,
                git_cli=shutil_which_git(),
                health_timeout=1,
                apply=True,
                retry_blocked=False,
            )
            with patch.object(ops, "profile_home", return_value=home), patch.object(
                ops, "hermes_version", return_value=(0, 20, 5)
            ), patch.object(
                ops, "desired_state_drift", return_value=[]
            ), patch.object(
                ops, "run_regressions"
            ), patch.object(
                ops, "qqbot_enabled", return_value=False
            ), patch.object(
                ops, "apply_managed_settings", side_effect=ops.UpdateError("boom")
            ):
                with self.assertRaises(ops.UpdateError):
                    ops.perform_update(args)
            self.assertEqual((home / "plugins/sample/runtime.py").read_text(), "old")
            state = json.loads((home / "state/hermes-dispatch-update.json").read_text())
            self.assertEqual(state["last_result"], "rolled-back")
            self.assertEqual(len(state["blocked_commit"]), 40)

    def test_active_profile_defers_before_regressions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = make_fixture_repo(base)
            home = base / "home"
            home.mkdir()
            args = argparse.Namespace(
                profile="default",
                remote=str(source),
                ref="main",
                hermes_cli=sys.executable,
                git_cli=shutil_which_git(),
                health_timeout=1,
                apply=True,
                retry_blocked=False,
            )
            regression = Mock()
            with patch.object(ops, "profile_home", return_value=home), patch.object(
                ops, "desired_state_drift", return_value=[]
            ), patch.object(
                ops, "read_gateway_state", return_value={"gateway_state": "running", "active_agents": 1}
            ), patch.object(
                ops, "run_regressions", regression
            ):
                result = ops.perform_update(args)
            self.assertEqual(result, {"status": "deferred", "commit": result["commit"], "active_agents": 1})
            regression.assert_not_called()

    def test_scheduler_arguments_default_to_dry_run(self) -> None:
        args = argparse.Namespace(
            profile="sales",
            remote="https://example.invalid/repo.git",
            ref="main",
            hermes_cli=sys.executable,
            git_cli=shutil_which_git(),
            health_timeout=90,
            apply=False,
        )
        command = ops.scheduler_arguments(args, Path("/tmp/updater.py"))
        self.assertNotIn("--apply", command)
        args.apply = True
        self.assertIn("--apply", ops.scheduler_arguments(args, Path("/tmp/updater.py")))

    def test_macos_launchagent_contract_is_profile_scoped(self) -> None:
        home = Path("/Users/team/.hermes/profiles/sales")
        payload = ops.macos_plist_payload(
            "com.mwe-support.hermes-dispatch-update.sales",
            ["/usr/bin/python3", str(home / "bin/updater.py"), "run"],
            home,
            1800,
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 1800)
        self.assertEqual(payload["EnvironmentVariables"]["HERMES_HOME"], str(home))
        self.assertTrue(payload["StandardErrorPath"].startswith(str(home)))

    def test_windows_task_contract_is_interactive_and_limited(self) -> None:
        command = [
            r"C:\Program Files\Python\python.exe",
            r"C:\Users\team\AppData\Local\hermes\bin\update.py",
            "run",
            "--apply",
        ]
        args = ops.windows_task_create_args(
            "Hermes_Dispatch_Update_default",
            command,
            1800,
            r"ORG\team",
        )
        self.assertIn("/IT", args)
        self.assertEqual(args[args.index("/RL") + 1], "LIMITED")
        self.assertEqual(args[args.index("/RU") + 1], r"ORG\team")
        self.assertEqual(args[args.index("/MO") + 1], "30")
        self.assertIn("python.exe", args[args.index("/TR") + 1])


def shutil_which_git() -> str:
    import shutil

    value = shutil.which("git")
    assert value
    return value


if __name__ == "__main__":
    unittest.main()
