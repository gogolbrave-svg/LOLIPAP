import asyncio
import datetime
import logging
import os
import random
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
)
from aiohttp import ClientSession, web
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SAFARIPAY_MERCHANT_ID = os.getenv("SAFARIPAY_MERCHANT_ID")
SAFARIPAY_API_KEY = os.getenv("SAFARIPAY_API_KEY")

if not BOT_TOKEN:
  raise ValueError("❌ BOT_TOKEN не найден!")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

admin_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(admin_env) if admin_env and admin_env.isdigit() else None


class OrderForm(StatesGroup):
  service = State()
  addon = State()
  country = State()
  city = State()
  address = State()
  photos = State()
  task = State()
  urgency = State()
  receipt = State()


class AdForm(StatesGroup):
  platform = State()
  duration = State()
  niche = State()
  link = State()
  creative = State()
  network = State()
  receipt = State()


class SupportForm(StatesGroup):
  reason = State()
  photo = State()


USER_ORDERS = {}
PENDING_STARS_ORDERS = {}
KNOWN_USERS = {}
PAYMENT_TIMERS = {}
ABANDONED_CHECKOUTS = {}
SAFARIPAY_PREFETCH_CACHE = {}

router = Router()

COUNTRY_CURRENCIES = {
    "Украина": {"code": "UAH", "symbol": "грн."},
    "Россия": {"code": "RUB", "symbol": "руб."},
    "Беларусь": {"code": "BYN", "symbol": "byn"},
    "Казахстан": {"code": "KZT", "symbol": "тңг."},
    "Молдова": {"code": "MDL", "symbol": "лей"},
    "Узбекистан": {"code": "UZS", "symbol": "сум"},
    "Кыргызстан": {"code": "KGS", "symbol": "сом"},
    "Таджикистан": {"code": "TJS", "symbol": "сом."},
    "Грузия": {"code": "GEL", "symbol": "lari"},
    "Армения": {"code": "AMD", "symbol": "драм"},
    "Азербайджан": {"code": "AZN", "symbol": "манат"},
}


async def get_exchange_rate(target_currency: str) -> float:
  url = "https://open.er-api.com/v6/latest/USD"
  try:
    async with ClientSession() as session:
      async with session.get(url, timeout=5) as response:
        if response.status == 200:
          data = await response.json()
          rates = data.get("rates", {})
          if target_currency in rates:
            return float(rates[target_currency])
  except Exception as e:
    logger.warning(f"Не удалось получить курс для {target_currency}: {e}.")

  fallback_rates = {
      "UAH": 41.5, "RUB": 95.0, "BYN": 3.3, "KZT": 480.0, "MDL": 18.0,
      "UZS": 12600.0, "KGS": 89.0, "TJS": 10.9, "GEL": 2.7, "AMD": 390.0, "AZN": 1.7,
  }
  return fallback_rates.get(target_currency, 1.0)


def get_dynamic_discount_content():
  today = datetime.date.today()
  target_date = today + datetime.timedelta(days=3)
  target_day = target_date.day
  target_month = target_date.month

  months_ru = [
      "", "января", "февраля", "марта", "апреля", "мая", "июня",
      "июля", "августа", "сентября", "октября", "ноября", "декабря",
  ]
  month_name = months_ru[target_month]

  text = (
      "🔥 <b>Специальное предложение!</b> 🔥\n\n"
      f"Напоминаем, что в нашем сервисе действуют скидки <b>-20%</b> только до"
      f" <b>{target_day} {month_name}</b>!\n\n"
      "Успейте оформить спортивную программу или консультацию на лучших условиях.\n\n"
      "Ждем вас в боте 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔥 Успеть заказать со скидкой -20%", callback_data="start_order")],
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")],
      ]
  )
  return text, keyboard


async def scheduled_broadcast(bot: Bot):
  while True:
    await asyncio.sleep(3600)
    now = datetime.datetime.now()
    for user_id, last_time in list(KNOWN_USERS.items()):
      if last_time is None or (now - last_time).total_seconds() >= 24 * 3600:
        try:
          text, keyboard = get_dynamic_discount_content()
          await bot.send_message(chat_id=user_id, text=text, reply_markup=keyboard, parse_mode="HTML")
          KNOWN_USERS[user_id] = now
        except Exception as e:
          logger.error(f"Не удалось отправить автосообщение пользователю {user_id}: {e}")


