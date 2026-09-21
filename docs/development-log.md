# Development Log

## 2026-09-21 — Native Windows updater and plugin regression compatibility

The first real Windows canary for the cross-platform dispatch updater exposed
three updater defects: enterprise policy denied `tasklist`, Task Scheduler
rejected a `/TR` command longer than 261 characters, and delayed SQLite handles
turned passing regressions into `WinError 32` cleanup failures. The updater now
uses `OpenProcess(SYNCHRONIZE)` for PID liveness, a short profile-local `.cmd`
wrapper for the Scheduled Task action, and bounded best-effort cleanup for its
outer regression directory. The canary verified InteractiveToken, Limited,
PT30M and a successful immediate task run without touching the default profile.

The second Windows canary then isolated plugin-test defects rather than
bypassing the release gate. Codex project and snapshot stores now close SQLite
connections at transaction exit, the project test expects the portable Windows
basename, QQ output extraction accepts validated drive-absolute paths, and the
Codex hook installer uses `msvcrt` locking on Windows instead of importing
`fcntl`. These changes are versions 1.8.6, 1.8.26 and 1.1.1 respectively. No
ACL or allowed-root relaxation was added. Rollback uses the updater's exact
pre-update backup; reverting restores the old Windows failures. macOS and the
full release manifest regressions must remain green before another Windows
canary is allowed to apply.

The third Windows canary found only test portability gaps: one migration
assertion still built the expected project path from raw colons, and two
symlink safety assertions assumed the current user held Windows symbolic-link
privilege. The regression now uses the same portable basename helper as the
runtime and skips only those two symlink assertions when Windows returns error
1314; every other setup error still fails the gate. Runtime behavior and
security boundaries are unchanged. Revert this test-only change to restore the
stricter host-privilege requirement.

The fourth Windows canary exposed two more host-dependent fixtures. The Codex
App registration check now starts with its opt-in disabled so a machine-level
environment setting cannot pre-register the fixture before the call-count
assertion. The QQ filename suite omits only the `>` basename on Windows, where
the filesystem rejects that character before message parsing is exercised.
All legal-name coverage remains active; runtime behavior is unchanged.

## 2026-08-31 — Move accepted QQ steering to a new display segment

Version 1.8.21 compensates for Hermes 0.20.5 preserving one cumulative native
draft after an accepted redirect. A real QQ reproduction showed new output in
the old bubble above the correction and acknowledgement. The Gateway consumer
regression reproduced `BEFOREAFTERDONE` in one sealed carrier before the fix.

The persistent QQ plugin now uses the upstream FIFO flush marker as a display
barrier, observes actual agent acceptance, seals/retires the old carrier and
rebases the live consumer plus its background-task final fallback. Existing
carrier retry, lifetime, prefix and final-ownership helpers are reused. A
per-session lock serializes steering; task-local identity prevents other
profiles/chats inheriting it. ACK text is never parsed, ACK throttling does not
govern segmentation, and no task/thread/session is recreated. The old reply
identity is closed to late callbacks without marking the agent turn complete.
An independent same-text final belongs to the new display segment.

The first canary passed ordinary redirect but exposed a second upstream gap:
explicit `/steer` acknowledged its local queue while Codex completed the old
instruction. Its correction was absent from the Codex transcript. The new
regression failed with `/steer never reached Codex turn/steer`. Only inside the
active QQ/Codex steering context, the plugin now routes `steer()` through the
existing native `redirect()` / `turn/steer` and relies on real acceptance.
There is no second Hermes pending-steer queue and no new Codex turn.

Tests cover ordinary/explicit steering, disabled/debounced and failed ACKs,
repeated steering, same-tick final, fallback rebasing, late callbacks, seal
retry/exhaustion/cancellation, deferred text, overflow, unauthorized/rejected
input, empty initial output, barrier timeout queueing, stop during ACK and
parallel routes. Group transport remains unchanged. Real QQ acceptance is
recorded separately after the complete plugin/installer/static matrices pass.

Enable by installing the persistent plugin with existing native-C2C settings;
no config migration. Canary only the approved profile. Roll back with
`scripts/install-plugins.sh --restore` using the exact external installer
backup, then restart only that profile. Do not copy credentials or edit Hermes
source. The final 1.8.21 procurement canary passed on 2026-08-31: explicit
`/steer` and ordinary redirect each sealed the old private carrier, displayed
the ACK, and completed in one new carrier without a duplicate final. A real
group @mention correction reached the same Codex task and produced separate
ordinary group replies; group transport was not changed to C2C streaming.
The [acceptance record](evidence/qq-steer-1.8.21/README.md) separates these
passes from the first failed explicit-steer attempt and timing-invalid runs.
The 27 steering checks, full plugin/installer matrix and legacy fail-closed
checks passed. Default/product processes and plugin hashes, and procurement
config/.env hashes, remained unchanged. This change is submitted for review;
the canary result is not approval to deploy default/product or merge it.

## 2026-08-31 — Require delta provenance before final ownership

PR #4's review reproduced an independent `FINAL` swallowed after streamed
`status NOTFINAL`. Version 1.8.19's turn-final context proved callback identity,
but did not prove that the final had actually been streamed. The real consumer
negative failed before the fix with `status NOTFINAL` instead of
`status NOTFINAL\nFINAL`.

Version 1.8.20 saves the think-filtered unfinished delta segment and full ledger
before Hermes replaces its accumulator in `finish()`. Completed commentary and
tool boundaries clear that candidate. Adapter/chat/anchor identity plus exact
segment equality (or a final extending it) now authorizes ledger reuse only if
the entire acknowledged QQ prefix is preserved. No wire fields or settings
changed. Additional red-to-green cases cover a same-tick final tail and a
post-stream footer; boundary checks cover tool breaks, filtered/chunked deltas,
rewritten finals and final-only streaming. Generic direct-send ownership is
unchanged.

Install the persistent plugin with existing native-C2C settings; first run the
full QQ, Codex, snapshot, installer and static/compatibility matrices. Then
canary only the approved profile using real QQ. Restore the exact external
installer backup and restart only that profile to roll back. Never modify the
Hermes installation or other profiles as part of this fix.

The [review evidence bundle](evidence/pr-4/README.md) now includes the original
1.8.19 long-run Gateway interval, complete native QQ carrier AX captures,
carrier lifecycle table and an independently reproducible content verifier.
It distinguishes historical evidence from the required new 1.8.20 client
retest; it contains no credentials or real chat/session/bot identifiers.
The 1.8.20 procurement retest passed on 2026-08-31: the suffix turn completed
in 18.2 seconds in one sealed native bubble; the 62.6-second overflow turn
delivered its 10,288-character final through 4,000 / 4,000 / 2,305-character
carriers including 17 progress characters. All carriers sealed, both final
markers appeared once, and ordinary finals were suppressed. Complete AX text
and lifecycle excerpts are attached, with automated exact-content verification.
The full unit/plugin/installer/static matrices and actual-runtime streaming
matrix passed. Only procurement was updated; default/product stayed unchanged.

## 2026-08-28 — Close QQ C2C lifetime acceptance gaps

### Problem

Version 1.8.18 handled QQ's observed explicit carrier-lifetime response, but a
lost transport response could still leave an accepted index locally retryable.
The 480-second rollover also ran only when a new draft arrived, and ordinary
non-terminal failures allowed every following delta to retry immediately.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.19.
- Retained one ambiguous submitted frame and added exactly one reconciliation.
  A stale-index response promotes the first request as accepted; an unresolved
  reconciliation retires the carrier while preserving the latest unconfirmed
  cumulative body for lossless final fallback.
- Added an independent, cancellable 480-second expiry task. A silent carrier is
  sealed and reset for a fresh index-0 carrier, or deliberately retired if the
  deadline seal cannot complete.
