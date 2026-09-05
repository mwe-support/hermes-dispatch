"""Installer ownership, unrelated hook/trust preservation, upgrade and removal."""
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import sys
import tempfile
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("install_qq_hook_test", Path(__file__).with_name("install-codex-qq-hook.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
SOURCE = Path(__file__).resolve().parents[1] / "plugins/codex-app-server-phase-hotfix/qq_delivery_hook.py"


def main():
    with tempfile.TemporaryDirectory(prefix="qq hook install ") as tmp:
        root = Path(tmp).resolve()
        profiles, homes = [], []
        for name in ("first", "second"):
            profile, home = root / name, root / f"codex {name}"
            target = profile / "plugins/codex-app-server-phase-hotfix/qq_delivery_hook.py"
            target.parent.mkdir(parents=True)
            shutil.copy2(SOURCE, target)
            home.mkdir()
            profiles.append(profile)
            homes.append(home)
            (home / "config.toml").write_text('# existing native trust\n[hooks.state.example]\ntrusted_hash="keep-me"\n')
            (home / "auth.json").write_text('{"test_only":"preserve-auth"}')
        profile, home = profiles[0], homes[0]
        other = {"hooks": [{"type": "command", "command": "printf existing-hook"}]}
        initial = {"description": "preserve this", "hooks": {"UserPromptSubmit": [other], "Stop": []}}
        hooks_path = home / "hooks.json"
        hooks_path.write_text(json.dumps(initial))
        config = (home / "config.toml").read_bytes()
        auth = (home / "auth.json").read_bytes()

        assert "installed" in installer.manage(profile, home)
        installed = json.loads(hooks_path.read_text())
        assert installed["hooks"]["UserPromptSubmit"][0] == other
        entry = installed["hooks"]["UserPromptSubmit"][1]
        args = shlex.split(entry["hooks"][0]["command"])
        assert args[0] == str(Path(sys.executable).resolve())
        assert args[-4:] == ["--hermes-home", str(profile), "--codex-home", str(home)]
        before = hooks_path.read_bytes()
        assert "already installed" in installer.manage(profile, home)
        assert hooks_path.read_bytes() == before
        assert not (homes[1] / "hooks.json").exists()

        try:
            installer.manage(profiles[1], home)
            assert False, "cross-profile overwrite was accepted"
        except ValueError:
            pass
        assert hooks_path.read_bytes() == before

        # A hook added by the operator later keeps its matcher index through
        # upgrades/removal, so its existing Codex trust lookup is unaffected.
        later = {"hooks": [{"type": "command", "command": "printf later-hook"}]}
        installed["hooks"]["UserPromptSubmit"].append(later)
        hooks_path.write_text(json.dumps(installed))
        with (profile / "plugins/codex-app-server-phase-hotfix/qq_delivery_hook.py").open("a") as f:
            f.write("\n# next installed revision\n")
        installer.manage(profile, home)
        updated = json.loads(hooks_path.read_text())
        assert updated["hooks"]["UserPromptSubmit"][2] == later
        assert updated["hooks"]["UserPromptSubmit"][1] != entry, "changed script did not change native trust definition"
        installer.manage(profile, home, remove=True)
        expected = {**initial, "hooks": {"UserPromptSubmit": [other, {"hooks": []}, later], "Stop": []}}
        assert json.loads(hooks_path.read_text()) == expected
        assert (home / "config.toml").read_bytes() == config and (home / "auth.json").read_bytes() == auth

        installer.manage(profiles[1], homes[1])
        installer.manage(profiles[1], homes[1], remove=True)
        assert not (homes[1] / "hooks.json").exists(), "removal did not restore absent hooks.json"

        # Shared hook files and operator edits are never silently overwritten.
        shared = root / "shared-hooks.json"
        shared.write_text('{}')
        (homes[1] / "hooks.json").symlink_to(shared)
        try:
            installer.manage(profiles[1], homes[1])
            assert False, "shared symlink was accepted"
        except ValueError:
            pass
        assert shared.read_text() == '{}'
        (homes[1] / "hooks.json").unlink()

        # A partial two-file write failure restores the prior hook config.
        original_write = installer.write_bytes
        failed = False

        def fail_state_once(path, content):
            nonlocal failed
            if path.name == "qq-delivery-hook.json" and content is not None and not failed:
                failed = True
                raise OSError("test state write failure")
            original_write(path, content)

        with patch.object(installer, "write_bytes", fail_state_once):
            try:
                installer.manage(profiles[1], homes[1])
                assert False, "injected write failure was ignored"
            except OSError:
                pass
        assert not (homes[1] / "hooks.json").exists()
        assert not (homes[1] / ".hermes-dispatch/qq-delivery-hook.json").exists()
    print("install/update/remove, native trust indices, unrelated state, profile ownership and rollback: PASS")


if __name__ == "__main__":
    main()
