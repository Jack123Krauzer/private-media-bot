"""Telegram downloader: video quality prompt; photos/carousels delivered automatically.
No database. Optional owner-only mode. Public media only; no account cookies.
"""
import asyncio
import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from yt_dlp import YoutubeDL
from omni_instagram import OmniInstagramIE

load_dotenv()
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
_owner = os.getenv("TELEGRAM_ALLOWED_USER_ID", "").strip()
OWNER_ID = int(_owner) if _owner else None
MAX_ITEMS = min(10, max(1, int(os.getenv("MAX_ITEMS", "10"))))
MAX_BYTES = min(49, max(1, int(os.getenv("MAX_UPLOAD_MB", "49")))) * 1024 * 1024
TIMEOUT = max(20, int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "240")))
IMAGE = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO = {".mp4", ".mkv", ".webm", ".mov"}
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
         "youtu.be", "instagram.com", "www.instagram.com"}
URL_RE = re.compile(r"https?://[^\s<>]+", re.I)
LOG = logging.getLogger("media-bot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
PENDING = {}
LOCK = asyncio.Lock()


def authorized(update):
    u, c = update.effective_user, update.effective_chat
    return bool(u and c and c.type == "private" and (OWNER_ID is None or u.id == OWNER_ID))


def url_from_text(text):
    for match in URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(".,;!?)]")
        try:
            parsed = urlsplit(candidate)
            if (parsed.scheme == "https" and parsed.hostname in HOSTS and
                not parsed.username and not parsed.password and
                parsed.port in (None, 443) and parsed.path not in ("", "/")):
                return candidate
        except ValueError:
            pass
    return None


def instagram(url):
    return urlsplit(url).hostname in {"instagram.com", "www.instagram.com"}


def run(cmd, cwd=None, timeout=TIMEOUT):
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise RuntimeError(f"Executable missing: {cmd[0]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Download timed out") from e
    if result.returncode:
        lines = (result.stderr or result.stdout or "").strip().splitlines()
        raise RuntimeError((lines[-1] if lines else "Extractor error")[:180])
    return result.stdout


def inspect(url):
    """Inspect metadata. A gallery means >=2 entries or a single image."""
    try:
        with YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                         "extract_flat": False, "noplaylist": not instagram(url),
                         "playlistend": MAX_ITEMS + 1, "socket_timeout": 20,
                         "retries": 1, "cachedir": False}) as ydl:
            info = OmniInstagramIE(ydl).extract(url) if instagram(url) else ydl.extract_info(url, download=False)
        if not info:
            raise RuntimeError("No media found")
        entries = info.get("entries")
        if entries is not None:
            entries = [e for e in entries if e]
            if len(entries) > MAX_ITEMS:
                raise RuntimeError("Too many carousel entries")
            if len(entries) > 1:
                return "gallery"
            if entries:
                info = entries[0]
        ext = str(info.get("ext") or "").lower().lstrip(".")
        formats = info.get("formats") or []
        if ext in {"jpg", "jpeg", "png", "webp", "avif"}:
            return "gallery"
        if formats and all(str(f.get("ext", "")).lower() in {"jpg","jpeg","png","webp","avif"} for f in formats):
            return "gallery"
        return "video"
    except Exception as initial:
        if not instagram(url):
            raise RuntimeError(f"Media inspection failed: {str(initial)[:120]}") from initial
        try:
            raw = run(["gallery-dl", "--config-ignore", "--dump-json",
                       "--range", f"1-{MAX_ITEMS+1}", url], timeout=min(TIMEOUT, 65))
            entries = []
            for line in raw.splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, list) and len(row) >= 3 and row[0] == 3:
                        entries.append(row)
                except json.JSONDecodeError:
                    continue
            if not entries:
                raise RuntimeError("No media metadata")
            if len(entries) > MAX_ITEMS:
                raise RuntimeError("Too many carousel entries")
            if len(entries) > 1:
                return "gallery"
            link, metadata = str(entries[0][1]), entries[0][2] or {}
            ext = str(metadata.get("extension") or link.split("?")[0].rsplit(".", 1)[-1]).lower()
            return "gallery" if ext in {"jpg","jpeg","png","webp","avif"} else "video"
        except Exception as e:
            raise RuntimeError("Instagram metadata unavailable anonymously; post may require login") from e


