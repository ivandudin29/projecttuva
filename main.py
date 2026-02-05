import os
import logging
from datetime import datetime
from typing import Optional, List

import asyncpg
from dotenv import load_dotenv
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

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
PORT = int(os.getenv("PORT", 10000))

if not all([BOT_TOKEN, DATABASE_URL, WEBHOOK_URL]):
    raise ValueError("Missing required environment variables: BOT_TOKEN, DATABASE_URL, WEBHOOK_URL")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# FSM состояния
class ProjectStates(StatesGroup):
    waiting_for_project_name = State()
    waiting_for_task_title = State()
    waiting_for_task_deadline = State()

# Класс для работы с базой данных
class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None

    async def connect(self):
        """Создание пула подключений к PostgreSQL"""
        self.pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=10,
            command_timeout=60
        )
        await self._init_db()
        logger.info("Database connected successfully")

    async def _init_db(self):
        """Инициализация таблиц"""
        async with self.pool.acquire() as conn:
            # Таблица проектов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name TEXT NOT NULL
                )
            ''')
            # Таблица задач
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    project_id INT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    deadline DATE NOT NULL
                )
            ''')
            logger.info("Database tables initialized")

    async def close(self):
        """Закрытие пула подключений"""
        if self.pool:
            await self.pool.close()
            logger.info("Database connection closed")

    async def add_project(self, user_id: int, name: str) -> int:
        """Добавление нового проекта"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                'INSERT INTO projects (user_id, name) VALUES ($1, $2) RETURNING id',
                user_id, name
            )

    async def get_user_projects(self, user_id: int) -> List[asyncpg.Record]:
        """Получение всех проектов пользователя"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                'SELECT id, name FROM projects WHERE user_id = $1 ORDER BY id',
                user_id
            )

    async def delete_project(self, project_id: int):
        """Удаление проекта (CASCADE автоматически удалит задачи)"""
        async with self.pool.acquire() as conn:
            await conn.execute('DELETE FROM projects WHERE id = $1', project_id)

    async def add_task(self, project_id: int, title: str, deadline: datetime.date):
        """Добавление новой задачи"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO tasks (project_id, title, deadline) VALUES ($1, $2, $3)',
                project_id, title, deadline
            )

    async def get_project_tasks(self, project_id: int) -> List[asyncpg.Record]:
        """Получение всех задач проекта"""
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                '''SELECT id, title, deadline 
                   FROM tasks 
                   WHERE project_id = $1 
                   ORDER BY deadline''',
                project_id
            )

# Глобальный объект БД
db = Database()

# Reply-клавиатура главного меню
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Проект"), KeyboardButton(text="📂 Проекты")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

# Обработчики команд
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    await message.answer(
        "👋 Привет! Я бот-планировщик задач.\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard()
    )

@router.message(F.text == "➕ Проект")
async def add_project_start(message: Message, state: FSMContext):
    """Начало создания нового проекта"""
    await state.set_state(ProjectStates.waiting_for_project_name)
    await message.answer("Введите название проекта:")

@router.message(ProjectStates.waiting_for_project_name)
async def add_project_finish(message: Message, state: FSMContext):
    """Завершение создания проекта"""
    project_name = message.text.strip()
    
    if not project_name:
        await message.answer("Название проекта не может быть пустым. Попробуйте снова:")
        return
    
    try:
        project_id = await db.add_project(message.from_user.id, project_name)
        await message.answer(
            f"✅ Проект '{project_name}' создан!",
            reply_markup=get_main_keyboard()
        )
        logger.info(f"Project created: id={project_id}, name='{project_name}'")
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        await message.answer(
            "❌ Произошла ошибка при создании проекта. Попробуйте снова.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.message(F.text == "📂 Проекты")
async def show_projects(message: Message):
    """Показать список проектов пользователя"""
    try:
        projects = await db.get_user_projects(message.from_user.id)
        
        if not projects:
            await message.answer(
                "📭 У вас пока нет проектов. Создайте первый!",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Создаем inline-клавиатуру с проектами
        keyboard = []
        for project in projects:
            keyboard.append([
                InlineKeyboardButton(
                    text=f"📁 {project['name']}",
                    callback_data=f"project_{project['id']}"
                )
            ])
        
        await message.answer(
            "📂 Ваши проекты:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
    except Exception as e:
        logger.error(f"Error fetching projects: {e}")
        await message.answer(
            "❌ Произошла ошибка при загрузке проектов.",
            reply_markup=get_main_keyboard()
        )

@router.callback_query(F.data.startswith("project_"))
async def project_menu(callback: CallbackQuery):
    """Меню проекта (показать задачи или удалить)"""
    project_id = int(callback.data.split("_")[1])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Задачи", callback_data=f"tasks_{project_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{project_id}")
        ]
    ])
    
    await callback.message.edit_text(
        "Выберите действие с проектом:",
        reply_markup=keyboard
    )
    await callback.answer()

@router.callback_query(F.data.startswith("tasks_"))
async def show_tasks(callback: CallbackQuery):
    """Показать задачи проекта"""
    project_id = int(callback.data.split("_")[1])
    
    try:
        tasks = await db.get_project_tasks(project_id)
        
        if not tasks:
            tasks_text = "📭 Задач пока нет."
        else:
            tasks_text = "📋 Задачи проекта:\n\n"
            for task in tasks:
                deadline = task['deadline'].strftime("%d.%m.%y")
                tasks_text += f"• {task['title']} — {deadline}\n"
        
        # Добавляем кнопку для добавления задачи
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task_{project_id}")]
        ])
        
        await callback.message.edit_text(
            tasks_text,
            reply_markup=keyboard
        )
    except Exception as e:
        logger.error(f"Error fetching tasks: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при загрузке задач.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("add_task_"))
async def add_task_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления задачи"""
    project_id = int(callback.data.split("_")[2])
    
    await state.set_state(ProjectStates.waiting_for_task_title)
    await state.update_data(project_id=project_id)
    
    await callback.message.answer("Название задачи?")
    await callback.answer()

