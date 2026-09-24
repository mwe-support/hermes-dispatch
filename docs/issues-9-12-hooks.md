# Issues #9 and #12: host-side lifecycle and delivery hooks

## Why Hermes owns the final routing decision

The two issues affect host execution boundaries: plugin discovery while the
Agent class is incomplete (#9), and scheduler delivery after the agent returns
(#12). The fix uses existing Hermes plugin registration to wrap those runtime
entry points in memory. There is no core-source edit or second delivery worker.

| Layer | Role | Enforcement boundary |
| --- | --- | --- |
| Hermes runtime plugin | Defer lifecycle patch; lock recipient/type; verify receipts; return delivery errors | Executes in the sender, including no-agent jobs; does not rely on model compliance |
| Codex UserPromptSubmit | Remind the model to emit requested artifacts using `MEDIA:` | Adds developer context to Codex; cannot govern a later Hermes send or a job without Codex |

Codex command hooks can enforce behavior at their own events; this is not a
claim that all Codex hooks are advisory. The relevant limitation is event
ownership: [UserPromptSubmit](https://learn.chatgpt.com/docs/hooks#userpromptsubmit)
adds context/blocks the prompt, and Stop governs the end of a Codex turn. Neither
is the Hermes cron dispatch boundary. The existing Codex file-delivery hook is
retained for formatting, with no trust/config change.

## #9

The registration-time `from run_agent import AIAgent` is removed. The complete
class is patched at first Codex runtime entry before session allocation. Existing
idle cleanup and active-turn protection remain. The regression launches four
fresh subprocesses for cold import, background discovery, cron and MCP entry;
it checks complete plugin registration and actual lifecycle activation, not
only absence of an exception. Official v0.21.0 still requires the plugin fix.
The upstream replayed-tool-name fix in v0.21.0 remains a separate concern.

## #12

The clarified requirement is automatic binding to the QQ conversation that
creates the job, with another target allowed only by an explicit instruction
in that user's original message. Version 1.8.33 captures native inbound context,
atomically stores a typed target with the job, preserves it across updates and
restarts, and enforces it for text/media/no-agent delivery. Codex child tools
receive per-turn context; a process-local CLI bootstrap covers upstream cron
commands that skip plugin discovery. Unknown or ambiguous destinations and
missing origins fail closed, rather than selecting home.

See the [QQ plugin README](../plugins/qqbot-connect-hotfix/README.md#automatic-qq-cron-conversation-binding-1833)
for the explicit delivery-clause format, supported entry points, legacy job
audit, enablement, verification and rollback. The native Codex attachment hook
and Hermes core source remain unchanged.

The earlier 1.8.29 tests below passed only explicit-target/pin delivery. They do
not establish acceptance of automatic source binding. Version 1.8.33 requires
new real QQ-created jobs on both hosts before that broader claim can be made.

## Release integration

This branch starts at updater commit `eefed8a`; it retains the existing Windows
SQLite, hook installer and short-path compatibility changes. Versions are Codex
plugin 1.8.7 and QQ plugin 1.8.33, avoiding confusion with the independently
validated older hook branch. Both new regressions are in `ops/release.json`
(22 scripts total). No credentials, target IDs or business jobs are included.

Earlier local live QQ results belong to the earlier pinned-hook implementation.
They are useful prior evidence, not native Windows or production acceptance of
this combined branch. This change does not resolve Windows ACL issue #11 or
replace the separate issue #10 branch.

## Historical isolated verification of the 1.8.29 candidate

On official v0.21.0 / `v2026.8.31` (`29112bef099274229cadff79cdff7bf7b99c4b77`),
all 21 release-manifest scripts passed. The cold-start test first failed with
`cannot import name 'AIAgent' from partially initialized module 'run_agent'`;
after the fix all four startup modes passed with lifecycle activation checked.
The routing test first observed `/v2/users/home-user/messages`, and the new
unpinned explicit-group case first observed `/v2/users/target-chat/messages`.
Both are now constrained to the selected type/ID or return a durable error.

The final group/c2c/dm matrix, including real no-agent script execution, also
passed on a clean snapshot of core `fcbd1076a93841fa88855acce810e342a5b78101`.
The plugin install/backup/restore checks and `git diff --check` passed. These
are isolated host tests with QQ HTTP replaced; this run did not deploy, send
production messages or perform native Windows acceptance.

## Two-host acceptance prerequisite

The user requires both the local Hermes and the C-end operations Mac mini peer
to pass before overall acceptance. Preflight found the local installed QQ plugin
already contains issue #10 timestamp/standalone protection. The candidate now
retains commits `fcde06c`/`8dc91c2` behavior rather than removing that live fix.
Its outbound module matches the existing local runtime byte-for-byte; the
new lifecycle and cron guards are tested on top of it. Host-specific results
must include exact bundle hashes, startup checks, real delivery and cleanup.

### macOS restart prerequisite

The operations peer's upstream API adapter uses `reuse_address=False` on
macOS. After stopping Gateway, an empty `lsof ... LISTEN` result does not prove
the port can be rebound: closed connections can still cause `EADDRINUSE`.
The adapter treats that error as non-retryable, so QQ may reconnect while the
peer API stays unavailable. This occurred during the September 24 acceptance
attempt and caused the candidate to roll back; it is not a passing acceptance.

For a supervised update, use an independent one-shot helper to stop the exact
Gateway service and wait for its tracked processes to exit. Before bootstrap,
also require a short-lived socket to bind the configured API address with
`SO_REUSEADDR` disabled, closing the probe immediately. Bound the wait and do
not terminate unrelated port owners. Use the same guard during rollback to
the saved plugin directories. Verify authenticated API health, QQ delivery and
unchanged core/configuration hashes afterward. This changes only the update
procedure; no Hermes source or socket security setting is changed.

### Historical explicit-target/pin result (2026-09-24, 1.8.29)

Candidate `31ade1f` passed real QQ acceptance on the local development bot and
the operations Mac mini peer. Local group/private results from September 23
remain valid: all 35 installed plugin Python files still match the candidate.
The peer passed explicit group and C2C delivery without pins, legacy `qqbot`
delivery with a `dm` pin, and conflicting-target rejection. Both hosts completed
real post-update agent replies. All ten downloaded TXT/PNG artifacts matched
their source SHA-256 hashes. Each host's prior 21-script regression gate passed.

The peer's 10,164 tracked core files and four protected configuration/hook files
retained their hashes. All nine finite test jobs across both hosts completed and
were disabled; temporary pins were removed, business jobs were preserved, and
temporary launchd helper plists were archived outside the auto-start directory.
This is candidate acceptance on two macOS hosts, not native Windows acceptance
or a merge/release authorization.
