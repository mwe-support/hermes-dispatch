#!/usr/bin/env bash
set -uo pipefail

REMOTE=${HERMES_DISPATCH_REMOTE:-https://github.com/mwe-support/hermes-dispatch.git}
REF=${HERMES_DISPATCH_REF:-main}
WAIT_SECONDS=${HERMES_DISPATCH_WAIT_SECONDS:-3600}
POLL_SECONDS=${HERMES_DISPATCH_POLL_SECONDS:-15}
DRY_RUN=0

usage() {
  echo "Usage: $0 [--dry-run] [--wait-active SECONDS]"
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --wait-active)
      [[ ${2:-} =~ ^[0-9]+$ ]] || { usage >&2; exit 2; }
      WAIT_SECONDS=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

[[ $WAIT_SECONDS =~ ^[0-9]+$ && $POLL_SECONDS =~ ^[1-9][0-9]*$ ]] || {
  echo "wait and poll intervals must be non-negative integers" >&2
  exit 2
}

command -v git >/dev/null || { echo "git is required" >&2; exit 1; }
GIT=$(command -v git)

if [[ -n ${HERMES_DISPATCH_ROOT:-} ]]; then
  HERMES_ROOT=$HERMES_DISPATCH_ROOT
elif [[ $(uname -s) =~ ^(MINGW|MSYS|CYGWIN) && -n ${LOCALAPPDATA:-} ]] && command -v cygpath >/dev/null; then
  HERMES_ROOT=$(cygpath -u "$LOCALAPPDATA")/hermes
else
  HERMES_ROOT=$HOME/.hermes
fi

[[ -d $HERMES_ROOT && ! -L $HERMES_ROOT ]] || {
  echo "Hermes root is missing or unsafe: $HERMES_ROOT" >&2
  exit 1
}

PYTHON=
for candidate in \
  "$HERMES_ROOT/hermes-agent/venv/bin/python" \
  "$HERMES_ROOT/hermes-agent/.venv/Scripts/python.exe" \
  "$HERMES_ROOT/hermes-agent/venv/Scripts/python.exe"; do
  [[ -x $candidate ]] && { PYTHON=$candidate; break; }
done
if [[ -z $PYTHON ]] && command -v python3 >/dev/null; then
  PYTHON=$(command -v python3)
elif [[ -z $PYTHON ]] && command -v python >/dev/null; then
  PYTHON=$(command -v python)
fi
[[ -n $PYTHON ]] && "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' || {
  echo "Python 3.9 or newer is required" >&2
  exit 1
}

if command -v hermes >/dev/null; then
  HERMES=$(command -v hermes)
elif [[ -x $HERMES_ROOT/hermes-agent/hermes ]]; then
  HERMES=$HERMES_ROOT/hermes-agent/hermes
else
  echo "hermes is required" >&2
  exit 1
fi

tmp=
cleanup() { [[ -z $tmp ]] || rm -rf "$tmp"; }
trap cleanup EXIT INT TERM

if [[ -n ${HERMES_DISPATCH_SOURCE_DIR:-} ]]; then
  SOURCE=$HERMES_DISPATCH_SOURCE_DIR
else
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/hermes-dispatch-all.XXXXXX")
  SOURCE=$tmp/source
  "$GIT" clone --quiet --depth 1 --branch "$REF" "$REMOTE" "$SOURCE"
fi

UPDATER=$SOURCE/ops/hermes_dispatch_update.py
[[ -f $UPDATER ]] || { echo "updater not found in fetched source: $UPDATER" >&2; exit 1; }
COMMIT=$("$GIT" -C "$SOURCE" rev-parse HEAD)
[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "invalid fetched commit: $COMMIT" >&2; exit 1; }

profiles=(default)
unsafe=()
shopt -s nullglob
for path in "$HERMES_ROOT"/profiles/*; do
  profile=${path##*/}
  if [[ -L $path ]]; then
    unsafe+=("$profile:unsafe-profile-path")
    continue
  fi
  [[ -d $path ]] || continue
  [[ $profile =~ ^[a-z0-9][a-z0-9_-]{0,63}$ && $profile != default ]] || continue
  profiles+=("$profile")
done

ok=()
failed=()
for item in "${unsafe[@]-}"; do
  [[ -z $item ]] || failed+=("$item")
done
echo "Updating ${#profiles[@]} Hermes profile(s) to $COMMIT from $REF"

for profile in "${profiles[@]}"; do
  echo
  echo "==> $profile"
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while :; do
    out=$(mktemp "${TMPDIR:-/tmp}/hermes-dispatch-result.XXXXXX")
    err=$out.err
    args=("$UPDATER" run --profile "$profile" --remote "$REMOTE" --ref "$COMMIT" --hermes-cli "$HERMES" --git-cli "$GIT")
    ((DRY_RUN)) || args+=(--apply)
    "$PYTHON" "${args[@]}" >"$out" 2>"$err"
    rc=$?
    cat "$out"
    cat "$err" >&2
    status=
    if ((rc == 0)); then
      status=$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin).get("status", ""))' <"$out" 2>/dev/null || true)
    fi
    rm -f "$out" "$err"

    if [[ $status == deferred && $(date +%s) -lt $deadline ]]; then
      echo "Profile $profile has active agents; retrying in ${POLL_SECONDS}s"
      sleep "$POLL_SECONDS"
      continue
    fi
    if ((DRY_RUN)); then
      [[ $status =~ ^(dry-run-passed|already-tested|no-live-change)$ ]] && ok+=("$profile") || failed+=("$profile:$status")
    else
      [[ $status =~ ^(updated|no-live-change)$ ]] && ok+=("$profile") || failed+=("$profile:${status:-exit-$rc}")
    fi
    break
  done
done

echo
echo "Commit: $COMMIT"
echo "Updated: ${ok[*]:-(none)}"
if ((${#failed[@]})); then
  echo "Failed/deferred: ${failed[*]}" >&2
  exit 1
fi
echo "All Hermes profiles are current."
