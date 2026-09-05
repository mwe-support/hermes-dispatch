# QQBot Connect Hotfix

Local Hermes QQBot adapter compatibility hotfix.

This plugin is seeded into each department profile as:

```text
/opt/data/plugins/qqbot-connect-hotfix
```

It is enabled by default in `templates/config.yaml`:

```yaml
plugins:
  enabled:
    - qqbot-connect-hotfix
```

Remove this plugin after the upstream Hermes image includes equivalent fixes for
QQBot connect signature compatibility, QQ group configuration interaction ACKs,
channel-directory chat routing,
`GROUP_MESSAGE_CREATE` context buffering, deterministic group-context compaction,
structured self-mention gating, emoji-only group mentions, reply `msg_id`
handling, native C2C streaming, bounded input notifications, markdown fallback,
and media caption compatibility.

Version 1.8.19 closes the remaining Issue #3 lifetime and failure-storm gaps.
Transport timeouts and disconnect-style errors are ambiguous because QQ may
have consumed a frame before the response was lost. The plugin now retains the
exact submitted frame and permits one bounded reconciliation of that index. An
`index needs to increment` response proves the first request was accepted, so
local ownership advances once; an exact reconciled seal is not sent again. If
the reconciliation is still inconclusive, the carrier is retired and the
latest unacknowledged cumulative body remains assigned to the ordinary final
fallback instead of being silently discarded.

Opening a carrier also arms an independent, cancellable 480-second monotonic
deadline. It seals the acknowledged carrier even when the turn emits no new
draft delta, then atomically leaves the same turn ready to open a fresh carrier
at index 0. A failed deadline seal deliberately retires the old carrier.
Completion, abandonment, explicit rollover, and state removal cancel the timer
so it cannot emit a late seal.

Non-terminal frame failures now use a per-carrier `0.2/0.8/2.0/5.0` second
bounded cooldown. Deltas received during a cooldown make no QQ request and
replace one retained cumulative body; the next eligible attempt sends only the
latest body. A final may bypass the progress cooldown, but its lossless target
includes the newest coalesced cumulative text. No new configuration key is
required.

QQ's `40034128` / `回复消息失败，被动回复时间或者次数超过限制`
response is not a transient frame failure. It means the inbound passive-reply
anchor can no longer authorize either a replacement carrier or an ordinary
reply. The plugin immediately retires the active replacement carrier and
suppresses later draft attempts and direct final fallback requests for that
turn instead of entering the non-terminal cooldown loop. The rejected body is
not recorded as visible, and the explicit failed final result leaves recovery
to the Gateway rather than falsely claiming delivery.

The real Gateway consumer can stream the final answer as deltas immediately
after commentary and then replace its accumulator with the same authoritative
`final_response` during `finish()`. There may be no whitespace at that phase
boundary. Version 1.8.20 corrects the 1.8.19 shortcut: turn-final identity alone
does not prove that a matching suffix was emitted as final deltas. The patch
records the current unfinished, think-filtered delta segment and its complete
ledger before upstream `finish()` can overwrite it. Completed commentary and
tool-segment boundaries reset the candidate segment. Only a final that equals
or prefix-extends that explicit segment can reuse its ledger, and only when
the result preserves QQ's entire acknowledged prefix. This also covers a tail
drained in the same tick as `finish()` and a post-stream verifier footer.
An independent `FINAL` after `status NOTFINAL` is appended, not swallowed.
Generic/direct sends retain the existing token-boundary rule. Provenance is
consumer-local, passed through a task-local adapter/chat/anchor context, and
does not modify QQ wire metadata or other platforms.

Enable: install the persistent plugin and keep the existing native-C2C
streaming settings; no new config key is needed. Roll back using the installer
backup as described below; do not edit the Hermes installation.

Version 1.8.21 gives accepted busy-message redirects and `/steer` a new **display
segment**, without changing the Hermes session, Codex thread or agent turn.
Upstream Hermes preserves the cumulative native draft across tool boundaries;
without this compatibility hook, post-steer output appears above the user's
correction inside the old bubble. A task-local route and per-session lock now
pause the consumer at its existing FIFO flush barrier, observe the agent's
actual acceptance, seal the old carrier, send the existing acknowledgement,
then let the next delta open a new draft using the correction's reply anchor.
For this active QQ/Codex context only, `steer()` uses Hermes' existing native
`redirect()` → Codex `turn/steer` implementation: upstream `steer()` otherwise
queues for Hermes-owned tool batches, which Codex app-server never drains.
An accepted local queue is not treated as proof of Codex acceptance. Other
platforms, standalone agent calls and non-Codex runtimes keep upstream behavior.
The full ledger is retained, but previously visible text is a committed prefix
and is never copied into the new bubble. Previous display segments cannot own
an independently completed answer just because its text matches theirs.

Acknowledgement cooldown/disable settings do not suppress segmentation. Empty
segments make no QQ request. Rejected or unauthorized steering keeps upstream
behavior. A five-second flush timeout queues the correction for the next turn
without calling the agent or claiming successful redirection. Seal failures use
the existing bounded retries, then retire the old carrier locally and retain
unacknowledged text for the next bubble; a warning explicitly records that QQ
did not confirm closure. Input-task cancellation after acceptance still commits
the display transition; `/stop`/`/new` keep the consumer's stale-run guard. The
old anchor rejects late drafts/finals, while the original background task's
final fallback is rebased to the latest anchor. None of this marks the agent
turn complete. Groups and guild DMs keep their existing non-C2C transport.

Enable/rollback: install/restore the persistent plugin with the existing C2C
settings, then restart only the target profile; no new setting or upstream
source edit is required. First run `test_steer.py` (real Gateway busy callbacks,
consumer and adapter with a fake QQ wire) and the complete existing matrix.
Then canary a real private chat: old bubble sealed → redirect acknowledgement →
new bubble, one final, unchanged thread, and no ordinary duplicate or `40034128`.
Repeat for `/steer`; check a group @mention stays on the existing group path.
Verify the explicit command's correction is present in the Codex transcript
and its requested final is returned, not merely an acknowledgement/new bubble.
The [1.8.21 procurement acceptance record](../../docs/evidence/qq-steer-1.8.21/README.md)
contains the final-build private/group results, lifecycle timings and excluded
first attempts. Steering acceptance does not imply instant cancellation of an
already running native tool.

