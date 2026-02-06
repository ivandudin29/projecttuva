import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    logger.error("❌ BOT_TOKEN не установлен!")
    raise ValueError("Установите BOT_TOKEN в переменных окружения")

WEBHOOK_URL = f"https://task-planner-bot.onrender.com/webhook"
PORT = int(os.getenv("PORT", 8080))

# Инициализация
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "✅ Бот работает!\n"
        f"Ваш ID: {message.from_user.id}\n"
        "Команды:\n"
        "/ping - проверка\n"
        "/id - ваш ID"
    )
    logger.info(f"Пользователь {message.from_user.id} запустил бота")

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    await message.answer("🏓 Pong!")
    logger.info(f"Ping от {message.from_user.id}")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(
        f"👤 Ваш ID: `{message.from_user.id}`\n"
        f"💬 ID чата: `{message.chat.id}`",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@dp.message()
async def echo(message: Message):
    await message.answer(f"Вы сказали: {message.text}")

async def on_startup():
    """Установка вебхука при запуске"""
    webhook_url = f"{WEBHOOK_URL}"
    
    # Удаляем старый вебхук
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Устанавливаем новый
    await bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True,
        allowed_updates=dp.resolve_used_update_types()
    )
    
    logger.info(f"✅ Вебхук установлен: {webhook_url}")
    
    # Проверяем
    webhook_info = await bot.get_webhook_info()
    logger.info(f"Информация о вебхуке: {webhook_info.url}")

async def on_shutdown():
    """Очистка при выключении"""
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.session.close()
    logger.info("Бот остановлен")

async def health_check(request):
    """Проверка здоровья"""
    return web.Response(text="Bot is running")

def main():
    """Запуск приложения"""
    logger.info(f"🚀 Запуск бота на порту {PORT}")
    
    # Регистрируем обработчики
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
    
    # Добавляем health check
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    # Настраиваем приложение
    setup_application(app, dp, bot=bot)
    
    # Запускаем сервер
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
