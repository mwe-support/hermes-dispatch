# Message Snapshot Store

Persistent, queryable QQ and WhatsApp message snapshots for Hermes gateways.
The plugin is separate from Hermes session transcripts: ordinary group context
is bounded, while the snapshot database remains available until the operator
deletes it.

## Why this plugin exists

The QQ compatibility buffer historically formatted only delivered events whose
`content` field was non-empty. Image-only, voice-only, video-only, and
file-only events could therefore be omitted from recent group context.

This plugin wraps the QQ raw-message callback and registers an explicit capture
hook used by `qqbot-connect-hotfix`. The hook captures every QQ message event
**delivered to the gateway** before routing decisions regardless of plugin load
order, and later enriches the same logical row when a normalized `MessageEvent`
is available. Repeated observation by nested wrappers is deduplicated by event
type and canonical payload hash. It does not modify files inside the Hermes
installation.

Version 1.1.1 closes every snapshot SQLite connection when its transaction
context exits. The standard Python context manager does not close a connection;
on Windows that retained handle caused `WinError 32` during temporary-profile
cleanup and automatic rollback. Transaction behavior and the database schema
are unchanged. No configuration or migration is required. Restoring 1.1.0 from
the installer backup restores the previous Windows handle lifetime.

For WhatsApp, the plugin wraps Hermes' deferred `WhatsAppAdapter` factory and
captures each normalized Baileys `messages.upsert` bridge event before
`_build_message_event` applies the mention-response gate. An unmentioned group
message is therefore snapshotted but does not trigger the agent. If a later
message mentions or replies to the bot, the same bounded durable context is
available to that turn.

Baileys emits `proto.IWebMessageInfo` updates and Hermes' Node bridge decrypts
media with `downloadMediaMessage()` before the Python adapter sees it. The
bridge event contains a local decrypted cache path rather than a durable
plaintext CDN URL. The plugin therefore mirrors WhatsApp media immediately into
the content-addressed archive, using streaming SHA-256/copy operations so a
large video is not loaded into Python memory a second time.

References:

- <https://baileys.wiki/docs/socket/receiving-updates/>
- <https://baileys.wiki/docs/socket/handling-messages/>
- <https://baileys.wiki/docs/api/functions/downloadMediaMessage/>

### QQ official group-event boundary

QQ's default receive scope for a group robot is mention-only. The group owner
can change that robot's per-group setting to **获取全部群消息**. With the switch
off, an unmentioned text, image, voice, video, or file message never reaches
Hermes and cannot be captured by this plugin. With it on, QQ delivers ordinary
traffic as `GROUP_MESSAGE_CREATE`; `qqbot-connect-hotfix` accepts that event and
this plugin snapshots it before the mention-response routing decision.

The native QQ group-owner switch is the authorization boundary. The separate
`qqbot-connect-hotfix` compatibility response for QQ interaction types
`2001`/`2002` only completes connector configuration exchanges when QQ sends
one; it is not required to grant or refresh the native permission.

If the group owner cannot or does not enable full-group delivery, supported
recovery paths are:

- send the media in the same message that mentions the bot;
- reply to/quote an older media message and mention the bot. QQ
  `message_type=103` carries the quoted attachment in `msg_elements`, allowing
  Hermes to download and snapshot it at that point.

Reference: <https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html>

## Storage model

The SQLite database contains:

- `messages`: one canonical logical message per platform/chat/message ID;
- `raw_events`: immutable transport payload snapshots, including duplicate
  event forms for the same logical message;
- `attachments`: per-file QQ URL or WhatsApp decrypted cache/archive path,
  remote ID, filename, MIME type, declared size, cache/archive state, and
  SHA-256 when bytes have been observed;
- `message_values`: flattened raw JSON scalar paths and values for exact
  indexed lookup;
- `message_fts`: FTS5 text index used for BM25 retrieval. Signed URLs and
  secret-like fields are intentionally excluded from this redundant index.

The database uses WAL, foreign keys, `synchronous=FULL`, and owner-only
filesystem permissions. Default location:

```text
$HERMES_HOME/message-snapshots/snapshots.sqlite3
```

Back up the SQLite database together with its `-wal` file using a SQLite-aware
backup operation, or stop the gateway before copying it.

## Media storage and its traps

Default mode is `link`:

```text
MESSAGE_SNAPSHOT_MEDIA_STORAGE=link
```

