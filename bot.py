import asyncio
import logging
import sqlite3
import time
import os
import shutil
from datetime import datetime
from typing import List, Tuple

from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.exceptions import TelegramForbiddenError

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ==================== КОНФИГУРАЦИЯ ====================
BOT_TOKEN = "8557190026:AAGLVLyjqSsre6-h5138wlRHNpZI-DjivVc"
NEW_BOT_USERNAME = "PulsChatManagerBot"  # ← ТОЛЬКО ЭТО МЕНЯЕШЬ (без @)
ADMIN_IDS = [6708209142]  # твой ID

# ==================== СОЗДАЁМ БОТА И ДИСПЕТЧЕРА ====================
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher(storage=MemoryStorage())

# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_path="redirect.db"):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)
    
    def init_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS users
                         (user_id INTEGER PRIMARY KEY,
                          username TEXT,
                          full_name TEXT,
                          first_seen INTEGER,
                          last_active INTEGER)''')
            c.execute('''CREATE TABLE IF NOT EXISTS stats
                         (id INTEGER PRIMARY KEY DEFAULT 1,
                          messages INTEGER DEFAULT 0,
                          commands INTEGER DEFAULT 0)''')
            c.execute('INSERT OR IGNORE INTO stats (id, messages, commands) VALUES (1, 0, 0)')
            conn.commit()
    
    def add_user(self, user_id: int, username: str, full_name: str):
        with self.get_connection() as conn:
            now = int(time.time())
            conn.execute('INSERT OR REPLACE INTO users (user_id, username, full_name, first_seen, last_active) VALUES (?, ?, ?, COALESCE((SELECT first_seen FROM users WHERE user_id = ?), ?), ?)',
                        (user_id, username, full_name, user_id, now, now))
            conn.commit()
    
    def update_activity(self, user_id: int):
        with self.get_connection() as conn:
            conn.execute('UPDATE users SET last_active = ? WHERE user_id = ?', (int(time.time()), user_id))
            conn.commit()
    
    def increment_messages(self):
        with self.get_connection() as conn:
            conn.execute('UPDATE stats SET messages = messages + 1 WHERE id = 1')
            conn.commit()
    
    def increment_commands(self):
        with self.get_connection() as conn:
            conn.execute('UPDATE stats SET commands = commands + 1 WHERE id = 1')
            conn.commit()
    
    def get_stats(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            users = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            active = c.execute('SELECT COUNT(*) FROM users WHERE last_active > ?', (int(time.time()) - 86400,)).fetchone()[0]
            stats = c.execute('SELECT messages, commands FROM stats WHERE id = 1').fetchone()
            return {'users': users, 'active_today': active, 'messages': stats[0], 'commands': stats[1]}
    
    def get_all_users(self) -> List[Tuple]:
        with self.get_connection() as conn:
            return conn.execute('SELECT user_id, username, full_name, last_active FROM users ORDER BY last_active DESC').fetchall()

db = Database()

# ==================== СОСТОЯНИЯ ====================
class BroadcastStates(StatesGroup):
    waiting_for_target = State()
    waiting_for_text = State()

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard(is_admin: bool = False):
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="🚀 Перейти в нового бота", url=f"https://t.me/{NEW_BOT_USERNAME}"))
    if is_admin:
        builder.add(InlineKeyboardButton(text="👑 Админ панель", callback_data="admin_panel"))
    builder.adjust(1)
    return builder.as_markup()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))
    builder.add(InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"))
    builder.add(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.add(InlineKeyboardButton(text="📦 Бэкап", callback_data="admin_backup"))
    builder.add(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
    builder.adjust(2)
    return builder.as_markup()

def get_broadcast_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📱 В ЛС", callback_data="broadcast_pm"))
    builder.add(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel"))
    builder.adjust(2)
    return builder.as_markup()

# ==================== ОСНОВНЫЕ ХЕНДЛЕРЫ ====================
async def send_redirect_message(message: Message):
    is_admin = message.from_user.id in ADMIN_IDS
    await message.answer(
        f"🤖 <b>Бот переехал!</b>\n\n"
        f"Новый бот: <b>@{NEW_BOT_USERNAME}</b>\n\n"
        f"Нажмите на кнопку ниже, чтобы перейти 👇",
        reply_markup=get_main_keyboard(is_admin),
        parse_mode="HTML"
    )

@dp.message(Command("start"))
async def cmd_start(message: Message):
    db.add_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    db.increment_commands()
    await send_redirect_message(message)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        return
    db.increment_commands()
    await message.answer("👑 Админ панель", reply_markup=get_admin_keyboard())

@dp.message()
async def handle_any_message(message: Message):
    # Администратору не отправляем сообщение о переезде на каждое сообщение
    if message.from_user.id in ADMIN_IDS:
        return
    
    db.add_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    db.increment_messages()
    await send_redirect_message(message)

# ==================== CALLBACK ХЕНДЛЕРЫ ====================
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    is_admin = callback.from_user.id in ADMIN_IDS
    await callback.message.edit_text(
        f"🤖 Бот переехал!\n\nНовый бот: @{NEW_BOT_USERNAME}",
        reply_markup=get_main_keyboard(is_admin)
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.message.edit_text("👑 Админ панель", reply_markup=get_admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    stats = db.get_stats()
    text = f"📊 <b>Статистика</b>\n\n"
    text += f"👥 Всего пользователей: {stats['users']}\n"
    text += f"📅 Активны за 24ч: {stats['active_today']}\n"
    text += f"💬 Всего сообщений: {stats['messages']}\n"
    text += f"⚙️ Команд: {stats['commands']}"
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_users")
async def admin_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    users = db.get_all_users()
    if not users:
        await callback.message.edit_text("👥 Нет пользователей", reply_markup=get_admin_keyboard())
        await callback.answer()
        return
    
    text = "👥 <b>Пользователи</b>\n\n"
    for uid, username, full_name, last_active in users[:30]:
        date = datetime.fromtimestamp(last_active).strftime("%d.%m %H:%M")
        name = full_name or username or str(uid)
        text += f"• {name}\n  ID: <code>{uid}</code> | {date}\n\n"
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard(), parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.message.edit_text("📢 Рассылка\n\nВыберите получателей:", reply_markup=get_broadcast_keyboard())
    await state.set_state(BroadcastStates.waiting_for_target)
    await callback.answer()

@dp.callback_query(F.data == "broadcast_pm")
async def broadcast_pm(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await state.update_data(broadcast_target='pm')
    await callback.message.edit_text(
        "📝 Отправьте текст для рассылки.\n\n"
        "Поддерживается HTML форматирование.",
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_panel")).as_markup()
    )
    await state.set_state(BroadcastStates.waiting_for_text)
    await callback.answer()

@dp.message(BroadcastStates.waiting_for_text)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        await state.clear()
        return
    
    data = await state.get_data()
    target = data.get('broadcast_target')
    if not target:
        await message.answer("❌ Ошибка!")
        await state.clear()
        return
    
    text = message.html_text
    
    # Получаем всех пользователей
    users = db.get_all_users()
    if not users:
        await message.answer("❌ Нет пользователей для рассылки!")
        await state.clear()
        return
    
    sent, failed = 0, 0
    status_msg = await message.answer(f"📤 Начинаю рассылку...\nВсего: {len(users)}")
    
    for user_id, _, _, _ in users:
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
            sent += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception as e:
            failed += 1
            logger.error(f"Ошибка отправки {user_id}: {e}")
        
        if (sent + failed) % 20 == 0:
            await status_msg.edit_text(f"📤 Прогресс: {sent + failed}/{len(users)}\n✅ {sent}\n❌ {failed}")
        await asyncio.sleep(0.05)
    
    await status_msg.edit_text(f"✅ Рассылка завершена!\n✅ {sent}\n❌ {failed}")
    await state.clear()

@dp.callback_query(F.data == "admin_backup")
async def admin_backup(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    try:
        backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2("redirect.db", backup_name)
        await callback.message.answer_document(FSInputFile(backup_name), caption=f"✅ Бэкап: {backup_name}")
        os.remove(backup_name)
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

# ==================== ЗАПУСК ====================
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
