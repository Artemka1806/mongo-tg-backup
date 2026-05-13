import os
import subprocess
import asyncio
import functools
from datetime import datetime
from pathlib import Path
import logging
import urllib.request
import urllib.error
import urllib.parse
import json
import difflib
import redis.asyncio as redis
from pyrogram import Client, raw
from pyrogram.errors import FloodWait
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import signal
import sys
from dotenv import load_dotenv

load_dotenv()

# Налаштування логування
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('backup.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Конфігурація з environment variables
MONGODB_URI = os.getenv('MONGODB_URI')
BACKUP_DIR = os.getenv('BACKUP_DIR', './backups')
TELEGRAM_API_ID = os.getenv('TELEGRAM_API_ID')
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')
CONTROL_API_URL = os.getenv(
    'CONTROL_API_URL',
    'https://control-api.undresstool.fun/v1/bots/?page=1&page_size=100&show_tokens=true'
)
CONTROL_API_CONTAINERS_URL = os.getenv(
    'CONTROL_API_CONTAINERS_URL',
    'https://control-api.undresstool.fun/v1/system/containers/small'
)
CONTROL_API_KEY = os.getenv('CONTROL_API_KEY')

try:
    TELEGRAM_CHAT_ID = int(os.getenv('TELEGRAM_CHAT_ID'))
except (ValueError, TypeError):
    logger.error(f"Змінна TELEGRAM_CHAT_ID ('{os.getenv('TELEGRAM_CHAT_ID')}') не є валідним числом! Перевірте .env файл.")
    sys.exit(1)

BACKUP_INTERVAL_MINUTES = int(os.getenv('BACKUP_INTERVAL_MINUTES', '5'))
BOT_CHECK_INTERVAL_MINUTES = int(os.getenv('BOT_CHECK_INTERVAL_MINUTES', '60'))
BOT_CHECK_START_DELAY_SECONDS = int(os.getenv('BOT_CHECK_START_DELAY_SECONDS', '10'))
BOT_USERBOT_RESOLVE_DELAY_SECONDS = float(os.getenv('BOT_USERBOT_RESOLVE_DELAY_SECONDS', '1'))
POSE_CHECK_INTERVAL_MINUTES = int(os.getenv('POSE_CHECK_INTERVAL_MINUTES', '3'))
POSE_CHECK_START_DELAY_SECONDS = int(os.getenv('POSE_CHECK_START_DELAY_SECONDS', '10'))
KEEP_LOCAL_BACKUPS = int(os.getenv('KEEP_LOCAL_BACKUPS', '10'))
SESSION_NAME = os.getenv('SESSION_NAME', 'mongodb_backup_userbot')
POSE_DATA_DIR = os.getenv('POSE_DATA_DIR', './pose_data')
POSE_API_BASE_URL = os.getenv('POSE_API_BASE_URL', 'http://84.247.168.144:8001')
POSE_API_TOKEN = os.getenv('POSE_API_TOKEN')
REDIS_URL = os.getenv('REDIS_URL', 'redis://redis:6379/0')
BOT_ALERT_REDIS_PREFIX = os.getenv('BOT_ALERT_REDIS_PREFIX', 'bot_alerted')

# Прапорець для запобігання паралельного виконання
backup_in_progress = False

# Створення директорії для бекапів
Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
Path(POSE_DATA_DIR).mkdir(parents=True, exist_ok=True)

# Змінна для відстеження прогресу відправки
last_reported_progress = -1
redis_client = None
redis_error_logged = False

def progress_callback(current, total):
    """Callback-функція для відображення прогресу відправки."""
    global last_reported_progress
    if total == 0:
        return

    percentage = int((current / total) * 100)
    if percentage % 10 == 0 and percentage > last_reported_progress:
        logger.info(f"Відправка в Telegram: {percentage}%")
        last_reported_progress = percentage


async def send_failure_notification(app: Client, reason: str, details: str = None):
    """Відправляє повідомлення про помилку в Telegram."""
    try:
        logger.info(f"Відправка повідомлення про помилку: {reason}")
        message = f"🔥 **Помилка створення бекапу MongoDB** 🔥\n\n"
        message += f"**Причина:** {reason}\n"
        
        if details:
            details_short = (details[:3500] + '...') if len(details) > 3500 else details
            message += f"\n**Деталі:**\n```\n{details_short}\n```"
            
        await send_message_with_flood_wait(
            app,
            chat_id=TELEGRAM_CHAT_ID,
            text=message
        )
    except Exception as e:
        logger.error(f"Не вдалося відправити повідомлення про помилку: {e}")


async def create_backup(app: Client):
    """Створює бекап MongoDB"""
    global backup_in_progress
    
    if backup_in_progress:
        logger.warning("Бекап вже виконується. Пропускаємо цей запуск.")
        return
    
    backup_in_progress = True
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_filename = f"mongodb_backup_{timestamp}.gz"
    backup_path = os.path.join(BACKUP_DIR, backup_filename)
    
    try:
        logger.info(f"Початок створення бекапу: {backup_filename}")
        
        cmd = [
            'mongodump',
            f'--uri={MONGODB_URI}',
            '--gzip',
            f'--archive={backup_path}'
        ]
        
        start_time = datetime.now()
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300 * 60
        )
        
        duration = (datetime.now() - start_time).total_seconds()
        
        if result.returncode != 0:
            error_message = f"Процес mongodump завершився з кодом помилки {result.returncode}."
            logger.error(f"Помилка при створенні бекапу: {result.stderr}")
            await send_failure_notification(app, error_message, result.stderr)
            return
        
        file_size = os.path.getsize(backup_path)
        file_size_mb = file_size / (1024 * 1024)
        
        logger.info(f"Бекап створено успішно. Розмір: {file_size_mb:.2f} MB, Час: {duration:.2f} сек")
        
        await send_to_telegram(app, backup_path, backup_filename, file_size_mb, duration)
        
        cleanup_old_backups()
        
    except subprocess.TimeoutExpired as e:
        error_message = "Таймаут виконання mongodump (перевищено 300 хвилин)."
        logger.error(error_message)
        await send_failure_notification(app, error_message, str(e))
    except Exception as e:
        error_message = "Несподівана помилка під час створення бекапу."
        logger.error(f"{error_message} {str(e)}", exc_info=True)
        await send_failure_notification(app, error_message, str(e))
    finally:
        backup_in_progress = False


