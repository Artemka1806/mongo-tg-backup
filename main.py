import os
import subprocess
import asyncio
import functools
from datetime import datetime
from pathlib import Path
import logging
from pyrogram import Client
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import signal
import sys

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

try:
    TELEGRAM_CHAT_ID = int(os.getenv('TELEGRAM_CHAT_ID'))
except (ValueError, TypeError):
    logger.error(f"Змінна TELEGRAM_CHAT_ID ('{os.getenv('TELEGRAM_CHAT_ID')}') не є валідним числом! Перевірте .env файл.")
    sys.exit(1)

BACKUP_INTERVAL_MINUTES = int(os.getenv('BACKUP_INTERVAL_MINUTES', '5'))
KEEP_LOCAL_BACKUPS = int(os.getenv('KEEP_LOCAL_BACKUPS', '10'))
SESSION_NAME = os.getenv('SESSION_NAME', 'mongodb_backup_userbot')

# Прапорець для запобігання паралельного виконання
backup_in_progress = False

# Створення директорії для бекапів
Path(BACKUP_DIR).mkdir(parents=True, exist_ok=True)

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
            timeout=60 * 60 
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
        error_message = "Таймаут виконання mongodump (перевищено 60 хвилин)."
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
        
        await app.send_document(
            chat_id=TELEGRAM_CHAT_ID,
            document=file_path,
            caption=caption,
            progress=progress_callback
        )
        
        logger.info("Файл успішно відправлено в Telegram")
        
    except Exception as e:
        logger.error(f"Помилка при відправці в Telegram: {str(e)}", exc_info=True)

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
        
        await app.send_document(
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

        await send_latest_backup_on_startup(app)
        
        job = functools.partial(create_backup, app)
        scheduler.add_job(
            job,
            trigger=IntervalTrigger(minutes=BACKUP_INTERVAL_MINUTES),
            id='backup_job',
            name='MongoDB Backup Job',
            replace_existing=True,
            max_instances=1
        )
        
        scheduler.start()
        logger.info("Scheduler запущено")
        
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
