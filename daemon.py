#!/usr/bin/env python3
"""
PMUD Daemon — 24/7 Telegram message hoarder.
Caches every message from every chat you have access to.
Tracks deletions and edits in real-time.
Runs in ghost mode (appear offline).

Usage:
    python daemon.py                        # Start live monitoring
    python daemon.py --backfill 500         # Backfill 500 msgs/chat then monitor
    python daemon.py --backfill-only 500    # One-shot backfill, exit when done
"""

import argparse
import asyncio
import logging
import os
import signal
import sqlite3
from datetime import datetime, timezone

from telethon import TelegramClient, errors, events
from telethon.tl.functions.account import UpdateStatusRequest
from telethon.tl.types import MessageService

# ===== CONFIG (env vars override defaults) =====
STATE = {
    'api_id': int(os.getenv('TG_API_ID', 30172652)),
    'api_hash': os.getenv('TG_API_HASH', '4b388db2483b7268e5e1dccf8f429c3a'),
    'phone_number': os.getenv('TG_PHONE', ''),
    'hoard_db': 'hoard.db',
    'session_file': 'daemon_session',
}

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.FileHandler('daemon.log'), logging.StreamHandler()],
)
log = logging.getLogger('pmud.daemon')

# =====================================================================
# DATABASE — SQLite hoard
# =====================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS message_cache (
    msg_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT,
    raw_text TEXT,
    date TEXT NOT NULL,
    edited_date TEXT,
    has_media INTEGER DEFAULT 0,
    media_type TEXT,
    media_id TEXT,
    is_deleted INTEGER DEFAULT 0,
    deleted_at TEXT,
    first_seen_at TEXT NOT NULL,
    is_service INTEGER DEFAULT 0,
    PRIMARY KEY (msg_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_msg_date   ON message_cache(date);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON message_cache(sender_id);
CREATE INDEX IF NOT EXISTS idx_msg_chat   ON message_cache(chat_id);
CREATE INDEX IF NOT EXISTS idx_msg_del    ON message_cache(is_deleted);
CREATE INDEX IF NOT EXISTS idx_msg_text   ON message_cache(text);

CREATE TABLE IF NOT EXISTS edit_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    old_text TEXT,
    old_raw_text TEXT,
    edited_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_edit_msg ON edit_history(msg_id, chat_id);

CREATE TABLE IF NOT EXISTS dialogs (
    chat_id INTEGER PRIMARY KEY,
    name TEXT,
    username TEXT,
    chat_type TEXT,
    unread_count INTEGER DEFAULT 0,
    last_sync_at TEXT
);

CREATE TABLE IF NOT EXISTS user_cache (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    display_name TEXT,
    first_seen TEXT,
    last_seen TEXT
);
"""

# FTS5 triggers — separate because sqlite may not have FTS5 compiled in
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
    text, raw_text,
    content=message_cache,
    content_rowid=rowid
);
CREATE TRIGGER IF NOT EXISTS msg_fts_insert AFTER INSERT ON message_cache BEGIN
    INSERT INTO msg_fts(rowid, text, raw_text) VALUES (new.rowid, new.text, new.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_delete AFTER DELETE ON message_cache BEGIN
    INSERT INTO msg_fts(msg_fts, rowid, text, raw_text) VALUES ('delete', old.rowid, old.text, old.raw_text);
END;
CREATE TRIGGER IF NOT EXISTS msg_fts_update AFTER UPDATE ON message_cache BEGIN
    INSERT INTO msg_fts(msg_fts, rowid, text, raw_text) VALUES ('delete', old.rowid, old.text, old.raw_text);
    INSERT INTO msg_fts(rowid, text, raw_text) VALUES (new.rowid, new.text, new.raw_text);
END;
"""


class Hoard:
    """Persistent message archive — append-only, never forgets."""

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        # FTS5 is optional — try, log if unavailable
        try:
            self.conn.executescript(FTS_SCHEMA)
            self.conn.commit()
            self._fts = True
        except sqlite3.OperationalError:
            self._fts = False
            log.warning("FTS5 not available — full-text search disabled. "
                        "Install libsqlite with FTS5 or rebuild Python.")

    # ---- message cache ---------------------------------------------------

    def msg_exists(self, msg_id: int, chat_id: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM message_cache WHERE msg_id=? AND chat_id=?",
            (msg_id, chat_id),
        ).fetchone() is not None

    def cache_message(self, msg, chat_name: str | None = None,
                       sender_name: str | None = None) -> bool:
        """Insert message if not already cached. Returns True if new."""
        if self.msg_exists(msg.id, msg.chat_id):
            return False

        # If no sender_name passed, look it up from user cache
        if not sender_name and msg.sender_id:
            row = self.conn.execute(
                "SELECT username, display_name FROM user_cache WHERE user_id=?",
                (msg.sender_id,)
            ).fetchone()
            if row:
                sender_name = row[0] or row[1] or None

        now = datetime.now(timezone.utc).isoformat()
        media_type, media_id = (self._media_info(msg.media)
                                if msg.media else (None, None))
        self.conn.execute(
            """INSERT OR IGNORE INTO message_cache
            (msg_id, chat_id, chat_name, sender_id, sender_name, text, raw_text,
             date, edited_date, has_media, media_type, media_id,
             is_deleted, first_seen_at, is_service)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (
                msg.id,
                msg.chat_id,
                chat_name,
                msg.sender_id,
                sender_name,
                msg.text,
                msg.raw_text,
                msg.date.isoformat() if msg.date else None,
                msg.edit_date.isoformat() if msg.edit_date else None,
                1 if msg.media else 0,
                media_type,
                media_id,
                now,
                1 if isinstance(msg, MessageService) else 0,
            ),
        )
        self.conn.commit()
        return True

    def mark_deleted(self, msg_id: int, chat_id: int):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "UPDATE message_cache SET is_deleted=1, deleted_at=? WHERE msg_id=? AND chat_id=?",
            (now, msg_id, chat_id),
        )
        self.conn.commit()

    def get_message_text(self, msg_id: int, chat_id: int) -> tuple:
        row = self.conn.execute(
            "SELECT text, raw_text FROM message_cache WHERE msg_id=? AND chat_id=?",
            (msg_id, chat_id),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def update_message_text(self, msg_id: int, chat_id: int, text, raw_text, edited_date):
        self.conn.execute(
            "UPDATE message_cache SET text=?, raw_text=?, edited_date=? WHERE msg_id=? AND chat_id=?",
            (text, raw_text, edited_date, msg_id, chat_id),
        )
        self.conn.commit()

    # ---- edit history ----------------------------------------------------

    def record_edit(self, msg_id: int, chat_id: int, old_text, old_raw_text):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO edit_history (msg_id, chat_id, old_text, old_raw_text, edited_at) VALUES (?,?,?,?,?)",
            (msg_id, chat_id, old_text, old_raw_text, now),
        )
        self.conn.commit()

    # ---- users -----------------------------------------------------------

    def upsert_user(self, user_id: int, username=None, display_name=None):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO user_cache (user_id, username, display_name, first_seen, last_seen)
            VALUES (?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username       = COALESCE(NULLIF(?,''), username),
                display_name   = COALESCE(NULLIF(?,''), display_name),
                last_seen      = ?""",
            (user_id, username, display_name, now, now, username, display_name, now),
        )
        self.conn.commit()

    # ---- dialogs ---------------------------------------------------------

    def upsert_dialog(self, dialog):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT OR REPLACE INTO dialogs
            (chat_id, name, username, chat_type, unread_count, last_sync_at)
            VALUES (?,?,?,?,?,?)""",
            (dialog.id, dialog.name,
             getattr(dialog.entity, 'username', None),
             type(dialog.entity).__name__,
             dialog.unread_count, now),
        )
        self.conn.commit()

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _media_info(media):
        if hasattr(media, 'photo') and media.photo:
            return 'photo', f"photo_{media.photo.id}"
        if hasattr(media, 'document') and media.document:
            doc_id = f"doc_{media.document.id}"
            for attr in media.document.attributes:
                tn = type(attr).__name__
                if tn == 'DocumentAttributeVideo':
                    return 'video', doc_id
                if tn == 'DocumentAttributeAudio':
                    return 'audio', doc_id
                if tn == 'DocumentAttributeFilename':
                    return attr.file_name.rsplit('.', 1)[-1] if '.' in attr.file_name else 'document', doc_id
            return 'document', doc_id
        if hasattr(media, 'webpage') and media.webpage:
            return 'link', None
        if hasattr(media, 'poll') and media.poll:
            return 'poll', None
        return 'other', None

    # ---- stats -----------------------------------------------------------

    @property
    def message_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM message_cache").fetchone()[0]

    @property
    def deleted_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM message_cache WHERE is_deleted=1").fetchone()[0]

    @property
    def edit_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM edit_history").fetchone()[0]

    @property
    def dialog_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM dialogs").fetchone()[0]

    @property
    def user_count(self):
        return self.conn.execute("SELECT COUNT(*) FROM user_cache").fetchone()[0]


# =====================================================================
# DAEMON
# =====================================================================
class Daemon:
    """Long-lived Telegram monitor — hoards messages, tracks deletes & edits."""

    def __init__(self, backfill: int = 0, backfill_all: bool = False):
        self.backfill = backfill
        self.backfill_all = backfill_all
        self.db = Hoard(STATE['hoard_db'])
        self.client = TelegramClient(
            STATE['session_file'],
            STATE['api_id'],
            STATE['api_hash'],
            # Reconnect settings — stay alive
            connection_retries=None,
            retry_delay=5,
        )
        self._running = True

        # Trap signals for graceful shutdown
        signal.signal(signal.SIGINT,  lambda *_: self._stop())
        signal.signal(signal.SIGTERM, lambda *_: self._stop())

    def _stop(self):
        self._running = False
        try:
            self.client.disconnect()
        except Exception:
            pass

    async def run(self):
        log.info("=" * 48)
        log.info("PMUD Daemon starting")
        log.info(f"Hoard DB: {STATE['hoard_db']}")
        log.info(f"Already cached: {self.db.message_count} msgs, "
                 f"{self.db.dialog_count} chats, {self.db.deleted_count} deleted")

        self._register_handlers()

        while self._running:
            try:
                # Connect to Telegram
                await self.client.connect()

                # Auth flow — only runs if session isn't already authorized
                if not await self.client.is_user_authorized():
                    print()
                    print("=" * 48)
                    print("     PMUD Daemon — Telegram Login")
                    print("=" * 48)
                    phone = STATE['phone_number'] or input("Phone number (e.g. +1234567890): ").strip()
                    STATE['phone_number'] = phone

                    # Send the code request
                    await self.client.send_code_request(phone)
                    code = input("Code from Telegram: ").strip()

                    try:
                        await self.client.sign_in(phone, code)
                    except errors.SessionPasswordNeededError:
                        pwd = input("2FA password: ")
                        await self.client.sign_in(password=pwd)

                    print("Authenticated!\n")

                me = await self.client.get_me()
                log.info(f"Connected as {me.first_name} (ID: {me.id})")

                # Ghost mode — appear offline
                await self.client(UpdateStatusRequest(offline=True))
                log.info("Ghost mode ON — appearing offline")

                # Sync dialogs
                await self._sync_dialogs()

                # Backfill history if requested
                if self.backfill_all:
                    await self._backfill_all_unlimited()
                elif self.backfill > 0:
                    await self._backfill_limited()
                if getattr(self, '_backfill_then_exit', False):
                    break

                # Print summary and hand over to event loop
                log.info(f"Live. Hoard: {self.db.message_count} msgs, "
                         f"{self.db.deleted_count} deleted, {self.db.edit_count} edits")
                log.info("Monitoring all chats...")

                # Start media queue watcher (downloads thumbs on demand from UI)
                watch_task = asyncio.create_task(self._watch_media_queue())

                await self.client.run_until_disconnected()

                watch_task.cancel()

            except Exception as e:
                if self._running:
                    log.error(f"Disconnected: {e}")
                    log.info("Reconnecting in 10s...")
                    await asyncio.sleep(10)

        log.info("Daemon stopped.")
        await self.client.disconnect()

        # Print final stats
        print(f"\nFinal hoard stats:")
        print(f"   Messages:    {self.db.message_count}")
        print(f"   Deletions:   {self.db.deleted_count}")
        print(f"   Edits saved: {self.db.edit_count}")
        print(f"   Chats:       {self.db.dialog_count}")
        print(f"   Users:       {self.db.user_count}")

    # ---- event handlers --------------------------------------------------

    def _register_handlers(self):
        @self.client.on(events.NewMessage)
        async def handle_new_message(event):
            try:
                msg = event.message
                if not msg:
                    return

                chat_name = await self._resolve_chat_name(msg.chat_id)

                # Cache sender info FIRST so cache_message can use it
                sender_name_resolved = None
                if msg.sender_id:
                    try:
                        sender = await event.get_sender()
                        if sender:
                            uname = getattr(sender, 'username', None)
                            fname = getattr(sender, 'first_name', None)
                            lname = getattr(sender, 'last_name', None)
                            disp = (fname or '') + (' ' + lname if lname else '')
                            sender_name_resolved = uname or disp.strip() or None
                            self.db.upsert_user(msg.sender_id, uname, disp.strip() or fname)
                    except Exception:
                        pass

                self.db.cache_message(msg, chat_name, sender_name=sender_name_resolved)

                # Download thumbnail for photo media (fire-and-forget)
                if msg.media and hasattr(msg.media, 'photo'):
                    asyncio.create_task(self._download_thumb(msg))

            except Exception as e:
                log.error(f"handle_new_message: {e}")

    async def _download_thumb(self, msg):
        """Download small thumbnail for a photo message."""
        try:
            thumb_dir = 'media_thumbs'
            os.makedirs(thumb_dir, exist_ok=True)
            path = os.path.join(thumb_dir, f"{msg.chat_id}_{msg.id}.jpg")
            if not os.path.exists(path):
                await self.client.download_media(message=msg, file=path, thumb=-1)
        except Exception:
            pass  # thumbnails are best-effort

        @self.client.on(events.MessageDeleted)
        async def handle_delete(event):
            try:
                chat_id = event.chat_id
                if not chat_id:
                    return
                for msg_id in event.deleted_ids:
                    self.db.mark_deleted(msg_id, chat_id)
                    log.info(f"DEL msg#{msg_id}")
            except Exception as e:
                log.error(f"handle_delete: {e}")

        @self.client.on(events.MessageEdited)
        async def handle_edit(event):
            try:
                msg = event.message
                if not msg:
                    return
                old_text, old_raw = self.db.get_message_text(msg.id, msg.chat_id)
                # Only record if we have the original and it actually changed
                if old_text is not None and old_text != msg.text:
                    self.db.record_edit(msg.id, msg.chat_id, old_text, old_raw)
                    ed = msg.edit_date.isoformat() if msg.edit_date else datetime.now(timezone.utc).isoformat()
                    self.db.update_message_text(msg.id, msg.chat_id, msg.text, msg.raw_text, ed)
                    log.info(f"EDIT msg#{msg.id}")
            except Exception as e:
                log.error(f"handle_edit: {e}")

    async def _resolve_chat_name(self, chat_id):
        try:
            entity = await self.client.get_entity(chat_id)
            return (getattr(entity, 'title', None)
                    or getattr(entity, 'first_name', None)
                    or str(chat_id))
        except Exception:
            return str(chat_id)

    # ---- sync & backfill -------------------------------------------------

    async def _sync_dialogs(self):
        log.info("Syncing dialog list...")
        try:
            count = 0
            async for dialog in self.client.iter_dialogs():
                self.db.upsert_dialog(dialog)
                count += 1
            log.info(f"Dialogs synced: {count}")
        except Exception as e:
            log.error(f"Dialog sync failed: {e}")

    async def _backfill_all_unlimited(self):
        """Fetch ALL messages from every dialog (no limit) — complete history."""
        log.info("Backfilling ALL messages from every dialog (this will take a while)...")
        total_new = 0
        processed = 0

        try:
            async for dialog in self.client.iter_dialogs():
                if not self._running:
                    break

                processed += 1
                chat_name = dialog.name or f"chat#{dialog.id}"

                # Skip if we already have all messages (no offset_id saved)
                # This fetches everything from scratch

                try:
                    count = 0
                    async for msg in self.client.iter_messages(dialog.id):
                        if isinstance(msg, MessageService):
                            continue

                        # Resolve sender name FIRST so cache_message can use it
                        sender_name = None
                        if msg.sender_id:
                            try:
                                sender = await msg.get_sender()
                                if sender:
                                    uname = getattr(sender, 'username', None)
                                    fname = getattr(sender, 'first_name', None)
                                    lname = getattr(sender, 'last_name', None)
                                    disp = (fname or '') + (' ' + lname if lname else '')
                                    sender_name = uname or disp.strip() or None
                                    self.db.upsert_user(msg.sender_id, uname, disp.strip() or fname)
                            except Exception:
                                pass

                        if self.db.cache_message(msg, chat_name, sender_name=sender_name):
                            count += 1

                        # Be polite — short pause every 100 messages
                        if count % 100 == 0:
                            await asyncio.sleep(0.1)

                    if count:
                        log.info(f"  [{processed}] {chat_name}: +{count}")
                        total_new += count

                    # Be polite — pause between dialogs
                    await asyncio.sleep(0.3)

                except errors.FloodWaitError as e:
                    log.warning(f"  Rate limited for {e.seconds}s — sleeping...")
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    log.warning(f"  Skipping {chat_name}: {e}")

        except Exception as e:
            log.error(f"Backfill failed: {e}")

        log.info(f"Backfill complete. +{total_new} new messages across {processed} chats")

    async def _backfill_limited(self):
        """Fetch up to N recent messages from each dialog."""
        limit = self.backfill
        log.info(f"Backfilling up to {limit} msgs per dialog...")
        total_new = 0
        processed = 0

        try:
            async for dialog in self.client.iter_dialogs():
                if not self._running:
                    break
                processed += 1
                chat_name = dialog.name or f"chat#{dialog.id}"
                try:
                    count = 0
                    async for msg in self.client.iter_messages(dialog.id, limit=limit):
                        if isinstance(msg, MessageService):
                            continue
                        sender_name = None
                        if msg.sender_id:
                            try:
                                sender = await msg.get_sender()
                                if sender:
                                    uname = getattr(sender, 'username', None)
                                    fname = getattr(sender, 'first_name', None)
                                    lname = getattr(sender, 'last_name', None)
                                    disp = (fname or '') + (' ' + lname if lname else '')
                                    sender_name = uname or disp.strip() or None
                                    self.db.upsert_user(msg.sender_id, uname, disp.strip() or fname)
                            except Exception:
                                pass
                        if self.db.cache_message(msg, chat_name, sender_name=sender_name):
                            count += 1
                    if count:
                        log.info(f"  [{processed}] {chat_name}: +{count}")
                        total_new += count
                    await asyncio.sleep(0.3)
                except errors.FloodWaitError as e:
                    log.warning(f"  Rate limited for {e.seconds}s...")
                    await asyncio.sleep(e.seconds + 1)
                except Exception as e:
                    log.warning(f"  Skipping {chat_name}: {e}")
        except Exception as e:
            log.error(f"Backfill failed: {e}")
        log.info(f"Backfill complete. +{total_new} new messages across {processed} chats")

    # ---- on-demand thumbnail download from API requests ----
    async def _watch_media_queue(self):
        """Background task: watch for thumbnail download requests from the API server."""
        queue_dir = 'media_queue'
        thumb_dir = 'media_thumbs'
        os.makedirs(queue_dir, exist_ok=True)
        os.makedirs(thumb_dir, exist_ok=True)

        while self._running:
            try:
                for fname in os.listdir(queue_dir):
                    if not self._running:
                        return
                    if not fname.endswith('.request'):
                        continue
                    # Parse: chatId_msgId.request
                    stem = fname[:-8]
                    parts = stem.split('_', 1)
                    if len(parts) != 2:
                        os.remove(os.path.join(queue_dir, fname))
                        continue
                    chat_id, msg_id = int(parts[0]), int(parts[1])
                    thumb_path = os.path.join(thumb_dir, f"{chat_id}_{msg_id}.jpg")

                    if not os.path.exists(thumb_path):
                        try:
                            entity = await self.client.get_input_entity(chat_id)
                            msg = await self.client.get_messages(entity, ids=msg_id)
                            if msg and msg.media:
                                await msg.download_media(file=thumb_path, thumb=-1)
                        except Exception:
                            pass

                    os.remove(os.path.join(queue_dir, fname))
                    log.debug(f"Thumbnail ready: {chat_id}_{msg_id}.jpg")

            except Exception as e:
                log.error(f"Media queue error: {e}")

            await asyncio.sleep(1.5)


# =====================================================================
# CLI
# =====================================================================
def main():
    parser = argparse.ArgumentParser(
        description="PMUD Daemon — 24/7 Telegram message hoarder",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--backfill', type=int, default=0,
                        help='Backfill N recent messages per dialog on start')
    parser.add_argument('--backfill-all', action='store_true',
                        help='Backfill EVERY message since the beginning (no limit)')
    parser.add_argument('--backfill-only', type=int, default=0,
                        help='Backfill N messages per dialog then exit')
    parser.add_argument('--backfill-all-only', action='store_true',
                        help='Backfill ALL messages then exit')
    parser.add_argument('--db', default=None,
                        help=f'Hoard database path (default: {STATE["hoard_db"]})')
    parser.add_argument('--phone', default=None,
                        help='Phone number for auth (can also set TG_PHONE env)')
    parser.add_argument('--session', default=None,
                        help=f'Session file (default: {STATE["session_file"]})')

    args = parser.parse_args()

    if args.db:
        STATE['hoard_db'] = args.db
    if args.phone:
        STATE['phone_number'] = args.phone
    if args.session:
        STATE['session_file'] = args.session

    backfill = args.backfill or args.backfill_only
    backfill_all = args.backfill_all or args.backfill_all_only
    daemon = Daemon(backfill=backfill, backfill_all=backfill_all)
    if args.backfill_only or args.backfill_all_only:
        daemon._backfill_then_exit = True

    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\nShutdown requested.")


if __name__ == '__main__':
    main()
