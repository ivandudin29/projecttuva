import os
import logging
import sys
from datetime import datetime
import asyncio

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, ReplyKeyboardMarkup, 
    KeyboardButton, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
import asyncpg

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("❌ BOT_TOKEN не найден! Установите переменную окружения")
    sys.exit(1)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    logger.error("❌ DATABASE_URL не найден!")
    sys.exit(1)

# Render автоматически устанавливает PORT (обычно 10000)
PORT = int(os.getenv("PORT", 8080))
# Для Render нужно использовать их домен
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if not WEBHOOK_HOST:
    logger.error("❌ RENDER_EXTERNAL_HOSTNAME не найден!")
    sys.exit(1)

WEBHOOK_URL = f"https://{WEBHOOK_HOST}/webhook"

logger.info(f"🚀 Конфигурация:")
logger.info(f"• PORT: {PORT}")
logger.info(f"• WEBHOOK_HOST: {WEBHOOK_HOST}")
logger.info(f"• WEBHOOK_URL: {WEBHOOK_URL}")
logger.info(f"• DATABASE_URL: {'Установлен' if DATABASE_URL else 'НЕТ!'}")

# Инициализация (исправлено для aiogram 3.7.0+)
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Глобальный пул подключений
db_pool = None

async def get_db_pool():
    """Создание пула подключений к PostgreSQL"""
    global db_pool
    if db_pool is None:
        try:
            logger.info("🔄 Создание пула подключений к PostgreSQL...")
            db_pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                command_timeout=60,
                server_settings={'search_path': 'public'}
            )
            logger.info("✅ Пул подключений создан")
        except Exception as e:
            logger.error(f"❌ Ошибка при создании пула подключений: {e}")
            raise
    return db_pool

# Создание таблиц если их нет
async def create_tables():
    """Создание таблиц projects и tasks если они не существуют"""
    try:
        logger.info("🔄 Проверка/создание таблиц...")
        pool = await get_db_pool()
        if not pool:
            logger.error("❌ Не удалось получить пул подключений")
            return False
            
        async with pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title VARCHAR(255) NOT NULL,
                    deadline DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            logger.info("✅ Таблицы созданы или уже существуют")
            return True
    except Exception as e:
        logger.error(f"❌ Ошибка при создании таблиц: {e}")
        return False

# ... (остальной код с хендлерами остается таким же как в предыдущей версии) ...

# Простая команда для теста
@dp.message(Command("test"))
async def cmd_test(message: Message):
    """Простая тестовая команда для проверки работы бота"""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval('SELECT COUNT(*) FROM projects')
        await message.answer(f"✅ Бот работает! Проектов в базе: {count}")
    except Exception as e:
        await message.answer(f"❌ Ошибка при работе с базой: {str(e)[:100]}")

@dp.message(Command("dbcheck"))
async def cmd_dbcheck(message: Message):
    """Проверка подключения к базе данных"""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            version = await conn.fetchval('SELECT version()')
            projects_count = await conn.fetchval('SELECT COUNT(*) FROM projects')
            tasks_count = await conn.fetchval('SELECT COUNT(*) FROM tasks')
        
        await message.answer(
            f"✅ База данных работает!\n"
            f"📊 PostgreSQL: {version.split()[0]}\n"
            f"📁 Проектов: {projects_count}\n"
            f"📝 Задач: {tasks_count}"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка подключения к базе: {str(e)[:200]}")

# Webhook логика
async def on_startup(bot: Bot):
    """Установка вебхука при запуске"""
    logger.info("🔄 Установка вебхука...")
    
    try:
        # Создаем таблицы
        success = await create_tables()
        if not success:
            logger.error("❌ Не удалось создать таблицы")
            # Не выходим, возможно таблицы уже созданы
            
        # Даем время на инициализацию
        await asyncio.sleep(2)
        
        # Удаляем старый вебхук
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Старый вебхук удален")
        
        # Устанавливаем новый
        await bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types()
        )
        logger.info(f"✅ Вебхук установлен: {WEBHOOK_URL}")
        
        # Проверяем
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Информация о вебхуке: {webhook_info.url}")
        logger.info(f"Ожидающих обновлений: {webhook_info.pending_update_count}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при установке вебхука: {e}")

async def on_shutdown(bot: Bot):
    """Очистка при выключении"""
    logger.info("🛑 Остановка бота...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        if db_pool:
            await db_pool.close()
        await bot.session.close()
        logger.info("Ресурсы освобождены")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

async def health_check(request):
    """Health check для Render"""
    try:
        # Проверяем подключение к базе
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.fetchval('SELECT 1')
        return web.Response(
            text="OK",
            status=200,
            headers={"Content-Type": "text/plain"}
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return web.Response(
            text="DATABASE ERROR",
            status=503,
            headers={"Content-Type": "text/plain"}
        )

async def webhook_info_page(request):
    """Страница с информацией о вебхуке"""
    try:
        info = await bot.get_webhook_info()
        html = f"""
        <html>
        <head><title>Telegram Bot Status</title></head>
        <body>
            <h1>🤖 Telegram Bot Status</h1>
            <p><strong>Webhook URL:</strong> {info.url or 'Not set'}</p>
            <p><strong>Pending Updates:</strong> {info.pending_update_count}</p>
            <p><strong>Last Error:</strong> {info.last_error_message or 'None'}</p>
            <p><strong>Service URL:</strong> https://{WEBHOOK_HOST}</p>
            <hr>
            <p>Health check: <a href="/health">/health</a></p>
            <p>Webhook endpoint: <a href="/webhook">/webhook</a></p>
            <p>Status page: <a href="/status">/status</a></p>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=500)

def main():
    """Запуск приложения"""
    logger.info("🚀 Запуск приложения...")
    
    # Регистрируем обработчики запуска/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Создаем веб-приложение
    app = web.Application()
    
    # Регистрируем вебхук
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    
    # Добавляем дополнительные маршруты
    app.router.add_get("/", webhook_info_page)
    app.router.add_get("/health", health_check)
    app.router.add_get("/status", webhook_info_page)
    
    # Настраиваем приложение
    setup_application(app, dp, bot=bot)
    
    # Запускаем сервер
    logger.info(f"🚀 Запуск сервера на порту {PORT}")
    logger.info(f"🌐 Вебхук будет установлен на: {WEBHOOK_URL}")
    
    try:
        web.run_app(
            app,
            host="0.0.0.0",  # Важно: слушаем все интерфейсы
            port=PORT,
            access_log=None  # Отключаем access логи чтобы не засорять
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске сервера: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
