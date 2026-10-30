# Private Media Bot (no database)

Telegram video downloader with automatic media-type detection.

- **Single video:** shows 360p / 720p / 1080p / MP3 selection.
- **Single Instagram photo:** automatically downloads and sends.
- **Instagram carousel or mixed post:** automatically downloads and sends a Telegram album where supported.
- **Access restriction:** set TELEGRAM_ALLOWED_USER_ID to your numeric Telegram user ID; blank = all private chats.
- **No history or database**, no Instagram account and no cookies in the project.
- **Sources:** YouTube and public Instagram. Authentication-required Instagram posts cannot be downloaded anonymously.

## Windows

1. Install Python 3.12+, ffmpeg, and ensure they are in PATH.
2. Copy `.env.example` to `.env` and set `TELEGRAM_BOT_TOKEN`.
3. Run:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python bot.py
```

## Docker

```bash
cp .env.example .env
# Fill TELEGRAM_BOT_TOKEN and optional TELEGRAM_ALLOWED_USER_ID
docker compose up -d --build
docker compose logs -f bot
```

## Operational warnings

- This is a starter integration, **not a production-scale public bot**: it allows one download at a time and has no per-user rate limits. Don't expose publicly at high traffic without a job queue and quotas.
- Instagram blocks many anonymous requests, including public posts; OmniDL does not bypass authentication. Live behavior depends on Instagram and the extractor versions.
- Media upload is capped below the hosted Telegram Bot API 50 MB limit.
- Temporary files are removed at the end of the download; no download history is saved.
- Keep .env private. Group chats are ignored.
- Use only for content you own or have permission to download.
- OmniDL source adapted from https://github.com/DzarelDeveloper/OmniDL. No license was declared when inspected; review licensing before redistributing or commercial use.
- No live end-to-end media download has been verified in this environment.

## Testing

```bash
python -m compileall -q bot.py omni_instagram.py
python -m pip check
```