async def delayed_abandoned_cart_reminder(bot: Bot, user_id: int):
  try:
    await asyncio.sleep(1800)
    if user_id in ABANDONED_CHECKOUTS and ABANDONED_CHECKOUTS[user_id]:
      reminder_text = (
          "👋 <b>Завершите оформление заказа!</b>\n\n"
          "Мы заметили, что вы остановились на этапе оплаты, но так и не завершили оформление.\n"
          "Нажмите кнопку ниже, чтобы вернуться в бот 👇"
      )
      keyboard = InlineKeyboardMarkup(
          inline_keyboard=[
              [InlineKeyboardButton(text="🛒 Вернуться к заказу", callback_data="start_order")],
              [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")]
          ]
      )
      await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=keyboard, parse_mode="HTML")
      ABANDONED_CHECKOUTS.pop(user_id, None)
  except asyncio.CancelledError:
    pass
  except Exception as e:
    logger.error(f"Ошибка отправки напоминания: {e}")


async def run_payment_timer(bot: Bot, chat_id: int, message_id: int, base_text: str, keyboard_buttons_factory):
  total_seconds = 20 * 60
  task_key = (chat_id, message_id)
  
  try:
    while total_seconds > 0:
      mins, secs = divmod(total_seconds, 60)
      timer_str = f"{mins:02d}:{secs:02d}"
      current_text = f"⏳ <b>Успейте оплатить! Осталось: <u>{timer_str}</u></b>\n\n{base_text}"
      keyboard = keyboard_buttons_factory(timer_active=True, time_left=timer_str)
      try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=current_text, reply_markup=keyboard, parse_mode="HTML")
      except Exception:
        pass
      await asyncio.sleep(1)
      total_seconds -= 1
      
    expired_text = "❌ <b>Время на оплату заказа истекло!</b>\n\nБронь заказа была отменена."
    expired_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Сделать новый заказ", callback_data="start_order")]])
    try:
      await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=expired_text, reply_markup=expired_kb, parse_mode="HTML")
    except Exception:
      pass
    if task_key in PAYMENT_TIMERS:
      del PAYMENT_TIMERS[task_key]
  except asyncio.CancelledError:
    pass


async def create_safaripay_invoice(amount_rub: float, method: str) -> str:
  if not SAFARIPAY_API_KEY:
    return ""

  url = "https://api.safaripay.pro/v1/invoices"
  headers = {"X-API-Key": SAFARIPAY_API_KEY, "Content-Type": "application/json"}
  payload = {"amount": float(amount_rub), "method": method}

  for attempt in range(1, 4):
    try:
      async with ClientSession() as session:
        async with session.post(f"{url}?wait=20", json=payload, headers=headers, timeout=15) as resp:
          if resp.status in (200, 201):
            data = await resp.json()
            link = data.get("link", "")
            if link:
              return link
    except Exception as e:
      logger.error(f"Попытка {attempt}/3 ошибки запроса SafariPay ({method}): {e}")
    if attempt < 3:
      await asyncio.sleep(attempt)
  return ""


