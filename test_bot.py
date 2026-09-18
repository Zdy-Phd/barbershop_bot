"""
БОТ-ЗАПИСЬ ДЛЯ БАРБЕРШОПА / САЛОНА (aiogram 3.x)
==================================================
Полный сценарий записи клиента:
  1. /start -> фото + приветствие, постоянная кнопка "Записаться" снизу
  2. Выбор услуги (нельзя выбрать услугу, на которую уже есть активная запись)
  3. Выбор мастера
  4. Выбор даты
  5. Выбор времени (занятые слоты просто не показываются)
  6. Подтверждение
  7. Номер телефона (текстом или кнопкой "Поделиться контактом")
  8. ФИО
  9. Итоговое уведомление админу одним сообщением + кнопка отмены у клиента

Запуск: pip install -r requirements.txt, затем python3 test_bot.py
Нужен .env рядом с файлом:
    BOT_TOKEN=токен_от_BotFather
    ADMIN_CHAT_ID=твой_telegram_id (узнать через @userinfobot)
"""

import asyncio
import logging
import os
from datetime import date, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from support_chat import support_router

# ---------- Настройка ----------
load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID")  # куда прилетают новые записи и отмены

router = Router()


# ---------- "База данных" (в реальном проекте заменить на SQLite/Postgres) ----------

SERVICES = {
    "haircut": {"title": "Стрижка", "price": "от 1500", "duration_min": 40},
    "beard": {"title": "Бритье", "price": "от 800", "duration_min": 20},
    "combo": {"title": "Стрижка + бритье", "price": "от 2000", "duration_min": 60},
}

MASTERS = {
    "ivan": "Иван",
    "petr": "Пётр",
    "anna": "Анна",
}

# Возможное время записи в течение дня (одинаковое для всех мастеров)
TIME_SLOTS = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00"]

# Занятые слоты: множество кортежей (мастер, дата, время)
BOOKED_SLOTS: set[tuple[str, str, str]] = set()

# Кто на какую услугу уже записан: {user_id: {"haircut", "beard", ...}}
USER_BOOKINGS: dict[int, set[str]] = {}


# ---------- Состояния диалога (FSM) ----------
class BookingFSM(StatesGroup):
    choosing_service = State()
    choosing_master = State()
    choosing_date = State()
    choosing_time = State()
    confirming = State()
    entering_phone = State()
    entering_fio = State()


# ---------- Клавиатуры ----------

def persistent_menu_kb() -> ReplyKeyboardMarkup:
    """Постоянная кнопка внизу экрана — не пропадает после нажатий."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📅 Записаться")]],
        resize_keyboard=True,
        is_persistent=True,
    )


def phone_request_kb() -> ReplyKeyboardMarkup:
    """Кнопка, которая отправляет номер телефона из профиля Telegram."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def get_main_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🛍️ Выбрать услугу", callback_data="open_services")
    return builder.as_markup()


def get_sub_menu_keyboard() -> InlineKeyboardMarkup:
    """Список услуг, сформированный прямо из словаря SERVICES."""
    builder = InlineKeyboardBuilder()
    for key, service in SERVICES.items():
        builder.button(
            text=f"{service['title']} — {service['price']} руб.",
            callback_data=f"service:{key}",
        )
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()


def masters_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, name in MASTERS.items():
        builder.button(text=name, callback_data=f"master:{key}")
    builder.button(text="⬅️ Назад к услугам", callback_data="open_services")
    builder.adjust(1)
    return builder.as_markup()


def dates_kb() -> InlineKeyboardMarkup:
    """Ближайшие 7 дней начиная с сегодняшнего."""
    builder = InlineKeyboardBuilder()
    today = date.today()
    for i in range(7):
        d = today + timedelta(days=i)
        builder.button(text=d.strftime("%d.%m (%a)"), callback_data=f"date:{d.isoformat()}")
    builder.button(text="⬅️ Назад к мастерам", callback_data="back_to_masters")
    builder.adjust(1)
    return builder.as_markup()


