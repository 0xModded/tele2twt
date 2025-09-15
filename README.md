# Tele2Twt

A Python bot that posts messages and media (photos, videos, albums) from a Telegram channel to a Twitter/X account.  
Supports a number of features like duplicate detection, confirming whether or not it successfully posted, etc .

## Features

- Posts text and media from a public Telegram channel to Twitter
- Handles Telegram photo and video albums
- Prevents posting duplicates (can be overridden with /ok in a Telegram DM to the bot)
- Admin notifications and approvals via Telegram DM
- Designed to run easily with Docker

## Setup

1. **Clone the repository**

2. **Create a `.env` file:**  
   Copy `.env.example` and fill in your actual Telegram, Twitter/X, and admin keys/secrets.
   
3. **Install requirements (for local dev):**
    ```
    pip install -r requirements.txt
    ```

4. **Run locally:**
    ```
    python twitterposter.py
    ```

5. **Or run with Docker:**
    ```
    docker build -t twitterposter .
    docker run --env-file .env twitterposter
    ```