async def get_reputation_text_and_keyboard():
  current_date = datetime.date.today()
  base_date = datetime.date(2026, 1, 1)
  days_passed = max(0, (current_date - base_date).days)
  completed = min(3752 + days_passed * 25, 20000)
  rng = random.Random(current_date.toordinal())
  in_progress = rng.randint(170, 500)
  new_requests = rng.randint(30, 80)

  text = (
      "📊 <b>Наша статистика и репутация:</b>\n\n"
      f"• Выполненных программ/тренировок: <b>{completed}</b>\n"
      f"• Активных клиентов: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Мы дорожим своей репутацией и гарантируем качество услуг.</i>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]])
  return text, keyboard


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🛒 Заказать программу / тренировку", callback_data="start_order")],
          [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders"),
           InlineKeyboardButton(text="📊 Наша репутация", callback_data="reputation")],
          [InlineKeyboardButton(text="🤝 Сотрудничество (Купить рекламу)", callback_data="start_ads")],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin")],
      ]
  )
  text = (
      "<b>Добро пожаловать в сервис «СПОРТ УСЛУГИ»!</b> 🏅\n\n"
      "Профессиональные персональные тренировки, разработка планов питания, "
      "фитнес-программы и онлайн-сопровождение.\n\nВыберите нужный пункт в меню ниже:"
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("reputation"))
async def cmd_reputation(message: Message):
  text, keyboard = await get_reputation_text_and_keyboard()
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("my_orders"))
async def cmd_my_orders(message: Message):
  user_id = message.from_user.id
  orders = USER_ORDERS.get(user_id, [])
  if not orders:
    text = "📦 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных заказов."
  else:
    text = "📦 <b>Ваши заказы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += f"<b>Заказ #{idx}</b>\n• Услуга: {ord_data['service_name']}\n• Сумма: {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"

  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]])
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("order", "make_order"))
async def cmd_order(message: Message, state: FSMContext):
  await state.clear()
  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🟢 Экспресс-консультация по питанию (10$)", callback_data="srv_Экспресс-консультация_по_питанию_10")],
          [InlineKeyboardButton(text="🔥 План домашних тренировок (20$)", callback_data="srv_План_домашних_тренировок_20")],
          [InlineKeyboardButton(text="🥊 Разовое онлайн-занятие (50$)", callback_data="srv_Разовое_онлайн-занятие_50")],
          [InlineKeyboardButton(text="🧎‍♂️ Растяжка и мобильность (курс) (70$)", callback_data="srv_Растяжка_и_мобильность_70")],
          [InlineKeyboardButton(text="🦴 Программа реабилитации после травм (90$)", callback_data="srv_Программа_реабилитации_90")],
          [InlineKeyboardButton(text="🦿 Курс «Здоровая спина и суставы» (110$)", callback_data="srv_Курс_Здоровая_спина_110")],
          [InlineKeyboardButton(text="🏥 Полный чек-ап физической формы (130$)", callback_data="srv_Чек-ап_физической_формы_130")],
          [InlineKeyboardButton(text="🪓 Интенсивный курс похудения/сушки (150$)", callback_data="srv_Интенсивный_курс_похудения_150")],
          [InlineKeyboardButton(text="🏹 Месячное персональное сопровождение (180$)", callback_data="srv_Месячное_сопровождение_180")],
          [InlineKeyboardButton(text="🌳 VIP-пакет «Трансформация тела за 90 дней» (200$)", callback_data="srv_VIP_трансформация_90_дней_200")],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await message.answer("<b>Шаг 1 из 8: Выбор программы или услуги</b>\n\nВыберите нужный спортивный пакет:", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "reputation")
