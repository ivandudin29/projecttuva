import os
import logging
import asyncio
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

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Конфигурация
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

logger.info(f"Config loaded: BOT_TOKEN present={bool(BOT_TOKEN)}")
logger.info(f"DATABASE_URL present={bool(DATABASE_URL)}")

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
                    max_size=10,
                    command_timeout=60
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
                    
                    # Таблица задач - deadline может быть NULL
                    await conn.execute('''
                        CREATE TABLE IF NOT EXISTS tasks (
                            id SERIAL PRIMARY KEY,
                            project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                            title TEXT NOT NULL,
                            description TEXT,
                            deadline DATE,
                            status TEXT DEFAULT 'active' CHECK (status IN ('active', 'completed')),
                            created_at TIMESTAMP DEFAULT NOW()
                        )
                    ''')
                    
                    # Индексы для ускорения запросов
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_projects_user_id ON projects(user_id)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)')
                    await conn.execute('CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)')
                    
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
    
    # Методы для задач
    async def add_task(self, project_id: int, title: str, deadline: Optional[date] = None) -> bool:
        """Добавление новой задачи"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    '''INSERT INTO tasks (project_id, title, deadline) 
                       VALUES ($1, $2, $3)''',
                    project_id, title, deadline
                )
                logger.info(f"Task added: project={project_id}, title={title}, deadline={deadline}")
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
                    # Показать все задачи
                    tasks = await conn.fetch(
                        '''SELECT id, title, deadline, status 
                           FROM tasks 
                           WHERE project_id = $1 
                           ORDER BY 
                             CASE WHEN deadline IS NULL THEN 1 ELSE 0 END,
                             deadline,
                             created_at''',
                        project_id
                    )
                else:
                    # Показать только активные задачи
                    tasks = await conn.fetch(
                        '''SELECT id, title, deadline, status 
                           FROM tasks 
                           WHERE project_id = $1 AND status = 'active'
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
                    'SELECT id, title, deadline, status, project_id FROM tasks WHERE id = $1',
                    task_id
                )
                return task
        except Exception as e:
            logger.error(f"Error getting task: {e}")
            return None
    
    async def update_task_status(self, task_id: int, status: str) -> bool:
        """Обновление статуса задачи"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    'UPDATE tasks SET status = $1 WHERE id = $2',
                    status, task_id
                )
                return True
        except Exception as e:
            logger.error(f"Error updating task status: {e}")
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
    
    async def toggle_task_status(self, task_id: int) -> bool:
        """Переключение статуса задачи"""
        if not self.pool:
            return False
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    '''UPDATE tasks 
                       SET status = CASE 
                         WHEN status = 'active' THEN 'completed'
                         ELSE 'active'
                       END
                       WHERE id = $1''',
                    task_id
                )
                return True
        except Exception as e:
            logger.error(f"Error toggling task: {e}")
            return False

# Глобальный объект БД
db = Database()

# Вспомогательные функции
def get_main_keyboard() -> ReplyKeyboardMarkup:
    """Клавиатура главного меню"""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📂 Мои проекты"), KeyboardButton(text="➕ Новый проект")]
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

# Обработчики команд
@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    welcome_text = (
        "👋 <b>Добро пожаловать в Task Planner Bot!</b>\n\n"
        "Я помогу вам организовать ваши проекты и задачи.\n"
        "Используйте кнопки ниже для навигации."
    )
    
    await message.answer(welcome_text, reply_markup=get_main_keyboard(), parse_mode="HTML")

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
        else:
            await message.answer(
                "❌ Не удалось создать проект.",
                reply_markup=get_main_keyboard()
            )
    
    except Exception as e:
        logger.error(f"Error creating project: {e}")
        await message.answer(
            "❌ Произошла ошибка при создании проекта.",
            reply_markup=get_main_keyboard()
        )
    
    await state.clear()

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
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await message.answer(
            f"📂 <b>Ваши проекты</b> (всего: {len(projects)}):",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error showing projects: {e}")
        await message.answer(
            "❌ Произошла ошибка при загрузке проектов.",
            reply_markup=get_main_keyboard()
        )

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
        
        # Получаем задачи для статистики
        tasks = await db.get_project_tasks(project_id, show_completed=True)
        active_tasks = [t for t in tasks if t['status'] == 'active']
        
        # Создаем клавиатуру для управления проектом
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Задачи", callback_data=f"tasks_{project_id}"),
                InlineKeyboardButton(text="➕ Задача", callback_data=f"add_task_{project_id}")
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_{project_id}")
            ]
        ])
        
        await callback.message.edit_text(
            f"📁 <b>Проект: {project['name']}</b>\n\n"
            f"📊 Статистика:\n"
            f"• Всего задач: {len(tasks)}\n"
            f"• Активных: {len(active_tasks)}\n"
            f"• Выполнено: {len(tasks) - len(active_tasks)}\n\n"
            f"Выберите действие:",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Error in project menu: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

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
        
        # Получаем активные задачи
        tasks = await db.get_project_tasks(project_id, show_completed=False)
        
        if not tasks:
            tasks_text = "📭 Задач пока нет. Создайте первую задачу!"
        else:
            tasks_text = f"📋 <b>Задачи проекта '{project['name']}':</b>\n\n"
            for i, task in enumerate(tasks, 1):
                status = "✅ " if task['status'] == 'completed' else "⬜ "
                deadline = format_date(task['deadline'])
                tasks_text += f"{i}. {status}<b>{task['title']}</b>\n"
                tasks_text += f"   📅 {deadline}\n\n"
        
        # Создаем клавиатуру для управления задачами
        keyboard_buttons = []
        
        # Кнопки для каждой задачи
        for task in tasks:
            task_status = "✅" if task['status'] == 'completed' else "⬜"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"{task_status} {task['title'][:20]}",
                    callback_data=f"task_toggle_{task['id']}"
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
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"project_{project_id}")
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
        "📅 <b>Установите дедлайн для задачи (необязательно):</b>\n\n"
        "Введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
        "Например: <code>15.02.2024</code>\n\n"
        "Или отправьте 'нет', если дедлайн не нужен.",
        parse_mode="HTML"
    )

