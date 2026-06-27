# pmuddumper

Telegram media dumper + gallery viewer. Bypasses "Protect Content" restrictions on groups/channels.

## Usage

```bash
# Install
pip install -r requirements.txt

# Start the server (logs into Telegram, serves web UI)
python api_server.py --no-reload

# Open http://localhost:8765
```

## Files

- `api_server.py` — Self-contained server (Telegram client + web UI + gallery)
- `tg_dumper.py` — CLI media downloader (original tool)
- `daemon.py` — Background message hoarder (optional)

## Setup

1. Run `python api_server.py --no-reload`
2. First-time auth: enter phone number + Telegram code
3. Click "Sync" to populate chats
4. Select a chat → browse photos with lightbox viewer
5. Click any photo to open with prev/next navigation and download

## Notes

- **Not for Vercel** — requires persistent storage and a long-running process
- Session is saved to `daemon_session.session` after first auth
- Thumbnails download on-demand (only when you view them)
