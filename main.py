import os
import asyncio
import json
import hashlib
import re
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError, MediaCaptionTooLongError
import logging
from datetime import datetime, timedelta, timezone
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

timeout_env_var = os.environ.get('STATUS_TIMEOUT_HOURS')
STATUS_TIMEOUT_HOURS = int(timeout_env_var) if timeout_env_var else 4


# --- تنظیمات محلی ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
STATE_REPO_PATH = 'state-repo'
QUEUE_FILE_PATH = os.path.join(STATE_REPO_PATH, "post_queue.json")
STATUS_FILE_PATH = os.path.join(STATE_REPO_PATH, "status.json")
STATUS_TIMESTAMP_FILE_PATH = os.path.join(STATE_REPO_PATH, "status_timestamp.json")
LAST_IDS_FILE = os.path.join(STATE_REPO_PATH, "last_ids.json")
MEDIA_DIR = "media"
MAX_CAPTION_LENGTH = 1024


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
    now_utc_iso = datetime.now(timezone.utc).isoformat()
    write_json_file(STATUS_TIMESTAMP_FILE_PATH, {"timestamp": now_utc_iso})

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

def _create_post_fingerprint(main_message):
    if getattr(main_message, 'text', None):
        return hashlib.md5(main_message.text.strip()[:250].encode()).hexdigest()
    if getattr(main_message, 'file', None):
        return f"{main_message.file.size}-{main_message.file.name or ''}"
    return None

def modify_queue_for_auto_publish():
    logging.info("Timeout detected. Modifying queue for auto-publishing.")
    post_queue = read_json_file(QUEUE_FILE_PATH, default_content=[])
    if not post_queue:
        logging.warning("Queue is empty, nothing to modify.")
        return

    destination_mention = DESTINATION_CHANNEL.lstrip('@')
    source_mentions = [ch.lstrip('@') for ch in SOURCE_CHANNELS]

    for post in post_queue:
        if "text" in post and post["text"]:
            original_text = post["text"]
            modified_text = original_text
            for src_mention in source_mentions:
                modified_text = re.sub(f'@{re.escape(src_mention)}', f'@{destination_mention}', modified_text, flags=re.IGNORECASE)
                modified_text = re.sub(r'\b' + re.escape(src_mention) + r'\b', destination_mention, modified_text, flags=re.IGNORECASE)

            if original_text != modified_text:
                post["text"] = modified_text
                logging.info(f"Modified text for post ID {post.get('post_id')}")

    write_json_file(QUEUE_FILE_PATH, post_queue)
    logging.info("Post queue modification complete.")

def check_status_timeout():
    current_status = get_status()
    if current_status != 1:
        return

    timestamp_data = read_json_file(STATUS_TIMESTAMP_FILE_PATH)
    if not timestamp_data or "timestamp" not in timestamp_data:
        logging.warning("Timestamp file not found or invalid. Resetting timestamp.")
        update_status(1)
        return

    last_update_str = timestamp_data.get("timestamp")
    try:
        last_update_dt = datetime.fromisoformat(last_update_str)
    except (ValueError, TypeError):
        logging.error("Invalid timestamp format found. Resetting timestamp.")
        update_status(1)
        return

    if last_update_dt.tzinfo is None:
        last_update_dt = last_update_dt.replace(tzinfo=timezone.utc)

    time_elapsed = datetime.now(timezone.utc) - last_update_dt
    timeout_delta = timedelta(hours=STATUS_TIMEOUT_HOURS)

    if time_elapsed > timeout_delta:
        logging.warning(f"Status 1 has been active for {time_elapsed}. Timeout of {STATUS_TIMEOUT_HOURS} hours exceeded.")
        modify_queue_for_auto_publish()
        update_status(2)
        logging.info("Status automatically updated to 2 due to timeout.")
    else:
        logging.info(f"Status 1 is active for {time_elapsed}. No timeout yet.")