- Added per-carrier `0.2/0.8/2.0/5.0` cooldowns for non-terminal failures and
  retained only the newest cumulative body during each cooldown.
- Classified QQ `40034128` / `回复消息失败，被动回复时间或者次数超过限制`
  as terminal for the inbound anchor. A replacement carrier rejected for that
  reason is retired immediately, and later draft callbacks make no QQ requests
  instead of retrying a permanently exhausted passive-reply budget. Final
  delivery returns an explicit failure without attempting the equally invalid
  ordinary reply, preserving Gateway recovery rather than claiming success.
- Added a task-local trusted turn-final context around the real
  `GatewayStreamConsumer._send_or_edit()` finalization call. When Codex already
  streamed the exact authoritative final directly after commentary without a
  whitespace boundary, the QQ adapter seals the existing suffix instead of
  appending the whole final again. Generic/direct sends retain the strict
  token-boundary negative.
- Replaced the positional completed-commentary context tuple with a frozen
  dataclass and removed the unread retirement-reason field.
- Added public adapter regressions for ambiguous draft/open/seal responses,
  silent expiry, timer cancellation, cooldown coalescing, and final delivery
  during cooldown, plus zero-request draft/final behavior after
  passive-reply-budget exhaustion following rollover. Added a combined real
  consumer regression for age rollover, no-boundary commentary/final, and a
  final larger than 9,000 characters, proving one exact owner across four
  carriers with every frame at most 4,000 characters.

### Verify and roll back

