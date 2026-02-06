import os
import logging
import sys
from datetime import datetime
import asyncio
import traceback

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

# Инициализация
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

# Простые хендлеры для теста
@dp.message(CommandStart())
async def cmd_start(message: Message):
    logger.info(f"Получен /start от пользователя {message.from_user.id}")
    await message.answer(
        "🎉 Добро пожаловать в менеджер проектов!\n\n"
        "Выберите действие:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="➕ Проект"), KeyboardButton(text="📂 Проекты")]
            ],
            resize_keyboard=True,
            one_time_keyboard=False
        )
    )

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    logger.info(f"Получен /ping от пользователя {message.from_user.id}")
    await message.answer("🏓 Pong! Бот жив и работает")

@dp.message(Command("test"))
async def cmd_test(message: Message):
    logger.info(f"Получен /test от пользователя {message.from_user.id}")
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval('SELECT COUNT(*) FROM projects')
        await message.answer(f"✅ Бот работает! Проектов в базе: {count}")
    except Exception as e:
        logger.error(f"Ошибка в /test: {e}")
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

@dp.message(Command("echo"))
async def cmd_echo(message: Message):
    logger.info(f"Получен echo: {message.text}")
    await message.answer(f"Эхо: {message.text}")

@dp.message(F.text == "➕ Проект")
async def test_button(message: Message):
    logger.info(f"Нажата кнопка '➕ Проект' от {message.from_user.id}")
    await message.answer("Тестовая кнопка работает! Для создания проекта используйте команду /newproject")

@dp.message(F.text == "📂 Проекты")
async def test_button2(message: Message):
    logger.info(f"Нажата кнопка '📂 Проекты' от {message.from_user.id}")
    await message.answer("Тестовая кнопка работает! Для просмотра проектов используйте команду /listprojects")

# Webhook логика
async def on_startup(bot: Bot):
    """Установка вебхука при запуске"""
    logger.info("🔄 Установка вебхука...")
    
    try:
        # Даем время на инициализацию
        await asyncio.sleep(1)
        
        # Получаем информацию о текущем вебхуке
        current_webhook = await bot.get_webhook_info()
        logger.info(f"Текущий вебхук: {current_webhook.url}")
        logger.info(f"Ожидающих обновлений: {current_webhook.pending_update_count}")
        
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
        logger.info(f"✅ Информация о вебхуке: {webhook_info.url}")
        logger.info(f"✅ Ожидающих обновлений: {webhook_info.pending_update_count}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при установке вебхука: {e}")
        logger.error(traceback.format_exc())

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
        # Простая проверка без базы данных
        return web.Response(
            text="OK",
            status=200,
            headers={"Content-Type": "text/plain"}
        )
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return web.Response(
            text="ERROR",
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
            <p><strong>Last Error Date:</strong> {info.last_error_date or 'None'}</p>
            <p><strong>Max Connections:</strong> {info.max_connections or 'Not set'}</p>
            <p><strong>Service URL:</strong> https://{WEBHOOK_HOST}</p>
            <hr>
            <p>Health check: <a href="/health">/health</a></p>
            <p>Webhook endpoint: <a href="/webhook">/webhook</a></p>
            <p>Status page: <a href="/status">/status</a></p>
            <p>Test links:</p>
            <ul>
                <li><a href="/test">/test</a> - Простой тест</li>
                <li><a href="/debug">/debug</a> - Отладка</li>
            </ul>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        logger.error(f"Ошибка в webhook_info_page: {e}")
        return web.Response(text=f"Error getting webhook info: {e}", status=500)

async def test_page(request):
    """Тестовая страница"""
    return web.Response(
        text="Test page is working!",
        status=200,
        headers={"Content-Type": "text/plain"}
    )

async def debug_page(request):
    """Страница отладки"""
    debug_info = f"""
    Debug Information:
    - TOKEN: {'SET' if TOKEN else 'NOT SET'}
    - DATABASE_URL: {'SET' if DATABASE_URL else 'NOT SET'}
    - WEBHOOK_HOST: {WEBHOOK_HOST}
    - WEBHOOK_URL: {WEBHOOK_URL}
    - PORT: {PORT}
    """
    return web.Response(
        text=debug_info,
        status=200,
        headers={"Content-Type": "text/plain"}
    )

async def handle_webhook(request):
    """Обработчик вебхука вручную для отладки"""
    logger.info("Получен запрос на /webhook")
    try:
        data = await request.json()
        logger.info(f"Данные вебхука: {data}")
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Ошибка в handle_webhook: {e}")
        return web.Response(text="ERROR", status=500)

def main():
    """Запуск приложения"""
    logger.info("🚀 Запуск приложения...")
    
    # Регистрируем обработчики запуска/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    # Создаем веб-приложение
    app = web.Application()
    
    # Регистрируем вебхук через SimpleRequestHandler
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
    )
    webhook_handler.register(app, path="/webhook")
    
    # Добавляем дополнительные маршруты
    app.router.add_get("/", webhook_info_page)
    app.router.add_get("/health", health_check)
    app.router.add_get("/status", webhook_info_page)
    app.router.add_get("/test", test_page)
    app.router.add_get("/debug", debug_page)
    
    # Настраиваем приложение
    setup_application(app, dp, bot=bot)
    
    # Запускаем сервер
    logger.info(f"🚀 Запуск сервера на порту {PORT}")
    logger.info(f"🌐 Вебхук будет установлен на: {WEBHOOK_URL}")
    
    try:
        web.run_app(
            app,
            host="0.0.0.0",
            port=PORT,
            access_log=None
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске сервера: {e}")
        logger.error(traceback.format_exc())
        sys.exit(1)

if __name__ == "__main__":
    main()
