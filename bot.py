import asyncio
import logging
import time
import sqlite3
from datetime import datetime
from typing import Optional, List, Tuple, Dict
from collections import defaultdict
import os
import shutil

from aiogram import Bot, Dispatcher, types, F, BaseMiddleware
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
NEW_BOT_USERNAME = "PulsChatManagerBot"  # юзернейм нового бота без @
ADMIN_IDS = [6708209142]  # твой ID

# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_path="redirect_bot.db"):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)
    
    def init_db(self):
        with self.get_connection() as conn:
            c = conn.cursor()
            # Пользователи которые написали боту
            c.execute('''CREATE TABLE IF NOT EXISTS users
                         (user_id INTEGER PRIMARY KEY,
                          username TEXT,
                          full_name TEXT,
                          first_seen INTEGER,
                          last_active INTEGER)''')
            # Статистика команд
            c.execute('''CREATE TABLE IF NOT EXISTS command_stats
                         (command TEXT PRIMARY KEY,
                          count INTEGER DEFAULT 0)''')
            # Кастомное сообщение
            c.execute('''CREATE TABLE IF NOT EXISTS custom_message
                         (id INTEGER PRIMARY KEY DEFAULT 1,
                          text TEXT,
                          photo_id TEXT,
                          photo_type TEXT,
                          button_text TEXT DEFAULT '🚀 Перейти в нового бота',
                          button_url TEXT)''')
            conn.commit()
            
            # Добавляем дефолтное сообщение если пусто
            c.execute('SELECT * FROM custom_message WHERE id = 1')
            if not c.fetchone():
                c.execute('INSERT INTO custom_message (id, text, button_text, button_url) VALUES (1, ?, ?, ?)',
                         ("🤖 Бот переехал!\n\nНовый бот: @" + NEW_BOT_USERNAME, 
                          "🚀 Перейти в нового бота", 
                          f"https://t.me/{NEW_BOT_USERNAME}"))
                conn.commit()
    
    def add_user(self, user_id: int, username: str, full_name: str):
        with self.get_connection() as conn:
            c = conn.cursor()
            now = int(time.time())
            c.execute('INSERT OR REPLACE INTO users (user_id, username, full_name, first_seen, last_active) VALUES (?, ?, ?, COALESCE((SELECT first_seen FROM users WHERE user_id = ?), ?), ?)',
                     (user_id, username, full_name, user_id, now, now))
            conn.commit()
    
    def update_user_activity(self, user_id: int):
        with self.get_connection() as conn:
            conn.execute('UPDATE users SET last_active = ? WHERE user_id = ?', (int(time.time()), user_id))
            conn.commit()
    
    def increment_command(self, command: str):
        with self.get_connection() as conn:
            conn.execute('INSERT INTO command_stats (command, count) VALUES (?, 1) ON CONFLICT(command) DO UPDATE SET count = count + 1', (command,))
            conn.commit()
    
    def get_stats(self) -> dict:
        with self.get_connection() as conn:
            c = conn.cursor()
            users = c.execute('SELECT COUNT(*) FROM users').fetchone()[0]
            active_today = c.execute('SELECT COUNT(*) FROM users WHERE last_active > ?', (int(time.time()) - 86400,)).fetchone()[0]
            commands = c.execute('SELECT command, count FROM command_stats ORDER BY count DESC').fetchall()
            return {'users': users, 'active_today': active_today, 'commands': commands}
    
    def get_all_users(self) -> List[Tuple[int, str, str, int]]:
        with self.get_connection() as conn:
            return conn.execute('SELECT user_id, username, full_name, last_active FROM users ORDER BY last_active DESC').fetchall()
    
    def get_custom_message(self) -> dict:
        with self.get_connection() as conn:
            r = conn.execute('SELECT text, photo_id, photo_type, button_text, button_url FROM custom_message WHERE id = 1').fetchone()
            return {'text': r[0], 'photo_id': r[1], 'photo_type': r[2], 'button_text': r[3], 'button_url': r[4]} if r else {}
    
    def update_custom_message(self, text: str = None, photo_id: str = None, photo_type: str = None, button_text: str = None, button_url: str = None):
        with self.get_connection() as conn:
            c = conn.cursor()
            if text is not None:
                c.execute('UPDATE custom_message SET text = ? WHERE id = 1', (text,))
            if photo_id is not None:
                c.execute('UPDATE custom_message SET photo_id = ?, photo_type = ? WHERE id = 1', (photo_id, photo_type))
            if button_text is not None:
                c.execute('UPDATE custom_message SET button_text = ? WHERE id = 1', (button_text,))
            if button_url is not None:
                c.execute('UPDATE custom_message SET button_url = ? WHERE id = 1', (button_url,))
            conn.commit()
    
    def reset_custom_message(self):
        with self.get_connection() as conn:
            conn.execute('UPDATE custom_message SET text = ?, photo_id = NULL, photo_type = NULL, button_text = ?, button_url = ? WHERE id = 1',
                        (f"🤖 Бот переехал!\n\nНовый бот: @{NEW_BOT_USERNAME}", "🚀 Перейти в нового бота", f"https://t.me/{NEW_BOT_USERNAME}"))
            conn.commit()

