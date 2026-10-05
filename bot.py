#%%
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ============================================================
# НАСТРОЙКИ
# ============================================================
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

PREMIUM_PRICE = 199
PREMIUM_DAYS = 30

DB_NAME = "anonymous_bot.db"


# ============================================================
# БАЗА ДАННЫХ
# ============================================================

db = sqlite3.connect(DB_NAME)
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    gender TEXT,
    premium_until TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    currency TEXT NOT NULL,
    payload TEXT NOT NULL,
    paid_at TEXT NOT NULL
)
""")

db.commit()


def create_user(user_id: int):
    db.execute(
        "INSERT OR IGNORE INTO users (user_id) VALUES (?)",
        (user_id,)
    )
    db.commit()


def get_user(user_id: int):
    return db.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()


def get_gender(user_id: int):
    user = get_user(user_id)
    return user["gender"] if user else None


def set_gender(user_id: int, gender: str):
    db.execute(
        "UPDATE users SET gender = ? WHERE user_id = ?",
        (gender, user_id)
    )
    db.commit()


def get_premium_until(user_id: int):
    user = get_user(user_id)

    if not user or not user["premium_until"]:
        return None

    try:
        return datetime.fromisoformat(user["premium_until"])
    except ValueError:
        return None


def premium_active(user_id: int):
    until = get_premium_until(user_id)

    if until is None:
        return False

    return until > datetime.now(timezone.utc)


def activate_premium(user_id: int):
    now = datetime.now(timezone.utc)
    current_until = get_premium_until(user_id)

    if current_until and current_until > now:
        new_until = current_until + timedelta(days=PREMIUM_DAYS)
    else:
        new_until = now + timedelta(days=PREMIUM_DAYS)

    db.execute(
        """
        UPDATE users
        SET premium_until = ?
        WHERE user_id = ?
        """,
        (new_until.isoformat(), user_id)
    )

    db.commit()

    return new_until


def payment_exists(charge_id: str):
    row = db.execute(
        "SELECT charge_id FROM payments WHERE charge_id = ?",
        (charge_id,)
    ).fetchone()

    return row is not None


def save_payment(
    charge_id: str,
    user_id: int,
    amount: int,
    currency: str,
    payload: str
):
    db.execute(
        """
        INSERT INTO payments
        (charge_id, user_id, amount, currency, payload, paid_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            charge_id,
            user_id,
            amount,
            currency,
            payload,
            datetime.now(timezone.utc).isoformat()
        )
    )

    db.commit()


# ============================================================
# СОСТОЯНИЯ
# ============================================================

class Registration(StatesGroup):
    gender = State()


# ============================================================
# ПОИСК
# ============================================================

# user_id -> wanted gender
search_queue = {}

# user_id -> partner_id
partners = {}


def remove_from_queue(user_id: int):
    search_queue.pop(user_id, None)


def end_dialog(user_id: int):
    partner_id = partners.pop(user_id, None)

    if partner_id is not None:
        partners.pop(partner_id, None)

    return partner_id


def find_partner(user_id: int, wanted_gender: str):
    my_gender = get_gender(user_id)

    for other_id, other_wanted_gender in list(search_queue.items()):

        if other_id == user_id:
            continue

        other_gender = get_gender(other_id)

        # Подходит ли другой пользователь мне?
        if wanted_gender != "any":
            if other_gender != wanted_gender:
                continue

        # Подхожу ли я другому пользователю?
        if other_wanted_gender != "any":
            if my_gender != other_wanted_gender:
                continue

        remove_from_queue(user_id)
        remove_from_queue(other_id)

        partners[user_id] = other_id
        partners[other_id] = user_id

        return other_id

    return None


# ============================================================
# КЛАВИАТУРЫ
# ============================================================

def main_keyboard(user_id: int):
    buttons = [
        [
            InlineKeyboardButton(
                text="🔎 Найти собеседника",
                callback_data="search_any"
            )
        ],
        [
            InlineKeyboardButton(
                text="👤 Мой профиль",
                callback_data="profile"
            )
        ],
        [
            InlineKeyboardButton(
                text="⭐ Premium",
                callback_data="premium"
            )
        ]
    ]

    if premium_active(user_id):
        buttons.insert(
            1,
            [
                InlineKeyboardButton(
                    text="⭐ 🔎 Поиск по полу",
                    callback_data="search_gender"
                )
            ]
        )

    return InlineKeyboardMarkup(inline_keyboard=buttons)


gender_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👨 Парень",
                callback_data="gender_male"
            ),
            InlineKeyboardButton(
                text="👩 Девушка",
                callback_data="gender_female"
            )
        ]
    ]
)


