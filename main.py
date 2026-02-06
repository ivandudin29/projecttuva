import os
import asyncio
import logging
import sys
from datetime import datetime

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

logger.info("🚀 Инициализация бота...")

# Инициализация
bot = Bot(
    token=TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

# Подключение к базе данных
async def get_db_pool():
    """Создание пула подключений к PostgreSQL"""
    try:
        logger.info("🔄 Подключение к PostgreSQL...")
        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        logger.info("✅ Подключено к PostgreSQL")
        return pool
    except Exception as e:
        logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")
        return None

# Создание таблиц если их нет
async def create_tables():
    """Создание таблиц projects и tasks если они не существуют"""
    try:
        logger.info("🔄 Проверка таблиц...")
        pool = await get_db_pool()
        if not pool:
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
            
            logger.info("✅ Таблицы готовы")
            return True
    except Exception as e:
        logger.error(f"❌ Ошибка при создании таблиц: {e}")
        return False

# FSM States
class ProjectState(StatesGroup):
    waiting_for_name = State()

class TaskState(StatesGroup):
    waiting_for_title = State()
    waiting_for_deadline = State()

# Reply клавиатура для главного меню
def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Проект"), KeyboardButton(text="📂 Проекты")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard

# Inline клавиатура для проекта
def get_project_keyboard(project_id: int):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Задачи", callback_data=f"tasks:{project_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{project_id}")
            ]
        ]
    )
    return keyboard

# Inline клавиатура для задач
def get_tasks_keyboard(project_id: int):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task:{project_id}")]
        ]
    )
    return keyboard

# Хендлеры
@dp.message(CommandStart())
async def cmd_start(message: Message):
    logger.info(f"📨 /start от {message.from_user.id}")
    await message.answer(
        "🎉 Добро пожаловать в менеджер проектов!\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    logger.info(f"🏓 /ping от {message.from_user.id}")
    await message.answer("🏓 Pong! Бот жив и работает")

@dp.message(Command("test"))
async def cmd_test(message: Message):
    logger.info(f"🧪 /test от {message.from_user.id}")
    await message.answer("✅ Тест пройден! Бот работает!")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    logger.info(f"🆔 /id от {message.from_user.id}")
    await message.answer(f"Ваш ID: {message.from_user.id}")

# Создание проекта
@dp.message(F.text == "➕ Проект")
async def start_create_project(message: Message, state: FSMContext):
    logger.info(f"📝 Создание проекта от {message.from_user.id}")
    await message.answer("Введите название проекта:")
    await state.set_state(ProjectState.waiting_for_name)

@dp.message(ProjectState.waiting_for_name)
async def process_project_name(message: Message, state: FSMContext):
    project_name = message.text.strip()
    
    if not project_name:
        await message.answer("Название проекта не может быть пустым. Попробуйте еще раз:")
        return
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO projects (user_id, name) VALUES ($1, $2)",
                message.from_user.id, project_name
            )
        
        await message.answer(f"✅ Проект '{project_name}' создан!", reply_markup=get_main_keyboard())
        logger.info(f"✅ Проект создан: {project_name}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при создании проекта: {e}")
        await message.answer("❌ Произошла ошибка при создании проекта.")
    
    await state.clear()

# Просмотр проектов
@dp.message(F.text == "📂 Проекты")
async def show_projects(message: Message):
    logger.info(f"📁 Просмотр проектов от {message.from_user.id}")
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            projects = await conn.fetch(
                "SELECT id, name FROM projects WHERE user_id = $1 ORDER BY created_at DESC",
                message.from_user.id
            )
        
        if not projects:
            await message.answer(
                "У вас пока нет проектов. Нажмите ➕ Проект.",
                reply_markup=get_main_keyboard()
            )
            return
        
        for project in projects:
            await message.answer(
                f"📁 {project['name']}",
                reply_markup=get_project_keyboard(project['id'])
            )
            
    except Exception as e:
        logger.error(f"❌ Ошибка при получении проектов: {e}")
        await message.answer("❌ Произошла ошибка при получении проектов.")

