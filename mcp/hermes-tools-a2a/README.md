# A2A tools for the Codex runtime

This opt-in stdio entrypoint extends Hermes' existing `hermes-tools` MCP with
`a2a_discover`, `a2a_list`, `a2a_call`, `a2a_history`, and `a2a_orchestrate`.
It preserves the other tools and the server name, and uses the upstream A2A
schemas and handlers. It does not start an A2A listener or configure peers.
Ordinary Codex CLI/App sessions can discover these tools without binding a
Hermes profile. Tool exports no longer require an explicit `HERMES_HOME`.

The optional [peer directory plugin](../../plugins/a2a-peer-directory/README.md)
adds configured responsibilities to `a2a_list` through the same native registry.
Peer aliases and capability tags remain profile-local configuration; changing an
alias does not rename the internal Hermes profile or its authenticated identity.

## Why the compatibility layer exists

Hermes 0.21.0 (`29112bef09`) includes the A2A platform/client, but
`agent/transports/hermes_tools_mcp_server.py` has a fixed `EXPOSED_TOOLS` list
without A2A. Enabling A2A in Hermes alone does not expose it to a Codex
app-server turn. Hermes' tool-search compaction can also omit the individual
schemas from the default tool catalog.

The wrapper temporarily supplements that catalog with the **raw A2A toolset
only**, adds the five names to the exports, and calls the original server
builder. It restores the patched globals after construction, including on
failure. Existing upstream definitions take precedence, so future native A2A
exports are not duplicated. Registration, argument validation, dispatch,
authentication, conversation persistence, and errors remain upstream-owned.
No upstream source file is edited and no new dependency is needed.

The upstream availability check still applies: peers under `a2a_agents`, an
enabled A2A platform, or `A2A_PORT` opt the profile in. An unconfigured profile
gets no A2A tools. The MCP transport does not apply Hermes' per-platform
`platform_toolsets` list; use the owning Codex config's `enabled_tools` /
`disabled_tools` if a narrower MCP surface is required.

References:

