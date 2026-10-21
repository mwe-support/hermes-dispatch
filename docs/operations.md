# Operations

## Install Plugins

From the repository root:

```bash
scripts/install-plugins.sh "$HOME/.hermes"
```

Inside a containerized deployment the target path is the host directory mounted
as `/opt/data`.

When an active plugin directory already exists, the installer copies it first
to a timestamped path below `$HERMES_HOME/plugin-backups`. This location is
outside the recursive `plugins` discovery root, so the preserved `plugin.yaml`
cannot register as a second plugin. The copy includes hidden files and is
completed before the active directory is replaced. A first installation does
not create an empty backup.

The backup root and active plugin directory must be real directories, not
symbolic links. The installer resolves canonical paths and requires the active
target to be exactly one direct child below the canonical `plugins` root; it
also rejects `.` and `..` as plugin names. These checks run before backup,
clear, or copy operations. When several plugins are requested, the installer
preflights and retains every canonical active target before mutating the first;
if a later target is invalid, all earlier active plugin directories remain
byte-for-byte unchanged and no backup is created. Backup-root validation also
runs before an absent active plugin directory is created, so a rejected fresh
install leaves no empty plugin artifact. Fix a rejected profile layout instead
of bypassing the guard.

To restore an exact installer-created copy:

```bash
scripts/install-plugins.sh --restore \
  "$HOME/.hermes" \
  qqbot-connect-hotfix \
  "$HOME/.hermes/plugin-backups/qqbot-connect-hotfix-<version>-<timestamp>"
```

Restore mode rejects a source below `$HERMES_HOME/plugins`, verifies that its
manifest names the requested plugin, and backs up the currently active copy
before replacing it. Run `hermes plugins list`, restart only the target
profile's Gateway, and verify its channel connection after either install or
restore. Do not delete the backup until that verification passes.

Enable plugins:

```bash
hermes plugins enable qqbot-connect-hotfix
hermes plugins enable codex-app-server-phase-hotfix
hermes plugins enable message-snapshot-store
hermes plugins enable whatsapp-bridge-policy-hotfix
```

Restart Hermes gateway after enabling or updating plugins.

Exercise both installation and rollback safety without touching a real profile:

```bash
scripts/test_install_plugins.sh
```

## Automatic Updates on macOS and Windows

### macOS launchd domain and Codex Keychain compatibility (#21)

Hermes 0.21.0 probes a Gateway's loaded service to choose its launchd domain.
After an updater's separate stop/start processes unload that service, a
Background caller instead selects `user/<uid>`. QQ/API connections can recover
while Codex `keyring` credentials are unavailable and real requests fail with
401/Missing bearer. Platform connectivity is not authentication acceptance.

Dispatch now records each profile's exact domain before stopping it and passes
the target domain explicitly to both normal and rollback starts. The removable
`ops/macos_launchd.py` CLI guard persists domains below the owner's Hermes
`state/dispatch-launchd-domains.json`; it also guards service-definition refresh
and ordinary restart. Explicit Codex app-server `keyring` profiles require
`gui/<uid>` and Aqua-only plists. Other profiles keep their existing domain and
session types. This applies to named profiles as well as default. Unspecified
credential-store settings are not reclassified as keyring.

The generated host launcher and the plist's core CLI entrypoint route through
`~/.hermes/scripts/dispatch-macos-launchd.py`. The installer preserves the QQ
cron bootstrap and unrelated shell content. It changes no Hermes core source,
Codex credential storage, tokens, profile model/provider or approval policy.
Run as the owning macOS department user:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/install-macos-launchd-guard.py
```

The all-profile update entrypoint installs this prerequisite automatically.
Updates wait for active work; GUI unavailability or duplicate service
registrations fail explicitly before stopping the Gateway. A guarded service
never silently falls back to a detached Background process. Keychain-backed
services require that macOS user to remain logged into a GUI session; this
workaround does not provide unattended authentication after logout/reboot.

Restart an idle affected profile through the guarded CLI to migrate its wrong
user-domain job. The guard waits for the previous PID, API bind port and launchd
registration to disappear before relaunching, compensating for upstream's
quick restart/late-unregistration race. Verify the exact label in gui and user
domains and issue a no-tool fixed-marker model request; require the completed
terminal state and exact output, not only HTTP 200 or `codex login status`.
Run `ops/test_macos_launchd.py`, `scripts/test_macos_launchd_install.py` and the
updater regressions. Live acceptance must include a genuine Background caller,
independent stop/start, ordinary restart, service-definition refresh and
rollback, followed by real model requests.

The updater also preserves owner-validated local plugins whose semantic
version is newer than the fetched release, reports `preserved_local_plugins`,
and verifies their full code digest remains unchanged. Equal-version drift
still follows the release. This prevents the finance profile's 1.8.9 approval
repair from being downgraded to the release's 1.8.8; it does not automatically
merge local repairs into the remote repository.

The installer prints an owner-local `plugin-backups/macos-launchd-*` snapshot
covering its launcher, guard, policy and plists. Stop the relevant idle services,
restore that snapshot, then restart them in their recorded original domains:

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/install-macos-launchd-guard.py \
  --restore /path/to/.hermes/plugin-backups/macos-launchd-TIMESTAMP
```