async def send_to_telegram(app: Client, file_path, filename, file_size_mb, duration):
    """Відправляє бекап файл в Telegram"""
    global last_reported_progress
    try:
        logger.info(f"Відправка файлу в Telegram: {filename}")
        last_reported_progress = -1
        
        caption = (
            f"📦 MongoDB Backup\n"
            f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"💾 Розмір: {file_size_mb:.2f} MB\n"
            f"⏱ Час створення: {duration:.2f} сек\n"
            f"✅ Статус: Успішно"
        )
        
        await send_document_with_flood_wait(
            app=app,
            chat_id=TELEGRAM_CHAT_ID,
            document=file_path,
            caption=caption,
            progress=progress_callback
        )
        
        logger.info("Файл успішно відправлено в Telegram")
        
    except Exception as e:
        logger.error(f"Помилка при відправці в Telegram: {str(e)}", exc_info=True)


async def send_document_with_flood_wait(app: Client, **kwargs):
    """Надійна відправка документу з очікуванням FloodWait."""
    while True:
        try:
            return await app.send_document(**kwargs)
        except FloodWait as e:
            wait_seconds = max(int(getattr(e, "value", 0)), 1)
            logger.warning(f"FloodWait при відправці файлу, очікування {wait_seconds} сек...")
            await asyncio.sleep(wait_seconds)