def download(url, mode, folder):
    if instagram(url):
        # Gallery mode tries gallery-dl first to preserve carousel order and images.
        if mode == "gallery":
            try:
                run(["gallery-dl", "--config-ignore", "--directory", str(folder),
                     "--range", f"1-{MAX_ITEMS}", "--no-mtime", url], cwd=folder)
            except RuntimeError:
                # OmniDL adapter still offers a fallback for images when possible.
                instagram_download(url, mode, folder)
        else:
            try:
                instagram_download(url, mode, folder)
            except RuntimeError:
                run(["gallery-dl", "--config-ignore", "--directory", str(folder),
                     "--range", f"1-{MAX_ITEMS}", "--no-mtime", url], cwd=folder)
                if mode == "mp3":
                    videos = [f for f in folder.rglob("*") if f.suffix.lower() in VIDEO]
                    if not videos:
                        raise RuntimeError("No audio-bearing video found")
                    run(["ffmpeg","-nostdin","-y","-hide_banner","-loglevel","error",
                         "-i",str(videos[0]),"-vn","-q:a","5",str(folder/"audio.mp3")], cwd=folder)
                    for video in videos:
                        video.unlink()
    else:
        if mode == "gallery":
            raise RuntimeError("Photo gallery not supported for this platform")
        out = str(folder / "%(id)s.%(ext)s")
        args = ["yt-dlp","--no-config","--no-playlist","--no-progress",
                "--no-write-info-json","--no-write-thumbnail","--no-write-subs",
                "--max-filesize",f"{MAX_BYTES//1048576}M","--output",out]
        if mode == "mp3":
            args += ["--extract-audio","--audio-format","mp3","--audio-quality","5"]
        else:
            height = int(mode)
            args += ["--format",f"bv*[height<={height}]+ba/b[height<={height}]/best[height<={height}]",
                     "--merge-output-format","mp4"]
        run(args + [url], cwd=folder)
    files = sorted((p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE | VIDEO | {".mp3"}),key=lambda p:str(p))
    if not files:
        raise RuntimeError("No supported media returned")
    if len(files) > MAX_ITEMS:
        raise RuntimeError(f"Exceeded maximum of {MAX_ITEMS} items")
    if any(p.stat().st_size > MAX_BYTES for p in files):
        raise RuntimeError("Media too large for hosted Telegram Bot API")
    return files


def instagram_download(url, mode, folder):
    # yt-dlp's protected extension points retain Instagram image URLs.
    import yt_dlp
    options = {
        "outtmpl": str(folder / "%(playlist_index|0)s-%(id)s.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": mode != "gallery",
        "playlistend": MAX_ITEMS, "cachedir": False, "socket_timeout": 20,
        "max_filesize": MAX_BYTES,
    }
    if mode == "mp3":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"5"}]
    elif mode in {"360","720","1080"}:
        options["format"] = f"best[height<={int(mode)}]/best"
    with YoutubeDL(options) as ydl:
        info = OmniInstagramIE(ydl).extract(url)
        # Download each result with the YoutubeDL instance so image URL mapping is respected.
        if info.get("entries"):
            for entry in list(info["entries"])[:MAX_ITEMS]:
                if entry:
                    ydl.process_ie_result(entry, download=True)
        else:
            ydl.process_ie_result(info, download=True)