Updater plugin rollback retains this independent domain guard and uses its
captured start domain. Remove/restore the guard separately when upstream fixes
domain selection and service refresh. Restoring an old user-domain keyring
setup can reintroduce the authentication failure.

### One-command update for every profile

macOS/Linux operators can update the default profile and every named profile
from the latest `main` without choosing a branch or commit:

```bash
curl -fsSL https://raw.githubusercontent.com/mwe-support/hermes-dispatch/main/scripts/update-hermes-dispatch-all.sh | bash
```

The script fetches `main` once, discovers profiles below the native Hermes
root, then invokes the existing profile-scoped updater sequentially with
`--apply`. Each profile retains its own lock, state, backup, health check and
rollback. Profiles with active agents are retried for up to one hour by
default; override that bound with `--wait-active SECONDS`. Any blocked,
deferred, or failed profile makes the command exit nonzero after the remaining
profiles have been attempted. Preview the same pinned update without mutation:

```bash
curl -fsSL https://raw.githubusercontent.com/mwe-support/hermes-dispatch/main/scripts/update-hermes-dispatch-all.sh | bash -s -- --dry-run
```

Windows PowerShell operators can run the native equivalent without Git Bash:

```powershell
irm https://raw.githubusercontent.com/mwe-support/hermes-dispatch/main/scripts/update-hermes-dispatch-all.ps1 | iex
```

For a PowerShell preview or custom wait, save the script locally and run it with
`-DryRun` or `-WaitActiveSeconds SECONDS`. Both entrypoints fetch `main` by
default and pin the fetched commit internally for that run, so every profile
receives the same code. Native Scheduled Tasks remain owned by the underlying
Python updater. Roll back an applied profile from its latest `update-backups` snapshot
or remove the managed plugin directories/settings using the per-profile
procedure below.

`ops/hermes_dispatch_update.py` is a host-side updater, not a Gateway plugin.
That separation is intentional: it can repair or roll back dispatch plugins
when the Gateway is stopped or broken, and it never replaces code from inside
the process currently importing that code.

The updater tracks reviewed `origin/main`, validates `ops/release.json`, runs
the declared regressions with a temporary `HERMES_HOME`, and changes only the
profile named on the command line. The default mode is read-only:

```bash
python3 ops/hermes_dispatch_update.py run --profile default
```

The result is `dry-run-passed`, `already-tested`, `blocked`, or a failure. A
dry run downloads and tests the candidate but does not alter plugins, config,
the environment file, hooks, or the Gateway. Enable live deployment only after
that succeeds:

```bash
python3 ops/hermes_dispatch_update.py install --profile default --apply
```

On macOS this installs a per-profile LaunchAgent below
`~/Library/LaunchAgents`, with `RunAtLoad` and a 30-minute interval. On native
Windows it installs a current-user, interactive, limited Scheduled Task named
`Hermes_Dispatch_Update_<profile>`. Both adapters use absolute Python, Git and
Hermes paths captured at install time. They run as the department user, never
as root or `SYSTEM`, so Codex keyring/plugin authentication and profile-owned
files keep the same owner.

Windows uses `%LOCALAPPDATA%\hermes` for the default profile; macOS uses
`~/.hermes`. Named profiles live below `profiles/<name>` on both platforms.
The existing Bash installer remains available for manual macOS/Linux work, but
the scheduled updater uses its Python implementation so Windows receives the
same preflight, backup, replacement and rollback behavior without requiring
Git Bash or WSL.

The Windows Scheduled Task invokes a short profile-local `.cmd` wrapper. The
wrapper holds the absolute Python, Git, Hermes and updater arguments; keeping
the Task Scheduler `/TR` value short avoids Windows' 261-character action
limit without relying on shell `PATH` lookup.

Inspect scheduler registration and the last durable outcome:

```bash
python3 ops/hermes_dispatch_update.py status --profile default
```

Remove only the scheduler, preserving updater state, installed plugins and all
Hermes data:

```bash
python3 ops/hermes_dispatch_update.py uninstall --profile default
```

### Managed State and Safety

Each profile owns its own files:

```text
$HERMES_HOME/state/hermes-dispatch-update.json
$HERMES_HOME/state/hermes-dispatch-update.lock
$HERMES_HOME/updates/hermes-dispatch/
$HERMES_HOME/update-staging/
$HERMES_HOME/update-backups/
```

Only keys named in `ops/release.json` may be written. Environment defaults are
added only when absent. QQ credentials, allowlists, `CODEX_HOME`, ports,
WhatsApp state, unrelated hooks and all other local settings are outside the
updater's Interface and remain untouched. It never upgrades Hermes or Codex;
an incompatible commit is recorded as `blocked_by_hermes_version`.

The updater passes plain scalar values to `hermes config set`. Hermes treats
quote characters in string arguments literally; older updater builds
JSON-encoded strings, which could save `model.openai_runtime` as
`"codex_app_server"` instead of `codex_app_server` and disable the app-server
runtime. Apply the fixed updater to each affected profile, then verify with
`hermes -p <profile> config get model.openai_runtime` and
`hermes -p <profile> config get compression.codex_app_server_auto`: the values
must be `codex_app_server` and `native` without printed quote characters.
If an update fails, restore that profile's `config.yaml` from its matching
`update-backups/<commit>-<timestamp>` snapshot using the rollback procedure
below.

Before live mutation, every managed plugin is staged on the profile's own
filesystem. Plugin roots, staging roots and backup roots must be real
directories, not symlinks, junctions or Windows reparse points. All targets are
preflighted before the Gateway is stopped. If `gateway_state.json` reports an
active Agent, deployment is deferred instead of interrupting the turn.

When the profile is idle, the updater:

1. saves `config.yaml`, `.env`, and every changing plugin;
2. stops only the target profile;
3. swaps the staged plugin directories into place;
4. applies the allowlisted config/default values and enables the declared
   plugins and QQ toolsets;
5. verifies `config check` and installed tree hashes;
6. restarts the Gateway only if it was running before the update;
7. requires a new QQ `Ready` log record and a deep Gateway status check.

Any failure restores the exact saved files and plugins, restarts the prior
Gateway when applicable, and records the candidate commit as blocked. The
automatic scheduler will not retry that commit. After correcting the cause, an
operator may deliberately retry it once:

```bash
python3 ops/hermes_dispatch_update.py run \
  --profile default --apply --retry-blocked
```

The optional Codex QQ delivery hook is not silently trusted. Its script and
definition may be updated separately, but a changed digest still requires
human review through `/hooks`; plugin enablement is not evidence of hook trust.

### Verification and Rollback