async def send_message_with_flood_wait(app: Client, **kwargs):
    """Надійна відправка повідомлення з очікуванням FloodWait."""
    while True:
        try:
            return await app.send_message(**kwargs)
        except FloodWait as e:
            wait_seconds = max(int(getattr(e, "value", 0)), 1)
            logger.warning(f"FloodWait при відправці повідомлення, очікування {wait_seconds} сек...")
            await asyncio.sleep(wait_seconds)

def cleanup_old_backups():
    """Видаляє старі бекапи, залишаючи лише останні N"""
    try:
        backup_files = sorted(
            Path(BACKUP_DIR).glob('mongodb_backup_*.gz'),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        
        if len(backup_files) > KEEP_LOCAL_BACKUPS:
            for old_backup in backup_files[KEEP_LOCAL_BACKUPS:]:
                old_backup.unlink()
                logger.info(f"Видалено старий бекап: {old_backup.name}")
                
    except Exception as e:
        logger.error(f"Помилка при очищенні старих бекапів: {str(e)}")


async def send_latest_backup_on_startup(app: Client):
    """Знаходить останній бекап і відправляє його при старті."""
    global last_reported_progress
    try:
        logger.info("Перевірка наявності останнього бекапу для відправки...")
        
        backup_files = sorted(
            Path(BACKUP_DIR).glob('mongodb_backup_*.gz'),
            key=lambda x: x.stat().st_mtime,
            reverse=True
        )
        
        if not backup_files:
            logger.warning("Локальні бекапи не знайдено. Пропускаємо відправку.")
            await send_message_with_flood_wait(
                app,
                chat_id=TELEGRAM_CHAT_ID,
                text="🤖 **Бота перезапущено.**\n\n⚠️ Локальні бекапи не знайдено."
            )
            return
            
        latest_backup = backup_files[0]
        filename = latest_backup.name
        
        logger.info(f"Знайдено останній бекап: {filename}. Відправка...")
        last_reported_progress = -1

        caption = f"🤖 **Бота перезапущено.**\n\n✅ Останній доступний бекап: `{filename}`"
        
        await send_document_with_flood_wait(
            app=app,
            chat_id=TELEGRAM_CHAT_ID,
            document=str(latest_backup),
            caption=caption,
            progress=progress_callback
        )
        
        logger.info("Останній бекап успішно відправлено.")
        
    except Exception as e:
        logger.error(f"Помилка при відправці останнього бекапу: {str(e)}", exc_info=True)
        try:
            await send_message_with_flood_wait(
                app,
                chat_id=TELEGRAM_CHAT_ID,
                text=f"🤖 **Бота перезапущено.**\n\n❌ Не вдалося відправити останній бекап.\nПомилка: {str(e)}"
            )
        except Exception as send_e:
            logger.error(f"Не вдалося навіть відправити повідомлення про помилку: {send_e}")


def fetch_json(url: str, headers: dict, timeout: int = 10):
    """Синхронний HTTP GET, повертає (status_code, json_obj)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.getcode()
            data = response.read()
    except urllib.error.HTTPError as e:
        status = e.code
        data = e.read()
    return status, json.loads(data) if data else {}


def send_json(url: str, headers: dict, payload: dict, method: str = "PATCH", timeout: int = 10):
    """Синхронний JSON request, повертає (status_code, json_obj)."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request_headers = {
        **headers,
        "content-type": "application/json",
    }
    req = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.getcode()
            data = response.read()
    except urllib.error.HTTPError as e:
        status = e.code
        data = e.read()
    return status, json.loads(data) if data else {}


def control_api_v1_base_url() -> str:
    parsed = urllib.parse.urlsplit(CONTROL_API_URL or CONTROL_API_CONTAINERS_URL)
    path_parts = [part for part in parsed.path.split("/") if part]
    if "v1" in path_parts:
        v1_index = path_parts.index("v1")
        path = "/" + "/".join(path_parts[:v1_index + 1])
    else:
        path = "/v1"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