wanted_gender_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👨 Искать парня",
                callback_data="wanted_male"
            )
        ],
        [
            InlineKeyboardButton(
                text="👩 Искать девушку",
                callback_data="wanted_female"
            )
        ],
        [
            InlineKeyboardButton(
                text="🎲 Искать любого",
                callback_data="wanted_any"
            )
        ],
        [
            InlineKeyboardButton(
                text="◀️ Назад",
                callback_data="back_menu"
            )
        ]
    ]
)


stop_search_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(
                text="⛔ Отменить поиск",
                callback_data="stop_search"
            )
        ]
    ]
)


# ============================================================
# BOT
# ============================================================

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML
    )
)

dp = Dispatcher()


# ============================================================
# START
# ============================================================

@dp.message(CommandStart())
async def start(message: Message, state: FSMContext):
    user_id = message.from_user.id

    create_user(user_id)

    if not get_gender(user_id):
        await message.answer(
            "👋 <b>Добро пожаловать в анонимку!</b>\n\n"
            "Для начала выбери свой пол:",
            reply_markup=gender_keyboard
        )

        await state.set_state(Registration.gender)
        return

    await message.answer(
        "🕵️ <b>Анонимка</b>\n\n"
        "Здесь ты можешь найти случайного собеседника.",
        reply_markup=main_keyboard(user_id)
    )


# ============================================================
# ВЫБОР ПОЛА
# ============================================================

@dp.callback_query(F.data.startswith("gender_"))
async def choose_gender(
    callback: CallbackQuery,
    state: FSMContext
):
    user_id = callback.from_user.id

    create_user(user_id)

    gender = callback.data.replace("gender_", "", 1)

    if gender not in ("male", "female"):
        await callback.answer(
            "Ошибка выбора пола.",
            show_alert=True
        )
        return

    set_gender(user_id, gender)

    await state.clear()

    await callback.message.edit_text(
        "✅ <b>Профиль создан!</b>\n\n"
        "Теперь можешь искать собеседника.",
        reply_markup=main_keyboard(user_id)
    )

    await callback.answer()


# ============================================================
# ОБЫЧНЫЙ ПОИСК
# ============================================================

@dp.callback_query(F.data == "search_any")
async def search_any(callback: CallbackQuery):
    user_id = callback.from_user.id

    if not get_gender(user_id):
        await callback.answer(
            "Сначала выбери свой пол.",
            show_alert=True
        )
        return

    if user_id in partners:
        await callback.answer(
            "Ты уже общаешься с собеседником.",
            show_alert=True
        )
        return

    if user_id in search_queue:
        await callback.answer(
            "Ты уже находишься в поиске.",
            show_alert=True
        )
        return

    search_queue[user_id] = "any"

    partner_id = find_partner(user_id, "any")

    if partner_id:
        await callback.message.edit_text(
            "🎉 <b>Собеседник найден!</b>\n\n"
            "Можешь отправлять сообщения.\n"
            "Для завершения диалога нажми /stop"
        )

        try:
            await bot.send_message(
                partner_id,
                "🎉 <b>Собеседник найден!</b>\n\n"
                "Можешь отправлять сообщения.\n"
                "Для завершения диалога нажми /stop"
            )
        except Exception:
            end_dialog(user_id)

            await callback.message.edit_text(
                "❌ Не удалось связаться с собеседником.",
                reply_markup=main_keyboard(user_id)
            )

    else:
        await callback.message.edit_text(
            "🔎 <b>Ищу собеседника...</b>\n\n"
            "Подожди, пока кто-нибудь подключится.",
            reply_markup=stop_search_keyboard
        )

    await callback.answer()


# ============================================================
# PREMIUM: ПОИСК ПО ПОЛУ
# ============================================================

@dp.callback_query(F.data == "search_gender")
async def search_gender(callback: CallbackQuery):
    user_id = callback.from_user.id

    if not premium_active(user_id):
        await callback.answer(
            "⭐ Функция доступна только Premium.",
            show_alert=True
        )
        return

    if user_id in partners:
        await callback.answer(
            "Сначала заверши текущий диалог.",
            show_alert=True
        )
        return

    if user_id in search_queue:
        await callback.answer(
            "Сначала останови текущий поиск.",
            show_alert=True
        )
        return

    await callback.message.edit_text(
        "⭐ <b>Поиск по полу</b>\n\n"
        "Кого хочешь найти?",
        reply_markup=wanted_gender_keyboard
    )

    await callback.answer()


