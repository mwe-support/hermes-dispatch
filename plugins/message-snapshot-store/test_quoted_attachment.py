from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace


def load_package():
    root = Path(__file__).parent
    name = "message_snapshot_quoted_test"
    spec = importlib.util.spec_from_file_location(
        name,
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(root)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


pkg = load_package()
SnapshotConfig = sys.modules[f"{pkg.__name__}.config"].SnapshotConfig
SnapshotStore = sys.modules[f"{pkg.__name__}.store"].SnapshotStore


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    cached = root / "quoted-cache.jpeg"
    cached.write_bytes(b"quoted-history-file")
    store = SnapshotStore(
        SnapshotConfig(
            root_dir=root / "snapshots",
            db_path=root / "snapshots" / "snapshots.sqlite3",
            media_dir=root / "snapshots" / "media",
            restore_dir=root / "snapshots" / "restored",
            media_storage="link",
        )
    )
    store.initialize()
    raw = {
        "id": "quote-event-1",
        "group_openid": "group-quote",
        "author": {"member_openid": "member-quote"},
        "content": "@bot 恢复引用文件",
        "message_type": 103,
        "msg_elements": [
            {
                "content": "",
                "attachments": [
                    {
                        "content_type": "file",
                        "filename": "AI-teenager.jpeg",
                        "size": 228602,
                        "url": "https://multimedia.example.invalid/quoted-file",
                    }
                ],
            }
        ],
    }
    snapshot_id = store.record_raw(
        profile="default",
        platform="qqbot",
        event_type="GROUP_AT_MESSAGE_CREATE",
        raw=raw,
    )
    normalized_text = (
        "[Quoted message]:\n"
        f"[file: AI-teenager.jpeg ({cached})]\n\n"
        "@bot 恢复引用文件"
    )
    store.record_normalized(
        SimpleNamespace(
            source=SimpleNamespace(
                platform="qqbot",
                chat_id="group-quote",
                chat_type="group",
                user_id="member-quote",
                user_name=None,
            ),
            raw_message=raw,
            message_id="quote-event-1",
            text=normalized_text,
            message_type="text",
            reply_to_message_id=None,
            timestamp=None,
            media_urls=[],
            media_types=[],
        )
    )
    snapshot = store.get_snapshot(snapshot_id)
    attachment = snapshot["attachments"][0]
    assert attachment["archive_status"] == "cached"
    assert attachment["local_path"] == str(cached)
    assert attachment["sha256"]
    assert attachment["archive_path"] == ""
    assert store.stats()["archived_bytes"] == 0

    # Startup migration repairs snapshots produced by the pre-fix plugin.
    with store._connect() as conn:
        conn.execute(
            "UPDATE attachments SET local_path='', sha256='', byte_size=NULL, archive_status='pending' WHERE message_snapshot_id=?",
            (snapshot_id,),
        )
    store.initialize()
    repaired = store.get_snapshot(snapshot_id)["attachments"][0]
    assert repaired["archive_status"] == "cached"
    assert repaired["local_path"] == str(cached)
    assert repaired["sha256"]

    print("quoted_message_type_103=true")
    print("embedded_cache_path_captured=true")
    print("link_mode_extra_bytes=0")
    print("startup_backfill=true")
