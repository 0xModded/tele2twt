import os
import hashlib
import asyncio
import logging
import json
import re
import sqlite3
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple, Dict
import logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

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

if os.environ.get("TELE2TWT_ALLOW_NEST_ASYNCIO", "").lower() in ("1", "true", "yes"):
    try:
        import nest_asyncio
        nest_asyncio.apply()
        log.info("nest_asyncio applied (dev mode).")
    except Exception:
        log.exception("Failed to apply nest_asyncio")

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
        )
        conn.commit()

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
        return int((datetime.now(timezone.utc) + timedelta(minutes=mins)).timestamp())
    m = re.search(r"#in\s+([0-9]+)\s*h(?:our)?", caption)
    if m:
        hrs = int(m.group(1))
        return int((datetime.now(timezone.utc) + timedelta(hours=hrs)).timestamp())
    return None

def strip_schedule_from_caption(caption: str) -> str:
    caption = re.sub(r"#in\s+\d+\s*[mh](in|our)?", "", caption, flags=re.IGNORECASE)
    caption = re.sub(r"#at\s+[0-9\-: Tt]+", "", caption, flags=re.IGNORECASE)
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

# ======= Album Buffering (with debounce) ===========
pending_albums: Dict[Tuple[int, str], List[Dict]] = {}
pending_album_tasks: Dict[Tuple[int, str], asyncio.Task] = {}

# ======= Duplicate approval storage ===========
pending_approvals: Dict[str, Dict] = {}

def _new_approval_id() -> str:
    return str(int(time.time() * 1000))

async def finalize_album(chat_id, group_id, context):
    key = (chat_id, group_id)
    pending_album_tasks.pop(key, None)
    items = pending_albums.pop(key, None)
    if not items:
        return

    caption = items[0]['caption']
    post_time = parse_schedule_from_caption(caption) or int(datetime.now(timezone.utc).timestamp())
    clean_caption = strip_schedule_from_caption(caption)

    videos = [i for i in items if i['type'] == 'video']
    photos = [i for i in items if i['type'] == 'photo']
    others = [i for i in items if i['type'] not in ('video', 'photo')]

    if len(videos) == 1 and photos:
        media_paths = [videos[0]['file']] + [p['file'] for p in photos]
        db_add_queue_item(media_paths, clean_caption, post_time)
    elif len(videos) > 1:
        media_paths = [v['file'] for v in videos] + [p['file'] for p in photos] + [o['file'] for o in others]
        db_add_queue_item(media_paths, clean_caption, post_time)
    elif photos:
        media_paths = [p['file'] for p in photos[:4]]
        db_add_queue_item(media_paths, clean_caption, post_time)
    else:
        media_paths = [items[0]['file']]
        db_add_queue_item(media_paths, clean_caption, post_time)

    # keep informative queued notification
    await context.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"Album queued for {datetime.fromtimestamp(post_time, timezone.utc):%Y-%m-%d %H:%M UTC}"
    )

def infer_type_from_document(msg) -> str:
    doc = getattr(msg, "document", None)
    if not doc:
        return "document"
    mime = getattr(doc, "mime_type", "") or ""
    fname = getattr(doc, "file_name", "") or ""
    mime = mime.lower()
    fname = fname.lower()
    if "image" in mime or fname.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return "photo"
    if "video" in mime or fname.endswith((".mp4", ".mov", ".mkv", ".webm")):
        return "video"
    return "document"

async def _delayed_finalize(key: Tuple[int, str], context: ContextTypes.DEFAULT_TYPE, delay: float = 1.8):
    try:
        await asyncio.sleep(delay)
        await finalize_album(key[0], key[1], context)
    except asyncio.CancelledError:
        pass
    finally:
        pending_album_tasks.pop(key, None)

async def media_to_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return

    chat_obj = msg.chat
    if not chat_obj or getattr(chat_obj, "username", None) != CHANNEL_USERNAME:
        return

    caption = msg.caption if getattr(msg, "caption", None) else (msg.text if getattr(msg, "text", None) else "Sent from Telegram")

    async def dl_file(file_obj):
        tg_file = await file_obj.get_file()
        saved = await tg_file.download_to_drive()
        return str(saved)

    media_type = None
    file_path = None

    if getattr(msg, "video", None):
        media_type = 'video'
        file_path = await dl_file(msg.video)
    elif getattr(msg, "photo", None):
        media_type = 'photo'
        file_path = await dl_file(msg.photo[-1])
    elif getattr(msg, "document", None):
        media_type = infer_type_from_document(msg)
        file_path = await dl_file(msg.document)

    if getattr(msg, "media_group_id", None):
        key = (chat_obj.id, msg.media_group_id)
        pending_albums.setdefault(key, []).append({'file': file_path, 'caption': caption, 'type': media_type})

        existing = pending_album_tasks.get(key)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.create_task(_delayed_finalize(key, context, delay=1.8))
        pending_album_tasks[key] = task

    else:
        post_time = parse_schedule_from_caption(caption) or int(datetime.now(timezone.utc).timestamp())
        clean_caption = strip_schedule_from_caption(caption)
        if file_path:
            db_add_queue_item([file_path], clean_caption, post_time)
        else:
            db_add_queue_item([], clean_caption, post_time)

        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=
            f"Queued post for {datetime.fromtimestamp(post_time, timezone.utc):%Y-%m-%d %H:%M UTC}")

