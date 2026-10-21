# Architecture

## Principle

`hermes-dispatch` treats the Hermes container image as immutable. Compatibility
code is mounted through `/opt/data` and activated by config or plugin discovery.
This keeps fixes durable across container recreation and makes every workaround
removable when upstream behavior changes.

## QQ Bot Area

`plugins/qqbot-connect-hotfix` is a user plugin that patches the built-in QQ Bot
adapter at runtime. It covers local integration gaps observed in group and DM
delivery:

- connect signature compatibility
- channel directory lookup for chat type routing
- emoji-only group mentions
- group context buffering and deterministic compaction
- plain-text retry for markdown/body compatibility
- one standalone text/keyboard retry when QQ explicitly expires a reply anchor
- media send retry without incompatible captions
- requester-bound approval buttons for shared group sessions

The plugin should be enabled only for QQ Bot deployments that need those
workarounds.

## Codex App-Server Area

`plugins/codex-app-server-phase-hotfix` is the mounted compatibility boundary
between Hermes' Codex runtime and Gateway adapters. It has seven independent,
removable patches:

- phase-aware interim/final message routing
- `imageGeneration` projection into normal gateway media delivery
- blocking approval routing through Hermes' existing per-session queue
- Computer Use application authorization through MCP elicitation
- configurable Codex app-server wall deadlines with terminal-event validation
- durable Hermes channel-project and session-thread mapping
- optional Codex Desktop sidebar registration through `codex app <path>`

Session identity is deliberately split into two levels:

```text
stable channel session_key -> stable Codex project
  durable Hermes session_id -> named Codex thread
```

QQ `/new` and `/reset` rotate `session_id` while preserving `session_key`.
Consequently they create a new thread under the existing project. Rebuilding an
Agent while both identifiers are unchanged resumes the stored thread. The
stable `session_key` names the logical default project; session ids name its
threads. SQLite under `$HERMES_HOME/state` is the authoritative mapping. On
macOS/Linux the ordinary colon-delimited key is also the directory basename.
Windows replaces forbidden ASCII colons with full-width colons only in the
physical basename. The manifest retains the exact key; raw transcripts and
credentials are not written into that project.

An app-server `thread/start(cwd=...)` does not register that cwd in Codex
Desktop's local-project sidebar. When explicitly enabled, the plugin invokes
the Codex CLI's cross-platform `codex app <path>` entrypoint in a detached
worker and persists success. The Agent turn never waits for Desktop launch. It
never edits Codex Desktop's private global-state file. Headless and container
deployments leave this disabled and perform host-side registration under the
matching desktop user when needed. Linux and Windows call the CLI directly;
macOS detached Gateways use `launchctl asuser` so the launch reaches the
logged-in Aqua desktop session.

On plugin load, the current entries in Hermes' authoritative
`sessions/sessions.json` are idempotently backfilled into missing project
mappings/scaffolds. Backfill does not guess which historical cwd-less Codex
thread belonged to a route. The next real turn creates its named thread in the
new project, avoiding empty-thread creation and startup races.

An authorized explicit bind changes only the project cwd associated with one
channel `session_key`. The current thread id is carried forward and resumed
with the new cwd on the next turn; the wrapper closes only that Agent's
`CodexAppServerSession`. Other channel sessions retain their clients, mappings
and active turns.

Long-turn state is call-local and session-local. Each cached Hermes agent owns
one `CodexAppServerSession`, one Codex thread and one event callback. The timeout
wrapper adds no global current-session/current-result variable; `turn/completed`
from another thread cannot satisfy the active turn because upstream notification
filtering checks both thread and turn identifiers. The same chat remains
serialized by Hermes' turn lease, while different chats can run independently.

The approval bridge does not implement a second authorization database. During
an active Gateway turn it resolves the current session key, reuses the notifier
already registered by `gateway/run.py`, and waits on `tools.approval` exactly as
Hermes terminal tools do. QQ remains only a presentation and interaction
adapter; clicking its approval buttons resolves the same Gateway queue.

Codex owns permission scope. The bridge records the request's exact UI choices
on the existing in-memory Gateway queue. QQ renders one-shot and session choices
separately, and renders a permanent command choice only when Codex supplies an
exec-policy or network-policy amendment. Selecting it returns that exact
amendment to app-server. Standalone permission requests remain limited to
`turn` or `session`; deny/timeout returns an empty subset. The bridge does not
invent rules or write `config.toml`.

