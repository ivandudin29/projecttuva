import os
import logging
import sys
from datetime import datetime, timedelta
import asyncio
from typing import Optional, List

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

PORT = int(os.getenv("PORT", 10000))
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME")
if not WEBHOOK_HOST:
    logger.error("❌ RENDER_EXTERNAL_HOSTNAME не найден!")
    sys.exit(1)

WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}"

logger.info(f"🚀 Конфигурация:")
logger.info(f"• PORT: {PORT}")
logger.info(f"• WEBHOOK_HOST: {WEBHOOK_HOST}")
logger.info(f"• WEBHOOK_URL: {WEBHOOK_URL}")

# Ваш Telegram ID
TELEGRAM_USER_ID = 209010651

# Инициализация
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Глобальные переменные
db_pool = None
notification_task = None

# Статусы задач
TASK_STATUSES = {
    'pending': '⏳ В ожидании',
    'in_progress': '🔄 В работе', 
    'completed': '✅ Завершена',
    'overdue': '⚠️ Просрочена'
}

# FSM States
class ProjectState(StatesGroup):
    waiting_for_name = State()

class TaskState(StatesGroup):
    waiting_for_title = State()
    waiting_for_deadline = State()

# ========== БАЗА ДАННЫХ ==========
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
                command_timeout=60
            )
            logger.info("✅ Пул подключений создан")
        except Exception as e:
            logger.error(f"❌ Ошибка при создании пула подключений: {e}")
            raise
    return db_pool

async def migrate_web_data():
    """Миграция данных из веб-версии на ваш Telegram ID"""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # 1. Обновляем все проекты с user_id = 1 на ваш Telegram ID
            result = await conn.execute('''
                UPDATE projects 
                SET user_id = $1 
                WHERE user_id = 1 OR user_id IS NULL
            ''', TELEGRAM_USER_ID)
            
            projects_updated = int(result.split()[1]) if 'UPDATE' in result else 0
            
            # 2. Получаем количество мигрированных задач
            tasks_count = await conn.fetchval('''
                SELECT COUNT(*) 
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE p.user_id = $1
            ''', TELEGRAM_USER_ID)
            
            logger.info(f"✅ Мигрировано {projects_updated} проектов и {tasks_count} задач на ID {TELEGRAM_USER_ID}")
            return {
                'success': True,
                'projects_updated': projects_updated,
                'tasks_count': tasks_count
            }
    except Exception as e:
        logger.error(f"❌ Ошибка миграции данных: {e}")
        return {
            'success': False,
            'error': str(e)
        }