async def show_reputation(callback: CallbackQuery):
  text, keyboard = await get_reputation_text_and_keyboard()
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
  await state.set_state(SupportForm.reason)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")]])
  text = "💬 <b>Служба поддержки</b>\n\n<b>Шаг 1 из 2: Причина обращения</b>\nПожалуйста, подробно опишите ваш вопрос:"
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
  await state.update_data(reason=message.text.strip())
  await state.set_state(SupportForm.photo)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⏩ Пропустить (Без фото)", callback_data="skip_support_photo")],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  text = "💬 <b>Служба поддержки</b>\n\n<b>Шаг 2 из 2: Прикрепить фото</b>\n📸 Отправьте скриншот (если есть), либо пропустите:"
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
  photo_id = message.photo[-1].file_id
  data = await state.get_data()
  reason = data.get("reason")
  user = message.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = f"📩 <b>Новое обращение в поддержку!</b>\n\n• От кого: {user_info}\n• User ID: <code>{user.id}</code>\n• Причина: {reason}"
  if ADMIN_ID:
    try:
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Ошибка отправки админу: {e}")

  await message.answer("✅ <b>Ваше обращение успешно отправлено!</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]), parse_mode="HTML")
  await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  reason = data.get("reason")
  user = callback.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = f"📩 <b>Новое обращение в поддержку!</b>\n\n• От кого: {user_info}\n• User ID: <code>{user.id}</code>\n• Причина: {reason}"
  if ADMIN_ID:
    try:
      await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Ошибка отправки админу: {e}")

  await callback.message.edit_text("✅ <b>Ваше обращение успешно отправлено!</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]), parse_mode="HTML")
  await state.clear()
  await callback.answer()


@router.message(SupportForm.photo, ~F.photo)
async def process_support_photo_invalid(message: Message):
  await message.answer("⚠️ Пожалуйста, отправьте именно <b>фотографию</b> или нажмите кнопку «Пропустить»!")


@router.message(F.reply_to_message)
async def admin_reply_handler(message: Message):
  if not ADMIN_ID or message.from_user.id != ADMIN_ID:
    return
  replied_msg = message.reply_to_message
  if not replied_msg:
    return
  text_to_parse = replied_msg.text or replied_msg.caption or ""
  target_user_id = None
  for line in text_to_parse.split("\n"):
    if "User ID:" in line:
      parts = line.split("<code>")
      if len(parts) > 1:
        id_str = parts[1].split("</code>")[0].strip()
        if id_str.isdigit():
          target_user_id = int(id_str)
  if not target_user_id:
    await message.reply("❌ Не удалось определить ID пользователя.")
    return
  try:
    await message.bot.send_message(chat_id=target_user_id, text=f"💬 <b>Ответ поддержки:</b>\n\n{message.text or message.caption or ''}", parse_mode="HTML")
    await message.reply("✅ Ответ отправлен!")
  except Exception as e:
    await message.reply(f"❌ Ошибка: {e}")


@router.callback_query(F.data == "back_home")
async def back_home(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task_key = (callback.message.chat.id, callback.message.message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)
  await state.clear()
  
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🛒 Заказать программу / тренировку", callback_data="start_order")],
          [InlineKeyboardButton(text="📦 Мои заказы", callback_data="my_orders"),
           InlineKeyboardButton(text="📊 Наша репутация", callback_data="reputation")],
          [InlineKeyboardButton(text="🤝 Сотрудничество (Купить рекламу)", callback_data="start_ads")],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin")],
      ]
  )
  text = (
      "<b>Добро пожаловать в сервис «СПОРТ УСЛУГИ»!</b> 🏅\n\n"
      "Профессиональные персональные тренировки, разработка планов питания, "
      "фитнес-программы и онлайн-сопровождение.\n\nВыберите нужный пункт в меню ниже:"
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "start_order")
async def process_start_order(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  task_key = (callback.message.chat.id, callback.message.message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)

  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🟢 Экспресс-консультация по питанию (10$)", callback_data="srv_Экспресс-консультация_по_питанию_10")],
          [InlineKeyboardButton(text="🔥 План домашних тренировок (20$)", callback_data="srv_План_домашних_тренировок_20")],
          [InlineKeyboardButton(text="🥊 Разовое онлайн-занятие (50$)", callback_data="srv_Разовое_онлайн-занятие_50")],
          [InlineKeyboardButton(text="🧎‍♂️ Растяжка и мобильность (курс) (70$)", callback_data="srv_Растяжка_и_мобильность_70")],
          [InlineKeyboardButton(text="🦴 Программа реабилитации после травм (90$)", callback_data="srv_Программа_реабилитации_90")],
          [InlineKeyboardButton(text="🦿 Курс «Здоровая спина и суставы» (110$)", callback_data="srv_Курс_Здоровая_спина_110")],
          [InlineKeyboardButton(text="🏥 Полный чек-ап физической формы (130$)", callback_data="srv_Чек-ап_физической_формы_130")],
          [InlineKeyboardButton(text="🪓 Интенсивный курс похудения/сушки (150$)", callback_data="srv_Интенсивный_курс_похудения_150")],
          [InlineKeyboardButton(text="🏹 Месячное персональное сопровождение (180$)", callback_data="srv_Месячное_сопровождение_180")],
          [InlineKeyboardButton(text="🌳 VIP-пакет «Трансформация тела за 90 дней» (200$)", callback_data="srv_VIP_трансформация_90_дней_200")],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text("<b>Шаг 1 из 8: Выбор программы или услуги</b>\n\nВыберите нужный спортивный пакет:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(OrderForm.service, F.data.startswith("srv_"))
async def process_service(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  price = int(parts[-1])
  service_name = " ".join(parts[1:-1]).replace("_", " ")

  await state.update_data(service_name=service_name, service_price=price)
  await state.set_state(OrderForm.addon)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="📸 Текстовый отчёт (Бесплатно)", callback_data="addon_photo_0")],
          [InlineKeyboardButton(text="🎥 Видео-разбор техники (+10$)", callback_data="addon_video_10")],
      ]
  )
  await callback.message.edit_text(f"<b>Шаг 2 из 8: Формат отчёта</b>\n\nВыбрана услуга: <b>{service_name}</b> ({price}$)\n\nВыберите формат:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(OrderForm.addon, F.data.startswith("addon_"))
async def process_addon(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  addon_price = int(parts[-1])
  addon_type = "Видео-разбор техники" if addon_price > 0 else "Текстовый отчёт"

  await state.update_data(addon_type=addon_type, addon_price=addon_price)
  await state.set_state(OrderForm.country)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🇺🇦 Украина", callback_data="country_Ukraine"), InlineKeyboardButton(text="🇷🇺 Россия", callback_data="country_Russia")],
          [InlineKeyboardButton(text="🇧🇾 Беларусь", callback_data="country_Belarus"), InlineKeyboardButton(text="🇰🇿 Казахстан", callback_data="country_Kazakhstan")],
          [InlineKeyboardButton(text="🇲🇩 Молдова", callback_data="country_Moldova"), InlineKeyboardButton(text="🇺🇿 Узбекистан", callback_data="country_Uzbekistan")],
          [InlineKeyboardButton(text="🇰🇬 Кыргызстан", callback_data="country_Kyrgyzstan"), InlineKeyboardButton(text="🇹🇯 Таджикистан", callback_data="country_Tajikistan")],
          [InlineKeyboardButton(text="🇬🇪 Грузия", callback_data="country_Georgia"), InlineKeyboardButton(text="🇦🇲 Армения", callback_data="country_Armenia")],
          [InlineKeyboardButton(text="🇦🇿 Азербайджан", callback_data="country_Azerbaijan")],
      ]
  )
  await callback.message.edit_text("<b>Шаг 3 из 8: Выбор страны</b>\n\nВыберите страну проживания:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(OrderForm.country, F.data.startswith("country_"))
async def process_country(callback: CallbackQuery, state: FSMContext):
  c_code = callback.data.split("_")[1]
  names = {
      "Ukraine": "Украина", "Russia": "Россия", "Belarus": "Беларусь",
      "Kazakhstan": "Казахстан", "Moldova": "Молдова", "Uzbekistan": "Узбекистан",
      "Kyrgyzstan": "Кыргызстан", "Tajikistan": "Таджикистан", "Georgia": "Грузия",
      "Armenia": "Армения", "Azerbaijan": "Азербайджан",
  }
  selected = names.get(c_code, c_code)
  curr_info = COUNTRY_CURRENCIES.get(selected, {"code": "USD", "symbol": "USD"})
  await state.update_data(country=selected, curr_code=curr_info["code"], curr_symbol=curr_info["symbol"])
  await state.set_state(OrderForm.city)
  await callback.message.edit_text(f"<b>Шаг 4 из 8: Город</b>\n\nВыбрано: <b>{selected}</b>\n\nВведите ваш город текстом:", parse_mode="HTML")
  await callback.answer()


@router.message(OrderForm.city, F.text)
async def process_city(message: Message, state: FSMContext):
  await state.update_data(city=message.text.strip())
  await state.set_state(OrderForm.address)
  await message.answer("<b>Шаг 5 из 8: Контакты</b>\n\nВведите ваш ник в Telegram (@username) для связи:", parse_mode="HTML")


@router.message(OrderForm.address, F.text)
async def process_address(message: Message, state: FSMContext):
  await state.update_data(address=message.text.strip(), photos=[])
  await state.set_state(OrderForm.photos)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово / Пропустить фото", callback_data="finish_photos")]])
  await message.answer("<b>Шаг 6 из 8: Фото формы (необязательно)</b>\n\n📷 Отправьте фото при необходимости. Нажмите «Готово»:", reply_markup=keyboard, parse_mode="HTML")


@router.message(OrderForm.photos, F.photo)
async def process_photo(message: Message, state: FSMContext):
  data = await state.get_data()
  photos = data.get("photos", [])
  if len(photos) >= 3:
    await message.answer("⚠️ Уже добавлено 3 фото.")
    return
  photos.append(message.photo[-1].file_id)
  await state.update_data(photos=photos)
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"✅ Готово ({len(photos)}/3)", callback_data="finish_photos")]])
  await message.answer(f"📸 Фото #{len(photos)} загружено!", reply_markup=keyboard)


