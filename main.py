import os
import logging
import sys
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

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

# Render автоматически устанавливает PORT (обычно 10000)
PORT = int(os.getenv("PORT", 8080))
# Для Render нужно использовать их домен
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME", f"localhost:{PORT}")
WEBHOOK_URL = f"https://{WEBHOOK_HOST}/webhook"

logger.info(f"🚀 Конфигурация:")
logger.info(f"• PORT: {PORT}")
logger.info(f"• WEBHOOK_URL: {WEBHOOK_URL}")
logger.info(f"• TOKEN: {'Установлен' if TOKEN else 'НЕТ!'}")

# Инициализация
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "🎉 Бот работает!\n"
        f"Ваш ID: {message.from_user.id}\n\n"
        "Команды:\n"
        "/ping - проверка связи\n"
        "/id - информация о чате\n"
        "/status - статус бота"
    )
    logger.info(f"Пользователь {message.from_user.id} запустил бота")

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer("🏓 Pong! Бот жив и работает")
    logger.info(f"Ping от {message.from_user.id}")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"👤 Ваш ID: `{message.from_user.id}`\n"
        f"💬 ID чата: `{message.chat.id}`\n"
        f"📝 Тип чата: {message.chat.type}",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@dp.message(Command("status"))
async def cmd_status(message: Message):
    await message.answer(
        f"✅ Бот работает на Render\n"
        f"🌐 URL: {WEBHOOK_HOST}\n"
        f"🔧 Порт: {PORT}"
    )

@dp.message()
async def echo(message: Message):
    await message.answer(f"Вы сказали: {message.text}")

async def on_startup(bot: Bot):
    """Установка вебхука при запуске"""
    logger.info("🔄 Установка вебхука...")
    
    try:
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
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.session.close()
    logger.info("Сессия закрыта")

async def health_check(request):
    """Health check для Render"""
    return web.Response(
        text="OK",
        status=200,
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
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=500)

def main():
    """Запуск приложения"""
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
    web.run_app(
        app,
        host="0.0.0.0",  # Важно: слушаем все интерфейсы
        port=PORT,
        access_log=logger
    )

if __name__ == "__main__":
    main()
