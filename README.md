# Tele2Twt

This bot connects a public Telegram channel to a Twitter/X account.  
It lets you **schedule/post photos, videos, or album threads** for future posting on Twitter directly from Telegram, with support for queue management and thread-style posting.

---

## Features

- **Schedule posts:** Use captions like `#in 30m` or `#at 2025-09-18 14:00` to specify a future posting time (UTC).
- **Album/Media group support:** When you send a Telegram album (photo/video group), it is posted as a Twitter thread (first video, then photos as reply).
- **Queue management:** View upcoming scheduled posts with `/queue` and clear them with `/clearqueue` in the bot.
- **approval on duplicates:** If you try to post the same file twice, bot waits for DM approval by `/ok`.
- **View last tweet:** Use `/lastpost` in DM to get the URL and caption of the most recent tweet.
- **Liveness ping:** Use `/ping` in DM to confirm the bot is running.

---

## Setup

1. **Clone repo and install dependencies:**
    ```
    git clone https://github.com/0xModded/tele2twt.git
    cd tele2twt
    pip install -r requirements.txt
    ```

2. **Create a `.env` file based on `.env.example`:**
    - Fill in your Telegram bot token, Twitter/X API secrets, channel username (no @), admin Telegram user ID, etc.

3. **Start the bot:**
    ```
    python twitterposter.py
    ```
    - Or use Docker (recommended because who wants to run it on their computer 24/7):
      ```
      docker build -t tele2twt .
      docker run -d --env-file .env --name tele2twt tele2twt
      ```

---

## Usage

### **Posting Media**

- Send a photo, video, or album in your Telegram channel, add a caption if you want one.
- Add to the media caption:
    - `#in 45m` — Schedule post for 45 minutes from now.
    - `#at 2025-09-18 10:00` — Schedule for specific **UTC time**.
    - No tag? Posts as soon as possible.

### ** DM Commands**

- `/ping` — Check if bot is running.
- `/lastpost` — Get latest tweet URL/caption.
- `/queue` — Show next scheduled posts.
- `/clearqueue` — Remove all pending scheduled posts.
- `/ok` — Approve duplicate post when prompted.

---

## Notes

- **Channel Requirements:** Your Telegram channel must be public and set as `CHANNEL_USERNAME` in `.env`.
- **Scheduling:** Scheduled times are UTC. Use `#in Xm` for relative, `#at YYYY-MM-DD HH:MM` for exact time.
- **Albums:** Captions from the first message in an album are used.
---

