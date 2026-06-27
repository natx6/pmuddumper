import os
import sys
import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, errors

# =====================================================================
# GLOBAL CONFIGURATION STATE MATRIX (With Saved Chat Bookmarks)
# =====================================================================
STATE = {
    'api_id': int(os.getenv('TG_API_ID', 30172652)),
    'api_hash': os.getenv('TG_API_HASH', '4b388db2483b7268e5e1dccf8f429c3a'),
    'phone_number': '',
    'chat_id': 'https://t.me/+dmUe833pzERmZTA0',
    'ignore_stickers': True,
    'ignore_voice': True,
    'base_output_dir': 'telegram_dump',
    'progress_file': 'last_message_id.txt',
    'db_metadata_file': 'media_manifest.db',  # SQLite Deduplication Asset Ledger

    'saved_chats': [
        {'link': 'https://t.me/+_0oTTsOoy8JjZThh', 'nickname': 'Main Dump Group'},
        {'link': '@durov', 'nickname': 'Durov Channel'},
        {'link': 'https://t.me/TelegramTips', 'nickname': 'Official Tips'},
        {'link': 'https://t.me/+dmUe833pzERmZTA0', 'nickname': 'Active Target Group'}
    ]
}

USER_NAME_CACHE = {}

class EscapeException(Exception):
    """Custom exception raised when user inputs 0 to go back (USSD Style)."""
    pass

