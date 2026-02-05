import os
import logging
from datetime import datetime, date
from typing import Optional, List

import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
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

logger.info(f"Config loaded: BOT_TOKEN={BOT_TOKEN[:10]}...")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не установлен")

# Инициализация
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

# FSM состояния
class ProjectStates(StatesGroup):
    waiting_for_project_name = State()
    waiting_for_task_title = State()
    waiting_for_task_deadline = State()
    waiting_for_edit_task_title = State()
    waiting_for_edit_task_deadline = State()

# Класс для работы с базой данных
class Database:
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
    
    async def connect(self):
        """Подключение к базе данных"""
        try:
            if DATABASE_URL:
                self.pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=10
                )
                await self.init_db()
                logger.info("✅ Database connected successfully")
            else:
                logger.warning("⚠️ DATABASE_URL не установлен, работаем без БД")
        except Exception as e:
            logger.error(f"❌ Database connection error: {e}")
    
    async def init_db(self):
        """Инициализация таблиц"""
        if self.pool:
            try:
                async with self.pool.acquire() as conn:
                    # Таблица проектов
                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS projects (
                            id SERIAL PRIMARY KEY,
                            user_id BIGINT NOT NULL,
                            name TEXT NOT NULL,
                            created_at TIMESTAMP DEFAULT NOW()
                        )
                    ''')
                    
                    # Таблица задач
                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS tasks (
                            id SERIAL PRIMARY KEY,
                            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                            title TEXT NOT NULL,
                            description TEXT,
                            deadline DATE,
                            is_completed BOOLEAN DEFAULT FALSE,
                            created_at TIMESTAMP DEFAULT NOW(),
                            updated_at TIMESTAMP DEFAULT NOW()
                        )
                    ''')
                    
                    # Индексы для ускорения запросов
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)')
                    
                    logger.info("✅ Database tables initialized")
            except Exception as e:
                logger.error(f"❌ Database init error: {e}")
    
    async def close(self):
        """Закрытие соединения"""
        if self.pool:
            await self.pool.close()
            logger.info("Database connection closed")
    
    # Методы для проектов
    async def add_project(self, user_id: int, name: str) -> Optional[int]:
        """Добавление нового проекта"""
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                project_id = await conn.fetchval(
                    'INSERT INTO projects (user_id, name) VALUES ($1, $2) RETURNING id',
                    user_id, name
                )
                logger.info(f"Project added: id={project_id}, user={user_id}, name={name}")
                return project_id
        except Exception as e:
            logger.error(f"Error adding project: {e}")
            return None
    
    async def get_user_projects(self, user_id: int) -> List[asyncpg.Record]:
        """Получение всех проектов пользователя"""
        if not self.pool:
            return []
        try:
            async with self.pool.acquire() as conn:
                projects = await conn.fetch(
                    'SELECT id, name FROM projects WHERE user_id = $1 ORDER BY created_at DESC',
                    user_id
                )
                return projects
        except Exception as e:
            logger.error(f"Error getting projects: {e}")
            return []
    
    async def get_project_by_id(self, project_id: int) -> Optional[asyncpg.Record]:
        """Получение проекта по ID"""
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                project = await conn.fetchrow(
                    'SELECT id, name, user_id FROM projects WHERE id = $1',
                    project_id
                )
                return project
        except Exception as e:
            logger.error(f"Error getting project: {e}")
            return None
    
    async def delete_project(self, project_id: int) -> bool:
        """Удаление проекта"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('DELETE FROM projects WHERE id = $1', project_id)
                logger.info(f"Project deleted: id={project_id}")
                return True
        except Exception as e:
            logger.error(f"Error deleting project: {e}")
            return False
    
    async def update_project_name(self, project_id: int, new_name: str) -> bool:
        """Обновление названия проекта"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'UPDATE projects SET name = $1 WHERE id = $2',
                    new_name, project_id
                )
                return True
        except Exception as e:
            logger.error(f"Error updating project: {e}")
            return False
    
    # Методы для задач
    async def add_task(self, project_id: int, title: str, deadline: Optional[date] = None, description: str = "") -> bool:
        """Добавление новой задачи"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    '''INSERT INTO tasks (project_id, title, description, deadline) 
                       VALUES ($1, $2, $3, $4)''',
                    project_id, title, description, deadline
                )
                logger.info(f"Task added: project={project_id}, title={title}")
                return True
        except Exception as e:
            logger.error(f"Error adding task: {e}")
            return False
    
    async def get_project_tasks(self, project_id: int, show_completed: bool = False) -> List[asyncpg.Record]:
        """Получение задач проекта"""
        if not self.pool:
            return []
        try:
            async with self.pool.acquire() as conn:
                if show_completed:
                    tasks = await conn.fetch(
                        '''SELECT id, title, description, deadline, is_completed 
                           FROM tasks 
                           WHERE project_id = $1 
                           ORDER BY 
                             CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                             deadline,
                             created_at''',
                        project_id
                    )
                else:
                    tasks = await conn.fetch(
                        '''SELECT id, title, description, deadline, is_completed 
                           FROM tasks 
                           WHERE project_id = $1 AND is_completed = FALSE
                           ORDER BY 
                             CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                             deadline,
                             created_at''',
                        project_id
                    )
                return tasks
        except Exception as e:
            logger.error(f"Error getting tasks: {e}")
            return []
    
    async def get_task_by_id(self, task_id: int) -> Optional[asyncpg.Record]:
        """Получение задачи по ID"""
        if not self.pool:
            return None
        try:
            async with self.pool.acquire() as conn:
                task = await conn.fetchrow(
                    'SELECT id, title, description, deadline, is_completed, project_id FROM tasks WHERE id = $1',
                    task_id
                )
                return task
        except Exception as e:
            logger.error(f"Error getting task: {e}")
            return None
    
    async def update_task(self, task_id: int, title: str = None, description: str = None, 
                         deadline: date = None, is_completed: bool = None) -> bool:
        """Обновление задачи"""
        if not self.pool:
            return False
        
        updates = []
        values = []
        
        if title is not None:
            updates.append("title = $%d" % (len(values) + 1))
            values.append(title)
        
        if description is not None:
            updates.append("description = $%d" % (len(values) + 1))
            values.append(description)
        
        if deadline is not None:
            updates.append("deadline = $%d" % (len(values) + 1))
            values.append(deadline)
        
        if is_completed is not None:
            updates.append("is_completed = $%d" % (len(values) + 1))
            values.append(is_completed)
        
        if not updates:
            return False
        
        updates.append("updated_at = NOW()")
        values.append(task_id)
        
        try:
            async with self.pool.acquire() as conn:
                query = f'UPDATE tasks SET {", ".join(updates)} WHERE id = ${len(values)}'
                await conn.execute(query, *values)
                return True
        except Exception as e:
            logger.error(f"Error updating task: {e}")
            return False
    
    async def delete_task(self, task_id: int) -> bool:
        """Удаление задачи"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('DELETE FROM tasks WHERE id = $1', task_id)
                return True
        except Exception as e:
            logger.error(f"Error deleting task: {e}")
            return False
    
    async def toggle_task_completion(self, task_id: int) -> bool:
        """Переключение статуса выполнения задачи"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    '''UPDATE tasks 
                       SET is_completed = NOT is_completed, 
                           updated_at = NOW() 
                       WHERE id = $1''',
                    task_id
                )
                return True
        except Exception as e:
            logger.error(f"Error toggling task: {e}")
            return False
    
    async def get_today_tasks(self, user_id: int) -> List[asyncpg.Record]:
        """Получение задач на сегодня"""
        if not self.pool:
            return []
        try:
            today = date.today()
            async with self.pool.acquire() as conn:
                tasks = await conn.fetch(
                    '''SELECT t.id, t.title, p.name as project_name
                       FROM tasks t
                       JOIN projects p ON t.project_id = p.id
                       WHERE p.user_id = $1 
                         AND t.deadline = $2 
                         AND t.is_completed = FALSE
                       ORDER BY t.created_at''',
                    user_id, today
                )
                return tasks
        except Exception as e:
            logger.error(f"Error getting today tasks: {e}")
            return []
    
    async def get_overdue_tasks(self, user_id: int) -> List[asyncpg.Record]:
        """Получение просроченных задач"""
        if not self.pool:
            return []
        try:
            today = date.today()
            async with self.pool.acquire() as conn:
                tasks = await conn.fetch(
                    '''SELECT t.id, t.title, p.name as project_name, t.deadline
                       FROM tasks t
                       JOIN projects p ON t.project_id = p.id
                       WHERE p.user_id = $1 
                         AND t.deadline < $2 
                         AND t.is_completed = FALSE
                       ORDER BY t.deadline''',
                    user_id, today
                )
                return tasks
        except Exception as e:
            logger.error(f"Error getting overdue tasks: {e}")
            return []

# Глобальный объект БД
db = Database()

# Вспомогательные функции
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура главного меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Мои проекты"), KeyboardButton(text="➕ Новый проект")],
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="⚠️ Просроченные")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="ℹ️ Помощь")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

def format_date(d: Optional[date]) -> str:
    """Форматирование даты"""
    if not d:
        return "Без срока"
    return d.strftime("%d.%m.%Y")

def parse_date(date_str: str) -> Optional[date]:
    """Парсинг даты из строки"""
    try:
        # Пробуем разные форматы
        for fmt in ("%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(date_str.strip(), fmt).date()
            except ValueError:
                continue
        return None
    except Exception:
        return None

def format_task(task: asyncpg.Record, index: int = None) -> str:
    """Форматирование задачи для отображения"""
    prefix = f"{index}. " if index is not None else "• "
    status = "✅ " if task['is_completed'] else "⬜ "
    deadline = format_date(task['deadline'])
    
    result = f"{prefix}{status}<b>{task['title']}</b>"
    if task['description']:
        result += f"\n   📝 {task['description']}"
    result += f"\n   📅 {deadline}"
    
    return result

# Обработчики команд
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 <b>Добро пожаловать в Task Planner Bot!</b>\n\n"
        "Я помогу вам организовать ваши проекты и задачи.\n"
        "Используйте кнопки ниже для навигации:\n\n"
        "📂 <b>Мои проекты</b> - просмотр всех проектов\n"
        "➕ <b>Новый проект</b> - создание нового проекта\n"
        "📅 <b>Сегодня</b> - задачи на сегодня\n"
        "⚠️ <b>Просроченные</b> - просроченные задачи\n"
        "📊 <b>Статистика</b> - ваша статистика\n"
        "ℹ️ <b>Помощь</b> - справка по командам"
    )
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    """Помощь по командам"""
    help_text = (
        "📚 <b>Справка по командам:</b>\n\n"
        "Основные команды:\n"
        "/start - Начать работу с ботом\n"
        "/help - Показать эту справку\n"
        "/projects - Показать все проекты\n"
        "/today - Задачи на сегодня\n"
        "/overdue - Просроченные задачи\n\n"
        
        "Управление проектами:\n"
        "➕ <b>Новый проект</b> - создать проект\n"
        "📂 <b>Мои проекты</b> - список проектов\n\n"
        
        "Управление задачами:\n"
        "Внутри проекта используйте кнопки:\n"
        "📋 Задачи - просмотр задач\n"
        "➕ Задача - добавить задачу\n"
        "✏️ Редактировать - изменить проект\n"
        "🗑 Удалить - удалить проект\n\n"
        
        "Для задач доступны действия:\n"
        "✅/❌ - отметить как выполненную/невыполненную\n"
        "✏️ - редактировать задачу\n"
        "🗑 - удалить задачу\n\n"
        
        "<i>Просто следуйте инструкциям бота!</i>"
    )
    
    await message.answer(help_text, parse_mode="HTML")

@router.message(Command("projects"))
@router.message(F.text == "📂 Мои проекты")
async def show_projects(message: Message):
    """Показать все проекты пользователя"""
    try:
        projects = await db.get_user_projects(message.from_user.id)
        
        if not projects:
            await message.answer(
                "📭 У вас пока нет проектов. Создайте первый проект!",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Создаем inline-клавиатуру с проектами
        keyboard_buttons = []
        for project in projects:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📁 {project['name']}",
                    callback_data=f"project_{project['id']}"
                )
            ])
        
        # Добавляем кнопку для создания нового проекта
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Создать новый проект", callback_data="create_project")
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await message.answer(
            f"📂 <b>Ваши проекты</b> (всего: {len(projects)}):\n"
            "Выберите проект для управления:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing projects: {e}")
        await message.answer(
            "❌ Произошла ошибка при загрузке проектов. Попробуйте позже.",
            reply_markup=get_main_keyboard()
        )

@router.message(F.text == "➕ Новый проект")
async def add_project_start(message: Message, state: FSMContext):
    """Начало создания нового проекта"""
    await state.set_state(ProjectStates.waiting_for_project_name)
    await message.answer(
        "📝 <b>Создание нового проекта</b>\n\n"
        "Введите название проекта:",
        parse_mode="HTML"
    )

@router.message(ProjectStates.waiting_for_project_name)
async def add_project_finish(message: Message, state: FSMContext):
    """Завершение создания проекта"""
    project_name = message.text.strip()
    
    if not project_name:
        await message.answer(
            "❌ Название проекта не может быть пустым. Попробуйте снова:"
        )
        return
    
    if len(project_name) > 100:
        await message.answer(
            "❌ Название слишком длинное (макс. 100 символов). Попробуйте снова:"
        )
        return
    
    try:
        project_id = await db.add_project(message.from_user.id, project_name)
        
        if project_id:
            await message.answer(
                f"✅ <b>Проект создан!</b>\n\n"
                f"📁 Название: <code>{project_name}</code>\n"
                f"🆔 ID: <code>{project_id}</code>\n\n"
                f"Теперь вы можете добавить задачи в этот проект.",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
            logger.info(f"Project created: id={project_id}, name='{project_name}'")
        else:
            await message.answer(
                "❌ Не удалось создать проект. Возможно, проблема с базой данных.",
                reply_markup=get_main_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        await message.answer(
            "❌ Произошла ошибка при создании проекта. Попробуйте позже.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data == "create_project")
async def create_project_callback(callback: CallbackQuery, state: FSMContext):
    """Создание проекта из callback"""
    await state.set_state(ProjectStates.waiting_for_project_name)
    await callback.message.answer(
        "📝 <b>Создание нового проекта</b>\n\n"
        "Введите название проекта:",
        parse_mode="HTML"
    )
    await callback.answer()

@router.callback_query(F.data.startswith("project_"))
async def project_menu(callback: CallbackQuery):
    """Меню проекта"""
    project_id = int(callback.data.split("_")[1])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project:
            await callback.message.edit_text("❌ Проект не найден.")
            await callback.answer()
            return
        
        # Проверяем, принадлежит ли проект пользователю
        if project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ У вас нет доступа к этому проекту.")
            await callback.answer()
            return
        
        # Создаем клавиатуру для управления проектом
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Задачи", callback_data=f"tasks_{project_id}"),
                InlineKeyboardButton(text="➕ Задача", callback_data=f"add_task_{project_id}")
            ],
            [
                InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_project_{project_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_project_{project_id}")
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад к проектам", callback_data="back_to_projects")
            ]
        ])
        
        # Получаем количество задач
        tasks = await db.get_project_tasks(project_id)
        completed_tasks = sum(1 for t in tasks if t['is_completed'])
        
        await callback.message.edit_text(
            f"📁 <b>Проект: {project['name']}</b>\n\n"
            f"📊 Статистика:\n"
            f"• Всего задач: {len(tasks)}\n"
            f"• Выполнено: {completed_tasks}\n"
            f"• Осталось: {len(tasks) - completed_tasks}\n\n"
            f"Выберите действие:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in project menu: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data == "back_to_projects")
async def back_to_projects(callback: CallbackQuery):
    """Возврат к списку проектов"""
    await show_projects_callback(callback)

async def show_projects_callback(callback: CallbackQuery):
    """Показать проекты из callback"""
    try:
        projects = await db.get_user_projects(callback.from_user.id)
        
        if not projects:
            await callback.message.edit_text(
                "📭 У вас пока нет проектов. Создайте первый проект!"
            )
            return
        
        keyboard_buttons = []
        for project in projects:
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📁 {project['name']}",
                    callback_data=f"project_{project['id']}"
                )
            ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Создать новый проект", callback_data="create_project")
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(
            f"📂 <b>Ваши проекты</b> (всего: {len(projects)}):\n"
            "Выберите проект для управления:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing projects from callback: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")

@router.callback_query(F.data.startswith("tasks_"))
async def show_tasks(callback: CallbackQuery):
    """Показать задачи проекта"""
    project_id = int(callback.data.split("_")[1])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Доступ запрещен.")
            await callback.answer()
            return
        
        # Получаем задачи (только активные по умолчанию)
        tasks = await db.get_project_tasks(project_id, show_completed=False)
        
        if not tasks:
            tasks_text = "📭 Задач пока нет. Создайте первую задачу!"
        else:
            tasks_text = f"📋 <b>Задачи проекта '{project['name']}':</b>\n\n"
            for i, task in enumerate(tasks, 1):
                tasks_text += format_task(task, i) + "\n\n"
        
        # Создаем клавиатуру
        keyboard_buttons = []
        
        if tasks:
            keyboard_buttons.append([
                InlineKeyboardButton(text="✅ Показать выполненные", callback_data=f"show_completed_{project_id}")
            ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task_{project_id}")
        ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="⬅️ Назад к проекту", callback_data=f"project_{project_id}")
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(
            tasks_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing tasks: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при загрузке задач.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("show_completed_"))
async def show_completed_tasks(callback: CallbackQuery):
    """Показать выполненные задачи"""
    project_id = int(callback.data.split("_")[2])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Доступ запрещен.")
            await callback.answer()
            return
        
        # Получаем ВСЕ задачи (включая выполненные)
        tasks = await db.get_project_tasks(project_id, show_completed=True)
        completed_tasks = [t for t in tasks if t['is_completed']]
        
        if not completed_tasks:
            tasks_text = "✅ Выполненных задач пока нет."
        else:
            tasks_text = f"✅ <b>Выполненные задачи проекта '{project['name']}':</b>\n\n"
            for i, task in enumerate(completed_tasks, 1):
                tasks_text += format_task(task, i) + "\n\n"
        
        # Создаем клавиатуру
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Показать активные", callback_data=f"tasks_{project_id}"),
                InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task_{project_id}")
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад к проекту", callback_data=f"project_{project_id}")
            ]
        ])
        
        await callback.message.edit_text(
            tasks_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing completed tasks: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("add_task_"))
async def add_task_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления задачи"""
    project_id = int(callback.data.split("_")[2])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.message.answer("❌ Доступ запрещен.")
            await callback.answer()
            return
        
        await state.set_state(ProjectStates.waiting_for_task_title)
        await state.update_data(project_id=project_id, project_name=project['name'])
        
        await callback.message.answer(
            f"📝 <b>Добавление задачи в проект '{project['name']}'</b>\n\n"
            "Введите название задачи:",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error starting task addition: {e}")
        await callback.message.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.message(ProjectStates.waiting_for_task_title)
async def add_task_title(message: Message, state: FSMContext):
    """Получение названия задачи"""
    title = message.text.strip()
    
    if not title:
        await message.answer("❌ Название задачи не может быть пустым. Попробуйте снова:")
        return
    
    if len(title) > 200:
        await message.answer("❌ Название слишком длинное (макс. 200 символов). Попробуйте снова:")
        return
    
    await state.update_data(title=title)
    await state.set_state(ProjectStates.waiting_for_task_deadline)
    
    await message.answer(
        "📅 <b>Установите дедлайн для задачи:</b>\n\n"
        "Введите дату в формате <code>ДД.ММ.ГГГГ</code> или <code>ДД.ММ.ГГ</code>\n"
        "Например: <code>15.02.2024</code> или <code>15.02.24</code>\n\n"
        "Или отправьте 'нет', если дедлайн не нужен.",
        parse_mode="HTML"
    )

@router.message(ProjectStates.waiting_for_task_deadline)
async def add_task_deadline(message: Message, state: FSMContext):
    """Получение дедлайна и сохранение задачи"""
    deadline_str = message.text.strip().lower()
    deadline = None
    
    if deadline_str not in ['нет', 'no', 'без срока']:
        deadline = parse_date(deadline_str)
        
        if not deadline:
            await message.answer(
                "❌ Неверный формат даты. Пожалуйста, введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
                "Или отправьте 'нет', если дедлайн не нужен.",
                parse_mode="HTML"
            )
            return
        
        # Проверка, что дата не в прошлом (можно убрать, если нужно)
        if deadline < date.today():
            await message.answer(
                "⚠️ Дата в прошлом. Вы уверены?\n"
                "Отправьте 'да' для подтверждения или введите новую дату:"
            )
            await state.update_data(deadline=deadline, needs_confirmation=True)
            return
    
    data = await state.get_data()
    
    # Если нужна подтверждение для даты в прошлом
    if data.get('needs_confirmation'):
        if message.text.strip().lower() not in ['да', 'yes', 'конечно']:
            await message.answer("Введите новую дату:")
            await state.update_data(needs_confirmation=False)
            return
        deadline = data['deadline']
    
    project_id = data['project_id']
    title = data['title']
    project_name = data.get('project_name', 'проект')
    
    try:
        success = await db.add_task(project_id, title, deadline)
        
        if success:
            deadline_text = format_date(deadline) if deadline else "без срока"
            
            await message.answer(
                f"✅ <b>Задача добавлена!</b>\n\n"
                f"📝 Название: <code>{title}</code>\n"
                f"📁 Проект: <code>{project_name}</code>\n"
                f"📅 Дедлайн: <code>{deadline_text}</code>\n\n"
                f"Теперь вы можете просмотреть задачи в проекте.",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
            
            logger.info(f"Task added: project={project_id}, title='{title}'")
        else:
            await message.answer(
                "❌ Не удалось добавить задачу. Попробуйте позже.",
                reply_markup=get_main_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Error adding task: {e}")
        await message.answer(
            "❌ Произошла ошибка при добавлении задачи.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data.startswith("task_toggle_"))
async def toggle_task_completion(callback: CallbackQuery):
    """Переключение статуса выполнения задачи"""
    task_id = int(callback.data.split("_")[2])
    
    try:
        task = await db.get_task_by_id(task_id)
        
        if not task:
            await callback.answer("❌ Задача не найдена.")
            return
        
        project = await db.get_project_by_id(task['project_id'])
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        success = await db.toggle_task_completion(task_id)
        
        if success:
            new_status = "выполнена" if not task['is_completed'] else "не выполнена"
            await callback.answer(f"✅ Задача отмечена как {new_status}!")
            
            # Обновляем список задач
            await show_tasks(callback)
        else:
            await callback.answer("❌ Не удалось обновить задачу.")
    
    except Exception as e:
        logger.error(f"Error toggling task: {e}")
        await callback.answer("❌ Произошла ошибка.")

@router.callback_query(F.data.startswith("task_edit_"))
async def edit_task_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования задачи"""
    task_id = int(callback.data.split("_")[2])
    
    try:
        task = await db.get_task_by_id(task_id)
        
        if not task:
            await callback.answer("❌ Задача не найдена.")
            return
        
        project = await db.get_project_by_id(task['project_id'])
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        await state.set_state(ProjectStates.waiting_for_edit_task_title)
        await state.update_data(task_id=task_id, project_id=task['project_id'], current_title=task['title'])
        
        await callback.message.answer(
            f"✏️ <b>Редактирование задачи</b>\n\n"
            f"Текущее название: <code>{task['title']}</code>\n\n"
            f"Введите новое название задачи:",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error starting task edit: {e}")
        await callback.message.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.message(ProjectStates.waiting_for_edit_task_title)
async def edit_task_title(message: Message, state: FSMContext):
    """Получение нового названия задачи"""
    new_title = message.text.strip()
    
    if not new_title:
        await message.answer("❌ Название не может быть пустым. Попробуйте снова:")
        return
    
    await state.update_data(new_title=new_title)
    await state.set_state(ProjectStates.waiting_for_edit_task_deadline)
    
    data = await state.get_data()
    current_deadline = format_date(data.get('current_deadline'))
    
    await message.answer(
        f"📅 <b>Установите новый дедлайн:</b>\n\n"
        f"Текущий дедлайн: <code>{current_deadline}</code>\n\n"
        f"Введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
        f"Или отправьте 'нет', чтобы убрать дедлайн.",
        parse_mode="HTML"
    )

@router.message(ProjectStates.waiting_for_edit_task_deadline)
async def edit_task_deadline(message: Message, state: FSMContext):
    """Сохранение отредактированной задачи"""
    deadline_str = message.text.strip().lower()
    new_deadline = None
    
    if deadline_str not in ['нет', 'no', 'без срока']:
        new_deadline = parse_date(deadline_str)
        
        if not new_deadline:
            await message.answer(
                "❌ Неверный формат даты. Пожалуйста, введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
                "Или отправьте 'нет', чтобы убрать дедлайн.",
                parse_mode="HTML"
            )
            return
    
    data = await state.get_data()
    task_id = data['task_id']
    new_title = data['new_title']
    project_id = data['project_id']
    
    try:
        success = await db.update_task(
            task_id=task_id,
            title=new_title,
            deadline=new_deadline
        )
        
        if success:
            await message.answer(
                f"✅ <b>Задача обновлена!</b>\n\n"
                f"📝 Новое название: <code>{new_title}</code>\n"
                f"📅 Новый дедлайн: <code>{format_date(new_deadline)}</code>",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
            
            logger.info(f"Task updated: id={task_id}, title='{new_title}'")
        else:
            await message.answer(
                "❌ Не удалось обновить задачу.",
                reply_markup=get_main_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Error updating task: {e}")
        await message.answer(
            "❌ Произошла ошибка при обновлении задачи.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data.startswith("task_delete_"))
async def delete_task(callback: CallbackQuery):
    """Удаление задачи"""
    task_id = int(callback.data.split("_")[2])
    
    try:
        task = await db.get_task_by_id(task_id)
        
        if not task:
            await callback.answer("❌ Задача не найдена.")
            return
        
        project = await db.get_project_by_id(task['project_id'])
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        # Создаем клавиатуру для подтверждения
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_task_{task_id}"),
                InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"tasks_{task['project_id']}")
            ]
        ])
        
        await callback.message.edit_text(
            f"🗑 <b>Удаление задачи</b>\n\n"
            f"Вы уверены, что хотите удалить задачу?\n"
            f"<code>{task['title']}</code>\n\n"
            f"Это действие нельзя отменить!",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    except Exception as e:
        logger.error(f"Error deleting task: {e}")
        await callback.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_delete_task_"))
async def confirm_delete_task(callback: CallbackQuery):
    """Подтверждение удаления задачи"""
    task_id = int(callback.data.split("_")[3])
    
    try:
        task = await db.get_task_by_id(task_id)
        
        if not task:
            await callback.answer("❌ Задача не найдена.")
            return
        
        success = await db.delete_task(task_id)
        
        if success:
            await callback.message.edit_text(
                f"✅ Задача <code>{task['title']}</code> удалена!",
                parse_mode="HTML"
            )
            
            # Возвращаемся к списку задач
            await callback.answer("✅ Задача удалена!")
            
            # Обновляем список задач
            await show_tasks(callback)
        else:
            await callback.message.edit_text("❌ Не удалось удалить задачу.")
            await callback.answer()
    
    except Exception as e:
        logger.error(f"Error confirming task deletion: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
        await callback.answer()

@router.callback_query(F.data.startswith("edit_project_"))
async def edit_project_start(callback: CallbackQuery, state: FSMContext):
    """Начало редактирования проекта"""
    project_id = int(callback.data.split("_")[2])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        await state.set_state(ProjectStates.waiting_for_project_name)
        await state.update_data(editing_project_id=project_id, current_name=project['name'])
        
        await callback.message.answer(
            f"✏️ <b>Редактирование проекта</b>\n\n"
            f"Текущее название: <code>{project['name']}</code>\n\n"
            f"Введите новое название проекта:",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error starting project edit: {e}")
        await callback.message.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("delete_project_"))
async def delete_project_start(callback: CallbackQuery):
    """Начало удаления проекта"""
    project_id = int(callback.data.split("_")[2])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        # Получаем количество задач в проекте
        tasks = await db.get_project_tasks(project_id, show_completed=True)
        
        # Создаем клавиатуру для подтверждения
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_project_{project_id}"),
                InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"project_{project_id}")
            ]
        ])
        
        await callback.message.edit_text(
            f"🗑 <b>Удаление проекта</b>\n\n"
            f"Вы уверены, что хотите удалить проект?\n"
            f"<code>{project['name']}</code>\n\n"
            f"📊 В проекте {len(tasks)} задач.\n"
            f"⚠️ Все задачи будут удалены безвозвратно!\n\n"
            f"Это действие нельзя отменить!",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    except Exception as e:
        logger.error(f"Error starting project deletion: {e}")
        await callback.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_delete_project_"))
async def confirm_delete_project(callback: CallbackQuery):
    """Подтверждение удаления проекта"""
    project_id = int(callback.data.split("_")[3])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project:
            await callback.answer("❌ Проект не найден.")
            return
        
        success = await db.delete_project(project_id)
        
        if success:
            await callback.message.edit_text(
                f"✅ Проект <code>{project['name']}</code> удален!",
                parse_mode="HTML"
            )
            
            # Возвращаемся к списку проектов
            await callback.answer("✅ Проект удален!")
            await show_projects_callback(callback)
        else:
            await callback.message.edit_text("❌ Не удалось удалить проект.")
            await callback.answer()
    
    except Exception as e:
        logger.error(f"Error confirming project deletion: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
        await callback.answer()

@router.message(F.text == "📅 Сегодня")
@router.message(Command("today"))
async def show_today_tasks(message: Message):
    """Показать задачи на сегодня"""
    try:
        tasks = await db.get_today_tasks(message.from_user.id)
        
        if not tasks:
            await message.answer(
                "🎉 <b>Задач на сегодня нет!</b>\n\n"
                "Можете отдохнуть или заняться планированием на будущее.",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
            return
        
        tasks_text = "📅 <b>Задачи на сегодня:</b>\n\n"
        for i, task in enumerate(tasks, 1):
            tasks_text += f"{i}. <b>{task['title']}</b>\n"
            tasks_text += f"   📁 Проект: {task['project_name']}\n\n"
        
        await message.answer(
            tasks_text,
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing today tasks: {e}")
        await message.answer(
            "❌ Произошла ошибка при загрузке задач.",
            reply_markup=get_main_keyboard()
        )

@router.message(F.text == "⚠️ Просроченные")
@router.message(Command("overdue"))
async def show_overdue_tasks(message: Message):
    """Показать просроченные задачи"""
    try:
        tasks = await db.get_overdue_tasks(message.from_user.id)
        
        if not tasks:
            await message.answer(
                "✅ <b>Нет просроченных задач!</b>\n\n"
                "Отличная работа! Вы успеваете по всем дедлайнам.",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
            return
        
        tasks_text = "⚠️ <b>Просроченные задачи:</b>\n\n"
        for i, task in enumerate(tasks, 1):
            overdue_days = (date.today() - task['deadline']).days
            tasks_text += f"{i}. <b>{task['title']}</b>\n"
            tasks_text += f"   📁 Проект: {task['project_name']}\n"
            tasks_text += f"   📅 Просрочено на: {overdue_days} д.\n\n"
        
        await message.answer(
            tasks_text,
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing overdue tasks: {e}")
        await message.answer(
            "❌ Произошла ошибка при загрузке задач.",
            reply_markup=get_main_keyboard()
        )

@router.message(F.text == "📊 Статистика")
async def show_statistics(message: Message):
    """Показать статистику пользователя"""
    try:
        projects = await db.get_user_projects(message.from_user.id)
        
        if not projects:
            await message.answer(
                "📊 <b>Ваша статистика</b>\n\n"
                "Проектов: 0\n"
                "Задач: 0\n\n"
                "Создайте первый проект, чтобы начать!",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
            return
        
        # Собираем статистику
        total_tasks = 0
        completed_tasks = 0
        today_tasks = 0
        overdue_tasks = 0
        
        for project in projects:
            tasks = await db.get_project_tasks(project['id'], show_completed=True)
            total_tasks += len(tasks)
            completed_tasks += sum(1 for t in tasks if t['is_completed'])
        
        today_tasks_list = await db.get_today_tasks(message.from_user.id)
        today_tasks = len(today_tasks_list)
        
        overdue_tasks_list = await db.get_overdue_tasks(message.from_user.id)
        overdue_tasks = len(overdue_tasks_list)
        
        # Рассчитываем прогресс
        progress = (completed_tasks / total_tasks * 100) if total_tasks > 0 else 0
        
        stats_text = (
            f"📊 <b>Ваша статистика</b>\n\n"
            f"📁 <b>Проекты:</b> {len(projects)}\n"
            f"📋 <b>Всего задач:</b> {total_tasks}\n"
            f"✅ <b>Выполнено:</b> {completed_tasks}\n"
            f"⬜ <b>В работе:</b> {total_tasks - completed_tasks}\n"
            f"📅 <b>На сегодня:</b> {today_tasks}\n"
            f"⚠️ <b>Просрочено:</b> {overdue_tasks}\n\n"
            f"📈 <b>Прогресс:</b> {progress:.1f}%\n"
        )
        
        # Добавляем прогресс-бар
        progress_bar_length = 10
        filled = int(progress / 100 * progress_bar_length)
        progress_bar = "█" * filled + "░" * (progress_bar_length - filled)
        stats_text += f"   {progress_bar}\n\n"
        
        if progress == 100:
            stats_text += "🎉 <i>Отличная работа! Все задачи выполнены!</i>"
        elif progress > 70:
            stats_text += "👏 <i>Хороший прогресс! Продолжайте в том же духе!</i>"
        elif progress > 30:
            stats_text += "💪 <i>Держите темп! Вы на верном пути!</i>"
        else:
            stats_text += "🚀 <i>Время начинать! Каждый день - новый шаг!</i>"
        
        await message.answer(
            stats_text,
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing statistics: {e}")
        await message.answer(
            "❌ Произошла ошибка при загрузке статистики.",
            reply_markup=get_main_keyboard()
        )

@router.message()
async def handle_other_messages(message: Message):
    """Обработка всех остальных сообщений"""
    await message.answer(
        "🤖 <b>Используйте кнопки ниже для навигации:</b>\n\n"
        "Или отправьте /help для справки по командам.",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

# Обработка ошибок
@router.errors()
async def error_handler(event, **kwargs):
    """Глобальный обработчик ошибок"""
    logger.error(f"Unhandled error: {event.exception}", exc_info=True)
    return True

# Health check endpoint
async def health_check(request):
    """Проверка работоспособности"""
    return web.Response(text="OK")

async def on_startup(app: web.Application):
    """Действия при запуске"""
    logger.info("Starting up...")
    
    # Подключение к БД
    await db.connect()
    
    # Установка webhook
    await bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"]
    )
    
    logger.info(f"Webhook set to: {WEBHOOK_URL}")
    
    # Проверка вебхука
    webhook_info = await bot.get_webhook_info()
    logger.info(f"Webhook info: {webhook_info.url}")

async def on_shutdown(app: web.Application):
    """Действия при остановке"""
    logger.info("Shutting down...")
    
    # Удаление webhook
    await bot.delete_webhook()
    
    # Закрытие соединения с БД
    await db.close()
    
    logger.info("Bot stopped successfully")

def main():
    """Основная функция запуска"""
    app = web.Application()
    
    # Health check endpoints
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    
    # Создаем обработчик вебхуков
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot
    )
    
    # Регистрируем вебхук
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    # Настраиваем события запуска/остановки
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    # Запускаем приложение
    logger.info(f"Starting server on port {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