Verify with `test_streaming.py`, `test_steer.py` and the complete QQ/plugin/install matrices.
The deterministic regressions cover accepted-frame timeouts, ambiguous seals,
an unknowable index-0 response, silent expiry with no new callback, timer
cancellation, repeated non-terminal failures, coalescing, and a final arriving
during cooldown, terminal passive-reply-budget retirement after rollover, and
a combined age-rollover + consecutive-commentary + 9,000-character streamed
final whose completion payload has exactly one visible owner.
Real-consumer negatives cover independent suffix finals with and without a
completed-commentary callback; additional cases cover tool boundaries,
think-filtered and chunked deltas, same-tick tails, augmented and rewritten
finals. See the [review evidence bundle](../../docs/evidence/pr-4/README.md)
for separately versioned live captures, redacted logs, and carrier ownership.
Before release, also run one real QQ C2C turn longer than 12
minutes with more than 9,000 final characters and a WebSocket reconnect. Every
message must remain at or below 4,000 characters, the completion marker must
appear once, every carrier must be sealed or deliberately retired, and logs
must contain no stale-index storm or duplicate final. Roll back from the exact
external installer backup and restart only the affected profile.

The 1.8.19 procurement acceptance run completed on 2026-08-29: 872.5 seconds,
one silent 480.5-second age rollover, one natural `4009` reconnect, a 10,234
character final, and visible carrier lengths of 4,000, 4,000, and 2,318 after
the 78-character progress carrier. QQ showed one response completion marker;
Gateway confirmed final suppression with no stale-index, passive-reply-budget,
ordinary-fallback, or duplicate-final error in the acceptance interval.

Version 1.8.18 fixes a real QQ regression discovered during the 1.8.17 age
rollover canary. Codex can finish consecutive commentary items whose streamed
deltas are concatenated without whitespace, for example `STARTSTEP1`. Hermes
then emits a completed `_interim_send` callback containing only `STEP1`. The
general token-boundary guard correctly refused to infer ownership from that
word-internal suffix, but the result was one growing native bubble plus one
ordinary duplicate bubble for every completed stage.

The plugin now wraps only `GatewayStreamConsumer._send_commentary()` for an
active QQ C2C native lane and publishes a task-local completed-commentary
context while the original callback runs. The adapter may accept a boundaryless
terminal suffix only when adapter, chat, inbound reply anchor, and exact cleaned
commentary all match that same consumer callback and the native carrier already
ends with the text. Generic `_interim_send` calls, unrelated statuses, other
anchors, ambiguous streams, and existing word-internal overlap negatives keep
their prior behavior. Verify with the consecutive-commentary consumer
regression and a real QQ private task that emits at least two alphanumeric stage
markers; only one growing native carrier may be visible before the final.

Version 1.8.17 prevents expired QQ C2C native carriers from entering a stale
index/seal retry loop. QQ documents monotonically increasing `index` values and
one stable `stream_msg_id`, but does not publish a carrier lifetime. Production
evidence showed a carrier expiring after roughly ten minutes and, critically,
QQ consuming the last frame while returning `同一流式消息发送超过时间限制`;
retrying that index then returned `请求参数index需要递增`.

The plugin now rolls an open carrier at an internal 480-second monotonic safety
boundary, sealing its acknowledged body and opening index 0 on a new carrier
before the observed expiry. If the exact terminal lifetime response still
occurs, the submitted frame becomes the carrier's final visible owner and that
carrier is deliberately retired: later drafts, final cleanup, and abandonment
never send another index or seal to it. A real final uses the existing
per-anchor single-flight and tombstone machinery to send only the suffix not
owned by the terminal frame; a terminal frame that already contains the whole
final sends no ordinary duplicate. Other API/network errors retain the existing
retry and final-fallback behavior.

No new configuration key is required. Enable the existing QQ streaming
settings, install with `scripts/install-plugins.sh`, and verify with
`test_streaming.py` plus the complete plugin and installer matrices. The
regressions cover age rollover, terminal responses during draft/final/seal,
late frames, cancellation, suffix-only fallback, and repeated-final
suppression. Roll back from the exact external installer backup, restart only
the affected profile, and verify QQ Ready; disabling QQ streaming restores the
upstream non-streaming private-message path.

Version 1.8.16 retains successful abandon-first completion context on the
existing bounded per-anchor claim until every caller already registered on that
claim exits. A final or late draft queued behind the abandonment therefore does
not depend on the independently evictable per-chat tombstone. The contextual
owner is never copied to the long-lived replay LRU by abandonment alone and is
dropped with the claim; an arbitrary partial abandonment can suppress stale
draft frames but cannot swallow a different later final. If a registered final
matches the exact or strict terminal owner, that real final callback may then
publish normal completed replay evidence.

Terminal matching also accepts a suffix whose first character is whitespace or
Unicode punctuation other than connector punctuation, such as `\nFINAL`,
`,FINAL`, or `，FINAL`. The payload carries its own token boundary, so checking
only the character before the whole suffix would send a duplicate ordinary
final. `_FINAL`, partial overlaps, and word-internal cases remain unowned.
Dedicated public-adapter regressions force same-chat tombstone eviction while
abandonment, a final waiter, and a late draft share one claim, and separately
cover the leading whitespace/punctuation table. Enablement is unchanged: use
the streaming settings below and install through `scripts/install-plugins.sh`. Verify with
`test_final_delivery.py`, `test_streaming.py`, the full plugin matrix and static
checks. Roll back only from the exact external installer backup, then restart
and verify the affected profile.

Version 1.8.15 closes the completion-boundary gaps left by the initial
single-flight implementation. Hermes can cancel `GatewayStreamConsumer.run()`
while the broker-owned ordinary final request is still in flight; patched
`abandon_open_draft()` now coordinates on the same `(chat_id, reply anchor)`
flight before it reads or seals stream state, preventing the native final and
ordinary unseen suffix from both becoming visible owners. This compensates for
the upstream lifecycle in which `/new`, `/stop`, interruption and timeout
cleanup may abandon a stream concurrently with shielded final delivery.