The release manifest includes the updater's own regression plus every managed
plugin regression. Test it without installing a scheduler:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 ops/test_hermes_dispatch_update.py -v
python3 ops/hermes_dispatch_update.py run --profile default
```

For a live canary, install the scheduler without `--apply`, inspect its state
and logs for at least one cycle, then reinstall it with `--apply`. Successful
deployment requires the recorded commit, expected plugin hashes, enabled
plugins/toolsets, a supervised target Gateway, and a fresh QQ `Ready` line.
Backups are retained under `update-backups`; do not delete the latest successful
pre-update backup until channel acceptance is complete.

## Permanent Message Snapshots

`message-snapshot-store` 1.1.0 registers an explicit raw-event hook that
`qqbot-connect-hotfix` 1.5.3 invokes before suppressing passive group routing.
It also wraps Hermes 0.20's deferred WhatsApp adapter and captures the
normalized Baileys bridge event before the mention-response gate. It therefore
captures raw QQ and WhatsApp events regardless of plugin load order into:

```text
$HERMES_HOME/message-snapshots/snapshots.sqlite3
```

QQ defaults a group robot to mention-only delivery. The group owner can open
the group's robot settings and enable **获取全部群消息**. Once enabled, ordinary
messages arrive as `GROUP_MESSAGE_CREATE` and are captured before routing. If
the setting remains off, unmentioned text and media are outside the observable
event stream and cannot be snapshot. To recover older media in that mode,
reply to/quote it and mention the bot; a `message_type=103` event can carry the
referenced attachment.

The native group-owner setting is the authorization boundary and does not need
to be toggled or reconfirmed when it is already effective. Hermes 0.18.2 also
acknowledges QQ connector configuration interactions without their required
`claw_cfg`; the hotfix handles interaction types 2001/2002 when QQ sends them,
but that separate compatibility exchange is not a prerequisite for native
full-message delivery.

`qqbot-connect-hotfix` 1.5.2 and later also gate QQ's broad
`GROUP_AT_MESSAGE_CREATE` label with `mentions[].is_you`. Messages mentioning
the owner or another member remain snapshot/context input and do not wake the
agent as though the bot itself had been mentioned.

The default `MESSAGE_SNAPSHOT_MEDIA_STORAGE=link` records QQ media URLs and
metadata without copying bytes. `/message-snapshot restore <id>` explicitly downloads
and pins an attachment when the current QQ credentials can still access its
URL. Set `MESSAGE_SNAPSHOT_MEDIA_STORAGE=mirror` only when offline permanence
is worth the storage cost.

WhatsApp is different: Baileys decrypts inbound media into a local cache file,
and the current Hermes bridge does not expose a durable plaintext CDN URL.
WhatsApp media is therefore always streamed into the SHA-256 archive at capture
time, even when QQ remains in `link` mode. If Baileys cannot download media,
the database records an unavailable attachment marker but cannot restore bytes
that never reached the bridge.

Recent group context defaults to 20 messages and approximately 4000 tokens:

```text
MESSAGE_SNAPSHOT_CONTEXT_MESSAGES=20
MESSAGE_SNAPSHOT_CONTEXT_TOKENS=4000
```

## QQ Long-Turn Reply Delivery

`qqbot-connect-hotfix` 1.7.0 compensates for QQ reply anchors expiring while an
agent turn is still running. The first send remains a normal reply. If QQ
explicitly reports the `msg_id` or `message_id` as expired, the plugin retries
the same C2C, group, approval-keyboard, or guild text once as a standalone
message. Keyboard data is retained; unrelated errors are not retried.

This is only a channel-delivery fallback. It does not itself extend Codex
app-server's wall-clock limit, persist a running task, or cover media sends. A successful
live check is a long turn whose final text appears without a reply relationship
after the initial referenced send is rejected. The Gateway log should contain
`reply anchor expired` followed by no second send error.

## Codex App-Server Compatibility

`codex-app-server-phase-hotfix` converts completed Codex app-server
`imageGeneration` items into image files below `$HERMES_HOME/cache/images` and
standard `image_generate`/`MEDIA:` output. This compensates for Hermes 0.18.2
truncating the opaque base64 item and dropping media-only turns at its
empty-response branch. The existing QQ adapter then performs native image
upload; no container source file is modified.

Version 1.4.0 also bridges Codex command execution, file change, permission,
and bundled Computer Use app-authorization requests into Hermes' registered
Gateway approval queue. This compensates for Hermes 0.18.2 using only the
terminal callback for command/file approvals and hard-coding permission and
non-Hermes MCP elicitation requests to decline. On Codex 0.145.0, permission
approval returns the requested, schema-filtered subset; denial or timeout
returns an empty subset. Computer Use approval returns the MCP elicitation
response and lets Codex own session/permanent app policy.

Version 1.5.0 compensates for Hermes 0.20.0's fixed 600-second Codex
app-server wall deadline. With
`HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS=0` (the plugin default), a healthy
turn waits for `turn/completed`; `/stop`, steer/interrupt, process-exit checks
and the post-tool watchdog remain active. A positive value restores a finite
deadline. At that deadline, unterminated commentary is converted to a failed
turn and the Codex subprocess is retired rather than returned as final output.

Hermes' outer Gateway timeout is inactivity-based and independent. Set it above
the longest legitimate silent task, for example:

```bash
hermes config set agent.gateway_timeout 7200
```

Restart draining is independent. Hermes 0.20.0 defaults
`agent.restart_drain_timeout` to zero, which makes an explicit restart force
immediately. Set it to a bounded value such as 300 seconds on a multi-session
Gateway. Timeout/terminal tracking is local to each Codex session, and
regression tests run two sessions concurrently to ensure results and callbacks
do not cross. Messages arriving in the same chat still follow
`display.busy_input_mode`.

Version 1.7.0 also compensates for Hermes 0.20.0 keeping the Codex thread id
only on the cached `AIAgent`. It persists one Codex project per stable Gateway
`session_key` and one Codex thread per Hermes `session_id`. `/new` and `/reset`
therefore create a new session-id-named thread in the same project; Gateway
restart and cache eviction resume the existing thread. The default project's
folder name is the exact `session_key` on macOS/Linux; Windows uses the same
readable key with `:` replaced by `：`. Legacy 1.6.x folders are migrated on
their next access. Inspect the current mapping with:

```text
/codex-project status
/codex-project list_threads
```

Codex app-server cwd grouping is separate from Codex Desktop's sidebar list.
On a desktop host, enable one-time registration through the CLI-owned,
cross-platform launcher:

```dotenv
HERMES_CODEX_APP_REGISTER_PROJECTS=true
# Default true; set false only to skip old-route project scaffolding:
HERMES_CODEX_SESSION_PROJECTS_BACKFILL=true
# Optional when Hermes uses a non-default Codex executable:
HERMES_CODEX_APP_CLI=/absolute/path/to/codex
```

After the next Codex turn, `/codex-project status` reports registration state.
The plugin schedules `codex app <project-path>` outside the Agent turn and runs
it once per successfully registered path; this may launch or focus Codex
Desktop but does not delay the channel reply. Leave it disabled on headless
Linux, services without an interactive desktop, and containers. For Docker,
run `codex app <host-mounted-project-path>` on the host as the same desktop
account. A detached macOS Gateway is bridged into the logged-in Aqua session
with `launchctl asuser`; Linux and Windows desktop processes invoke the CLI
directly. Registration failure is logged and retried after a cooldown.

At Gateway/plugin load, existing routes from Hermes' own `sessions.json` get
missing project directories and mappings automatically. Their first later
message creates the correctly named Codex thread. Historical Codex threads
whose old cwd did not identify a route are deliberately not guessed or moved.

For an operator-approved project alias, use:

```text
/codex-project bind finance
/codex-project default
```

The command route is channel-neutral: a pre-dispatch hook resolves the current
Hermes routing entry with the Gateway's own session-key generator before plugin
slash-command dispatch. Test `status` independently in every enabled channel
(for example QQ and Baileys WhatsApp); no platform-specific identifier parsing
is used by this plugin.

The same operations are available to the Agent through
`codex_session_project`, but `bind`/`default` require the sender to be listed in
`HERMES_CODEX_PROJECT_ADMIN_USERS`. Paths must be aliases or fall below
`HERMES_CODEX_PROJECT_ALLOWED_ROOTS`. A binding change takes effect on the next
Codex turn and must not interrupt another channel's task.

`qqbot-connect-hotfix` 1.6.0 renders **本次允许**, **会话允许**, and **拒绝**
for every bridged request. It adds **始终允许同类** only when Codex supplied a
persistent exec-policy/network-policy amendment or an elicitation advertised
permanent persistence. It also makes those buttons usable when
`group_sessions_per_user: false`: each prompt carries a short-lived opaque
nonce bound to the member who initiated the current turn. Another member, a
different group, an expired token, or a repeated click cannot resolve the
pending approval. Typed `/approve` and `/deny` commands enforce the same owner
binding.

After updating the plugin, restart the gateway and ask Codex to generate one
image. Verification succeeds when the gateway log reports the materialized
cache path and QQ receives the image rather than an empty response.

Run both plugin regressions first. They drive the real Hermes approval queue and
QQ keyboard serializer without contacting QQ:

```text
python plugins/codex-app-server-phase-hotfix/test_hotfix.py
python plugins/qqbot-connect-hotfix/test_hotfix.py
```

For a live manual approval test, temporarily use
`approvals_reviewer="user"`; `auto_review` intentionally resolves eligible
requests before they reach QQ. Ask Codex through QQ to perform an operation that
requires network access or an additional writable path. The QQ chat must receive
an approval request while the turn remains blocked. Verify **本次允许**,
**会话允许**, and **拒绝**. **始终允许同类** must appear only when the request
contains a proposed persistent policy amendment, and must suppress later
matching prompts. Restore `approvals_reviewer="auto_review"` after the manual
test. Do not enable `/yolo` or set `approvals.mode: off`; those modes
intentionally bypass the human approval boundary.

To verify Computer Use specifically, first remove the test app from Codex App's
Computer Use allowlist, then ask through QQ to operate a non-forbidden app such
as Notes. The QQ approval card must name the app. Verify rejection by another
group member, approval by the requester, successful continuation of the same
turn, and **始终允许** reuse on a later turn. A hard safety-denied app must remain
blocked without a bypass prompt.

Check capture and FTS5 availability with `/message-snapshot stats`. Search examples:

```text
/message-snapshot search 关键词 chat_id=<id>
/message-snapshot search attachment_filename=report.pdf
/message-snapshot search field_path=author.member_openid value=<id>
```

## WhatsApp Group Response Control

Use:

```text
WHATSAPP_REQUIRE_MENTION=true
```

The canonical YAML equivalent is:

```yaml
platforms:
  whatsapp:
    require_mention: true
