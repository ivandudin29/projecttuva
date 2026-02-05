#!/usr/bin/env python3
"""
Task Planner Bot - Telegram бот для управления проектами и задачами
Версия для развертывания на Render с использованием webhook
"""

import os
import logging
import asyncio
from datetime import datetime, date
from typing import Optional, List, Dict, Any
from threading import Thread
import time

import asyncpg
from aiogram import Bot, Dispatcher, Router, F, html
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardRemove
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiohttp import web
import aiohttp

# ==================== НАСТРОЙКА ЛОГГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
# Получаем переменные окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "SECRET_TOKEN")

# Webhook настройки
RENDER_EXTERNAL_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME", "task-planner-bot.onrender.com")
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{RENDER_EXTERNAL_HOSTNAME}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))

# Проверка обязательных переменных
required_env_vars = ["BOT_TOKEN"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]

if missing_vars:
    logger.warning(f"⚠️ Отсутствуют переменные окружения: {missing_vars}")
    logger.warning("Продолжаем работу, но некоторые функции могут не работать")

logger.info("=" * 50)
logger.info("Task Planner Bot Configuration:")
logger.info(f"Bot Token: {'Present' if BOT_TOKEN else 'Missing'}")
logger.info(f"Database URL: {'Present' if DATABASE_URL else 'Missing'}")
logger.info(f"Webhook URL: {WEBHOOK_URL}")
logger.info(f"Port: {PORT}")
logger.info("=" * 50)

if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN не установлен! Создайте бота через @BotFather и установите токен.")

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
# Инициализация бота с настройками по умолчанию
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)

# Инициализация диспетчера
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# ==================== FSM СОСТОЯНИЯ ====================
class ProjectStates(StatesGroup):
    """Состояния для управления проектами"""
    waiting_for_project_name = State()
    waiting_for_task_title = State()
    waiting_for_task_deadline = State()