Every stable-anchor `send_draft()` callback now joins that same keyed
transaction too. This closes the interval after final ownership starts but
before the broker result/tombstone becomes externally visible: neither the
original draft id nor a stale changed draft id can replace or open a carrier
while the final's ordinary fallback is in flight. A different reply anchor
still uses a distinct claim and is not serialized behind that final. In the
reverse order, if abandonment has already sealed a complete cumulative body,
a later short final is suppressed only when that payload is an exact
token-bounded terminal suffix of the recorded visible body; arbitrary partial
or word-internal overlap remains unowned. If old Hermes omits reply metadata,
the adapter freezes the current `_last_msg_id` before waiting, so a newer
inbound message cannot change which anchor the queued frame mutates.

The bounded 1024-key completed-result LRU is also the anchor-scoped completion
index for late drafts. While that LRU entry is retained, a fully sealed turn
cannot be reopened merely because a stale Hermes callback uses a different
draft id. After its anchor entry is evicted, only a still-retained tombstone with
the original draft id remains a secondary check; it does not extend the
changed-draft guarantee beyond the documented LRU bound. When no stable inbound
reply anchor exists, final delivery bypasses single-flight and
completed replay entirely: separate unanchored finals are delivered
independently instead of collapsing into `(chat_id, "")`. Dedicated regressions
cover both final-versus-abandon orderings, same- and changed-draft callbacks
during an active final flight, same-anchor changed-draft replay, sequential and
parallel unanchored finals, cleanup coordination, bounded anchor-result
eviction and different-anchor independence. Enablement and
rollback remain unchanged: use the streaming settings below, install through
`scripts/install-plugins.sh`, run both final-delivery and streaming suites, and
restore only an exact installer-created external backup before restarting the
affected profile.

Version 1.8.14 replaces path-specific final locks with a bounded keyed
single-flight broker. Every C2C `notify=True` completion path—including active
native rollover/fallback, unopened native state, final-only pending,
cancellation promotion, and completed replay—re-resolves lifecycle state and
finishes delivery plus ownership promotion inside one `(chat_id, reply anchor)`
transaction. The first successful result remains on that flight until every
already-registered caller exits, so waiter correctness does not depend on the
separately bounded completed-owner tombstone registry.

The broker admits at most 128 distinct active final keys. Same-key callers join
an existing flight even at capacity; a new key waits for a released slot, so
the registry has a hard bound without serializing independent chats or anchors.
The external delivery attempt belongs to the flight and is shielded from an
individual caller cancellation: a waiter observes the same in-flight result,
and only a definite failure can hand off to a new attempt. After the last
caller exits, successfully closed or fully delivered outcomes enter a separate
1024-key LRU replay cache that does not consume active-flight admission; this
covers a sole cancelled caller whose shielded QQ request finishes successfully.
A visible-but-still-open `qq_stream_close_pending` result is shared only with
callers registered on the current flight and is deliberately not replay-cached,
so a later final callback can retry the idempotent seal without redelivering an
ordinary-owned suffix. While that exact complete final remains visible but
unsealed, stale draft frames on the same inbound reply anchor are acknowledged
without extending, replacing, or opening a second stream, even if their Hermes
draft id changed; another anchor remains independent. If Hermes cancellation
cleanup later seals a retained
stream whose complete turn-final identity was already recorded, the adapter
refreshes the per-chat completed-owner tombstone and publishes the successful
close into the same bounded broker replay LRU; a later same-anchor final
therefore remains suppressed even if another anchor evicts the tombstone. An
arbitrary partial-draft abandonment has no such final identity and is not
promoted into the anchor-wide replay cache. Dedicated broker
contract tests cover 100 same-key callers, retained success after external
tombstone eviction, failure/exception handoff, holder and admission
cancellation, sole-holder replay, close-pending retry, bounded completed-result eviction,
different-key parallelism, capacity backpressure, same-key joining after a full
capacity wakeup, cleanup, and a 200-key stress run. The adapter integration
test covers three concurrent active-stream failures with exactly one ordinary
unseen-suffix message even when an independent anchor evicts the real
per-chat completed-owner tombstone before the waiting callbacks resume.

Version 1.8.13 serialized ordinary final ownership for each QQ private chat and
inbound reply anchor. The adapter registers a short-lived delivery claim before
awaiting the external QQ send, so concurrent `notify=True` callbacks cannot
both deliver the same cancellation or final-only-pending payload. A waiting
callback rechecks the lifecycle state after acquiring the claim: it suppresses
a replay after success, or retries the unchanged tombstone/pending record after
failure. Claims are reference-counted and removed when their last caller exits,
so unrelated chats and anchors remain independent and no adapter-lifetime lock
registry accumulates. Public concurrent regressions cover successful
three-caller cancellation and pending delivery, failure handoff, cancelled
waiter cleanup, cancelled-holder and raised-exception handoff, independent
anchors reaching the external boundary in parallel, an empty claim registry
after exit, and replay suppression.
Install with `scripts/install-plugins.sh`, run the complete Python command in
the Verification section plus `scripts/test_install_plugins.sh`, then restart
only the affected profile. Roll back only from the exact external backup
printed by a successful earlier install.

Version 1.8.12 separates cancellation evidence from successful final delivery.
When a capacity-triggered final-only turn is abandoned, its cancellation
tombstone still suppresses a stale `send_draft`, but it is not eligible to
suppress a later normal turn-final send. The first successful normal final
promotes that record to delivered ownership, so any repeated final is then
acknowledged without another QQ message.

Native-lane membership is now a 1024-chat least-recently-used registry instead
of an adapter-lifetime set. Inactive chats expire first; a chat with an open
native stream remains protected until the stream is sealed or abandoned, and a
live streaming disable still revokes that chat immediately. The installer now
preflights every requested plugin's canonical active target before any plugin
is created, backed up, cleared, or copied. A two-plugin regression proves that
an invalid second target leaves the first directory, including hidden files,
unchanged and creates no backup. Install with `scripts/install-plugins.sh`, run
the complete Python command in the Verification section plus
`scripts/test_install_plugins.sh`, then restart only the affected profile. Roll
back only from the exact external backup printed by a successful earlier
install.