@router.message(ProjectStates.waiting_for_task_deadline)
async def add_task_deadline(message: Message, state: FSMContext):
    """Получение дедлайна и сохранение задачи"""
    deadline_str = message.text.strip().lower()
    deadline = None
    
    if deadline_str not in ['нет', 'no', 'без срока', 'пропустить', 'skip']:
        deadline = parse_date(deadline_str)
        
        if not deadline:
            await message.answer(
                "❌ Неверный формат даты. Пожалуйста, введите дату в формате <code>ДД.ММ.ГГГГ</code>\n"
                "Или отправьте 'нет', если дедлайн не нужен.",
                parse_mode="HTML"
            )
            return
    
    data = await state.get_data()
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
                f"📅 Дедлайн: <code>{deadline_text}</code>",
                reply_markup=get_main_keyboard(),
                parse_mode="HTML"
            )
        else:
            await message.answer(
                "❌ Не удалось добавить задачу.",
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
async def toggle_task_status(callback: CallbackQuery):
    """Переключение статуса задачи"""
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
        
        success = await db.toggle_task_status(task_id)
        
        if success:
            new_status = "выполнена" if task['status'] == 'active' else "не выполнена"
            await callback.answer(f"✅ Задача отмечена как {new_status}!")
            
            # Обновляем список задач
            project_id = task['project_id']
            await show_tasks(callback)
        else:
            await callback.answer("❌ Не удалось обновить задачу.")
    
    except Exception as e:
        logger.error(f"Error toggling task: {e}")
        await callback.answer("❌ Произошла ошибка.")

@router.callback_query(F.data.startswith("delete_"))
async def delete_project_handler(callback: CallbackQuery):
    """Удаление проекта"""
    project_id = int(callback.data.split("_")[1])
    
    try:
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
            f"<code>{project['name']}</code>\n\n"
            f"⚠️ Все задачи в проекте будут удалены!\n"
            f"Это действие нельзя отменить!",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
    
    except Exception as e:
        logger.error(f"Error starting project deletion: {e}")
        await callback.answer("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("confirm_delete_"))
async def confirm_delete_project(callback: CallbackQuery):
    """Подтверждение удаления проекта"""
    project_id = int(callback.data.split("_")[2])
    
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
            
            # Показываем обновленный список проектов
            projects = await db.get_user_projects(callback.from_user.id)
            
            if not projects:
                await callback.message.answer(
                    "📭 У вас пока нет проектов. Создайте первый проект!",
                    reply_markup=get_main_keyboard()
                )
                await callback.answer()
                return
            
            keyboard_buttons = []
            for project in projects:
                keyboard_buttons.append([
                    InlineKeyboardButton(
                        text=f"📁 {project['name']}",
                        callback_data=f"project_{project['id']}"
                    )
                ])
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
            
            await callback.message.answer(
                f"📂 <b>Ваши проекты</b> (всего: {len(projects)}):",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
        else:
            await callback.message.edit_text("❌ Не удалось удалить проект.")
    
    except Exception as e:
        logger.error(f"Error confirming project deletion: {e}")
        await callback.message.edit_text("❌ Произошла ошибка.")
    
    await callback.answer()

@router.callback_query(F.data.startswith("completed_"))
async def show_completed_tasks(callback: CallbackQuery):
    """Показать выполненные задачи"""
    project_id = int(callback.data.split("_")[1])
    
    try:
        project = await db.get_project_by_id(project_id)
        
        if not project or project['user_id'] != callback.from_user.id:
            await callback.message.edit_text("❌ Доступ запрещен.")
            await callback.answer()
            return
        
        # Получаем ВСЕ задачи
        all_tasks = await db.get_project_tasks(project_id, show_completed=True)
        completed_tasks = [t for t in all_tasks if t['status'] == 'completed']
        
        if not completed_tasks:
            tasks_text = "✅ Выполненных задач пока нет."
        else:
            tasks_text = f"✅ <b>Выполненные задачи проекта '{project['name']}':</b>\n\n"
            for i, task in enumerate(completed_tasks, 1):
                deadline = format_date(task['deadline'])
                tasks_text += f"{i}. ✅ <b>{task['title']}</b>\n"
                tasks_text += f"   📅 {deadline}\n\n"
        
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

@router.message()
async def handle_other_messages(message: Message):
    """Обработка всех остальных сообщений"""
    await message.answer(
        "🤖 Используйте кнопки ниже для навигации:",
        reply_markup=get_main_keyboard()
    )

async def main():
    """Основная асинхронная функция"""
    logger.info("Starting bot...")
    
    # Подключаемся к БД
    await db.connect()
    
    # Запускаем polling
    logger.info("Bot started. Polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
