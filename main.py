import asyncio
import logging
import sys
import os
from typing import Optional

from aiogram import Bot, Dispatcher, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Конфигурация
TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = os.getenv("WEBHOOK_URL") + WEBHOOK_PATH if os.getenv("WEBHOOK_URL") else None
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", 8080))

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Инициализация
bot = Bot(token=TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Статистика бота
bot_stats = {
    "start_count": 0,
    "total_messages": 0
}

@router.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    """Обработчик команды /start"""
    bot_stats["start_count"] += 1
    bot_stats["total_messages"] += 1
    
    user = message.from_user
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я тестовый бот для отладки инфраструктуры.\n"
        f"Статистика бота:\n"
        f"• Запусков: {bot_stats['start_count']}\n"
        f"• Всего сообщений: {bot_stats['total_messages']}\n\n"
        f"Команды:\n"
        f"/start - Начальное сообщение\n"
        f"/ping - Проверка работы бота\n"
        f"/stats - Статистика\n"
        f"/id - Получить ID чата"
    )

@router.message(Command("ping"))
async def ping_handler(message: Message) -> None:
    """Обработчик команды /ping"""
    bot_stats["total_messages"] += 1
    await message.answer("🏓 Pong! Бот работает корректно.")

@router.message(Command("stats"))
async def stats_handler(message: Message) -> None:
    """Обработчик команды /stats"""
    bot_stats["total_messages"] += 1
    await message.answer(
        f"📊 Статистика бота:\n"
        f"• Запусков: {bot_stats['start_count']}\n"
        f"• Всего сообщений: {bot_stats['total_messages']}\n"
        f"• ID чата: {message.chat.id}"
    )

@router.message(Command("id"))
async def id_handler(message: Message) -> None:
    """Обработчик команды /id"""
    bot_stats["total_messages"] += 1
    await message.answer(
        f"📋 Информация о чате:\n"
        f"• ID чата: `{message.chat.id}`\n"
        f"• Тип чата: {message.chat.type}\n"
        f"• Ваш ID: `{message.from_user.id}`",
        parse_mode=ParseMode.MARKDOWN_V2
    )

@router.message()
async def echo_handler(message: Message) -> None:
    """Эхо-обработчик для всех сообщений"""
    bot_stats["total_messages"] += 1
    try:
        await message.answer(f"Эхо: {message.text}")
    except Exception as e:
        logger.error(f"Ошибка при отправке эхо: {e}")

async def on_startup(bot: Bot) -> None:
    """Действия при запуске бота"""
    logger.info("Бот запускается...")
    
    if WEBHOOK_URL:
        # Установка вебхука
        await bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types()
        )
        logger.info(f"Вебхук установлен: {WEBHOOK_URL}")
    else:
        logger.info("Режим поллинга")

async def on_shutdown(bot: Bot) -> None:
    """Действия при остановке бота"""
    logger.info("Бот останавливается...")
    
    if WEBHOOK_URL:
        # Удаление вебхука
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Вебхук удален")
    
    await bot.session.close()
    logger.info("Сессия закрыта")

async def main() -> None:
    """Основная функция запуска бота"""
    logger.info(f"Запуск бота на порту {PORT}")
    
    # Подключаем обработчики запуска/остановки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    if WEBHOOK_URL and os.getenv("RENDER"):
        # Режим вебхука (для Render)
        app = web.Application()
        webhook_requests_handler = SimpleRequestHandler(
            dispatcher=dp,
            bot=bot,
        )
        webhook_requests_handler.register(app, path=WEBHOOK_PATH)
        
        # Настраиваем CORS (опционально)
        async def health_check(request):
            return web.Response(text="Bot is running")
        
        app.router.add_get("/health", health_check)
        
        setup_application(app, dp, bot=bot)
        
        logger.info(f"Запуск веб-сервера на {HOST}:{PORT}")
        await web._run_app(app, host=HOST, port=PORT)
    else:
        # Режим поллинга (для локальной разработки)
        logger.info("Запуск в режиме поллинга...")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)

if __name__ == "__main__":
    # Проверка наличия токена
    if not TOKEN:
        logger.error("Токен бота не найден! Установите переменную BOT_TOKEN")
        sys.exit(1)
    
    # Запуск бота
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)