@dp.callback_query(F.data.startswith("wanted_"))
async def wanted_gender(callback: CallbackQuery):
    user_id = callback.from_user.id

    if not premium_active(user_id):
        await callback.answer(
            "⭐ Premium закончился.",
            show_alert=True
        )
        return

    if user_id in partners:
        await callback.answer(
            "Ты уже общаешься с собеседником.",
            show_alert=True
        )
        return

    wanted = callback.data.replace("wanted_", "", 1)

    if wanted not in ("male", "female", "any"):
        await callback.answer(
            "Ошибка.",
            show_alert=True
        )
        return

    search_queue[user_id] = wanted

    partner_id = find_partner(user_id, wanted)

    if partner_id:
        await callback.message.edit_text(
            "🎉 <b>Собеседник найден!</b>\n\n"
            "Можешь начинать общение.\n"
            "Для завершения — /stop"
        )

        try:
            await bot.send_message(
                partner_id,
                "🎉 <b>Собеседник найден!</b>\n\n"
                "Можешь начинать общение.\n"
                "Для завершения — /stop"
            )
        except Exception:
            end_dialog(user_id)

            await callback.message.edit_text(
                "❌ Не удалось связаться с собеседником.",
                reply_markup=main_keyboard(user_id)
            )

    else:
        if wanted == "male":
            text = "👨 Ищу парня..."
        elif wanted == "female":
            text = "👩 Ищу девушку..."
        else:
            text = "🎲 Ищу любого собеседника..."

        await callback.message.edit_text(
            f"🔎 <b>{text}</b>\n\n"
            "Подожди, пока найдётся подходящий собеседник.",
            reply_markup=stop_search_keyboard
        )

    await callback.answer()


# ============================================================
# ОСТАНОВКА ПОИСКА / ДИАЛОГА
# ============================================================

@dp.callback_query(F.data == "stop_search")
async def stop_search(callback: CallbackQuery):
    user_id = callback.from_user.id

    remove_from_queue(user_id)

    partner_id = end_dialog(user_id)

    if partner_id:
        try:
            await bot.send_message(
                partner_id,
                "❌ Собеседник завершил диалог.",
                reply_markup=main_keyboard(partner_id)
            )
        except Exception:
            pass

        await callback.message.edit_text(
            "❌ <b>Диалог завершён.</b>",
            reply_markup=main_keyboard(user_id)
        )

    else:
        await callback.message.edit_text(
            "⛔ <b>Поиск остановлен.</b>",
            reply_markup=main_keyboard(user_id)
        )

    await callback.answer()


@dp.message(Command("stop"))
async def stop_command(message: Message):
    user_id = message.from_user.id

    remove_from_queue(user_id)

    partner_id = end_dialog(user_id)

    if partner_id:
        try:
            await bot.send_message(
                partner_id,
                "❌ Собеседник завершил диалог.",
                reply_markup=main_keyboard(partner_id)
            )
        except Exception:
            pass

        await message.answer(
            "❌ <b>Диалог завершён.</b>",
            reply_markup=main_keyboard(user_id)
        )

    else:
        await message.answer(
            "⛔ <b>Поиск остановлен.</b>",
            reply_markup=main_keyboard(user_id)
        )


# ============================================================
# ПРОФИЛЬ
# ============================================================

@dp.callback_query(F.data == "profile")
async def profile(callback: CallbackQuery):
    user_id = callback.from_user.id

    gender = get_gender(user_id)

    if gender == "male":
        gender_text = "👨 Парень"
    elif gender == "female":
        gender_text = "👩 Девушка"
    else:
        gender_text = "Не выбран"

    until = get_premium_until(user_id)

    if until and until > datetime.now(timezone.utc):
        premium_text = (
            "⭐ <b>Premium активен</b>\n"
            f"До: {until.strftime('%d.%m.%Y %H:%M')} UTC"
        )
    else:
        premium_text = "❌ <b>Premium не активен</b>"

    await callback.message.edit_text(
        "👤 <b>Мой профиль</b>\n\n"
        f"Пол: {gender_text}\n\n"
        f"{premium_text}",
        reply_markup=main_keyboard(user_id)
    )

    await callback.answer()


# ============================================================
# PREMIUM
# ============================================================