async def update_control_api_restriction_status(
    bot_number,
    userbot_info: dict,
):
    """Синхронізує restriction-state в control API."""
    if not CONTROL_API_KEY or not userbot_info:
        return

    url = f"{control_api_v1_base_url()}/bots/restricted"
    body = {
        "bot_number": str(bot_number),
        "telegram_id": userbot_info.get("id"),
        "restricted": bool(userbot_info.get("restricted")),
        "restriction_reason": userbot_info.get("restriction_reason") or [],
        "restriction_checked_at": datetime.now().astimezone().isoformat(),
    }

    try:
        status, response_payload = await asyncio.to_thread(
            send_json,
            url,
            {"accept": "application/json", "X-API-Key": CONTROL_API_KEY},
            body,
            "PATCH",
            10,
        )
    except Exception as e:
        logger.error(f"Не вдалося оновити restriction-state в control API для bot{bot_number}: {e}")
        return

    if status not in (200, 201):
        logger.error(
            "Невдалий статус restriction update для bot%s: status=%s, body=%s",
            bot_number,
            status,
            normalize_json(response_payload) if isinstance(response_payload, (dict, list)) else response_payload,
        )


async def update_control_api_bot_api_status(
    bot_number,
    status_code,
    response_payload: dict,
):
    """Синхронізує Bot API ban/token-state в control API."""
    if not CONTROL_API_KEY:
        return

    ok = response_payload.get("ok") if isinstance(response_payload, dict) else None
    is_banned = status_code == 401 or ok is False
    error_text = None
    if isinstance(response_payload, dict):
        error_text = response_payload.get("description") or response_payload.get("error")

    url = f"{control_api_v1_base_url()}/bots/bot-api-status"
    body = {
        "bot_number": str(bot_number),
        "bot_api_banned": bool(is_banned),
        "bot_api_ok": bool(status_code == 200 and ok is True),
        "bot_api_status": status_code,
        "bot_api_error": error_text,
        "bot_api_checked_at": datetime.now().astimezone().isoformat(),
    }

    try:
        patch_status, patch_payload = await asyncio.to_thread(
            send_json,
            url,
            {"accept": "application/json", "X-API-Key": CONTROL_API_KEY},
            body,
            "PATCH",
            10,
        )
    except Exception as e:
        logger.error(f"Не вдалося оновити Bot API status в control API для bot{bot_number}: {e}")
        return

    if patch_status not in (200, 201):
        logger.error(
            "Невдалий статус Bot API update для bot%s: status=%s, body=%s",
            bot_number,
            patch_status,
            normalize_json(patch_payload) if isinstance(patch_payload, (dict, list)) else patch_payload,
        )


def clean_bot_username(username: str) -> str:
    """Нормалізує username для resolveUsername."""
    return str(username or "").strip().lstrip("@")


async def get_redis_client():
    """Повертає Redis client або None, якщо Redis недоступний."""
    global redis_client, redis_error_logged

    if not REDIS_URL:
        return None

    if redis_client is None:
        redis_client = redis.from_url(REDIS_URL, decode_responses=True)

    try:
        await redis_client.ping()
        return redis_client
    except Exception as e:
        if not redis_error_logged:
            logger.error(f"Redis недоступний, дедуп алертів ботів вимкнено: {e}")
            redis_error_logged = True
        return None


def bot_alert_key(bot_tg_id) -> str:
    return f"{BOT_ALERT_REDIS_PREFIX}:{bot_tg_id}"


async def was_bot_alerted(bot_tg_id) -> bool:
    """Перевіряє, чи вже писали про цього бота."""
    if not bot_tg_id:
        return False

    client = await get_redis_client()
    if client is None:
        return False

    try:
        return bool(await client.exists(bot_alert_key(bot_tg_id)))
    except Exception as e:
        logger.error(f"Не вдалося перевірити Redis для bot_id={bot_tg_id}: {e}")
        return False