Version 1.8.11 makes the public adapter identity boundary explicit. Active
native streams are keyed by `(chat_id, draft_id)`, so two private chats may use
the same Hermes draft id concurrently without sharing or rejecting state. A
capacity-triggered final-only turn that is successfully abandoned now records
a completed owner before its pending identity is removed, preventing a late
draft callback from reopening that cancelled turn after capacity is freed.

The completed-owner and final-only-pending registries retain their independent
256-entry per-chat quotas and now also cap their least-recently-used outer chat
sets at 1024. Recent activity moves a chat to the end of its own registry; when
the total-chat bound is exceeded, only the least-recently-used chat bucket in
that registry expires. This bounds adapter memory while preserving independent
quotas for recently active conversations. The installer also validates the
profile-level backup root before it creates an absent active plugin directory,
so a rejected fresh install leaves no empty `plugins/<name>` artifact. Public
adapter and installer regressions cover each behavior. Roll back only from the
exact external backup printed by the installer.

Version 1.8.10 isolates completed-turn ownership per private chat. Each chat has
its own 256-entry FIFO quota, so high completion volume in one QQ conversation
cannot evict another conversation's replay protection. Tombstones are now
created for every successful managed completion path: ordinary suffix fallback,
all-native seal (including rollover and second-round recovery), first-frame
final-only degradation, capacity-triggered final-only degradation, a
committed-only rollover head, and successful draft abandonment. Capacity-only
turns retain a separate per-chat pending identity until their ordinary final
succeeds, allowing the completed owner to keep the original Hermes draft id.
Repeated final callbacks and late draft frames for each path are regression
tested through the public adapter lifecycle.

The installer used by this release validates canonical filesystem boundaries
before any backup or replacement. A profile-level `plugin-backups` symlink is
rejected even on a first install; an active `plugins/<name>` symlink is rejected
for both install and restore; the canonical active target must be one direct
child of the canonical plugin root; and `.`/`..` are not valid plugin names.
These checks prevent an old manifest from escaping into recursive discovery,
prevent writes through an external active-target link, and prevent restore from
clearing the whole plugin root. `scripts/test_install_plugins.sh` covers normal
canonical install/restore plus every rejected boundary before active data is
changed. Roll back with an exact installer-created external backup as described
below.

Version 1.8.9 keeps final ownership after a successful native recovery close.
When an ordinary fallback has already delivered the unseen final suffix, the
adapter records a completed-turn tombstone keyed by private chat, inbound reply
anchor, and Hermes draft id before the active stream is removed. The map is
bounded to 256 recent turns. A repeated turn-final callback or late draft frame
for that exact completed turn is acknowledged without creating another QQ
message, while a different inbound anchor remains a distinct new turn even if
the draft id is reused. This compensates for Hermes consumer cleanup callbacks
that can arrive after QQ has accepted the recovery seal.

The terminal ownership boundary now uses Unicode punctuation categories plus
whitespace rather than a handwritten punctuation list. ASCII/CJK commas, em
dashes, and other Unicode punctuation therefore suppress an already-streamed
completed commentary carrier consistently, while word-internal suffixes remain
unowned. `test_streaming.py` covers successful recovery removal, repeated
finals, late frames, anchor isolation, bounded eviction, Unicode interim
carriers, and Unicode turn-final composition. Install with
`scripts/install-plugins.sh`; it now creates a timestamped external backup under
`$HERMES_HOME/plugin-backups` before replacement. Restore that exact backup
with the documented `--restore` command and restart only the affected profile.

Version 1.8.8 removes a second message-carrier race found by the real QQ C2C
canary. Codex app-server can stream a commentary item's live deltas and then
Hermes can emit the completed item again as an ordinary `_interim_send` without
its original reply anchor. When the same anchored open native stream already
owns that exact token-bounded terminal payload, the adapter now acknowledges
the interim callback without posting a duplicate ordinary QQ bubble. If Hermes
does not provide an anchor, the plugin recovers only a unique matching open
stream in the same private chat; multiple concurrent matches remain ambiguous
and keep the ordinary path. Earlier/nonterminal occurrences, word-internal
suffixes, unopened streams, other anchors, groups, and non-interim messages are
unchanged. `test_streaming.py` covers the real Gateway consumer sequence,
unique and ambiguous unanchored recovery, rollover-boundary ownership, and all
negative isolation cases. Roll back a deployed copy by restoring its external
pre-install backup; do not assume a version-named artifact exists in Git.

Version 1.8.7 closes the remaining final-ownership gaps found during review of
1.8.6. When an unseen final suffix is successfully delivered by the immutable
ordinary-message fallback but the native-prefix recovery seal remains pending,
the retained stream now records that ordinary owner. A delayed
`abandon_open_draft`, a repeated turn-final callback, or a late draft frame may
close the native prefix but cannot absorb or resend the ordinary-owned suffix.
This prevents a recovered stream from displaying the final twice.

Final composition no longer treats an arbitrary substring or suffix/prefix
overlap as ownership. A cumulative final must explicitly extend the complete
QQ-visible body. An independent final is considered already visible only when
the exact payload is at the terminal position with a token boundary; otherwise
the complete payload is appended once. The composer also respects an existing
leading whitespace boundary instead of inserting a second newline. These rules
compensate for Hermes turns where commentary may mention the same words as the
later independent final. `test_streaming.py` covers delayed close, final retry,
late-frame races, non-terminal repeats, partial overlaps, word-internal suffixes,
leading boundaries, cumulative replacement, and exact final ownership. Roll
back by restoring 1.8.6 from outside the plugin discovery tree and restarting
only the affected profile.

Version 1.8.6 makes final-message ownership explicit across sealed native
chunks, the currently visible native stream, and an ordinary fallback. Hermes
can supply either a cumulative final or a short independent answer; the plugin
now composes both forms losslessly with the QQ-acknowledged draft before one
unified rollover path. A full 4000-character draft therefore rolls an
independent final into a new stream instead of truncating it. If rollover stops
while sealing a head, only the suffix that QQ has never acknowledged is sent
normally. If a tail is already visible but its close fails, the plugin retries
that close without sending the same tail normally; after both bounded close
rounds fail, the visible state remains addressable for a later abandon/retry.
The seal composer also returns overflow explicitly instead of silently capping
it. These guarantees compensate for Hermes' mixed cumulative/final-only turn
payloads and QQ's immutable replace-prefix plus 4000-character stream limit.
No new setting is required: keep the C2C streaming settings below enabled and
run `test_streaming.py`. The regression suite checks exact single ownership for
full-draft independent finals, partial-draft growth, exhausted head-seal
retries, tail-open failure, and tail-close recovery. Roll back by restoring
1.8.5 from outside the plugin discovery tree and restarting only the affected
profile.