- [Hermes A2A](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/a2a)
- [Hermes Codex runtime](https://hermes-agent.nousresearch.com/docs/user-guide/features/codex-app-server-runtime)
- [Codex stdio MCP environment forwarding](https://learn.chatgpt.com/docs/extend/mcp#stdio-servers)
- [Upstream MCP builder at the verified version](https://github.com/NousResearch/hermes-agent/blob/29112bef09/agent/transports/hermes_tools_mcp_server.py)

## Enable

Use the Python environment that runs Hermes, with its source importable and
its existing MCP SDK installed. The verified combination is Hermes 0.21.0,
Python 3.11.15, MCP SDK 2.0.0, and Codex CLI 0.159.2. This wrapper uses internal
Hermes builder APIs; rerun the checks below after upgrading Hermes.

1. As the profile's OS owner, copy `hermes_tools_a2a.py` into a persistent
   scripts directory, for example `$HERMES_HOME/scripts/hermes_tools_a2a.py`
   on the host or `/opt/data/scripts/hermes_tools_a2a.py` inside a container.
   Back up an existing copy before replacing it. Do not write files into the
   container's Hermes source tree or create root-owned files in mounted data.
   The plugin installer and fleet updater do not install this optional entrypoint.
2. Back up the owning `CODEX_HOME/config.toml`. Edit the **existing**
   `[mcp_servers.hermes-tools]` entry: keep its interpreter and other settings,
   replace its `args`, and add `HERMES_HOME` to `env_vars` without removing any
   existing forwarded names. Do not add a second server with duplicate tools.

Example (replace both absolute paths with the target runtime's paths):

```toml
[mcp_servers.hermes-tools]
command = "/absolute/hermes/venv/bin/python"
args = ["/absolute/persistent/scripts/hermes_tools_a2a.py"]
env_vars = ["HERMES_HOME"]
tool_timeout_sec = 300

[mcp_servers.hermes-tools.env]
HERMES_QUIET = "1"
HERMES_REDACT_SECRETS = "true"
```

`HERMES_HOME` is optional. When absent or empty, Hermes uses its platform-native
default home (`~/.hermes` on macOS/Linux; `%LOCALAPPDATA%/hermes` on Windows,
falling back to the user's `AppData/Local/hermes`). Its configured peers,
authentication and A2A conversation storage are therefore available to an
ordinary Codex session using this MCP entrypoint. No SOUL identity or proactive
collaboration policy is injected by this change.

When supplied, `HERMES_HOME` must be absolute; a relative home is rejected with
code 2. Codex does not forward arbitrary parent environment variables to stdio
MCP processes, so keep `env_vars` for Gateway-launched profiles. This selects
their existing configuration, not whether the five tools may be exported.
Do not pin a default profile's home in shared MCP config: a multi-profile
Gateway must retain its own home when launching each Codex runtime. The wrapper
does not merge peers from different profiles or bypass upstream A2A availability
checks. A default home with no A2A configuration still has no A2A tools.

Keep unrelated credentials, other MCP entries, sandbox/approval settings, and
per-profile `CODEX_HOME` choices unchanged. If a runtime cannot import Hermes
from the copied script's location, add its actual source directory as
`PYTHONPATH` in this MCP entry's `env`; use the interpreter from that install.

3. Install the [HTTPS-only A2A transport](../../plugins/a2a-https-hotfix/README.md)
   on the calling and receiving profiles, then configure peers in the caller's
   runtime-only `config.yaml`. An example without credentials is:

```yaml
a2a_agents:
  researcher:
    url: https://node.example.ts.net:9901/researcher
    timeout: 120
    capabilities: [research]
```

For authenticated peers, configure upstream's `auth` block locally; never
commit real tokens. The target profile separately needs its A2A listener,
distinct port, and authentication/trust settings. Consult the upstream A2A
guide for those settings. The MCP timeout must exceed the peer call timeout
plus Agent Card lookup time; the example allows 300 seconds.

4. Reload the MCP configuration / start a fresh Codex runtime when the owning
   session is idle. If enabling inbound A2A requires a Gateway restart, drain
   active work first. The wrapper does not restart anything itself.

## Verify

Self-contained startup patch regressions:

```bash
python mcp/hermes-tools-a2a/test_hermes_tools_a2a.py
```

Real MCP subprocess checks, using the installed Hermes environment:

```bash
HERMES_SOURCE_DIR=/absolute/hermes/source \
  /absolute/hermes/venv/bin/python mcp/hermes-tools-a2a/test_integration.py -v
```

To also exercise a real Codex app-server's MCP discovery and tool-call RPC:

```bash
CODEX_BINARY=/absolute/path/to/codex \
HERMES_SOURCE_DIR=/absolute/hermes/source \
  /absolute/hermes/venv/bin/python mcp/hermes-tools-a2a/test_integration.py -v
```

The integration checks use temporary Hermes/Codex homes and a local HTTPS test
peer. They check unchanged baseline tools, upstream A2A opt-in gating, required arguments,
all five handlers, exact message/context forwarding, bearer-auth refusal, and
separate peer lists/conversation files for two profiles using the same context
ID. With no `HERMES_HOME`, they verify default-home peers and conversation
storage even when the working directory belongs to another profile. The
Windows isolation regression repeats that roundtrip with a disposable parent
`LOCALAPPDATA` and asserts it remains untouched. The test launchers explicitly
set `HOME`, `USERPROFILE`, and `LOCALAPPDATA` to fixture paths: the MCP SDK merges
inherited Windows environment variables, and Hermes prefers `LOCALAPPDATA` over
`USERPROFILE` for its default home. Run this suite on native Windows as well as
POSIX to exercise that resolution; this is test-only isolation and requires no
deployment or runtime configuration change. The optional
Codex checks cover both explicit and absent `HERMES_HOME`, using
`mcpServerStatus/list` and `mcpServer/tool/call` in ephemeral threads; they perform
no model turn and need no account credentials. Without `CODEX_BINARY`, those
checks are skipped. On Windows, cleanup terminates the test app-server's process
tree so its Git children cannot keep temporary-home files locked.

After a real deployment, verify separately:

- The affected Codex runtime lists all five A2A tools under `hermes-tools`.
- `a2a_list` shows the selected home's configured peers, including the default
  home when no profile was supplied.
- A real A -> B -> A request returns the correct target's answer to the
  originating QQ/WhatsApp conversation. Confirm the profile, A2A context, and
  original-channel identity. A local test peer is **not** recipient acceptance.

An A2A conversation does not automatically share a profile's QQ/WhatsApp chat
history. Send the needed task context explicitly. The bridge also preserves
upstream's direct-URL calls (HTTPS only with the transport plugin): a peer list
is a directory, not an outbound network allowlist. Apply upstream/infrastructure
access controls if required.

A fresh MCP subprocess check does not establish what an existing Codex session
has loaded. Query `mcpServerStatus/list` through that runtime's existing client
connection and associate the returned tools with its thread. After replacing
the wrapper, reload MCP or start a fresh runtime; existing MCP children retain
their previously loaded code and tool catalog.

## 无需绑定 profile 的工具导出（2026-10-21）

旧包装脚本在缺少 `HERMES_HOME` 时主动隐藏 A2A，导致普通 Codex 无法发现这五个
工具。本次移除这一额外门槛，仍通过同一个 `hermes-tools` 服务补齐上游缺少的
A2A 导出名单与原生参数定义。未指定目录时使用 Hermes 平台默认目录；已指定
profile 的 Gateway 继续使用原来的 peer、凭证和会话存储，不合并各部门目录。

启用：备份并替换既有包装脚本，保留现有 MCP 配置，重新加载 MCP 或启动新的
Codex 运行时。普通 Codex 无需设置 `HERMES_HOME`，但默认 Hermes 目录仍须满足
上游 A2A 可用条件。验证必须包括实际 Codex 连接上的五项工具清单和 `a2a_list`
读取结果；另起 MCP 的 `tools/list` 不能证明旧会话已经更新。此改动不注入 SOUL，
也不启用自动协作策略。回滚时恢复备份脚本并重新加载 MCP，即恢复原来的
profile 门槛；无需修改账号、模型、peer 配置或删除对话历史。

### Local verification, 2026-10-21

The seven startup unit tests, eight MCP/TLS integration tests (including both
Codex app-server cases), and seven CLI/App SOUL-isolation wire tests passed.
The new no-home regression failed against the old gate before the change.

PR #25 review follow-up: the parent-`LOCALAPPDATA` regression failed on native
Windows before the fixture fix, both losing A2A tools and writing beneath the
disposable parent directory. After the fix, all seven startup and nine MCP/TLS
tests passed on macOS and native Windows, including both Codex app-server cases;
the seven CLI/App SOUL-isolation wire tests also passed on macOS. The Windows
parent directory stayed untouched, and test app-server process-tree cleanup
prevented Git children from blocking temporary-directory removal. These checks
used local fixtures; they do not establish Windows GUI refresh or production
profile/QQ delivery acceptance.

After backing up and replacing the three installed wrapper copies, four fresh
ephemeral Codex runtimes used their existing `hermes-tools` configuration:

| Runtime | Explicit `HERMES_HOME` | A2A tools in that runtime | `a2a_list` |
| --- | --- | --- | --- |
| Ordinary CLI 0.159.2 | absent | 5/5 | default-home peers |
| Ordinary App backend 0.163.0-alpha.10.2 | absent | 5/5 | default-home peers |
| Product CLI 0.159.2 | product home | 5/5 | product peers |
| Procurement CLI 0.159.2 | procurement home | 5/5 | procurement peers |

Each inventory came from `mcpServerStatus/list` on the same Codex connection and
thread that executed `mcpServer/tool/call` for `a2a_list`. The twelve total
Hermes tools comprised the seven baseline tools and five A2A tools. Unrelated
MCP servers and Codex plugins were disabled only in these child-process arguments.
These four checks sent no online model turn or real peer message; task dispatch
was exercised against the local TLS fixture in integration tests. The subsequent
current-chat verification below covers one real procurement request.

The installed wrappers were the only production files changed. Gateway PIDs,
configurations, credentials and SOUL contents were unchanged. These results
establish availability in fresh runtimes, not an automatic refresh of existing
Codex App chats or Gateway-owned Codex sessions. Windows and GUI refresh were
not tested.

### Current-chat live verification, 2026-10-21

After the tool surface was refreshed, the existing Codex chat's own callable
tool inventory contained all five `mcp__hermes_tools__a2a_*` tools. The assistant
then used those tools directly, without a shell-launched MCP or a separate
Codex test runtime:

| Check | Observed result |
| --- | --- |
| Current-chat tool availability | `a2a_discover`, `a2a_list`, `a2a_call`, `a2a_history`, `a2a_orchestrate` all present |
| Direct `a2a_list` | Successful; configured peers were `product` and `procurement` |
| Direct `a2a_call` to configured `procurement` | One request, no retry; `completed`, `isError=false` |
| Returned content | Exactly the one-line verification response below |

```text
CURRENT-SESSION-A2A-20261021-7D4C-OK
```

The user explicitly authorized this request. It asked the peer to return only
the marker, without tool use, further delegation, or QQ/WhatsApp delivery. No
429 was returned by this call. This establishes tool discovery and a successful
outbound request from the actual chat; it does not establish sustained capacity,
concurrent-call reliability, or that every existing Gateway-owned Codex session
has refreshed. Only `a2a_list` and `a2a_call` were invoked in this live check;
the other three were observed in the callable inventory and covered by the
local integration tests. The refresh action itself was not a GUI acceptance test.
Raw peer URLs, authentication material and private conversation identifiers are
omitted from this public record.

当前聊天已实际看到五个工具，并直接调用 `a2a_list` 和一次采购 `a2a_call`，没有
通过另起 MCP 进程代替验证。采购完整回复如上，状态为 `completed`、
`isError=false`，本次未返回 429。这里只证明当前聊天的一次真实通信成功，
不代表所有旧 Gateway 会话都已刷新，也不代表持续或并发限流问题已解决。

## Roll back

To restore only the previous explicit-profile export gate, restore the wrapper
backup from before this change and reload the affected MCP runtimes. Existing
credentials, peer configuration, and conversation history stay in place.

Restore the prior `hermes-tools` arguments (normally
`["-m", "agent.transports.hermes_tools_mcp_server"]`) and only the other entry
fields changed during this installation, preserving subsequent unrelated
config edits. Reload the MCP configuration / replace idle runtimes and verify
the previous tool inventory before removing the copied wrapper. Revert any
new inbound A2A or peer configuration separately, using the owning profile's
backup and an idle Gateway restart where necessary. Do not remove conversation
history, credentials, or other plugins.

Once an upstream MCP bridge exposes these five tools correctly, passes the
profile-isolation checks, and works through the actual Codex runtime, restore
the upstream entrypoint and remove this wrapper.

## Error results

The startup wrapper now translates native A2A tool error envelopes into MCP
`CallToolResult(isError=true)`, preserving their text and the SDK's existing
`{result: string}` output schema. This covers authentication/transport errors,
argument errors, RPC errors and failed/rejected/canceled tasks when used with
`a2a-https-hotfix` 1.0.1. Successful reply bodies are not scanned for error words.
The other Hermes tools retain their upstream behavior. Orchestration `all` is
an aggregate report; its per-peer errors must still be inspected.

The workaround is needed because upstream Hermes returns tool failures as normal
strings. Update the wrapper file and A2A plugin together, restart the idle
Gateway/MCP children, and run `test_integration.py`; it verifies real MCP wire
responses for both successful and failed TLS fixtures. Roll back both components
from the deployment backup and restart if needed. Neither enabling nor rolling
back this change requires changing tokens or global model defaults.

Hermes' registry also catches escaped handler exceptions and serializes them as
JSON objects with a top-level string `error`. The wrapper recognizes these native
envelopes as well as the text prefixes, retaining the original serialized text
in both `content` and `{result: string}`. For example, a rejected Agent Card RPC
origin must return `isError=true` instead of a successful tool result containing
an error message. This follows the [MCP tool-error contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#error-handling).
Successful peer replies containing quoted JSON error examples remain successful.

To apply just this correction, back up and replace the installed
`hermes_tools_a2a.py`, then replace the owning idle MCP runtime. No plugin or
configuration change is required. `test_integration.py` covers cross-origin and
HTTP-downgrade cards through a real local TLS peer and MCP wire, checks error
text/output-schema preservation, and checks the quoted-JSON success case.
Rollback restores the wrapper backup and reloads the idle MCP runtime; doing so
restores the old false-success status for these registry errors.

## First-turn availability after a Gateway restart

A successful standalone `tools/list` does not prove that the first Codex model
turn waited for the server. `a2a-https-hotfix` 1.0.2 uses Codex's native required
MCP setting for Hermes-created clients, without rewriting the shared config.
See the [startup fix and QQ acceptance](../../docs/validation/a2a-windows-first-turn-2026-10-15.md).

## One-click updates for existing installations

The all-profile updater now refreshes an already-installed
`$HERMES_HOME/scripts/hermes_tools_a2a.py` together with an explicitly enabled
`a2a-https-hotfix`. The file shares the plugin update's idle check, backup, hash
verification and rollback. It does not install a missing wrapper, rewrite MCP
configuration, or enable optional plugins. Other command paths remain operator
managed; a newer local A2A plugin preserves the wrapper too. See the
[operating procedure](../../docs/operations.md#updating-existing-a2a-and-soul-deployments).