# Callback для кнопок проекта
@dp.callback_query(F.data.startswith("tasks:"))
async def show_tasks(callback: CallbackQuery):
    project_id = int(callback.data.split(":")[1])
    logger.info(f"📋 Задачи проекта {project_id} от {callback.from_user.id}")
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT name FROM projects WHERE id = $1 AND user_id = $2",
                project_id, callback.from_user.id
            )
            
            if not project:
                await callback.answer("Проект не найден!")
                return
            
            tasks = await conn.fetch(
                "SELECT title, deadline FROM tasks WHERE project_id = $1 ORDER BY deadline ASC",
                project_id
            )
        
        if not tasks:
            message_text = f"📁 Проект: {project['name']}\n\nЗадач пока нет."
        else:
            message_text = f"📁 Проект: {project['name']}\n\n📋 Задачи:\n"
            for task in tasks:
                deadline = task['deadline'].strftime('%d.%m.%y')
                message_text += f"• {task['title']} — {deadline}\n"
        
        await callback.message.edit_text(
            message_text,
            reply_markup=get_tasks_keyboard(project_id)
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении задач: {e}")
        await callback.answer("❌ Произошла ошибка.")

# Удаление проекта
@dp.callback_query(F.data.startswith("delete:"))
async def delete_project(callback: CallbackQuery):
    project_id = int(callback.data.split(":")[1])
    logger.info(f"🗑 Удаление проекта {project_id} от {callback.from_user.id}")
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT name FROM projects WHERE id = $1 AND user_id = $2",
                project_id, callback.from_user.id
            )
            
            if not project:
                await callback.answer("Проект не найден!")
                return
            
            await conn.execute("DELETE FROM projects WHERE id = $1", project_id)
        
        await callback.message.edit_text(f"🗑 Проект '{project['name']}' удален.")
        await callback.answer("✅ Проект удален!")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при удалении проекта: {e}")
        await callback.answer("❌ Произошла ошибка при удалении.")

# Добавление задачи
@dp.callback_query(F.data.startswith("add_task:"))
async def start_add_task(callback: CallbackQuery, state: FSMContext):
    project_id = int(callback.data.split(":")[1])
    logger.info(f"➕ Добавление задачи в проект {project_id}")
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT id FROM projects WHERE id = $1 AND user_id = $2",
                project_id, callback.from_user.id
            )
            
            if not project:
                await callback.answer("Проект не найден!")
                return
    
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке проекта: {e}")
        await callback.answer("❌ Произошла ошибка.")
        return
    
    await state.update_data(project_id=project_id)
    await callback.message.answer("Название задачи?")
    await state.set_state(TaskState.waiting_for_title)
    await callback.answer()

@dp.message(TaskState.waiting_for_title)
async def process_task_title(message: Message, state: FSMContext):
    title = message.text.strip()
    
    if not title:
        await message.answer("Название задачи не может быть пустым. Введите название:")
        return
    
    await state.update_data(title=title)
    await message.answer("Дедлайн (ДД.ММ.ГГ, например: 05.02.26)?")
    await state.set_state(TaskState.waiting_for_deadline)

@dp.message(TaskState.waiting_for_deadline)
async def process_task_deadline(message: Message, state: FSMContext):
    deadline_str = message.text.strip()
    
    # Валидация формата даты
    try:
        deadline = datetime.strptime(deadline_str, '%d.%m.%y').date()
        today = datetime.now().date()
        if deadline < today:
            raise ValueError("Дата в прошлом")
            
    except ValueError as e:
        logger.warning(f"Неверный формат даты: {deadline_str}")
        await message.answer(
            "❌ Неверный формат или дата в прошлом. Попробуйте снова (ДД.ММ.ГГ):"
        )
        return
    
    # Сохранение задачи
    data = await state.get_data()
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO tasks (project_id, title, deadline) VALUES ($1, $2, $3)",
                data['project_id'], data['title'], deadline
            )
        
        await message.answer("✅ Задача добавлена!")
        logger.info(f"✅ Задача добавлена в проект {data['project_id']}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при сохранении задачи: {e}")
        await message.answer("❌ Произошла ошибка при сохранении задачи.")
    
    await state.clear()

# Простое эхо для теста
@dp.message()
async def echo_message(message: Message):
    logger.info(f"📨 Сообщение от {message.from_user.id}: {message.text}")
    await message.answer(f"Вы сказали: {message.text}")

# Основная функция
async def main():
    """Основная функция для запуска бота"""
    logger.info("🚀 Запуск бота в режиме polling...")
    
    # Удаляем возможный вебхук
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("✅ Вебхук удален (если был)")
    
    # Создаем таблицы
    await create_tables()
    
    # Запускаем polling
    logger.info("✅ Бот запущен и готов к работе!")
    logger.info("📱 Отправьте /start вашему боту в Telegram")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен")
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}")