async def mark_bot_alerted(bot_tg_id, alert_payload: dict):
    """Записує Telegram id бота в Redis після успішного алерта."""
    if not bot_tg_id:
        return

    client = await get_redis_client()
    if client is None:
        return

    try:
        await client.set(
            bot_alert_key(bot_tg_id),
            json.dumps(alert_payload, ensure_ascii=False, sort_keys=True),
        )
    except Exception as e:
        logger.error(f"Не вдалося записати Redis для bot_id={bot_tg_id}: {e}")


async def send_bot_alert_once(app: Client, bot_tg_id, message: str, alert_payload: dict):
    """Відправляє алерт один раз на Telegram id бота."""
    if bot_tg_id and await was_bot_alerted(bot_tg_id):
        logger.info(f"Алерт для bot_id={bot_tg_id} вже був відправлений, пропускаємо.")
        return

    await send_message_with_flood_wait(app, chat_id=TELEGRAM_CHAT_ID, text=message)
    await mark_bot_alerted(bot_tg_id, alert_payload)


async def resolve_bot_via_userbot(app: Client, bot_username: str):
    """Отримує raw Telegram user info через поточну userbot-сесію."""
    username = clean_bot_username(bot_username)
    if not username or username == "unknown":
        return None

    while True:
        try:
            resolved = await app.invoke(raw.functions.contacts.ResolveUsername(username=username))
            break
        except FloodWait as e:
            wait_seconds = max(int(getattr(e, "value", 0)), 1)
            logger.warning(
                "Telegram FloodWait на userbot resolveUsername для @%s: очікування %s сек",
                username,
                wait_seconds,
            )
            await asyncio.sleep(wait_seconds)
        except Exception as e:
            logger.error(f"Помилка userbot resolveUsername для @{username}: {e}")
            return None

    users = getattr(resolved, "users", []) or []
    if not users:
        logger.warning(f"Userbot resolveUsername не повернув users для @{username}")
        return None

    user = next(
        (
            u for u in users
            if clean_bot_username(getattr(u, "username", "")) == username
        ),
        users[0],
    )
    reasons = []
    for reason in getattr(user, "restriction_reason", None) or []:
        reasons.append({
            "platform": getattr(reason, "platform", None),
            "reason": getattr(reason, "reason", None),
            "text": getattr(reason, "text", None),
        })

    return {
        "id": getattr(user, "id", None),
        "username": getattr(user, "username", username),
        "bot": getattr(user, "bot", None),
        "restricted": getattr(user, "restricted", None),
        "restriction_reason": reasons,
    }


def format_restriction_reasons(reasons: list) -> str:
    if not reasons:
        return "немає деталей"

    lines = []
    for reason in reasons:
        lines.append(
            "- "
            f"platform={reason.get('platform')}, "
            f"reason={reason.get('reason')}, "
            f"text={reason.get('text')}"
        )
    return "\n".join(lines)