Computer Use remains the policy owner. The bridge recognizes only the bundled
`computer-use` connector, forwards its app authorization elicitation, and sends
the accepted persistence scope back to Codex. Hard runtime safety denials are
not converted into prompts or bypassed.

For QQ shared groups, the adapter cannot infer an approver from the shared
session key. `qqbot-connect-hotfix` therefore issues a one-time opaque button
token bound to the current turn's `HERMES_SESSION_USER_ID`. The real session key
and requester identity remain in memory; only the matching operator in the
matching group can consume the token. This is an interaction-routing record,
not a durable permission database. Typed `/approve` and `/deny` commands consult
the same requester record. The dynamic approval keyboard can resolve the opaque
token back to the real pending queue entry, so shared-group prompts retain the
same complete choice set as direct chats without exposing the session key.

## WhatsApp Area

`plugins/whatsapp-bridge-policy-hotfix` separates three concerns that are often
mixed in the upstream bridge path:

- private chat authorization through `WHATSAPP_ALLOWED_USERS`
- group chat openness through `WHATSAPP_GROUP_POLICY`
- group response behavior through `WHATSAPP_REQUIRE_MENTION`

The target model is:

```text
WHATSAPP_DM_POLICY=allowlist
WHATSAPP_ALLOWED_USERS=<private-dm-users>
WHATSAPP_GROUP_POLICY=open
WHATSAPP_ALLOW_ALL_USERS=true
WHATSAPP_REQUIRE_MENTION=true
```

`WHATSAPP_ALLOW_ALL_USERS=true` is only the required explicit opt-in for open
group policy. With `WHATSAPP_DM_POLICY=allowlist`, private chats still require
`WHATSAPP_ALLOWED_USERS`.

`message-snapshot-store` hooks the Python side of the Baileys bridge before the
mention-response decision. Passive group events are persisted but cannot wake
the agent. Because the bridge has already used Baileys `downloadMediaMessage()`
to decrypt media into local cache paths, WhatsApp attachments are streamed into
the content-addressed archive immediately; QQ can continue to use signed-link
metadata until explicit restore.

## MCP Area

`plugins/a2a-https-hotfix` replaces the disabled bundled A2A platform registration
with the same upstream protocol over native TLS. It rejects HTTP, redirects,
unverified certificates, and Agent Card origin changes. There is no plaintext
proxy hop. Certificate material and per-peer tokens stay in the owning profile's
runtime data. See its [deployment and rollback guide](../plugins/a2a-https-hotfix/README.md).

`mcp/hermes-tools-a2a` is an optional stdio startup wrapper for the Codex runtime's
`hermes-tools` callback. Hermes 0.21.0 ships A2A handlers but omits them from this
callback's fixed exports. The wrapper adds only the five upstream A2A tools,
retains their profile-config availability gates, and supplements raw A2A schemas
when tool-search compaction hides them. It reuses the upstream builder and
dispatch path; authentication and conversation storage remain upstream-owned.
Profile-bound Codex sessions must forward the calling profile's absolute
`HERMES_HOME` through the MCP entry's `env_vars`; relative homes are rejected.
Ordinary Codex sessions with an absent or empty `HERMES_HOME` use Hermes' native
platform default home, exposing A2A when that home's upstream availability gate
passes. Peers, authentication, and conversation storage all use the selected
home. This entrypoint neither starts inbound A2A nor
changes Gateway routes. See its [enable, verification, and rollback guide](../mcp/hermes-tools-a2a/README.md).

`mcp/http-gateway` exposes Hermes MCP over streamable HTTP with bearer-token
authentication. It is intended for local network reverse proxying through Caddy
or Cloudflare Tunnel while keeping authentication inside the Python wrapper, so
loopback access and proxied access share the same authorization path.

The optional QQBot target patch can be enabled for deployments where MCP
outbound dispatch needs QQBot target normalization.

## Deployment Area

`deploy/mwe-support-dev` is an example deployment shape:

- Hermes gateway container
- HTTP MCP wrapper container
- Caddy reverse proxy
- optional Cloudflare Tunnel

Copy it and provide a local `.env` derived from `.env.example`. Do not commit
filled runtime env files.
