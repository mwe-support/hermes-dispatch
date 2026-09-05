# Codex App-Server Compatibility Hotfix

Persistent Hermes plugin for seven Hermes Codex app-server integration
gaps:

1. A completed Codex `final_answer` is also emitted through the gateway
   interim-message callback, which duplicates the final channel reply.
2. Codex's built-in image generator completes as an `imageGeneration` item
   containing base64 bytes. Hermes treats that item as an opaque, truncated
   assistant note. When the turn has no text final answer, the gateway returns
   before its normal media-result scan, so the generated image is never sent.
3. Codex app-server approval requests are wired only to the interactive CLI
   callback. Gateway sessions have no callback, so command/file requests fail
   closed without reaching QQ. Hermes also unconditionally declines
   `item/permissions/requestApproval` with the legacy `decision` response.
   Codex 0.144.6 expects a granted `permissions` subset and optional `scope`.
4. Bundled Computer Use asks for per-app authorization through
   `mcpServer/elicitation/request` with `connector_id=computer-use`. Hermes
   accepts only `hermes-tools` elicitations and silently declines every other
   server, so CLI-hosted Computer Use reports `was not approved` without
   presenting the Gateway user with an approval request.
5. Hermes 0.20.0 stops every Codex app-server turn at a fixed 600-second wall
   deadline. If commentary already exists, it is incorrectly accepted as the
   final answer while a foreground tool can remain alive.
6. Hermes stores the Codex thread id only on the process-local cached Agent.
   Rebuilding that Agent starts another thread, so one durable QQ private/group
   session is scattered across unrelated Codex projects and conversations.
7. Starting an app-server thread with a project `cwd` does not register that
   directory in Codex Desktop's sidebar project list.

The plugin forwards explicit `commentary`, suppresses explicit `final_answer`,
and defers unknown-phase messages until later turn activity proves they are
interim. The Codex session projector remains authoritative for final text.

For completed `imageGeneration` items, the plugin validates and atomically
writes the image to:

```text
$HERMES_HOME/cache/images/codex_<item-id>.<ext>
```

It then projects a standard `image_generate` tool result. If the Codex turn has
no final text, it returns `MEDIA:<path>` so the existing Hermes adapter sends
the file as a native image. Set `HERMES_CODEX_IMAGE_CACHE_DIR` only when the
gateway's allowed media roots include that directory. Encoded results above
80 MiB and unsupported file signatures are rejected.

## Gateway approval bridge

The plugin supplies a gateway-aware approval callback only when Hermes did not
already provide one. It looks up the notifier registered for the active Hermes
session, enters the same blocking approval queue used by Hermes terminal tools,
and lets the adapter render its normal approval UI. QQ therefore uses its
existing approval keyboard and `/approve` or `/deny` fallback rather than a
plugin-specific state store.

