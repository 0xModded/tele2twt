import os
import hashlib
import asyncio
import logging
import json
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import tweepy
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
)

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ======= Environment Variables =======
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_USERNAME = os.environ["CHANNEL_USERNAME"].lstrip("@")
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
CONSUMER_KEY = os.environ["CONSUMER_KEY"]
CONSUMER_SECRET = os.environ["CONSUMER_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_SECRET"]
BEARER_TOKEN = os.environ["BEARER_TOKEN"]

HASH_TRACK_FILE = Path("posted_hashes.txt")
LAST_POST_FILE = Path("lastpost.txt")
DB_FILE = "queue.sqlite3"

# ======= SQLite Queue ==========
def db_init():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS queue ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "media_paths TEXT, caption TEXT, scheduled_time INTEGER)"
        )
        conn.commit()
def db_add_queue_item(paths: List[str], caption: str, scheduled_time: int):
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute(
            "INSERT INTO queue (media_paths, caption, scheduled_time) VALUES (?, ?, ?)",
            (json.dumps(paths), caption, scheduled_time)
        ); conn.commit()
def db_get_next_items(n=5):
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "SELECT id, media_paths, caption, scheduled_time FROM queue "
            "ORDER BY scheduled_time ASC LIMIT ?", (n,)
        )
        return cur.fetchall()
def db_pop_due_items():
    now = int(datetime.now(timezone.utc).timestamp())
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.execute(
            "SELECT id, media_paths, caption, scheduled_time FROM queue "
            "WHERE scheduled_time <= ?", (now,)
        )
        rows = cur.fetchall()
        ids = [row[0] for row in rows]
        if ids:
            conn.executemany("DELETE FROM queue WHERE id=?", [(id,) for id in ids])
        conn.commit()
    return rows
def db_clear_queue():
    with sqlite3.connect(DB_FILE) as conn:
        conn.execute("DELETE FROM queue")
        conn.commit()

# ======= Scheduling Parser ==========
def parse_schedule_from_caption(caption: str) -> Optional[int]:
    m = re.search(r"#at\s+([0-9\-: Tt]+)", caption)
    if m:
        timestr = m.group(1).replace('t', 'T').replace('T', ' ')
        try:
            dt = datetime.strptime(timestr.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                dt = datetime.strptime(timestr.strip(), "%Y-%m-%dT%H:%M")
            except Exception:
                return None
        dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    m = re.search(r"#in\s+([0-9]+)\s*m(?:in)?", caption)
    if m:
        mins = int(m.group(1))
        ts = int((datetime.now(timezone.utc) + timedelta(minutes=mins)).timestamp())
        return ts
    m = re.search(r"#in\s+([0-9]+)\s*h(?:our)?", caption)
    if m:
        hrs = int(m.group(1))
        ts = int((datetime.now(timezone.utc) + timedelta(hours=hrs)).timestamp())
        return ts
    return None

def strip_schedule_from_caption(caption: str) -> str:
    # Remove #in 23m, #in 2h, #at 2025-09-17 19:30, etc.
    caption = re.sub(r"#in\s+\d+\s*[mh](in|our)?", "", caption, flags=re.IGNORECASE)
    caption = re.sub(r"#at\s+[0-9\-: Tt]+", "", caption, flags=re.IGNORECASE)
    # Remove any extra whitespace
    return re.sub(r"\s+", " ", caption).strip()


# ======= Twitter/Tweepy Setup ==========
def hash_file(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8192)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

client = tweepy.Client(
    bearer_token=BEARER_TOKEN,
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_SECRET,
)
v1_auth = tweepy.OAuth1UserHandler(CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_SECRET)
api_v1 = tweepy.API(v1_auth)

# ======= Last post info (for /lastpost) ==========
def save_last_post(tweet_url: str, caption: str):
    with open(LAST_POST_FILE, "w", encoding="utf-8") as f:
        f.write(f"{tweet_url}\n{caption}\n")
def load_last_post() -> Optional[tuple]:
    if not LAST_POST_FILE.exists():
        return None
    lines = LAST_POST_FILE.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2:
        return None
    return lines[0], lines[1]

# ======= Pending Approval State ==========
pending_duplicate_approval = {"future": None, "media_info": None}
async def wait_for_approval(context: ContextTypes.DEFAULT_TYPE, media_info, timeout: int = 120) -> bool:
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    pending_duplicate_approval["future"] = fut
    pending_duplicate_approval["media_info"] = media_info
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text="⚠️ Duplicate media detected. Send /ok in the bot's chat within 2 minutes to force-post."
    )
    try:
        await asyncio.wait_for(fut, timeout)
        return True
    except asyncio.TimeoutError:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="⏱️ Duplicate post request timed out; skipping.")
        return False
    finally:
        pending_duplicate_approval["future"] = None
        pending_duplicate_approval["media_info"] = None
