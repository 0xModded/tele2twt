import os
import hashlib
import asyncio
import logging
from pathlib import Path
from typing import Optional
import tweepy
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ========== CREDENTIALS FROM ENV ==========
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CHANNEL_USERNAME = os.environ["CHANNEL_USERNAME"].lstrip("@")
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
CONSUMER_KEY = os.environ["CONSUMER_KEY"]
CONSUMER_SECRET = os.environ["CONSUMER_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_SECRET = os.environ["ACCESS_SECRET"]
BEARER_TOKEN = os.environ["BEARER_TOKEN"]

HASH_TRACK_FILE = Path("posted_hashes.txt")
posted_hashes = set()
if HASH_TRACK_FILE.exists():
    posted_hashes = set(line.strip() for line in HASH_TRACK_FILE.read_text().splitlines() if line.strip())

LAST_POST_FILE = Path("lastpost.txt")
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
    log.info(
        "approve_duplicate HANDLER CALLED -- effective_user=%s, chat_id=%s, text=%s",
        getattr(update.effective_user, "id", None),
        getattr(update.effective_chat, "id", None),
        getattr(update.effective_message, "text", None),
    )
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

async def print_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if user:
        sender = f"user:{user.id} @{user.username or ''}"
    elif getattr(msg, "sender_chat", None):
        sc = msg.sender_chat
        sender = f"sender_chat:{sc.id} {getattr(sc,'title','')} @{getattr(sc,'username','')}"
    else:
        sender = "unknown"
    text = None
    if getattr(msg, "text", None):
        text = msg.text
    elif getattr(msg, "caption", None):
        text = msg.caption
    log.info(f"DEBUG: sender={sender} chat.id={chat.id if chat else None} chat.username={getattr(chat,'username',None)} text={text}")

pending_albums = {}
ALBUM_FINALIZE_DELAY = 1.0
async def finalize_album(chat_id, media_group_id, context: ContextTypes.DEFAULT_TYPE):
    key = (chat_id, media_group_id)
    data = pending_albums.get(key)
    if not data:
        return
    await asyncio.sleep(ALBUM_FINALIZE_DELAY)
    files = data["files"]
    caption = data.get("caption") or "Sent from Telegram"
    pending_albums.pop(key, None)
    await process_and_post_media(files, caption, context)

async def process_and_post_media(file_paths: list[str], caption: str, context: ContextTypes.DEFAULT_TYPE):
    hashes = [hash_file(p) for p in file_paths]
    duplicate = any(h in posted_hashes for h in hashes)
    log.info("Posting files %s Duplicate? %s", file_paths, duplicate)
    if duplicate:
        approved = await wait_for_approval(context, {"files": file_paths})
        if not approved:
            for f in file_paths:
                try:
                    os.remove(f)
                except Exception:
                    pass
            return

    reply_to_id = None
    tweet_url = None
    try:
        user_id = ACCESS_TOKEN.split('-')[0]  # Twitter's user ID as string
        video_path = next((p for p in file_paths if p.lower().endswith((".mp4", ".mov", ".mkv", ".webm"))), None)
        photo_paths = [p for p in file_paths if p != video_path]
        if video_path:
            try:
                media = api_v1.media_upload(video_path)
                res = client.create_tweet(text=caption if not photo_paths else caption + " (video)", media_ids=[media.media_id_string])
                if getattr(res, "data", None) and res.data.get("id"):
                    reply_to_id = res.data["id"]
                    tweet_url = f"https://twitter.com/{user_id}/status/{reply_to_id}"
                    save_last_post(tweet_url, caption)
                log.info("Posted video, tweet id: %s", reply_to_id)
                h = hash_file(video_path)
                posted_hashes.add(h)
                HASH_TRACK_FILE.open("a").write(h + "\n")
            except Exception as e:
                log.exception("Failed to post video: %s", e)
        if photo_paths:
            media_ids = []
            for path in photo_paths:
                try:
                    media = api_v1.media_upload(path)
                    media_ids.append(media.media_id_string)
                except Exception as e:
                    log.exception("Failed to upload photo %s: %s", path, e)
            if media_ids:
                res = client.create_tweet(text=caption if not video_path else caption + " (photos)", media_ids=media_ids, in_reply_to_tweet_id=reply_to_id)
                if getattr(res, "data", None) and res.data.get("id"):
                    tweet_id = res.data["id"]
                    tweet_url = f"https://twitter.com/{user_id}/status/{tweet_id}"
                    save_last_post(tweet_url, caption)
                    log.info("Posted photos, tweet id: %s", tweet_id)
                for path in photo_paths:
                    h = hash_file(path)
                    posted_hashes.add(h)
                    HASH_TRACK_FILE.open("a").write(h + "\n")
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="✅ Telegram bot: Posted to Twitter successfully!")
    except Exception as e:
        log.exception("Error while posting to Twitter: %s", e)
        try:
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"❌ Telegram-to-Twitter Error:\n{e}")
        except Exception:
            pass
    finally:
        for p in file_paths:
            try:
                os.remove(p)
            except Exception:
                pass