# ============= Posting Logic & Approval Handling ==============
async def _post_media_now(file_paths: list, caption: str, context: ContextTypes.DEFAULT_TYPE):
    if not file_paths:
        client.create_tweet(text=caption)
        if context:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"✅ Twitter post successful!\nCaption: {caption[:100]}")
        return

    videos = [p for p in file_paths if p.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))]
    photos = [p for p in file_paths if p.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp"))]
    others = [p for p in file_paths if p not in videos and p not in photos]

    try:
        if len(videos) == 1 and photos:
            vid_media = api_v1.media_upload(videos[0])
            main_tweet = client.create_tweet(text=caption, media_ids=[vid_media.media_id])
            main_id = main_tweet.data["id"]
            await asyncio.sleep(1)
            for p in photos:
                m = api_v1.media_upload(p)
                client.create_tweet(text=caption, media_ids=[m.media_id], in_reply_to_tweet_id=main_id)
                await asyncio.sleep(1)
            for o in others:
                m = api_v1.media_upload(o)
                client.create_tweet(text="", media_ids=[m.media_id], in_reply_to_tweet_id=main_id)
                await asyncio.sleep(1)

        elif len(videos) > 1:
            main_media = api_v1.media_upload(videos[0])
            main_tweet = client.create_tweet(text=caption, media_ids=[main_media.media_id])
            main_id = main_tweet.data["id"]
            await asyncio.sleep(1)
            for v in videos[1:]:
                vm = api_v1.media_upload(v)
                client.create_tweet(text="", media_ids=[vm.media_id], in_reply_to_tweet_id=main_id)
                await asyncio.sleep(1)
            for p in photos + others:
                pm = api_v1.media_upload(p)
                client.create_tweet(text="", media_ids=[pm.media_id], in_reply_to_tweet_id=main_id)
                await asyncio.sleep(1)

        else:
            media_ids = []
            for mpath in file_paths[:4]:
                m = api_v1.media_upload(mpath)
                media_ids.append(m.media_id)
                await asyncio.sleep(1)
            client.create_tweet(text=caption, media_ids=media_ids if media_ids else None)

        with open(HASH_TRACK_FILE, "a", encoding="utf-8") as f:
            for p in file_paths:
                try:
                    h = hash_file(p)
                    f.write(h + "\n")
                except Exception:
                    pass

        if context:
            try:
                await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"✅ Twitter post successful!\nCaption: {caption[:100]}")
            except Exception as notify_e:
                log.warning(f"Could not send success DM: {notify_e}")

    except tweepy.errors.TooManyRequests:
        db_add_queue_item(file_paths, caption, int(datetime.now(timezone.utc).timestamp()) + 900)
        log.warning("⚠️ Twitter rate limit (429). Requeued post for +15 minutes.")
        if context:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="❌ Twitter post failed with rate limit (429). Requeued +15 min.")
    except Exception as e:
        db_add_queue_item(file_paths, caption, int(datetime.now(timezone.utc).timestamp()) + 60)
        log.error(f"Failed posting tweet: {e}")
        if context:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"❌ Twitter post failed, requeued. Reason: {str(e)}")

async def process_and_post_media(file_paths: list, caption: str, context: ContextTypes.DEFAULT_TYPE):
    if not file_paths:
        try:
            client.create_tweet(text=caption)
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"✅ Twitter post successful!\nCaption: {caption[:100]}")
        except Exception as e:
            log.error(f"Failed posting tweet: {e}")
        return

    hashes = [hash_file(p) for p in file_paths]
    existing_text = HASH_TRACK_FILE.read_text(encoding="utf-8") if HASH_TRACK_FILE.exists() else ""
    is_dup = any(h in existing_text for h in hashes)

    if is_dup:
        approval_id = _new_approval_id()
        pending_approvals[approval_id] = {
            "media_paths": file_paths,
            "caption": caption,
            "created_at": int(time.time())
        }
        file_list = "\n".join(file_paths[:8]) + (f"\n...(+{len(file_paths)-8} more)" if len(file_paths) > 8 else "")
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=(
                f"⚠️ Duplicate detected for queued post (approval id: {approval_id}).\n\n"
                f"Files:\n{file_list}\n\n"
                f"Caption preview: {caption[:200]}\n\n"
                f"To approve posting anyway, send: /ok {approval_id}\n"
                f"To ignore this item, send: /ignore {approval_id}"
            )
        )
        log.info(f"Duplicate detected, approval {approval_id} requested.")
        return

    await _post_media_now(file_paths, caption, context)