async def approve_duplicate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ADMIN_CHAT_ID:
        try:
            await update.effective_message.reply_text("You are not authorized to approve duplicates.")
        except Exception:
            log.info("Could not reply to unauthorized /ok sender.")
        return
    fut = pending_duplicate_approval.get("future")
    if fut and not fut.done():
        fut.set_result(True)
        try:
            await update.effective_message.reply_text("Duplicate approved — posting now.")
        except Exception:
            log.info("Failed to send approval reply to admin.")
        log.info("Duplicate approved by admin.")
    else:
        try:
            await update.effective_message.reply_text("No duplicate is currently awaiting approval.")
        except Exception:
            log.info("Failed to inform admin no duplicate was pending.")
        log.info("Admin issued /ok but nothing pending.")

# ========= Album Buffering ===========
pending_albums = {}
ALBUM_FINALIZE_DELAY = 2.5  # seconds

async def finalize_album(chat_id, group_id, context):
    await asyncio.sleep(ALBUM_FINALIZE_DELAY)
    key = (chat_id, group_id)
    items = pending_albums.pop(key, None)
    if not items:
        return
    caption = items[0]['caption']
    post_time = parse_schedule_from_caption(caption)
    if post_time is None:
        post_time = int(datetime.now(timezone.utc).timestamp())
    filepaths = [i['file'] for i in items]
    clean_caption = strip_schedule_from_caption(caption)
    db_add_queue_item(filepaths, clean_caption, post_time)

    await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
        f"Album queued for {datetime.fromtimestamp(post_time, timezone.utc):%Y-%m-%d %H:%M UTC}")

async def media_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    chat = msg.chat
    if not chat or getattr(chat, "username", None) != CHANNEL_USERNAME:
        return
    caption = msg.caption if getattr(msg, "caption", None) else (msg.text if getattr(msg, "text", None) else "Sent from Telegram")

    async def dl_file(file_obj):
        tg_file = await file_obj.get_file()
        saved = await tg_file.download_to_drive()
        return str(saved)
    # Album handling
    if getattr(msg, "media_group_id", None):
        key = (chat.id, msg.media_group_id)
        file_path = None
        if getattr(msg, "video", None):
            file_path = await dl_file(msg.video)
        elif getattr(msg, "photo", None):
            file_path = await dl_file(msg.photo[-1])
        elif getattr(msg, "document", None):
            file_path = await dl_file(msg.document)
        else:
            return
        pending_albums.setdefault(key, []).append({'file': file_path, 'caption': caption})
        # Start or refresh finalizer task for this group
        if not any(x for x in asyncio.all_tasks() if getattr(x, 'album_key', None) == key):
            t = asyncio.create_task(finalize_album(chat.id, msg.media_group_id, context))
            t.album_key = key  # for deduping
    else:
        file_path = None
        if getattr(msg, "video", None):
            file_path = await dl_file(msg.video)
        elif getattr(msg, "photo", None):
            file_path = await dl_file(msg.photo[-1])
        elif getattr(msg, "document", None):
            file_path = await dl_file(msg.document)
        else:
            return
        post_time = parse_schedule_from_caption(caption)
        if post_time is None:
            post_time = int(datetime.now(timezone.utc).timestamp())
        clean_caption = strip_schedule_from_caption(caption)
        db_add_queue_item([file_path], clean_caption, post_time)

        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"Queued post for {datetime.fromtimestamp(post_time, timezone.utc):%Y-%m-%d %H:%M UTC}")