@router.message(ProjectStates.waiting_for_task_title)
async def add_task_title(message: Message, state: FSMContext):
    """Получение названия задачи"""
    title = message.text.strip()
    
    if not title:
        await message.answer("Название задачи не может быть пустым. Попробуйте снова:")
        return
    
    await state.update_data(title=title)
    await state.set_state(ProjectStates.waiting_for_task_deadline)
    
    await message.answer(
        "Дедлайн (в формате ДД.ММ.ГГ, например: 05.02.26)?"
    )

@router.message(ProjectStates.waiting_for_task_deadline)
async def add_task_deadline(message: Message, state: FSMContext):
    """Получение и валидация дедлайна задачи"""
    deadline_str = message.text.strip()
    
    try:
        # Парсинг даты
        deadline = datetime.strptime(deadline_str, "%d.%m.%y").date()
        
        # Проверка, что дата не в прошлом
        if deadline < datetime.now().date():
            await message.answer(
                "❌ Неверный формат или дата в прошлом. Попробуйте снова:"
            )
            return
        
        # Получение данных из состояния
        data = await state.get_data()
        project_id = data['project_id']
        title = data['title']
        
        # Сохранение задачи
        await db.add_task(project_id, title, deadline)
        
        await message.answer(
            f"✅ Задача '{title}' добавлена с дедлайном {deadline_str}!",
            reply_markup=get_main_keyboard()
        )
        logger.info(f"Task added: project={project_id}, title='{title}', deadline={deadline_str}")
        
    except ValueError:
        await message.answer(
            "❌ Неверный формат или дата в прошлом. Попробуйте снова:"
        )
        return
    except Exception as e:
        logger.error(f"Error adding task: {e}")
        await message.answer(
            "❌ Произошла ошибка при добавлении задачи.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data.startswith("delete_"))
async def delete_project(callback: CallbackQuery):
    """Удаление проекта"""
    project_id = int(callback.data.split("_")[1])
    
    try:
        await db.delete_project(project_id)
        await callback.message.edit_text("✅ Проект удален!")
        logger.info(f"Project deleted: id={project_id}")
    except Exception as e:
        logger.error(f"Error deleting project: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при удалении проекта.")
    
    await callback.answer()

# Обработка неизвестных сообщений
@router.message()
async def handle_other_messages(message: Message):
    """Обработка всех остальных сообщений"""
    await message.answer(
        "Выберите действие на клавиатуре:",
        reply_markup=get_main_keyboard()
    )

# Обработка ошибок
@router.errors()
async def error_handler(event, **kwargs):
    """Глобальный обработчик ошибок"""
    logger.error(f"Error occurred: {event.exception}")
    return True

# Основная функция запуска
async def on_startup(app: web.Application = None):
    """Действия при запуске бота"""
    # Подключение к БД
    await db.connect()
    
    # Установка webhook
    webhook_url = f"{WEBHOOK_URL}{WEBHOOK_PATH}"
    await bot.set_webhook(
        webhook_url,
        drop_pending_updates=True
    )
    logger.info(f"Webhook set to: {webhook_url}")

async def on_shutdown(app: web.Application = None):
    """Действия при остановке бота"""
    # Закрытие соединения с БД
    await db.close()
    
    # Удаление webhook
    await bot.delete_webhook()
    logger.info("Bot stopped")

def main():
    """Запуск приложения"""
    # Создание aiohttp приложения
    app = web.Application()
    
    # Настройка событий запуска и остановки
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Создание обработчика webhook
    webhook_requests_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=BOT_TOKEN
    )
    
    # Настройка маршрутов
    webhook_requests_handler.register(app, path=WEBHOOK_PATH)
    
    # Запуск сервера
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
