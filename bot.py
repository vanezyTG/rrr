import asyncio
import logging
import time
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import *
from aiogram.filters import CommandStart, Command

TOKEN = "8533732699:AAFi654Hr34MSQIA7chQUG2Jd3aOhs-TBAc"
ADMINS = [6708209142]

bot = Bot(TOKEN)
dp = Dispatcher()

logging.basicConfig(level=logging.INFO)

flood = {}
media_cd = {}

# ================= DATABASE =================

async def init_db():

    async with aiosqlite.connect("support.db") as db:

        await db.execute("""
        CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY,
        name TEXT
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS tickets(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        operator INTEGER,
        status TEXT,
        created INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS bans(
        user_id INTEGER
        )
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS ratings(
        ticket INTEGER,
        rate INTEGER
        )
        """)

        await db.commit()

# ================= ANTI FLOOD =================

def anti_flood(user, cooldown=3):

    now = time.time()

    if user in flood:

        if now - flood[user] < cooldown:
            return False

    flood[user] = now
    return True


def media_cooldown(user):

    now = time.time()

    if user in media_cd:

        if now - media_cd[user] < 8:
            return False

    media_cd[user] = now
    return True

# ================= KEYBOARDS =================

def user_menu():

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [InlineKeyboardButton(
                text="📩 Написать поддержку",
                callback_data="support"
            )],

            [InlineKeyboardButton(
                text="🎫 Мои тикеты",
                callback_data="mytickets"
            )]

        ]
    )


def ticket_admin(ticket, user):

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [InlineKeyboardButton(
                text="💬 Ответить",
                callback_data=f"reply_{user}"
            )],

            [InlineKeyboardButton(
                text="👤 Взять тикет",
                callback_data=f"take_{ticket}"
            )],

            [InlineKeyboardButton(
                text="❌ Закрыть",
                callback_data=f"close_{ticket}"
            )],

            [InlineKeyboardButton(
                text="🚫 Бан",
                callback_data=f"ban_{user}"
            )]

        ]
    )


def rating_keyboard(ticket):

    return InlineKeyboardMarkup(
        inline_keyboard=[

            [
                InlineKeyboardButton(text="⭐", callback_data=f"rate_{ticket}_1"),
                InlineKeyboardButton(text="⭐⭐", callback_data=f"rate_{ticket}_2"),
                InlineKeyboardButton(text="⭐⭐⭐", callback_data=f"rate_{ticket}_3"),
                InlineKeyboardButton(text="⭐⭐⭐⭐", callback_data=f"rate_{ticket}_4"),
                InlineKeyboardButton(text="⭐⭐⭐⭐⭐", callback_data=f"rate_{ticket}_5")
            ]

        ]
    )

# ================= START =================

@dp.message(CommandStart())
async def start(message: Message):

    if message.chat.type != "private":
        return

    async with aiosqlite.connect("support.db") as db:

        await db.execute(
            "INSERT OR IGNORE INTO users VALUES(?,?)",
            (message.from_user.id, message.from_user.full_name)
        )

        await db.commit()

    await message.answer(
        "👋 Добро пожаловать в поддержку",
        reply_markup=user_menu()
    )

# ================= SUPPORT BUTTON =================

@dp.callback_query(F.data == "support")
async def support(callback: CallbackQuery):

    if callback.message.chat.type != "private":
        return

    await callback.message.answer(
        "✉️ Напишите ваше сообщение."
    )

# ================= USER MESSAGE =================

