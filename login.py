#!/usr/bin/env python3
"""
Скрипт для авторизації Telegram USER BOT
Запустіть це ОДИН РАЗ перед першим використанням для створення session файлу
"""
import os
import asyncio
from pyrogram import Client
from dotenv import load_dotenv

# Завантаження змінних з .env
load_dotenv()

TELEGRAM_API_ID = os.getenv('TELEGRAM_API_ID')
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH')
SESSION_NAME = os.getenv('SESSION_NAME', 'mongodb_backup_userbot')

async def main():
    print("=" * 60)
    print("🔐 Авторизація Telegram USER BOT")
    print("=" * 60)
    print()
    
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("❌ TELEGRAM_API_ID або TELEGRAM_API_HASH не встановлені в .env файлі!")
        return
    
    print(f"📱 API ID: {TELEGRAM_API_ID}")
    print(f"🔑 API Hash: {TELEGRAM_API_HASH[:10]}...")
    print(f"💾 Session: {SESSION_NAME}")
    print()
    print("📝 Вам потрібно буде ввести:")
    print("   1. Номер телефону (з кодом країни, наприклад: +380123456789)")
    print("   2. Код підтвердження з Telegram")
    print("   3. Пароль 2FA (якщо увімкнено)")
    print()
    
    # Створення директорії для sessions
    os.makedirs("./sessions", exist_ok=True)
    
    # Створення клієнта
    app = Client(
        SESSION_NAME,
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH,
        workdir="./sessions"
    )
    
    print("🚀 Запуск авторизації...")
    print()
    
    try:
        # Запуск клієнта (тут буде інтерактивний запит номера та коду)
        await app.start()
        
        # Отримання інформації про акаунт
        me = await app.get_me()
        
        print()
        print("=" * 60)
        print("✅ Авторизація успішна!")
        print("=" * 60)
        print(f"👤 Ім'я: {me.first_name} {me.last_name or ''}")
        print(f"🆔 ID: {me.id}")
        print(f"📱 Username: @{me.username if me.username else 'немає'}")
        print(f"☎️  Телефон: {me.phone_number if me.phone_number else 'N/A'}")
        print()
        print(f"💾 Session файл створено: ./sessions/{SESSION_NAME}.session")
        print()
        print("🎉 Тепер ви можете запустити основний сервіс:")
        print("   docker-compose up -d --build")
        print("=" * 60)
        
        # Зупинка клієнта
        await app.stop()
        
    except Exception as e:
        print()
        print("=" * 60)
        print(f"❌ Помилка авторизації: {str(e)}")
        print("=" * 60)
        print()
        print("💡 Можливі причини:")
        print("   1. Неправильний API_ID або API_HASH")
        print("   2. Неправильний номер телефону")
        print("   3. Неправильний код підтвердження")
        print("   4. Проблеми з інтернет з'єднанням")
        print()
        print("Спробуйте ще раз!")

if __name__ == "__main__":
    asyncio.run(main())