The bridge covers:

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`
- Computer Use `mcpServer/elicitation/request`

Version 1.4.0 preserves the decisions advertised by each Codex request instead
of reducing command and file approvals to allow/deny. Command requests expose
one-shot and session approval, plus a persistent choice only when Codex supplies
an exec-policy or network-policy amendment. File changes expose one-shot and
session approval. A persistent command choice returns the exact amendment
proposed by Codex; the compatibility layer never invents a broader rule.

For permission requests, allowing once returns the requested, schema-filtered
permission subset with `scope: turn`; session approval returns `scope: session`.
The permissions protocol has no permanent scope, so it does not modify
`~/.codex/config.toml` or a Hermes allowlist. Deny, timeout, a missing Gateway
notifier, or an internal exception returns an empty permission profile. Codex
treats every omitted permission as denied.

For Computer Use, the bridge matches only the bundled connector identity,
renders the requested display name and bundle id through the same Gateway
queue, and returns the MCP elicitation `accept`/`decline` response. **始终允许**
adds `_meta.persist=always`, allowing Codex to persist its own Computer Use app
policy. Unknown or third-party MCP elicitations continue through Hermes'
upstream fail-closed handler. Apps marked `forbidden` by the Computer Use
runtime never emit this elicitation and remain blocked.

This behavior follows the current upstream [Codex app-server approval
contract](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#approvals).

## QQ file-delivery hook (1.8.4)

Codex can finish with local file links or output citations instead of the
`MEDIA:` contract consumed by Hermes' streamed attachment delivery. This
optional native `UserPromptSubmit` hook supplies that contract on every QQ
DM/group turn, including vague report, presentation, and reusable-code requests.
It does not rewrite the user's request, upload files, or choose an upload target.

The three layers are:

1. The Hermes runtime reads the Agent's trusted channel session key. It uses a
   context variable to give only that QQ turn's Codex subprocess a profile/home
   origin marker. Other channels, guild channels without native file upload,
   and direct CLI clients receive empty markers. No process-global environment
   is changed, and session-project mapping need not be enabled.
2. `scripts/install-codex-qq-hook.py` registers the native hook in one explicitly
   selected `CODEX_HOME/hooks.json`. The hook verifies both profile and Codex
   home before emitting its short developer contract. It does not infer QQ from
   the prompt or cwd. It writes only turn/session IDs and a contract hash to
   `<profile>/logs/qq-delivery-hook.jsonl`, never prompt text or QQ identifiers.
3. `qqbot-connect-hotfix` keeps the existing validated file-reference fallback
   and native uploader. The hook requests final `MEDIA:` markers; the sender
   owns actual delivery and reports a failed upload. Hook execution alone is
   not delivery success.

### Install for one profile (macOS/Linux)

Use the profile's actual, already authenticated Codex home; do not derive it
from a department name or copy another profile's configuration. Both paths are
explicit installer arguments so an inherited desktop `CODEX_HOME` cannot select
the wrong target. Run as the owner of the mounted/profile data.

```bash
QQ_HOOK_PROFILE="$HOME/.hermes/profiles/procurement"
QQ_HOOK_CODEX_HOME="$HOME/.codex-hermes/procurement"
QQ_HOOK_PYTHON="$HOME/.hermes/hermes-agent/venv/bin/python"
scripts/install-plugins.sh "$QQ_HOOK_PROFILE" codex-app-server-phase-hotfix qqbot-connect-hotfix
"$QQ_HOOK_PYTHON" scripts/install-codex-qq-hook.py \
  --hermes-home "$QQ_HOOK_PROFILE" --codex-home "$QQ_HOOK_CODEX_HOME"
CODEX_HOME="$QQ_HOOK_CODEX_HOME" codex
# In Codex: /hooks, review and trust the QQ file-delivery hook, then exit.
hermes -p procurement gateway restart
```

The hook runs before every model prompt in that Codex home, but emits context
only for the marked QQ process. This scope is persistent until removal. Codex
skips untrusted hooks: installing is not equivalent to activation. The installed
script digest is part of the hook definition, so updating it requires normal
native trust review again. Never add trust hashes or bypass trust in a deploy
script. See [Codex Hooks](https://learn.chatgpt.com/docs/hooks#userpromptsubmit).

The installer preserves unrelated hook definitions and does not touch
`config.toml`, credentials, or MCP configuration. An ownership record prevents
two profiles from claiming the same Codex home. Updates replace only our group
in place; removal may leave an empty group to preserve subsequent hooks' trust
indices. Symlinked shared hook/state files are rejected. Backups are stored
outside plugin discovery in `<profile>/plugin-backups/codex-qq-hook-*`.

### Verify and remove

Run `test_qq_delivery_hook.py`, `scripts/test_install_codex_qq_hook.py`, and
`qqbot-connect-hotfix/test_file_delivery.py` with the Hermes Python and an
isolated `HERMES_HOME`. The checks cover QQ/group origin, other channels and
same-cwd CLI exclusion, parallel contexts, two profile/home pairs, upgrade,
unrelated trust/config preservation, rollback, and native file upload failures.

For live acceptance, isolate automatic skill/memory learning and start fresh QQ
turns. Ask naturally for a report, presentation or script, without sending or
upload hints. Correlate native hook records with Codex turn IDs, inspect final
`MEDIA:` and the QQ file card, then download and compare bytes. Use hook-only,
bridge-only, combined and neither controls to separate their contributions.
Also revise an output in the same conversation and check one new attachment.

The 2026-09-05 procurement canary used Codex CLI 0.153.4 and
`gpt-5.6-sol` / high. Hook-only, bridge-only, combined and negative controls
behaved as expected; same-session revision, Python, four-slide PPTX, two-page
PDF and ordinary-chat checks completed. A same-home/same-cwd direct CLI
control emitted no QQ contract. A subsequent continuous-conversation check
covered six natural file requests in a private chat and six in a real group:
12/12 native hook executions, 12/12 final MEDIA responses and 16/16 downloaded
files matching source bytes. Group history was retained, so this extension is
live acceptance, not a clean-history causal comparison. Only procurement was
deployed. Learning settings were restored and QQ reconnected at 20:06:12 +08:00.
Private streaming text still sometimes exposes MEDIA fragments or literal
output citations; attachment delivery and text rendering are separate results.
See [the acceptance record](../../docs/ablation-2026-09-05.md#常驻-qq-hook-实现与私聊群聊验收2026-09-05-完成)
for conditions and limits; these small samples do not establish long-term rates.

Remove the hook before restoring an older Hermes plugin that lacks its script:

```bash
"$QQ_HOOK_PYTHON" scripts/install-codex-qq-hook.py \
  --hermes-home "$QQ_HOOK_PROFILE" --codex-home "$QQ_HOOK_CODEX_HOME" --remove
