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

def _create_post_fingerprint(message):
    if getattr(message, 'text', None):
        return hashlib.md5(message.text.strip()[:250].encode()).hexdigest()
    if getattr(message, 'file', None):
        return f"{message.file.size}-{message.file.name or ''}"
    return None

async def schedule_posts_for_publishing(client):
    logging.info("--- Entering Publishing Mode (Status 3) ---")
    post_queue = read_json_file(QUEUE_FILE_PATH, default_content=[])
    if not post_queue:
        logging.warning("Publishing triggered, but post queue is empty.")
        update_status(0)
        return

    remaining_posts = []
    scheduled_posts_count = 0
    # محدودیت کاراکتر تلگرام برای کپشن رسانه‌ها
    MAX_CAPTION_LENGTH = 1024

    for index, post in enumerate(post_queue):
        try:
            post_id = post.get("post_id")
            text = post.get("text", "") # استفاده از مقدار پیش‌فرض برای جلوگیری از خطا
            media_path = post.get("media_path")
            local_media_path = media_path if media_path else None
            schedule_time = datetime.now() + timedelta(minutes=(scheduled_posts_count + 1) * SCHEDULE_INTERVAL_MINUTES)
            
            if local_media_path and os.path.exists(local_media_path):
                caption_to_send = text
                # **تغییر اصلی اینجاست: کوتاه کردن کپشن در صورت نیاز**
                if len(text) > MAX_CAPTION_LENGTH:
                    caption_to_send = text[:MAX_CAPTION_LENGTH - 4] + "..."
                    logging.warning(f"Caption for post {post_id} was too long. Truncating it.")
                
                await client.send_file(DESTINATION_CHANNEL, local_media_path, caption=caption_to_send, schedule=schedule_time)
                logging.info(f"Post {post_id} with media scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
                os.remove(local_media_path)
            
            elif text and text.strip():
                # محدودیت پیام متنی 4096 کاراکتر است و معمولا مشکلی ایجاد نمی‌کند
                await client.send_message(DESTINATION_CHANNEL, text, schedule=schedule_time)
                logging.info(f"Text post {post_id} scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
            else:
                logging.warning(f"Post {post_id} has no valid text or media. Skipping.")
                continue
            
            scheduled_posts_count += 1
            await asyncio.sleep(2)
        except MediaCaptionTooLongError:
            # این خطا به طور خاص برای کپشن طولانی است، پست را رد می‌کنیم تا در حلقه نیفتد
            logging.error(f"Could not process post {post.get('post_id')}: Caption is definitely too long. Skipping this post permanently.")
            # این پست را به صف باقی‌مانده اضافه نمی‌کنیم
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
        logging.warning(f"{len(remaining_posts)} posts remain in queue. Status remains 3.")

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
            messages = await client.get_messages(channel, min_id=last_message_id, limit=100)
            new_messages = [m for m in messages if m.id > last_message_id]

            if new_messages:
                grouped_messages = {}
                for msg in new_messages:
                    if msg.grouped_id:
                        if msg.grouped_id not in grouped_messages: grouped_messages[msg.grouped_id] = []
                        grouped_messages[msg.grouped_id].append(msg)
                    else:
                        grouped_messages[msg.id] = [msg]

                for group_id, message_group in grouped_messages.items():
                    message = message_group[0]
                    if not is_post_valid(message, channel): continue
                    
                    fingerprint = _create_post_fingerprint(message)
                    if fingerprint and fingerprint in existing_fingerprints: continue

                    media_path_in_repo = None
                    caption_text = message.text or ""

                    if message.media:
                        downloaded_path = await message.download_media(file=MEDIA_DIR)
                        media_path_in_repo = os.path.relpath(downloaded_path, '.')
                    
                    if not media_path_in_repo and not caption_text.strip(): continue

                    post_queue.append({
                        "post_id": message.id,
                        "text": caption_text,
                        "media_path": media_path_in_repo,
                        "fingerprint": fingerprint
                    })
                    total_new_posts_count += 1
                
                last_ids[channel] = max(m.id for m in new_messages)
        except Exception as e:
            logging.error(f"Error processing channel {channel}: {e}")

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

        if final_status == 3:
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