def cleanup_old_media_files():
    """
    Deletes files in the media directory that are older than 7 days.
    """
    logging.info("--- Running media cleanup ---")
    now = datetime.now(timezone.utc)
    cleanup_threshold = timedelta(days=7)
    deleted_count = 0

    if not os.path.exists(MEDIA_DIR):
        logging.info("Media directory does not exist. Nothing to clean up.")
        return

    for filename in os.listdir(MEDIA_DIR):
        file_path = os.path.join(MEDIA_DIR, filename)
        if os.path.isfile(file_path):
            try:
                # Get file modification time as a timezone-aware datetime object
                file_mod_time_stamp = os.path.getmtime(file_path)
                file_mod_datetime = datetime.fromtimestamp(file_mod_time_stamp, timezone.utc)
                
                if now - file_mod_datetime > cleanup_threshold:
                    os.remove(file_path)
                    logging.info(f"Deleted old media file: {filename}")
                    deleted_count += 1
            except Exception as e:
                logging.error(f"Could not process or delete file {file_path}: {e}")

    if deleted_count > 0:
        logging.info(f"Cleanup complete. Deleted {deleted_count} old media file(s).")
    else:
        logging.info("No old media files found to delete.")


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

            if media_path:
                caption_to_send = text
                if len(text) > MAX_CAPTION_LENGTH:
                    caption_to_send = text[:MAX_CAPTION_LENGTH - 4] + "..."
                    logging.warning(f"Caption for post {post_id} was too long. Truncating it.")

                if isinstance(media_path, list):
                    valid_media_paths = [p for p in media_path if os.path.exists(p)]
                elif isinstance(media_path, str):
                    valid_media_paths = [media_path] if os.path.exists(media_path) else []
                else:
                    valid_media_paths = []

                if valid_media_paths:
                    try:
                        # --- تلاش برای ارسال به صورت آلبوم (اولویت با ظاهر آلبوم) ---
                        logging.info(f"Attempting to send post {post_id} as a single album.")
                        await client.send_file(
                            DESTINATION_CHANNEL,
                            valid_media_paths,
                            caption=caption_to_send,
                            schedule=schedule_time
                        )
                    except Exception as album_error:
                        # --- تغییر جدید: اگر ارسال آلبوم خطا داد، فقط اولین فایل را ارسال کن ---
                        logging.warning(f"Could not send post {post_id} as an album. Error: {album_error}. Falling back to sending only the first file.")
                        
                        if valid_media_paths:
                            first_media_path = valid_media_paths[0]
                            await client.send_file(
                                DESTINATION_CHANNEL,
                                first_media_path,
                                caption=caption_to_send,
                                schedule=schedule_time
                            )
                            logging.info(f"Successfully sent the first media file for post {post_id} as a fallback.")
                        else:
                            logging.error(f"Fallback failed for post {post_id} because there were no valid media paths.")

                    # در هر صورت (موفق یا جایگزین)، تمام فایل‌های دانلود شده برای این پست حذف می‌شوند.
                    logging.info(f"Post {post_id} with {len(valid_media_paths)} media file(s) scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
                    for p in valid_media_paths:
                        os.remove(p)
                else:
                    logging.warning(f"Media for post {post_id} not found. Skipping media part.")
                    if text and text.strip():
                        await client.send_message(DESTINATION_CHANNEL, text, schedule=schedule_time)
                        logging.info(f"Text part of post {post_id} scheduled for {schedule_time.strftime('%Y-%m-%d %H:%M')}")
                    else:
                        continue

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
                    main_message = next((msg for msg in message_group if msg.text), message_group[0])
                    caption_text = main_message.text or ""
                    
                    if not is_post_valid(main_message, channel): continue
                    
                    fingerprint = _create_post_fingerprint(main_message)
                    if fingerprint and fingerprint in existing_fingerprints: continue

                    media_paths_in_repo = []
                    for msg in message_group:
                        if msg.media:
                            try:
                                downloaded_path = await msg.download_media(file=MEDIA_DIR)
                                if downloaded_path:
                                    media_paths_in_repo.append(os.path.relpath(downloaded_path, '.'))
                            except Exception as dl_error:
                                logging.error(f"Could not download media for message {msg.id} in group {group_id}: {dl_error}")

                    if not media_paths_in_repo and not caption_text.strip():
                        continue
                    
                    final_media_path = None
                    if len(media_paths_in_repo) == 1:
                        final_media_path = media_paths_in_repo[0]
                    elif len(media_paths_in_repo) > 1:
                        final_media_path = media_paths_in_repo

                    post_queue.append({
                        "post_id": main_message.id,
                        "text": caption_text,
                        "media_path": final_media_path,
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

    cleanup_old_media_files()
    
    check_status_timeout()

    final_status = get_status()

    if final_status not in [0, 2]:
        logging.info(f"Status is {final_status}. No action required. Exiting.")
        return

    if not TELETHON_SESSION_STRING:
        logging.error("TELETHON_SESSION secret is not set!")
        sys.exit(1)

    client = TelegramClient(StringSession(TELETHON_SESSION_STRING), API_ID, API_HASH)
    
    try:
        await client.connect()
        logging.info("Telegram client connected successfully.")

        if final_status == 2:
            await schedule_posts_for_publishing(client)
        elif final_status == 0:
            await collect_new_posts(client)
    
    finally:
        if client.is_connected():
            await client.disconnect()
            logging.info("Telegram client disconnected.")


if __name__ == "__main__":
    asyncio.run(main())
