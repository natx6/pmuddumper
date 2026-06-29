#!/usr/bin/env python3
"""
PMUD Media Server — self-contained Telegram media browser.

Connects to Telegram directly (no daemon needed), serves all chat photos
in a gallery UI with click-to-load and download.

Usage:
    python tg_dumper.py                          # First: download media
    python api_server.py                         # Then: browse & download
    python api_server.py --port 8080             # Custom port
    python api_server.py --db ../hoard.db        # Custom DB

Open http://localhost:8765
"""

import argparse
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse, Response
from telethon import TelegramClient, errors
from telethon.tl.functions.account import UpdateStatusRequest
import uvicorn

# ===== CONFIG =====
DEFAULT_DB = os.getenv('HOARD_DB', 'hoard.db')
DEFAULT_PORT = int(os.getenv('PORT', os.getenv('HOARD_PORT', '8765')))
STATE = {
    'api_id': int(os.getenv('TG_API_ID', 30172652)),
    'api_hash': os.getenv('TG_API_HASH', '4b388db2483b7268e5e1dccf8f429c3a'),
    'phone_number': os.getenv('TG_PHONE', ''),
    'session_file': 'daemon_session',
}

app = FastAPI(title='pmuddumper', version='1.0')

# ===== TELEGRAM CLIENT (runs inside the API server) =====
_tg_client: TelegramClient | None = None

async def start_telegram():
    global _tg_client
    _tg_client = TelegramClient(STATE['session_file'], STATE['api_id'], STATE['api_hash'],
                                connection_retries=None, retry_delay=5)
    await _tg_client.connect()
    if not await _tg_client.is_user_authorized():
        print("\n=== First-time Telegram Login ===")
        phone = STATE['phone_number'] or input("Phone (e.g. +1234567890): ").strip()
        await _tg_client.send_code_request(phone)
        code = input("Code from Telegram: ").strip()
        try:
            await _tg_client.sign_in(phone, code)
        except errors.SessionPasswordNeededError:
            pwd = input("2FA password: ")
            await _tg_client.sign_in(password=pwd)
        print("Authenticated!\n")
    me = await _tg_client.get_me()
    print(f"Connected as {me.first_name}")
    await _tg_client(UpdateStatusRequest(offline=True))

async def stop_telegram():
    global _tg_client
    if _tg_client:
        await _tg_client.disconnect()

@app.on_event('startup')
async def startup():
    print("Starting Telegram client...")
    asyncio.create_task(start_telegram())

@app.on_event('shutdown')
async def shutdown():
    await stop_telegram()

async def get_tg():
    """Wait for Telegram client to be ready."""
    for _ in range(50):
        if _tg_client and _tg_client.is_connected():
            return _tg_client
        await asyncio.sleep(0.1)
    raise RuntimeError("Telegram client not ready")

# ===== DB (read-only connection) =====
_conn: sqlite3.Connection | None = None
_hoard_db: str = DEFAULT_DB

