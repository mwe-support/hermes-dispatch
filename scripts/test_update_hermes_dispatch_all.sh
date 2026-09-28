#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TMP=$(mktemp -d "${TMPDIR:-/tmp}/dispatch-all-test.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

HOME_DIR=$TMP/home
HERMES_ROOT=$HOME_DIR/.hermes
SOURCE=$TMP/source
BIN=$TMP/bin
mkdir -p "$HERMES_ROOT/profiles/alpha" "$HERMES_ROOT/profiles/beta" "$SOURCE/ops" "$SOURCE/scripts" "$SOURCE/plugins/qqbot-connect-hotfix" "$BIN"

git -C "$SOURCE" init -q -b main
git -C "$SOURCE" config user.email test@example.com
git -C "$SOURCE" config user.name Test
cp "$ROOT/scripts/update-hermes-dispatch-all.sh" "$SOURCE/update.sh"
cat >"$SOURCE/ops/hermes_dispatch_update.py" <<'PY'
import json, os, sys
profile = sys.argv[sys.argv.index("--profile") + 1]
ref = sys.argv[sys.argv.index("--ref") + 1]
with open(os.environ["CALL_LOG"], "a", encoding="utf-8") as handle:
    handle.write(f"{profile} {ref} {'--apply' in sys.argv}\n")
status = "blocked" if profile == os.environ.get("FAIL_PROFILE") else "updated" if "--apply" in sys.argv else "dry-run-passed"
print(json.dumps({"status": status}))
PY
git -C "$SOURCE" add .
git -C "$SOURCE" commit -qm fixture
COMMIT=$(git -C "$SOURCE" rev-parse HEAD)

cat >"$BIN/hermes" <<'SH'
#!/usr/bin/env bash
exit 0
SH
chmod +x "$BIN/hermes" "$SOURCE/update.sh"
cat >"$SOURCE/scripts/install-qq-cron-bootstrap.py" <<'PYCODE'
import os,sys
with open(os.environ['CALL_LOG'], 'a') as handle: handle.write('bootstrap\n')
if os.environ.get('FAIL_HOOK'): raise SystemExit(3)
PYCODE

CALL_LOG=$TMP/calls
PATH="$BIN:$PATH" CALL_LOG="$CALL_LOG" \
  HERMES_DISPATCH_ROOT="$HERMES_ROOT" HERMES_DISPATCH_SOURCE_DIR="$SOURCE" \
  "$SOURCE/update.sh" >/dev/null
diff -u <(printf 'bootstrap\ndefault %s True\nalpha %s True\nbeta %s True\n' "$COMMIT" "$COMMIT" "$COMMIT") "$CALL_LOG"

: >"$CALL_LOG"
env PATH="$BIN:$PATH" CALL_LOG="$CALL_LOG" \
  HERMES_DISPATCH_ROOT="$HERMES_ROOT" HERMES_DISPATCH_SOURCE_DIR="$SOURCE" \
  bash -s -- --dry-run <"$SOURCE/update.sh" >/dev/null
diff -u <(printf 'default %s False\nalpha %s False\nbeta %s False\n' "$COMMIT" "$COMMIT" "$COMMIT") "$CALL_LOG"

ln -s alpha "$HERMES_ROOT/profiles/linked"
if PATH="$BIN:$PATH" CALL_LOG="$CALL_LOG" \
  HERMES_DISPATCH_ROOT="$HERMES_ROOT" HERMES_DISPATCH_SOURCE_DIR="$SOURCE" \
  "$SOURCE/update.sh" >"$TMP/unsafe.out" 2>&1; then
  echo "unsafe profile path did not fail the aggregate command" >&2
  exit 1
fi
grep -q 'linked:unsafe-profile-path' "$TMP/unsafe.out"
rm "$HERMES_ROOT/profiles/linked"

: >"$CALL_LOG"
if PATH="$BIN:$PATH" CALL_LOG="$CALL_LOG" FAIL_PROFILE=alpha \
  HERMES_DISPATCH_ROOT="$HERMES_ROOT" HERMES_DISPATCH_SOURCE_DIR="$SOURCE" \
  "$SOURCE/update.sh" >"$TMP/failure.out" 2>&1; then
  echo "blocked profile did not fail the aggregate command" >&2
  exit 1
fi
grep -q 'Failed/deferred: alpha:blocked' "$TMP/failure.out"
diff -u <(printf 'bootstrap\ndefault %s True\nalpha %s True\nbeta %s True\n' "$COMMIT" "$COMMIT" "$COMMIT") "$CALL_LOG"

# A failed launcher prerequisite must not reach any profile updater.
: >"$CALL_LOG"
if PATH="$BIN:$PATH" CALL_LOG="$CALL_LOG" FAIL_HOOK=1 \
  HERMES_DISPATCH_ROOT="$HERMES_ROOT" HERMES_DISPATCH_SOURCE_DIR="$SOURCE" \
  "$SOURCE/update.sh" >"$TMP/hook-failure.out" 2>&1; then
  echo "failed hook prerequisite was ignored" >&2; exit 1
fi
[[ $(cat "$CALL_LOG") == bootstrap ]]
echo "all-profile discovery, pinned commit, apply, dry-run and aggregate failure: PASS"
