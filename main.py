import os
import asyncio
import json
import hashlib
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, MediaCaptionTooLongError
import logging
from datetime import datetime, timedelta
import sys

# --- ۱. خواندن تنظیمات از متغیرهای محیطی ---
API_ID = int(os.environ.get('API_ID'))
API_HASH = os.environ.get('API_HASH')
TELETHON_SESSION_STRING = os.environ.get('TELETHON_SESSION')
SOURCE_CHANNELS_STR = os.environ.get('SOURCE_CHANNELS', '')
SOURCE_CHANNELS = [ch.strip() for ch in SOURCE_CHANNELS_STR.split(',') if ch.strip()]
DESTINATION_CHANNEL = os.environ.get('DESTINATION_CHANNEL')
SCHEDULE_INTERVAL_MINUTES = int(os.environ.get('SCHEDULE_INTERVAL_MINUTES', 180))
PUBLISHER_NAME = os.environ.get('PUBLISHER_NAME', 'DefaultPublisher')

# --- تنظیمات محلی ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
STATE_REPO_PATH = 'state-repo'
QUEUE_FILE_PATH = os.path.join(STATE_REPO_PATH, "post_queue.json")
STATUS_FILE_PATH = os.path.join(STATE_REPO_PATH, "status.json")
LAST_IDS_FILE = os.path.join(STATE_REPO_PATH, "last_ids.json")
MEDIA_DIR = "media"
MAX_CAPTION_LENGTH = 1024 # محدودیت کاراکتر تلگرام برای کپشن


# --- توابع کمکی ---
def read_json_file(file_path, default_content=None):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        if default_content is not None:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            write_json_file(file_path, default_content)
            return default_content
        return None