@router.message(OrderForm.photos, ~F.photo)
async def process_photo_invalid(message: Message, state: FSMContext):
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово / Пропустить", callback_data="finish_photos")]])
  await message.answer("⚠️ Отправьте фотографию или нажмите «Готово».", reply_markup=keyboard)


@router.callback_query(OrderForm.photos, F.data == "finish_photos")
async def finish_photos(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.task)
  await callback.message.edit_text("<b>Шаг 7 из 8: Пожелания и цели</b>\n\nОпишите ваши цели, вес/рост или ограничения:", parse_mode="HTML")
  await callback.answer()


@router.message(OrderForm.task, F.text)
async def process_task(message: Message, state: FSMContext):
  await state.update_data(task=message.text.strip())
  await state.set_state(OrderForm.urgency)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⚡ Срочно за 24 часа (+10$)", callback_data="urg_fast_10")],
          [InlineKeyboardButton(text="🕐 К завтрашнему дню (+5$)", callback_data="urg_tomorrow_5")],
          [InlineKeyboardButton(text="📅 Стандартный срок (Бесплатно)", callback_data="urg_standard_0")],
      ]
  )
  await message.answer("<b>Шаг 8 из 8: Срочность</b>\n\nВыберите срочность выполнения:", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(OrderForm.urgency, F.data.startswith("urg_"))
async def process_urgency(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  urg_type = parts[1]
  urg_price = int(parts[2])
  urg_map = {"fast": "⚡ Срочно", "tomorrow": "🕐 К завтра", "standard": "📅 Стандарт"}
  urgency = urg_map.get(urg_type, "📅 Стандарт")

  await state.update_data(urgency=urgency, urgency_price=urg_price)
  data = await state.get_data()
  total_price = data.get("service_price", 0) + data.get("addon_price", 0) + urg_price
  await state.update_data(total_price=total_price)

  curr_code, curr_symbol = data.get("curr_code", "USD"), data.get("curr_symbol", "USD")
  rate = await get_exchange_rate(curr_code)
  local_str = f"{round(total_price * rate):,}".replace(",", " ")

  summary_text = (
      "🎉 <b>Ваша заявка успешно сформирована!</b>\n\n"
      f"• Услуга: <b>{data.get('service_name')}</b>\n"
      f"• Итого к оплате: <b>{total_price}$</b> (≈ {local_str} {curr_symbol})\n\n"
      "⚠️ <b>Выберите способ оплаты ниже 👇</b>"
  )
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить", callback_data="go_to_payment")]])
  await callback.message.edit_text(summary_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id, message_id = callback.message.chat.id, callback.message.message_id
  
  if user_id in ABANDONED_CHECKOUTS:
    ABANDONED_CHECKOUTS[user_id].cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(delayed_abandoned_cart_reminder(callback.bot, user_id))

  data = await state.get_data()
  total_price = data.get("total_price", 250)
  country = data.get("country", "")
  rub_amount = max(100, int(total_price * 90))
  stars_amount = max(1, rub_amount)

  SAFARIPAY_PREFETCH_CACHE[user_id] = {
      "sbp": asyncio.create_task(create_safaripay_invoice(rub_amount, "sbp")),
      "stars": asyncio.create_task(create_safaripay_invoice(rub_amount, "stars"))
  }

  base_text = f"💳 <b>ОПЛАТА ЗАКАЗА</b>\n\n• <b>Сумма:</b> {total_price}$\n\n⚠️ Выберите способ оплаты 👇"

  def get_payment_keyboard(timer_active=False, time_left="20:00"):
    kb = []
    if country != "Украина":
      kb.append([InlineKeyboardButton(text=f"💳 Карта / СБП (~{rub_amount} ₽)", callback_data="pay_sbp")])
    kb.append([InlineKeyboardButton(text=f"⭐ Telegram Stars ({stars_amount} ⭐)", callback_data="pay_safari_stars")])
    kb.append([InlineKeyboardButton(text="🪙 Крипта USDT / BTC / TON", callback_data="crypto_expert")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

  await callback.message.edit_text(f"⏳ <b>Осталось: <u>20:00</u></b>\n\n{base_text}", reply_markup=get_payment_keyboard(), parse_mode="HTML")
  
  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
  PAYMENT_TIMERS[task_key] = asyncio.create_task(run_payment_timer(callback.bot, chat_id, message_id, base_text, get_payment_keyboard))
  await callback.answer()


@router.callback_query(F.data == "pay_sbp")
async def process_pay_sbp(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  data = await state.get_data()
  rub_amount = max(100, int(data.get("total_price", 250) * 90))

  pref_task = SAFARIPAY_PREFETCH_CACHE.get(user_id, {}).get("sbp")
  pay_url = await pref_task if pref_task else await create_safaripay_invoice(rub_amount, "sbp")
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)

  if not pay_url:
    await callback.message.edit_text("⚠️ Ошибка шлюза. Попробуйте еще раз.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить", callback_data="pay_sbp")]]))
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить СБП / Картой", url=pay_url)],
          [InlineKeyboardButton(text="✅ Я оплатил(-а), проверить", callback_data="safaripay_check_success")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")]
      ]
  )
  await callback.message.edit_text(f"💳 <b>Оплата картой / СБП</b>\n\nСумма: <b>{rub_amount} ₽</b>", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "pay_safari_stars")