Run the complete QQ/plugin/installer/static and Hermes compatibility matrices.
Then run a real QQ private turn longer than 12 minutes with more than 9,000
final characters and an observed WebSocket reconnect. Require one completion
marker, no visible prefix or final duplicate, every message at most 4,000
characters, no stale-index storm, and every native carrier sealed or retired.
Keep the turn inside QQ's inbound passive-reply window: a preliminary 34-minute
canary correctly exposed the terminal budget response but could not deliver its
final and therefore did not pass acceptance.
A second 15-minute canary passed the independent age rollover and natural
WebSocket reconnect, then exposed a no-boundary final ownership bug: the UI
showed the complete marker once and immediately began the final body again,
driving committed content past 12,000 characters and exhausting QQ's carrier
budget. That run also did not pass; its shape is now a deterministic real
consumer regression and must be repeated after deployment.
The post-fix procurement canary passed on 2026-08-29: the real QQ turn ran
872.5 seconds, sealed the silent first carrier at 480.5 seconds, continued
after a natural `4009` WebSocket timeout/reconnect, and delivered a 10,234
character final through visible carrier lengths 4,000, 4,000, and 2,318 after
the 78-character progress carrier. The full QQ accessibility tree contained
one response `FINAL_BEGIN` and one response `FINAL_OK` (the only other
occurrence of each was the user's test prompt). Gateway logs confirmed final
suppression and contained no stale-index, passive-reply-budget, ordinary
fallback, or duplicate-final error during the acceptance interval.
Restore the exact external installer backup and restart only the affected
profile to roll back.

## 2026-08-28 — Bind completed commentary to its native consumer

### Problem

The 1.8.17 procurement canary exposed a duplicate-carrier boundary not covered
by the existing single-commentary regression. Consecutive Codex commentary
items were concatenated as `STARTSTEP1` in one native stream, then Hermes sent
the completed `STEP1` item through ordinary `_interim_send`. The conservative
token-boundary predicate treated that word-internal suffix as independent, so
each minute produced both a growing native bubble and a new ordinary bubble.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.18.
- Wrapped the original `GatewayStreamConsumer._send_commentary()` only for an
  active QQ C2C lane and exposed its exact adapter/chat/anchor/text identity in
  a task-local context while the original callback executes.
- Allowed boundaryless suffix ownership only under that trusted context and
  only after the same native carrier visibly ends with the exact cleaned item.
  General interim sends and word-internal overlap negatives remain unchanged.
- Added a real-consumer regression with two consecutive alphanumeric commentary
  items that reproduced the visible duplicate before the fix.

### Verify and roll back

Run the complete QQ and plugin matrices, installer/static checks, then a real QQ
C2C canary with at least two consecutive stage markers. The UI must show one
growing native carrier and no per-stage ordinary bubbles. Restore the exact
installer backup and restart only the affected profile to roll back.

## 2026-08-28 — Retire expired QQ C2C carriers

### Problem

QQ's C2C streaming contract requires one stable `stream_msg_id` and increasing
indices, but does not publish the native carrier lifetime. A production turn
crossed the platform lifetime after roughly ten minutes. QQ consumed the last
frame while returning `同一流式消息发送超过时间限制`; the plugin kept that index
retryable, so later drafts and final cleanup retried a stale index and seal,
received `请求参数index需要递增`, and could fall back with already-visible text.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.17.
- Added a 480-second monotonic safety rollover independent of message length.
  The acknowledged old body is sealed before a fresh carrier opens at index 0.
- Classified only the observed terminal lifetime response as carrier-terminal.
  Its submitted frame becomes visible ownership and the carrier is retired;
  unrelated transport and API failures keep their prior retry semantics.
- Made draft, final, seal-retry, fallback-recovery and abandonment paths respect
  retired state. They never touch the expired carrier again, deliver only an
  unseen final suffix, and publish the existing bounded per-turn tombstone on
  every successful completion.
- Added public-adapter regressions for age rollover and terminal responses
  during draft continuation, cumulative final, rollover seal and cancellation,
  including late-frame and repeated-final suppression.

### Verify and roll back

Run `test_final_delivery.py` and `test_streaming.py`, then the complete QQ,
plugin, installer and static matrices. The lifetime cases must issue exactly one
terminal request, zero stale index/seal retries, one unseen-suffix fallback at
most, and no repeated final. Restore only an exact external backup created by
`scripts/install-plugins.sh`, restart only the affected profile, and verify QQ
Ready. Disabling QQ streaming restores the upstream ordinary-message path.

## 2026-08-28 — Retain abandon-first ownership through claim drain

### Problem

Version 1.8.15 coordinated abandonment and final delivery on one anchor, but a
successful abandon recorded completion only in the independently bounded
per-chat tombstone. Another anchor could evict that record before an already
registered same-key final waiter acquired the claim, causing the waiter to send
the terminal suffix again. Terminal matching also inspected only the character
before the suffix, so a payload that carried its own leading whitespace or
punctuation, such as `\nFINAL`, `,FINAL`, or `，FINAL`, was misclassified as
unowned.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.16.
- Added claim-scoped transient completion context. Successful abandon-first
  native completion is retained until every same-key user already registered
  on the bounded claim drains; queued final and draft callbacks recheck it even
  if the per-chat tombstone was evicted.
- Kept that context out of the long-lived replay LRU. A partial abandonment
  does not become anchor-wide replay: final suppression still requires exact or
  strict terminal ownership, while a different final follows normal delivery.
- Applied the existing boundary predicate to the payload's first character as
  well as the character before it: whitespace and Unicode punctuation other
  than connector punctuation are valid. `_FINAL`, partial overlap and
  word-internal suffixes remain negative.
- Extended the public adapter regression with forced same-chat tombstone
  eviction, a registered final waiter, a registered changed-draft callback,
  claim-drain cleanup, and a leading-boundary table for newline, ASCII/Chinese
  comma, connector punctuation, and word-internal negatives.

### Verify and roll back

Run `test_final_delivery.py` and `test_streaming.py`, then the complete
plugin/MCP, Hermes 0.20.0 fail-closed, installer and static matrices. The
abandon-first case must show one native sealed owner, zero ordinary finals, no
late carrier, and zero active/transient claim state after all callers exit. The
leading-boundary finals must add no ordinary message, while connector and
word-internal negatives must still deliver normally. Restore only an exact
external backup created by `scripts/install-plugins.sh`, restart only the affected profile, and
verify QQ Ready before live use.

## 2026-08-28 — Coordinate final cleanup and stable anchor completion

### Problem

The 1.8.14 broker serialized concurrent final callbacks, but Hermes cancellation
cleanup still ran outside the per-anchor flight. An abandon could seal the full
native final while a shielded ordinary unseen-suffix request was pending, then
that request could succeed and duplicate the suffix. Completed late-draft
suppression also required the old draft id, and missing reply identity collapsed
unrelated finals into the shared `(chat_id, "")` replay key. A late frame could
also enter while the ordinary final request was active but before completion
evidence was published; in the reverse order, abandonment could seal the full
cumulative final before the later short final callback sent that suffix again.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.15.
- Added broker cleanup coordination. Stable-anchor abandonment waits for the
  active final attempt to settle, then resolves and closes the resulting state
  while holding the same keyed transaction.
- Coordinated every stable-anchor draft callback on that transaction as well.
  Same- and changed-draft late frames wait through external final delivery and
  then re-resolve completion; different anchors remain independent. A fallback
  anchor derived from `_last_msg_id` is frozen before the wait to prevent a
  newer inbound message from redirecting the queued operation to another turn.
- Recognized a later short final as already owned by an abandoned cumulative
  seal only when it is an exact token-bounded terminal suffix. Partial overlap
  and word-internal matches remain ordinary final deliveries.
- Reused the bounded completed-result LRU as anchor-scoped late-draft evidence,
  so a fully sealed anchor cannot reopen under a changed Hermes draft id.
- Bypassed single-flight and completed replay for finals without a stable
  inbound reply anchor; sequential and parallel unanchored finals stay
  independent rather than sharing an empty key.
- Added public adapter regressions for both gated final/abandon orderings,
  same- and changed-draft callbacks during an active final flight,
  changed-draft late frames after full native completion, and unanchored final
  independence. Broker contract coverage now includes cleanup waiting for an
  active delivery and the anchor-scoped completed-result lookup.

### Verify and roll back

Run `test_final_delivery.py` and `test_streaming.py` before the full plugin/MCP,
Hermes 0.20.0 fail-closed, installer and static matrices. The abandon race must
show no native seal while the ordinary suffix is blocked and exactly one final
owner after release; an abandon-first full cumulative seal must own a later
token-bounded short final; same- and changed-draft frames must add no QQ API
call while final delivery is active; a changed-draft post-completion frame must
also add no QQ API call; two different unanchored finals must both reach the
ordinary sender. Roll back only
from the exact external backup created by `scripts/install-plugins.sh`, then
restart and verify only the affected profile.

## 2026-08-28 — Bound and unify concurrent final ownership

### Problem

The 1.8.13 claim covered cancellation and final-only-pending sends but left an
active stream's ordinary unseen-suffix fallback outside that transaction.
Concurrent final callbacks could therefore emit the same suffix twice. Waiting
callers also recovered success from the independently evictable completed-owner
tombstone instead of the claim itself, and distinct blocked anchors could grow
the claim registry without a numerical bound.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.14.
- Replaced path-specific claims with a 128-key bounded single-flight broker.
  All C2C `notify=True` final paths now run lifecycle lookup, external delivery,
  ownership promotion and cleanup in one per-anchor transaction.
- Retained the first successful result on the flight until every caller already
  registered on it exits. Tombstone eviction cannot make those waiters resend.
- Made the external attempt flight-owned and shielded it from one caller's
  cancellation. Same-key callers join the in-flight result; a definite failed
  result or exception can hand off to one fresh attempt.
- Added a separate 1024-key LRU for successfully closed or fully delivered
  post-flight replay. It does not consume active admission and prevents a later
  duplicate if a sole cancelled caller's shielded QQ request completes after
  that caller exits. A visible-but-unsealed `qq_stream_close_pending` outcome is
  shared with current waiters but excluded from this LRU, so a later callback
  retries the seal without repeating an ordinary-owned suffix. Late draft
  frames on the same inbound reply anchor cannot extend a recorded complete
  final or open a second stream while its close is pending, including when the
  stale Hermes draft id changed; different anchors remain independent. When
  `abandon_open_draft()` subsequently closes a retained stream with a recorded
  complete turn-final identity, it refreshes the per-chat completed owner and
  publishes the successful close to the bounded replay LRU, preserving
  deduplication across tombstone eviction. Ordinary partial-draft cancellation
  is not promoted into this anchor-wide cache.
- Added a focused broker contract suite plus active-stream adapter regression.
  It covers same-key fan-in, independent-key parallelism, tombstone-independent
  result sharing, failure/exception/cancellation handoff, queued admission
  cancellation, same-key joining after a full-capacity wakeup, sole-holder
  replay, close-pending retry, completed-cache eviction, capacity backpressure
  and a 200-key stress bound.

### Verify and roll back

Run `test_final_delivery.py` before `test_streaming.py`, then run all QQ,
plugin/MCP, installer, Hermes 0.20.0 fail-closed and static checks. The broker
suite must report an active-flight peak no higher than 128 (or the test's lower
configured bound), return to zero, and expose one external send for same-key
success. The active-stream regression must deliver exactly one ordinary unseen
suffix to three concurrent final callers after a real independent-anchor
tombstone eviction. Restore only an exact
installer-created external backup and restart only the affected profile.

## 2026-08-28 — Serialize concurrent ordinary final ownership

### Problem

The cancellation-tombstone and final-only-pending paths read lifecycle state,
awaited the ordinary QQ sender, and recorded successful delivery afterward.
Two concurrent `notify=True` callbacks could therefore both observe the same
undelivered record and emit the same final in separate QQ messages.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.13.
- Added a reference-counted delivery claim keyed by QQ private chat and inbound
  reply anchor. It is registered before any external send await and removed
  after the last caller exits.
- Rechecked completed, cancelled, and pending lifecycle state inside the claim.
  A successful first sender leaves replay suppression for its waiter; a failed
  sender leaves the source record unchanged so the waiter can retry.
- Added public adapter regressions for concurrent cancellation and pending
  finals with three callers, failure handoff, cancelled-waiter cleanup, claim
  registry removal, cancelled-holder and raised-exception handoff,
  independent-anchor parallel delivery, exactly one successful visible
  delivery per turn, and replay suppression.

### Verify and roll back

Run the complete QQ streaming lifecycle test against Hermes 0.20.5, all five QQ
compatibility tests against Hermes 0.20.0, the offline plugin/MCP matrix,
`scripts/test_install_plugins.sh`, and the static checks. The concurrency cases
must record one successful ordinary message; the failure-handoff case must
record one failed attempt followed by one successful visible message. Confirm
cancelled waiters and holders plus raised send exceptions leave no stale claim,
and different anchors still reach the external send boundary concurrently.
Restore only an exact installer-created external backup and restart only the
affected profile after verification.

## 2026-08-28 — Separate cancellation ownership and preflight complete updates

### Problem

The capacity-abandon path recorded invisible content as completed delivery, so
a later ordinary turn-final with the same anchor and content could be
suppressed even though QQ had never received it. Multi-plugin installation
validated and replaced one target at a time, allowing an invalid later target
to leave an earlier plugin already updated. Native-lane membership also used an
adapter-lifetime set with no chat-count bound.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.12.
- Split cancelled and delivered completed-owner semantics. Cancellation still
  blocks a late draft; a successful later ordinary final promotes the record
  to delivered ownership and only then suppresses final replays.
- Changed native-lane membership to a 1024-chat LRU that evicts inactive chats,
  protects open streams, converges after close, and preserves live config
  disable behavior.
- Split plugin installation into a complete target-preflight pass and a later
  mutation pass. A two-plugin regression compares the first active directory
  recursively and verifies that no backup exists when the second target fails.

### Verify and roll back

Run `plugins/qqbot-connect-hotfix/test_streaming.py` and
`scripts/test_install_plugins.sh`, then run the complete offline plugin/MCP and
Hermes 0.20.0/0.20.5 QQ matrices. Verify capacity abandonment produces one
ordinary final and suppresses only its replay, lane membership stays bounded
without evicting an open stream, and an invalid second install target leaves
the first untouched. Restore only an exact installer-created external backup
and restart only the affected profile after verification.

## 2026-08-28 — Scope active streams and bound chat registries

### Problem

Version 1.8.10 keyed active native streams by draft id alone even though the
public adapter contract only requires draft ids to be unique within one chat.
Two private chats using the same id therefore collided. A capacity-final-only
turn also lost its identity when abandonment removed pending state, allowing a
late callback to re-arm the cancelled turn. Finally, per-chat inner quotas did
not bound the number of retained chat buckets, and a rejected fresh install
could create an empty active plugin directory before detecting an invalid
backup-root symlink.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.11.
- Keyed every active stream and anchor reference by `(chat_id, draft_id)` and
  added a same-id/two-chat public adapter regression.
- Recorded a completed owner before removing a successfully abandoned
  capacity-final-only pending identity.
- Added 1024-chat least-recently-used bounds to the completed-owner and
  final-only-pending registries while preserving each chat's 256-entry quota.
- Moved canonical backup-root validation ahead of active-target creation and
  made the rejected-fresh-install regression explicitly fail on any artifact.

### Verify and roll back

Run `scripts/test_install_plugins.sh`, the complete QQ streaming lifecycle test,
the five QQ regressions against Hermes 0.20.0 and 0.20.5, and the offline
plugin/MCP matrix. Verify same-id private chats produce two independent native
frames, abandoned pending turns produce no late frame, recent chat buckets
retain ownership across LRU eviction, and rejected fresh installs leave no
active directory. Restore only an exact installer-created external backup and
restart only the affected profile after verification.

## 2026-08-28 — Isolate completed owners and harden plugin paths

### Problem

The 1.8.9 completed-owner FIFO was adapter-global. Enough completions in one QQ
private chat could evict another chat's tombstone and allow its repeated final
or late frame to create a second carrier. Tombstones were also written only
after an ordinary suffix fallback, leaving successful native seals,
first-frame/final-only degradation, committed-only rollover completion, and
abandon cleanup without replay protection.

The new pre-install backup mechanism also trusted filesystem names without
fully enforcing canonical boundaries. A symlinked `plugin-backups` root could
place an old manifest below recursive plugin discovery; a symlinked active
plugin could write through to an external directory and retain stale files; and
restore accepted `.` as a manifest/plugin name, allowing `plugins/.` to become
the destructive target.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.10.
- Replaced the global completed-owner FIFO with an independent 256-entry quota
  for each QQ private chat. More-than-capacity traffic in chat B no longer
  consumes chat A's quota; each chat still evicts its own oldest identities.
- Added completed owners before every successful managed completion leaves its
  active/pending state. Capacity-triggered final-only turns retain a bounded
  per-chat pending draft identity until the ordinary final succeeds.
- Added public lifecycle regressions for ordinary fallback, all-native and
  rollover seals, first-frame/final-only and capacity-final-only paths,
  committed-only rollover heads, abandon completion, late frames, repeated
  finals, same-chat eviction, and cross-chat isolation.
- Hardened install and restore with explicit `.`/`..` rejection, symlink checks
  for backup roots and active targets, canonical backup containment, and an
  exact canonical direct-child requirement for active plugin directories.
- Expanded the shell regression to prove normal install/restore remains
  recoverable while every unsafe path is rejected before active data changes.

### Verify and roll back

Run `scripts/test_install_plugins.sh`, the five QQ regressions against Hermes
0.20.0 and 0.20.5, and the complete offline plugin/MCP matrix. An unsafe path
must exit non-zero without changing the active plugin. Roll back only from the
exact external backup path printed by the installer; never move that backup
under `plugins` or replace a rejected directory with a symlink workaround.

## 2026-08-28 — Preserve QQ final ownership after recovery close

### Problem

After a native QQ C2C update failed, the ordinary-message fallback could
successfully deliver the unseen final suffix and the immediate native-prefix
recovery seal could then succeed. The seal removed the active stream state, so
a repeated Hermes final callback sent the final again and a late draft frame
could open a new stream for the already-completed turn. The terminal ownership
check also used a handwritten boundary list that omitted common Unicode
punctuation such as Chinese and ASCII commas and em dashes. Finally, the plugin
README named 1.8.7 as a rollback target although Git contains no recoverable
1.8.7 artifact and the installer replaced active directories without making an
external plugin backup.

### Change

- Bumped `qqbot-connect-hotfix` to 1.8.9.
- Added a 256-entry completed-turn ownership map keyed by QQ private chat,
  inbound reply anchor, and Hermes draft id. It survives successful active-map
  removal, suppresses stale final/draft replays, and keeps another inbound
  anchor isolated as a new turn.
- Replaced the punctuation whitelist with Unicode punctuation-category
  detection while retaining whitespace boundaries and word-internal negative
  cases.
- Made `scripts/install-plugins.sh` copy an existing plugin, including hidden
  files, into the profile-level `plugin-backups` directory before replacement.
  Added a guarded `--restore` mode that verifies the manifest, rejects backups
  under plugin discovery, and preserves the active copy before rollback.
- Added public adapter lifecycle regressions for successful close, repeated
  final, late frame, anchor isolation, bounded eviction, and punctuation, plus
  an isolated shell regression for install/restore safety.

### Verify and roll back

Run `scripts/test_install_plugins.sh` and the complete plugin regression matrix
documented in the deployment guide before touching a running profile. During an
update, record the installer's printed `plugin-backups` path. To roll back, run
`scripts/install-plugins.sh --restore <profile-home> qqbot-connect-hotfix
<exact-backup-path>`, verify `hermes plugins list`, restart only that profile's
Gateway, and confirm QQ reaches `Ready`. The restore source must remain outside
the `plugins` directory.

## 2026-08-25 — Release idle Codex thread writers on Agent cache eviction

### Problem

Hermes 0.20.5 hard Agent teardown closes `_codex_session`, but its soft
`release_clients()` path does not. After a slash command such as `/reload-mcp`
updates the durable transcript, Gateway detects a cross-process message-count
change, soft-evicts the cached Agent and immediately rebuilds it. The abandoned
Codex app-server remains alive as the thread's writer, so the replacement
Agent's `thread/resume` fails with JSON-RPC `-32600` and `already has an active
writer` on every later message.

### Change

- Bumped `codex-app-server-phase-hotfix` to 1.8.3.
- Wrapped `AIAgent.release_clients()` to close and clear an idle
  `_codex_session`, while preserving a session whose turn is still active.
- Added a process-local weak thread-owner registry. Before `thread/resume`, the
  plugin synchronously closes only a known idle Hermes owner left in the same
  Gateway process. External writers remain untouched and retain Codex's native
  single-writer protection.
- Added regression coverage for idle cleanup, active-turn preservation and
  stale-owner retirement before resume.

### Verify and roll back

Run `python plugins/codex-app-server-phase-hotfix/test_hotfix.py`. In a mapped
QQ session, complete one Codex turn, run `/reload-mcp`, then send another
message. The stored thread id must resume without `active writer`, and only one
app-server child may own that thread. Disable the plugin and restart Gateway to
roll back; this restores upstream lifecycle behavior without modifying Hermes'
installation directory or the persisted session/thread mapping.

## 2026-08-25 — Cross-platform Codex Desktop project registration

### Problem

Codex app-server correctly stored Hermes threads with the session project's
`cwd`, but Codex Desktop did not automatically add that directory to its
sidebar. The app maintains a separate local-project registry, so cwd/thread
creation alone was insufficient. Editing the private Desktop global-state JSON
would be version-fragile and would bypass the running app's window updates.

### Change

- Bumped `codex-app-server-phase-hotfix` to 1.8.2.
- Added opt-in `HERMES_CODEX_APP_REGISTER_PROJECTS=true`. After the first
  successful thread start/resume for a project, the plugin schedules the
  supported Codex CLI entrypoint `codex app <project-path>` outside the Agent
  turn and records success in the existing mapping SQLite database.
- Linux and Windows invoke that CLI entrypoint directly. On macOS, a detached
  Gateway may not inherit the logged-in Aqua bootstrap namespace, so the
  worker uses `launchctl asuser <uid> codex app <project-path>`. The plugin
  never edits Codex Desktop's private state file.
- Registration is best-effort and cannot fail or delay the Agent turn. Child
  stdio is detached so a Desktop process cannot keep Gateway pipes open. A
  successful path is not launched again after later turns or Gateway restarts;
  failures retry only after a cooldown.
- Added startup backfill from Hermes' authoritative `sessions/sessions.json`.
  Existing channel routes receive missing project scaffolds/mappings without
  waiting for `/new` or a management command. Their next real message creates
  the named thread lazily, avoiding empty threads and startup/inbound races.
- Windows replaces the filesystem-forbidden ASCII `:` with the compatible
  full-width `：` only in the physical directory basename. The database,
  manifest and logical project name retain the exact Hermes `session_key`.
- Headless/container deployments keep registration disabled. Their host
  desktop user can run `codex app <host-project-path>` explicitly.

### Verify and roll back

Enable registration on a desktop host, restart Gateway, and send one channel
message that reaches Codex. `/codex-project status` must show
`codex_app_registration.status: registered`, and the project must appear in
Codex Desktop. Disable `HERMES_CODEX_APP_REGISTER_PROJECTS` to stop future
registration; already registered Desktop projects and mapping rows are
retained for audit and can be removed separately through the Desktop UI.

The macOS live regression backfilled five missing routes, retained two existing
routes, and skipped none. A QQ group turn resumed its stored thread, registered
the project through the detached worker, and returned exactly one final reply.
The deployment guide now keeps the unused WhatsApp channel and plugin disabled
by default and documents that long-turn/project settings belong to Hermes
`config.yaml`/`.env`, not private keys in Codex `config.toml`.

## 2026-08-25 — Codex project name equals Hermes session key

### Problem

The initial mapping correctly grouped multiple Hermes `session_id` values under
one project per stable `session_key`, but named the physical project folder
after the first session id. That made the Codex project label describe only the
first thread instead of the durable channel route that owns every thread.

### Change

- Bumped `codex-app-server-phase-hotfix` to 1.7.0.
- New default project folders use the exact `session_key` as their basename;
  each `session_id` remains only the Codex thread name.
- Added an atomic, ownership-checked migration for 1.6.x folders. It renames
  the directory and updates `channel_projects`, `session_threads`,
  `thread_history`, and `.hermes-dispatch.json` without changing thread ids or
  shared project memory.
- Explicit user bindings remain unchanged; `/codex-project default` returns to
  the session-key-named default project.
- Unsafe or overlong session keys fail explicitly instead of being silently
  converted into a different visible project name.

### Verify and roll back

Run `test_hotfix.py`, then verify `/codex-project status` reports the complete
session key as `project_name`, while `/new` changes only `session_id` and later
creates a new thread under the same `project_path`. To roll back the code,
disable the plugin and restart Gateway; the migrated directory is retained and
can be renamed manually using the SQLite mapping if an older plugin must be
restored.

## 2026-08-25 — Channel-neutral `/codex-project` command context

### Problem

Hermes 0.20.0 clears session ContextVars when an inbound Gateway task starts,
but dispatches plugin slash commands before the normal Agent path binds the
current `session_key` and `session_id`. The 1.6.0 `/codex-project` handler
therefore returned `no active Hermes channel session` even though the QQ or
WhatsApp chat already had a valid routing entry. Version 1.6.1 captured that
route, but still preferred Hermes' process-global fallback values; immediately
after `/new`, `status` could therefore report the old session and thread.

### Change

- Bumped `codex-app-server-phase-hotfix` to 1.6.2.
- Added a read-only `pre_gateway_dispatch` capture using Hermes' own
  `_session_key_for_source` and routing entry; no QQ- or WhatsApp-specific ID
  format is duplicated.
- If `/codex-project` is the first message in a channel route, the async command
  handler creates the session through Hermes' normal `async_session_store` only
  after the Gateway authorization checks have passed.
- Normal prompt-callable tool invocations continue using Hermes' official
  task-local session ContextVars, preserving concurrent channel isolation.
- The captured inbound route is authoritative for plugin slash commands;
  process-global fallback values left by an Agent before `/new` cannot replace
  the newly rotated Gateway `session_id`.
- Added regression coverage proving independent QQ and WhatsApp command routes.

### Verify and roll back

Run `test_hotfix.py`, then invoke `/codex-project status` in both an authorized
QQ chat and an authorized WhatsApp chat. Disable the plugin and restart the
Gateway to roll back; the mapping database and generated projects are retained.

## 2026-08-25 — Hermes channel session to Codex project/thread continuity

### Problem

Hermes 0.20.0 derives a stable `session_key` from a QQ private/group route and
stores the current durable conversation in a separate `session_id`. `/new` and
`/reset` preserve the routing key while rotating the session id. The Codex
app-server adapter, however, stores its thread id only on the process-local
cached `AIAgent`; rebuilding that object always calls `thread/start`. One QQ
chat therefore accumulated unrelated Codex threads and cwd/project groupings.

### Change

- Bumped `codex-app-server-phase-hotfix` to 1.6.0.
- Added a WAL SQLite mapping at
  `$HERMES_HOME/state/codex-session-projects.sqlite3`.
- The first `session_id` names a default project below
  `$HERMES_HOME/codex-projects`; every later session id under the same
  `session_key` creates a separately named thread in that project.
- An unchanged session id uses Codex `thread/resume` after Gateway restart,
  Agent-cache eviction or app-server retirement. A missing/deleted stored
  thread is archived in mapping history before one replacement is started.
- Default projects receive non-destructive `AGENTS.md` and
  `PROJECT_MEMORY.md` scaffolding; existing files are never overwritten.
- Added the `codex_session_project` tool and `/codex-project` command. Explicit
  project changes are admin-gated and limited to configured aliases/roots.
  A changed cwd is applied on the next turn by resuming the current thread;
  only the current Hermes session's app-server client is retired.

The implementation uses the locally generated Codex 0.145.0 app-server schema:
`thread/resume` accepts `threadId` and `cwd`, `thread/name/set` assigns the
Hermes session id, and `thread/list` groups by exact cwd. An unsupported or
transient resume error is surfaced rather than silently starting duplicates;
only an explicit missing-thread error permits replacement.

### Enable and verify

```dotenv
HERMES_CODEX_SESSION_PROJECTS_ENABLED=true
HERMES_CODEX_PROJECT_ADMIN_USERS=<authorized-platform-user-id>
HERMES_CODEX_PROJECT_ALIASES={"finance":"/absolute/path/to/finance"}
```

```bash
scripts/install-plugins.sh "$HOME/.hermes" codex-app-server-phase-hotfix
hermes plugins enable codex-app-server-phase-hotfix --no-allow-tool-override
hermes tools enable --platform qqbot codex_session_project
hermes gateway restart

"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/codex-app-server-phase-hotfix/test_hotfix.py
```

Verify one QQ chat across a Gateway restart (same thread), then `/new` (same
project, new session-id-named thread). If aliases are configured, verify an
authorized bind preserves thread history on the next turn and another QQ user
cannot change the mapping.

### Rollback

Disable `codex-app-server-phase-hotfix` and restart Gateway. This restores
Hermes 0.20.0's start-only Codex behavior. Mapping SQLite, generated project
directories and project memory files are deliberately retained; remove them
only as a separate, explicit data-destruction operation.

## 2026-08-19 — Codex app-server long-turn deadline compatibility

### Problem

Hermes 0.20.0 calls `CodexAppServerSession.run_turn()` without overriding its
fixed 600-second wall deadline. A live QQ C2C test reached that deadline while
a 1,900-second foreground command was healthy. Because Codex had already sent
commentary, upstream Hermes promoted the latest assistant message to terminal
output without receiving `turn/completed`; the tool process continued after
the Gateway released the turn.

The Gateway also has a separate 1,800-second inactivity watchdog. It is not a
wall clock—tool/API/stream activity resets it—but a deliberately silent
foreground command longer than 30 minutes can still reach it.

### Change

- Bumped `codex-app-server-phase-hotfix` to 1.5.0.
- Added `HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS`; `0` (the default)
  disables only the Codex turn wall deadline, while a positive value restores
  a finite limit.
- A finite deadline without `turn/completed` now interrupts and retires the
  session rather than delivering commentary as a successful final answer.
- The current long-turn test profile uses `agent.gateway_timeout=7200` so the
  independent Gateway inactivity watchdog does not preempt the 30-minute run.
  A live deployment check also exposed the independent
  `agent.restart_drain_timeout=0` default, which forces an explicit restart
  without draining other sessions; the test host now uses 300 seconds.
- Completion flags, deadlines and callbacks remain local to each
  `CodexAppServerSession`. A concurrent two-session regression verifies that
  final events, timeout values and results do not cross sessions.

The change does not disable `/stop`, `turn/steer`, incoming-message interrupt,
subprocess-exit detection, or the existing post-tool completion watchdog. One
fixed chat is still serialized by Hermes and follows `display.busy_input_mode`;
only distinct session keys run independently.

### Enable and verify

```bash
scripts/install-plugins.sh "$HOME/.hermes" codex-app-server-phase-hotfix
hermes plugins enable codex-app-server-phase-hotfix --no-allow-tool-override
hermes config set agent.gateway_timeout 7200
hermes config set agent.restart_drain_timeout 300
hermes gateway restart

"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/codex-app-server-phase-hotfix/test_hotfix.py
```

The live acceptance test is one QQ private-chat turn containing a foreground
wait longer than 30 minutes followed by a unique final marker. Success requires
the marker persisted after `turn/completed` and a successful QQ send path, no 600-second
deadline warning, no interim text treated as final, no orphan child process,
and an independent second-session probe that does not alter the long turn.

The 2026-08-19 live QQ C2C acceptance passed. The effective turn began at
11:01:31, started `python3 -c 'import time; time.sleep(1900)'` at approximately
11:01:46, and returned at 11:33:27. Gateway recorded 1,916.5 seconds end to end
and persisted the exact final text `LONG_TASK_30M_QQ_V2_FINAL_OK`. Checkpoints
at 5, 10, 15, 20, 25 and 30 minutes all showed the same tool PID and parent
Codex app-server, one active private-session agent, no final delivery, and no
deadline/idle/session-retirement/stale-result event. In particular, the old
600-second `accepting the assistant text as the terminal response` warning did
not recur.

At 15 minutes, Gateway sent its expected non-conversational inactivity warning:
the 7,200-second outer watchdog still had 105 minutes remaining. That warning
did not enter the final-delivery ledger or release the turn. At 11:27:19 QQ
requested a WebSocket reconnect with code 4009; the adapter reconnected and
resumed the session in about 2.4 seconds. The final QQ send path ran at
11:33:27.918 with no send/fallback/retry error. QQ did not reject the original
reply anchor, so the expired-anchor standalone fallback remained unexercised in
this live run.

During the private turn, a group mention entered distinct Hermes session
`20260812_105439_64454baf` and distinct Codex thread `01a017fb`. It returned
`ISOLATION_OK` in 11.0 seconds while the private `sleep(1900)` continued under
its original PID/parent. No interrupt or stale result was emitted. After the
private final, `active_agents` returned to zero and no `sleep(1900)` process
remained; the surviving app-server children were its normal MCP, Computer Use,
live-server and code-mode hosts.

### Rollback

Disable `codex-app-server-phase-hotfix`, unset
`HERMES_CODEX_APP_SERVER_TURN_TIMEOUT_SECONDS`, restore a positive
`agent.gateway_timeout`, and restart the Gateway. This restores Hermes 0.20.0's
fixed Codex 600-second deadline together with its upstream terminal behavior.

## 2026-08-19 — QQ expired reply-anchor fallback

### Problem

QQ associates a passive reply with the inbound message's `msg_id`. During a
long agent turn that reply anchor can expire before the final text is sent, so
QQ rejects the referenced send even though the Hermes task produced a result.
Upstream Hermes PR
[#85221](https://github.com/NousResearch/hermes-agent/pull/85221) uses a narrow
delivery fallback: retry once without the expired reply relationship.

### Change

- Bumped `qqbot-connect-hotfix` to 1.7.0.
- C2C text, group text, approval keyboards, and guild text keep their reply
  anchor on the first attempt.
- Only an explicit expired `msg_id`/`message_id` error triggers one standalone
  retry. Approval keyboard data is passed through unchanged.
- Detection includes QQ's Chinese error wording and conservative English field
  aliases. Unrelated send failures are returned unchanged, and a failed retry
  retains both the original and fallback diagnostics.

This first-stage workaround intentionally does not change Codex app-server's
wall-clock limit, add persistent task probing, or retry media. Those require a
separate task-lifecycle design rather than a channel-send compatibility patch.

### Enable and verify

Install the persistent plugin, restart the Gateway, and run the QQ regressions:

```bash
scripts/install-plugins.sh "$HOME/.hermes" qqbot-connect-hotfix
hermes plugins enable qqbot-connect-hotfix --no-allow-tool-override
hermes gateway restart

HERMES_PY="$HOME/.hermes/hermes-agent/venv/bin/python"
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_expired_reply.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_hotfix.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_media_reply.py
"$HERMES_PY" plugins/qqbot-connect-hotfix/test_group_roundtrip.py
```

The focused regression verifies C2C, group, guild, and keyboard retries,
idempotent patching, conservative error matching, and two-error diagnostics.
For a live check, inspect `~/.hermes/logs/agent.log` for
`reply anchor expired`; the following standalone send must succeed.

The 2026-08-19 macOS C2C live check used one real QQ inbound message at
10:22:15 and held the same Codex app-server turn open with a 370-second
foreground command. Hermes produced the fixed final text at 10:28:50 after
394.9 seconds, and the delivery ledger recorded `delivered`, zero retries, and
no error. QQ accepted the original reply anchor, so this proves that the final
result survived a greater-than-five-minute turn on that private channel but
does **not** prove that the live expired-anchor fallback branch was exercised.
That branch remains covered by the focused adapter regression until QQ returns
an explicit expired `msg_id` in production.

A second live test attempted a 1,900-second foreground command in the same QQ
turn. The inbound message arrived at 10:31:58, but at 10:42:04 the installed
Codex app-server transport hit its hard-coded 600-second turn deadline. Hermes
reported the turn ready after 605.9 seconds and delivered the last interim
assistant text, `仍在等待前台命令完成。`, as though it were the final response.
The requested `LONG_TASK_30M_QQ_FINAL_OK` result was never produced. The
foreground tool process also remained alive after Gateway released the active
turn and required explicit cleanup. This confirms that the QQ expired-anchor
fallback alone cannot support 30-minute tasks: the app-server deadline,
interim-as-terminal behavior, and orphaned tool cleanup must be addressed
before a longer end-to-end retry is meaningful.

### Rollback

Disable `qqbot-connect-hotfix` and restart the Gateway to restore upstream send
behavior. This removes all QQ compatibility patches, not just the new retry. To
roll back only this change, reinstall plugin version 1.6.1 from repository
history and restart. No message database or task state is migrated.

## 2026-08-04 — WhatsApp mention routing and durable Baileys snapshots

### Problem

Hermes 0.20 had stored the intended WhatsApp mention policy at
`display.platforms.whatsapp.require_mention`. The WhatsApp adapter only reads
`platforms.whatsapp.require_mention` or `WHATSAPP_REQUIRE_MENTION`, so the live
value silently remained false and every group message triggered the agent.

The existing `message-snapshot-store` captured only QQ. WhatsApp text and media
could reach the agent and ordinary Hermes transcript/cache, but they were not
written to the permanent structured database, included in BM25/hybrid recall,
or recoverable through the content-addressed archive.

### Change

- Bumped `whatsapp-bridge-policy-hotfix` to 0.2.2. It treats the misplaced
  display value as a compatibility fallback only when the canonical adapter
  config and environment variable are absent. Deployments should still migrate
  to `platforms.whatsapp.require_mention: true`.
- Bumped `message-snapshot-store` to 1.1.0 and wrapped Hermes 0.20's deferred
  WhatsApp factory.
- Baileys bridge events are captured before Hermes applies its mention gate, so
  passive group traffic is retained without triggering Codex.
- Routed WhatsApp messages receive the same bounded 20-message/4000-token
  database context and exact/FTS5-BM25/substring/fuzzy/RRF retrieval as QQ.
- WhatsApp's decrypted media cache paths are mirrored immediately into the
  SHA-256 content-addressed archive. Hashing and copying are streamed in 1 MiB
  chunks to avoid reading large media into Python memory a second time.
- If Baileys cannot download/decrypt media even after its reupload recovery,
  the snapshot records an unavailable attachment marker rather than claiming
  that a restorable URL exists.

This compensates for Hermes 0.20's configuration placement mismatch and for the
Baileys bridge boundary: `messages.upsert` exposes WhatsApp Web message objects,
while `downloadMediaMessage()` yields decrypted bytes, not a durable plaintext
link suitable for QQ-style link-only retention.

### Enable and verify

Install the persistent plugin directories, enable both plugins, set the
canonical mention policy, and restart the Gateway:

```bash
scripts/install-plugins.sh "$HOME/.hermes" \
  whatsapp-bridge-policy-hotfix message-snapshot-store
hermes plugins enable whatsapp-bridge-policy-hotfix
hermes plugins enable message-snapshot-store
hermes gateway restart
```

Run:

```bash
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/whatsapp-bridge-policy-hotfix/test_hotfix.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/message-snapshot-store/test_store.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/message-snapshot-store/test_capture.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/message-snapshot-store/test_materialize.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/message-snapshot-store/test_quoted_attachment.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/message-snapshot-store/test_whatsapp_capture.py
```

Live verification requires two group messages after restart: an ordinary
unmentioned message must create a WhatsApp snapshot without an agent turn; a
later mention must trigger one turn whose durable context includes the passive
message. A media test must report `archive_status=archived` and remain
restorable after the Baileys cache file is unavailable.

### Rollback

Disable `message-snapshot-store` to stop new snapshots and retrieval tools; the
existing database/archive remains untouched. Disable
`whatsapp-bridge-policy-hotfix` to restore upstream policy handling. Correctly
configured `platforms.whatsapp.require_mention` continues to work without the
fallback. Delete `$HERMES_HOME/message-snapshots` only as a separate intentional
data-destruction action.

## 2026-08-04 — Hermes 0.20 persistent-plugin compatibility verification

### Scope

Upgraded the live macOS Hermes installation from 0.19.0 to 0.20.0 and checked
the repository and installed copies of both persistent compatibility layers:

- `qqbot-connect-hotfix` 1.6.1
- `codex-app-server-phase-hotfix` 1.4.0

Hermes 0.20 retains the QQ adapter approval and reconnect call shapes, and the
Codex app-server request-handler call shape, that these wrappers depend on.
The upgrade also updated CUA Driver from 0.9.0 to 0.17.0.

### Result

No plugin code change or version bump was required. Both regression suites
passed against the repository copies and again against the copies installed in
`~/.hermes/plugins`. After reinstalling the persistent plugins and restarting
the LaunchAgent, the Gateway loaded both patches, connected QQ, sent Identify,
and reached `Ready`. The new startup interval contained no `TypeError`,
`unexpected keyword`, approval fallback, or traceback.

### Enable and upgrade

```bash
hermes gateway stop
hermes update --yes
scripts/install-plugins.sh "$HOME/.hermes" \
  qqbot-connect-hotfix codex-app-server-phase-hotfix
hermes gateway start
```

### Verification

```bash
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/qqbot-connect-hotfix/test_hotfix.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  plugins/codex-app-server-phase-hotfix/test_hotfix.py
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  "$HOME/.hermes/plugins/qqbot-connect-hotfix/test_hotfix.py"
"$HOME/.hermes/hermes-agent/venv/bin/python" \
  "$HOME/.hermes/plugins/codex-app-server-phase-hotfix/test_hotfix.py"
```

Also confirm the current-version banner, the LaunchAgent state, both plugin
startup summaries, and QQ's `Ready` event in `~/.hermes/logs/agent.log`.

### Rollback

Stop the Gateway, restore the pre-upgrade Hermes checkout or backup, reinstall
that release's dependencies, reinstall the persistent plugins, and start the
Gateway. To isolate a plugin regression without downgrading Hermes, disable the
affected plugin in `~/.hermes/config.yaml`, restart, and then re-enable it after
diagnosis. This verification changed no plugin code, so there is no repository
code rollback for the 0.20 compatibility check.

## 2026-07-27 — Hermes 0.19 approval signature compatibility

### Problem

Hermes' newer 0.19-era Gateway passes `allow_session` to the cross-adapter
`send_exec_approval` contract. The persistent QQ shared-group owner wrapper was
written against Hermes 0.18.2 and did not accept that keyword. Live logs showed
the call failing in Python before any QQ API request:

```text
Button-based approval failed, falling back to text:
QQAdapter.send_exec_approval() got an unexpected keyword argument 'allow_session'
```

The Gateway therefore emitted a text-only `/approve` prompt even though the
Codex approval bridge, QQ adapter, and four-scope keyboard patch were all
registered successfully.

### Change

- Bumped `qqbot-connect-hotfix` to 1.6.1.
- The shared-group wrapper now accepts `allow_session` and future keyword
  additions.
- Keyword forwarding is signature-aware: the wrapper preserves
  `allow_session` for current adapters and omits it only when calling an older
  Hermes 0.18.2 adapter that does not implement the parameter.
- Calls to the wrapped adapter use named arguments so later optional-parameter
  insertion cannot silently shift approval semantics.

This compensates for the cross-version adapter contract change while retaining
one persistent plugin build for both Hermes 0.18.2 and 0.19-era installations.

### Verification

Run:

```bash
python plugins/qqbot-connect-hotfix/test_hotfix.py
```

The regression covers both the legacy method signature and the current
`allow_session` signature, including shared-group owner-token routing.
After installing the plugin, restart the Gateway and verify the startup log
still reports `approval sender patched`. A live approval must render buttons
without `unexpected keyword argument 'allow_session'` or the text fallback.

### Rollback

Reinstall `qqbot-connect-hotfix` 1.6.0 and restart the Gateway. On newer
0.19-era Gateway code this intentionally restores the text-only fallback, so
rollback is intended only for diagnosing a regression on an older runtime.

## 2026-07-24 — Codex automatic reviewer and complete QQ approval scopes

### Problem

Hermes 0.18.2 reduced Codex command and file approvals to two QQ buttons because
its app-server callback always passed `allow_permanent=False`. This hid
Codex's supported `acceptForSession` decision. The remaining generic
**始终允许** label was also ambiguous: standalone permission requests support
only turn/session scope, while permanent command approval is valid only when
Codex supplies an exec-policy or network-policy amendment.

The local Codex configuration had no explicit reviewer, so the upstream default
sent every interactive approval to the user instead of using the requested
automatic reviewer.

### Change

- Set the user Codex defaults through app-server `config/batchWrite` to
  `approval_policy="on-request"`, `approvals_reviewer="auto_review"`, and
  `sandbox_mode="workspace-write"`.
- `codex-app-server-phase-hotfix` 1.4.0 now intercepts command and file approval
  responses as well as permission requests, records exact request choices on
  Hermes' existing in-memory queue, returns `acceptForSession` for session
  approval, and returns only the persistent amendment proposed by Codex.
- `qqbot-connect-hotfix` 1.6.0 adds the missing `allow-session` callback and
  renders the choices in two rows. Shared-group opaque owner tokens resolve back
  to the same queue metadata without exposing a real session key.

This compensates for Hermes' incomplete QQ presentation and legacy Codex
response mapping. It does not modify container source, create a second approval
database, or invent permanent allow rules.

### Verification

`codex config/read` returned the three requested defaults and
`codex --strict-config doctor` accepted the configuration. Plugin regressions
cover one-shot/session/persistent/deny response mapping, exact exec/network
amendment round-trips, the real Hermes blocking queue, four-button QQ
serialization, and shared-group owner binding.

For live QQ verification with human buttons, temporarily switch the reviewer to
`user`; automatic review normally resolves eligible requests before a QQ prompt
exists. Restore `auto_review` afterward.

### Rollback

Remove the three Codex keys to restore upstream defaults. Disable
`codex-app-server-phase-hotfix` and `qqbot-connect-hotfix`, reinstall the
persistent plugin set, and restart the Gateway to restore Hermes 0.18.2
behavior.

## 2026-07-20 — Computer Use elicitation and QQ shared-group approvals

### Problem

Codex 0.144.6's bundled Computer Use runtime requests access to an unapproved
macOS app through `mcpServer/elicitation/request`, with extended metadata
identifying `connector_id=computer-use`, the bundle id, display name, risk, and
allowed persistence scopes. Hermes 0.18.2 accepts only `hermes-tools`
elicitations and immediately declines every other server. The resulting tool
output is `Computer Use was not approved to use <app>`, but no approval reaches
QQ.

A second upstream mismatch prevents existing QQ buttons from working in shared
groups. With `group_sessions_per_user: false`, the session key has no user id;
the QQ click validator nevertheless requires the operator to match a user id
parsed from that key. Logs confirmed that the initiating member's own click was
rejected as unauthorized.

The request/response shape was checked against Codex's generated 0.144.6
app-server schema, the bundled `@oai/sky` Computer Use policy implementation,
and the upstream [Codex app-server protocol](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#mcp-elicitation).

### Change

- `codex-app-server-phase-hotfix` 1.3.0 recognizes only bundled Computer Use
  elicitations and bridges them through the existing Gateway queue.
- Allow/deny responses use the current MCP elicitation schema; the persistent
  choice returns `_meta.persist=always` to Codex.
- Third-party elicitations and hard Computer Use safety denials remain
  fail-closed.
- `qqbot-connect-hotfix` 1.5.4 replaces a shared-group approval button's public
  session key with a five-minute, single-use random token bound to the current
  `HERMES_SESSION_USER_ID` and group.
- The token mapping is memory-only, is consumed before queue resolution, and
  cannot approve a later request after a duplicate click.
- QQ `/approve` and `/deny` commands use the same requester record, preventing
  the text fallback from bypassing button ownership.

### Verification

The regression suites cover response mapping, connector filtering, behavioral
upstream detection, actual Hermes approval queue integration, requester/group
matching, unauthorized clicks, and nonce replay:

```bash
python plugins/codex-app-server-phase-hotfix/test_hotfix.py
python plugins/qqbot-connect-hotfix/test_hotfix.py
```

Both versions were copied to the persistent `~/.hermes/plugins` installation
and the launchd-supervised Gateway was restarted. Startup logs confirmed
`Computer Use elicitations patched`, `approval sender patched`, and
`approval dispatcher patched`; QQ reconnected and reached ready state. The two
installed-plugin regression scripts also passed after restart.

End-to-end human acceptance passed on 2026-07-20. The test removed Notes from
the Codex Computer Use allowlist, requested a Notes action from QQ, exercised
the shared-group approval path, approved as the requester, and confirmed that
the interrupted Computer Use call resumed.

### Rollback

Disable both compatibility plugins and restart the Gateway. Codex settings are
not edited by plugin installation; only an explicit **始终允许** response can ask
Codex to persist a Computer Use app policy.

## 2026-07-20 — Codex Gateway approval bridge

### Problem

Codex app-server permission prompts did not reach QQ. Hermes 0.18.2 obtains an
approval callback from its interactive terminal path, but Gateway/cron sessions
have no terminal UI. Command execution and file changes therefore failed closed
without notifying the user. The standalone permissions branch also returned a
hard-coded legacy `{"decision":"decline"}` response.

The active Codex CLI is 0.144.6. Its app-server protocol requires
`item/permissions/requestApproval` responses to contain the granted permission
subset in `permissions`; an optional `scope` selects turn or session lifetime.
Permissions omitted from the response are denied. Upstream reference:
[Codex app-server permission requests](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#permission-requests).

### Change

Added `codex-app-server-phase-hotfix` 1.2.0 with `approval_bridge.py`:

- injects a Gateway-aware callback when Hermes provides no CLI callback;
- reuses the active session's existing Hermes blocking approval queue;
- surfaces command, file-change, and permission requests through the adapter's
  normal approval UI, including QQ buttons and text fallback;
- returns only schema-approved requested permission fields;
- maps one-shot approval to turn scope and QQ's persistent choice to the current
  Codex session only;
- treats deny, timeout, missing notifier, and bridge errors as no grant;
- detects an equivalent upstream permission-profile response and skips that
  portion of the patch.

No running-container source was edited. Installation remains a copy into the
host-mounted Hermes plugin directory followed by a Gateway restart.

The local deployment copied version 1.2.0 into the persistent Hermes plugin
directory and restarted the launchd-supervised Gateway. Startup logging
confirmed both `gateway callback patched` and `permission requests patched`.

### Verification

The plugin regression script validates phase routing, image materialization,
empty-image final recovery, Gateway callback queueing, requested-subset grants,
an actual Hermes queue round trip, session-scope mapping, and empty-subset
denial:

```bash
python plugins/codex-app-server-phase-hotfix/test_hotfix.py
```

Live verification requires starting a Codex turn through QQ that requests
network or additional filesystem access, confirming that the approval message
arrives, and separately testing allow-once and deny. Silence must time out to an
empty grant.

### Rollback

Disable `codex-app-server-phase-hotfix` and restart the Gateway. This restores
the upstream Hermes 0.18.2 approval behavior and leaves Codex configuration and
permission profiles unchanged.