Version 1.8.5 extends rollover ownership to the authoritative final payload.
If the visible draft is still below QQ's limit but the final first crosses it,
the active stream is sealed at the limit and a new stream carries the suffix;
the suffix is no longer truncated. If a prior overflow head is already sealed
but QQ cannot open the new tail stream, the ordinary fallback sends only the
uncommitted suffix instead of duplicating the sealed prefix. A successful
suffix fallback also removes the unopened placeholder state. These two paths
are regression-tested with 3900-to-4100 final growth and repeated tail-open
failure. Roll back this release by restoring 1.8.4 from outside the plugin
discovery tree and restarting the affected profile.

Version 1.8.4 gives QQ native streams their own overflow lifecycle. When a
cumulative C2C reply exceeds QQ's per-message limit, the plugin seals the full
active stream chunk and opens a new stream for only the remaining suffix.
Every stream therefore keeps an independent prefix-stable `replace` sequence;
Hermes' generic ordinary-message overflow path is bypassed only for an active
QQ C2C native lane. The turn-final seal covers the last stream, so the sealed
chunks concatenate to the authoritative final response without an ordinary
duplicate.

The 1.8.4 route gate also distinguishes QQ C2C from guild direct messages.
Both arrive with `chat_type="dm"`, but only the adapter's explicit `"c2c"`
route may use `/v2/users/{openid}/stream_messages`. A live configuration
transition from enabled to disabled removes the chat from the native lane on
the next turn; an already-open stream remains addressable through the stream
map until it is sealed or abandoned. `test_streaming.py` covers continued
output beyond one message, final sealing, guild-DM rejection, and the
enabled-to-disabled transition. Roll back this release by restoring 1.8.3
from outside the plugin discovery tree and restarting the affected profile.

Version 1.8.3 activates the native QQ lane only after resolving both Hermes'
global streaming switch and `display.platforms.qqbot.streaming`. Consumer
creation for `interim_assistant_messages=true` is not treated as evidence that
streaming is enabled. This preserves the upstream typing and final-only path
when a profile explicitly opts out of streaming. Version parsing is also
strict: pre-release, local-suffix, and unknown version strings fail closed.

Version 1.8.3 requires Hermes 0.20.5 or newer for native C2C streaming. Hermes
0.20.0 does not pass `chat_id` to the draft-capability probe and its
`GatewayStreamConsumer.finish()` cannot accept the authoritative final text.
On an older or unknown runtime the streaming patch now fails closed: it does
not replace `send`, `send_typing`, or the Gateway stream gate, while the other
QQ hotfix modules continue to load. Check with `hermes --version`; on an older
installation run `hermes update --check`, run `hermes update --plan` only when
`hermes update --help` lists that option, then run `hermes update --backup` and
verify a stable 0.20.5 or newer release before enabling the settings below.

The 1.8.2 typing budget applies only after the Gateway has selected a native
C2C lane for that chat (or a native stream is actually open). With streaming
disabled, the plugin leaves Hermes' original periodic `send_typing` behavior
unchanged. A transient final-seal error is retried at the same unacknowledged
index. If those retries fail, the complete ordinary final is sent first; the
plugin then tries to close the older stream with its last acknowledged partial
body, avoiding a second copy of the final answer. If QQ remains unavailable,
the opened state is retained so `abandon_open_draft` or a later seal attempt can
still close it. Capacity pressure removes only streams whose first frame never
opened; it never discards a client-visible stream. The extra turn stays
final-only when all 128 slots are opened. These disabled-mode, retry, recovery,
safe-degradation, and capacity contracts are covered by `test_streaming.py`
against the official Hermes 0.20.0 and 0.20.5 release sources.

Version 1.8.1 preserves QQ's already-submitted stream prefix when Hermes seals
a tool-using private-chat turn. Hermes' cumulative draft can contain commentary
and tool progress before the final answer, while its turn-final `send()` may
contain only the short final answer. Sending that shorter text with
`input_mode=replace` removes the visible prefix and QQ rejects the
`input_state=10` frame as immutable content. The hotfix now seals with the
cumulative draft when it already contains the final, or appends only the
non-overlapping final suffix when needed. This keeps one visible message,
allows the native stream to reach its completed state, and retains the existing
single ordinary-final fallback if the stream itself fails. Verify with
`test_streaming.py` and a tool-using C2C task whose commentary is visible before
a short final. Roll back by restoring 1.8.0 and restarting only the affected
profile Gateway.

Keep rollback copies outside every configured plugin discovery root. Hermes can
recursively discover a `plugin.yaml` below the profile `plugins` directory, so
a path such as `plugins/.backups/qqbot-connect-hotfix-1.8.0` may register the
old copy before the active plugin. A profile-level path such as
`plugin-backups/qqbot-connect-hotfix-1.8.0-<timestamp>` keeps the backup
available without loading it.

Version 1.8.0 adds QQ's official C2C streaming-message protocol without
modifying the installed Hermes package.  The plugin advertises native draft
streaming only for private chats, maps Hermes cumulative draft frames to
[`POST /v2/users/{openid}/stream_messages`](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_users_user_openid_stream_messages.post.html)
with `input_mode=replace`, and seals
the same visible message with `input_state=10` when the turn completes.  Each
stream is keyed by chat, inbound `msg_id`, and Hermes draft id so concurrent
private sessions cannot share indices or `stream_msg_id` values.  Approval,
slash-command, heartbeat, and steering messages bypass the seal path and remain
independent messages.