# ============= Posting Logic (dequeue) ===============
async def process_and_post_media(file_paths: list[str], caption: str, context: ContextTypes.DEFAULT_TYPE):
    hashes = [hash_file(p) for p in file_paths]
    duplicate = any(h in HASH_TRACK_FILE.read_text().splitlines() if HASH_TRACK_FILE.exists() else [] for h in hashes)
    if duplicate:
        approved = await wait_for_approval(context, {"files": file_paths})
        if not approved:
            for f in file_paths:
                try: os.remove(f)
                except Exception: pass
            return

    reply_to_id = None
    tweet_url = None
    try:
        user_id = ACCESS_TOKEN.split('-')[0]
        video_path = next((p for p in file_paths if p.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))), None)
        photo_paths = [p for p in file_paths if p != video_path]
        # Video tweet (if present)
        if video_path:
            try:
                media = api_v1.media_upload(video_path)
                res = client.create_tweet(text=caption if not photo_paths else caption + " (video)", media_ids=[media.media_id_string])
                if getattr(res, "data", None) and res.data.get("id"):
                    reply_to_id = res.data["id"]
                    tweet_url = f"https://twitter.com/{user_id}/status/{reply_to_id}"
                    save_last_post(tweet_url, caption)
                h = hash_file(video_path)
                with open(HASH_TRACK_FILE, "a") as f:
                    f.write(h + "\n")
            except Exception as e:
                log.exception("Failed to post video: %s", e)
        # Photos reply (group)
        if photo_paths:
            media_ids = []
            for path in photo_paths:
                try:
                    media = api_v1.media_upload(path)
                    media_ids.append(media.media_id_string)
                except Exception as e:
                    log.exception("Failed to upload photo %s: %s", path, e)
            if media_ids:
                res = client.create_tweet(
                    text=caption if not video_path else caption + " (photos)",
                    media_ids=media_ids,
                    in_reply_to_tweet_id=reply_to_id
                )
                if getattr(res, "data", None) and res.data.get("id"):
                    thread_url = f"https://twitter.com/{user_id}/status/{res.data['id']}"
                    save_last_post(thread_url, caption)
                for path in photo_paths:
                    h = hash_file(path)
                    with open(HASH_TRACK_FILE, "a") as f:
                        f.write(h + "\n")
        elif not video_path:  # pure text fallback—rare but possible
            try:
                res = client.create_tweet(text=caption)
                if getattr(res, "data", None) and res.data.get("id"):
                    tweet_url = f"https://twitter.com/{user_id}/status/{res.data['id']}"
                    save_last_post(tweet_url, caption)
            except Exception as e:
                log.exception("Failed to post text tweet: %s", e)

        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="✅ Telegram bot: Posted to Twitter successfully!")
    except Exception as e:
        log.exception("Error while posting to Twitter: %s", e)
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"❌ Telegram-to-Twitter Error:\n{e}")
        except Exception:
            pass
    finally:
        for p in file_paths:
            try: os.remove(p)
            except Exception: pass

# ============== Scheduled Poster ==================
async def scheduled_poster(context: ContextTypes.DEFAULT_TYPE):
    to_post = db_pop_due_items()
    for row in to_post:
        id, media_paths, caption, scheduled_time = row
        files = json.loads(media_paths)
        await process_and_post_media(files, caption, context)

# ============== Admin Command Handlers =============
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is up and running.")

async def lastpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = load_last_post()
    if post is None:
        await update.effective_message.reply_text("No post has been made yet.")
        return
    url, caption = post
    await update.effective_message.reply_text(f"Last post:\n{url}\nCaption: {caption}")

async def show_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = db_get_next_items(10)
    if not rows:
        await update.effective_message.reply_text("Queue is empty.")
        return
    msgs = []
    for row in rows:
        files = json.loads(row[1])
        when = datetime.fromtimestamp(row[3], tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        preview = (row[2][:50] + "...") if row[2] and len(row[2]) > 50 else row[2]
        msgs.append(f"ID {row[0]}: {files} scheduled {when}\nCaption: {preview}")
    await update.effective_message.reply_text("\n\n".join(msgs))
async def clear_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_clear_queue()
    await update.effective_message.reply_text("Queue cleared.")

# ===================== Launch ======================
if __name__ == "__main__":
    db_init()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.job_queue.run_repeating(scheduled_poster, interval=60, first=1)

    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("lastpost", lastpost))
    app.add_handler(CommandHandler("queue", show_queue))
    app.add_handler(CommandHandler("clearqueue", clear_queue))
    app.add_handler(CommandHandler("ok", approve_duplicate))
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/ok(?:@[\w_]+)?$") & filters.Chat(chat_id=ADMIN_CHAT_ID),
            approve_duplicate,
        )
    )

    app.add_handler(MessageHandler(filters.ALL & filters.Chat(username=CHANNEL_USERNAME), media_to_queue))
    log.info("Starting bot polling...")
    app.run_polling()
