import os
import subprocess
import asyncio
import functools
from datetime import datetime
from pathlib import Path
import logging
import urllib.request
import urllib.error
import json
import difflib
from pyrogram import Client
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
POSE_CHECK_INTERVAL_MINUTES = int(os.getenv('POSE_CHECK_INTERVAL_MINUTES', '3'))
POSE_CHECK_START_DELAY_SECONDS = int(os.getenv('POSE_CHECK_START_DELAY_SECONDS', '10'))
KEEP_LOCAL_BACKUPS = int(os.getenv('KEEP_LOCAL_BACKUPS', '10'))
SESSION_NAME = os.getenv('SESSION_NAME', 'mongodb_backup_userbot')
POSE_DATA_DIR = os.getenv('POSE_DATA_DIR', './pose_data')
POSE_API_BASE_URL = os.getenv('POSE_API_BASE_URL', 'http://84.247.168.144:8001')
POSE_API_TOKEN = os.getenv('POSE_API_TOKEN')

# Прапорець для запобігання паралельного виконання
backup_in_progress = False

# Створення директорії для бекапів
Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)
Path(POSE_DATA_DIR).mkdir(parents=True, exist_ok=True)

# Змінна для відстеження прогресу відправки
last_reported_progress = -1

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
            
        await app.send_message(
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
            await app.send_message(
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
            await app.send_message(
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


async def check_bots_status(app: Client):
    """Перевіряє доступність ботів та повідомляє про 401."""
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

    for item in items:
        checked_total += 1
        token = item.get("bot_token")
        if not token:
            continue

        bot_username = item.get("bot_username", "unknown")
        bot_number = item.get("bot_number", "unknown")
        container_name = f"bot{bot_number}"
        if container_name not in container_names:
            continue

        checked_with_token += 1
        checked_with_container += 1

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

        if status == 401 or payload.get("ok") is False:
            message = (
                "🚫 **Бот в бані або токен недійсний**\n\n"
                f"**bot_username:** @{bot_username}\n"
                f"**bot_number:** {bot_number}\n"
                "@Artemka1806 @redditmarketing"
            )
            try:
                await app.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
            except Exception as e:
                logger.error(f"Не вдалося відправити повідомлення про бан: {e}")
        elif status != 200:
            logger.warning(f"Неочікуваний статус getMyName для {bot_username}: {status}")

    logger.info(
        "Підсумок перевірки ботів: всього=%s, з токеном=%s, в контейнері=%s, викликів getMyName=%s",
        checked_total,
        checked_with_token,
        checked_with_container,
        checked_api_calls,
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
            await app.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


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
        workdir="./sessions"
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
        logger.info("Сервіс повністю зупинено.")


if __name__ == "__main__":
    asyncio.run(main())
