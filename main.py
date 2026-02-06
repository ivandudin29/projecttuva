import os
import logging
import sys
from datetime import datetime
import asyncio

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

# Инициализация
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# Глобальный пул подключений
db_pool = None

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
    try:
        pool = await get_db_pool()
        async with pool.acquire() as conn:
            count = await conn.fetchval('SELECT COUNT(*) FROM projects')
        await message.answer(f"✅ Бот работает! Проектов в базе: {count}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {str(e)[:100]}")

@dp.message(Command("id"))
async def cmd_id(message: Message):
    logger.info(f"🆔 /id от {message.from_user.id}")
    await message.answer(f"Ваш ID: {message.from_user.id}")

@dp.message(Command("status"))
async def cmd_status(message: Message):
    logger.info(f"📊 /status от {message.from_user.id}")
    await message.answer(f"✅ Бот работает на Render\n🌐 URL: {WEBHOOK_HOST}")

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

# Webhook логика
async def on_startup(bot: Bot):
    """Установка вебхука при запуске"""
    logger.info("🔄 Установка вебхука...")
    
    try:
        # Создаем таблицы
        await create_tables()
        
        # Даем время на инициализацию
        await asyncio.sleep(1)
        
        # Удаляем старый вебхук
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Старый вебхук удален")
        
        # Устанавливаем новый
        await bot.set_webhook(
            url=WEBHOOK_URL,
            drop_pending_updates=True,
            allowed_updates=dp.resolve_used_update_types()
        )
        logger.info(f"✅ Вебхук установлен: {WEBHOOK_URL}")
        
        # Проверяем
        webhook_info = await bot.get_webhook_info()
        logger.info(f"✅ Информация о вебхуке: {webhook_info.url}")
        logger.info(f"✅ Ожидающих обновлений: {webhook_info.pending_update_count}")
        
    except Exception as e:
        logger.error(f"❌ Ошибка при установке вебхука: {e}")

async def on_shutdown(bot: Bot):
    """Очистка при выключении"""
    logger.info("🛑 Остановка бота...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        if db_pool:
            await db_pool.close()
        await bot.session.close()
        logger.info("✅ Ресурсы освобождены")
    except Exception as e:
        logger.error(f"Ошибка при остановке: {e}")

# HTTP хендлеры для Render
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
    <head><title>Task Planner Bot</title></head>
    <body>
        <h1>🤖 Task Planner Bot</h1>
        <p>Бот для управления проектами и задачами</p>
        <p><strong>Status:</strong> ✅ Работает</p>
        <p><strong>URL:</strong> https://{WEBHOOK_HOST}</p>
        <hr>
        <p><a href="/health">Health Check</a></p>
        <p><a href="/status">Bot Status</a></p>
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

async def status_page(request):
    """Страница статуса бота"""
    try:
        info = await bot.get_webhook_info()
        html = f"""
        <html>
        <head><title>Bot Status</title></head>
        <body>
            <h1>🤖 Статус бота</h1>
            <p><strong>Webhook URL:</strong> {info.url or 'Not set'}</p>
            <p><strong>Pending Updates:</strong> {info.pending_update_count}</p>
            <p><strong>Last Error:</strong> {info.last_error_message or 'None'}</p>
            <p><strong>Max Connections:</strong> {info.max_connections}</p>
            <p><strong>Service URL:</strong> https://{WEBHOOK_HOST}</p>
            <hr>
            <p><a href="/">Главная</a></p>
            <p><a href="/health">Health Check</a></p>
        </body>
        </html>
        """
        return web.Response(text=html, content_type="text/html")
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=500)

def main():
    """Запуск приложения"""
    logger.info("🚀 Запуск приложения...")
    
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
    app.router.add_get("/status", status_page)
    
    # Настраиваем приложение
    setup_application(app, dp, bot=bot)
    
    # Запускаем сервер
    logger.info(f"🚀 Запуск сервера на порту {PORT}")
    logger.info(f"🌐 Вебхук: {WEBHOOK_URL}")
    
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