```

`display.platforms.whatsapp.require_mention` is not an adapter routing setting.
Hotfix 0.2.2 accepts that Hermes 0.20 Dashboard placement only as a fallback;
new deployments should use the canonical key or environment variable.

When enabled, group messages are processed only if one of these is true:

- the message mentions the bot
- the message replies to the bot
- the message starts with `/`
- the message matches `WHATSAPP_MENTION_PATTERNS`

Set it to `false` to let all allowed group messages reach the agent.

## Rollback

Disable the plugin and restart the gateway:

```bash
hermes plugins disable whatsapp-bridge-policy-hotfix
hermes plugins disable qqbot-connect-hotfix
hermes plugins disable codex-app-server-phase-hotfix
hermes plugins disable message-snapshot-store
```

Disabling `codex-app-server-phase-hotfix` restores the affected upstream
behavior:
Codex Gateway approvals are no longer sent to QQ, permission requests fail
closed, Computer Use app elicitations are declined, duplicate-final protection
is removed, Codex media-only image turns may again be dropped, and Hermes
0.20.0's fixed 600-second Codex turn deadline is restored. Codex project/thread
mapping also stops, but `$HERMES_HOME/state/codex-session-projects.sqlite3`
and `$HERMES_HOME/codex-projects` are retained for re-enable or audit. Disabling
`qqbot-connect-hotfix` restores the shared-group approval rejection when group
sessions are not user-isolated and removes the expired-reply standalone retry.
Restart the gateway after rollback.

For script-level MCP changes, stop the `http-mcp` container or point the compose
service back to the upstream Hermes MCP command.

## Verification

Run plugin tests on the host:

```bash
python plugins/qqbot-connect-hotfix/test_hotfix.py
python plugins/qqbot-connect-hotfix/test_expired_reply.py
python plugins/qqbot-connect-hotfix/test_media_reply.py
python plugins/qqbot-connect-hotfix/test_group_roundtrip.py
python plugins/qqbot-connect-hotfix/test_final_delivery.py
python plugins/qqbot-connect-hotfix/test_streaming.py
python plugins/qqbot-connect-hotfix/test_steer.py
python plugins/codex-app-server-phase-hotfix/test_hotfix.py
python plugins/message-snapshot-store/test_store.py
python plugins/message-snapshot-store/test_capture.py
python plugins/message-snapshot-store/test_materialize.py
python plugins/message-snapshot-store/test_quoted_attachment.py
python plugins/message-snapshot-store/test_whatsapp_capture.py
python plugins/whatsapp-bridge-policy-hotfix/test_hotfix.py
```

Run inside a Hermes container when validating the real image:

```bash
docker exec <gateway-container> /opt/hermes/.venv/bin/python3 \
  /opt/data/plugins/whatsapp-bridge-policy-hotfix/test_hotfix.py