async def process_pay_safari_stars(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  data = await state.get_data()
  rub_amount = max(1, int(data.get("total_price", 250) * 90))

  pref_task = SAFARIPAY_PREFETCH_CACHE.get(user_id, {}).get("stars")
  pay_url = await pref_task if pref_task else await create_safaripay_invoice(rub_amount, "stars")
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)

  if not pay_url:
    await callback.message.edit_text("⚠️ Ошибка шлюза Stars.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Повторить", callback_data="pay_safari_stars")]]))
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⭐ Оплатить звёздами", url=pay_url)],
          [InlineKeyboardButton(text="✅ Я оплатил(-а), проверить", callback_data="safaripay_check_success")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")]
      ]
  )
  await callback.message.edit_text(f"⭐ <b>Оплата Telegram Звёздами</b>\n\nСумма: <b>{rub_amount} ⭐</b>", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "safaripay_check_success")
async def process_safaripay_check(callback: CallbackQuery, state: FSMContext):
  user = callback.from_user
  data = await state.get_data()
  USER_ORDERS.setdefault(user.id, []).append({"service_name": data.get("service_name"), "total_price": data.get("total_price"), "status": "⏳ Ожидает проверки платежа"})
  
  if ADMIN_ID:
    try:
      user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
      await callback.bot.send_message(chat_id=ADMIN_ID, text=f"💳 <b>Новый платеж Safari Pay!</b>\n• От: {user_info}\n• Сумма: {data.get('total_price')}$", parse_mode="HTML")
    except Exception:
      pass

  await callback.message.edit_text("✅ <b>Платеж обрабатывается!</b>\n\nМодератор проверит поступление в течение 25 минут.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]), parse_mode="HTML")
  await state.clear()


@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery):
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔴 USDT (TRC-20)", callback_data="pay_trc20")],
          [InlineKeyboardButton(text="🟢 USDT (BEP-20)", callback_data="pay_bep20")],
          [InlineKeyboardButton(text="🔵 TON", callback_data="pay_ton")],
          [InlineKeyboardButton(text="🟠 Bitcoin (BTC)", callback_data="pay_btc")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")],
      ]
  )
  await callback.message.edit_text("💳 <b>Выберите сеть для криптовалюты:</b>", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data.in_({"pay_trc20", "pay_bep20", "pay_ton", "pay_btc"}))