# ==================== БАЗА ДАННЫХ ====================
class Database:
    """Класс для работы с PostgreSQL базой данных"""
    
    def __init__(self):
        self.pool: Optional[asyncpg.Pool] = None
        self.connection_attempts = 0
        self.max_attempts = 3
    
    async def connect(self) -> bool:
        """Подключение к базе данных"""
        if not DATABASE_URL:
            logger.warning("⚠️ DATABASE_URL не установлен, работаем без БД")
            return False
        
        while self.connection_attempts < self.max_attempts:
            try:
                self.connection_attempts += 1
                logger.info(f"Попытка подключения к БД #{self.connection_attempts}")
                
                self.pool = await asyncpg.create_pool(
                    DATABASE_URL,
                    min_size=1,
                    max_size=10,
                    command_timeout=30,
                    server_settings={'search_path': 'public'}
                )
                
                await self.init_db()
                logger.info("✅ База данных подключена успешно")
                return True
                
            except Exception as e:
                logger.error(f"❌ Ошибка подключения к БД (попытка {self.connection_attempts}): {e}")
                if self.connection_attempts < self.max_attempts:
                    await asyncio.sleep(2)  # Ждем перед повторной попыткой
        
        logger.error("❌ Не удалось подключиться к базе данных после нескольких попыток")
        return False
    
    async def init_db(self):
        """Инициализация таблиц в базе данных"""
        if not self.pool:
            return
        
        try:
            async with self.pool.acquire() as conn:
                # Таблица проектов - добавляем updated_at
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS projects (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        name VARCHAR(255) NOT NULL,
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Таблица задач - добавляем updated_at
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS tasks (
                        id SERIAL PRIMARY KEY,
                        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        title VARCHAR(500) NOT NULL,
                        description TEXT,
                        deadline DATE,
                        status VARCHAR(20) DEFAULT 'active' CHECK (status IN ('active', 'completed', 'archived')),
                        created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                
                # Создаем индексы для ускорения запросов
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_projects_user_id 
                    ON projects(user_id)
                ''')
                
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_tasks_project_id 
                    ON tasks(project_id)
                ''')
                
                await conn.execute('''
                    CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline 
                    ON tasks(status, deadline) WHERE status = 'active'
                ''')
                
                # Если нужно добавить столбец updated_at к существующей таблице
                try:
                    await conn.execute('ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP')
                    await conn.execute('ALTER TABLE projects ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP')
                except Exception as e:
                    logger.warning(f"Не удалось добавить столбец updated_at (возможно уже существует): {e}")
                
                logger.info("✅ Таблицы базы данных инициализированы")
                
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации БД: {e}")
            raise
    
    async def close(self):
        """Закрытие соединения с базой данных"""
        if self.pool:
            await self.pool.close()
            logger.info("🔌 Соединение с базой данных закрыто")
    
    async def health_check(self) -> bool:
        """Проверка работоспособности базы данных"""
        if not self.pool:
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка проверки здоровья БД: {e}")
            return False
    
    # ========== МЕТОДЫ ДЛЯ РАБОТЫ С ПРОЕКТАМИ ==========
    
    async def add_project(self, user_id: int, name: str) -> Optional[int]:
        """Добавление нового проекта"""
        if not self.pool:
            logger.warning("Нет подключения к БД, проект не сохранен")
            return None
        
        try:
            async with self.pool.acquire() as conn:
                project_id = await conn.fetchval('''
                    INSERT INTO projects (user_id, name)
                    VALUES ($1, $2)
                    RETURNING id
                ''', user_id, name[:255])
                
                logger.info(f"✅ Проект добавлен: ID={project_id}, пользователь={user_id}")
                return project_id
                
        except Exception as e:
            logger.error(f"❌ Ошибка при добавлении проекта: {e}")
            return None
    
    async def get_user_projects(self, user_id: int) -> List[Dict[str, Any]]:
        """Получение всех проектов пользователя"""
        if not self.pool:
            return []
        
        try:
            async with self.pool.acquire() as conn:
                projects = await conn.fetch('''
                    SELECT 
                        p.id,
                        p.name,
                        p.created_at,
                        COUNT(t.id) as total_tasks,
                        COUNT(CASE WHEN t.status = 'active' THEN 1 END) as active_tasks
                    FROM projects p
                    LEFT JOIN tasks t ON p.id = t.project_id
                    WHERE p.user_id = $1
                    GROUP BY p.id, p.name, p.created_at
                    ORDER BY p.created_at DESC
                ''', user_id)
                
                return [dict(project) for project in projects]
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении проектов: {e}")
            return []
    
    async def get_project_by_id(self, project_id: int) -> Optional[Dict[str, Any]]:
        """Получение проекта по ID"""
        if not self.pool:
            return None
        
        try:
            async with self.pool.acquire() as conn:
                project = await conn.fetchrow('''
                    SELECT id, name, user_id, created_at
                    FROM projects
                    WHERE id = $1
                ''', project_id)
                
                return dict(project) if project else None
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении проекта: {e}")
            return None
    
    async def delete_project(self, project_id: int) -> bool:
        """Удаление проекта"""
        if not self.pool:
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    DELETE FROM projects
                    WHERE id = $1
                ''', project_id)
                
                logger.info(f"🗑 Проект удален: ID={project_id}")
                return True
                
        except Exception as e:
            logger.error(f"❌ Ошибка при удалении проекта: {e}")
            return False
    
    async def update_project_name(self, project_id: int, new_name: str) -> bool:
        """Обновление названия проекта"""
        if not self.pool:
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    UPDATE projects
                    SET name = $1
                    WHERE id = $2
                ''', new_name[:255], project_id)
                
                return True
                
        except Exception as e:
            logger.error(f"❌ Ошибка при обновлении проекта: {e}")
            return False
    
    # ========== МЕТОДЫ ДЛЯ РАБОТЫ С ЗАДАЧАМИ ==========
    
    async def add_task(self, project_id: int, title: str, deadline: Optional[date] = None) -> bool:
        """Добавление новой задачи"""
        if not self.pool:
            logger.warning("Нет подключения к БД, задача не сохранена")
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    INSERT INTO tasks (project_id, title, deadline)
                    VALUES ($1, $2, $3)
                ''', project_id, title[:500], deadline)
                
                logger.info(f"✅ Задача добавлена: проект={project_id}, заголовок={title[:50]}")
                return True
                
        except Exception as e:
            logger.error(f"❌ Ошибка при добавлении задачи: {e}")
            return False
    
    async def get_project_tasks(self, project_id: int, show_completed: bool = False) -> List[Dict[str, Any]]:
        """Получение задач проекта"""
        if not self.pool:
            return []
        
        try:
            async with self.pool.acquire() as conn:
                if show_completed:
                    # Показать все задачи
                    tasks = await conn.fetch('''
                        SELECT 
                            id, title, description, deadline, status,
                            created_at
                        FROM tasks
                        WHERE project_id = $1
                        ORDER BY 
                            CASE 
                                WHEN deadline IS NULL THEN 1
                                ELSE 0
                            END,
                            deadline ASC,
                            created_at DESC
                    ''', project_id)
                else:
                    # Показать только активные задачи
                    tasks = await conn.fetch('''
                        SELECT 
                            id, title, description, deadline, status,
                            created_at
                        FROM tasks
                        WHERE project_id = $1 AND status = 'active'
                        ORDER BY 
                            CASE 
                                WHEN deadline IS NULL THEN 1
                                ELSE 0
                            END,
                            deadline ASC,
                            created_at DESC
                    ''', project_id)
                
                return [dict(task) for task in tasks]
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении задач: {e}")
            return []
    
    async def get_task_by_id(self, task_id: int) -> Optional[Dict[str, Any]]:
        """Получение задачи по ID"""
        if not self.pool:
            return None
        
        try:
            async with self.pool.acquire() as conn:
                task = await conn.fetchrow('''
                    SELECT 
                        id, title, description, deadline, status,
                        project_id, created_at
                    FROM tasks
                    WHERE id = $1
                ''', task_id)
                
                return dict(task) if task else None
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении задачи: {e}")
            return None
    
    async def toggle_task_status(self, task_id: int) -> bool:
        """Переключение статуса задачи"""
        if not self.pool:
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    UPDATE tasks
                    SET status = CASE 
                        WHEN status = 'active' THEN 'completed'
                        ELSE 'active'
                    END,
                    updated_at = CURRENT_TIMESTAMP
                    WHERE id = $1
                ''', task_id)
                
                return True
                
        except Exception as e:
            logger.error(f"❌ Ошибка при обновлении статуса задачи: {e}")
            return False
    
    async def delete_task(self, task_id: int) -> bool:
        """Удаление задачи"""
        if not self.pool:
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    DELETE FROM tasks
                    WHERE id = $1
                ''', task_id)
                
                return True
                
        except Exception as e:
            logger.error(f"❌ Ошибка при удалении задачи: {e}")
            return False
    
    async def update_task_deadline(self, task_id: int, new_deadline: Optional[date]) -> bool:
        """Обновление дедлайна задачи"""
        if not self.pool:
            return False
        
        try:
            async with self.pool.acquire() as conn:
                await conn.execute('''
                    UPDATE tasks
                    SET deadline = $1,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $2
                ''', new_deadline, task_id)
                
                return True
                
        except Exception as e:
            logger.error(f"❌ Ошибка при обновлении дедлайна: {e}")
            return False
    
    async def get_upcoming_tasks(self, user_id: int, days_ahead: int = 7) -> List[Dict[str, Any]]:
        """Получение предстоящих задач"""
        if not self.pool:
            return []
        
        try:
            async with self.pool.acquire() as conn:
                tasks = await conn.fetch('''
                    SELECT 
                        t.id, t.title, t.deadline, t.status,
                        p.name as project_name,
                        p.id as project_id
                    FROM tasks t
                    JOIN projects p ON t.project_id = p.id
                    WHERE p.user_id = $1
                      AND t.status = 'active'
                      AND t.deadline IS NOT NULL
                      AND t.deadline <= CURRENT_DATE + INTERVAL '1 day' * $2
                    ORDER BY t.deadline ASC
                    LIMIT 20
                ''', user_id, days_ahead)
                
                return [dict(task) for task in tasks]
                
        except Exception as e:
            logger.error(f"❌ Ошибка при получении предстоящих задач: {e}")
            return []

# Глобальный объект базы данных
db = Database()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура главного меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Мои проекты")],
            [KeyboardButton(text="➕ Новый проект"), KeyboardButton(text="📅 Ближайшие задачи")],
            [KeyboardButton(text="ℹ️ Помощь"), KeyboardButton(text="🔄 Перезапустить")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Выберите действие..."
    )

def get_cancel_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура для отмены действий"""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

def format_date(d: Optional[date]) -> str:
    """Форматирование даты в читаемый вид"""
    if not d:
        return "⏳ Без срока"
    
    today = date.today()
    if d == today:
        return "⏰ Сегодня"
    elif d == date.today().replace(day=date.today().day + 1):
        return "📅 Завтра"
    
    # Форматируем дату
    return d.strftime("%d.%m.%Y")

def parse_date(date_str: str) -> Optional[date]:
    """Парсинг даты из строки"""
    date_str = date_str.strip().lower()
    
    # Специальные ключевые слова
    if date_str in ['нет', 'no', 'без срока', 'пропустить', 'skip', 'null', 'none']:
        return None
    
    # Попробуем разные форматы
    date_formats = [
        "%d.%m.%Y", "%d.%m.%y",  # 15.02.2024, 15.02.24
        "%d/%m/%Y", "%d/%m/%y",  # 15/02/2024, 15/02/24
        "%d-%m-%Y", "%d-%m-%y",  # 15-02-2024, 15-02-24
        "%Y.%m.%d", "%Y/%m/%d", "%Y-%m-%d",  # 2024.02.15, 2024/02/15, 2024-02-15
    ]
    
    for date_format in date_formats:
        try:
            return datetime.strptime(date_str, date_format).date()
        except ValueError:
            continue
    
    return None

def format_project_stats(project: Dict[str, Any]) -> str:
    """Форматирование статистики проекта"""
    total = project.get('total_tasks', 0) or 0
    active = project.get('active_tasks', 0) or 0
    completed = total - active
    
    return (
        f"📊 Задачи: {total} всего\n"
        f"   • 🟢 Активных: {active}\n"
        f"   • ✅ Выполнено: {completed}"
    )

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 <b>Добро пожаловать в Task Planner Bot!</b>\n\n"
        "Я помогу вам организовать ваши проекты и задачи.\n\n"
        "<b>Основные возможности:</b>\n"
        "• 📂 Создание и управление проектами\n"
        "• 📝 Добавление задач с дедлайнами\n"
        "• ✅ Отслеживание выполнения задач\n"
        "• 📅 Просмотр предстоящих задач\n\n"
        "Используйте кнопки ниже для навигации."
    )
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard())

@router.message(Command("help"))
@router.message(F.text == "ℹ️ Помощь")
async def cmd_help(message: Message):
    """Обработчик команды помощи"""
    help_text = (
        "🤖 <b>Task Planner Bot - Помощь</b>\n\n"
        
        "<b>Основные команды:</b>\n"
        "/start - Начать работу с ботом\n"
        "/help - Показать это сообщение\n"
        "/projects - Показать все проекты\n"
        "/tasks - Показать ближайшие задачи\n\n"
        
        "<b>Управление проектами:</b>\n"
        "• <b>➕ Новый проект</b> - создать проект\n"
        "• <b>📂 Мои проекты</b> - список проектов\n"
        "• В проекте можно добавлять, удалять и отмечать задачи\n\n"
        
        "<b>Управление задачами:</b>\n"
        "• 📝 У каждой задачи есть название и дедлайн\n"
        "• ✅ Отмечайте выполненные задачи\n"
        "• 📅 Просматривайте предстоящие задачи\n\n"
        
        "<b>Формат даты:</b>\n"
        "При создании задачи укажите дату в формате:\n"
        "<code>ДД.ММ.ГГГГ</code> (например, 15.02.2024)\n"
        "Или отправьте 'нет' для задачи без дедлайна\n\n"
        
        "Если возникли проблемы, перезапустите бота командой /start"
    )
    
    await message.answer(help_text, reply_markup=get_main_keyboard())

@router.message(F.text == "🔄 Перезапустить")
async def cmd_restart(message: Message):
    """Перезапуск бота"""
    await cmd_start(message)

@router.message(Command("projects"))
@router.message(F.text == "📂 Мои проекты")
async def show_projects(message: Message):
    """Показать все проекты пользователя"""
    try:
        projects = await db.get_user_projects(message.from_user.id)
        
        if not projects:
            await message.answer(
                "📭 <b>У вас пока нет проектов.</b>\n\n"
                "Создайте первый проект, нажав кнопку <b>➕ Новый проект</b>",
                reply_markup=get_main_keyboard()
            )
            return
        
        # Создаем inline-клавиатуру с проектами
        keyboard_buttons = []
        
        for project in projects:
            project_name = html.quote(project['name'][:30])
            if len(project['name']) > 30:
                project_name += "..."
            
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📁 {project_name}",
                    callback_data=f"project_{project['id']}"
                )
            ])
        
        # Добавляем кнопку для обновления списка
        keyboard_buttons.append([
            InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_projects")
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        # Формируем текст с проектами
        projects_text = f"📂 <b>Ваши проекты</b> (всего: {len(projects)}):\n\n"
        
        for i, project in enumerate(projects, 1):
            created_date = project['created_at'].strftime("%d.%m.%Y") if project['created_at'] else "неизвестно"
            project_name = html.quote(project['name'])
            
            projects_text += (
                f"{i}. <b>{project_name}</b>\n"
                f"   📅 Создан: {created_date}\n"
                f"   {format_project_stats(project)}\n\n"
            )
        
        await message.answer(projects_text, reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при показе проектов: {e}")
        await message.answer(
            "❌ <b>Произошла ошибка при загрузке проектов.</b>\n"
            "Попробуйте позже или перезапустите бота.",
            reply_markup=get_main_keyboard()
        )

@router.message(F.text == "➕ Новый проект")
async def add_project_start(message: Message, state: FSMContext):
    """Начало создания нового проекта"""
    await state.set_state(ProjectStates.waiting_for_project_name)
    
    await message.answer(
        "📝 <b>Создание нового проекта</b>\n\n"
        "Введите название проекта:\n\n"
        "<i>Примеры:</i>\n"
        "<code>Разработка веб-сайта</code>\n"
        "<code>Личные цели на год</code>\n"
        "<code>Рабочие задачи</code>",
        reply_markup=get_cancel_keyboard(),
        parse_mode=ParseMode.HTML
    )

@router.message(ProjectStates.waiting_for_project_name)
async def add_project_finish(message: Message, state: FSMContext):
    """Завершение создания проекта"""
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "❌ Создание проекта отменено.",
            reply_markup=get_main_keyboard()
        )
        return
    
    project_name = message.text.strip()
    
    # Валидация названия проекта
    if not project_name:
        await message.answer(
            "❌ <b>Название проекта не может быть пустым.</b>\n"
            "Пожалуйста, введите название проекта:"
        )
        return
    
    if len(project_name) > 255:
        await message.answer(
            "❌ <b>Название слишком длинное.</b> (макс. 255 символов)\n"
            "Пожалуйста, введите более короткое название:"
        )
        return
    
    try:
        project_id = await db.add_project(message.from_user.id, project_name)
        
        if project_id:
            await message.answer(
                f"✅ <b>Проект успешно создан!</b>\n\n"
                f"📁 <b>Название:</b> <code>{html.quote(project_name)}</code>\n"
                f"🆔 <b>ID проекта:</b> <code>{project_id}</code>\n\n"
                f"Теперь вы можете добавить задачи в этот проект. "
                f"Нажмите <b>📂 Мои проекты</b>, чтобы увидеть его в списке.",
                reply_markup=get_main_keyboard()
            )
            
            # Логируем создание проекта
            logger.info(f"Пользователь {message.from_user.id} создал проект: {project_name}")
        else:
            await message.answer(
                "❌ <b>Не удалось создать проект.</b>\n"
                "Проверьте подключение к базе данных и попробуйте снова.",
                reply_markup=get_main_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Ошибка при создании проекта: {e}")
        await message.answer(
            "❌ <b>Произошла ошибка при создании проекта.</b>\n"
            "Попробуйте позже.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.message(Command("tasks"))
@router.message(F.text == "📅 Ближайшие задачи")
async def show_upcoming_tasks(message: Message):
    """Показать ближайшие задачи"""
    try:
        tasks = await db.get_upcoming_tasks(message.from_user.id, days_ahead=14)
        
        if not tasks:
            await message.answer(
                "📭 <b>У вас нет предстоящих задач на ближайшие 2 недели.</b>\n\n"
                "Создайте новые задачи в своих проектах.",
                reply_markup=get_main_keyboard()
            )
            return
        
        tasks_text = "📅 <b>Ближайшие задачи (14 дней):</b>\n\n"
        
        current_date = None
        for task in tasks:
            task_date = task['deadline']
            
            # Показываем дату, если она изменилась
            if task_date != current_date:
                current_date = task_date
                date_str = format_date(task_date)
                tasks_text += f"\n<b>{date_str}:</b>\n"
            
            project_name = html.quote(task['project_name'][:20])
            if len(task['project_name']) > 20:
                project_name += "..."
            
            tasks_text += (
                f"  • {html.quote(task['title'])}\n"
                f"    📁 Проект: <i>{project_name}</i>\n"
            )
        
        await message.answer(tasks_text, reply_markup=get_main_keyboard())
        
    except Exception as e:
        logger.error(f"Ошибка при показе предстоящих задач: {e}")
        await message.answer(
            "❌ <b>Не удалось загрузить предстоящие задачи.</b>",
            reply_markup=get_main_keyboard()
        )

# ==================== CALLBACK ОБРАБОТЧИКИ ====================

@router.callback_query(F.data == "refresh_projects")
async def refresh_projects(callback: CallbackQuery):
    """Обновление списка проектов"""
    await callback.answer("🔄 Обновляем список...")
    await show_projects(callback.message)

@router.callback_query(F.data.startswith("project_"))
async def project_menu(callback: CallbackQuery):
    """Меню проекта"""
    try:
        project_id = int(callback.data.split("_")[1])
        project = await db.get_project_by_id(project_id)
        
        if not project:
            await callback.message.edit_text("❌ Проект не найден.")
            await callback.answer()
            return
        
        # Проверяем права доступа
        if project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ У вас нет доступа к этому проекту.")
            await callback.answer()
            return
        
        # Получаем статистику по задачам
        all_tasks = await db.get_project_tasks(project_id, show_completed=True)
        active_tasks = await db.get_project_tasks(project_id, show_completed=False)
        
        # Формируем текст
        project_text = (
            f"📁 <b>Проект: {html.quote(project['name'])}</b>\n\n"
            f"📊 <b>Статистика:</b>\n"
            f"• Всего задач: {len(all_tasks)}\n"
            f"• Активных: {len(active_tasks)}\n"
            f"• Выполнено: {len(all_tasks) - len(active_tasks)}\n\n"
        )
        
        # Добавляем информацию о ближайших задачах
        if active_tasks:
            upcoming = [t for t in active_tasks if t['deadline']]
            if upcoming:
                upcoming.sort(key=lambda x: x['deadline'] or date.max)
                project_text += "📅 <b>Ближайшие задачи:</b>\n"
                for i, task in enumerate(upcoming[:3], 1):
                    deadline_str = format_date(task['deadline'])
                    project_text += f"{i}. {html.quote(task['title'][:30])} - {deadline_str}\n"
                project_text += "\n"
        
        project_text += "Выберите действие:"
        
        # Создаем клавиатуру
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Задачи", callback_data=f"tasks_{project_id}"),
                InlineKeyboardButton(text="➕ Задача", callback_data=f"add_task_{project_id}")
            ],
            [
                InlineKeyboardButton(text="✅ Выполненные", callback_data=f"completed_{project_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{project_id}")
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад к проектам", callback_data="refresh_projects")
            ]
        ])
        
        await callback.message.edit_text(project_text, reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка в меню проекта: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("tasks_"))
async def show_tasks(callback: CallbackQuery):
    """Показать задачи проекта"""
    try:
        project_id = int(callback.data.split("_")[1])
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Доступ запрещен.")
            await callback.answer()
            return
        
        # Получаем активные задачи
        tasks = await db.get_project_tasks(project_id, show_completed=False)
        
        if not tasks:
            tasks_text = (
                f"📭 <b>В проекте '{html.quote(project['name'])}' нет активных задач.</b>\n\n"
                "Добавьте первую задачу или просмотрите выполненные задачи."
            )
        else:
            tasks_text = f"📋 <b>Активные задачи проекта '{html.quote(project['name'])}':</b>\n\n"
            
            for i, task in enumerate(tasks, 1):
                status_icon = "⬜"
                deadline_str = format_date(task['deadline'])
                
                tasks_text += (
                    f"{i}. {status_icon} <b>{html.quote(task['title'])}</b>\n"
                    f"   📅 {deadline_str}\n\n"
                )
        
        # Создаем клавиатуру
        keyboard_buttons = []
        
        # Кнопки для переключения статуса задач
        for task in tasks[:10]:  # Ограничиваем 10 задачами
            task_title = html.quote(task['title'][:15])
            if len(task['title']) > 15:
                task_title += "..."
            
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"✅ {task_title}",
                    callback_data=f"toggle_task_{task['id']}_{project_id}"
                )
            ])
        
        # Общие кнопки
        if tasks:
            keyboard_buttons.append([
                InlineKeyboardButton(text="✅ Показать выполненные", callback_data=f"completed_{project_id}")
            ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task_{project_id}")
        ])
        
        keyboard_buttons.append([
            InlineKeyboardButton(text="⬅️ Назад к проекту", callback_data=f"project_{project_id}")
        ])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(tasks_text, reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при показе задач: {e}")
        await callback.message.edit_text("❌ Произошла ошибка при загрузке задач.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("add_task_"))
async def add_task_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления задачи"""
    try:
        project_id = int(callback.data.split("_")[2])
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        await state.set_state(ProjectStates.waiting_for_task_title)
        await state.update_data(
            project_id=project_id,
            project_name=project['name'],
            message_id=callback.message.message_id,
            chat_id=callback.message.chat.id
        )
        
        await callback.message.answer(
            f"📝 <b>Добавление задачи в проект '{html.quote(project['name'])}'</b>\n\n"
            "Введите название задачи:\n\n"
            "<i>Примеры:</i>\n"
            "<code>Изучить документацию</code>\n"
            "<code>Написать код модуля</code>\n"
            "<code>Подготовить отчет</code>",
            reply_markup=get_cancel_keyboard()
        )
        
    except Exception as e:
        logger.error(f"Ошибка при начале добавления задачи: {e}")
        await callback.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.message(ProjectStates.waiting_for_task_title)
async def add_task_title(message: Message, state: FSMContext):
    """Получение названия задачи"""
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "❌ Добавление задачи отменено.",
            reply_markup=get_main_keyboard()
        )
        return
    
    title = message.text.strip()
    
    if not title:
        await message.answer(
            "❌ <b>Название задачи не может быть пустым.</b>\n"
            "Пожалуйста, введите название задачи:"
        )
        return
    
    if len(title) > 500:
        await message.answer(
            "❌ <b>Название слишком длинное.</b> (макс. 500 символов)\n"
            "Пожалуйста, введите более короткое название:"
        )
        return
    
    await state.update_data(title=title)
    await state.set_state(ProjectStates.waiting_for_task_deadline)
    
    await message.answer(
        "📅 <b>Установите дедлайн для задачи:</b>\n\n"
        "Введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
        "<i>Например:</i> <code>15.02.2024</code>\n\n"
        "Или отправьте <b>нет</b>, если дедлайн не нужен.\n\n"
        "<i>Другие форматы дат:</i>\n"
        "<code>15/02/2024</code> или <code>15-02-2024</code>",
        reply_markup=get_cancel_keyboard()
    )

@router.message(ProjectStates.waiting_for_task_deadline)
async def add_task_deadline(message: Message, state: FSMContext):
    """Получение дедлайна и сохранение задачи"""
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "❌ Добавление задачи отменено.",
            reply_markup=get_main_keyboard()
        )
        return
    
    deadline_str = message.text.strip()
    deadline = parse_date(deadline_str)
    
    if deadline_str not in ['нет', 'no', 'без срока', 'пропустить', 'skip'] and not deadline:
        await message.answer(
            "❌ <b>Неверный формат даты.</b>\n\n"
            "Пожалуйста, введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
            "Или отправьте <b>нет</b>, если дедлайн не нужен."
        )
        return
    
    data = await state.get_data()
    project_id = data['project_id']
    title = data['title']
    project_name = data.get('project_name', 'проект')
    
    try:
        success = await db.add_task(project_id, title, deadline)
        
        if success:
            deadline_text = format_date(deadline)
            
            await message.answer(
                f"✅ <b>Задача успешно добавлена!</b>\n\n"
                f"📝 <b>Название:</b> <code>{html.quote(title)}</code>\n"
                f"📁 <b>Проект:</b> <code>{html.quote(project_name)}</code>\n"
                f"📅 <b>Дедлайн:</b> <code>{deadline_text}</code>\n\n"
                f"Теперь вы можете управлять задачами в проекте.",
                reply_markup=get_main_keyboard()
            )
            
            # Логируем добавление задачи
            logger.info(f"Пользователь {message.from_user.id} добавил задачу: {title[:50]}")
        else:
            await message.answer(
                "❌ <b>Не удалось добавить задачу.</b>\n"
                "Проверьте подключение к базе данных и попробуйте снова.",
                reply_markup=get_main_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Ошибка при добавлении задачи: {e}")
        await message.answer(
            "❌ <b>Произошла ошибка при добавлении задачи.</b>\n"
            "Попробуйте позже.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

@router.callback_query(F.data.startswith("toggle_task_"))
async def toggle_task_status_handler(callback: CallbackQuery):
    """Переключение статуса задачи"""
    try:
        parts = callback.data.split("_")
        task_id = int(parts[2])
        project_id = int(parts[3])
        
        task = await db.get_task_by_id(task_id)
        
        if not task:
            await callback.answer("❌ Задача не найдена.")
            return
        
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        success = await db.toggle_task_status(task_id)
        
        if success:
            new_status = "✅ выполнена" if task['status'] == 'active' else "🔄 активна"
            await callback.answer(f"Задача отмечена как {new_status}!")
            
            # Обновляем список задач
            await show_tasks(callback)
        else:
            await callback.answer("❌ Не удалось обновить задачу.")
    
    except Exception as e:
        logger.error(f"Ошибка при переключении статуса задачи: {e}")
        await callback.answer("❌ Произошла ошибка.")

@router.callback_query(F.data.startswith("completed_"))
async def show_completed_tasks(callback: CallbackQuery):
    """Показать выполненные задачи"""
    try:
        project_id = int(callback.data.split("_")[1])
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Доступ запрещен.")
            await callback.answer()
            return
        
        # Получаем все задачи и фильтруем выполненные
        all_tasks = await db.get_project_tasks(project_id, show_completed=True)
        completed_tasks = [t for t in all_tasks if t['status'] == 'completed']
        
        if not completed_tasks:
            tasks_text = f"✅ <b>В проекте '{html.quote(project['name'])}' нет выполненных задач.</b>"
        else:
            tasks_text = f"✅ <b>Выполненные задачи проекта '{html.quote(project['name'])}':</b>\n\n"
            
            for i, task in enumerate(completed_tasks, 1):
                completed_date = task['created_at'].strftime("%d.%m.%Y") if task['created_at'] else "неизвестно"
                deadline_str = format_date(task['deadline'])
                
                tasks_text += (
                    f"{i}. ✅ <b>{html.quote(task['title'])}</b>\n"
                    f"   📅 Дедлайн был: {deadline_str}\n"
                    f"   📝 Создана: {completed_date}\n\n"
                )
        
        # Создаем клавиатуру
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Активные задачи", callback_data=f"tasks_{project_id}"),
                InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task_{project_id}")
            ],
            [
                InlineKeyboardButton(text="⬅️ Назад к проекту", callback_data=f"project_{project_id}")
            ]
        ])
        
        await callback.message.edit_text(tasks_text, reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Ошибка при показе выполненных задач: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("delete_"))
async def delete_project_handler(callback: CallbackQuery):
    """Удаление проекта"""
    try:
        project_id = int(callback.data.split("_")[1])
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.answer("❌ Доступ запрещен.")
            return
        
        # Подтверждение удаления
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_{project_id}"),
                InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"project_{project_id}")
            ]
        ])
        
        await callback.message.edit_text(
            f"🗑 <b>Удаление проекта</b>\n\n"
            f"Вы уверены, что хотите удалить проект?\n"
            f"<code>{html.quote(project['name'])}</code>\n\n"
            f"⚠️ <b>Внимание!</b>\n"
            f"Все задачи в проекте также будут удалены!\n"
            f"Это действие нельзя отменить!",
            reply_markup=keyboard
        )
    
    except Exception as e:
        logger.error(f"Ошибка при начале удаления проекта: {e}")
        await callback.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_project(callback: CallbackQuery):
    """Подтверждение удаления проекта"""
    try:
        project_id = int(callback.data.split("_")[2])
        project = await db.get_project_by_id(project_id)
        
        if not project:
            await callback.answer("❌ Проект не найден.")
            return
        
        success = await db.delete_project(project_id)
        
        if success:
            await callback.message.edit_text(
                f"✅ Проект <code>{html.quote(project['name'])}</code> успешно удален!"
            )
            
            # Показываем обновленный список проектов
            await asyncio.sleep(1)
            await show_projects(callback.message)
            
        else:
            await callback.message.edit_text("❌ Не удалось удалить проект.")
    
    except Exception as e:
        logger.error(f"Ошибка при удалении проекта: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

@router.message()
async def handle_other_messages(message: Message):
    """Обработка всех остальных сообщений"""
    if message.text:
        await message.answer(
            "🤖 <b>Используйте кнопки ниже для навигации:</b>",
            reply_markup=get_main_keyboard()
        )

# ==================== WEBHOOK И СЕРВЕР ====================

async def health_check(request):
    """Endpoint для проверки работоспособности"""
    try:
        # Проверяем подключение к базе данных
        db_healthy = await db.health_check() if DATABASE_URL else True
        
        if db_healthy:
            return web.Response(
                text="✅ OK - Bot is running\n"
                     f"Database: {'Connected' if DATABASE_URL else 'Not configured'}\n"
                     f"Webhook: {WEBHOOK_URL}\n"
                     f"Uptime: {time.time() - start_time:.0f} seconds",
                status=200
            )
        else:
            return web.Response(
                text="⚠️ WARNING - Database connection failed",
                status=503
            )
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return web.Response(
            text=f"❌ ERROR - {str(e)}",
            status=500
        )

async def keep_alive_endpoint(request):
    """Endpoint для поддержания приложения активным"""
    try:
        # Простая проверка, возвращаем OK
        return web.Response(
            text="✅ Keep-alive endpoint is working\n"
                 f"Timestamp: {datetime.now().isoformat()}\n"
                 f"Bot is alive and responding",
            status=200
        )
    except Exception as e:
        return web.Response(
            text=f"❌ Error: {str(e)}",
            status=500
        )

async def handle_webhook_test(request):
    """Тестовый endpoint для вебхука"""
    return web.Response(
        text="✅ Webhook endpoint is working\n"
             "This endpoint receives Telegram updates",
        status=200
    )

async def on_startup():
    """Действия при запуске приложения"""
    logger.info("🚀 Starting Task Planner Bot...")
    
    # Подключение к базе данных
    if DATABASE_URL:
        await db.connect()
    else:
        logger.warning("⚠️ DATABASE_URL не установлен, некоторые функции будут недоступны")
    
    # Установка вебхука
    try:
        webhook_info = await bot.get_webhook_info()
        logger.info(f"Current webhook info: {webhook_info.url}")
        
        if webhook_info.url != WEBHOOK_URL:
            await bot.set_webhook(
                url=WEBHOOK_URL,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types(),
                secret_token=WEBHOOK_SECRET
            )
            logger.info(f"✅ Webhook set to: {WEBHOOK_URL}")
        else:
            logger.info("✅ Webhook already set correctly")
            
    except Exception as e:
        logger.error(f"❌ Error setting webhook: {e}")
        raise
    
    logger.info("✅ Bot startup completed successfully")
    logger.info("📞 Webhook URL: " + WEBHOOK_URL)
    logger.info("🌐 Health check: https://" + RENDER_EXTERNAL_HOSTNAME + "/health")

async def on_shutdown():
    """Действия при остановке приложения"""
    logger.info("🛑 Shutting down...")
    
    # Удаление вебхука
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        logger.info("✅ Webhook deleted")
    except Exception as e:
        logger.error(f"❌ Error deleting webhook: {e}")
    
    # Закрытие соединения с базой данных
    await db.close()
    
    logger.info("✅ Bot shutdown completed")

def main():
    """Основная функция запуска приложения"""
    # Создаем aiohttp приложение
    app = web.Application()
    
    # Health check endpoints
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    app.router.add_get("/keep-alive", keep_alive_endpoint)
    app.router.add_get("/webhook", handle_webhook_test)
    
    # Создаем обработчик вебхуков
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET
    )
    
    # Регистрируем вебхук
    webhook_handler.register(app, path="/webhook")
    
    # Запускаем приложение
    logger.info(f"🌐 Starting web server on port {PORT}")
    logger.info(f"📞 Webhook URL: {WEBHOOK_URL}")
    logger.info(f"🔑 Webhook secret: {'Set' if WEBHOOK_SECRET else 'Not set'}")
    
    # Запускаем startup-функции
    asyncio.run(on_startup())
    
    try:
        web.run_app(
            app,
            host="0.0.0.0",
            port=PORT,
            access_log=logger,
            print=None  # Отключаем стандартное логирование aiohttp
        )
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        logger.error(f"❌ Failed to start server: {e}")
        raise
    finally:
        # Запускаем shutdown-функции
        asyncio.run(on_shutdown())

if __name__ == "__main__":
    # Глобальная переменная времени запуска
    start_time = time.time()
    
    # Запускаем основное приложение
    main()