```

For HTTP MCP:

```bash
python mcp/http-gateway/test_hermes_mcp_http_auth.py
python mcp/http-gateway/test_hermes_mcp_qqbot_target_patch.py
```


### Remote CLI and restart compatibility (2026-09-28)

The updater invokes Python-form Hermes launchers with the installation's venv
interpreter. This fixes plain SSH sessions whose PATH lacks `hermes` and would
otherwise run the checkout launcher under macOS system Python 3.9, or try to
execute a Python file directly on Windows. Native executable/shell launchers
retain their normal command form.

A cached `last_tested_commit` no longer bypasses current CLI/version validation
or regressions when changes are pending. Dry-run completion explicitly reports
preflight success, not that an update was applied. Busy profiles still defer;
activity and running state are re-read after regressions, immediately before
backup and live mutation, so a task started during testing also defers the update.

For a running Gateway, the updater waits for its old PID to exit and its API
address to become bindable before restart. It requires a fresh live PID plus
reconnection of previously connected platforms (and enabled QQ), rather than a
literal `Ready` log. QQ `Resumed` is valid; a stale PID or a lost API listener is
not. Rollback preserves the same original platform requirements.

The normal one-command entrypoints enable this behavior automatically after
publication to main; no scheduler or Hermes core changes are required. Verify
with `--dry-run` / `-DryRun`, then an idle `--apply` run, profile state/PID,
platform health and before/after business configuration hashes. Tests are in
`ops/test_hermes_dispatch_update.py`, including actual Python-launcher execution,
stale-cache rejection, resumed connectivity, port rebinding and rollback.
Rollback uses the profile's recorded `update-backups` snapshot and its original
Gateway service. Restore the previous updater script/ref separately if needed.

A venv whose uv trampoline points to a deleted base interpreter must be repaired
before this Python updater can start. On Windows, install a user-owned Python
of the same minor version, back up venv startup files, then use uv's native
`venv --allow-existing --python <base> <venv>` mode and verify dependency hashes
and `hermes --version`. This preserves packages; do not use `--clear` or change
execution policy to mask a missing interpreter. See the official
[uv Python guide](https://docs.astral.sh/uv/guides/install-python/).


The macOS/Linux all-profile entrypoint also runs the existing QQ cron launcher
installer before profile changes. Some generated `hermes` shell launchers clear
`PYTHONPATH`; without the conditional bootstrap, the newly installed QQ binding
plugin cannot cover CLI-created jobs. The installer is idempotent, validates
ownership, skips unsupported/non-clearing/symlink launchers, and never edits
Hermes core. A setup failure stops before profile mutation. Dry-run leaves the
launcher unchanged. Its separate original-file backup is printed under the
native Hermes root's `plugin-backups/qq-cron-launcher-*`; restore that file when
rolling back this host prerequisite, in addition to any profile snapshots.
Windows uses its native launcher and the QQ plugin's venv-overlay integration.

## WeKnora-first QQ routing

`plugins/weknora-qq-routing` provides independent Hermes and native Codex
routing placements with one shared semantic policy. It checks the current
profile's enabled endpoint, matching credential/transport identity and actual
retrieval tool surface after QQ authorization. Ordinary/no-KB requests and
unavailable tools retain normal behavior. It does not alter delivery routing,
slash input, permissions, knowledge-base data or Hermes source.

The 2026-10-07 pilot ran 72 real-model cases: baseline positive retrieval 3/9;
Hermes, Codex and both 9/9 each, with no negative false positives or turn failures.
Observed stability tied. When explicitly enabled, default to Hermes to avoid
additional native-Hook/version/trust dependencies; Codex remains optional.
Native UserPromptSubmit produces developer context, whereas Hermes's Codex
compatibility adapter adds an ephemeral user-input context at turn/start.
No production profile was enabled by the experimental runner.

See the plugin README and [sanitized acceptance record](validation/weknora-qq-2026-10-13.md) for
source contracts, same-profile/Prompt/PID guards, native scoped Hook trust,
experiment limitations and rollback. Never equate tool listing with successful
inference/retrieval: initial never-policy trials were blocked by MCP review and
excluded; final on-request trials preserved review and only exposed read tools.


### WeKnora and finance approval rollout (2026-10-13)

The release manifest includes `weknora-qq-routing` as optional. The updater
installs or updates it only when the profile explicitly lists it in
`plugins.enabled` and does not list it in `plugins.disabled`. Missing or invalid
enable lists are not opt-in. Unselected optional plugins are excluded from file
replacement and hash verification; an existing disabled copy stays untouched.
The default installer omits it. To opt in deliberately, install this named
plugin and enable it after checking the profile's own authorized MCP declaration.
This avoids Hermes' legacy migration that grandfathers already-installed user
plugins when `plugins.enabled` is absent. A Codex-only connector declaration is
not sufficient for the profile guard.
Select mode `hermes` through
`plugins.entries.weknora-qq-routing.settings.mode`; keep existing credentials
as environment references, never copy another profile's connector.

The shared Codex hotfix is 1.8.10: it includes the finance 1.8.9 MCP consent and
effective reviewer synchronization, plus the current main-branch long-turn
implementation. Do not replace finance's plugin with an older release. QQ 1.8.41
keeps Working-only stream boundaries, accepted-task receipts, and commentary/
attachment deduplication. These are separate plugins and do not change model
effort, approval policy, tool permissions or knowledge-base data.

Run the manifest regressions before an idle update, then verify Gateway/QQ
connectivity, actual read-only MCP execution and visible QQ final delivery.
Current-turn scope evidence can help a reviewer assess authorization; it is not
a permission grant or a guarantee of approval. Keep failures distinct from
no-results and preserve reviewer denials. Roll back each changed plugin using
the updater's exact profile backup, retain the recorded launchd domain, and
restart only that profile. See the validation record for the observed scope and
remaining rollout limits.

### Update review guards

The updater rechecks managed plugin hashes and optional-plugin selection after
regressions, before backup, stop or replacement. If an operator changed a plugin
or its selection during tests, the run reports `deferred-local-state-change` and
leaves services/plugins unchanged. The next run reevaluates current versions;
this is not permanent protection for unversioned private edits. Locally newer
plugin versions remain preserved by the existing version guard.

The finance 1.8.9 runtime code and dedicated approval regressions are byte-identical
to those included in the 1.8.10 shared release. Version metadata and documentation
differ; the generic deadline regression now uses a deterministic clock instead
of a 30 ms sleep, without changing production timeout logic. That version upgrade can replace the plugin directory
while retaining its MCP consent and reviewer behavior; it is not a downgrade to
the old shared 1.8.8 implementation. Both approval regressions remain mandatory.

If optional plugin files already exist but the profile has no valid enable list
and no explicit disable entry, the updater stops before calling Hermes CLI.
Choose the intended enabled/disabled state first; otherwise Hermes migration
could grandfather those pre-existing files. This check does not change that
profile's configuration or stop its Gateway.

### Authorized capability-gated WeKnora rollout

When an operator explicitly requests automatic enablement on accessible profiles,
use `ops/enable_weknora_routing.py --home PROFILE --apply` before the one-click
all-profile updater. This is a separate, deliberate opt-in step; scheduled and
ordinary updates keep their existing non-enabling behavior. See the plugin's
[probe, verification and rollback procedure](../plugins/weknora-qq-routing/README.md#capability-gated-fleet-opt-in).
Run it as the profile owner. Record skipped/offline/deferred profiles separately,
and preserve any pre-existing disabled selection. The bundled phase hotfix 1.8.10
includes Finance's approved 1.8.9 runtime/approval tests; compare its approval
bridge with the live profile before replacement. A newer local repair still wins
under the updater's version guard.

## A2A conversation limit for the cross-host test profiles

The two cross-host test profiles may set `A2A_MAX_PINGPONG_TURNS=20` in their
existing profile `.env` files. Hermes 0.21.0 defaults to five inbound calls per
`context_id` and clamps this setting to 1–20. Raising it allows longer
collaboration while retaining the built-in anti-loop guard; this is configuration
of an upstream feature and requires no source-code workaround.

Back up `.env`, change only this variable, and restart each idle Gateway using its
existing service/task mechanism. Preserve other environment values, credentials,
Hermes YAML, Codex configuration and session mappings. Verify both actual MCP
inventories and sequential calls in the same context beyond turn five, including
correct retained context and successful sixth/seventh/eighth replies. Check a
recipient-visible QQ round trip separately when accepting the channel path.
Runtime failures still consume turns; an idle context's counter expires after one
hour. A different context needs an explicit background handoff.

Rollback by restoring this variable's previous value (or removing only the added
line if it was absent), then restart the idle Gateway. Do not overwrite unrelated
configuration with an older whole-file backup. Returning to the default brings
back the five-call limit; it does not delete conversation history. This setting
is local to the receiving profile and must be applied to both directions.

Validation record: [cross-host limit 20, nine rounds](validation/a2a-limit20-2026-10-15.md).

### Updating existing A2A and SOUL deployments

The release manifest also carries `a2a-https-hotfix`, `a2a-peer-directory` and
`codex-soul-bridge` as optional plugins. The one-click updater refreshes them only
where the profile already explicitly enables them; it does not add their enable
entries. This closes the deployment gap where a successful update of the base
plugins left an opted-in A2A transport or identity bridge on its old version.

For a profile that opts into `a2a-https-hotfix`, an existing regular
`scripts/hermes_tools_a2a.py` is updated from the release's MCP wrapper in the same
idle Gateway transaction. Missing wrappers are not created, arbitrary MCP command
paths are not changed, and shared/symlinked paths are rejected. Configure any
nonstandard wrapper location separately according to its owner. The wrapper is
backed up with the plugin/config snapshot, hash-checked, and restored on a failed
update. An edit during regression checks defers installation; a newer local A2A
plugin also preserves its associated wrapper. Credentials, SOUL contents and
Codex MCP configuration are outside this update operation.

The A2A MCP wrapper no longer requires an explicit `HERMES_HOME` to export the
five tools. An ordinary Codex session uses the default Hermes home's A2A
configuration; Gateway sessions retain their supplied profile home. Back up and
replace the existing wrapper, then reload MCP or start a fresh Codex runtime.
Verify the tool inventory through that Codex connection and read `a2a_list`;
standalone MCP discovery does not prove an existing session refreshed. Restore
the wrapper backup and reload MCP to recover the former profile-only export
behavior. This changes neither SOUL injection nor automatic collaboration rules.

Run the standard all-profile script, then inspect its applied commit, installed
plugin hashes and `a2a_wrapper_changed` result. Confirm existing enable/disable
lists and Finance's approval bridge are preserved. Test a real authenticated A2A
call and original QQ reply separately from the updater's process/connectivity
checks. Roll back using the reported backup, including its wrapper file, and
restart the affected idle Gateway through the guarded launcher. Keep HTTPS and
existing authentication requirements enabled.