async def check_bots_status(app: Client):
    """Перевіряє доступність ботів, Bot API статус та userbot restrictions."""
    if not CONTROL_API_KEY:
        logger.warning(
            "CONTROL_API_KEY не заданий. Додайте його в .env (CONTROL_API_KEY=...). "
            "Перевірку ботів пропущено."
        )
        return

    try:
        containers_status, containers_payload = await asyncio.to_thread(
            fetch_json,
            CONTROL_API_CONTAINERS_URL,
            {"accept": "application/json", "X-API-Key": CONTROL_API_KEY},
            15
        )
    except Exception as e:
        logger.error(f"Помилка при отриманні контейнерів: {e}")
        return

    logger.info(
        "Відповідь containers endpoint: status=%s, body=%s",
        containers_status,
        normalize_json(containers_payload) if isinstance(containers_payload, (dict, list)) else containers_payload,
    )

    if containers_status != 200:
        logger.error(f"Невдалий статус при отриманні контейнерів: {containers_status}")
        return

    if isinstance(containers_payload, dict):
        containers_items = containers_payload.get("items", containers_payload)
    else:
        containers_items = containers_payload
    if not isinstance(containers_items, list):
        logger.error("Неочікуваний формат відповіді контейнерів")
        return

    container_names = set()
    for item in containers_items:
        if isinstance(item, dict):
            name = item.get("name") or item.get("container_name")
            if name:
                container_names.add(name)
        elif isinstance(item, str):
            container_names.add(item)

    try:
        logger.info("Запуск перевірки ботів...")
        status, payload = await asyncio.to_thread(
            fetch_json,
            CONTROL_API_URL,
            {"accept": "application/json", "X-API-Key": CONTROL_API_KEY},
            15
        )
    except Exception as e:
        logger.error(f"Помилка при отриманні списку ботів: {e}")
        return

    logger.info(
        "Відповідь bots endpoint: status=%s, body=%s",
        status,
        normalize_json(payload) if isinstance(payload, (dict, list)) else payload,
    )

    if status != 200:
        logger.error(f"Невдалий статус при отриманні ботів: {status}")
        return

    items = payload.get("items", [])
    if not items:
        logger.info("Список ботів порожній.")
        return

    checked_total = 0
    checked_with_container = 0
    checked_with_token = 0
    checked_api_calls = 0
    checked_userbot_calls = 0

    for item in items:
        checked_total += 1

        bot_username = item.get("bot_username", "unknown")
        bot_number = item.get("bot_number", "unknown")
        container_name = f"bot{bot_number}"
        if container_name not in container_names:
            continue

        checked_with_container += 1

        userbot_info = await resolve_bot_via_userbot(app, bot_username)
        if userbot_info:
            checked_userbot_calls += 1
            logger.info(
                "Userbot info для %s: %s",
                bot_username,
                normalize_json(userbot_info),
            )
            # Redis дедупить тільки повідомлення в групу; control API має оновлюватися завжди.
            await update_control_api_restriction_status(bot_number, userbot_info)

            if userbot_info.get("restricted"):
                bot_tg_id = userbot_info.get("id")
                message = (
                    "⚠️ **У бота є Telegram restriction**\n\n"
                    f"**bot_username:** @{clean_bot_username(bot_username)}\n"
                    f"**bot_number:** {bot_number}\n"
                    f"**telegram_id:** `{bot_tg_id}`\n"
                    f"**bot:** {userbot_info.get('bot')}\n\n"
                    f"**restriction_reason:**\n```\n"
                    f"{format_restriction_reasons(userbot_info.get('restriction_reason') or [])}\n"
                    "```\n"
                    "@Artemka1806 @redditmarketing"
                )
                try:
                    await send_bot_alert_once(
                        app,
                        bot_tg_id,
                        message,
                        {
                            "type": "restriction",
                            "bot_username": clean_bot_username(bot_username),
                            "bot_number": bot_number,
                            "telegram_id": bot_tg_id,
                            "restriction_reason": userbot_info.get("restriction_reason") or [],
                            "created_at": datetime.now().isoformat(),
                        },
                    )
                except Exception as e:
                    logger.error(f"Не вдалося відправити повідомлення про restriction: {e}")

            if BOT_USERBOT_RESOLVE_DELAY_SECONDS > 0:
                await asyncio.sleep(BOT_USERBOT_RESOLVE_DELAY_SECONDS)

        token = item.get("bot_token")
        if not token:
            continue

        checked_with_token += 1

        try:
            status, payload = await asyncio.to_thread(
                fetch_json,
                f"https://api.telegram.org/bot{token}/getMyName",
                {"accept": "application/json"},
                10
            )
        except Exception as e:
            logger.error(f"Помилка при getMyName для {bot_username}: {e}")
            continue

        checked_api_calls += 1
        logger.info(
            "Відповідь getMyName для %s: status=%s, body=%s",
            bot_username,
            status,
            normalize_json(payload) if isinstance(payload, (dict, list)) else payload,
        )
        await update_control_api_bot_api_status(bot_number, status, payload)

        if status == 401 or payload.get("ok") is False:
            bot_tg_id = userbot_info.get("id") if userbot_info else None
            message = (
                "🚫 **Бот в бані або токен недійсний**\n\n"
                f"**bot_username:** @{clean_bot_username(bot_username)}\n"
                f"**bot_number:** {bot_number}\n"
                f"**telegram_id:** `{bot_tg_id or 'unknown'}`\n"
                "@Artemka1806 @redditmarketing"
            )
            try:
                await send_bot_alert_once(
                    app,
                    bot_tg_id,
                    message,
                    {
                        "type": "bot_api",
                        "bot_username": clean_bot_username(bot_username),
                        "bot_number": bot_number,
                        "telegram_id": bot_tg_id,
                        "status": status,
                        "payload": payload,
                        "created_at": datetime.now().isoformat(),
                    },
                )
            except Exception as e:
                logger.error(f"Не вдалося відправити повідомлення про бан: {e}")
        elif status != 200:
            logger.warning(f"Неочікуваний статус getMyName для {bot_username}: {status}")

    logger.info(
        "Підсумок перевірки ботів: всього=%s, з токеном=%s, в контейнері=%s, викликів getMyName=%s, userbot resolve=%s",
        checked_total,
        checked_with_token,
        checked_with_container,
        checked_api_calls,
        checked_userbot_calls,
    )


