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