# Restore the required plugin backups with scripts/install-plugins.sh --restore.
hermes -p procurement gateway restart
```

Removal is surgical and preserves unrelated hooks added since installation.
Existing native trust records are left to Codex; without the hook definition
they execute nothing. Removing only this optional hook leaves normal QQ
file-reference delivery available. This feature does not change turn or
inactivity timeouts.

## Long-turn deadline

Version 1.5.0 makes the app-server turn deadline configurable. The default is
unlimited, equivalent to:

```dotenv
HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS=0
```

A positive value restores a finite wall deadline in seconds. When that finite
deadline is reached without `turn/completed`, the plugin interrupts and retires
the Codex subprocess instead of delivering the latest commentary as success.
This does not disable `/stop`, message interrupt/steer, subprocess-death checks,
or Hermes' post-tool quiet watchdog.

Hermes Gateway has a separate inactivity watchdog. For deliberately silent
foreground work lasting more than 30 minutes, set it above the longest valid
task, for example:

```bash
hermes config set agent.gateway_timeout 7200
```

Use a large finite inactivity timeout so genuinely wedged work is eventually
released. Restart draining is a separate setting; on multi-session gateways,
configure it explicitly instead of accepting Hermes 0.20.0's zero-second
default:

```bash
hermes config set agent.restart_drain_timeout 300
```

Each Hermes conversation owns a separate `CodexAppServerSession`, Codex thread,
callback wrapper, terminal flag, and deadline value. The plugin stores none of
that per-turn state globally, so concurrent private chats/groups cannot complete
or interrupt one another. A single fixed chat still serializes or interrupts
new input according to `display.busy_input_mode`.

## Persistent session projects and threads

Version 1.7.0 adds a durable mapping without changing Hermes' own session
database:

```text
Hermes channel session_key -> one stable Codex project
  Hermes session_id A -> Codex thread named A
  Hermes session_id B -> Codex thread named B