def get_db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(_hoard_db, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA query_only=ON")
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


# =====================================================================
# API ENDPOINTS
# =====================================================================

@app.get('/api/stats')
def api_stats():
    """Dashboard statistics."""
    conn = get_db()

    def sc(q):
        return conn.execute(q).fetchone()[0]

    # Size on disk
    db_path = conn.execute("PRAGMA database_list").fetchone()
    db_size = os.path.getsize(db_path[2]) if db_path and os.path.exists(db_path[2]) else 0

    # Time range
    oldest = conn.execute("SELECT MIN(date) FROM message_cache").fetchone()[0]
    newest = conn.execute("SELECT MAX(date) FROM message_cache").fetchone()[0]

    return {
        'message_count':     sc("SELECT COUNT(*) FROM message_cache"),
        'deleted_count':     sc("SELECT COUNT(*) FROM message_cache WHERE is_deleted=1"),
        'edit_count':        sc("SELECT COUNT(*) FROM edit_history"),
        'dialog_count':      sc("SELECT COUNT(*) FROM dialogs"),
        'user_count':        sc("SELECT COUNT(*) FROM user_cache"),
        'media_count':       sc("SELECT COUNT(*) FROM message_cache WHERE has_media=1"),
        'db_size_bytes':     db_size,
        'oldest_message':    oldest,
        'newest_message':    newest,
        'last_24h_messages': sc("SELECT COUNT(*) FROM message_cache WHERE date >= datetime('now', '-1 day')"),
        'last_24h_deleted':  sc("SELECT COUNT(*) FROM message_cache WHERE is_deleted=1 AND deleted_at >= datetime('now', '-1 day')"),
    }


@app.get('/api/chats')
def api_chats(search: str = '', sort: str = 'last_active', order: str = 'desc',
              limit: int = 200, offset: int = 0):
    """List all tracked dialogs."""
    conn = get_db()

    where = ''
    params = []
    if search:
        where = "WHERE d.name LIKE ?"
        params.append(f'%{search}%')

    sort_col = {'name': 'd.name', 'messages': 'cnt', 'last_active': 'COALESCE(max_date, d.last_sync_at)'}.get(sort, 'd.name')
    dir = 'ASC' if order.lower() == 'asc' else 'DESC'

    rows = conn.execute(f"""
        SELECT d.*,
               COUNT(m.msg_id) AS message_count,
               SUM(m.is_deleted) AS deleted_count,
               MAX(m.date) AS max_date
        FROM dialogs d
        LEFT JOIN message_cache m ON m.chat_id = d.chat_id
        {where}
        GROUP BY d.chat_id
        ORDER BY {sort_col} {dir}
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    return [dict(r) for r in rows]


@app.get('/api/messages')
def api_messages(chat_id: int = 0, limit: int = 100, offset: int = 0,
                 deleted: int = -1, sender_id: int = 0, before: str = ''):
    """Get messages, optionally filtered by chat/sender/deleted status."""
    conn = get_db()
    clauses = []
    params = []

    if chat_id:
        clauses.append("m.chat_id = ?")
        params.append(chat_id)
    if deleted >= 0:
        clauses.append("m.is_deleted = ?")
        params.append(deleted)
    if sender_id:
        clauses.append("m.sender_id = ?")
        params.append(sender_id)
    if before:
        clauses.append("m.date < ?")
        params.append(before)
    if not clauses:
        clauses.append("1=1")

    where = " AND ".join(clauses)

    rows = conn.execute(f"""
        SELECT m.*, u.username AS _uname, u.display_name AS _dname
        FROM message_cache m
        LEFT JOIN user_cache u ON u.user_id = m.sender_id
        WHERE {where}
        ORDER BY m.date DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    return [dict(r) for r in rows]


@app.get('/api/messages/{msg_id}')
def api_message_detail(msg_id: int, chat_id: int = 0):
    """Single message with edit history."""
    conn = get_db()
    clause = "m.msg_id = ?"
    params = [msg_id]
    if chat_id:
        clause += " AND m.chat_id = ?"
        params.append(chat_id)

    msg = conn.execute(f"""
        SELECT m.*, u.username AS _uname, u.display_name AS _dname
        FROM message_cache m
        LEFT JOIN user_cache u ON u.user_id = m.sender_id
        WHERE {clause}
        LIMIT 1
    """, params).fetchone()

    if not msg:
        return {'error': 'not found'}

    result = dict(msg)
    result['edits'] = [dict(e) for e in conn.execute(
        "SELECT * FROM edit_history WHERE msg_id=? AND chat_id=? ORDER BY edited_at",
        (msg_id, msg['chat_id'])
    ).fetchall()]

    return result


@app.get('/api/search')
def api_search(q: str = '', chat_id: int = 0, limit: int = 100, offset: int = 0):
    """Search messages by text content."""
    conn = get_db()
    if not q:
        return []

    clauses = []
    params = []

    # Try full-text search first
    fts = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='msg_fts'"
    ).fetchone()

    if fts:
        # FTS5 — tokenize the query
        clauses.append("m.rowid IN (SELECT rowid FROM msg_fts WHERE msg_fts MATCH ?)")
        params.append(q)
    else:
        # Fallback: LIKE
        clauses.append("(m.text LIKE ? OR m.raw_text LIKE ?)")
        params.extend([f'%{q}%', f'%{q}%'])

    if chat_id:
        clauses.append("m.chat_id = ?")
        params.append(chat_id)

    where = " AND ".join(clauses)

    rows = conn.execute(f"""
        SELECT m.*, u.username AS _uname, u.display_name AS _dname
        FROM message_cache m
        LEFT JOIN user_cache u ON u.user_id = m.sender_id
        WHERE {where}
        ORDER BY m.date DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    return [dict(r) for r in rows]


@app.get('/api/deleted')
def api_deleted(limit: int = 100, offset: int = 0):
    """Recently deleted messages (content you can see that others can't)."""
    conn = get_db()
    rows = conn.execute("""
        SELECT m.*, u.username AS _uname, u.display_name AS _dname
        FROM message_cache m
        LEFT JOIN user_cache u ON u.user_id = m.sender_id
        WHERE m.is_deleted = 1 AND m.text IS NOT NULL
        ORDER BY m.deleted_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/edits')
def api_edits(limit: int = 100, offset: int = 0):
    """Recent message edits with the original text."""
    conn = get_db()
    rows = conn.execute("""
        SELECT e.*, m.chat_id, m.chat_name, m.sender_id,
               u.username AS _uname, u.display_name AS _dname,
               m.text AS new_text, m.raw_text AS new_raw_text
        FROM edit_history e
        JOIN message_cache m ON m.msg_id = e.msg_id AND m.chat_id = e.chat_id
        LEFT JOIN user_cache u ON u.user_id = m.sender_id
        ORDER BY e.edited_at DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/users')
def api_users(search: str = '', limit: int = 100, offset: int = 0):
    """All tracked users with message counts."""
    conn = get_db()
    where = ''
    params = []
    if search:
        where = "WHERE (u.username LIKE ? OR u.display_name LIKE ?)"
        params.extend([f'%{search}%', f'%{search}%'])

    rows = conn.execute(f"""
        SELECT u.*,
               COUNT(m.msg_id) AS message_count,
               SUM(m.is_deleted) AS deleted_count,
               MIN(m.date) AS first_msg,
               MAX(m.date) AS last_msg
        FROM user_cache u
        LEFT JOIN message_cache m ON m.sender_id = u.user_id
        {where}
        GROUP BY u.user_id
        ORDER BY message_count DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/users/{user_id}')
def api_user_detail(user_id: int):
    """Detailed user profile with activity breakdown."""
    conn = get_db()
    user = conn.execute(
        "SELECT * FROM user_cache WHERE user_id=?", (user_id,)
    ).fetchone()
    if not user:
        return {'error': 'not found'}

    result = dict(user)

    # Message count
    result['message_count'] = conn.execute(
        "SELECT COUNT(*) FROM message_cache WHERE sender_id=?", (user_id,)
    ).fetchone()[0]

    # Deleted count
    result['deleted_count'] = conn.execute(
        "SELECT COUNT(*) FROM message_cache WHERE sender_id=? AND is_deleted=1", (user_id,)
    ).fetchone()[0]

    # By chat
    result['chats'] = [dict(r) for r in conn.execute("""
        SELECT d.name AS chat_name, d.chat_id, COUNT(*) AS cnt
        FROM message_cache m
        JOIN dialogs d ON d.chat_id = m.chat_id
        WHERE m.sender_id=?
        GROUP BY m.chat_id
        ORDER BY cnt DESC
        LIMIT 20
    """, (user_id,)).fetchall()]

    # Activity by hour
    result['hourly_activity'] = [dict(r) for r in conn.execute("""
        SELECT CAST(strftime('%H', date) AS INTEGER) AS hour, COUNT(*) AS cnt
        FROM message_cache
        WHERE sender_id=? AND date IS NOT NULL
        GROUP BY hour
        ORDER BY hour
    """, (user_id,)).fetchall()]

    # Activity by day of week
    result['daily_activity'] = [dict(r) for r in conn.execute("""
        SELECT CAST(strftime('%w', date) AS INTEGER) AS day, COUNT(*) AS cnt
        FROM message_cache
        WHERE sender_id=? AND date IS NOT NULL
        GROUP BY day
        ORDER BY day
    """, (user_id,)).fetchall()]

    # Recent messages
    result['recent_messages'] = [dict(r) for r in conn.execute("""
        SELECT m.*, d.name AS chat_name
        FROM message_cache m
        LEFT JOIN dialogs d ON d.chat_id = m.chat_id
        WHERE m.sender_id=?
        ORDER BY m.date DESC
        LIMIT 20
    """, (user_id,)).fetchall()]

    return result


@app.get('/api/export')
def api_export(chat_id: int = 0, format: str = 'json'):
    """Export messages (JSON or CSV line)."""
    conn = get_db()
    if chat_id:
        rows = conn.execute(
            "SELECT * FROM message_cache WHERE chat_id=? ORDER BY date", (chat_id,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM message_cache ORDER BY date").fetchall()

    if format == 'json':
        return [dict(r) for r in rows]

    return {'error': 'unsupported format (try json)'}


# =====================================================================
# MEDIA THUMBNAILS
# =====================================================================
MEDIA_THUMBS_DIR = 'media_thumbs'

@app.get('/media/{filename}')
def serve_media(filename: str, dl: bool = False):
    """Serve downloaded media thumbnails. Add ?dl=1 for forced download."""
    # Basic security: only allow expected filename patterns
    name, _, ext = filename.partition('.')
    if ext not in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'mp4'):
        return HTMLResponse('', status_code=400)
    path = os.path.join(MEDIA_THUMBS_DIR, filename)
    if os.path.exists(path):
        mime = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
                'webp': 'image/webp', 'gif': 'image/gif', 'mp4': 'video/mp4'}
        headers = {'Content-Disposition': f'attachment; filename="{filename}"'} if dl else {}
        return FileResponse(path, media_type=mime.get(ext, 'application/octet-stream'), headers=headers)
    return HTMLResponse('', status_code=404)


# ===== AVATARS =====
@app.get('/api/avatar/{user_id}')
def user_avatar(user_id: int):
    """Generate a colored SVG avatar with initials for any user."""
    conn = get_db()
    user = conn.execute(
        "SELECT display_name, username FROM user_cache WHERE user_id=?",
        (user_id,)
    ).fetchone()
    name = ''
    if user:
        name = user[0] or user[1] or ''

    # Get initials (up to 2 chars)
    initials = '?'
    if name:
        parts = name.split()
        if len(parts) >= 2:
            initials = (parts[0][0] + parts[-1][0]).upper()
        else:
            initials = (name[:2]).upper()

    # Generate a consistent color from user_id
    h = abs(user_id * 137) % 360
    color = f'hsl({h}, 50%, 48%)'

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36">
      <circle cx="18" cy="18" r="18" fill="{color}"/>
      <text x="18" y="18" text-anchor="middle" dominant-baseline="central"
            fill="#fff" font-family="-apple-system,BlinkMacSystemFont,sans-serif"
            font-size="14" font-weight="600">{initials}</text>
    </svg>'''
    return Response(content=svg, media_type='image/svg+xml')


@app.get('/api/users/{user_id}/media')
def user_media(user_id: int, chat_id: int = 0, limit: int = 0, offset: int = 0):
    """Get ALL photos from a specific user (limit=0 means no limit)."""
    conn = get_db()
    where = "m.sender_id=? AND m.has_media=1 AND m.media_type='photo'"
    params = [user_id]
    if chat_id:
        where += " AND m.chat_id=?"
        params.append(chat_id)
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ? OFFSET ?"
        params += [limit, offset]
    rows = conn.execute(f"""
        SELECT m.*, d.name AS chat_name
        FROM message_cache m
        LEFT JOIN dialogs d ON d.chat_id = m.chat_id
        WHERE {where}
        ORDER BY m.date DESC
        {limit_clause}
    """, params).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/media')
def all_media(chat_id: int = 0, limit: int = 0, offset: int = 0):
    """Get ALL photo media (limit=0 means no limit)."""
    conn = get_db()
    where = "m.has_media=1 AND m.media_type='photo'"
    params = []
    if chat_id:
        where += " AND m.chat_id=?"
        params.append(chat_id)
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT ? OFFSET ?"
        params += [limit, offset]
    rows = conn.execute(f"""
        SELECT m.*, u.username AS _uname, u.display_name AS _dname, d.name AS chat_name
        FROM message_cache m
        LEFT JOIN user_cache u ON u.user_id = m.sender_id
        LEFT JOIN dialogs d ON d.chat_id = m.chat_id
        WHERE {where}
        ORDER BY m.date DESC
        {limit_clause}
    """, params).fetchall()
    return [dict(r) for r in rows]


@app.get('/api/media/count')
def media_count():
    """Total number of photos in the hoard."""
    conn = get_db()
    return {'count': conn.execute(
        "SELECT COUNT(*) FROM message_cache WHERE has_media=1 AND media_type='photo'"
    ).fetchone()[0]}


# ===== ON-DEMAND THUMBNAIL DOWNLOAD =====
THUMBS_DIR = 'media_thumbs'
_download_tasks = {}  # track active batch downloads

@app.post('/api/request_media/{chat_id}/{msg_id}')
async def request_media(chat_id: int, msg_id: int):
    """Download a single thumbnail using the API's Telegram connection."""
    os.makedirs(THUMBS_DIR, exist_ok=True)
    thumb_path = os.path.join(THUMBS_DIR, f"{chat_id}_{msg_id}.jpg")

    if os.path.exists(thumb_path):
        return {'status': 'ready', 'path': f'/media/{chat_id}_{msg_id}.jpg'}

    try:
        client = await get_tg()
        if not client or not client.is_connected():
            print(f"Telegram client not connected for {chat_id}/{msg_id}")
            return {'status': 'error', 'path': None, 'reason': 'not connected'}

        entity = await client.get_input_entity(chat_id)
        msg = await client.get_messages(entity, ids=msg_id)
        if not msg:
            print(f"Message {chat_id}/{msg_id} not found")
            return {'status': 'error', 'path': None, 'reason': 'msg not found'}
        if not msg.media:
            print(f"Message {chat_id}/{msg_id} has no media")
            return {'status': 'error', 'path': None, 'reason': 'no media'}

        await msg.download_media(file=thumb_path, thumb=-1)
        if os.path.exists(thumb_path):
            return {'status': 'ready', 'path': f'/media/{chat_id}_{msg_id}.jpg'}
        else:
            print(f"Download failed for {chat_id}/{msg_id} - no file created")
            return {'status': 'error', 'path': None, 'reason': 'download failed'}
    except ValueError as e:
        print(f"Entity error for {chat_id}: {e}")
        return {'status': 'error', 'path': None, 'reason': f'entity error: {e}'}
    except Exception as e:
        import traceback
        print(f"Download error for {chat_id}/{msg_id}: {e}")
        traceback.print_exc()
        return {'status': 'error', 'path': None, 'reason': str(e)}


@app.post('/api/request_all_media/{chat_id}')
async def request_all_media(chat_id: int):
    """Start downloading ALL thumbnails for a chat in the background."""
    os.makedirs(THUMBS_DIR, exist_ok=True)
    conn = get_db()
    rows = conn.execute("""
        SELECT msg_id FROM message_cache
        WHERE chat_id=? AND has_media=1 AND media_type='photo'
        ORDER BY date DESC
    """, (chat_id,)).fetchall()

    msg_ids = [r[0] for r in rows if not os.path.exists(
        os.path.join(THUMBS_DIR, f"{chat_id}_{r[0]}.jpg"))]

    if not msg_ids:
        return {'status': 'done', 'total': len(rows), 'already': len(rows), 'to_download': 0}

    total = len(msg_ids)
    _download_tasks[chat_id] = {'total': total, 'done': 0}

    async def batch_download():
        client = await get_tg()
        entity = await client.get_input_entity(chat_id)
        count = 0
        for mid in msg_ids:
            thumb_path = os.path.join(THUMBS_DIR, f"{chat_id}_{mid}.jpg")
            if os.path.exists(thumb_path):
                count += 1
                continue
            try:
                msg = await client.get_messages(entity, ids=mid)
                if msg and msg.media:
                    await msg.download_media(file=thumb_path, thumb=-1)
                await asyncio.sleep(0.3)
            except errors.FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
            except Exception:
                pass
            count += 1
            _download_tasks[chat_id]['done'] = count
        _download_tasks[chat_id]['done'] = total

    asyncio.create_task(batch_download())
    return {'status': 'started', 'to_download': total}


@app.get('/api/download_progress/{chat_id}')
def download_progress(chat_id: int):
    """Check progress of batch thumbnail download."""
    info = _download_tasks.get(chat_id)
    if not info:
        return {'running': False}
    return {'running': info['done'] < info['total'], 'done': info['done'], 'total': info['total']}


# ===== SYNC (import messages from Telegram into DB) =====
_sync_state = {'running': False, 'done': 0, 'total': 0, 'dialogs': 0}

@app.post('/api/sync')
async def trigger_sync(full: bool = False):
    """Scan all dialogs and cache message metadata (no media downloads)."""
    if _sync_state.get('running'):
        return {'status': 'already_running'}

    _sync_state['running'] = True
    _sync_state['done'] = 0
    _sync_state['total'] = 0
    _sync_state['full'] = full
    _sync_state['error'] = None

    async def do_sync():
        global _conn
        try:
            client = await get_tg()

            # Create the hoard DB if needed
            conn = sqlite3.connect(_hoard_db)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS message_cache (
                    msg_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                    chat_name TEXT, sender_id INTEGER, sender_name TEXT,
                    text TEXT, raw_text TEXT, date TEXT, edited_date TEXT,
                    has_media INTEGER DEFAULT 0, media_type TEXT, media_id TEXT,
                    is_deleted INTEGER DEFAULT 0, deleted_at TEXT,
                    first_seen_at TEXT NOT NULL, is_service INTEGER DEFAULT 0,
                    PRIMARY KEY (msg_id, chat_id)
                );
                CREATE INDEX IF NOT EXISTS idx_msg_chat ON message_cache(chat_id);
                CREATE INDEX IF NOT EXISTS idx_msg_sender ON message_cache(sender_id);
                CREATE TABLE IF NOT EXISTS dialogs (
                    chat_id INTEGER PRIMARY KEY, name TEXT, username TEXT,
                    chat_type TEXT, unread_count INTEGER DEFAULT 0, last_sync_at TEXT
                );
                CREATE TABLE IF NOT EXISTS user_cache (
                    user_id INTEGER PRIMARY KEY, username TEXT, display_name TEXT,
                    first_seen TEXT, last_seen TEXT
                );
            """)
            conn.commit()

            # Sync dialogs
            dialogs = []
            async for d in client.iter_dialogs():
                dialogs.append(d)
                conn.execute("INSERT OR REPLACE INTO dialogs VALUES (?,?,?,?,?,?)",
                    (d.id, d.name, getattr(d.entity, 'username', None),
                     type(d.entity).__name__, d.unread_count,
                     datetime.now().isoformat()))
            conn.commit()
            _sync_state['dialogs'] = len(dialogs)
            print(f"Synced {len(dialogs)} dialogs")

            # Backfill recent messages from each dialog
            total_msgs = 0
            for d in dialogs:
                try:
                    count = 0
                    msg_limit = None if _sync_state.get('full') else 200
                    async for msg in client.iter_messages(d.id, limit=msg_limit):
                        if hasattr(msg, 'action') and msg.action:
                            continue
                        exists = conn.execute(
                            "SELECT 1 FROM message_cache WHERE msg_id=? AND chat_id=?",
                            (msg.id, msg.chat_id)).fetchone()
                        if exists:
                            continue

                        sender_name = None
                        if msg.sender_id:
                            try:
                                s = await msg.get_sender()
                                if s:
                                    uname = getattr(s, 'username', None)
                                    fname = getattr(s, 'first_name', None)
                                    lname = getattr(s, 'last_name', None)
                                    disp = (fname or '') + (' ' + lname if lname else '')
                                    sender_name = uname or disp.strip() or None
                                    conn.execute(
                                        "INSERT OR REPLACE INTO user_cache VALUES (?,?,?,?,?)",
                                        (msg.sender_id, uname, disp.strip() or fname,
                                         datetime.now().isoformat(), datetime.now().isoformat()))
                            except Exception:
                                pass

                        media_type = None
                        media_id = None
                        if msg.media:
                            if hasattr(msg.media, 'photo') and msg.media.photo:
                                media_type, media_id = 'photo', f"photo_{msg.media.photo.id}"
                            elif hasattr(msg.media, 'document') and msg.media.document:
                                media_type, media_id = 'document', f"doc_{msg.media.document.id}"

                        conn.execute("""INSERT OR IGNORE INTO message_cache
                            (msg_id,chat_id,chat_name,sender_id,sender_name,text,raw_text,date,edited_date,
                             has_media,media_type,media_id,is_deleted,first_seen_at,is_service)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?,0)""",
                            (msg.id, msg.chat_id, d.name, msg.sender_id, sender_name,
                             msg.text, msg.raw_text,
                             msg.date.isoformat() if msg.date else None,
                             msg.edit_date.isoformat() if msg.edit_date else None,
                             1 if msg.media else 0, media_type, media_id,
                             datetime.now().isoformat()))
                        count += 1

                    conn.commit()
                    total_msgs += count
                    _sync_state['done'] += 1
                    _sync_state['total'] = len(dialogs)
                    print(f"  {d.name}: +{count} msgs")
                    await asyncio.sleep(0.2)

                except Exception as e:
                    print(f"  Error syncing {d.name}: {e}")

            # Reconnect the global DB
            _conn = None
            print(f"Sync complete: {total_msgs} new messages across {len(dialogs)} chats")

        except Exception as e:
            _sync_state['error'] = str(e)
            print(f"Sync error: {e}")
            import traceback
            traceback.print_exc()
        finally:
            _sync_state['running'] = False

    asyncio.create_task(do_sync())
    return {'status': 'started'}


@app.get('/api/sync_progress')
def sync_progress():
    """Check sync progress."""
    return {
        'running': _sync_state.get('running', False),
        'done': _sync_state.get('done', 0),
        'total': _sync_state.get('total', 0),
        'full': _sync_state.get('full', False),
        'error': _sync_state.get('error'),
    }


# ===== SETTINGS =====
@app.get('/api/settings')
def get_settings():
    """Return current settings (without secrets)."""
    return {
        'api_id': STATE.get('api_id', ''),
        'api_hash': STATE.get('api_hash', '')[:8] + '...' if STATE.get('api_hash') else '',
        'phone': STATE.get('phone_number', ''),
    }


@app.post('/api/settings')
def save_settings(data: dict = {}):
    """Update settings."""
    if 'api_id' in data:
        STATE['api_id'] = int(data['api_id'])
    if 'api_hash' in data and len(data['api_hash']) > 10:
        STATE['api_hash'] = data['api_hash']
    if 'phone' in data:
        STATE['phone_number'] = data['phone']
    return {'status': 'ok'}


# =====================================================================
# WEB UI
# =====================================================================

HTML = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>pmuddumper</title>
<style>
  *{margin:0;padding:0;box-sizing:border-box}
  body{background:#0d0d10;color:#d0d0d8;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column}
  .bar{padding:10px 16px;border-bottom:1px solid #1c1c24;background:#111115;display:flex;align-items:center;gap:10px;flex-shrink:0;flex-wrap:wrap}
  .bar h1{font-size:15px;font-weight:700;color:#5b8cff}
  .bar .sub{color:#6b6b80;font-size:12px}
  .bar select{padding:5px 10px;border:1px solid #1c1c24;border-radius:4px;background:#181820;color:#d0d0d8;font-size:12px;cursor:pointer;outline:none;max-width:300px;flex:1;min-width:120px}
  .main{flex:1;overflow-y:auto;padding:12px 16px}
  .empty{text-align:center;padding:50px 20px}
  .empty h2{font-size:17px;font-weight:600;margin-bottom:4px}
  .empty p{color:#5b5b6a;font-size:13px;margin-bottom:14px}
  .btn{padding:7px 18px;border:1px solid #5b8cff;border-radius:4px;background:rgba(91,140,255,0.08);color:#5b8cff;cursor:pointer;font-size:12px}
  .btn:hover{background:rgba(91,140,255,0.18)}
  .btn:disabled{opacity:.4}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
  .grid .item{background:#131318;border:1px solid #1c1c24;border-radius:4px;overflow:hidden;cursor:pointer}
  .grid .item:hover{border-color:#5b8cff}
  .grid .item .t{aspect-ratio:1;background:#181820;display:flex;align-items:center;justify-content:center;color:#3a3a48;font-size:18px;position:relative;overflow:hidden}
  .grid .item .t img{width:100%;height:100%;object-fit:cover;position:absolute;top:0;left:0}
  .grid .item .i{padding:4px 8px;display:flex;justify-content:space-between;font-size:10px;color:#5b5b6a;align-items:center}
  .grid .item .i .sv{color:#5b8cff}
  .lb{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.92);z-index:1000;justify-content:center;align-items:center}
  .lb.open{display:flex}
  .lb-x{position:absolute;top:10px;right:14px;color:#fff;font-size:22px;cursor:pointer;border:none;background:none;z-index:10}
  .lb-p,.lb-n{position:absolute;top:50%;transform:translateY(-50%);color:#fff;font-size:24px;cursor:pointer;background:rgba(255,255,255,.06);border:none;padding:6px 10px;border-radius:4px;z-index:10}
  .lb-p:hover,.lb-n:hover{background:rgba(255,255,255,.12)}
  .lb-p{left:6px}.lb-n{right:6px}
  .lb-c{display:flex;flex-direction:column;align-items:center}
  .lb-c img,.lb-c video{max-width:90vw;max-height:80vh;object-fit:contain;border-radius:4px}
  .lb-m{display:flex;gap:10px;align-items:center;margin-top:6px;color:#6b6b80;font-size:11px;flex-wrap:wrap;justify-content:center}
  .lb-m a{color:#5b8cff;padding:2px 8px;border:1px solid #5b8cff;border-radius:3px;font-size:11px;text-decoration:none}
  .lb-m a:hover{background:#5b8cff;color:#000}
  .more{text-align:center;padding:10px}
  ::-webkit-scrollbar{width:4px}
  ::-webkit-scrollbar-thumb{background:#1c1c24}

  @media(max-width:600px){
    .bar{flex-wrap:wrap;gap:6px;padding:8px 10px}
    .bar h1{font-size:14px;width:100%}
    .bar select{font-size:11px;padding:4px 6px;min-width:0;flex:1}
    .bar .btn{font-size:11px;padding:4px 10px}
    .main{padding:8px 8px}
    .grid{grid-template-columns:repeat(2,1fr);gap:5px}
    .grid .item .i{font-size:9px;padding:3px 6px}
    .grid .item .t{font-size:14px}
    .lb-p{left:2px;font-size:20px;padding:4px 6px}
    .lb-n{right:2px;font-size:20px;padding:4px 6px}
    .lb-x{top:6px;right:8px;font-size:18px}
    .lb-c img,.lb-c video{max-width:96vw;max-height:70vh}
    .lb-m{font-size:10px;gap:6px}
    .lb-m a{font-size:10px;padding:2px 6px}
  }
  @media(max-width:400px){
    .grid{grid-template-columns:repeat(2,1fr);gap:4px}
    .bar .btn{font-size:10px;padding:3px 8px}
  }
</style>
</head>
<body>
<div class="bar">
  <h1>pmuddumper</h1>
  <span class="sub" id="status"></span>
  <select id="chatSelect" onchange="selectChat(this.value)" style="display:none">
    <option value="">Select a chat...</option>
  </select>
  <button class="btn" onclick="refreshChat()">Refresh</button>
  <button class="btn" onclick="showSettings()" style="margin-left:auto">Settings</button>
</div>
<div class="main" id="main">
  <div class="empty" id="welcome">
    <h2>Telegram Media Gallery</h2>
    <p>Connect and sync to browse photos from your groups.</p>
    <button class="btn" onclick="startSync()">Sync Telegram</button>
    <div id="prog" style="margin-top:12px;font-size:12px;color:#5b5b6a"></div>
  </div>
</div>

<!-- Lightbox -->
<div class="lb" id="lb" onclick="if(event.target===this)closeLB()">
  <button class="lb-x" onclick="closeLB()">&times;</button>
  <button class="lb-p" onclick="prevLB()">&lsaquo;</button>
  <button class="lb-n" onclick="nextLB()">&rsaquo;</button>
  <div class="lb-c">
    <div id="lbM"></div>
    <div class="lb-m">
      <span id="lbMeta"></span>
      <span id="lbCount"></span>
      <a id="lbDl" href="#" download>Download</a>
    </div>
  </div>
</div>

<script>
// === State ===
let _items = [];
let _lbIdx = 0;

// === API helper ===
async function api(p) { const r = await fetch(p); return r.json(); }

// === Init ===
async function init() {
  const s = await api('/api/stats');
  document.getElementById('status').textContent = s.message_count + ' msgs, ' + s.media_count + ' photos';
  if (s.message_count > 0) {
    document.getElementById('welcome').style.display = 'none';
    loadChats();
  }
}

// === Sync (defined below after Settings) ===
async function loadChats() {
  const chats = await api('/api/chats?limit=200');
  const sel = document.getElementById('chatSelect');
  sel.style.display = '';
  sel.innerHTML = '<option value="">Select a chat...</option>' +
    chats.map(c => '<option value="' + c.chat_id + '">' + esc(c.name || '#'+c.chat_id) + ' (' + c.message_count + ' msgs)</option>').join('');
}

// === Chat photos ===
async function selectChat(chatId) {
  if (!chatId) return;
  const main = document.getElementById('main');
  main.innerHTML = '<div style="text-align:center;padding:40px;color:#5b5b6a;font-size:13px">Loading photos...</div>';
  const data = await api('/api/media?chat_id=' + chatId + '&limit=0');
  _items = data;

  if (!data.length) {
    main.innerHTML = '<div class="empty"><h2>No photos</h2><p>Nothing here yet.</p></div>';
    return;
  }

  // Extract available months
  const months = [...new Set(data.map(m => (m.date || '').substring(0, 7)))]
    .filter(Boolean).sort().reverse();

  // Store for filter
  window._chatMonths = months;
  window._chatData = data;

  function renderGrid(filterMonth) {
    const filtered = filterMonth ? data.filter(m => (m.date || '').startsWith(filterMonth)) : data;
    const gridHtml = filtered.slice(0, 50).map((m, i) => renderItem(m, data.indexOf(m))).join('');
    return { filtered, gridHtml, count: filtered.length };
  }

  function buildMonthFilter() {
    return '<select id="monthFilter" onchange="applyMonthFilter()" style="padding:4px 8px;border:1px solid #1c1c24;border-radius:4px;background:#181820;color:#d0d0d8;font-size:12px;cursor:pointer;outline:none">' +
      '<option value="">All months</option>' +
      months.map(m => '<option value="' + m + '">' + m + '</option>').join('') +
      '</select>';
  }

  const initial = renderGrid('');
  window._filteredData = initial.filtered;
  window._filteredIdx = 50;

  main.innerHTML =
    '<div style="margin-bottom:10px;font-size:12px;color:#5b5b6a;display:flex;align-items:center;gap:10px;flex-wrap:wrap">' +
    '<span>' + data.length + ' photos</span>' +
    buildMonthFilter() +
    '<button class="btn" onclick="dlAll()">Download all</button></div>' +
    '<div id="gridContainer"></div>' +
    '<div class="more" id="more"></div>';

  renderGroupedGrid(initial.filtered, initial.count);

  // Click handler
  document.getElementById('gridContainer').onclick = function(e) {
    const item = e.target.closest('.item');
    if (!item) return;
    const idx = parseInt(item.dataset.idx);
    if (idx >= 0 && data[idx]) {
      fetch('/api/request_media/' + data[idx].chat_id + '/' + data[idx].msg_id, {method:'POST'});
      openLB(idx);
    }
  };

  // Auto-load first 30
  data.slice(0, 30).forEach(m => {
    fetch('/api/request_media/' + m.chat_id + '/' + m.msg_id, {method:'POST'});
  });
}

function applyMonthFilter() {
  const month = document.getElementById('monthFilter').value;
  const data = window._chatData || [];
  const filtered = month ? data.filter(m => (m.date || '').startsWith(month)) : data;
  window._filteredData = filtered;
  window._filteredIdx = 50;
  renderGroupedGrid(filtered, filtered.length);
}

function renderGroupedGrid(items, total) {
  const container = document.getElementById('gridContainer');
  if (!container) return;

  // Group by month
  const groups = {};
  items.slice(0, window._filteredIdx).forEach(m => {
    const month = (m.date || '').substring(0, 7) || 'unknown';
    if (!groups[month]) groups[month] = [];
    groups[month].push(m);
  });

  let html = '';
  const allData = window._chatData || items;
  const sortedMonths = Object.keys(groups).sort().reverse();
  sortedMonths.forEach(month => {
    html += '<div style="font-size:12px;color:#5b5b6a;margin:12px 0 6px;font-weight:500">' + month + ' (' + groups[month].length + ')</div>';
    html += '<div class="grid">' + groups[month].map(m => renderItem(m, allData.indexOf(m))).join('') + '</div>';
  });

  container.innerHTML = html;

  const rem = items.length - window._filteredIdx;
  document.getElementById('more').innerHTML = rem > 0
    ? '<button class="btn" onclick="showMore()">Show 10 more (' + rem + ' left)</button>'
    : '';
}

function renderItem(m, idx) {
  const d = m.date ? m.date.substring(0,10) : '';
  const s = m._uname || m._dname || '';
  const url = '/media/' + m.chat_id + '_' + m.msg_id + '.jpg';
  const cid = m.chat_id, mid = m.msg_id;
  return '<div class="item" data-idx="' + idx + '">' +
    '<div class="t">' +
    '<img src="' + url + '" onerror="retryImg(this,\'' + cid + '\',' + mid + ')" onload="this.style.opacity=1">' +
    '</div>' +
    '<div class="i"><span>' + d + '</span><a href="' + url + '?dl=1" download class="sv" style="text-decoration:none">Save</a></div></div>';
}

function retryImg(img, cid, mid) {
  if (img.dataset.retry > 3) return;
  img.dataset.retry = (parseInt(img.dataset.retry||0) + 1);
  // Trigger thumbnail download
  fetch('/api/request_media/' + cid + '/' + mid, {method:'POST'});
  // Retry loading image after delay
  setTimeout(() => { img.src = '/media/' + cid + '_' + mid + '.jpg?t=' + Date.now(); }, 2000);
}

function showMore() {
  window._filteredIdx += 10;
  renderGroupedGrid(window._filteredData || _items, (window._filteredData || _items).length);

  // Auto-load new thumbnails
  const items = window._filteredData || _items;
  items.slice(window._filteredIdx - 10, window._filteredIdx).forEach(m => {
    fetch('/api/request_media/' + m.chat_id + '/' + m.msg_id, {method:'POST'});
  });
}

function dlAll() {
  _items.forEach(m => {
    const a = document.createElement('a');
    a.href = '/media/' + m.chat_id + '_' + m.msg_id + '.jpg?dl=1';
    a.download = m.chat_id + '_' + m.msg_id + '.jpg';
    a.click();
  });
}

// === Lightbox ===
function openLB(idx) {
  _lbIdx = idx;
  renderLB();
  document.getElementById('lb').classList.add('open');
  document.body.style.overflow = 'hidden';
}
function closeLB() {
  document.getElementById('lb').classList.remove('open');
  document.body.style.overflow = '';
}
function renderLB() {
  const m = _items[_lbIdx];
  const url = '/media/' + m.chat_id + '_' + m.msg_id + '.jpg';
  const s = m._uname || m._dname || 'User#' + (m.sender_id||0);
  const d = m.date ? m.date.substring(0,10) : '';
  const caption = m.text || m.raw_text || '';
  document.getElementById('lbM').innerHTML = '<img src="' + url + '" onclick="window.open(\''+url+'\')">';
  document.getElementById('lbMeta').textContent = s + '  ' + d;
  document.getElementById('lbCount').textContent = (_lbIdx+1) + '/' + _items.length;
  const dl = document.getElementById('lbDl');
  dl.href = url + '?dl=1';
  dl.download = m.chat_id + '_' + m.msg_id + '.jpg';
  // Show caption if exists
  let capEl = document.getElementById('lbCaption');
  if (caption) {
    if (!capEl) {
      capEl = document.createElement('div');
      capEl.id = 'lbCaption';
      capEl.style.cssText = 'color:#8b8ba0;font-size:13px;margin-top:4px;max-width:80vw;text-align:center';
      document.querySelector('.lb-m').before(capEl);
    }
    capEl.textContent = caption;
    capEl.style.display = '';
  } else if (capEl) {
    capEl.style.display = 'none';
  }
}
function nextLB() { _lbIdx = (_lbIdx+1) % _items.length; renderLB(); }
function prevLB() { _lbIdx = (_lbIdx-1+_items.length) % _items.length; renderLB(); }

document.addEventListener('keydown', function(e) {
  if (!document.getElementById('lb').classList.contains('open')) return;
  if (e.key === 'Escape') closeLB();
  if (e.key === 'ArrowRight') nextLB();
  if (e.key === 'ArrowLeft') prevLB();
});

// === Settings ===
async function refreshChat() {
  const sel = document.getElementById('chatSelect');
  const chatId = sel ? sel.value : '';
  // Quick sync to get latest messages
  await fetch('/api/sync', {method:'POST'});
  // Wait a moment then reload the current chat
  setTimeout(() => {
    if (chatId) {
      selectChat(chatId);
    } else {
      location.reload();
    }
  }, 1500);
}

async function showSettings() {
  const main = document.getElementById('main');
  const s = await api('/api/settings');
  main.innerHTML = `
    <div style="max-width:400px;margin:20px auto">
      <h2 style="font-size:16px;font-weight:600;margin-bottom:12px">Settings</h2>
      <div style="margin-bottom:10px">
        <label style="font-size:12px;color:#6b6b80">API ID</label>
        <input id="sApiId" value="${esc(s.api_id)}" style="width:100%;padding:6px 10px;border:1px solid #1c1c24;border-radius:4px;background:#181820;color:#d0d0d8;font-size:13px;margin-top:2px;outline:none">
      </div>
      <div style="margin-bottom:10px">
        <label style="font-size:12px;color:#6b6b80">API Hash</label>
        <input id="sApiHash" value="${esc(s.api_hash)}" style="width:100%;padding:6px 10px;border:1px solid #1c1c24;border-radius:4px;background:#181820;color:#d0d0d8;font-size:13px;margin-top:2px;outline:none">
      </div>
      <div style="margin-bottom:14px">
        <label style="font-size:12px;color:#6b6b80">Phone</label>
        <input id="sPhone" value="${esc(s.phone)}" style="width:100%;padding:6px 10px;border:1px solid #1c1c24;border-radius:4px;background:#181820;color:#d0d0d8;font-size:13px;margin-top:2px;outline:none">
      </div>
      <button class="btn" onclick="saveSettings()" style="margin-right:8px">Save</button>
      <button class="btn" onclick="location.reload()">Back</button>
      <span id="sStatus" style="margin-left:8px;font-size:12px;color:#5b5b6a"></span>
    </div>
  `;
}

async function saveSettings() {
  await fetch('/api/settings', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      api_id: document.getElementById('sApiId').value,
      api_hash: document.getElementById('sApiHash').value,
      phone: document.getElementById('sPhone').value,
    })
  });
  document.getElementById('sStatus').textContent = 'Saved! Restart to apply.';
}

async function startSync(full) {
  const btn = document.querySelector('[onclick*="startSync"]');
  const prog = document.getElementById('prog') || document.getElementById('syncProg');
  if (btn) btn.disabled = true;
  if (prog) { prog.textContent = 'Starting...'; prog.style.color = '#5b5b6a'; }
  try {
    const r = await fetch('/api/sync' + (full ? '?full=true' : ''), {method:'POST'});
    const data = await r.json();
    if (data.status === 'already_running') {
      if (prog) prog.textContent = 'Sync already running...';
    }
    const iv = setInterval(async () => {
      try {
        const p = await api('/api/sync_progress');
        if (p.error) {
          if (prog) { prog.textContent = 'Error: ' + p.error; prog.style.color = '#ff4757'; }
          clearInterval(iv);
          if (btn) btn.disabled = false;
          return;
        }
        if (prog) prog.textContent = p.running ? (p.done + '/' + p.total + ' chats' + (p.full ? ' (full)' : '')) : 'Complete!';
        if (!p.running) {
          clearInterval(iv);
          if (btn) btn.disabled = false;
          setTimeout(() => location.reload(), 1000);
        }
      } catch(e) {
        if (prog) { prog.textContent = 'Error: ' + e.message; prog.style.color = '#ff4757'; }
        clearInterval(iv);
        if (btn) btn.disabled = false;
      }
    }, 1000);
  } catch(e) {
    if (prog) { prog.textContent = 'Connection error: ' + e.message; prog.style.color = '#ff4757'; }
    if (btn) btn.disabled = false;
  }
}

function esc(s) { if(!s)return ''; const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }

init();
</script>
</body>
</html>'''

@app.get('/')
def web_ui():
    return HTMLResponse(HTML)


def main():
    parser = argparse.ArgumentParser(description='PMUD Media Server')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'Port (default: {DEFAULT_PORT})')
    parser.add_argument('--host', default='127.0.0.1', help='Bind address (default: 127.0.0.1)')
    parser.add_argument('--db', default=DEFAULT_DB, help=f'Hoard DB path (default: {DEFAULT_DB})')
    parser.add_argument('--no-reload', action='store_true', help='Disable auto-reload')
    args = parser.parse_args()

    global _hoard_db, _conn
    _hoard_db = args.db

    if os.path.exists(args.db):
        db_size = os.path.getsize(args.db)
        print(f"DB: {args.db} ({db_size/1024/1024:.1f} MB)")
    else:
        print(f"New DB: {args.db}")

    uvicorn.run(
        'api_server:app',
        host=args.host,
        port=args.port,
        reload=not args.no_reload,
        log_level='info',
    )


if __name__ == '__main__':
    main()