It stores the exact QQ attachment URL and metadata but creates no extra media
copy. If Hermes already downloaded a routed attachment, its cache path and
SHA-256 are recorded without duplicating the bytes. This includes non-image
files recovered from QQ `message_type=103` quote markers, whose cache paths are
embedded in normalized text rather than `media_urls`. `/message-snapshot restore` first
reuses that cache; otherwise it downloads through the currently connected QQ
adapter and only then creates a content-addressed archive.

Link storage has important limits:

- a QQ CDN URL may require the bot Authorization header;
- deletion, permission changes, token/app changes, or URL expiry can make a
  saved link unusable;
- a link snapshot is permanent metadata, not a guarantee that remote bytes
  remain permanently recoverable;
- signed URLs are sensitive and the database must not be shared casually;
- URLs are not fetched during search or context construction.

`link` applies to QQ. WhatsApp media is always mirrored at capture time because
Baileys' decrypted local file is the only reliable plaintext artifact exposed
by the current Hermes bridge. If Baileys reports media but download/reupload
recovery fails, the snapshot still records an unavailable attachment marker;
it cannot fabricate a durable link or restore bytes that were never decrypted.

For offline permanence, set `MESSAGE_SNAPSHOT_MEDIA_STORAGE=mirror`. This
downloads each observed attachment immediately. Mirrored bytes are deduplicated
by SHA-256 under `media/<sha-prefix>/<sha256>.<ext>`, but large videos and files
will still consume real storage.

## Recent context

QQ and WhatsApp group events visible to the bot receive a database-backed
context block. QQ includes unmentioned traffic only for groups whose owner
enabled **获取全部群消息**. WhatsApp includes all group traffic admitted by the
Baileys bridge, while `WHATSAPP_REQUIRE_MENTION=true` independently controls
whether a particular message triggers the agent.
Defaults:

```text
MESSAGE_SNAPSHOT_CONTEXT_MESSAGES=20
MESSAGE_SNAPSHOT_CONTEXT_TOKENS=4000
```

The current message is excluded. Pure-media messages appear as attachment
markers containing snapshot IDs, filenames, MIME types, and hashes when known.
The token budget uses a conservative tokenizer-independent estimate. Older
snapshots remain in SQLite but are not injected unless a human asks for them or
the agent invokes a retrieval tool.

## Hybrid retrieval

`message_snapshot_search` combines:

1. hard structured filters and exact matches;
2. SQLite FTS5/BM25 lexical retrieval;
3. exact substring recall;
4. Unicode character bi-gram fuzzy recall;
5. reciprocal-rank fusion (RRF) for the final ordering.

Chinese text is expanded with CJK bi-grams and tri-grams before FTS indexing,
because SQLite's default Unicode tokenizer does not consistently segment
continuous Chinese text. No message is sent to an external embedding service.
This is a private structured/lexical hybrid; a vector backend can be added
later only with an explicit local or privacy-approved embedding provider.

Exact filters include snapshot ID, platform message ID, chat, sender, event
type, message kind, attachment filename/remote ID/URL/SHA-256/MIME, time range,
and arbitrary flattened `field_path=value` pairs.

## Commands and tools

Deterministic chat commands:

```text
/message-snapshot search 蓝色导航 chat_id=<id>
/message-snapshot search message_id=<exact-id>
/message-snapshot search field_path=author.member_openid value=<exact-value>
/message-snapshot get <snapshot-id-or-message-id>
/message-snapshot restore <snapshot-id-or-message-id>
/message-snapshot stats
```

The agent also receives these tools by default:

- `message_snapshot_search`
- `message_snapshot_get`
- `message_snapshot_restore`

`restore` is the explicit human reminder/action that may cause linked QQ media
to be downloaded and pinned locally. WhatsApp media is already pinned during
capture and restore creates a readable hard-link/copy plus a manifest.

## Install and rollback

Enable the snapshot plugin plus the connector compatibility plugins in use.
Capture is independent of runtime load order:

```bash
hermes plugins enable qqbot-connect-hotfix
hermes plugins enable whatsapp-bridge-policy-hotfix
hermes plugins enable message-snapshot-store
hermes gateway restart
```

Rollback only disables capture and tools; it does not delete snapshots:

```bash
hermes plugins disable message-snapshot-store
hermes gateway restart
```

Delete `$HERMES_HOME/message-snapshots` separately only when data destruction is
intentional.

## Verification

```bash
python plugins/message-snapshot-store/test_store.py
python plugins/message-snapshot-store/test_capture.py
python plugins/message-snapshot-store/test_materialize.py
python plugins/message-snapshot-store/test_quoted_attachment.py
python plugins/message-snapshot-store/test_whatsapp_capture.py
```