def normalize_json(data) -> str:
    """Стабільний текстовий формат JSON для порівняння."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)


def load_text_if_exists(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


async def check_pose_endpoints(app: Client):
    """Перевіряє зміни у pose endpoints та повідомляє в чат."""
    if not POSE_API_TOKEN:
        logger.warning(
            "POSE_API_TOKEN не заданий. Додайте його в .env (POSE_API_TOKEN=...). "
            "Перевірку поз пропущено."
        )
        return

    headers = {"accept": "application/json", "access-token": POSE_API_TOKEN}
    endpoints = {
        "video_all_poses": f"{POSE_API_BASE_URL}/video/all_poses",
        "pose_poses": f"{POSE_API_BASE_URL}/pose/poses",
    }

    for name, url in endpoints.items():
        try:
            status, payload = await asyncio.to_thread(fetch_json, url, headers, 15)
        except Exception as e:
            logger.error(f"Помилка при запиті {name}: {e}")
            continue

        logger.info(
            "Відповідь %s endpoint: status=%s, body=%s",
            name,
            status,
            normalize_json(payload) if isinstance(payload, (dict, list)) else payload,
        )

        if status != 200:
            logger.error(f"Невдалий статус {status} для {name}")
            continue

        normalized = normalize_json(payload)
        data_path = Path(POSE_DATA_DIR) / f"{name}.json"
        previous = load_text_if_exists(data_path)

        if not previous:
            data_path.write_text(normalized, encoding="utf-8")
            logger.info(f"Збережено початковий стан для {name}")
            continue

        if previous == normalized:
            continue

        diff_lines = difflib.unified_diff(
            previous.splitlines(),
            normalized.splitlines(),
            fromfile=f"{name}_prev",
            tofile=f"{name}_new",
            lineterm=""
        )
        diff_text = "\n".join(diff_lines)
        data_path.write_text(normalized, encoding="utf-8")

        header = f"🔄 **Зміни в {name}**\n@Artemka1806 @redditmarketing"
        if len(diff_text) > 3500:
            diff_file = Path(POSE_DATA_DIR) / f"{name}_diff_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            diff_file.write_text(diff_text, encoding="utf-8")
            await send_document_with_flood_wait(
                app=app,
                chat_id=TELEGRAM_CHAT_ID,
                document=str(diff_file),
                caption=header
            )
        else:
            message = f"{header}\n\n```\n{diff_text}\n```"
            await send_message_with_flood_wait(app, chat_id=TELEGRAM_CHAT_ID, text=message)


async def main():
    """Головна функція"""
    logger.info("=" * 50)
    logger.info("Запуск MongoDB Backup Service (USER BOT)")
    logger.info(f"Інтервал бекапів: {BACKUP_INTERVAL_MINUTES} хвилин")
    logger.info(f"Директорія бекапів: {BACKUP_DIR}")
    logger.info(f"Зберігати локально: {KEEP_LOCAL_BACKUPS} бекапів")
    logger.info("=" * 50)
    
    if not all([MONGODB_URI, TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_CHAT_ID]):
        logger.error("Не всі необхідні змінні оточення встановлені!")
        sys.exit(1)

    app = Client(
        SESSION_NAME,
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH,
        workdir="./sessions",
        sleep_threshold=0
    )

    scheduler = AsyncIOScheduler()

    def signal_handler(signum, frame):
        logger.info("Отримано сигнал завершення, зупиняємо процеси...")
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await app.start()
        logger.info("Telegram USER BOT запущено")
        
        me = await app.get_me()
        logger.info(f"Авторизовано як: {me.first_name} (@{me.username if me.username else 'без username'})")
        logger.info(f"ID: {me.id}, Phone: {me.phone_number if me.phone_number else 'N/A'}")

        chat = await app.get_chat(TELEGRAM_CHAT_ID)
        logger.info(f"Resolved chat id: {chat.id}, type: {type(chat).__name__}")

        # await send_latest_backup_on_startup(app)
        
        job = functools.partial(create_backup, app)
        scheduler.add_job(
            job,
            trigger=IntervalTrigger(minutes=BACKUP_INTERVAL_MINUTES),
            id='backup_job',
            name='MongoDB Backup Job',
            replace_existing=True,
            max_instances=1
        )

        bot_check_job = functools.partial(check_bots_status, app)
        scheduler.add_job(
            bot_check_job,
            trigger=IntervalTrigger(minutes=BOT_CHECK_INTERVAL_MINUTES),
            id='bot_check_job',
            name='Bot Availability Check Job',
            replace_existing=True,
            max_instances=1
        )

        pose_check_job = functools.partial(check_pose_endpoints, app)
        scheduler.add_job(
            pose_check_job,
            trigger=IntervalTrigger(minutes=POSE_CHECK_INTERVAL_MINUTES),
            id='pose_check_job',
            name='Pose Endpoints Check Job',
            replace_existing=True,
            max_instances=1
        )

        scheduler.start()
        logger.info("Scheduler запущено")

        logger.info(
            "Перевірка ботів запланована кожні %s хвилин",
            BOT_CHECK_INTERVAL_MINUTES
        )

        if BOT_CHECK_START_DELAY_SECONDS > 0:
            logger.info(
                "Перший запуск перевірки ботів через %s сек",
                BOT_CHECK_START_DELAY_SECONDS
            )
            await asyncio.sleep(BOT_CHECK_START_DELAY_SECONDS)
        await check_bots_status(app)

        logger.info(
            "Перевірка pose endpoints запланована кожні %s хвилин",
            POSE_CHECK_INTERVAL_MINUTES
        )

        if POSE_CHECK_START_DELAY_SECONDS > 0:
            logger.info(
                "Перший запуск перевірки pose endpoints через %s сек",
                POSE_CHECK_START_DELAY_SECONDS
            )
            await asyncio.sleep(POSE_CHECK_START_DELAY_SECONDS)
        await check_pose_endpoints(app)
        
        while True:
            await asyncio.sleep(3600)
            
    except asyncio.CancelledError:
        logger.info("Головна задача була скасована.")
    finally:
        logger.info("Початок процедури зупинки...")
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler зупинено.")
        if app.is_initialized:
            await app.stop()
            logger.info("Telegram клієнт зупинено.")
        if redis_client is not None:
            await redis_client.aclose()
            logger.info("Redis клієнт зупинено.")
        logger.info("Сервіс повністю зупинено.")


if __name__ == "__main__":
    asyncio.run(main())