def write_json_file(file_path, data):
    with open(file_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def get_status():
    status_data = read_json_file(STATUS_FILE_PATH, default_content={"final_status": 0})
    return status_data.get("final_status", 0)

def update_status(status_value):
    logging.info(f"Updating status to {status_value}")
    write_json_file(STATUS_FILE_PATH, {"final_status": status_value})

def is_post_valid(message, source_channel_username):
    text = message.text
    if not text: return True
    url_pattern = r'https?://\S+|www\.\S+|t\.me/\S+'
    if re.search(url_pattern, text):
        logging.warning(f"Skipping post {message.id} from {source_channel_username} because it contains a link.")
        return False
    mention_pattern = r'@(\w+)'
    mentions = re.findall(mention_pattern, text)
    source_username_without_at = source_channel_username.lstrip('@')
    for mention in mentions:
        if mention.lower() != source_username_without_at.lower():
            logging.warning(f"Skipping post {message.id} from {source_channel_username} because it contains an external mention: @{mention}")
            return False
    return True

# <--- بهبود: این تابع حالا پیام اصلی گروه را برای ساخت اثرانگشت دریافت می‌کند
def _create_post_fingerprint(main_message):
    if getattr(main_message, 'text', None):
        return hashlib.md5(main_message.text.strip()[:250].encode()).hexdigest()
    if getattr(main_message, 'file', None):
        return f"{main_message.file.size}-{main_message.file.name or ''}"
    return None

async def schedule_posts_for_publishing(client):
    logging.info("--- Entering Publishing Mode (Status 2) ---")
    post_queue = read_json_file(QUEUE_FILE_PATH, default_content=[])
    if not post_queue:
        logging.warning("Publishing triggered, but post queue is empty.")
        update_status(0)
        return

    remaining_posts = []
    scheduled_posts_count = 0

    for index, post in enumerate(post_queue):
        try:
            post_id = post.get("post_id")
            text = post.get("text", "")
            media_path = post.get("media_path")
            schedule_time = datetime.now() + timedelta(minutes=(scheduled_posts_count + 1) * SCHEDULE_INTERVAL_MINUTES)

            # <--- تغییر کلیدی: بررسی می‌کند که آیا media_path یک لیست است (برای آلبوم) یا یک رشته
            if media_path:
                caption_to_send = text
                if len(text) > MAX_CAPTION_LENGTH:
                    caption_to_send = text[:MAX_CAPTION_LENGTH - 4] + "..."
                    logging.warning(f"Caption for post {post_id} was too long. Truncating it.")

                # بررسی می‌کنیم که آیا مسیرهای رسانه وجود دارند
                if isinstance(media_path, list):
                    valid_media_paths = [p for p in media_path if os.path.exists(p)]
                elif isinstance(media_path, str):
                    valid_media_paths = [media_path] if os.path.exists(media_path) else []
                else:
                    valid_media_paths = []

                if valid_media_paths:
                    await client.send_file(DESTINATION_CHANNEL, valid_media_paths, caption=caption_to_send, schedule=schedule_time)
                    logging.info(f"Post {post_id} with {len(valid_media_paths)} media file(s) scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
                    # حذف تمام فایل‌های رسانه‌ای پس از زمان‌بندی
                    for p in valid_media_paths:
                        os.remove(p)
                else:
                     logging.warning(f"Media for post {post_id} not found. Skipping media part.")
                     # اگر متنی وجود داشت، آن را به تنهایی ارسال کن
                     if text and text.strip():
                        await client.send_message(DESTINATION_CHANNEL, text, schedule=schedule_time)
                        logging.info(f"Text part of post {post_id} scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
                     else:
                        continue # اگر نه رسانه بود و نه متن، از این پست بگذر

            elif text and text.strip():
                await client.send_message(DESTINATION_CHANNEL, text, schedule=schedule_time)
                logging.info(f"Text post {post_id} scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
            else:
                logging.warning(f"Post {post_id} has no valid text or media. Skipping.")
                continue

            scheduled_posts_count += 1
            await asyncio.sleep(2)
        except MediaCaptionTooLongError:
            logging.error(f"Could not process post {post.get('post_id')}: Caption is definitely too long. Skipping this post permanently.")
        except FloodWaitError as e:
            logging.warning(f"Flood wait triggered. Pausing for {e.seconds}s.")
            await asyncio.sleep(e.seconds + 5)
            remaining_posts.extend(post_queue[index:])
            break
        except Exception as e:
            logging.error(f"Could not process post {post.get('post_id')}: {e}")
            remaining_posts.append(post)

    write_json_file(QUEUE_FILE_PATH, remaining_posts)
    if not remaining_posts:
        logging.info("All posts scheduled successfully. Resetting status to 0.")
        update_status(0)
    else:
        logging.warning(f"{len(remaining_posts)} posts remain in queue. Status remains 2.")

async def collect_new_posts(client):
    logging.info("--- Entering Collection Mode (Status 0) ---")
    last_ids = read_json_file(LAST_IDS_FILE, default_content={})
    post_queue = read_json_file(QUEUE_FILE_PATH, default_content=[])
    existing_fingerprints = {p.get('fingerprint') for p in post_queue if p.get('fingerprint')}
    total_new_posts_count = 0

    for channel in SOURCE_CHANNELS:
        try:
            last_message_id = last_ids.get(channel, 0)
            logging.info(f"Checking {channel} since ID: {last_message_id}...")
            # <--- بهبود: محدودیت را افزایش می‌دهیم تا پست‌های گروهی بزرگ را از دست ندهیم
            messages = await client.get_messages(channel, min_id=last_message_id, limit=200)
            new_messages = [m for m in messages if m.id > last_message_id]

            if new_messages:
                grouped_messages = {}
                for msg in new_messages:
                    group_key = msg.grouped_id if msg.grouped_id else msg.id
                    if group_key not in grouped_messages:
                        grouped_messages[group_key] = []
                    grouped_messages[group_key].append(msg)

                for group_id, message_group in grouped_messages.items():
                    # <--- تغییر اصلی: پیدا کردن پیام اصلی (که متن دارد) و متن کپشن
                    main_message = next((msg for msg in message_group if msg.text), message_group[0])
                    caption_text = main_message.text or ""
                    
                    if not is_post_valid(main_message, channel): continue
                    
                    fingerprint = _create_post_fingerprint(main_message)
                    if fingerprint and fingerprint in existing_fingerprints: continue

                    # <--- تغییر اصلی: دانلود تمام رسانه‌های گروه و ذخیره مسیر آنها در یک لیست
                    media_paths_in_repo = []
                    for msg in message_group:
                        if msg.media:
                            try:
                                downloaded_path = await msg.download_media(file=MEDIA_DIR)
                                if downloaded_path:
                                    media_paths_in_repo.append(os.path.relpath(downloaded_path, '.'))
                            except Exception as dl_error:
                                logging.error(f"Could not download media for message {msg.id} in group {group_id}: {dl_error}")

                    # اگر هیچ رسانه‌ای دانلود نشد و متنی هم وجود نداشت، از این گروه بگذر
                    if not media_paths_in_repo and not caption_text.strip():
                        continue
                    
                    # <--- تغییر اصلی: اگر فقط یک رسانه وجود دارد، رشته ذخیره کن، در غیر این صورت لیست
                    final_media_path = None
                    if len(media_paths_in_repo) == 1:
                        final_media_path = media_paths_in_repo[0]
                    elif len(media_paths_in_repo) > 1:
                        final_media_path = media_paths_in_repo


                    post_queue.append({
                        "post_id": main_message.id,
                        "text": caption_text,
                        "media_path": final_media_path, # <--- اینجا حالا می‌تواند لیست یا رشته باشد
                        "fingerprint": fingerprint
                    })
                    total_new_posts_count += 1
                
                if grouped_messages:
                    last_ids[channel] = max(m.id for m in new_messages)
        except Exception as e:
            logging.error(f"Error processing channel {channel}: {e}", exc_info=True)

    if total_new_posts_count > 0:
        logging.info(f"Collected {total_new_posts_count} new posts.")
        write_json_file(QUEUE_FILE_PATH, post_queue)
        write_json_file(LAST_IDS_FILE, last_ids)
        update_status(1)
    else:
        logging.info("No new messages found.")

async def main():
    os.makedirs(MEDIA_DIR, exist_ok=True)
    os.makedirs(STATE_REPO_PATH, exist_ok=True)

    if not TELETHON_SESSION_STRING:
        logging.error("TELETHON_SESSION secret is not set!")
        sys.exit(1)

    client = TelegramClient(StringSession(TELETHON_SESSION_STRING), API_ID, API_HASH)
    
    await client.connect()
    logging.info("Telegram client connected successfully.")

    try:
        final_status = get_status()

        if final_status == 2:
            await schedule_posts_for_publishing(client)
        elif final_status == 0:
            await collect_new_posts(client)
        else:
            logging.info(f"Status is {final_status}. No action required. Exiting.")
    
    finally:
        await client.disconnect()
        logging.info("Telegram client disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