# Command handlers for approval
async def ok_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.effective_message.reply_text("You are not authorized to approve.")
        return

    args = context.args or []
    approval_id = None
    if args:
        approval_id = args[0].strip()
    else:
        if len(pending_approvals) == 1:
            approval_id = next(iter(pending_approvals.keys()))

    if not approval_id or approval_id not in pending_approvals:
        await update.effective_message.reply_text("No matching pending approval found. Provide `/ok <id>`.")
        return

    item = pending_approvals.pop(approval_id)
    await update.effective_message.reply_text(f"Approval {approval_id} received — posting now.")
    await _post_media_now(item["media_paths"], item["caption"], context)

async def ignore_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.effective_message.reply_text("You are not authorized to ignore.")
        return

    args = context.args or []
    approval_id = None
    if args:
        approval_id = args[0].strip()
    else:
        if len(pending_approvals) == 1:
            approval_id = next(iter(pending_approvals.keys()))

    if not approval_id or approval_id not in pending_approvals:
        await update.effective_message.reply_text("No matching pending approval found. Provide `/ignore <id>`.")
        return

    pending_approvals.pop(approval_id, None)
    await update.effective_message.reply_text(f"Ignored pending approval {approval_id}.")

async def list_approvals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id != ADMIN_CHAT_ID:
        await update.effective_message.reply_text("You are not authorized.")
        return
    if not pending_approvals:
        await update.effective_message.reply_text("No pending approvals.")
        return
    lines = []
    for aid, item in pending_approvals.items():
        created = datetime.fromtimestamp(item["created_at"], timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        lines.append(f"{aid}: {len(item['media_paths'])} files queued at {created} — preview: {item['caption'][:80]}")
    await update.effective_message.reply_text("\n".join(lines))

async def process_queue(context: ContextTypes.DEFAULT_TYPE):
    items = db_pop_due_items()
    for item in items:
        _, media_json, caption, _ = item
        media_paths = json.loads(media_json)
        await process_and_post_media(media_paths, caption, context)

async def scheduled_poster(context: ContextTypes.DEFAULT_TYPE):
    await process_queue(context)

# ============= Basic Commands =============
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

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is up and running.")

# ============= Main =============
def main():
    db_init()
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # If you added handlers earlier in your file, keep them; otherwise add here
    application.add_handler(CommandHandler("queue", show_queue))
    application.add_handler(CommandHandler("clearqueue", clear_queue))
    application.add_handler(CommandHandler("ping", ping))

    application.add_handler(CommandHandler("ok", ok_command))
    application.add_handler(CommandHandler("ignore", ignore_command))
    application.add_handler(CommandHandler("approvals", list_approvals_command))

    application.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, media_to_queue)
    )

    # Start scheduled poster: prefer job_queue, otherwise fallback to asyncio task
    if getattr(application, "job_queue", None) is not None:
        try:
            application.job_queue.run_repeating(scheduled_poster, interval=60, first=5)
            log.info("Started scheduled_poster via application.job_queue")
        except Exception:
            log.exception("Failed to start scheduled_poster via job_queue; will use asyncio fallback")
            application.job_queue = None

    if getattr(application, "_periodic_task", None):
        # `_periodic_task` is set earlier as coroutine function; create the background task via Application's create_task
        # Application provides create_task on its event loop when run_polling starts, but we can schedule it using application.create_task
        # after initialization. We'll use the Application.run_polling lifecycle to safely schedule it.
        async def _start_periodic_task_on_app_startup(app):
            # wait until the application is initialized and running
            await asyncio.sleep(0.1)
            # create the task
            app.create_task(app._periodic_task())
            log.info("Started fallback periodic process_queue task")

        # register a startup callback to create the task
        application.post_init = getattr(application, "post_init", None)
        # We'll schedule the helper to run when run_polling starts by wrapping run_polling below.

    log.info("Bot started.")

    # run the bot (synchronous call; PTB manages the event loop)
    # If we need to create fallback background task, start it before calling run_polling
    # (application.initialize/start handled inside run_polling)
    try:
        # If fallback task is required, create and schedule it on the running loop by wrapping run_polling
        if getattr(application, "_periodic_task", None):
            # create a wrapper coroutine that schedules the fallback task then runs polling
            async def _run_with_startup():
                # schedule periodic task
                app = application
                app.create_task(app._periodic_task())
                # now run polling (this will block until shutdown)
                await app.run_polling()
            # run polling by executing the wrapper
            # Because we're in a synchronous main, call run() on asyncio to run the wrapper
            asyncio.run(_run_with_startup())
        else:
            # normal sync run (this call will manage the loop)
            application.run_polling()
    except KeyboardInterrupt:
        log.info("Shutting down by user interrupt")
    except Exception:
        log.exception("Unexpected error in main()")


if __name__ == "__main__":
    main()

