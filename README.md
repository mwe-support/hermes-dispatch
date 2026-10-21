# hermes-dispatch

Persistent compatibility layers for Hermes official containers.

This repository is for behavior that the upstream `nousresearch/hermes-agent`
container cannot currently provide directly, or for integration bugs that need
a removable local workaround. The intended deployment model is to mount these
files into Hermes data directories and enable them through Hermes config, not to
edit container-internal source files.

## Layout

```text
plugins/
  a2a-https-hotfix/                 HTTPS-only native A2A transport
  a2a-peer-directory/              Optional peer responsibilities in a2a_list
  codex-soul-bridge/               Gateway-only SOUL injection and thread refresh
  codex-app-server-phase-hotfix/     Codex routing, media, approvals, and persistent session projects
  message-snapshot-store/            Persistent snapshots and hybrid retrieval
  qqbot-connect-hotfix/              QQ Bot adapter compatibility plugin
  whatsapp-bridge-policy-hotfix/     WhatsApp bridge/policy/admin plugin
  weknora-qq-routing/                Opt-in profile-scoped owned/shared KB routing
mcp/
  http-gateway/                      Streamable HTTP MCP wrapper and tests
  hermes-tools-a2a/                  Opt-in A2A tools for Codex app-server runtime
  servers/                           Codex/Hermes MCP client examples
deploy/
  mwe-support-dev/                   Gateway + HTTP MCP + Caddy example
docs/
  architecture.md                    Compatibility design notes
  development-log.md                 Dated compatibility change record
  macos-hermes-codex-deployment.md    Reusable multi-user macOS deployment
  operations.md                      Install, verify, rollback notes
```

## Current Scope

- QQ Bot: message send fallbacks, media send compatibility, group message
  routing/context buffering, emoji-only mention handling, channel directory
  routing, long-context compaction, and persistent snapshots of bot-visible
  text/media events with structured + BM25 hybrid retrieval. QQ defaults each
  group robot to mention-only; its group owner can enable **获取全部群消息** for
  `GROUP_MESSAGE_CREATE` capture. The snapshot hook runs before passive routing
  suppression and is independent of plugin load order.
  Shared-group approval buttons use short-lived requester-bound tokens, so the
  initiating member can approve without splitting the group conversation and
  other members cannot reuse the button.
- Codex app server: preserve commentary without duplicate final replies,
  convert completed `imageGeneration` results into native gateway media, and
  bridge execution, file-change, permission, and Computer Use application
  authorization requests into Hermes' existing Gateway approval queue. Stable
  channel projects can optionally be registered in Codex Desktop through the
  CLI-owned cross-platform `codex app <path>` entrypoint.
- WeKnora: opt-in QQ routing with current-turn owned/shared KB evidence,
  profile/endpoint isolation, and Hermes/Codex ablation modes. The updater ships
  it only for profiles that explicitly enabled it; the default installer omits
  it. See its README for the explicit installation path.
- WhatsApp: split private-chat allowlist from group openness, enforce mention
  response policy, capture passive Baileys events before that response gate,
  and persist text/media in the same structured + BM25 snapshot store as QQ.
- MCP: authenticated streamable HTTP MCP wrapper for Hermes, local reverse proxy
  examples, and an optional QQBot target compatibility patch for MCP outbound
  dispatch.

## One-command Update

On a host with Hermes, Git, and Python already installed, update the default
profile and all named profiles from the latest `main`:

macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/mwe-support/hermes-dispatch/main/scripts/update-hermes-dispatch-all.sh | bash
```

Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/mwe-support/hermes-dispatch/main/scripts/update-hermes-dispatch-all.ps1 | iex
```

The updater backs up changed files and may restart an idle Gateway. See
[`docs/operations.md`](docs/operations.md#automatic-updates-on-macos-and-windows)
for dry-run, verification, and rollback.

## Installation Shape

For A2A calls from the Codex app-server runtime, see the opt-in
[`hermes-tools` A2A entrypoint](mcp/hermes-tools-a2a/README.md). It reuses Hermes'
A2A client without changing the messaging HTTP bridge or installing a listener.
The entrypoint also exports available A2A tools to ordinary Codex sessions without
`HERMES_HOME`, using Hermes' default home; explicit profile routing is preserved.
For responsibility descriptions and business-facing peer aliases, see the optional
[A2A peer directory](plugins/a2a-peer-directory/README.md). It preserves internal
profile IDs and existing authenticated routes.
The [Codex SOUL bridge](plugins/codex-soul-bridge/README.md) forwards
profile identity only for Hermes Gateway threads; it is not enabled by default.
The [proactive A2A collaboration study](docs/research/a2a-proactive-collaboration-2026-10-20.md)
proposes when to request peer assistance and how to blind-test routing, failures,
and ordinary Codex isolation. It is a research proposal, not an enabled policy.
See the [A2A and identity release checks](docs/validation/a2a-release-2026-10-20.md)
for the included versions, rerun regressions, and deployment evidence boundaries.

Copy plugins into the target Hermes data directory:

```bash
scripts/install-plugins.sh "$HOME/.hermes"
```

An update automatically preserves each existing plugin below the profile-level
`plugin-backups` directory before replacement. Restore an exact copy with:

```bash
scripts/install-plugins.sh --restore \
  "$HOME/.hermes" <plugin> "$HOME/.hermes/plugin-backups/<backup-directory>"
```

Backups stay outside the recursive `plugins` discovery tree. See
[`docs/operations.md`](docs/operations.md) for verification and rollback.
The installer rejects symbolic-link backup roots/active plugin targets,
non-direct canonical targets, and dot path components before it changes active
data. For a multi-plugin invocation it preflights every requested active target
before it creates, backs up, clears, or copies any plugin. Backup-root rejection
on a fresh install also occurs before the active plugin directory is created.

Then enable the required plugins from inside the Hermes container or host
runtime:

```bash
hermes plugins enable qqbot-connect-hotfix
hermes plugins enable codex-app-server-phase-hotfix
hermes plugins enable message-snapshot-store
hermes plugins enable whatsapp-bridge-policy-hotfix
```

Restart the gateway after enabling or updating plugins.

For a complete, version-unpinned, multi-department Mac mini deployment procedure,
see [`docs/macos-hermes-codex-deployment.md`](docs/macos-hermes-codex-deployment.md).

## Security Boundary

This repository intentionally contains only source, templates, examples, and
tests. Runtime secrets belong in the target Hermes `.env` or a secret manager.
Do not commit:

- Cloudflare tunnel tokens
- dashboard/API passwords or bearer tokens
- QQ/WhatsApp production identifiers
- private phone-number allowlists
- local session data under `.hermes`

## Verification

The plugin tests are self-contained:

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
python mcp/http-gateway/test_hermes_mcp_http_auth.py
python mcp/http-gateway/test_hermes_mcp_qqbot_target_patch.py
scripts/test_install_plugins.sh
```

For live MCP validation, start the HTTP MCP wrapper and run:

```bash
MCP_URL=http://127.0.0.1:8765/mcp \
MCP_BEARER_TOKEN='<token>' \
python mcp/http-gateway/test_mcp_streamable.py
```