```

For QQ C2C, `session_key` contains the private `user_openid`; for a shared QQ
group it contains the `group_openid`. `/new` and `/reset` keep that routing key
but create a new Hermes `session_id`, so the plugin creates a new named Codex
thread in the same project. A Gateway restart, Agent-cache eviction or
app-server retirement with an unchanged `session_id` uses `thread/resume`
instead of `thread/start`.

Version 1.8.3 closes an idle Hermes-owned Codex app-server when Hermes
soft-evicts its `AIAgent` after an LRU/TTL event or a cross-process transcript
write such as `/reload-mcp`. Hermes 0.20.5 closes `_codex_session` during hard
Agent teardown but omits it from `release_clients()`, leaving the old process
as the Codex thread's active writer while the replacement Agent immediately
tries `thread/resume`. The plugin preserves any session with an active turn,
and synchronously retires only a known idle writer from this Gateway process
before resume. Writers owned by Codex Desktop or another process remain
untouched and still fail with Codex's normal single-writer protection.

The logical default project name is the exact Hermes `session_key`, created at:

```text
$HERMES_HOME/codex-projects/<session_key>/
```

For ordinary Hermes keys on macOS/Linux, the colon-delimited key is used
verbatim. Windows replaces `:` with the compatible full-width `：` in the
physical directory name because NTFS/Win32 forbids ASCII colons; SQLite and
`.hermes-dispatch.json` still retain the exact original key. A key containing
a path separator, control character or an overlong basename is rejected.
Version 1.7.0 atomically migrates 1.6.x first-session-id folders and all mapping
rows on the next access. `AGENTS.md` and `PROJECT_MEMORY.md` are created only
when absent. The former directs later threads to the latter and to Hermes'
indexed session/message search tools; raw chat history is not injected into
every turn. Mappings live in:

```text
$HERMES_HOME/state/codex-session-projects.sqlite3
```

The prompt-callable `codex_session_project` tool and deterministic
`/codex-project` command support `status`, `list_threads`, `bind`, and
`default`. Project changes are fail-closed unless the current platform user is
listed in `HERMES_CODEX_PROJECT_ADMIN_USERS`. Prefer operator-defined aliases:

Hermes 0.20.0 dispatches plugin slash commands before binding its normal Agent
session ContextVars. At that point its compatibility accessor can fall back to
process-global environment values left by the previous Agent, including the
session id from before `/new`. The plugin therefore treats the route captured
during the platform-neutral `pre_gateway_dispatch` hook as authoritative and
resolves it with Hermes' own session-key generator. This makes
`/codex-project` work consistently on QQ, WhatsApp and other Gateway channels,
including immediately after `/new`; it does not bypass channel authorization
or create a parallel channel identity scheme.

```dotenv
HERMES_CODEX_SESSION_PROJECTS_ENABLED=true
HERMES_CODEX_SESSION_PROJECTS_BACKFILL=true
HERMES_CODEX_APP_REGISTER_PROJECTS=true
HERMES_CODEX_PROJECT_ADMIN_USERS=<owner-platform-user-id>
HERMES_CODEX_PROJECT_ALIASES={"finance":"/absolute/path/to/finance"}
HERMES_CODEX_PROJECT_ALLOWED_ROOTS=/absolute/path/to/department-projects
```

`HERMES_CODEX_APP_REGISTER_PROJECTS=true` is optional and intended for a
logged-in desktop user with Codex CLI and Codex Desktop installed. Version
1.8.2 schedules the CLI-supported, cross-platform `codex app <project-path>`
entry point outside the Agent turn and stores successful registration in the
mapping database. Redirected child stdio prevents a launched Desktop process
from holding Gateway pipes open. Linux and Windows invoke the CLI directly;
on macOS the worker crosses from a detached Gateway into the logged-in user's
Aqua bootstrap with `launchctl asuser`. The plugin does not edit Codex
Desktop's private global-state file.
The command can launch or focus the app, so leave it disabled on headless
servers and inside containers. In a container deployment, run
`codex app <host-project-path>` as the matching host desktop user instead. If
Hermes uses a non-default CLI, set `HERMES_CODEX_APP_CLI` to its executable
path. Registration failure is logged, retried after a cooldown, and never
fails or delays the Agent turn.

`HERMES_CODEX_SESSION_PROJECTS_BACKFILL=true` is the default. At plugin load,
the compatibility layer reads Hermes' own `sessions/sessions.json` and creates
missing mappings/project scaffolds for channel routes that existed before the
plugin was installed. It does not guess ownership of old cwd-less Codex
threads. The first real message in each backfilled route creates its correctly
named thread lazily, preventing empty threads and startup/inbound races.

`HERMES_CODEX_PROJECT_ALLOWED_ROOTS` uses the host path separator for multiple
roots. Alias targets are implicitly allowed. An explicit bind carries the
current thread id and takes effect on the next Codex turn via
`thread/resume(cwd=...)`; other Hermes sessions and chats are untouched. With
`QQ_ALLOW_ALL_USERS=true`, never set the admin list to `*` unless every QQ user
is trusted to select project working directories.

Enable the toolset on channels that may manage project bindings:

```bash
hermes tools enable --platform qqbot codex_session_project
```

Install under the Hermes data directory and enable it:

```bash
scripts/install-plugins.sh "$HOME/.hermes"
hermes plugins enable codex-app-server-phase-hotfix
```

Restart the gateway after installation. Verify all fixes with:

```bash
python plugins/codex-app-server-phase-hotfix/test_hotfix.py
```

To roll back, disable the plugin and restart Hermes. Existing generated cache
files are not deleted automatically:

```bash
hermes plugins disable codex-app-server-phase-hotfix
hermes gateway restart
```

The phase portion behaviorally detects an equivalent upstream fix and becomes a
no-op. The approval portion independently skips its permission or Computer Use
handler after upstream behaviorally implements the corresponding response.
Remove the image portion
once upstream projects `imageGeneration` into a normal deliverable media result
and handles media-only turns before its empty response return. Remove the
approval bridge once Hermes passes Gateway approval callbacks into Codex, uses
the current permission response schema, and forwards Computer Use elicitations.
Remove the long-turn portion once upstream exposes a configurable/unlimited
turn deadline and never promotes an unterminated assistant message to final.
Remove the session-project portion once upstream durably maps Gateway
`session_key`/`session_id` to a project cwd and resumes Codex thread ids after
Agent reconstruction. Disabling the plugin does not delete its SQLite mapping,
default project directories or project memory files.