async def media_to_twitter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg:
        return
    chat = msg.chat
    if not chat or getattr(chat, "username", None) != CHANNEL_USERNAME:
        return
    log.info("media_to_twitter triggered for channel %s message_id=%s", CHANNEL_USERNAME, msg.message_id)
    caption = msg.caption if getattr(msg, "caption", None) else (msg.text if getattr(msg, "text", None) else "Sent from Telegram")
    downloaded = []
    async def dl_file(file_obj) -> str | None:
        try:
            tg_file = await file_obj.get_file()
            saved = await tg_file.download_to_drive()
            return str(saved)
        except Exception:
            log.exception("Failed to download file from Telegram")
            return None
    if getattr(msg, "media_group_id", None):
        key = (chat.id, msg.media_group_id)
        if getattr(msg, "video", None):
            p = await dl_file(msg.video)
            if p:
                pending_albums.setdefault(key, {"files": [], "caption": caption})["files"].append(p)
        elif getattr(msg, "photo", None):
            p = await dl_file(msg.photo[-1])
            if p:
                pending_albums.setdefault(key, {"files": [], "caption": caption})["files"].append(p)
        elif getattr(msg, "document", None):
            p = await dl_file(msg.document)
            if p:
                pending_albums.setdefault(key, {"files": [], "caption": caption})["files"].append(p)
        entry = pending_albums.get(key)
        if entry and "task" not in entry:
            entry["task"] = asyncio.create_task(finalize_album(chat.id, msg.media_group_id, context))
        return
    if getattr(msg, "video", None):
        p = await dl_file(msg.video)
        if p:
            downloaded.append(p)
    elif getattr(msg, "photo", None):
        p = await dl_file(msg.photo[-1])
        if p:
            downloaded.append(p)
    elif getattr(msg, "document", None):
        p = await dl_file(msg.document)
        if p:
            downloaded.append(p)
    else:
        try:
            res = client.create_tweet(text=caption)
            if getattr(res, "data", None) and res.data.get("id"):
                user_id = ACCESS_TOKEN.split('-')[0]
                tweet_url = f"https://twitter.com/{user_id}/status/{res.data['id']}"
                save_last_post(tweet_url, caption)
            log.info("Posted text-only tweet: %s", getattr(res, "data", None))
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text="✅ Posted text-only tweet.")
        except Exception as e:
            log.exception("Failed to post text tweet: %s", e)
            await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=f"❌ Error posting text: {e}")
        return
    if downloaded:
        await process_and_post_media(downloaded, caption, context)

# ----------- Command Handlers -----------
async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("✅ Bot is up and running.")

async def lastpost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    post = load_last_post()
    if post is None:
        await update.effective_message.reply_text("No post has been made yet.")
        return
    url, caption = post
    await update.effective_message.reply_text(f"Last post:\n{url}\nCaption: {caption}")

# ----------- Start Bot -----------
if __name__ == "__main__":
    if not TELEGRAM_TOKEN:
        raise RuntimeError("Missing TELEGRAM_TOKEN.")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Approval handlers
    app.add_handler(CommandHandler("ok", approve_duplicate))
    app.add_handler(
        MessageHandler(
            filters.Regex(r"^/ok(?:@[\w_]+)?$") & filters.Chat(chat_id=ADMIN_CHAT_ID),
            approve_duplicate,
        )
    )
    # Ping and lastpost (could restrict to admin with filters.Chat if desired)
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("lastpost", lastpost))

    # Diagnostics, prints every message except for the main channel
    app.add_handler(MessageHandler(filters.ALL & ~filters.Chat(username=CHANNEL_USERNAME), print_user))
    # Main handler for channel posts
    app.add_handler(MessageHandler(filters.ALL & filters.Chat(username=CHANNEL_USERNAME), media_to_twitter))

    log.info("Starting bot polling...")
    app.run_polling()
