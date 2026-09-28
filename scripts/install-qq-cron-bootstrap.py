#!/usr/bin/env python3
"""Preserve the QQ-only bootstrap through generated shell launchers, not core."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import tempfile

BEGIN = '# BEGIN hermes-dispatch QQ cron bootstrap'
END = '# END hermes-dispatch QQ cron bootstrap'
OLD_BLOCK = '''# BEGIN hermes-dispatch QQ cron bootstrap
if [ -n "${HERMES_QQ_CRON_CONTEXT:-}" ] && [ -n "${HERMES_QQ_CRON_BOOTSTRAP:-}" ]; then
    export PYTHONPATH="$HERMES_QQ_CRON_BOOTSTRAP"
else
    unset PYTHONPATH
fi
# END hermes-dispatch QQ cron bootstrap'''
BLOCK = OLD_BLOCK.replace(
    '[ -n "${HERMES_QQ_CRON_CONTEXT:-}" ] &&',
    '{ [ -n "${HERMES_QQ_CRON_CONTEXT:-}" ] || [ "${HERMES_QQ_CRON_AUTO_DELIVERY:-}" = "1" ]; } &&')


def install(launcher, home, remove=False):
    path = Path(launcher)
    # Never follow an entrypoint symlink into the actual Hermes checkout.
    if path.is_symlink():
        return 'unchanged: symlink entrypoint (no core edit)'
    try:
        text = path.read_text(encoding='utf-8')
    except (UnicodeError, FileNotFoundError):
        return 'unchanged: no generated shell launcher'
    if hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
        raise ValueError('launcher must be modified by its owning user')
    if BEGIN in text:
        existing = BLOCK if text.count(BLOCK) == 1 else OLD_BLOCK
        if text.count(existing) != 1:
            raise ValueError('existing QQ bootstrap block was modified; refusing overwrite')
        if not remove and existing == BLOCK:
            return 'already installed'
        updated = text.replace(existing, 'unset PYTHONPATH' if remove else BLOCK)
    else:
        if remove:
            return 'already absent'
        if not text.startswith(('#!/usr/bin/env bash\n', '#!/bin/bash\n', '#!/bin/sh\n')):
            return 'unchanged: entrypoint does not use the supported shell wrapper'
        if text.splitlines().count('unset PYTHONPATH') != 1:
            return 'unchanged: launcher does not clear PYTHONPATH'
        if path.resolve().is_relative_to((Path(home) / 'hermes-agent').resolve()):
            raise ValueError('refusing to edit an entrypoint inside Hermes core')
        updated = text.replace('\nunset PYTHONPATH\n', '\n' + BLOCK + '\n')
    backup = Path(home) / 'plugin-backups' / ('qq-cron-launcher-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(path, backup / path.name)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(updated)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return ('removed' if remove else 'installed') + '; backup=' + str(backup)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    parser.add_argument('--launcher', default=shutil.which('hermes'))
    parser.add_argument('--remove', action='store_true')
    args = parser.parse_args()
    print(install(args.launcher, args.home, args.remove) if args.launcher else 'unchanged: no hermes launcher found')