@dp.callback_query(F.data == "premium")
async def premium_menu(callback: CallbackQuery):
    user_id = callback.from_user.id

    until = get_premium_until(user_id)

    if until and until > datetime.now(timezone.utc):
        await callback.message.edit_text(
            "⭐ <b>Premium активен</b>\n\n"
            f"Действует до:\n"
            f"{until.strftime('%d.%m.%Y %H:%M')} UTC\n\n"
            "Доступная функция:\n"
            "🔎 Поиск собеседника по полу.",
            reply_markup=main_keyboard(user_id)
        )

        await callback.answer()
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⭐ Купить за 199 Stars",
                    callback_data="buy_premium"
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data="back_menu"
                )
            ]
        ]
    )

    await callback.message.edit_text(
        "⭐ <b>Premium</b>\n\n"
        "Цена: <b>199 ⭐</b>\n"
        "Срок: <b>30 дней</b>\n\n"
        "Premium открывает:\n"
        "🔎 поиск собеседника по полу.",
        reply_markup=keyboard
    )

    await callback.answer()


# ============================================================
# СОЗДАНИЕ СЧЁТА
# ============================================================

@dp.callback_query(F.data == "buy_premium")
async def buy_premium(callback: CallbackQuery):
    user_id = callback.from_user.id

    if premium_active(user_id):
        await callback.answer(
            "Premium уже активен.",
            show_alert=True
        )
        return

    payload = f"premium30:{user_id}"

    await bot.send_invoice(
        chat_id=user_id,
        title="Premium на 30 дней",
        description="Поиск собеседника по полу в анонимке",
        payload=payload,
        currency="XTR",
        prices=[
            LabeledPrice(
                label="Premium — 30 дней",
                amount=PREMIUM_PRICE
            )
        ],
        provider_token=""
    )

    await callback.answer()


# ============================================================
# ПРОВЕРКА ПЕРЕД ОПЛАТОЙ
# ============================================================

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    if query.currency != "XTR":
        await query.answer(
            ok=False,
            error_message="Неверная валюта платежа."
        )
        return

    if query.total_amount != PREMIUM_PRICE:
        await query.answer(
            ok=False,
            error_message="Неверная сумма платежа."
        )
        return

    if not query.invoice_payload.startswith("premium30:"):
        await query.answer(
            ok=False,
            error_message="Неверный товар."
        )
        return

    try:
        payload_user_id = int(
            query.invoice_payload.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        await query.answer(
            ok=False,
            error_message="Некорректный платёж."
        )
        return

    if payload_user_id != query.from_user.id:
        await query.answer(
            ok=False,
            error_message="Платёж привязан к другому пользователю."
        )
        return

    await query.answer(ok=True)


# ============================================================
# УСПЕШНАЯ ОПЛАТА
# ============================================================

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    user_id = message.from_user.id
    payment = message.successful_payment

    if payment.currency != "XTR":
        return

    if payment.total_amount != PREMIUM_PRICE:
        return

    if not payment.invoice_payload.startswith("premium30:"):
        return

    try:
        payload_user_id = int(
            payment.invoice_payload.split(":", 1)[1]
        )
    except (ValueError, IndexError):
        return

    if payload_user_id != user_id:
        return

    charge_id = payment.telegram_payment_charge_id

    # Защита от повторной обработки одного платежа
    if payment_exists(charge_id):
        return

    save_payment(
        charge_id=charge_id,
        user_id=user_id,
        amount=payment.total_amount,
        currency=payment.currency,
        payload=payment.invoice_payload
    )

    until = activate_premium(user_id)

    await message.answer(
        "🎉 <b>Оплата успешно получена!</b>\n\n"
        "⭐ Premium активирован.\n"
        "🔎 Теперь доступен поиск по полу.\n\n"
        f"⏳ Premium действует до:\n"
        f"{until.strftime('%d.%m.%Y %H:%M')} UTC",
        reply_markup=main_keyboard(user_id)
    )


# ============================================================
# НАЗАД В МЕНЮ
# ============================================================

@dp.callback_query(F.data == "back_menu")
async def back_menu(callback: CallbackQuery):
    user_id = callback.from_user.id

    await callback.message.edit_text(
        "🕵️ <b>Анонимка</b>\n\n"
        "Выбери действие:",
        reply_markup=main_keyboard(user_id)
    )

    await callback.answer()


# ============================================================
# АНОНИМНАЯ ПЕРЕДАЧА СООБЩЕНИЙ
# ============================================================

@dp.message()
async def anonymous_message(message: Message):
    user_id = message.from_user.id

    partner_id = partners.get(user_id)

    if partner_id is None:
        return

    try:
        await bot.copy_message(
            chat_id=partner_id,
            from_chat_id=user_id,
            message_id=message.message_id
        )

    except Exception:
        partners.pop(user_id, None)
        partners.pop(partner_id, None)

        await message.answer(
            "❌ Не удалось отправить сообщение.\n"
            "Диалог завершён.",
            reply_markup=main_keyboard(user_id)
        )


# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    print("🤖 Бот запускается...")

    try:
        await dp.start_polling(bot)
    finally:
        db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