Hermes 0.20.5 still rejects adapters with
`SUPPORTS_MESSAGE_EDITING=false` before its stream consumer probes native draft
support. QQ ordinary messages are not editable, but C2C streams do not require
ordinary-message editing. Version 1.8.0 therefore bypasses that legacy gate
only when the active adapter is QQ and the source is a C2C chat. QQ groups and
all other non-editable platforms retain the upstream guard. If a native frame
cannot be opened, the adapter keeps the consumer in a buffered final-only lane
so the user receives one ordinary final instead of an uneditable partial plus
a duplicate final.

QQ counts `input_notify` calls against the passive-reply budget associated with
an inbound message.  Hermes normally refreshes that status every 50 seconds;
long turns can therefore exhaust the budget before their final response.  The
1.8.0 patch permits at most one typing notification per inbound `msg_id` and
then uses the native stream for continuing status.  If the first stream frame
fails, Hermes falls back to its normal final-message path.

Enable the feature per profile:

```bash
hermes --version  # must report 0.20.5 or newer
```

```yaml
streaming:
  enabled: true
  transport: auto

display:
  platforms:
    qqbot:
      streaming: true
      interim_assistant_messages: true
      tool_progress: new
```

Verification:

```bash
PYTHONPATH=/path/to/hermes-agent \
  /path/to/hermes-agent/venv/bin/python \
  plugins/qqbot-connect-hotfix/test_final_delivery.py
PYTHONPATH=/path/to/hermes-agent \
  /path/to/hermes-agent/venv/bin/python \
  plugins/qqbot-connect-hotfix/test_streaming.py
PYTHONPATH=/path/to/hermes-agent \
  /path/to/hermes-agent/venv/bin/python \
  plugins/qqbot-connect-hotfix/test_steer.py
```

In a real QQ private chat, start a tool-using task and verify that one message
updates in place, a completed commentary is not repeated in an ordinary bubble,
the last frame is sealed rather than duplicated, and accepted `/steer` seals
only the old display segment before its acknowledgement and new bubble. Logs
must contain neither error `40034128` nor a second final send. Roll
back by setting
`display.platforms.qqbot.streaming: false` and restarting only the affected
profile's Gateway. The restart creates a fresh adapter, so the native-lane
typing budget is also removed. To roll back the complete current code change,
restore the previous plugin directory from outside the plugin discovery tree
with `scripts/install-plugins.sh --restore`, then restart that profile.