async def create_tables():
    """Создание таблиц если их не существует"""
    try:
        logger.info("🔄 Проверка таблиц...")
        pool = await get_db_pool()
        if not pool:
            return False
            
        async with pool.acquire() as conn:
            # Таблица проектов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS projects (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица задач с исправленной структурой
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title VARCHAR(255) NOT NULL,
                    description TEXT,
                    deadline DATE NOT NULL,
                    status VARCHAR(20) DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица для уведомлений
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS notifications (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
                    notification_type VARCHAR(50) NOT NULL,
                    notification_time TIMESTAMP NOT NULL,
                    is_sent BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Создаем индексы для производительности
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_tasks_project_id ON tasks(project_id)
            ''')
            
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
            ''')
            
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline)
            ''')
            
            await conn.execute('''
                CREATE INDEX IF NOT EXISTS idx_notifications_user_time 
                ON notifications(user_id, notification_time) WHERE is_sent = FALSE
            ''')
            
            logger.info("✅ Таблицы созданы/проверены")
            return True
            
    except Exception as e:
        logger.error(f"❌ Ошибка при создании таблиц: {e}")
        return False

# ========== УВЕДОМЛЕНИЯ ==========
async def create_notification(user_id: int, task_id: int, notification_type: str, days_before: int = 0):
    """Создание уведомления"""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            task = await conn.fetchrow(
                "SELECT deadline FROM tasks WHERE id = $1",
                task_id
            )
            
            if not task:
                logger.error(f"❌ Задача {task_id} не найдена для создания уведомления")
                return
            
            deadline = task['deadline']
            notification_time = datetime.combine(deadline, datetime.min.time().replace(hour=9, minute=0)) - timedelta(days=days_before)
            
            # Проверяем, не существует ли уже такое уведомление
            existing = await conn.fetchrow('''
                SELECT id FROM notifications 
                WHERE task_id = $1 AND notification_type = $2 AND is_sent = FALSE
                AND ABS(EXTRACT(EPOCH FROM (notification_time - $3))) < 60
            ''', task_id, notification_type, notification_time)
            
            if existing:
                logger.info(f"ℹ️ Уведомление уже существует для задачи {task_id} ({notification_type})")
                return
            
            await conn.execute('''
                INSERT INTO notifications (user_id, task_id, notification_type, notification_time)
                VALUES ($1, $2, $3, $4)
            ''', user_id, task_id, notification_type, notification_time)
            
            logger.info(f"📅 Уведомление создано для задачи {task_id} ({notification_type}) на {notification_time}")
            
    except Exception as e:
        logger.error(f"❌ Ошибка создания уведомления для задачи {task_id}: {e}")

async def check_overdue_tasks():
    """Проверка и обновление просроченных задач"""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            result = await conn.execute('''
                UPDATE tasks 
                SET status = 'overdue',
                    updated_at = CURRENT_TIMESTAMP
                WHERE deadline < CURRENT_DATE 
                AND status NOT IN ('completed', 'overdue')
            ''')
            
            if 'UPDATE' in result:
                count = result.split()[1]
                if int(count) > 0:
                    logger.info(f"🔄 Обновлено {count} просроченных задач")
                    
    except Exception as e:
        logger.error(f"❌ Ошибка обновления просроченных задач: {e}")

async def check_and_send_notifications():
    """Проверка и отправка уведомлений"""
    try:
        await check_overdue_tasks()
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            notifications = await conn.fetch('''
                SELECT n.*, t.title, t.deadline, p.user_id
                FROM notifications n
                JOIN tasks t ON n.task_id = t.id
                JOIN projects p ON t.project_id = p.id
                WHERE n.is_sent = FALSE 
                AND n.notification_time <= NOW()
                LIMIT 20
            ''')
            
            sent_count = 0
            for notification in notifications:
                user_id = notification['user_id']
                task_title = notification['title']
                deadline = notification['deadline'].strftime('%d.%m.%Y')
                notification_type = notification['notification_type']
                
                message_text = ""
                if notification_type == "deadline_today":
                    message_text = f"📢 **СЕГОДНЯ ДЕДЛАЙН!**\n\nЗадача: {task_title}\nДедлайн: {deadline}"
                elif notification_type == "deadline_tomorrow":
                    message_text = f"📢 **ЗАВТРА ДЕДЛАЙН!**\n\nЗадача: {task_title}\nДедлайн: {deadline}"
                elif "days_before" in notification_type:
                    days = notification_type.split("_")[2]
                    message_text = f"📢 **Напоминание**\n\nЗадача: {task_title}\nДедлайн: {deadline}\nОсталось дней: {days}"
                else:
                    message_text = f"📢 **Напоминание**\n\nЗадача: {task_title}\nДедлайн: {deadline}"
                
                if message_text:
                    try:
                        await bot.send_message(user_id, message_text, parse_mode=ParseMode.MARKDOWN)
                        await conn.execute(
                            "UPDATE notifications SET is_sent = TRUE WHERE id = $1",
                            notification['id']
                        )
                        sent_count += 1
                        logger.info(f"📨 Уведомление отправлено пользователю {user_id} для задачи '{task_title}'")
                    except Exception as e:
                        logger.error(f"❌ Ошибка отправки уведомления пользователю {user_id}: {e}")
            
            if sent_count > 0:
                logger.info(f"✅ Отправлено {sent_count} уведомлений")
                        
    except Exception as e:
        logger.error(f"❌ Ошибка проверки уведомлений: {e}")

async def notification_scheduler():
    """Планировщик уведомлений"""
    logger.info("⏰ Запуск планировщика уведомлений...")
    while True:
        try:
            await check_and_send_notifications()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info("⏰ Планировщик уведомлений остановлен")
            break
        except Exception as e:
            logger.error(f"❌ Ошибка в планировщике: {e}")
            await asyncio.sleep(60)

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    """Главная клавиатура"""
    keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Проект"), KeyboardButton(text="📂 Проекты")],
            [KeyboardButton(text="🔔 Уведомления"), KeyboardButton(text="📊 Статистика")]
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )
    return keyboard

def get_project_keyboard(project_id: int):
    """Клавиатура проекта"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Задачи", callback_data=f"tasks:{project_id}"),
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete:{project_id}")
            ]
        ]
    )
    return keyboard

def get_task_keyboard(task_id: int, current_status: str = 'pending'):
    """Клавиатура задачи с выбором статуса"""
    status_buttons = []
    
    for status_key, status_name in TASK_STATUSES.items():
        if status_key == current_status:
            status_buttons.append(
                InlineKeyboardButton(text=f"✓ {status_name}", callback_data=f"noop")
            )
        else:
            status_buttons.append(
                InlineKeyboardButton(text=status_name, callback_data=f"set_status:{task_id}:{status_key}")
            )
    
    keyboard_rows = []
    for i in range(0, len(status_buttons), 2):
        keyboard_rows.append(status_buttons[i:i+2])
    
    keyboard_rows.append([
        InlineKeyboardButton(text="🔔 Напомнить завтра", callback_data=f"remind:{task_id}:1"),
        InlineKeyboardButton(text="🔔 Напомнить сегодня", callback_data=f"remind:{task_id}:0")
    ])
    
    keyboard_rows.append([
        InlineKeyboardButton(text="↩️ Назад к задачам", callback_data=f"back_to_task_list:{task_id}")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def get_tasks_keyboard(project_id: int, show_back: bool = False):
    """Клавиатура задач проекта"""
    keyboard_rows = [
        [InlineKeyboardButton(text="➕ Добавить задачу", callback_data=f"add_task:{project_id}")],
        [InlineKeyboardButton(text="📊 Управление задачами", callback_data=f"task_statuses:{project_id}")]
    ]
    
    if show_back:
        keyboard_rows.append([InlineKeyboardButton(text="↩️ Назад к проектам", callback_data="back_to_projects")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

def get_notification_settings_keyboard():
    """Клавиатура настроек уведомлений"""
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="За 3 дня", callback_data="notif_setting:3"),
                InlineKeyboardButton(text="За 2 дня", callback_data="notif_setting:2"),
                InlineKeyboardButton(text="За 1 день", callback_data="notif_setting:1")
            ],
            [
                InlineKeyboardButton(text="В день дедлайна", callback_data="notif_setting:0"),
                InlineKeyboardButton(text="Отключить все", callback_data="notif_setting:off")
            ],
            [InlineKeyboardButton(text="📋 Мои уведомления", callback_data="list_notifications")],
            [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_main")]
        ]
    )
    return keyboard

def get_tasks_list_keyboard(tasks, project_id: int):
    """Клавиатура со списком задач"""
    keyboard_rows = []
    for task in tasks:
        deadline = task['deadline'].strftime('%d.%m.%y')
        status_icon = {
            'pending': '⏳',
            'in_progress': '🔄',
            'completed': '✅',
            'overdue': '⚠️'
        }.get(task['display_status'], '⏳')
        
        keyboard_rows.append([
            InlineKeyboardButton(
                text=f"{status_icon} {task['title']} - {deadline}",
                callback_data=f"task_detail:{task['id']}"
            )
        ])
    
    keyboard_rows.append([
        InlineKeyboardButton(text="↩️ Назад к проекту", callback_data=f"tasks:{project_id}")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)

# ========== ХЕНДЛЕРЫ ==========
@dp.message(CommandStart())
async def cmd_start(message: Message):
    """Команда /start"""
    user_id = message.from_user.id
    logger.info(f"👉 /start от {user_id}")
    
    # Автоматически мигрируем данные если нужно
    if user_id == TELEGRAM_USER_ID:
        await message.answer("🔄 Проверка данных из веб-версии...")
        result = await migrate_web_data()
        if result['success'] and result['projects_updated'] > 0:
            await message.answer(f"✅ Перенесено {result['projects_updated']} проектов и {result['tasks_count']} задач из веб-версии!")
    
    await message.answer(
        f"🎉 Добро пожаловать в Task Planner Pro!\n\n"
        f"Ваш ID: {user_id}\n"
        f"Веб-версия также использует этот ID для синхронизации\n\n"
        f"Используйте кнопки ниже:",
        reply_markup=get_main_keyboard()
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    """Команда /help"""
    help_text = """
📚 **Помощь по командам:**

**Основные команды:**
/start - Начало работы (автоматическая миграция)
/ping - Проверка связи
/id - Ваш ID
/status - Статус бота
/help - Эта справка
/migrate - Принудительная миграция данных

**Функционал:**
• Создание проектов и задач
• Управление статусами задач
• Уведомления о дедлайнах
• Статистика по задачам
• Синхронизация с веб-версией

**Статусы задач:**
⏳ В ожидании - задача не начата
🔄 В работе - задача выполняется
✅ Завершена - задача выполнена
⚠️ Просрочена - дедлайн прошел

**Уведомления:**
Бот напомнит о дедлайнах за 3, 2, 1 день и в день выполнения.

**Синхронизация:**
Все проекты и задачи синхронизируются между ботом и веб-версией.
    """
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("migrate"))
async def cmd_migrate(message: Message):
    """Принудительная миграция данных из веб-версии"""
    user_id = message.from_user.id
    logger.info(f"🔄 Принудительная миграция от {user_id}")
    
    if user_id != TELEGRAM_USER_ID:
        await message.answer("❌ Эта команда доступна только владельцу бота.")
        return
    
    await message.answer("🔄 Начинаю принудительную миграцию данных из веб-версии...")
    
    try:
        result = await migrate_web_data()
        if result['success']:
            if result['projects_updated'] > 0:
                await message.answer(
                    f"✅ Успешно мигрировано!\n\n"
                    f"• Проектов: {result['projects_updated']}\n"
                    f"• Задач: {result['tasks_count']}\n\n"
                    f"Теперь все данные из веб-версии доступны в боте!"
                )
            else:
                await message.answer("ℹ️ Нет данных для миграции. Возможно, данные уже синхронизированы.")
        else:
            await message.answer(f"❌ Ошибка при миграции: {result['error']}")
    except Exception as e:
        logger.error(f"❌ Ошибка миграции: {e}")
        await message.answer(f"❌ Ошибка: {str(e)}")

@dp.message(Command("ping"))
async def cmd_ping(message: Message):
    logger.info(f"🏓 /ping от {message.from_user.id}")
    await message.answer("🏓 Pong! Бот жив и работает")

@dp.message(Command("test"))
async def cmd_test(message: Message):
    logger.info(f"🧪 /test от {message.from_user.id}")
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            projects_count = await conn.fetchval('SELECT COUNT(*) FROM projects WHERE user_id = $1', message.from_user.id)
            tasks_count = await conn.fetchval('''
                SELECT COUNT(*) FROM tasks t 
                JOIN projects p ON t.project_id = p.id 
                WHERE p.user_id = $1
            ''', message.from_user.id)
            notifications_count = await conn.fetchval("SELECT COUNT(*) FROM notifications WHERE is_sent = FALSE")
            
            await message.answer(
                f"✅ Бот работает!\n"
                f"📁 Ваших проектов: {projects_count}\n"
                f"📋 Ваших задач: {tasks_count}\n"
                f"🔔 Активных уведомлений: {notifications_count}"
            )
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    """Показать ID пользователя"""
    user_id = message.from_user.id
    logger.info(f"🆔 /id от {user_id}")
    
    info_text = f"""
🆔 **Ваш Telegram ID:** `{user_id}`

**Синхронизация с веб-версией:**
• Веб-версия настроена на использование ID: `{TELEGRAM_USER_ID}`
• Для синхронизации используйте команду `/migrate`
• Все данные автоматически синхронизируются между ботом и вебом

**Текущий статус:**
"""
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Проверяем данные для этого пользователя
            user_projects = await conn.fetchval('SELECT COUNT(*) FROM projects WHERE user_id = $1', user_id)
            user_tasks = await conn.fetchval('''
                SELECT COUNT(*) FROM tasks t 
                JOIN projects p ON t.project_id = p.id 
                WHERE p.user_id = $1
            ''', user_id)
            
            info_text += f"• Ваших проектов: {user_projects}\n"
            info_text += f"• Ваших задач: {user_tasks}\n"
            
            # Проверяем данные с user_id = 1 (старые данные из веба)
            web_projects = await conn.fetchval('SELECT COUNT(*) FROM projects WHERE user_id = 1')
            if web_projects > 0:
                info_text += f"\n⚠️ **Обнаружены данные из веб-версии:** {web_projects} проектов\n"
                info_text += f"Используйте команду `/migrate` чтобы перенести их в ваш аккаунт."
    
    except Exception as e:
        logger.error(f"❌ Ошибка получения информации: {e}")
    
    await message.answer(info_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🔔 Уведомления")
async def notifications_menu(message: Message):
    """Меню уведомлений"""
    await message.answer(
        "🔔 **Настройка уведомлений**\n\n"
        "Выберите, за сколько дней до дедлайна получать уведомления:",
        reply_markup=get_notification_settings_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(F.text == "📊 Статистика")
async def statistics_menu(message: Message):
    """Статистика по задачам"""
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Статистика по статусам
            stats = await conn.fetch('''
                SELECT 
                    COUNT(*) as total,
                    COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed,
                    COUNT(CASE WHEN status = 'in_progress' THEN 1 END) as in_progress,
                    COUNT(CASE WHEN status = 'pending' THEN 1 END) as pending,
                    COUNT(CASE WHEN status = 'overdue' THEN 1 END) as overdue
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE p.user_id = $1
            ''', message.from_user.id)
            
            # Активные уведомления
            active_notifications = await conn.fetchval('''
                SELECT COUNT(*) FROM notifications n
                JOIN tasks t ON n.task_id = t.id
                JOIN projects p ON t.project_id = p.id
                WHERE p.user_id = $1 AND n.is_sent = FALSE
            ''', message.from_user.id)
            
            if stats and len(stats) > 0 and stats[0]['total'] > 0:
                stat = stats[0]
                efficiency = round((stat['completed'] / stat['total']) * 100, 1) if stat['total'] > 0 else 0
                message_text = (
                    f"📊 **Ваша статистика:**\n\n"
                    f"• Всего задач: {stat['total']}\n"
                    f"• ✅ Завершено: {stat['completed']}\n"
                    f"• 🔄 В работе: {stat['in_progress']}\n"
                    f"• ⏳ В ожидании: {stat['pending']}\n"
                    f"• ⚠️ Просрочено: {stat['overdue']}\n"
                    f"• 🔔 Активных уведомлений: {active_notifications}\n\n"
                    f"**Эффективность:** {efficiency}%"
                )
            else:
                message_text = "📊 У вас пока нет задач для статистики."
            
            await message.answer(message_text, parse_mode=ParseMode.MARKDOWN)
            
    except Exception as e:
        logger.error(f"❌ Ошибка получения статистики: {e}")
        await message.answer("❌ Ошибка при получении статистики.")

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
    user_id = message.from_user.id
    logger.info(f"📁 Просмотр проектов от {user_id}")
    
    # Если это владелец, проверяем миграцию
    if user_id == TELEGRAM_USER_ID:
        try:
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                # Проверяем есть ли данные с user_id = 1
                web_data_count = await conn.fetchval('SELECT COUNT(*) FROM projects WHERE user_id = 1')
                if web_data_count > 0:
                    await message.answer(
                        f"⚠️ Обнаружено {web_data_count} проектов из веб-версии.\n"
                        f"Используйте команду `/migrate` чтобы перенести их в ваш аккаунт."
                    )
        except Exception as e:
            logger.error(f"❌ Ошибка проверки веб-данных: {e}")
    
    try:
        pool = await get_db_pool()
        
        async with pool.acquire() as conn:
            projects = await conn.fetch(
                "SELECT id, name FROM projects WHERE user_id = $1 ORDER BY created_at DESC",
                user_id
            )
        
        if not projects:
            await message.answer(
                "У вас пока нет проектов. Нажмите ➕ Проект.",
                reply_markup=get_main_keyboard()
            )
            return
        
        for project in projects:
            async with pool.acquire() as conn:
                tasks_stats = await conn.fetchrow('''
                    SELECT 
                        COUNT(*) as total,
                        COUNT(CASE WHEN status = 'completed' THEN 1 END) as completed
                    FROM tasks 
                    WHERE project_id = $1
                ''', project['id'])
                
                stats_text = ""
                if tasks_stats and tasks_stats['total'] > 0:
                    stats_text = f" ({tasks_stats['completed']}/{tasks_stats['total']} завершено)"
                
                await message.answer(
                    f"📁 {project['name']}{stats_text}",
                    reply_markup=get_project_keyboard(project['id'])
                )
                
    except Exception as e:
        logger.error(f"❌ Ошибка при получении проектов: {e}")
        await message.answer("❌ Произошла ошибка при получении проектов.")

# Callback для кнопок проекта
@dp.callback_query(F.data.startswith("tasks:"))
async def show_tasks(callback: CallbackQuery):
    project_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    logger.info(f"📋 Задачи проекта {project_id} от {user_id}")
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT name FROM projects WHERE id = $1 AND user_id = $2",
                project_id, user_id
            )
            
            if not project:
                await callback.answer("Проект не найден!")
                return
            
            # Получаем задачи с учетом просроченных
            tasks = await conn.fetch('''
                SELECT id, title, deadline, status,
                    CASE 
                        WHEN deadline < CURRENT_DATE AND status != 'completed' THEN 'overdue'
                        ELSE status
                    END as display_status
                FROM tasks 
                WHERE project_id = $1 
                ORDER BY 
                    CASE WHEN deadline < CURRENT_DATE AND status != 'completed' THEN 0 ELSE 1 END,
                    deadline ASC
                LIMIT 20
            ''', project_id)
        
        if not tasks:
            message_text = f"📁 **Проект: {project['name']}**\n\nЗадач пока нет."
        else:
            message_text = f"📁 **Проект: {project['name']}**\n\n📋 **Задачи:**\n"
            for task in tasks:
                deadline = task['deadline'].strftime('%d.%m.%y')
                status_icon = {
                    'pending': '⏳',
                    'in_progress': '🔄',
                    'completed': '✅',
                    'overdue': '⚠️'
                }.get(task['display_status'], '⏳')
                
                message_text += f"{status_icon} {task['title']} — {deadline}\n"
        
        await callback.message.edit_text(
            message_text,
            reply_markup=get_tasks_keyboard(project_id, show_back=True),
            parse_mode=ParseMode.MARKDOWN
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении задач: {e}")
        await callback.answer("❌ Произошла ошибка.")

@dp.callback_query(F.data.startswith("task_statuses:"))
async def show_task_statuses(callback: CallbackQuery):
    """Показать задачи с возможностью изменения статуса"""
    project_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT name FROM projects WHERE id = $1 AND user_id = $2",
                project_id, user_id
            )
            
            if not project:
                await callback.answer("Проект не найден!")
                return
            
            tasks = await conn.fetch('''
                SELECT id, title, deadline, status,
                    CASE 
                        WHEN deadline < CURRENT_DATE AND status != 'completed' THEN 'overdue'
                        ELSE status
                    END as display_status
                FROM tasks 
                WHERE project_id = $1 
                ORDER BY deadline ASC
                LIMIT 20
            ''', project_id)
        
        if not tasks:
            await callback.message.edit_text(
                f"📁 **Проект: {project['name']}**\n\nВ этом проекте пока нет задач.",
                reply_markup=get_tasks_keyboard(project_id, show_back=True),
                parse_mode=ParseMode.MARKDOWN
            )
            await callback.answer("В этом проекте пока нет задач!")
            return
        
        message_text = f"📁 **Проект: {project['name']}**\n\n📋 **Задачи (кликните для изменения статуса):**\n"
        
        await callback.message.edit_text(
            message_text,
            reply_markup=get_tasks_list_keyboard(tasks, project_id),
            parse_mode=ParseMode.MARKDOWN
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении статусов задач: {e}")
        await callback.answer("❌ Произошла ошибка.")

@dp.callback_query(F.data.startswith("task_detail:"))
async def show_task_detail(callback: CallbackQuery):
    """Детальная информация о задаче с выбором статуса"""
    task_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            task = await conn.fetchrow('''
                SELECT t.*, p.name as project_name, p.id as project_id
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE t.id = $1 AND p.user_id = $2
            ''', task_id, user_id)
            
            if not task:
                await callback.answer("Задача не найдена!")
                return
            
            deadline = task['deadline'].strftime('%d.%m.%Y')
            created = task['created_at'].strftime('%d.%m.%Y')
            status_text = TASK_STATUSES.get(task['status'], '⏳ В ожидании')
            
            # Проверяем, просрочена ли задача
            current_status = task['status']
            if task['deadline'] < datetime.now().date() and current_status != 'completed':
                current_status = 'overdue'
                status_text = TASK_STATUSES.get('overdue')
            
            message_text = (
                f"📋 **Задача:** {task['title']}\n"
                f"📁 **Проект:** {task['project_name']}\n"
                f"📅 **Создана:** {created}\n"
                f"⏰ **Дедлайн:** {deadline}\n"
                f"📊 **Статус:** {status_text}\n\n"
                f"Выберите новый статус:"
            )
            
            await callback.message.edit_text(
                message_text,
                reply_markup=get_task_keyboard(task_id, current_status),
                parse_mode=ParseMode.MARKDOWN
            )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении деталей задачи: {e}")
        await callback.answer("❌ Произошла ошибка.")

@dp.callback_query(F.data.startswith("set_status:"))
async def set_task_status(callback: CallbackQuery):
    """Изменение статуса задачи"""
    _, task_id, new_status = callback.data.split(":")
    task_id = int(task_id)
    user_id = callback.from_user.id
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Проверяем доступ к задаче
            task = await conn.fetchrow('''
                SELECT t.*, p.id as project_id FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE t.id = $1 AND p.user_id = $2
            ''', task_id, user_id)
            
            if not task:
                await callback.answer("Задача не найдена!")
                return
            
            # Обновляем статус
            if new_status == 'completed':
                await conn.execute('''
                    UPDATE tasks 
                    SET status = $1, 
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = $2
                ''', new_status, task_id)
            else:
                await conn.execute('''
                    UPDATE tasks 
                    SET status = $1, 
                        completed_at = NULL,
                        updated_at = NOW()
                    WHERE id = $2
                ''', new_status, task_id)
            
            status_text = TASK_STATUSES.get(new_status, 'Неизвестный статус')
            await callback.answer(f"✅ Статус изменен на: {status_text}")
            
            # Обновляем сообщение
            deadline = task['deadline'].strftime('%d.%m.%Y')
            created = task['created_at'].strftime('%d.%m.%Y')
            
            message_text = (
                f"📋 **Задача:** {task['title']}\n"
                f"📁 **Проект:** {task['project_name'] if 'project_name' in task else '...'}\n"
                f"📅 **Создана:** {created}\n"
                f"⏰ **Дедлайн:** {deadline}\n"
                f"📊 **Статус:** {status_text}\n\n"
                f"Выберите новый статус:"
            )
            
            await callback.message.edit_text(
                message_text,
                reply_markup=get_task_keyboard(task_id, new_status),
                parse_mode=ParseMode.MARKDOWN
            )
            
    except Exception as e:
        logger.error(f"❌ Ошибка при изменении статуса: {e}")
        await callback.answer("❌ Ошибка при изменении статуса")

@dp.callback_query(F.data.startswith("remind:"))
async def set_reminder(callback: CallbackQuery):
    """Установка напоминания"""
    _, task_id, days_before = callback.data.split(":")
    task_id = int(task_id)
    days_before = int(days_before)
    user_id = callback.from_user.id
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Проверяем доступ к задаче
            task = await conn.fetchrow('''
                SELECT t.* FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE t.id = $1 AND p.user_id = $2
            ''', task_id, user_id)
            
            if not task:
                await callback.answer("Задача не найдена!")
                return
            
            # Создаем уведомление
            notification_type = f"reminder_{days_before}_days" if days_before > 0 else "deadline_today"
            await create_notification(user_id, task_id, notification_type, days_before)
            
            if days_before == 0:
                await callback.answer("✅ Напоминание установлено на сегодня!")
            else:
                await callback.answer(f"✅ Напоминание установлено за {days_before} дня!")
            
    except Exception as e:
        logger.error(f"❌ Ошибка при установке напоминания: {e}")
        await callback.answer("❌ Ошибка при установке напоминания")

@dp.callback_query(F.data.startswith("back_to_task_list:"))
async def back_to_task_list(callback: CallbackQuery):
    """Возврат к списку задач"""
    user_id = callback.from_user.id
    
    try:
        task_id = int(callback.data.split(":")[1])
        
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Получаем информацию о задаче
            task_info = await conn.fetchrow('''
                SELECT t.project_id, p.name as project_name
                FROM tasks t
                JOIN projects p ON t.project_id = p.id
                WHERE t.id = $1 AND p.user_id = $2
            ''', task_id, user_id)
            
            if not task_info:
                await callback.answer("Задача не найдена!")
                return
            
            project_id = task_info['project_id']
            
            # Получаем задачи проекта
            tasks = await conn.fetch('''
                SELECT id, title, deadline, status,
                    CASE 
                        WHEN deadline < CURRENT_DATE AND status != 'completed' THEN 'overdue'
                        ELSE status
                    END as display_status
                FROM tasks 
                WHERE project_id = $1 
                ORDER BY deadline ASC
                LIMIT 20
            ''', project_id)
        
        if not tasks:
            message_text = f"📁 **Проект: {task_info['project_name']}**\n\nВ этом проекте пока нет задач."
            keyboard = get_tasks_keyboard(project_id, show_back=True)
        else:
            message_text = f"📁 **Проект: {task_info['project_name']}**\n\n📋 **Задачи (кликните для изменения статуса):**\n"
            keyboard = get_tasks_list_keyboard(tasks, project_id)
        
        await callback.message.edit_text(
            message_text,
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
        await callback.answer()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при возврате к списку задач: {e}")
        await callback.answer("❌ Произошла ошибка")

# Уведомления
@dp.callback_query(F.data.startswith("notif_setting:"))
async def set_notification_setting(callback: CallbackQuery):
    """Настройка уведомлений"""
    setting = callback.data.split(":")[1]
    
    try:
        if setting == "off":
            # Отключаем все уведомления
            pool = await get_db_pool()
            async with pool.acquire() as conn:
                await conn.execute('''
                    UPDATE notifications SET is_sent = TRUE 
                    WHERE user_id = $1 AND is_sent = FALSE
                ''', callback.from_user.id)
            
            await callback.answer("🔕 Все уведомления отключены")
        else:
            days = int(setting)
            await callback.answer(f"✅ Уведомления будут приходить за {days} дня до дедлайна")
            
    except Exception as e:
        logger.error(f"❌ Ошибка настройки уведомлений: {e}")
        await callback.answer("❌ Ошибка при настройке уведомлений")

@dp.callback_query(F.data == "list_notifications")
async def list_notifications(callback: CallbackQuery):
    """Список активных уведомлений"""
    user_id = callback.from_user.id
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            notifications = await conn.fetch('''
                SELECT n.*, t.title, t.deadline
                FROM notifications n
                JOIN tasks t ON n.task_id = t.id
                JOIN projects p ON t.project_id = p.id
                WHERE p.user_id = $1 AND n.is_sent = FALSE
                ORDER BY n.notification_time
                LIMIT 20
            ''', user_id)
        
        if not notifications:
            message_text = "🔕 У вас нет активных уведомлений."
        else:
            message_text = "🔔 **Ваши активные уведомления:**\n\n"
            for notif in notifications:
                time = notif['notification_time'].strftime('%d.%m.%Y %H:%M')
                deadline = notif['deadline'].strftime('%d.%m.%Y')
                days_left = (notif['deadline'] - datetime.now().date()).days
                days_text = f" (через {days_left} дней)" if days_left > 0 else " (сегодня)" if days_left == 0 else f" (просрочено на {abs(days_left)} дней)"
                
                message_text += f"• **{notif['title']}**\n"
                message_text += f"  ⏰ Уведомление: {time}\n"
                message_text += f"  📅 Дедлайн: {deadline}{days_text}\n\n"
        
        await callback.message.answer(message_text, parse_mode=ParseMode.MARKDOWN)
        await callback.answer()
        
    except Exception as e:
        logger.error(f"❌ Ошибка при получении уведомлений: {e}")
        await callback.answer("❌ Ошибка при получении уведомлений")

# Навигационные callback
@dp.callback_query(F.data == "back_to_projects")
async def back_to_projects(callback: CallbackQuery):
    """Возврат к списку проектов"""
    try:
        await show_projects(callback.message)
        await callback.answer()
    except Exception as e:
        logger.error(f"❌ Ошибка при возврате к проектам: {e}")
        await callback.answer("❌ Ошибка")

@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    """Возврат к главному меню"""
    try:
        await callback.message.answer("Используйте кнопки ниже:", reply_markup=get_main_keyboard())
        await callback.answer()
    except Exception as e:
        logger.error(f"❌ Ошибка при возврате в главное меню: {e}")

@dp.callback_query(F.data == "noop")
async def noop_callback(callback: CallbackQuery):
    """Пустой callback для неактивных кнопок"""
    await callback.answer()

# Удаление проекта
@dp.callback_query(F.data.startswith("delete:"))
async def delete_project(callback: CallbackQuery):
    project_id = int(callback.data.split(":")[1])
    user_id = callback.from_user.id
    logger.info(f"🗑 Удаление проекта {project_id} от {user_id}")
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT name FROM projects WHERE id = $1 AND user_id = $2",
                project_id, user_id
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
    user_id = callback.from_user.id
    logger.info(f"➕ Добавление задачи в проект {project_id}")
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            project = await conn.fetchrow(
                "SELECT id, name FROM projects WHERE id = $1 AND user_id = $2",
                project_id, user_id
            )
            
            if not project:
                await callback.answer("Проект не найден!")
                return
            
            await state.update_data(project_id=project_id, project_name=project['name'])
    
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке проекта: {e}")
        await callback.answer("❌ Произошла ошибка.")
        return
    
    await callback.message.answer(f"📝 Добавление задачи в проект '{project['name']}'\n\nНазвание задачи?")
    await state.set_state(TaskState.waiting_for_title)
    await callback.answer()

@dp.message(TaskState.waiting_for_title)
async def process_task_title(message: Message, state: FSMContext):
    title = message.text.strip()
    
    if not title:
        await message.answer("Название задачи не может быть пустым. Введите название:")
        return
    
    await state.update_data(title=title)
    await message.answer("📅 Дедлайн (ДД.ММ.ГГ, например: 05.02.26)?")
    await state.set_state(TaskState.waiting_for_deadline)

@dp.message(TaskState.waiting_for_deadline)
async def process_task_deadline(message: Message, state: FSMContext):
    deadline_str = message.text.strip()
    
    # Валидация формата даты
    try:
        for fmt in ('%d.%m.%y', '%d.%m.%Y'):
            try:
                deadline = datetime.strptime(deadline_str, fmt).date()
                break
            except ValueError:
                continue
        else:
            raise ValueError("Неверный формат даты")
            
        today = datetime.now().date()
        if deadline < today:
            logger.warning(f"Дата в прошлом: {deadline_str}")
            
    except ValueError as e:
        logger.warning(f"Неверный формат даты: {deadline_str}")
        await message.answer(
            "❌ Неверный формат даты. Попробуйте снова (ДД.ММ.ГГ или ДД.ММ.ГГГГ):"
        )
        return
    
    # Сохранение задачи
    data = await state.get_data()
    
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            # Добавляем задачу в транзакции
            async with conn.transaction():
                # Добавляем задачу
                result = await conn.fetchrow(
                    "INSERT INTO tasks (project_id, title, deadline) VALUES ($1, $2, $3) RETURNING id",
                    data['project_id'], data['title'], deadline
                )
                
                task_id = result['id']
                
                # Автоматически создаем уведомления
                notification_types = [
                    ("days_before_3", 3),
                    ("days_before_2", 2),
                    ("days_before_1", 1),
                    ("deadline_today", 0)
                ]
                
                for notif_type, days in notification_types:
                    await create_notification(message.from_user.id, task_id, notif_type, days)
        
        await message.answer(
            f"✅ Задача '{data['title']}' добавлена в проект '{data['project_name']}'!\n\n"
            f"📅 Дедлайн: {deadline.strftime('%d.%m.%Y')}\n"
            f"🔔 Уведомления установлены за 3, 2, 1 день и в день дедлайна.",
            reply_markup=get_main_keyboard()
        )
        logger.info(f"✅ Задача добавлена в проект {data['project_id']}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при сохранении задачи: {e}")
        await message.answer("❌ Произошла ошибка при сохранении задачи.")
    
    await state.clear()

# ========== WEBHOOK ЛОГИКА ==========
async def on_startup(bot: Bot):
    """Установка вебхука при запуске"""
    logger.info("🔄 Запуск бота...")
    
    try:
        # Создаем и проверяем таблицы
        await create_tables()
        
        # Автоматическая миграция данных при старте
        logger.info("🔄 Проверка данных из веб-версии...")
        result = await migrate_web_data()
        if result['success'] and result['projects_updated'] > 0:
            logger.info(f"✅ Автоматически мигрировано {result['projects_updated']} проектов и {result['tasks_count']} задач")
        
        # Запускаем планировщик уведомлений
        global notification_task
        notification_task = asyncio.create_task(notification_scheduler())
        
        # Удаляем старый вебхук
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            logger.info("✅ Старый вебхук удален")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось удалить вебхук: {e}")
        
        # Проверяем, есть ли уже установленный вебхук
        webhook_info = await bot.get_webhook_info()
        
        if webhook_info.url != WEBHOOK_URL:
            # Устанавливаем новый вебхук
            await bot.set_webhook(
                url=WEBHOOK_URL,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types(),
                max_connections=40
            )
            logger.info(f"✅ Вебхук установлен: {WEBHOOK_URL}")
        else:
            logger.info(f"✅ Вебхук уже установлен: {WEBHOOK_URL}")
        
        # Проверяем
        webhook_info = await bot.get_webhook_info()
        logger.info(f"✅ Информация о вебхуке: {webhook_info.url}")
        logger.info(f"✅ Ожидающих обновлений: {webhook_info.pending_update_count}")
        logger.info(f"✅ Последняя ошибка: {webhook_info.last_error_message or 'Нет'}")
        
        logger.info("🎉 Бот запущен с уведомлениями и статусами!")
        
    except Exception as e:
        logger.error(f"❌ Критическая ошибка при запуске: {e}")
        logger.info("🔄 Пробуем продолжить без вебхука...")

async def on_shutdown(bot: Bot):
    """Очистка при выключении"""
    logger.info("🛑 Остановка бота...")
    try:
        # Останавливаем планировщик уведомлений
        global notification_task
        if notification_task:
            notification_task.cancel()
            try:
                await notification_task
            except asyncio.CancelledError:
                pass
        
        if db_pool:
            await db_pool.close()
        
        logger.info("✅ Ресурсы освобождены")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

# ========== HTTP ХЕНДЛЕРЫ ==========
async def health_check(request):
    """Health check для Render"""
    return web.Response(
        text="OK",
        status=200,
        headers={"Content-Type": "text/plain"}
    )

async def home_page(request):
    """Главная страница"""
    html = f"""
    <html>
    <head><title>Task Planner Pro</title></head>
    <body>
        <h1>🤖 Task Planner Pro</h1>
        <p>Бот с уведомлениями и статусами задач</p>
        <p><strong>Status:</strong> ✅ Работает</p>
        <p><strong>URL:</strong> https://{WEBHOOK_HOST}</p>
        <p><strong>Уведомления:</strong> Активны</p>
        <p><strong>Синхронизация:</strong> Активна (User ID: {TELEGRAM_USER_ID})</p>
        <hr>
        <p><a href="/health">Health Check</a></p>
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

# ========== ОСНОВНАЯ ФУНКЦИЯ ==========
def main():
    """Запуск приложения"""
    logger.info("🚀 Запуск Task Planner Pro...")
    
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
    webhook_handler.register(app, path=WEBHOOK_PATH)
    
    # Добавляем дополнительные маршруты
    app.router.add_get("/", home_page)
    app.router.add_get("/health", health_check)
    
    # Настраиваем приложение
    setup_application(app, dp, bot=bot)
    
    # Запускаем сервер
    logger.info(f"🚀 Запуск сервера на порту {PORT}")
    logger.info(f"🌐 Вебхук: {WEBHOOK_URL}")
    logger.info(f"👤 Используется Telegram ID: {TELEGRAM_USER_ID}")
    
    try:
        web.run_app(
            app,
            host="0.0.0.0",
            port=PORT,
            access_log=None
        )
    except Exception as e:
        logger.error(f"❌ Ошибка при запуске сервера: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
