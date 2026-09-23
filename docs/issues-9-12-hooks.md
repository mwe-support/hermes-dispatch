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

Explicit single QQ targets are guarded by default using profile-local durable
type data. Unknown type fails closed. Optional per-job pins override legacy
home/origin defaults and support both group and private QQ. A conflicting
explicit target fails before sending. The same guard covers files/images and
checks the actual API receipt; failure propagates to `last_delivery_error`.
No fallback can silently send to another conversation. QQ proactive permissions
are still required and cannot be granted by this code.

The repeatable test traverses the real scheduler/router/adapter, with QQ HTTP
replaced. It separately executes a real no-agent script while forbidding model
runtime import. Installation, configuration, verification and rollback are in
the [Codex plugin README](../plugins/codex-app-server-phase-hotfix/README.md#cold-start-lifecycle-registration-187)
and [QQ plugin README](../plugins/qqbot-connect-hotfix/README.md#cron-routing-guard-1829).

## Release integration

This branch starts at updater commit `eefed8a`; it retains the existing Windows
SQLite, hook installer and short-path compatibility changes. Versions are Codex
plugin 1.8.7 and QQ plugin 1.8.29, avoiding confusion with the independently
validated older hook branch. Both new regressions are in `ops/release.json`
(21 scripts total). No credentials, target IDs or business jobs are included.

Earlier local live QQ results belong to the earlier pinned-hook implementation.
They are useful prior evidence, not native Windows or production acceptance of
this combined branch. This change does not resolve Windows ACL issue #11 or
replace the separate issue #10 branch.

## Verification of this combined branch

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