Version 1.7.0 adds the narrow expired-reply fallback used by upstream Hermes
PR [#85221](https://github.com/NousResearch/hermes-agent/pull/85221). QQ can
reject a valid inbound `msg_id` after a long-running turn. Text sends now keep
the reply anchor on the first attempt and, only when QQ explicitly reports that
`msg_id`/`message_id` expired, retry once as a standalone message. The same
low-level wrapper covers C2C text, group text, approval keyboards, and guild
text while preserving the keyboard payload. It does not change task lifetime,
Codex app-server timeouts, or media delivery; those remain separate concerns.

Version 1.6.1 keeps the shared-group approval wrapper compatible with both
Hermes 0.18.2 and the newer 0.19-era cross-adapter contract. New Gateway code
passes an explicit `allow_session` keyword to `send_exec_approval`; the old
wrapper rejected that keyword before QQ could send a keyboard and forced the
Gateway into its plain-text `/approve` fallback. The wrapper now accepts
current and future keyword additions, forwards only parameters implemented by
the installed adapter, and preserves `allow_session` when the adapter supports
it. This is a runtime signature compatibility fix; it does not broaden any
approval scope.

Version 1.6.0 restores the Codex approval choices that Hermes 0.18.2 drops from
QQ. The upstream adapter passes `allow_permanent=False` for command and file
requests, so QQ renders only allow/deny even though app-server supports
`acceptForSession`. The Codex compatibility plugin now records the exact
request-scoped choices on Hermes' existing short-lived approval queue. This
plugin reads that entry and renders:

- **本次允许**
- **会话允许**
- **始终允许同类**, only when Codex proposed a persistent command or network
  policy amendment
- **拒绝**

The buttons use two rows for QQ mobile compatibility. The new
`allow-session` callback maps to Hermes' existing `session` queue decision.
Permission and file-change prompts never claim permanent scope; Computer Use
shows permanent approval only when its elicitation advertises `persist=always`.
If the Codex plugin is disabled or the queue has no decision metadata, the
upstream QQ keyboard remains unchanged.

Version 1.5.4 also compensates for shared-group approval ownership. With
`group_sessions_per_user: false`, Hermes' group session key contains no user id,
but the upstream QQ click validator requires one; consequently every approval
button is rejected, including a click by the person who initiated the turn.
The plugin captures `HERMES_SESSION_USER_ID` when the approval is sent, places a
short-lived opaque nonce in the QQ button, and resolves the real Gateway session
only when the same group member clicks it. The nonce is single-use, expires with
the normal five-minute approval timeout, and is kept only in process memory.
This preserves shared group context without allowing other members or stale
buttons to approve a later request. The same requester check covers typed
`/approve` and `/deny`, so the text fallback cannot bypass button ownership.

Compatibility contract:

- Tencent's current connector answers `INTERACTION_CREATE` configuration query
  and update types `2001`/`2002` with a `claw_cfg` object. Hermes 0.18.2 ACKs
  those events without the required data. This plugin implements the narrow
  ACK contract, including QQ's `claw_type=openclaw` wire identifier, and
  defaults `QQBOT_GROUP_RECEIVE_MODE=all`. The independent
  `QQBOT_GROUP_MESSAGE_CREATE_MODE=mention` default still prevents the agent
  from responding to every passive group message.

- When delivered, QQ group event payloads expose `id`, `content`,
  `group_openid`, and `author.member_openid` on
  `GROUP_AT_MESSAGE_CREATE`/`GROUP_MESSAGE_CREATE`, so the latter can be
  buffered while only mention messages are routed to the agent.
- QQ defaults each group's robot receive scope to mention-only. The **group
  owner** (not an ordinary member or group administrator) can open the QQ group
  robot settings and select **获取全部群消息**. QQ then delivers ordinary group
  traffic as `GROUP_MESSAGE_CREATE` on the existing connection; this plugin
  buffers those events and injects recent context when the bot is mentioned.
  Before returning from a passive event, version 1.5.3 invokes the optional
  `message-snapshot-store` raw-capture hook. This keeps full-message snapshots
  independent of plugin load order without routing passive traffic to the
  agent.
  Without that per-group switch, the server does not deliver unmentioned
  messages and no Hermes-side plugin or database wrapper can recover them.
  The native owner setting itself is authoritative. The separate 2001/2002
  compatibility patch responds with `claw_cfg` only when QQ sends a connector
  configuration interaction; owners do not need to toggle or reconfirm an
  already effective native permission just to activate snapshot capture.
- QQ replies use the explicit inbound `reply_to` while it remains valid and do
  not reuse stale `_last_msg_id` values. If QQ explicitly rejects that anchor as
  expired, version 1.7.0 retries text or keyboard delivery once without the
  reply relationship. Unrelated errors are returned unchanged. Media does not
  use this fallback yet.
- QQ may label a message that mentions another member as
  `GROUP_AT_MESSAGE_CREATE`. Version 1.5.2 and later check the authoritative
  `mentions[].is_you` field, so @owner/@member traffic is captured as context
  but does not wake the agent; only an actual @bot does.

Group context controls:

```text
QQBOT_GROUP_CONTEXT_MESSAGES=20
QQBOT_GROUP_CONTEXT_BUFFER_MESSAGES=100
QQBOT_GROUP_CONTEXT_CHARS=4000
QQBOT_GROUP_CONTEXT_SUMMARY_CHARS=1200
```

When the buffered group history exceeds the message or character threshold, the
plugin sends a compact extractive block: a count and small sample of earlier
messages plus the latest messages that fit in the budget. It does not call an
LLM for compression.

## Ablation simplification (1.8.22)

Four independently checked deletions remove a redundant cancellation rethrow,
an unreachable duplicate final-completion branch, and two repeated lane-registry
cleanup scans. No protocol behavior changes: claim-owned tasks remain shielded,
completed delivery remains replayable, and active stream ownership still blocks
LRU eviction. Unmark/remove each scan the lane registry once instead of twice.

This remains a mounted plugin workaround for Hermes' missing native QQ C2C
lifecycle. Enable/install and rollback as described above; to verify the
simplification run `test_streaming.py`, `test_final_delivery.py`, and
`test_steer.py` in an isolated Hermes environment. A negative ablation removing
`asyncio.shield` fails the cancelled-holder delivery check and is **not** retained.
See `docs/ablation-2026-09-05.md` for the baseline, individual results and live
file-delivery acceptance. Existing profile data and credentials are unchanged.

## Codex output attachments (1.8.22)

Codex may deliver a local Markdown download link or a
`:codex-file-citation{path="..." purpose="output"}` instead of a `MEDIA:`
directive. Hermes' streamed-final dispatcher intentionally extracts explicit
media directives only, leaving these output references visible but not uploaded.

The QQ adapter's final `extract_media` boundary now converts these references to
the existing Hermes attachment format. It uses Hermes path validation, resolves
file names, removes the original target from the returned display text (avoiding
non-streaming bare-path double uploads), and avoids adding directives for already-listed files.
It reuses QQ's existing upload and `file_info` / `msg_type=7` sending code, per
[QQ's official rich-media contract](https://bot.q.qq.com/wiki/develop/api-v2/server-inter/message/rich-media.html).
No upload client, hosting service, dependency, or model prompt is added.

Only explicit output citations and absolute/home-relative inline Markdown links
(including angle-bracket paths with spaces and local file URIs) are newly
recognized. Source citations, line references, examples in code/quotes, remote
URLs, malformed links and unsafe/missing paths are not promoted. Ordinary paths
and explicit media directives outside examples retain Hermes' delivery behavior. The new
parser runs in QQ final delivery, including background/queued responses; it does
not scan historical tool results or alter Codex runtime/streamed text. An already
streamed citation can remain visible as text, followed by the real attachment.

Install into an idle, selected canary Gateway:

```bash
scripts/install-plugins.sh "$HOME/.hermes/profiles/procurement" qqbot-connect-hotfix
hermes -p procurement gateway restart
```

Verify with isolated `HERMES_HOME`, the Hermes Python environment and Hermes
source on `PYTHONPATH`:

```bash
python plugins/qqbot-connect-hotfix/test_file_delivery.py
python plugins/qqbot-connect-hotfix/test_streaming.py
python plugins/qqbot-connect-hotfix/test_final_delivery.py
python plugins/qqbot-connect-hotfix/test_steer.py
```

File tests drive real Gateway streamed/ordinary dispatch and QQ upload code,
replacing only HTTP; private and group sends must upload matching bytes exactly
once. For live acceptance, start a fresh QQ conversation and ask naturally to
create a CSV. Omit delivery requests such as "send me", media directives, and
upload APIs. Require
a downloadable file card and compare downloaded bytes to the generated file.
See `docs/ablation-2026-09-05.md` for recorded results and limits.

The follow-up live comparison disabled automatic skill/memory learning and used
identical natural-language TXT/CSV/JSON requests. Native Codex skills, native
Codex UserPromptSubmit hooks, and this extraction bridge each delivered 3/3
downloadable files. That small sample does not rank long-term reliability. The
bridge remains the default here and adds no persistent prompt guidance. Hermes
skill discovery and pre_llm_call context did not reach the Codex app-server in
the diagnostic controls; merely registering them is not evidence of model
exposure. The report records the actual loading boundary and restored state.

The extended paired comparison used six report/PDF, presentation and code requests
without delivery wording. Both native mechanisms triggered in 7/7 actual runs;
each delivered 5/6 first attempts and covered 6/6 scenarios after one runtime
silence interruption was retried. All 14 target/example attachments matched their
QQ downloads. This did not establish a reliability winner or add persistent
guidance. See the ablation report for exact prompts, separate interruption
counts, artifact-validation limits, and restored environment.

Version 1.8.23 also reports a failed post-stream media upload through Hermes'
existing user-visible attachment-failure notice. Upstream ordinary delivery
already checks `SendResult.success`, but its post-stream rescan ignores that
return value. This QQ-only wrapper adds the missing notice after the existing
caption retry finishes, without duplicating ordinary-path notices or retrying
the upload itself. `test_file_delivery.py` includes a failing HTTP upload
control and verifies one notice on failure and one attachment on success.

Gateway-dependent patches install when the first QQ adapter is constructed,
before it receives messages. Importing `gateway.run` during Hermes' background
plugin discovery can deadlock with the main thread waiting for that discovery.
`test_startup.py` checks that discovery does not import GatewayRunner and that
the first adapter activates the patches. No special launcher or disabled
background discovery is required.

The optional Codex-native QQ delivery hook is documented in
[`codex-app-server-phase-hotfix`](../codex-app-server-phase-hotfix/README.md#qq-file-delivery-hook-184).
It is installed separately per profile/CODEX_HOME; this QQ plugin does not
modify Codex hook configuration or hook trust. Remove that hook before rolling
its companion Codex plugin back to a version without the hook script.

The 2026-09-05 procurement acceptance with the native hook enabled covered
six continuous private-chat turns and six continuous real-group turns. All
12 emitted MEDIA and delivered the expected files: 16 downloads matched
source bytes, with 16 file uploads and no observed duplicate file cards.
Private streaming text still showed occasional MEDIA/citation fragments;
this is not a text-rendering fix. The earlier hook-only/bridge-only controls
and the scope of this single-model canary are recorded in
[`docs/ablation-2026-09-05.md`](../../docs/ablation-2026-09-05.md).

Rollback using the exact backup printed by the installer:

```bash
scripts/install-plugins.sh --restore "$HOME/.hermes/profiles/procurement" qqbot-connect-hotfix <backup-directory>
hermes -p procurement gateway restart
```

Configuration, credentials, other plugins and generated files are preserved.
Remove this bridge once upstream QQ extraction recognizes these output formats
consistently in streamed and ordinary delivery.

### Compatibility corrections (1.8.24)

Version 1.8.24 supports the official Hermes 0.20.5 release
[`v2026.8.19`, `fcbd1076a`](https://github.com/NousResearch/hermes-agent/tree/fcbd1076a93841fa88855acce810e342a5b78101)
and 0.21.0 release
[`v2026.8.31`, `29112bef`](https://github.com/NousResearch/hermes-agent/tree/29112bef099274229cadff79cdff7bf7b99c4b77).
The path validator's signature is inspected: older Hermes receives only
`path`, while newer Hermes also receives `session_key`. Validation is never
skipped, and a TypeError inside the validator is not treated as an API fallback.
The original 1.8.22/1.8.23 bridge regressed both explicit MEDIA and output
references on official 0.20.5; use 1.8.24 for that release.

Earlier live acceptance used development commit `1bbb6e5bc`, whose version
field reported 0.20.5 but whose validator already supported `session_key`.
That result is not proof of compatibility with the official 0.20.5 tag.

QQ scans now use CommonMark block source maps from `markdown-it-py`, already
included by Hermes' required Rich package, and its actual inline-code rule
(including escaped openers and equal-length delimiters). Fences (including tildes, longer and unclosed fences), indented
code, nested/lazy blockquotes and inline examples are protected during both
MEDIA extraction and ordinary bare-path scanning. Top-level indented code is
represented as an equivalent fenced block so image removal and Hermes' intervening `.strip()`
cannot turn its contents into attachment candidates. Example content remains
text; QQ's normal display formatting still applies. Upstream-recognized inline
MEDIA forms, including a backtick-quoted path, remain supported. Duplicate
detection uses the same protected extraction as delivery, so an example cannot
suppress a real output of the same path. No new dependency installation or global Base adapter patch
is introduced.

Enable by installing this QQ plugin version and restarting the selected profile;
the Codex hook script, registration and trust do not change. Run
`test_file_delivery.py` against each official source checkout using the isolated
environment described above. Its public Gateway/QQ tests cover existing MEDIA,
new references, example-only and mixed replies, C2C/group, streamed/ordinary
delivery, safe path validation and exactly-once success/failure behavior.
Use the same backup-based rollback command above; reverting to 1.8.23 also
restores the known compatibility defects.

The final 1.8.24 review gate passed 13 isolated regression scripts against
each official release above and development commit `1bbb6e5bc` (39 runs).
Two independent review axes found no remaining P0/P1 after their initial
counterexamples were fixed. The procurement-only live canary then downloaded
one byte-matching CSV in each of a private chat and a real group; follow-up
replies containing the actual files' links solely in code/quote examples sent
no files. Replaying those same finals through the old bridge would extract
one attachment each. See the ablation document for hashes, scope and limits.

### Trailing-example correction (1.8.25)

The 1.8.24 mixed-response test placed the real output after the example.
The reverse order still lost a real output: generated MEDIA text appended at
the end became a lazy continuation of a trailing blockquote or part of an
unclosed fence. Adding a blank line cannot terminate an unclosed fence.

Version 1.8.25 keeps validated output attachments as structured extraction
results, separate from the reply text. It no longer appends generated MEDIA
directives to the model's text. Native MEDIA handling, path safety, duplicate
filtering and the existing QQ uploader remain in use, and the original example
is retained for display. The same official 0.20.5/0.21.0 compatibility applies.

Native MEDIA is captured from the original reply before display links are
replaced. A safe plain basename may remain in the text; names containing
spaces or Markdown syntax use the neutral label `attachment` there. The native
QQ file card and downloaded file retain the exact filename. This prevents
filenames such as `~~~report.txt` from changing the example boundaries during
later ordinary-path scanning.

Install 1.8.25 and restart only the selected profile; no hook registration or
trust update is required. `test_file_delivery.py` covers both orderings with
Markdown links and output citations, trailing/indented quotes, unclosed tilde
and backtick fences, closed-fence controls, and streamed/ordinary C2C/group
delivery. It requires one byte-matching real attachment and zero example
uploads. Existing native MEDIA and audio markers are also checked. Rollback
uses the same installer backup command above; 1.8.24 restores this known P2.

The 1.8.25 submission gate passed the 39-run official/development source matrix
and two independent reviews with no remaining P0/P1/P2 findings. A fixed-reply
canary covered both trailing quotes and unclosed tilde fences in real procurement
private/group chats: four real files were downloaded with matching bytes, zero
example files uploaded. The exact four model replies each reproduce zero
attachments through 1.8.24 and one through the fix. This is targeted delivery
acceptance, not a claim about natural generation or unlimited input coverage.