async def process_crypto_wallet(callback: CallbackQuery, state: FSMContext):
  wallets = {
      "pay_trc20": ("USDT (TRC-20)", "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>"),
      "pay_bep20": ("USDT (BEP-20)", "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>"),
      "pay_ton": ("TON", "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>"),
      "pay_btc": ("Bitcoin (BTC)", "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>"),
  }
  net_name, address = wallets[callback.data]
  data = await state.get_data()
  total_price = data.get("total_price", 250)

  text = f"💳 <b>ОПЛАТА {net_name}</b>\n\n• <b>Сумма:</b> {total_price}$\n• <b>Реквизиты:</b>\n{address}\n\n⚠️ После оплаты нажмите кнопку ниже и отправьте чек."
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ask_for_receipt")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="crypto_expert")]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.receipt)
  await callback.message.edit_text("🧾 <b>Подтверждение оплаты</b>\n\nОтправьте в чат <b>фотографию чека</b> о переводе.", parse_mode="HTML")
  await callback.answer()


@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
  user = message.from_user
  data = await state.get_data()
  USER_ORDERS.setdefault(user.id, []).append({"service_name": data.get("service_name"), "total_price": data.get("total_price"), "status": "⏳ Ожидает проверки"})

  if ADMIN_ID:
    try:
      user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=f"🛒 <b>Новый чек!</b>\n• От: {user_info}\n• Сумма: {data.get('total_price')}$", parse_mode="HTML")
    except Exception:
      pass

  await message.answer("✅ <b>Чек принят!</b> Ожидайте проверки модератором.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]), parse_mode="HTML")
  await state.clear()


@router.message(OrderForm.receipt, ~F.photo)
async def process_receipt_invalid(message: Message):
  await message.answer("⚠️ Пожалуйста, отправьте именно <b>фотографию чека</b> (картинкой)!")


@router.callback_query(F.data == "start_ads")
async def process_start_ads(callback: CallbackQuery, state: FSMContext):
  await state.clear()
  await state.set_state(AdForm.platform)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🤖 Реклама в боте", callback_data="adplat_bot")],
          [InlineKeyboardButton(text="📢 Реклама в канале", callback_data="adplat_channel")],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text("📢 <b>Покупка рекламы</b>\n\nВыберите площадку:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(AdForm.platform, F.data.startswith("adplat_"))
async def process_ad_platform(callback: CallbackQuery, state: FSMContext):
  plat_code = callback.data.split("_")[1]
  await state.update_data(platform="Реклама в боте" if plat_code == "bot" else "Реклама в канале")
  await state.set_state(AdForm.duration)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="24 часа = 5$", callback_data="addur_24h_5")],
          [InlineKeyboardButton(text="3 дня = 10$", callback_data="addur_3d_10")],
          [InlineKeyboardButton(text="7 дней = 25$", callback_data="addur_7d_25")],
          [InlineKeyboardButton(text="30 дней = 100$", callback_data="addur_30d_100")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="start_ads")],
      ]
  )
  await callback.message.edit_text("<b>Выберите срок размещения рекламы:</b>", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(AdForm.duration, F.data.startswith("addur_"))
async def process_ad_duration(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  dur_map = {"24h": "24 часа", "3d": "3 дня", "7d": "7 дней", "30d": "30 дней"}
  await state.update_data(duration=dur_map.get(parts[1], parts[1]), ad_price=int(parts[2]))
  await state.set_state(AdForm.niche)
  await callback.message.edit_text("<b>Шаг 3: Введите тематику проекта текстом:</b>", parse_mode="HTML")
  await callback.answer()


@router.message(AdForm.niche, F.text)
async def process_ad_niche(message: Message, state: FSMContext):
  await state.update_data(niche=message.text.strip())
  await state.set_state(AdForm.link)
  await message.answer("<b>Шаг 4: Введите ссылку на ресурс:</b>", parse_mode="HTML")


@router.message(AdForm.link, F.text)
async def process_ad_link(message: Message, state: FSMContext):
  await state.update_data(link=message.text.strip())
  await state.set_state(AdForm.creative)
  await message.answer("<b>Шаг 5: Отправьте рекламный креатив (текст/фото):</b>", parse_mode="HTML")


@router.message(AdForm.creative)
async def process_ad_creative(message: Message, state: FSMContext):
  creative_desc = f"[Фото] {message.caption or ''}" if message.photo else (message.text or "[Текст]")
  await state.update_data(creative=creative_desc)
  data = await state.get_data()

  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить рекламу ({data.get('ad_price')}$)", callback_data="ad_pay_select")]])
  await message.answer(f"🎉 <b>Заявка сформирована!</b>\nСумма: <b>{data.get('ad_price')}$</b>", reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "ad_pay_select")
async def process_ad_pay_select(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔴 USDT (TRC-20)", callback_data="adpay_trc20")],
          [InlineKeyboardButton(text="🟢 USDT (BEP-20)", callback_data="adpay_bep20")],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(f"💳 <b>Оплата рекламы ({data.get('ad_price')}$)</b>\n\nВыберите сеть:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data.in_({"adpay_trc20", "adpay_bep20"}))
async def adpay_wallet(callback: CallbackQuery):
  address = "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>" if "trc20" in callback.data else "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>"
  keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ad_ask_receipt")]])
  await callback.message.edit_text(f"💳 <b>Реквизиты для оплаты:</b>\n{address}\n\nПосле оплаты нажмите кнопку ниже:", reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "ad_ask_receipt")
async def ad_ask_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(AdForm.receipt)
  await callback.message.edit_text("🧾 <b>Подтверждение оплаты рекламы</b>\n\nОтправьте скриншот чека:", parse_mode="HTML")
  await callback.answer()


@router.message(AdForm.receipt, F.photo)
async def process_ad_receipt(message: Message, state: FSMContext):
  user = message.from_user
  if ADMIN_ID:
    try:
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=f"📢 <b>Новая оплата рекламы от @{user.username}</b>", parse_mode="HTML")
    except Exception:
      pass
  await message.answer("✅ <b>Чек принят!</b> Ожидайте подтверждения.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]), parse_mode="HTML")
  await state.clear()


async def handle_ping(request):
  return web.Response(text="Bot is running!")


async def start_web_server():
  app = web.Application()
  app.router.add_get("/", handle_ping)
  runner = web.AppRunner(app)
  await runner.setup()
  port = int(os.getenv("PORT", 10000))
  site = web.TCPSite(runner, "0.0.0.0", port)
  await site.start()


async def main():
  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher()
  dp.include_router(router)
  asyncio.create_task(start_web_server())
  asyncio.create_task(scheduled_broadcast(bot))
  await bot.delete_webhook(drop_pending_updates=True)
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