def times_kb(master: str, chosen_date: str) -> InlineKeyboardMarkup:
    """Занятые слоты (уже в BOOKED_SLOTS) просто не попадают в клавиатуру."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for t in TIME_SLOTS:
        busy = (master, chosen_date, t) in BOOKED_SLOTS
        if busy:
            continue

        row.append(InlineKeyboardButton(text=t, callback_data=f"time:{t}"))
        if len(row) == 4:
            rows.append(row)
            row = []

    if row:
        rows.append(row)

    if not rows:
        rows.append([InlineKeyboardButton(text="Нет свободных слотов 😔", callback_data="noop")])

    rows.append([InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="back_to_dates")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- Старт и главное меню ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(BookingFSM.choosing_service)

    photo = FSInputFile("test_photo_bot2.jpg")
    await message.answer_photo(
        photo=photo,
        caption="Привет! Это бот для записи в наш салон 💈",
        reply_markup=persistent_menu_kb(),
    )
    await message.answer(
        "Нажмите кнопку ниже, чтобы посмотреть услуги мастеров:",
        reply_markup=get_sub_menu_keyboard(),
    )


@router.message(F.text == "📅 Записаться")
async def start_booking(message: Message, state: FSMContext) -> None:
    await state.set_state(BookingFSM.choosing_service)
    await message.answer("Выберите услугу:", reply_markup=get_sub_menu_keyboard())


@router.callback_query(F.data == "open_services")
async def open_services_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BookingFSM.choosing_service)
    await callback.message.edit_text(
        text="Привет! Это бот для записи в наш салон 💈\nВыбери услугу:",
        reply_markup=get_sub_menu_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "back_to_main")
async def process_back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        text="Вы вернулись в главное меню. Выберите действие:",
        reply_markup=get_main_keyboard(),
    )
    await callback.answer()


# ---------- Шаг 1: выбор услуги ----------

@router.callback_query(BookingFSM.choosing_service, F.data.startswith("service:"))
async def choose_service(callback: CallbackQuery, state: FSMContext) -> None:
    service_key = callback.data.split(":")[1]
    user_id = callback.from_user.id

    if service_key in USER_BOOKINGS.get(user_id, set()):
        await callback.answer(
            "У вас уже есть активная запись на эту услугу. "
            "Отмените её, если хотите записаться заново.",
            show_alert=True,
        )
        return

    await state.update_data(service=service_key)
    await state.set_state(BookingFSM.choosing_master)
    await callback.message.edit_text(
        text="Отлично! Теперь выберите мастера:",
        reply_markup=masters_kb(),
    )
    await callback.answer()


# ---------- Шаг 2: выбор мастера ----------

@router.callback_query(F.data == "back_to_masters")
async def back_to_masters_step(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BookingFSM.choosing_master)
    await callback.message.edit_text(text="Вы вернулись назад. Выбери мастера:", reply_markup=masters_kb())
    await callback.answer()


@router.callback_query(BookingFSM.choosing_master, F.data.startswith("master:"))
async def choose_master(callback: CallbackQuery, state: FSMContext) -> None:
    master_key = callback.data.split(":")[1]
    await state.update_data(master=master_key)
    await state.set_state(BookingFSM.choosing_date)
    await callback.message.edit_text(text="Выберите удобную дату:", reply_markup=dates_kb())
    await callback.answer()


# ---------- Шаг 3: выбор даты ----------

@router.callback_query(BookingFSM.choosing_date, F.data.startswith("date:"))
async def choose_date(callback: CallbackQuery, state: FSMContext) -> None:
    chosen_date = callback.data.split(":")[1]
    data = await state.update_data(chosen_date=chosen_date)
    await state.set_state(BookingFSM.choosing_time)
    await callback.message.edit_text(
        "Выбери время:",
        reply_markup=times_kb(data["master"], chosen_date),
    )
    await callback.answer()


# ---------- Шаг 4: выбор времени ----------

@router.callback_query(BookingFSM.choosing_time)
async def choose_time(callback: CallbackQuery, state: FSMContext) -> None:
    """Ловит любой callback в состоянии choosing_time — в том числе кнопку
    "Назад к датам", поэтому отдельный хендлер для неё не нужен."""

    if callback.data == "back_to_dates":
        await state.set_state(BookingFSM.choosing_date)
        await callback.message.edit_text(text="Вы вернулись назад. Выберите удобную дату:", reply_markup=dates_kb())
        await callback.answer()
        return

    if callback.data.startswith("time:"):
        chosen_time = callback.data.split(":", 1)[1]
        data = await state.update_data(chosen_time=chosen_time)

        service_data = SERVICES[data["service"]]
        master_name = MASTERS[data["master"]]

        confirm_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm")],
                [InlineKeyboardButton(text="⬅️ Назад к времени", callback_data="back_to_times")],
            ]
        )

        await callback.message.edit_text(
            f"Проверь запись:\n\n"
            f"Услуга: {service_data['title']} — {service_data['price']} руб.\n"
            f"Мастер: {master_name}\n"
            f"Дата: {data['chosen_date']}\n"
            f"Время: {chosen_time}\n",
            reply_markup=confirm_kb,
        )
        await state.set_state(BookingFSM.confirming)
        await callback.answer()
        return

    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer("Этот слот уже занят", show_alert=True)


# ---------- Шаг 5: подтверждение ----------

@router.callback_query(BookingFSM.confirming, F.data == "back_to_times")
async def process_back_to_times(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    master_key = data.get("master")
    chosen_date = data.get("chosen_date")

    await state.set_state(BookingFSM.choosing_time)
    await callback.message.edit_text(
        text="Вы вернулись назад. Выберите удобное время:",
        reply_markup=times_kb(master_key, chosen_date),
    )
    await callback.answer()


@router.callback_query(BookingFSM.confirming, F.data == "confirm")
async def confirm_booking(callback: CallbackQuery, state: FSMContext) -> None:
    """Бронируем слот и переходим к сбору контактов."""
    data = await state.get_data()
    slot = (data["master"], data["chosen_date"], data["chosen_time"])
    BOOKED_SLOTS.add(slot)

    await callback.message.edit_text("Отлично! Остался последний шаг.")
    await callback.message.answer(
        "Напишите номер телефона для связи или поделитесь им кнопкой ниже 👇",
        reply_markup=phone_request_kb(),
    )
    await state.set_state(BookingFSM.entering_phone)
    await callback.answer()


# ---------- Шаги 6-7: телефон и ФИО ----------

@router.message(BookingFSM.entering_phone)
async def get_phone(message: Message, state: FSMContext) -> None:
    if message.contact:
        phone = message.contact.phone_number
    elif message.text:
        phone = message.text.strip()
    else:
        await message.answer("Отправьте номер текстом или кнопкой ниже 👇")
        return

    await state.update_data(phone=phone)
    await message.answer(
        "Спасибо! Теперь напишите, пожалуйста, ваше ФИО:",
        reply_markup=ReplyKeyboardRemove(),
    )
    await state.set_state(BookingFSM.entering_fio)


@router.message(BookingFSM.entering_fio)
async def get_fio(message: Message, state: FSMContext, bot: Bot) -> None:
    """Финальный шаг: получаем ФИО и одним сообщением уведомляем админа."""
    fio = message.text.strip() if message.text else None
    if not fio:
        await message.answer("Пожалуйста, напишите ФИО текстом.")
        return

    data = await state.get_data()
    service = SERVICES[data["service"]]
    master_name = MASTERS[data["master"]]
    user_id = message.from_user.id

    USER_BOOKINGS.setdefault(user_id, set()).add(data["service"])

    cancel_kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="❌ Отменить запись",
                callback_data=f"cancel_booking:{data['service']}:{data['master']}:{data['chosen_date']}:{data['chosen_time']}",
            )
        ]]
    )

    await message.answer(
        "✅ Запись подтверждена! Мы свяжемся с вами для уточнения деталей.",
        reply_markup=cancel_kb,
    )
    await message.answer(
        "Если понадобится ещё что-то — используйте меню ниже 👇",
        reply_markup=persistent_menu_kb(),
    )

    if ADMIN_CHAT_ID:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"🆕 <b>Новая запись</b>\n\n"
            f"💈 Услуга: {service['title']}\n"
            f"🧑 Мастер: {master_name}\n"
            f"📅 Дата: {data['chosen_date']}\n"
            f"🕐 Время: {data['chosen_time']}\n\n"
            f"👤 ФИО: {fio}\n"
            f"📱 Телефон: <code>{data.get('phone', '—')}</code>\n"
            f"💬 Telegram: @{message.from_user.username or user_id}",
            parse_mode="HTML",
        )

    await state.clear()


# ---------- Отмена уже подтверждённой записи ----------

@router.callback_query(F.data.startswith("cancel_booking:"))
async def cancel_existing_booking(callback: CallbackQuery, bot: Bot) -> None:
    _, service, master, chosen_date, chosen_time = callback.data.split(":", 4)
    slot = (master, chosen_date, chosen_time)
    user_id = callback.from_user.id

    BOOKED_SLOTS.discard(slot)
    USER_BOOKINGS.get(user_id, set()).discard(service)

    await callback.message.edit_text("❌ Запись отменена. Если понадобится — можете записаться заново через меню.")

    if ADMIN_CHAT_ID:
        await bot.send_message(
            ADMIN_CHAT_ID,
            f"⚠️ Клиент @{callback.from_user.username or user_id} отменил запись: "
            f"{SERVICES[service]['title']} | {MASTERS[master]}, {chosen_date} {chosen_time}",
        )

    await callback.answer("Запись отменена")


# ---------- Отладочный хендлер ----------

@router.callback_query()
async def global_debug_handler(callback: CallbackQuery) -> None:
    """Ловит необработанные нажатия. Перед показом бота реальному клиенту
    эту функцию лучше удалить, чтобы он не видел технические DEBUG-сообщения."""
    await callback.message.answer(
        text=f"DEBUG: Вы нажали кнопку!\nПолучен callback_data: <code>{callback.data}</code>",
        parse_mode="HTML",
    )
    await callback.answer()


# ---------- Точка входа ----------

async def main() -> None:
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())

    router.include_router(support_router)
    dp.include_router(router)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())