db = Database()

# ==================== СОСТОЯНИЯ ====================
class BroadcastStates(StatesGroup):
    waiting_for_target = State()
    waiting_for_text = State()
    waiting_for_media = State()

class CustomMessageStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()

# ==================== MIDDLEWARE ====================
class RedirectMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            db.add_user(event.from_user.id, event.from_user.username or "", event.from_user.full_name or "")
            db.update_user_activity(event.from_user.id)
            
            if event.text and event.text.startswith('/'):
                cmd = event.text.split()[0].replace('/', '')
                db.increment_command(cmd)
            
            # Отправляем сообщение о переезде
            custom = db.get_custom_message()
            button = InlineKeyboardButton(text=custom.get('button_text', '🚀 Перейти'), url=custom.get('button_url', f'https://t.me/{NEW_BOT_USERNAME}'))
            builder = InlineKeyboardBuilder()
            builder.add(button)
            
            if custom.get('photo_id'):
                if custom.get('photo_type') == 'photo':
                    await event.reply_photo(custom['photo_id'], caption=custom.get('text', ''), reply_markup=builder.as_markup())
                elif custom.get('photo_type') == 'video':
                    await event.reply_video(custom['photo_id'], caption=custom.get('text', ''), reply_markup=builder.as_markup())
                elif custom.get('photo_type') == 'animation':
                    await event.reply_animation(custom['photo_id'], caption=custom.get('text', ''), reply_markup=builder.as_markup())
            else:
                await event.reply(custom.get('text', f'🤖 Бот переехал!\n\nНовый бот: @{NEW_BOT_USERNAME}'), reply_markup=builder.as_markup())
            return
        return await handler(event, data)

# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard(is_admin: bool = False):
    builder = InlineKeyboardBuilder()
    if is_admin:
        builder.add(InlineKeyboardButton(text="👑 Админ панель", callback_data="admin_panel"))
        builder.add(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.add(InlineKeyboardButton(text="ℹ️ О боте", callback_data="about"))
    builder.adjust(1)
    return builder.as_markup()

def get_admin_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))
    builder.add(InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users"))
    builder.add(InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"))
    builder.add(InlineKeyboardButton(text="🎨 Кастомизация сообщения", callback_data="admin_custom"))
    builder.add(InlineKeyboardButton(text="📦 Бэкап БД", callback_data="admin_backup"))
    builder.add(InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_main"))
    builder.adjust(2)
    return builder.as_markup()

def get_custom_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📝 Изменить текст", callback_data="custom_text"))
    builder.add(InlineKeyboardButton(text="🖼 Изменить фото/видео/GIF", callback_data="custom_photo"))
    builder.add(InlineKeyboardButton(text="🔘 Изменить кнопку", callback_data="custom_button"))
    builder.add(InlineKeyboardButton(text="🔄 Сбросить", callback_data="custom_reset"))
    builder.add(InlineKeyboardButton(text="👁 Просмотр", callback_data="custom_preview"))
    builder.add(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel"))
    builder.adjust(2)
    return builder.as_markup()

def get_broadcast_keyboard():
    builder = InlineKeyboardBuilder()
    builder.add(InlineKeyboardButton(text="📱 Только в ЛС", callback_data="broadcast_pm"))
    builder.add(InlineKeyboardButton(text="🌍 Только в группы", callback_data="broadcast_groups"))
    builder.add(InlineKeyboardButton(text="🌐 В ЛС и группы", callback_data="broadcast_all"))
    builder.add(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_panel"))
    builder.adjust(2)
    return builder.as_markup()

# ==================== ОСНОВНЫЕ ХЕНДЛЕРЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    db.add_user(message.from_user.id, message.from_user.username or "", message.from_user.full_name or "")
    is_admin = message.from_user.id in ADMIN_IDS
    await message.answer(
        f"👋 Привет, {message.from_user.full_name}!\n\n"
        f"Этот бот больше не используется.\n"
        f"Пожалуйста, перейдите в нового бота по кнопке ниже 👇",
        reply_markup=get_main_keyboard(is_admin)
    )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        return
    await message.answer("👑 Админ панель", reply_markup=get_admin_keyboard())

@dp.message()
async def handle_any_message(message: Message):
    # Это сообщение уже обрабатывается middleware, но оставляем на случай
    pass

# ==================== CALLBACK ХЕНДЛЕРЫ ====================
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    is_admin = callback.from_user.id in ADMIN_IDS
    await callback.message.edit_text(
        "👋 Главное меню",
        reply_markup=get_main_keyboard(is_admin)
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_panel")
async def admin_panel(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.message.edit_text(
        "👑 Админ панель\n\nВыберите действие:",
        reply_markup=get_admin_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    stats = db.get_stats()
    text = f"📊 Статистика бота\n\n"
    text += f"👥 Всего пользователей: {stats['users']}\n"
    text += f"📅 Активны за 24ч: {stats['active_today']}\n\n"
    text += f"📈 Статистика команд:\n"
    for cmd, count in stats['commands'][:10]:
        text += f"• /{cmd}: {count}\n"
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
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
    
    text = "👥 Последние пользователи:\n\n"
    for uid, username, full_name, last_active in users[:20]:
        date = datetime.fromtimestamp(last_active).strftime("%Y-%m-%d %H:%M")
        name = full_name or username or str(uid)
        text += f"• {name}\n  ID: {uid} | {date}\n\n"
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "admin_broadcast")
async def admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.message.edit_text(
        "📢 Рассылка\n\nВыберите получателей:",
        reply_markup=get_broadcast_keyboard()
    )
    await state.set_state(BroadcastStates.waiting_for_target)
    await callback.answer()

@dp.callback_query(F.data.startswith("broadcast_"))
async def broadcast_target(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    target = callback.data.replace("broadcast_", "")
    await state.update_data(broadcast_target=target)
    await callback.message.edit_text(
        "📝 Отправьте текст или медиа для рассылки.\n\n"
        "Поддерживается: текст, фото, видео, GIF, стикер",
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
    
    # Сохраняем медиа
    media_id = None
    media_type = None
    caption = message.caption or ""
    text = message.text or caption
    
    if message.photo:
        media_id = message.photo[-1].file_id
        media_type = 'photo'
    elif message.video:
        media_id = message.video.file_id
        media_type = 'video'
    elif message.animation:
        media_id = message.animation.file_id
        media_type = 'animation'
    elif message.sticker:
        media_id = message.sticker.file_id
        media_type = 'sticker'
    
    # Получаем список получателей
    users = []
    if target in ['pm', 'all']:
        with db.get_connection() as conn:
            users = [row[0] for row in conn.execute('SELECT user_id FROM users').fetchall()]
    
    groups = []
    if target in ['groups', 'all']:
        # Получаем группы где есть бот
        async for chat in bot.get_updates():
            pass  # Это сложно, проще получить из базы если сохраняли
        # Для простоты - только ЛС рассылка работает полноценно
    
    all_targets = list(set(users))
    
    if not all_targets:
        await message.answer("❌ Нет получателей для рассылки!")
        await state.clear()
        return
    
    sent, failed = 0, 0
    errors = []
    status_msg = await message.answer(f"📤 Начинаю рассылку...\nВсего: {len(all_targets)}")
    
    for user_id in all_targets:
        try:
            if media_id:
                if media_type == 'photo':
                    await bot.send_photo(user_id, media_id, caption=text or None)
                elif media_type == 'video':
                    await bot.send_video(user_id, media_id, caption=text or None)
                elif media_type == 'animation':
                    await bot.send_animation(user_id, media_id, caption=text or None)
                elif media_type == 'sticker':
                    await bot.send_sticker(user_id, media_id)
            else:
                await bot.send_message(user_id, text)
            sent += 1
        except TelegramForbiddenError:
            failed += 1
            errors.append(f"❌ {user_id}: Бот заблокирован")
        except Exception as e:
            failed += 1
            errors.append(f"❌ {user_id}: {str(e)[:50]}")
        
        if (sent + failed) % 10 == 0:
            await status_msg.edit_text(f"📤 Прогресс: {sent + failed}/{len(all_targets)}\n✅ {sent}\n❌ {failed}")
        await asyncio.sleep(0.05)
    
    await status_msg.edit_text(f"✅ Рассылка завершена!\n✅ {sent}\n❌ {failed}")
    if errors:
        await message.answer("\n".join(errors[:10]))
    await state.clear()

@dp.callback_query(F.data == "admin_custom")
async def admin_custom(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.message.edit_text(
        "🎨 Кастомизация сообщения о переезде\n\n"
        "Вы можете изменить текст, фото/видео/GIF и кнопку.",
        reply_markup=get_custom_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "custom_text")
async def custom_text(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    current = db.get_custom_message()
    await callback.message.edit_text(
        f"📝 Текущий текст:\n{current.get('text', 'Не установлен')}\n\n"
        f"Отправьте новый текст для сообщения.\n\n"
        f"Поддерживается HTML форматирование.",
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_custom")).as_markup()
    )
    await state.set_state(CustomMessageStates.waiting_for_text)
    await callback.answer()

@dp.message(CustomMessageStates.waiting_for_text)
async def process_custom_text(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        await state.clear()
        return
    db.update_custom_message(text=message.html_text)
    await message.answer("✅ Текст сохранён!")
    await state.clear()

@dp.callback_query(F.data == "custom_photo")
async def custom_photo(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    await callback.message.edit_text(
        "🖼 Отправьте фото, видео или GIF для сообщения.\n\n"
        "Отправьте /skip чтобы удалить текущее медиа.",
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_custom")).as_markup()
    )
    await state.set_state(CustomMessageStates.waiting_for_photo)
    await callback.answer()

@dp.message(CustomMessageStates.waiting_for_photo, F.photo | F.video | F.animation)
async def process_custom_photo(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        await state.clear()
        return
    
    if message.photo:
        media_id = message.photo[-1].file_id
        media_type = 'photo'
    elif message.video:
        media_id = message.video.file_id
        media_type = 'video'
    elif message.animation:
        media_id = message.animation.file_id
        media_type = 'animation'
    else:
        await message.answer("❌ Отправьте фото, видео или GIF!")
        return
    
    db.update_custom_message(photo_id=media_id, photo_type=media_type)
    await message.answer("✅ Медиа сохранено!")
    await state.clear()

@dp.message(CustomMessageStates.waiting_for_photo, F.text == "/skip")
async def skip_photo(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        await state.clear()
        return
    db.update_custom_message(photo_id=None, photo_type=None)
    await message.answer("✅ Медиа удалено!")
    await state.clear()

@dp.message(CustomMessageStates.waiting_for_photo)
async def invalid_photo(message: Message, state: FSMContext):
    await message.answer("❌ Отправьте фото, видео, GIF или /skip")

@dp.callback_query(F.data == "custom_button")
async def custom_button(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    current = db.get_custom_message()
    await callback.message.edit_text(
        f"🔘 Текущая кнопка:\nТекст: {current.get('button_text', '🚀 Перейти')}\nСсылка: {current.get('button_url', f'https://t.me/{NEW_BOT_USERNAME}')}\n\n"
        f"Отправьте новый текст для кнопки:",
        reply_markup=InlineKeyboardBuilder().add(InlineKeyboardButton(text="◀️ Назад", callback_data="admin_custom")).as_markup()
    )
    await state.set_state(CustomMessageStates.waiting_for_button_text)
    await callback.answer()

@dp.message(CustomMessageStates.waiting_for_button_text)
async def process_button_text(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        await state.clear()
        return
    await state.update_data(button_text=message.text)
    await message.answer(
        "📎 Отправьте ссылку для кнопки (https://t.me/... или любая другая):\n\n"
        "Отправьте /skip чтобы оставить текущую"
    )
    await state.set_state(CustomMessageStates.waiting_for_button_url)

@dp.message(CustomMessageStates.waiting_for_button_url)
async def process_button_url(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Доступ запрещён!")
        await state.clear()
        return
    data = await state.get_data()
    button_text = data.get('button_text')
    
    if message.text == '/skip':
        current = db.get_custom_message()
        button_url = current.get('button_url', f'https://t.me/{NEW_BOT_USERNAME}')
    else:
        button_url = message.text.strip()
    
    db.update_custom_message(button_text=button_text, button_url=button_url)
    await message.answer(f"✅ Кнопка сохранена!\n\nТекст: {button_text}\nСсылка: {button_url}")
    await state.clear()

@dp.callback_query(F.data == "custom_reset")
async def custom_reset(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    db.reset_custom_message()
    await callback.message.edit_text(
        "✅ Сообщение сброшено к стандартному!",
        reply_markup=get_custom_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "custom_preview")
async def custom_preview(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    custom = db.get_custom_message()
    button = InlineKeyboardButton(text=custom.get('button_text', '🚀 Перейти'), url=custom.get('button_url', f'https://t.me/{NEW_BOT_USERNAME}'))
    builder = InlineKeyboardBuilder()
    builder.add(button)
    
    if custom.get('photo_id'):
        if custom.get('photo_type') == 'photo':
            await callback.message.answer_photo(custom['photo_id'], caption=custom.get('text', ''), reply_markup=builder.as_markup())
        elif custom.get('photo_type') == 'video':
            await callback.message.answer_video(custom['photo_id'], caption=custom.get('text', ''), reply_markup=builder.as_markup())
        elif custom.get('photo_type') == 'animation':
            await callback.message.answer_animation(custom['photo_id'], caption=custom.get('text', ''), reply_markup=builder.as_markup())
    else:
        await callback.message.answer(custom.get('text', f'🤖 Бот переехал!\n\nНовый бот: @{NEW_BOT_USERNAME}'), reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data == "admin_backup")
async def admin_backup(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("❌ Доступ запрещён!", show_alert=True)
        return
    try:
        backup_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        shutil.copy2("redirect_bot.db", backup_name)
        await callback.message.answer_document(FSInputFile(backup_name), caption=f"✅ Бэкап создан: {backup_name}")
        os.remove(backup_name)
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

@dp.callback_query(F.data == "about")
async def about(callback: CallbackQuery):
    await callback.message.edit_text(
        f"🤖 Бот-переходник\n\n"
        f"Этот бот больше не используется.\n"
        f"Новый бот: @{NEW_BOT_USERNAME}\n\n"
        f"Все функции модерации и управления группами доступны в новом боте.",
        reply_markup=get_main_keyboard(callback.from_user.id in ADMIN_IDS)
    )
    await callback.answer()

# ==================== ЗАПУСК ====================
async def main():
    dp.message.middleware(RedirectMiddleware())
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