async def deliver(message, files):
    # Album: 2-10 small Telegram-compatible media files.
    eligible = 2 <= len(files) <= 10 and all(
        f.suffix.lower() in IMAGE | {".mp4"} and f.stat().st_size < 9*1024*1024
        for f in files
    )
    if eligible:
        handles = [p.open("rb") for p in files]
        try:
            media = [InputMediaPhoto(h) if p.suffix.lower() in IMAGE
                     else InputMediaVideo(h, supports_streaming=True)
                     for p,h in zip(files,handles)]
            await message.reply_media_group(media, read_timeout=120, write_timeout=120)
            return
        except Exception:
            LOG.exception("Album upload failed; trying individual documents")
        finally:
            for handle in handles:
                handle.close()
    for path in files:
        with path.open("rb") as handle:
            if path.suffix.lower() in IMAGE and path.stat().st_size < 9*1024*1024:
                try:
                    await message.reply_photo(handle, read_timeout=120, write_timeout=120)
                    continue
                except Exception:
                    handle.seek(0)
            if path.suffix.lower() == ".mp3":
                await message.reply_audio(handle, filename=path.name, read_timeout=120, write_timeout=120)
            else:
                await message.reply_document(handle, filename=path.name, read_timeout=120, write_timeout=120)


async def execute(message, url, mode):
    if LOCK.locked():
        await message.reply_text("Another download is in progress. Try again shortly.")
        return
    async with LOCK:
        status = await message.reply_text("Downloading…")
        try:
            with tempfile.TemporaryDirectory(prefix="media-bot-") as temp:
                files = await asyncio.to_thread(download, url, mode, Path(temp))
                await status.edit_text(f"Sending {len(files)} item(s)…")
                await deliver(message, files)
            await status.edit_text("Done.")
        except Exception as e:
            LOG.warning("Download failed: %s", type(e).__name__)
            await status.edit_text(f"Download failed: {str(e)[:250]}")


async def start(update, context):
    if authorized(update):
        await update.message.reply_text(
            "Send an Instagram or YouTube URL. Photos/carousels download automatically; "
            "single videos offer 360p / 720p / 1080p / MP3.")


async def incoming(update, context):
    if not authorized(update):
        return
    url = url_from_text(update.message.text)
    if not url:
        return await update.message.reply_text("Send a supported HTTPS Instagram or YouTube URL.")
    status = await update.message.reply_text("Checking media type…")
    try:
        kind = await asyncio.to_thread(inspect, url)
    except Exception as e:
        return await status.edit_text(str(e)[:300])
    if kind == "gallery":
        await status.delete()
        return await execute(update.message, url, "gallery")
    now = time.monotonic()
    for k, value in list(PENDING.items()):
        if value["expires"] <= now:
            PENDING.pop(k, None)
    key = secrets.token_hex(5)
    PENDING[key] = {"url":url, "user":update.effective_user.id,
                    "chat":update.effective_chat.id,"expires":now+600}
    markup = InlineKeyboardMarkup([
        [InlineKeyboardButton(q+"p", callback_data=f"{key}:{q}") for q in ("360","720","1080")],
        [InlineKeyboardButton("MP3 audio", callback_data=f"{key}:mp3")],
        [InlineKeyboardButton("Cancel", callback_data=f"{key}:cancel")]
    ])
    await status.edit_text("Video detected. Choose quality:", reply_markup=markup)


async def callback(update, context):
    query = update.callback_query
    if not authorized(update):
        return
    key, _, mode = (query.data or "").partition(":")
    item = PENDING.get(key)
    if not item or item["user"] != update.effective_user.id or item["chat"] != update.effective_chat.id or time.monotonic() >= item["expires"]:
        return await query.answer("Selection expired. Resend the URL.", show_alert=True)
    if mode not in ("360","720","1080","mp3","cancel"):
        return await query.answer("Invalid choice")
    PENDING.pop(key, None)
    await query.answer()
    await query.edit_message_text("Cancelled." if mode == "cancel" else f"Selected: {mode}")
    if mode != "cancel":
        await execute(query.message, item["url"], mode)


def main():
    LOG.warning("PUBLIC MODE enabled" if OWNER_ID is None else "Owner-only mode enabled")
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, incoming))
    app.run_polling(drop_pending_updates=True, allowed_updates=["message","callback_query"])


if __name__ == "__main__":
    main()
