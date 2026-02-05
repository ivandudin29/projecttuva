import os
import logging
from datetime import datetime
from typing import Optional, List

import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, 
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME") or "task-planner-bot.onrender.com"
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

logger.info(f"Config: BOT_TOKEN={BOT_TOKEN[:10]}..., DATABASE_URL={DATABASE_URL[:30]}..., WEBHOOK_URL={WEBHOOK_URL}")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен")

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# FSM
class ProjectStates(StatesGroup):
    waiting_for_project_name = State()

# Database
class Database:
    def __init__(self):
        self.pool = None
    
    async def connect(self):
        if DATABASE_URL:
            self.pool = await asyncpg.create_pool(DATABASE_URL)
            await self.init_db()
            logger.info("Database connected")
        else:
            logger.warning("DATABASE_URL не установлен, работаем без БД")
    
    async def init_db(self):
        if self.pool:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS projects (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        name TEXT NOT NULL
                    )
                ''')
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS tasks (
                        id SERIAL PRIMARY KEY,
                        project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        title TEXT NOT NULL,
                        deadline DATE NOT NULL
                    )
                ''')
    
    async def close(self):
        if self.pool:
            await self.pool.close()
    
    async def add_project(self, user_id: int, name: str):
        if self.pool:
            async with self.pool.acquire() as conn:
                return await conn.fetchval(
                    'INSERT INTO projects (user_id, name) VALUES ($1, $2) RETURNING id',
                    user_id, name
                )
        return None

db = Database()

# Handlers
@router.message(CommandStart())
async def start(message: Message):
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Проект"), KeyboardButton(text="📂 Проекты")]
        ],
        resize_keyboard=True
    )
    await message.answer("👋 Привет! Я бот-планировщик. Выберите действие:", reply_markup=keyboard)

@router.message(F.text == "➕ Проект")
async def add_project_start(message: Message, state: FSMContext):
    await state.set_state(ProjectStates.waiting_for_project_name)
    await message.answer("Введите название проекта:")

@router.message(ProjectStates.waiting_for_project_name)
async def add_project_finish(message: Message, state: FSMContext):
    name = message.text.strip()
    if name:
        try:
            await db.add_project(message.from_user.id, name)
            await message.answer(f"✅ Проект '{name}' создан!")
        except Exception as e:
            logger.error(f"Error: {e}")
            await message.answer("❌ Ошибка при создании проекта.")
    else:
        await message.answer("Название не может быть пустым.")
    await state.clear()

@router.message()
async def echo(message: Message):
    await message.answer(f"Вы написали: {message.text}")

# Health check
async def health_check(request):
    return web.Response(text="OK")

async def on_startup(app: web.Application):
    """Запуск при старте приложения"""
    await db.connect()
    
    # Устанавливаем вебхук
    await bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )
    logger.info(f"Webhook установлен: {WEBHOOK_URL}")
    
    # Проверяем информацию о вебхуке
    webhook_info = await bot.get_webhook_info()
    logger.info(f"Webhook info: {webhook_info}")

async def on_shutdown(app: web.Application):
    """Очистка при завершении"""
    await db.close()
    await bot.session.close()

def main():
    """Основная функция запуска"""
    app = web.Application()
    
    # Health check endpoint
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    # Создаем обработчик вебхуков БЕЗ secret_token
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
        # secret_token не нужен для Telegram!
    )
    
    # Регистрируем вебхук
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    # Настраиваем события
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Запускаем приложение
    logger.info(f"Запуск сервера на порту {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