# =====================================================================
# METADATA & DEDUPLICATION LEDGER MANAGEMENT LAYER
# =====================================================================
class DeduplicationLedger:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        # WAL mode allows reads during writes — faster for concurrent access
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_db()

    def init_db(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS asset_ledger (
                telegram_msg_id INTEGER,
                chat_identity TEXT,
                unique_file_id TEXT PRIMARY KEY,
                file_type TEXT,
                local_disk_path TEXT,
                downloaded_at TEXT
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_chat ON asset_ledger (telegram_msg_id, chat_identity);")
        self.conn.commit()

    def check_duplicate(self, unique_file_id, msg_id, chat_link):
        """Checks both unique telegram file identifiers and redundant trace targets."""
        cursor = self.conn.cursor()
        if unique_file_id:
            cursor.execute("SELECT local_disk_path FROM asset_ledger WHERE unique_file_id = ?", (unique_file_id,))
            row = cursor.fetchone()
            if row: return row[0]

        cursor.execute("SELECT local_disk_path FROM asset_ledger WHERE telegram_msg_id = ? AND chat_identity = ?", (msg_id, chat_link))
        row = cursor.fetchone()
        return row[0] if row else None

    def log_asset(self, msg_id, chat_link, unique_file_id, file_type, disk_path):
        try:
            self.conn.execute("""
                INSERT OR REPLACE INTO asset_ledger
                (telegram_msg_id, chat_identity, unique_file_id, file_type, local_disk_path, downloaded_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (msg_id, chat_link, unique_file_id, file_type, disk_path, datetime.now().isoformat()))
            self.conn.commit()
        except sqlite3.Error as e:
            print(f"⚠️ Metadata log warning: {e}")

# Initialize global tracking ledger
LEDGER = DeduplicationLedger(STATE['db_metadata_file'])

def clear_screen():
    print("\033[2J\033[H", end="")

async def ask_input(prompt):
    try:
        raw_input = await asyncio.get_event_loop().run_in_executor(None, input, prompt)
        cleaned = raw_input.strip()
        if cleaned == '0':
            raise EscapeException()
        return raw_input
    except (KeyboardInterrupt, EOFError):
        print("\n\nProcess terminated by operator.")
        sys.exit(0)

def get_media_subfolder(message):
    if message.photo: return 'Photos'
    if message.video: return 'Videos'
    if message.gif: return 'GIFs'
    if message.audio: return 'Audio'
    if message.document: return 'Documents'
    return 'Other'

def get_last_message_id():
    if os.path.exists(STATE['progress_file']):
        with open(STATE['progress_file'], 'r') as f:
            content = f.read().strip()
            return int(content) if content.isdigit() else 0
    return 0

def save_last_message_id(msg_id):
    with open(STATE['progress_file'], 'w') as f:
        f.write(str(msg_id))

async def resolve_user_folder_name(client, uid):
    if not uid or uid == "Anonymous":
        return "Anonymous"
    if uid in USER_NAME_CACHE:
        return USER_NAME_CACHE[uid]

    display_name = f"User_{uid}"
    try:
        entity = await client.get_entity(uid)
        if getattr(entity, 'username', None):
            display_name = f"@{entity.username}"
        elif getattr(entity, 'first_name', None):
            safe_name = "".join(c for c in entity.first_name if c.isalnum() or c in (' ', '_', '-')).strip()
            if safe_name:
                display_name = safe_name
    except Exception:
        pass

    USER_NAME_CACHE[uid] = display_name
    return display_name

# =====================================================================
# QUICK CHAT SWITCHER INTERFACE MODULE (INSIDE SETTINGS SUB-MENU)
# =====================================================================
async def run_chat_switcher():
    while True:
        for i, item in enumerate(STATE['saved_chats']):
            if isinstance(item, str):
                STATE['saved_chats'][i] = {'link': item, 'nickname': ''}

        clear_screen()
        print("======================================================================")
        print("                     TARGET CHAT REGISTRY MATRIX                      ")
        print("======================================================================")

        current_nickname = next((x['nickname'] for x in STATE['saved_chats'] if x['link'] == STATE['chat_id']), '')
        target_display = f"{current_nickname} ({STATE['chat_id']})" if current_nickname else STATE['chat_id']
        print(f" 🔥 CURRENT ACTIVE TARGET: {target_display}")
        print(" ----------------------------------------------------------------------")
        print(" SAVED SHORTCUT CHANNELS / GROUPS:")

        for idx, item in enumerate(STATE['saved_chats'], 1):
            marker = "➔ " if item['link'] == STATE['chat_id'] else "  "
            if item['nickname']:
                print(f" {marker}[{idx}] {item['nickname']} ({item['link']})")
            else:
                print(f" {marker}[{idx}] {item['link']}")

        print(" ----------------------------------------------------------------------")
        print(" [A] Add New Chat Link to Shortcuts Array")
        print(" [N] Assign/Modify Shortcut Nickname Label")
        print(" [D] Delete a Chat Link from Shortcuts Array")
        print(" [0] Return to Profile Manager Dashboard")
        print("======================================================================")

        choice = await ask_input("Select a number to switch target, or entry configuration: ")
        choice_clean = choice.strip().lower()

        if choice_clean == 'a':
            new_chat = await ask_input("\nEnter Telegram Channel/Group Target link or @Username (0 to cancel): ")
            if new_chat:
                new_chat = new_chat.strip()
                if not any(x['link'] == new_chat for x in STATE['saved_chats']):
                    new_nick = await ask_input("Enter optional nickname descriptive label (Or Enter to leave blank): ")
                    STATE['saved_chats'].append({'link': new_chat, 'nickname': new_nick.strip()})
                STATE['chat_id'] = new_chat
                print(f"\n✓ Added and targeted successfully.")
                await asyncio.sleep(1.2)

        elif choice_clean == 'n':
            nick_idx = await ask_input("\nEnter index number [1-N] to assign nickname to (0 to cancel): ")
            if nick_idx.isdigit():
                idx_to_nick = int(nick_idx) - 1
                if 0 <= idx_to_nick < len(STATE['saved_chats']):
                    target_item = STATE['saved_chats'][idx_to_nick]
                    current_lbl = target_item['nickname'] if target_item['nickname'] else "None"
                    print(f"\nTargeting Shortcut: {target_item['link']}")
                    print(f"Current Nickname Label: {current_lbl}")
                    assigned_nick = await ask_input("Enter new nickname identity label (0 to cancel): ")
                    target_item['nickname'] = assigned_nick.strip()
                    print("\n✓ Shortcut label catalog parameters updated.")
                    await asyncio.sleep(1.2)

        elif choice_clean == 'd':
            del_idx = await ask_input("\nEnter index number [1-N] to delete (0 to cancel): ")
            if del_idx.isdigit():
                idx_to_remove = int(del_idx) - 1
                if 0 <= idx_to_remove < len(STATE['saved_chats']):
                    removed = STATE['saved_chats'].pop(idx_to_remove)
                    print(f"\n✓ Removed shortcut: {removed['link']}")
                    if STATE['chat_id'] == removed['link']:
                        STATE['chat_id'] = STATE['saved_chats'][0]['link'] if STATE['saved_chats'] else ""
                    await asyncio.sleep(1.2)

        elif choice.isdigit():
            target_idx = int(choice) - 1
            if 0 <= target_idx < len(STATE['saved_chats']):
                STATE['chat_id'] = STATE['saved_chats'][target_idx]['link']
                print(f"\n🚀 Target chat switched to: {STATE['chat_id']}")
                await asyncio.sleep(1)
                break

# =====================================================================
# UPGRADED FEATURE 1: DUMP INFRASTRUCTURE WITH DEDUPLICATION SYSTEM
# =====================================================================
async def run_media_dump(client, chat, specific_user=None, min_date=None, max_date=None, standard_resume=False):
    clear_screen()
    print("======================================================================")
    print("           LIVE HARVESTER DASHBOARD ACTIVATED (DEDUP RUNNING)         ")
    print("======================================================================")
    print("💡 Tip: Relational SQLite Ledger is checking for unique file footprints.")
    print("======================================================================")

    kwargs = {'entity': chat}
    if specific_user: kwargs['from_user'] = specific_user
    if max_date: kwargs['offset_date'] = max_date

    if standard_resume:
        last_id = get_last_message_id()
        if last_id > 0:
            kwargs['offset_id'] = last_id
            print(f"🔄 Resuming from historical checkpoint ID: {last_id}")

    iterator = client.iter_messages(**kwargs)
    downloaded, skipped, ignored = 0, 0, 0

    # Semaphore limits concurrent downloads to avoid hammering Telegram
    download_sem = asyncio.Semaphore(3)

    async def _download_one(message, full_file_path):
        """Download a single media file, respecting the concurrency limit."""
        async with download_sem:
            try:
                path = await client.download_media(message, file=full_file_path)
                return path
            except errors.FloodWaitError as e:
                print(f"⚠️ Rate limit hit! Sleeping for {e.seconds}s...")
                await asyncio.sleep(e.seconds)
                return await client.download_media(message, file=full_file_path)
            except Exception as e:
                print(f"❌ Error downloading file ID {message.id}: {e}")
                return None

    # Progress tracking: buffer writes to disk to avoid fsync on every message
    latest_msg_id = 0
    progress_counter = 0
    PROGRESS_FLUSH_INTERVAL = 50

    try:
        # Collect downloads and fire them concurrently in small batches
        pending_batch = []

        async for message in iterator:
            if min_date and message.date < min_date:
                break

            if message.media:
                if (STATE['ignore_stickers'] and message.sticker) or (STATE['ignore_voice'] and message.voice):
                    ignored += 1
                    if standard_resume:
                        latest_msg_id = message.id
                        progress_counter += 1
                    continue

                # Fetch unique hash fingerprint from Telegram document/photo layers if available
                uniq_id = None
                if getattr(message.media, 'document', None):
                    uniq_id = f"doc_{message.media.document.id}"
                elif getattr(message.media, 'photo', None):
                    uniq_id = f"photo_{message.media.photo.id}"

                # Matrix Check: Does this asset exist locally or across channel records?
                duplicate_match_path = LEDGER.check_duplicate(uniq_id, message.id, STATE['chat_id'])
                if duplicate_match_path:
                    skipped += 1
                    if standard_resume:
                        latest_msg_id = message.id
                        progress_counter += 1
                    continue

                date_folder = message.date.strftime('%Y-%m')
                sender_id = message.sender_id if message.sender_id else "Anonymous"
                user_folder = await resolve_user_folder_name(client, sender_id)
                sub_folder = get_media_subfolder(message)

                target_dir = os.path.join(STATE['base_output_dir'], date_folder, user_folder, sub_folder)
                os.makedirs(target_dir, exist_ok=True)

                ext = message.file.ext if (message.file and message.file.ext) else '.jpg'
                filename = f"{message.id}_{message.file.name}" if (message.file and message.file.name) else f"{message.id}{ext}"
                full_file_path = os.path.join(target_dir, filename)

                if os.path.exists(full_file_path):
                    LEDGER.log_asset(message.id, STATE['chat_id'], uniq_id, sub_folder, full_file_path)
                    skipped += 1
                    if standard_resume:
                        latest_msg_id = message.id
                        progress_counter += 1
                    continue

                print(f"🚀 [DL] File ID: {message.id} | Type: {sub_folder} | From: {user_folder}")

                # Queue download — don't await yet
                pending_batch.append((message, full_file_path, sub_folder, uniq_id))

                # Fire batch when we have enough queued
                if len(pending_batch) >= 3:
                    results = await asyncio.gather(*[
                        _download_one(msg, path)
                        for msg, path, _, _ in pending_batch
                    ])
                    for (msg, path, sub, uid), result in zip(pending_batch, results):
                        if result:
                            downloaded += 1
                            LEDGER.log_asset(msg.id, STATE['chat_id'], uid, sub, path)
                    pending_batch = []

            if standard_resume:
                latest_msg_id = message.id
                progress_counter += 1
                if progress_counter % PROGRESS_FLUSH_INTERVAL == 0:
                    save_last_message_id(latest_msg_id)

        # Flush any remaining queued downloads
        if pending_batch:
            results = await asyncio.gather(*[
                _download_one(msg, path)
                for msg, path, _, _ in pending_batch
            ])
            for (msg, path, sub, uid), result in zip(pending_batch, results):
                if result:
                    downloaded += 1
                    LEDGER.log_asset(msg.id, STATE['chat_id'], uid, sub, path)
            pending_batch = []

    except Exception as e:
        print(f"\n❌ Scraping interrupted: {e}")
    finally:
        # Save final progress position so we can resume from here
        if standard_resume and latest_msg_id:
            save_last_message_id(latest_msg_id)

    print("\n======================================================================")
    print(f" DUMP COMPLETE | Saved: {downloaded} | Skipped (Deduped): {skipped} | Ignored: {ignored}")
    print("======================================================================")
    try:
        await ask_input("\nPress 0 to return to main menu...")
    except EscapeException:
        pass

# =====================================================================
# FEATURE 2: DAILY & DATE-RANGE ANALYTICS
# =====================================================================
async def run_date_analytics(client, chat):
    clear_screen()
    print("======================================================================")
    print("                      DAILY METADATA ANALYTICS                        ")
    print("======================================================================")
    print("Format required: YYYY-MM-DD (Example: 2026-06-25) | Type 0 to cancel")
    date_input = await ask_input("➔ Enter target date (or press Enter for today): ")

    if not date_input:
        target_date = datetime.now(timezone.utc)
    else:
        try:
            target_date = datetime.strptime(date_input.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print("❌ Invalid date format configuration.")
            await asyncio.sleep(2)
            return

    start_window = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
    end_window = start_window + timedelta(days=1)

    print(f"\nScanning indexes for {start_window.date()}... (Instantly fetching metadata...)")

    metrics = {'Photos': 0, 'Videos': 0, 'GIFs': 0, 'Audio': 0, 'Documents': 0, 'Other': 0, 'Ignored': 0}

    async for message in client.iter_messages(chat, offset_date=end_window):
        if message.date < start_window:
            break

        if message.media:
            if (STATE['ignore_stickers'] and message.sticker) or (STATE['ignore_voice'] and message.voice):
                metrics['Ignored'] += 1
                continue
            sub = get_media_subfolder(message)
            metrics[sub] += 1

    total_media = sum(metrics.values()) - metrics['Ignored']

    print(f"\n📊 RESULTS FOR {start_window.date()}:")
    print(f" 📸 Photos:     {metrics['Photos']}")
    print(f" 🎥 Videos:     {metrics['Videos']}")
    print(f" 🎞️ GIFs:       {metrics['GIFs']}")
    print(f" 🎵 Audio:      {metrics['Audio']}")
    print(f" 📄 Documents:  {metrics['Documents']}")
    print(f" 📦 Others:     {metrics['Other']}")
    print(f" 🚫 Stickers/Voice Ignored: {metrics['Ignored']}")
    print(f" ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f" Total Downloadable items: {total_media}")

    if total_media > 0:
        choice = await ask_input("\nHarvest this specific day right now? (y/n or 0 to go back): ")
        if choice.lower().strip() == 'y':
            await run_media_dump(client, chat, min_date=start_window, max_date=end_window)
    else:
        try:
            await ask_input("\nNo downloadable assets found. Press 0 to return...")
        except EscapeException:
            pass

# =====================================================================
# FEATURE 3: LEADERBOARD CONTRIBUTORS
# =====================================================================
async def run_leaderboard(client, chat):
    clear_screen()
    print("======================================================================")
    print("                     TOP MEDIA CONTRIBUTORS LEADERBOARD               ")
    print("======================================================================")
    limit_input = await ask_input("How many historical messages to scan? (Default 500 / 0 to cancel): ")
    limit = int(limit_input) if limit_input.isdigit() else 500

    print(f"\nCrunching ledger matrix for the last {limit} messages inside group...")
    leaderboard = {}

    async for message in client.iter_messages(chat, limit=limit):
        if message.media:
            if (STATE['ignore_stickers'] and message.sticker) or (STATE['ignore_voice'] and message.voice):
                continue
            uid = message.sender_id if message.sender_id else "Anonymous"
            sub = get_media_subfolder(message)

            if uid not in leaderboard:
                leaderboard[uid] = {'Photos': 0, 'Videos': 0, 'Total': 0}

            leaderboard[uid]['Total'] += 1
            if sub in ['Photos', 'Videos']:
                leaderboard[uid][sub] += 1

    if not leaderboard:
        print("No active media distribution accounts found in sample size.")
        try:
            await ask_input("\nPress 0 to return...")
        except EscapeException:
            pass
        return

    sorted_board = sorted(leaderboard.items(), key=lambda x: x[1]['Total'], reverse=True)[:10]

    print("\n🔍 Resolving structural identity profiles from Telegram directory...")
    print("🏆 TOP 10 MEDIA DISTRIBUTORS IN SCANNED DEPTH:")
    print(f" {'Rank':<5} | {'Identity / Handle Name':<25} | {'Photos':<8} | {'Videos':<8} | {'Total Media':<10}")
    print(" -------------------------------------------------------------------------------")

    user_mapping = {}
    for idx, (uid, stats) in enumerate(sorted_board, 1):
        user_mapping[idx] = uid
        display_identity = await resolve_user_folder_name(client, uid)
        print(f" [{idx}]   | {display_identity:<25} | {stats['Photos']:<8} | {stats['Videos']:<8} | {stats['Total']:<10}")

    print("\n💡 Targeted Action Shortcut:")
    action = await ask_input("Type a Rank Number [1-10] to harvest ONLY that user, or 0 to go back: ")

    if action.isdigit() and int(action) in user_mapping:
        selected_uid = user_mapping[int(action)]
        print(f"\nTarget locked on User ID {selected_uid}. Initiating download sequence...")
        await asyncio.sleep(1.5)
        await run_media_dump(client, chat, specific_user=selected_uid)

# =====================================================================
# FEATURE 4: FULL CORE CREDENTIALS PROFILE MANAGER
# =====================================================================
async def run_settings():
    while True:
        clear_screen()
        print("======================================================================")
        print("                     CONFIGURATION PROFILE MANAGER                   ")
        print("======================================================================")
        print("📁 TARGET PARAMETERS:")

        current_nickname = next((x['nickname'] for x in STATE['saved_chats'] if x['link'] == STATE['chat_id']), '')
        target_display = f"{current_nickname} ({STATE['chat_id']})" if current_nickname else STATE['chat_id']

        print(f" [1] Target Chat Registry Config  (Active: {target_display})")
        print(f" [2] Skip/Ignore Stickers         : {STATE['ignore_stickers']}")
        print(f" [3] Skip/Ignore Voice            : {STATE['ignore_voice']}")
        print(f" [4] Local Root Save Path         : {STATE['base_output_dir']}/")
        print("----------------------------------------------------------------------")
        print("🔐 TELEGRAM CORE IDENTITY CREDENTIALS:")
        print(f" [5] API App ID Key               : {STATE['api_id'] if STATE['api_id'] else '[NOT SET]'}")
        print(f" [6] API Application Hash         : {STATE['api_hash'] if STATE['api_hash'] else '[NOT SET]'}")
        print(f" [7] Target Phone Number          : {STATE['phone_number'] if STATE['phone_number'] else '[NOT SET]'}")
        print("----------------------------------------------------------------------")
        print(" [0] Save Environment Parameters & Return to Terminal Main Menu")
        print("======================================================================")

        try:
            choice = await ask_input("Select target entry line to adjust (0-7): ")
        except EscapeException:
            break

        if choice == '1':
            try:
                await run_chat_switcher()
            except EscapeException:
                pass
        elif choice == '2':
            STATE['ignore_stickers'] = not STATE['ignore_stickers']
        elif choice == '3':
            STATE['ignore_voice'] = not STATE['ignore_voice']
        elif choice == '4':
            new_dir = await ask_input("\nEnter root directory storage destination (0 to cancel): ")
            if new_dir: STATE['base_output_dir'] = new_dir.strip()
        elif choice == '5':
            new_id = await ask_input("\nEnter your 7-8 Digit Telegram API App ID (0 to cancel): ")
            if new_id.isdigit(): STATE['api_id'] = int(new_id.strip())
        elif choice == '6':
            new_hash = await ask_input("\nEnter your 32-character Telegram API App Hash string (0 to cancel): ")
            if new_hash: STATE['api_hash'] = new_hash.strip()
        elif choice == '7':
            new_phone = await ask_input("\nEnter account number string with Int Code (e.g. +1234567890) (0 to cancel): ")
            if new_phone:
                STATE['phone_number'] = new_phone.strip()
                if os.path.exists('interactive_session.session'):
                    try: os.remove('interactive_session.session')
                    except Exception: pass

# =====================================================================
# SYSTEM CORE RUNTIME LOOP INTERFACE
# =====================================================================
async def main():
    while True:
        if not STATE['api_id'] or not STATE['api_hash']:
            clear_screen()
            print("======================================================================")
            print("                  💥 CRITICAL BOOT CONFIGURATION MISSING              ")
            print("======================================================================")
            print(" Telegram Engine infrastructure requires manual API values to register.")
            print(" Please populate your core keys inside the settings layout panel now.\n")
            print(" Complete values on my.telegram.org")
            print("======================================================================")
            try:
                await ask_input("Press Enter to access Configuration Profile Manager...")
                await run_settings()
            except EscapeException:
                pass
            continue

        print("\nEstablishing active uplink connection to Telegram master node array...")
        client = TelegramClient('interactive_session', STATE['api_id'], STATE['api_hash'])

        try:
            phone_arg = STATE['phone_number'] if STATE['phone_number'] else None
            await client.start(phone=lambda: phone_arg if phone_arg else input("Enter Phone Number: "))

            while True:
                clear_screen()
                print("======================================================================")
                print("                 PMUD TERMINAL MEDIA HARVEST ENGINE v4.5             ")
                print("======================================================================")

                current_nickname = next((x['nickname'] for x in STATE['saved_chats'] if x['link'] == STATE['chat_id']), '')
                target_display = f"{current_nickname} ({STATE['chat_id']})" if current_nickname else STATE['chat_id']

                print(f" Active Target Chat : {target_display}")
                print(f" Phone Uplink Target: {STATE['phone_number'] if STATE['phone_number'] else '[Default Profile Run]'}")
                print(f" Media Filters      : Voice/Stickers -> {('FILTERED' if STATE['ignore_stickers'] else 'DOWNLOADING')}")
                print(" ----------------------------------------------------------------------")
                print(" [1] 📥 Run Standard/Resume Media Dump (Ledger Tracking Active)")
                print(" [2] 📅 Scan Analytics and Media Density for Specific Date")
                print(" [3] 🏆 Generate Group Top Contributors Media Leaderboard Chart")
                print(" [4] ⚙️  Open Full Configuration Profile Manager Dashboard")
                print(" [0] ❌ Terminate Session and Close Connection")
                print("======================================================================")

                try:
                    choice = await ask_input("Enter your desired operating choice selection (0-4): ")
                except EscapeException:
                    choice = '0'

                if choice == '0':
                    print("\nSession safely closed. Goodbye!")
                    await client.disconnect()
                    return

                if choice != '4':
                    try:
                        chat = await client.get_input_entity(STATE['chat_id'])
                    except Exception as e:
                        print(f"\n❌ Exception mapping Target '{STATE['chat_id']}': {e}")
                        print("Use Option [4] to access Configuration Profile Manager to switch chats.")
                        try: await ask_input("\nPress 0 to clear alert buffer...")
                        except EscapeException: pass
                        continue

                try:
                    if choice == '1':
                        await run_media_dump(client, chat, standard_resume=True)
                    elif choice == '2':
                        await run_date_analytics(client, chat)
                    elif choice == '3':
                        await run_leaderboard(client, chat)
                    elif choice == '4':
                        await run_settings()
                        break
                except EscapeException:
                    print("\n➔ Operation aborted. Returning to main engine terminal...")
                    await asyncio.sleep(1)

        except Exception as e:
            print(f"\n💥 Core uplink crash event intercepted: {e}")
            try:
                await ask_input("\nPress 0 to re-initialize configuration profile state stack...")
            except EscapeException:
                pass
        finally:
            await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nProcess safely closed.")