@dp.message(F.chat.type == "private", F.from_user.id.not_in(ADMINS))
async def user_message(message: Message):

    user = message.from_user.id

    if not anti_flood(user):

        await message.answer("⏳ Подождите пару секунд.")
        return

    if message.photo or message.video or message.document:

        if not media_cooldown(user):

            await message.answer("📎 Медиа можно отправлять раз в 8 секунд")
            return

    async with aiosqlite.connect("support.db") as db:

        cursor = await db.execute(
            "SELECT * FROM bans WHERE user_id=?",
            (user,)
        )

        if await cursor.fetchone():

            await message.answer("🚫 Вы заблокированы")
            return

        cursor = await db.execute(
            "SELECT COUNT(*) FROM tickets WHERE user_id=? AND status='open'",
            (user,)
        )

        active = (await cursor.fetchone())[0]

        if active >= 3:

            await message.answer("❗ У вас уже есть 3 активных тикета")
            return

        cursor = await db.execute(
            "INSERT INTO tickets(user_id,status,created) VALUES(?,?,?)",
            (user, "open", int(time.time()))
        )

        ticket = cursor.lastrowid

        await db.commit()

    for admin in ADMINS:

        await bot.forward_message(
            admin,
            message.chat.id,
            message.message_id
        )

        await bot.send_message(
            admin,
            f"""
🎫 Тикет #{ticket}

👤 {message.from_user.full_name}
ID: {user}
""",
            reply_markup=ticket_admin(ticket, user)
        )

    await message.answer("✅ Сообщение отправлено в поддержку")

# ================= ADMIN REPLY =================

@dp.callback_query(F.data.startswith("reply_"))
async def reply(callback: CallbackQuery):

    user = int(callback.data.split("_")[1])

    await callback.message.answer(
        f"Ответьте на это сообщение\nID:{user}"
    )


@dp.message(F.from_user.id.in_(ADMINS))
async def admin_message(message: Message):

    if not message.reply_to_message:
        return

    if "ID:" not in message.reply_to_message.text:
        return

    user = int(
        message.reply_to_message.text.split("ID:")[1]
    )

    try:

        await bot.forward_message(
            user,
            message.chat.id,
            message.message_id
        )

        await message.answer("Ответ отправлен")

    except:

        await message.answer("Ошибка отправки")

# ================= TAKE =================

@dp.callback_query(F.data.startswith("take_"))
async def take_ticket(callback: CallbackQuery):

    ticket = int(callback.data.split("_")[1])

    async with aiosqlite.connect("support.db") as db:

        await db.execute(
            "UPDATE tickets SET operator=? WHERE id=?",
            (callback.from_user.id, ticket)
        )

        await db.commit()

    await callback.answer("Вы взяли тикет")

# ================= CLOSE =================

@dp.callback_query(F.data.startswith("close_"))
async def close_ticket(callback: CallbackQuery):

    ticket = int(callback.data.split("_")[1])

    async with aiosqlite.connect("support.db") as db:

        cursor = await db.execute(
            "SELECT user_id FROM tickets WHERE id=?",
            (ticket,)
        )

        row = await cursor.fetchone()

        if not row:
            return

        user = row[0]

        await db.execute(
            "UPDATE tickets SET status='closed' WHERE id=?",
            (ticket,)
        )

        await db.commit()

    await bot.send_message(
        user,
        "❌ Тикет закрыт",
        reply_markup=rating_keyboard(ticket)
    )

    await callback.message.edit_text("Тикет закрыт")

# ================= RATING =================

@dp.callback_query(F.data.startswith("rate_"))
async def rating(callback: CallbackQuery):

    data = callback.data.split("_")

    ticket = int(data[1])
    rate = int(data[2])

    async with aiosqlite.connect("support.db") as db:

        await db.execute(
            "INSERT INTO ratings VALUES(?,?)",
            (ticket, rate)
        )

        await db.commit()

    await callback.message.edit_text("Спасибо за оценку")

# ================= BAN =================

@dp.callback_query(F.data.startswith("ban_"))
async def ban(callback: CallbackQuery):

    user = int(callback.data.split("_")[1])

    async with aiosqlite.connect("support.db") as db:

        await db.execute(
            "INSERT INTO bans VALUES(?)",
            (user,)
        )

        await db.commit()

    await callback.answer("Пользователь забанен")

# ================= ADMIN COMMANDS =================

@dp.message(Command("stats"))
async def stats(message: Message):

    if message.from_user.id not in ADMINS:
        return

    async with aiosqlite.connect("support.db") as db:

        cursor = await db.execute("SELECT COUNT(*) FROM tickets")
        tickets = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM users")
        users = (await cursor.fetchone())[0]

    await message.answer(
        f"""
📊 Статистика

Пользователей: {users}
Тикетов: {tickets}
"""
    )

# ================= RUN =================

async def main():

    await init_db()

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
