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
    PreCheckoutQuery,
    SuccessfulPayment,
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

# ADMIN_ID теперь необязательный. Укажите в .env (ADMIN_ID=ваш_id) или оставьте пустым.
admin_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(admin_env) if admin_env and admin_env.isdigit() else None


class OrderForm(StatesGroup):
  target_count = State()
  service = State()
  addon = State()
  country = State()
  target_info = State()
  city = State()
  address = State()
  photos = State()
  task = State()
  urgency = State()
  receipt = State()


class VacancyForm(StatesGroup):
  country = State()
  city = State()
  name = State()
  age = State()
  parameters = State()
  sports_exp = State()
  experience = State()
  photo = State()
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


# База данных в памяти для хранения заказов пользователей
USER_ORDERS = {}

# Временное хранилище для заказов, оплачиваемых звездами
PENDING_STARS_ORDERS = {}

# Словарь для отслеживания пользователей и времени последней авто-рассылки: {user_id: datetime}
KNOWN_USERS = {}

# Хранилище задач таймеров и дожима для отмененных/неоплаченных оплат
PAYMENT_TIMERS = {}
ABANDONED_CHECKOUTS = {}

# Двойной кэш префетча для SafariPay (и карта/СБП, и звёзды генерируются параллельно)
SAFARIPAY_PREFETCH_CACHE = {}

router = Router()


# === СЛОВАРЬ ВАЛЮТ ДЛЯ СТРАН ===
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


# === ФУНКЦИЯ ПОЛУЧЕНИЯ РЕАЛЬНЫХ КУРСОВ ВАЛЮТ ===
async def get_exchange_rate(target_currency: str) -> float:
  url = f"https://open.er-api.com/v6/latest/USD"
  try:
    async with ClientSession() as session:
      async with session.get(url, timeout=5) as response:
        if response.status == 200:
          data = await response.json()
          rates = data.get("rates", {})
          if target_currency in rates:
            return float(rates[target_currency])
  except Exception as e:
    logger.warning(
        f"Не удалось получить курс для {target_currency}: {e}. Используем резервные значения."
    )

  fallback_rates = {
      "UAH": 41.5,
      "RUB": 95.0,
      "BYN": 3.3,
      "KZT": 480.0,
      "MDL": 18.0,
      "UZS": 12600.0,
      "KGS": 89.0,
      "TJS": 10.9,
      "GEL": 2.7,
      "AMD": 390.0,
      "AZN": 1.7,
  }
  return fallback_rates.get(target_currency, 1.0)


# === ФУНКЦИЯ ГЕНЕРАЦИИ ДИНАМИЧЕСКОЙ ДАТЫ ДЛЯ СКИДКИ ===
def get_dynamic_discount_content():
  today = datetime.date.today()
  target_date = today + datetime.timedelta(days=3)
  target_day = target_date.day
  target_month = target_date.month

  months_ru = [
      "",
      "января",
      "февраля",
      "марта",
      "апреля",
      "мая",
      "июня",
      "июля",
      "августа",
      "сентября",
      "октября",
      "ноября",
      "декабря",
  ]
  month_name = months_ru[target_month]

  text = (
      "🔥 <b>Специальное предложение!</b> 🔥\n\n"
      f"Напоминаем, что в нашем спортивном клубе действуют скидки <b>-20%</b> только до"
      f" <b>{target_day} {month_name}</b>!\n\n"
      "Успейте оформить абонемент на тренировки на лучших условиях. Мы гарантируем результат,"
      " профессиональный подход и качественное оборудование.\n\n"
      "Ждем вас в боте 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔥 Записаться со скидкой -20%",
                  callback_data="start_order",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home"
              )
          ],
      ]
  )
  return text, keyboard


# === ФОНОВАЯ ЗАДАЧА АВТО-РАССЫЛКИ КАЖДЫЕ 24 ЧАСА ===
async def scheduled_broadcast(bot: Bot):
  while True:
    await asyncio.sleep(3600)
    now = datetime.datetime.now()
    for user_id, last_time in list(KNOWN_USERS.items()):
      if (
          last_time is None
          or (now - last_time).total_seconds() >= 24 * 3600
      ):
        try:
          text, keyboard = get_dynamic_discount_content()
          await bot.send_message(
              chat_id=user_id, text=text, reply_markup=keyboard, parse_mode="HTML"
          )
          KNOWN_USERS[user_id] = now
        except Exception as e:
          logger.error(
              f"Не удалось отправить автосообщение пользователю {user_id}: {e}"
          )


# === ФОНОВАЯ ЗАДАЧА ДОЖИМА ЛИДОВ ЧЕРЕЗ 30 МИНУТ ===
async def delayed_abandoned_cart_reminder(bot: Bot, user_id: int):
  try:
    await asyncio.sleep(1800)
    if user_id in ABANDONED_CHECKOUTS and ABANDONED_CHECKOUTS[user_id]:
      reminder_text = (
          "👋 <b>Завершите бронирование тренировки!</b>\n\n"
          "Мы заметили, что вы остановились на этапе оплаты, но так и не завершили оформление.\n"
          "Возникли вопросы по расписанию или оплате? Наша поддержка всегда на связи!\n\n"
          "Нажмите кнопку ниже, чтобы вернуться в бот 👇"
      )
      keyboard = InlineKeyboardMarkup(
          inline_keyboard=[
              [InlineKeyboardButton(text="🛒 Вернуться к оформлению", callback_data="start_order")],
              [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")]
          ]
      )
      await bot.send_message(chat_id=user_id, text=reminder_text, reply_markup=keyboard, parse_mode="HTML")
      ABANDONED_CHECKOUTS.pop(user_id, None)
  except asyncio.CancelledError:
    pass
  except Exception as e:
    logger.error(f"Ошибка отправки напоминания о дожиме для {user_id}: {e}")


# === ФУНКЦИЯ ЖИВОГО ТАЙМЕРА ОБРАТНОГО ОТСЧЕТА (КАЖДУЮ СЕКУНДУ) ===
async def run_payment_timer(bot: Bot, chat_id: int, message_id: int, base_text: str, keyboard_buttons_factory):
  total_seconds = 20 * 60
  task_key = (chat_id, message_id)
  
  try:
    while total_seconds > 0:
      mins, secs = divmod(total_seconds, 60)
      timer_str = f"{mins:02d}:{secs:02d}"
      
      current_text = (
          f"⏳ <b>Успейте оплатить! До окончания бронирования осталось: <u>{timer_str}</u></b>\n\n"
          f"{base_text}"
      )
      keyboard = keyboard_buttons_factory(timer_active=True, time_left=timer_str)
      
      try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=current_text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )
      except Exception:
        pass
      
      await asyncio.sleep(1)
      total_seconds -= 1
      
    expired_text = (
        "❌ <b>Время на оплату заказа истекло!</b>\n\n"
        "Бронь абонемента была отменена. Пожалуйста, оформите заказ заново."
    )
    expired_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 Сделать новый заказ", callback_data="start_order")]]
    )
    try:
      await bot.edit_message_text(
          chat_id=chat_id,
          message_id=message_id,
          text=expired_text,
          reply_markup=expired_kb,
          parse_mode="HTML"
      )
    except Exception:
      pass
      
    if task_key in PAYMENT_TIMERS:
      del PAYMENT_TIMERS[task_key]
      
  except asyncio.CancelledError:
    pass


# === УСИЛЕННАЯ ФУНКЦИЯ SAFARIPAY С АВТОМАТИЧЕСКИМ ПОВТОРОМ (RETRY) ===
async def create_safaripay_invoice(amount_rub: float, method: str) -> str:
  if not SAFARIPAY_API_KEY:
    return ""

  url = "https://api.safaripay.pro/v1/invoices"
  headers = {
      "X-API-Key": SAFARIPAY_API_KEY,
      "Content-Type": "application/json"
  }
  
  payload = {
      "amount": float(amount_rub),
      "method": method
  }

  for attempt in range(1, 4):
    try:
      async with ClientSession() as session:
        async with session.post(f"{url}?wait=20", json=payload, headers=headers, timeout=15) as resp:
          if resp.status in (200, 201):
            data = await resp.json()
            link = data.get("link", "")
            if link:
              return link
          else:
            logger.warning(f"SafariPay вернул статус {resp.status} на попытке {attempt}")
    except Exception as e:
      logger.error(f"Попытка {attempt}/3 ошибки запроса SafariPay ({method}): {e}")
    
    if attempt < 3:
      await asyncio.sleep(attempt)

  return ""


# === ФУНКЦИЯ ДЛЯ ГЕНЕРАЦИИ РЕПУТАЦИИ ===
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
      f"• Проведенных тренировок: <b>{completed}</b>\n"
      f"• Активных клиентов: <b>{in_progress}</b>\n"
      f"• Новых заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Мы гордимся успехами наших клиентов и высоким уровнем тренерского состава.</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]
      ]
  )
  return text, keyboard


# === 0. ГЛАВНОЕ МЕНЮ И КОМАНДЫ ===
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🛒 Записаться на тренировку", callback_data="start_order"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📦 Мои абонементы", callback_data="my_orders"
              ),
              InlineKeyboardButton(
                  text="📊 Наша репутация", callback_data="reputation"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🤝 Сотрудничество (Реклама)",
                  callback_data="start_ads",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Поддержка", callback_data="contact_admin"
              )
          ],
          [
              InlineKeyboardButton(
                  text="💼 Стать тренером", callback_data="start_vacancy"
              )
          ],
      ]
  )
  text = (
      "<b>Добро пожаловать в клуб «PRO СПОРТ»!</b> 🏅\n\n"
      "Профессиональные персональные тренировки, фитнес-программы, составление планов питания "
      "и работа с сертифицированными тренерами.\n\n"
      "Выберите нужный пункт в меню ниже:"
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
    text = (
        "📦 <b>Мои абонементы</b>\n\nУ вас пока нет активных или завершенных"
        " абонементов на тренировки."
    )
  else:
    text = "📦 <b>Ваши абонементы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Абонемент #{idx}</b>\n• Программа: {ord_data['service_name']}\n• Сумма:"
          f" {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"
      )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("order", "make_order"))
async def cmd_order(message: Message, state: FSMContext):
  await state.clear()
  await state.set_state(OrderForm.target_count)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="👤 1 человек (Базово)", callback_data="targets_1_0"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 2 человека (+10$)", callback_data="targets_2_10"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 3 человека (+20$)", callback_data="targets_3_20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 4 человека (+30$)", callback_data="targets_4_30"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 5 человек (+40$)", callback_data="targets_5_40"
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await message.answer(
      "<b>Шаг 1 из 9: Количество участников</b>\n\nВыберите количество человек для тренировок:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )


@router.callback_query(F.data == "reputation")
async def show_reputation(callback: CallbackQuery):
  text, keyboard = await get_reputation_text_and_keyboard()
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "contact_admin")
async def contact_admin_info(callback: CallbackQuery, state: FSMContext):
  await state.set_state(SupportForm.reason)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")]
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 1 из 2: Причина обращения</b>\n"
      "Пожалуйста, опишите ваш вопрос (подбор расписания, выбор тренера, проблема с оплатой и т.д.):"
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
  await state.update_data(reason=message.text.strip())
  await state.set_state(SupportForm.photo)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⏩ Пропустить (Без фото)", callback_data="skip_support_photo"
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 2 из 2: Прикрепить скриншот</b>\n"
      "📸 Отправьте скриншот или фото (если есть вопрос по оплате), либо нажмите кнопку ниже, чтобы пропустить:"
  )
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

  admin_text = (
      f"📩 <b>Новое обращение в поддержку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {reason}\n\n"
      f"💬 <i>Чтобы ответить пользователю, просто сделайте Reply (ответ) на это сообщение в боте.</i>"
  )

  bot = message.bot
  if ADMIN_ID:
    try:
      await bot.send_photo(
          chat_id=ADMIN_ID,
          photo=photo_id,
          caption=admin_text,
          parse_mode="HTML",
      )
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Ваше обращение успешно отправлено!</b>\n\n"
      "Администратор клуба рассмотрит его и ответит вам в ближайшее время."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await message.answer(success_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  reason = data.get("reason")
  user = callback.from_user

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_text = (
      f"📩 <b>Новое обращение в поддержку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {reason}\n\n"
      f"💬 <i>Чтобы ответить пользователю, просто сделайте Reply (ответ) на это сообщение в боте.</i>"
  )

  bot = callback.bot
  if ADMIN_ID:
    try:
      await bot.send_message(
          chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Ваше обращение успешно отправлено!</b>\n\n"
      "Администратор клуба рассмотрит его и ответит вам в ближайшее время."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await callback.message.edit_text(
      success_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await state.clear()
  await callback.answer()


@router.message(SupportForm.photo, ~F.photo)
async def process_support_photo_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию (картинкой)</b> или нажмите кнопку «Пропустить»!"
  )


# === АДМИН-ПАНЕЛЬ: ОТВЕТ ПОЛЬЗОВАТЕЛЮ ЧЕРЕЗ REPLY ===
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
    for word in text_to_parse.split():
      if word.isdigit() and len(word) > 5:
        target_user_id = int(word)
        break

  if not target_user_id:
    await message.reply(
        "❌ Не удалось определить ID пользователя из сообщения, на которое вы ответили."
    )
    return

  try:
    answer_text = (
        "💬 <b>Ответ от службы поддержки клуба:</b>\n\n"
        f"{message.text or message.caption or ''}"
    )
    await message.bot.send_message(
        chat_id=target_user_id, text=answer_text, parse_mode="HTML"
    )
    await message.reply("✅ Ответ успешно доставлен пользователю!")
  except Exception as e:
    await message.reply(f"❌ Ошибка отправки сообщения пользователю: {e}")


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
  user_id = callback.from_user.id
  orders = USER_ORDERS.get(user_id, [])

  if not orders:
    text = (
        "📦 <b>Мои абонементы</b>\n\nУ вас пока нет активных или завершенных"
        " абонементов."
    )
  else:
    text = "📦 <b>Ваши абонементы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Абонемент #{idx}</b>\n• Программа: {ord_data['service_name']}\n• Сумма:"
          f" {ord_data['total_price']}$\n• Статус: {ord_data['status']}\n\n"
      )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


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
          [
              InlineKeyboardButton(
                  text="🛒 Записаться на тренировку", callback_data="start_order"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📦 Мои абонементы", callback_data="my_orders"
              ),
              InlineKeyboardButton(
                  text="📊 Наша репутация", callback_data="reputation"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🤝 Сотрудничество (Реклама)",
                  callback_data="start_ads",
              )
          ],
          [
              InlineKeyboardButton(
                  text="💬 Поддержка", callback_data="contact_admin"
              )
          ],
          [
              InlineKeyboardButton(
                  text="💼 Стать тренером", callback_data="start_vacancy"
              )
          ],
      ]
  )
  text = (
      "<b>Добро пожаловать в клуб «PRO СПОРТ»!</b> 🏅\n\n"
      "Профессиональные персональные тренировки, фитнес-программы, составление планов питания "
      "и работа с сертифицированными тренерами.\n\n"
      "Выберите нужный пункт в меню ниже:"
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

  await state.set_state(OrderForm.target_count)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="👤 1 человек (Базово)", callback_data="targets_1_0"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 2 человека (+10$)", callback_data="targets_2_10"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 3 человека (+20$)", callback_data="targets_3_20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 4 человека (+30$)", callback_data="targets_4_30"
              )
          ],
          [
              InlineKeyboardButton(
                  text="👥 5 человек (+40$)", callback_data="targets_5_40"
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(
      "<b>Шаг 1 из 9: Количество участников</b>\n\nВыберите количество человек для тренировок:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.target_count, F.data.startswith("targets_"))
async def process_target_count(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  count = int(parts[1])
  price = int(parts[2])

  await state.update_data(target_count=count, target_count_price=price)
  await state.set_state(OrderForm.service)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🟢 Функциональный тренинг (10$)",
                  callback_data="srv_Функциональный_тренинг_10",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔥 Кардио и выносливость (20$)",
                  callback_data="srv_Кардио_и_выносливость_20",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🥊 Бокс и единоборства (50$)", callback_data="srv_Бокс_и_единоборства_50"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🧎‍♂️ Растяжка и гибкость (70$)",
                  callback_data="srv_Растяжка_и_гибкость_70",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🦴 Коррекция осанки (90$)",
                  callback_data="srv_Коррекция_осанки_90",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🦿 Реабилитационный фитнес (110$)",
                  callback_data="srv_Реабилитационный_фитнес_110",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🏥 Спортивный массаж (130$)",
                  callback_data="srv_Спортивный_массаж_130",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🪓 Набор мышечной массы (150$)",
                  callback_data="srv_Набор_мышечной_массы_150",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🏹 Жиросжигание и сушка (180$)",
                  callback_data="srv_Жиросжигание_и_сушка_180",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🌳 Круговая на свежем воздухе (200$)", callback_data="srv_Круговая_на_свежем_воздухе_200"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="start_order"
              )
          ],
      ]
  )
  await callback.message.edit_text(
      f"<b>Шаг 2 из 9: Выбор программы тренировок</b>\n\nУчастников: <b>{count}</b>"
      f" (+{price}$)\nВыберите нужную программу:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
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
          [
              InlineKeyboardButton(
                  text="📸 Фотоотчет тренера (Бесплатно)", callback_data="addon_photo_0"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🎥 Видеоразбор техники (+10$)", callback_data="addon_video_10"
              )
          ],
      ]
  )
  await callback.message.edit_text(
      f"<b>Шаг 3 из 9: Дополнительные материалы</b>\n\nВыбрана программа: <b>{service_name}</b>"
      f" ({price}$)\n\nВыберите тип отчета о тренировке:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.addon, F.data.startswith("addon_"))
async def process_addon(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  addon_price = int(parts[-1])
  addon_type = "Видеоразбор техники" if addon_price > 0 else "Фотоотчет тренера"

  await state.update_data(addon_type=addon_type, addon_price=addon_price)
  await state.set_state(OrderForm.country)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🇺🇦 Украина", callback_data="country_Ukraine"
              ),
              InlineKeyboardButton(
                  text="🇷🇺 Россия", callback_data="country_Russia"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇧🇾 Беларусь", callback_data="country_Belarus"
              ),
              InlineKeyboardButton(
                  text="🇰🇿 Казахстан", callback_data="country_Kazakhstan"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇲🇩 Молдова", callback_data="country_Moldova"
              ),
              InlineKeyboardButton(
                  text="🇺🇿 Узбекистан", callback_data="country_Uzbekistan"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇰🇬 Кыргызстан", callback_data="country_Kyrgyzstan"
              ),
              InlineKeyboardButton(
                  text="🇹🇯 Таджикистан", callback_data="country_Tajikistan"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇬🇪 Грузия", callback_data="country_Georgia"
              ),
              InlineKeyboardButton(
                  text="🇦🇲 Армения", callback_data="country_Armenia"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇦🇿 Азербайджан", callback_data="country_Azerbaijan"
              )
          ],
      ]
  )
  await callback.message.edit_text(
      "<b>Шаг 4 из 9: Выбор страны</b>\n\nВыберите страну проведения тренировок:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.country, F.data.startswith("country_"))
async def process_country(callback: CallbackQuery, state: FSMContext):
  c_code = callback.data.split("_")[1]
  names = {
      "Ukraine": "Украина",
      "Russia": "Россия",
      "Belarus": "Беларусь",
      "Kazakhstan": "Казахстан",
      "Moldova": "Молдова",
      "Uzbekistan": "Узбекистан",
      "Kyrgyzstan": "Кыргызстан",
      "Tajikistan": "Таджикистан",
      "Georgia": "Грузия",
      "Armenia": "Армения",
      "Azerbaijan": "Азербайджан",
  }
  selected = names.get(c_code, c_code)

  curr_info = COUNTRY_CURRENCIES.get(
      selected, {"code": "USD", "symbol": "USD"}
  )
  await state.update_data(
      country=selected,
      curr_code=curr_info["code"],
      curr_symbol=curr_info["symbol"],
  )

  data = await state.get_data()
  target_count = data.get("target_count", 1)

  await state.set_state(OrderForm.target_info)
  info_prompt = (
      f"<b>Шаг 5 из 9: Данные участников</b>\n\nСтрана: <b>{selected}</b>\n\n"
      f"{'Введите ФИО, возраст, рост и уровень подготовки участников:' if target_count > 1 else 'Введите ваши ФИО, возраст, рост и текущий уровень подготовки (можно просто имя):'}"
  )
  await callback.message.edit_text(info_prompt, parse_mode="HTML")
  await callback.answer()


@router.message(OrderForm.target_info, F.text)
async def process_target_info(message: Message, state: FSMContext):
  await state.update_data(target_info=message.text.strip())
  await state.set_state(OrderForm.city)
  data = await state.get_data()
  target_count = data.get("target_count", 1)
  city_prompt = (
      f"<b>Шаг 6 из 9: Город</b>\n\n"
      f"{'Введите названия городов проведения занятий:' if target_count > 1 else 'Введите название вашего города:'}"
  )
  await message.answer(city_prompt, parse_mode="HTML")


@router.message(OrderForm.city, F.text)
async def process_city(message: Message, state: FSMContext):
  await state.update_data(city=message.text.strip())
  await state.set_state(OrderForm.address)
  data = await state.get_data()
  target_count = data.get("target_count", 1)
  address_prompt = (
      f"<b>Шаг 7 из 9: Локация / Адрес зала</b>\n\nГород:"
      f" <b>{message.text.strip()}</b>\n\n"
      f"{'Введите предпочтительные адреса залов или студий:' if target_count > 1 else 'Введите желаемый адрес зала или укажите онлайн-тренировка:'}"
  )
  await message.answer(address_prompt, parse_mode="HTML")


@router.message(OrderForm.address, F.text)
async def process_address(message: Message, state: FSMContext):
  await state.update_data(address=message.text.strip(), photos=[])
  await state.set_state(OrderForm.photos)
  data = await state.get_data()
  target_count = data.get("target_count", 1)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Готово / Без фото", callback_data="finish_photos"
              )
          ]
      ]
  )
  photos_prompt = (
      f"<b>Шаг 8 из 9: Фото формы / Справка (опционально)</b>\n\n📷 Отправьте фото текущей физической формы или медицинской справки (если требуется). Как закончите,"
      " нажмите «Готово»:"
  )
  await message.answer(photos_prompt, reply_markup=keyboard, parse_mode="HTML")


@router.message(OrderForm.photos, F.photo)
async def process_photo(message: Message, state: FSMContext):
  data = await state.get_data()
  photos = data.get("photos", [])
  if len(photos) >= 3:
    await message.answer("⚠️ Уже добавлено 3 фото. Нажмите кнопку «Готово».")
    return

  photos.append(message.photo[-1].file_id)
  await state.update_data(photos=photos)

  btn_text = (
      f"✅ Готово ({len(photos)}/3)"
      if len(photos) < 3
      else "✅ Готово (3/3) — Далее"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text=btn_text, callback_data="finish_photos"
              )
          ]
      ]
  )
  await message.answer(
      f"📸 Фото #{len(photos)} загружено!", reply_markup=keyboard
  )


@router.message(OrderForm.photos, ~F.photo)
async def process_photo_invalid(message: Message, state: FSMContext):
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Готово / Пропустить", callback_data="finish_photos"
              )
          ]
      ]
  )
  await message.answer(
      "⚠️ Пожалуйста, отправьте фотографию или нажмите кнопку «Готово / Без фото», чтобы продолжить.",
      reply_markup=keyboard
  )


@router.callback_query(OrderForm.photos, F.data == "finish_photos")
async def finish_photos(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.task)
  await callback.message.edit_text(
      "<b>Шаг 9 из 9: Пожелания и цели</b>\n\nПодробно опишите ваши спортивные цели"
      " (например: похудеть, набрать мышечную массу, сесть на шпагат) и удобное время для занятий:",
      parse_mode="HTML",
  )
  await callback.answer()


@router.message(OrderForm.task, F.text)
async def process_task(message: Message, state: FSMContext):
  await state.update_data(task=message.text.strip())
  await state.set_state(OrderForm.urgency)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="⚡ Срочный старт (сегодня) (+10$)",
                  callback_data="urg_fast_10",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🕐 Завтра (+5$)", callback_data="urg_tomorrow_5"
              )
          ],
          [
              InlineKeyboardButton(
                  text="📅 По расписанию (Бесплатно)", callback_data="urg_standard_0"
              )
          ],
      ]
  )
  await message.answer(
      "<b>График и срочность</b>\n\nВыберите желаемое время начала тренировок:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )


@router.callback_query(OrderForm.urgency, F.data.startswith("urg_"))
async def process_urgency(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  urg_type = parts[1]
  urg_price = int(parts[2])

  urg_map = {
      "fast": "⚡ Срочный старт (сегодня)",
      "tomorrow": "🕐 Завтра",
      "standard": "📅 По расписанию",
  }
  urgency = urg_map.get(urg_type, "📅 По расписанию")

  await state.update_data(urgency=urgency, urgency_price=urg_price)

  data = await state.get_data()
  service_price = data.get("service_price", 0)
  target_count = data.get("target_count", 1)
  target_count_price = data.get("target_count_price", 0)
  addon_price = data.get("addon_price", 0)
  total_price = (
      service_price + target_count_price + addon_price + urg_price
  )

  await state.update_data(total_price=total_price)

  curr_code = data.get("curr_code", "USD")
  curr_symbol = data.get("curr_symbol", "USD")
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  urgency_display = (
      f"{urgency} (+{urg_price}$)"
      if urg_price > 0
      else f"{urgency} (Бесплатно)"
  )

  summary_text = (
      "🎉 <b>Ваша заявка на тренировки успешно сформирована!</b>\n\n"
      "📋 <b>Сводка абонемента:</b>\n"
      f"• Участников: <b>{target_count} чел.</b>"
      + (
          f" (+{target_count_price}$)"
          if target_count_price > 0
          else " (Базово)"
      )
      + f"\n• Программа: <b>{data.get('service_name')}</b> ({service_price}$)\n"
      f"• Отчет: <b>{data.get('addon_type')}</b> ({addon_price}$)\n"
      f"• Старт: <b>{urgency_display}</b>\n"
      f"• Страна: <b>{data.get('country')}</b>\n"
      f"• Данные: <b>{data.get('target_info')}</b>\n"
      f"• Город: <b>{data.get('city')}</b>\n"
      f"• Адрес зала: <b>{data.get('address')}</b>\n"
      f"• Фотографий: <b>{len(data.get('photos', []))}</b>\n"
      f"• Цели и пожелания: <b>{data.get('task')}</b>\n\n"
      f"💵 <b>Итого к оплате:</b> <b>{total_price}$</b>\n"
      f"<i>(≈ {local_str} {curr_symbol})</i>\n\n"
      "⚠️ <b>Оплата принимается банковской картой, СБП или Telegram Stars!</b>\n"
      "Нажмите кнопку ниже для перехода к оплате 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💳 Оплатить абонемент", callback_data="go_to_payment")]
      ]
  )

  await callback.message.edit_text(
      summary_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ПЕРЕХОД К ВЫБОРУ ОПЛАТЫ С МОМЕНТАЛЬНЫМ ПАРАЛЛЕЛЬНЫМ ПРЕФЕТЧЕМ ===
@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id
  
  if user_id in ABANDONED_CHECKOUTS:
    ABANDONED_CHECKOUTS[user_id].cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(delayed_abandoned_cart_reminder(callback.bot, user_id))

  data = await state.get_data()
  total_price = data.get("total_price", 250)
  country = data.get("country", "")
  curr_symbol = data.get("curr_symbol", "USD")
  curr_code = data.get("curr_code", "USD")
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)
  local_str = f"{local_amount:,}".replace(",", " ")
  
  rub_amount = max(100, int(total_price * 90))
  stars_amount = max(1, rub_amount)

  SAFARIPAY_PREFETCH_CACHE[user_id] = {
      "sbp": asyncio.create_task(create_safaripay_invoice(rub_amount, "sbp")),
      "stars": asyncio.create_task(create_safaripay_invoice(rub_amount, "stars"))
  }

  base_text = (
      "💳 <b>ОПЛАТА АБОНЕМЕНТА</b>\n\n"
      f"• <b>Сумма к оплате:</b> {total_price}$ <i>(≈ {local_str}"
      f" {curr_symbol})</i>\n\n"
      "⚠️ Выберите удобный способ оплаты ниже 👇"
  )

  def get_payment_keyboard(timer_active=False, time_left="20:00"):
    kb_buttons = []
    
    if country == "Украина":
      kb_buttons.append([
          InlineKeyboardButton(
              text=f"⭐ Telegram Stars ({stars_amount} ⭐)",
              callback_data="pay_safari_stars",
          )
      ])
    else:
      kb_buttons.append([
          InlineKeyboardButton(
              text=f"💳 Карта / СБП (~{rub_amount} ₽)",
              callback_data="pay_sbp",
          )
      ])
      kb_buttons.append([
          InlineKeyboardButton(
              text=f"⭐ Telegram Stars ({stars_amount} ⭐)",
              callback_data="pay_safari_stars",
          )
      ])

    kb_buttons.append([
        InlineKeyboardButton(
            text="🪙 Крипта USDT / TON",
            callback_data="crypto_expert",
        )
    ])
    return InlineKeyboardMarkup(inline_keyboard=kb_buttons)

  initial_text = f"⏳ <b>Успейте оплатить! До окончания бронирования осталось: <u>20:00</u></b>\n\n{base_text}"
  await callback.message.edit_text(initial_text, reply_markup=get_payment_keyboard(), parse_mode="HTML")
  
  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
  
  PAYMENT_TIMERS[task_key] = asyncio.create_task(
      run_payment_timer(callback.bot, chat_id, message_id, base_text, get_payment_keyboard)
  )
  
  await callback.answer()


# === ОПЛАТА КАРТОЙ / СБП ===
@router.callback_query(F.data == "pay_sbp")
async def process_pay_sbp(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id

  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()
  total_price = data.get("total_price", 250)
  rub_amount = max(100, int(total_price * 90))

  user_cache = SAFARIPAY_PREFETCH_CACHE.get(user_id, {})
  pref_task = user_cache.get("sbp")
  
  pay_url = ""
  if pref_task:
    try:
      pay_url = await pref_task
    except Exception:
      pass

  if not pay_url:
    pay_url = await create_safaripay_invoice(rub_amount, "sbp")
    
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)

  if not pay_url:
    fail_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="pay_sbp")],
            [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")],
            [InlineKeyboardButton(text="⬅️ Назад к выбору", callback_data="go_to_payment")]
        ]
    )
    await callback.message.edit_text(
        "⚠️ <b>Временная задержка платежного шлюза</b>\n\n"
        "Мы уже работаем над устранением. Нажмите «Попробовать еще раз» или обратитесь в поддержку.",
        reply_markup=fail_kb,
        parse_mode="HTML"
    )
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить СБП / Картой", url=pay_url)],
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), проверить",
                  callback_data="safaripay_check_success",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору", callback_data="go_to_payment"
              )
          ],
      ]
  )
  text = (
      "💳 <b>Оплата картой / СБП (Safari Pay)</b>\n\n"
      f"Сумма: <b>{rub_amount} ₽</b>\n\n"
      "Ссылка готова! Нажмите кнопку ниже для перевода."
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "noop")
async def process_noop(callback: CallbackQuery):
  await callback.answer("", show_alert=False)


# === ОПЛАТА ЗВЁЗДАМИ ===
@router.callback_query(F.data == "pay_safari_stars")
async def process_pay_safari_stars(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id

  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()
  total_price = data.get("total_price", 250)
  rub_amount = max(1, int(total_price * 90))

  user_cache = SAFARIPAY_PREFETCH_CACHE.get(user_id, {})
  pref_task = user_cache.get("stars")
  
  pay_url = ""
  if pref_task:
    try:
      pay_url = await pref_task
    except Exception:
      pass

  if not pay_url:
    pay_url = await create_safaripay_invoice(rub_amount, "stars")
    
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)

  if not pay_url:
    fail_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="pay_safari_stars")],
            [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")],
            [InlineKeyboardButton(text="⬅️ Назад к выбору", callback_data="go_to_payment")]
        ]
    )
    await callback.message.edit_text(
        "⚠️ <b>Временная задержка шлюза Telegram Stars</b>\n\n"
        "Пожалуйста, повторите попытку через секунду или выберите другой способ оплаты.",
        reply_markup=fail_kb,
        parse_mode="HTML"
    )
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⭐ Оплатить звёздами (Safari Pay)", url=pay_url)],
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), проверить",
                  callback_data="safaripay_check_success",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору", callback_data="go_to_payment"
              )
          ],
      ]
  )
  text = (
      "⭐ <b>Оплата Telegram Звёздами (Safari Pay)</b>\n\n"
      f"Сумма: <b>{rub_amount} ⭐</b>\n\n"
      "Счет готов! Нажмите кнопку ниже для оплаты."
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "safaripay_check_success")
async def process_safaripay_check(callback: CallbackQuery, state: FSMContext):
  user = callback.from_user
  data = await state.get_data()

  order_info = {
      "service_name": data.get("service_name"),
      "total_price": data.get("total_price"),
      "status": "⏳ Ожидает подтверждения платежа",
  }
  if user.id not in USER_ORDERS:
    USER_ORDERS[user.id] = []
  USER_ORDERS[user.id].append(order_info)

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  admin_order_text = (
      f"💳 <b>Оплачен абонемент через Safari Pay!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Программа: <b>{data.get('service_name')}</b>\n"
      f"• Сумма: <b>{data.get('total_price')}$</b>\n"
      f"• Страна / Город: {data.get('country')} / {data.get('city')}"
  )
  if ADMIN_ID:
    try:
      await callback.bot.send_message(
          chat_id=ADMIN_ID, text=admin_order_text, parse_mode="HTML"
      )
    except Exception as e:
      logger.error(f"Не удалось отправить уведомление админу: {e}")

  final_text = (
      "✅ <b>Платеж обрабатывается!</b>\n\n"
      "Заявка принята. Администратор клуба проверит поступление средств и свяжется с вами в течение <b>15 минут</b> для назначения тренера."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]
      ]
  )
  await callback.message.edit_text(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


# === ВЫБОР СПОСОБА ОПЛАТЫ КРИПТОЙ ===
@router.callback_query(F.data == "crypto_expert")
async def process_crypto_expert(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id

  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  text = (
      "💳 <b>ВЫБОР КРИПТОВАЛЮТНОЙ СЕТИ</b>\n\n"
      "Пожалуйста, выберите сеть для перевода средств:"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔴 USDT (TRC-20)", callback_data="pay_trc20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟢 USDT (BEP-20)", callback_data="pay_bep20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔵 TON", callback_data="pay_ton"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟠 Bitcoin (BTC)", callback_data="pay_btc"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад", callback_data="go_to_payment"
              )
          ],
      ]
  )

  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


# === ОПЛАТА TRC-20 ===
@router.callback_query(F.data == "pay_trc20")
async def process_pay_trc20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 250)
  curr_symbol = data.get("curr_symbol", "USD")
  curr_code = data.get("curr_code", "USD")
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  payment_text = (
      "💳 <b>ОПЛАТА USDT (TRC-20)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$ <i>(≈ {local_str} {curr_symbol})</i>\n"
      "• <b>Сеть:</b> TRC-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети TRC-20. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека абонемент не будет активирован.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert"
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ОПЛАТА BEP-20 ===
@router.callback_query(F.data == "pay_bep20")
async def process_pay_bep20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 250)
  curr_symbol = data.get("curr_symbol", "USD")
  curr_code = data.get("curr_code", "USD")
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  payment_text = (
      "💳 <b>ОПЛАТА USDT (BEP-20)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$ <i>(≈ {local_str} {curr_symbol})</i>\n"
      "• <b>Сеть:</b> BEP-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети BEP-20. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека абонемент не будет активирован.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert"
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ОПЛАТА TON ===
@router.callback_query(F.data == "pay_ton")
async def process_pay_ton(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 250)
  curr_symbol = data.get("curr_symbol", "USD")
  curr_code = data.get("curr_code", "USD")
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  payment_text = (
      "💳 <b>ОПЛАТА TON</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$ <i>(≈ {local_str} {curr_symbol})</i>\n"
      "• <b>Сеть:</b> TON\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети TON. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека абонемент не будет активирован.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert"
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ОПЛАТА BITCOIN (BTC) ===
@router.callback_query(F.data == "pay_btc")
async def process_pay_btc(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 250)
  curr_symbol = data.get("curr_symbol", "USD")
  curr_code = data.get("curr_code", "USD")
  rate = await get_exchange_rate(curr_code)
  local_amount = round(total_price * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  payment_text = (
      "💳 <b>ОПЛАТА BITCOIN (BTC)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$ <i>(≈ {local_str} {curr_symbol})</i>\n"
      "• <b>Сеть:</b> Bitcoin\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети Bitcoin. После оплаты обязательно"
      " нажмите кнопку ниже и отправьте чек! Без чека абонемент не будет активирован.</i>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ask_for_receipt",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert"
              )
          ],
      ]
  )

  await callback.message.edit_text(
      payment_text, reply_markup=keyboard, parse_mode="HTML"
  )
  await callback.answer()


# === ЗАПРОС ЧЕКА У ПОЛЬЗОВАТЕЛЯ ===
@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.receipt)
  text = (
      "🧾 <b>Подтверждение оплаты</b>\n\n"
      "Пожалуйста, отправьте в чат <b>фотографию или скриншот чека</b> о"
      " переводе (без чека система не активирует абонемент)."
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


# === ПОЛУЧЕНИЕ ЧЕКА И СОХРАНЕНИЕ ===
@router.message(OrderForm.receipt, F.photo)
async def process_receipt_photo(message: Message, state: FSMContext):
  user = message.from_user
  user_id = user.id
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()

  order_info = {
      "service_name": data.get("service_name"),
      "total_price": data.get("total_price"),
      "status": "⏳ Ожидает проверки платежа",
  }

  if user_id not in USER_ORDERS:
    USER_ORDERS[user_id] = []
  USER_ORDERS[user_id].append(order_info)

  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_order_text = (
      f"🛒 <b>Новый оплаченный абонемент!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Программа: <b>{data.get('service_name')}</b>\n"
      f"• Сумма: <b>{data.get('total_price')}$</b>\n"
      f"• Страна / Город: {data.get('country')} / {data.get('city')}\n"
      f"• Данные: {data.get('target_info')}\n"
      f"• Цели: {data.get('task')}"
  )

  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(
          chat_id=ADMIN_ID,
          photo=photo_id,
          caption=admin_order_text,
          parse_mode="HTML",
      )
    except Exception as e:
      logger.error(f"Не удалось отправить заказ админу: {e}")

  final_text = (
      "✅ <b>Заявка и чек успешно приняты!</b>\n\n"
      "Администратор проверяет поступление средств. С вами свяжутся в"
      " течение <b>15 минут</b> для закрепления тренера."
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home"
              )
          ]
      ]
  )

  await message.answer(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.message(OrderForm.receipt, ~F.photo)
async def process_receipt_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию или скриншот чека</b>"
      " (картинкой), текстовые сообщения в качестве чека не принимаются!"
  )


# =====================================================================
# === АНКЕТА ДЛЯ ТРУДОУСТРОЙСТВА ТРЕНЕРОМ ==============================
# =====================================================================


@router.callback_query(F.data == "start_vacancy")
async def process_start_vacancy(callback: CallbackQuery, state: FSMContext):
  await state.clear()
  await state.set_state(VacancyForm.country)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🇺🇦 Украина", callback_data="vcountry_Ukraine"
              ),
              InlineKeyboardButton(
                  text="🇷🇺 Россия", callback_data="vcountry_Russia"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇧🇾 Беларусь", callback_data="vcountry_Belarus"
              ),
              InlineKeyboardButton(
                  text="🇰🇿 Казахстан", callback_data="vcountry_Kazakhstan"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇲🇩 Молдова", callback_data="vcountry_Moldova"
              ),
              InlineKeyboardButton(
                  text="🇺🇿 Узбекистан", callback_data="vcountry_Uzbekistan"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇰🇬 Кыргызстан", callback_data="vcountry_Kyrgyzstan"
              ),
              InlineKeyboardButton(
                  text="🇹🇯 Таджикистан", callback_data="vcountry_Tajikistan"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇬🇪 Грузия", callback_data="vcountry_Georgia"
              ),
              InlineKeyboardButton(
                  text="🇦🇲 Армения", callback_data="vcountry_Armenia"
              ),
          ],
          [
              InlineKeyboardButton(
                  text="🇦🇿 Азербайджан", callback_data="vcountry_Azerbaijan"
              )
          ],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(
      "💼 <b>АНКЕТА ТРЕНЕРА / ИНСТРУКТОРА</b>\n\n<b>Шаг 1 из 8: Страна проживания</b>\nВыберите"
      " вашу страну из списка:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(VacancyForm.country, F.data.startswith("vcountry_"))
async def process_vacancy_country(callback: CallbackQuery, state: FSMContext):
  c_code = callback.data.split("_")[1]
  names = {
      "Ukraine": "Украина",
      "Russia": "Россия",
      "Belarus": "Беларусь",
      "Kazakhstan": "Казахстан",
      "Moldova": "Молдова",
      "Uzbekistan": "Узбекистан",
      "Kyrgyzstan": "Кыргызстан",
      "Tajikistan": "Таджикистан",
      "Georgia": "Грузия",
      "Armenia": "Армения",
      "Azerbaijan": "Азербайджан",
  }
  selected = names.get(c_code, c_code)
  curr_info = COUNTRY_CURRENCIES.get(
      selected, {"code": "USD", "symbol": "USD"}
  )
  await state.update_data(
      country=selected,
      curr_code=curr_info["code"],
      curr_symbol=curr_info["symbol"],
  )
  await state.set_state(VacancyForm.city)
  await callback.message.edit_text(
      f"💼 <b>АНКЕТА ТРЕНЕРА</b>\n\nСтрана: <b>{selected}</b>\n\n<b>Шаг 2 из"
      " 8: Город</b>\nВведите название вашего города текстом:",
      parse_mode="HTML",
  )
  await callback.answer()


@router.message(VacancyForm.city, F.text)
async def process_vacancy_city(message: Message, state: FSMContext):
  await state.update_data(city=message.text.strip())
  await state.set_state(VacancyForm.name)
  await message.answer(
      "💼 <b>АНКЕТА ТРЕНЕРА</b>\n\n<b>Шаг 3 из 8: Имя и Фамилия</b>\nВведите ваши данные:",
      parse_mode="HTML",
  )


@router.message(VacancyForm.name, F.text)
async def process_vacancy_name(message: Message, state: FSMContext):
  await state.update_data(name=message.text.strip())
  await state.set_state(VacancyForm.age)
  await message.answer(
      "💼 <b>АНКЕТА ТРЕНЕРА</b>\n\n<b>Шаг 4 из 8: Возраст</b>\nУкажите сколько"
      " вам лет:",
      parse_mode="HTML",
  )


@router.message(VacancyForm.age, F.text)
async def process_vacancy_age(message: Message, state: FSMContext):
  await state.update_data(age=message.text.strip())
  await state.set_state(VacancyForm.parameters)
  await message.answer(
      "💼 <b>АНКЕТА ТРЕНЕРА</b>\n\n<b>Шаг 5 из 8: Специализация</b>\nУкажите"
      " ваше направление (например: <i>кроссфит, тяжелая атлетика, йога, нутрициология</i>):",
      parse_mode="HTML",
  )


@router.message(VacancyForm.parameters, F.text)
async def process_vacancy_parameters(message: Message, state: FSMContext):
  await state.update_data(parameters=message.text.strip())
  await state.set_state(VacancyForm.sports_exp)
  await message.answer(
      "💼 <b>АНКЕТА ТРЕНЕРА</b>\n\n<b>Шаг 6 из 8: Спортивные достижения и сертификаты</b>\nКаковы ваши успехи в спорте, есть ли профильное образование и сертификаты?",
      parse_mode="HTML",
  )


@router.message(VacancyForm.sports_exp, F.text)
async def process_vacancy_sports_exp(message: Message, state: FSMContext):
  await state.update_data(sports_exp=message.text.strip())
  await state.set_state(VacancyForm.experience)
  await message.answer(
      "💼 <b>АНКЕТА ТРЕНЕРА</b>\n\n<b>Шаг 7 из 8: Опыт работы тренером</b>\nБыл ли у вас"
      " опыт работы в фитнес-клубах или онлайн? Кратко опишите стаж:",
      parse_mode="HTML",
  )


@router.message(VacancyForm.experience, F.text)
async def process_vacancy_experience(message: Message, state: FSMContext):
  await state.update_data(experience=message.text.strip())
  await state.set_state(VacancyForm.photo)
  await message.answer(
      "💼 <b>АНКЕТА ТРЕНЕРА</b>\n\n<b>Шаг 8 из 8: Фото сертификатов или портфолио</b>\n📸 Прикрепите фото ваших сертификатов, дипломов или рабочее фото.",
      parse_mode="HTML",
  )


@router.message(VacancyForm.photo, F.photo)
async def process_vacancy_photo(message: Message, state: FSMContext):
  photo_id = message.photo[-1].file_id
  await state.update_data(photo=photo_id)

  data = await state.get_data()
  curr_code = data.get("curr_code", "USD")
  curr_symbol = data.get("curr_symbol", "USD")
  rate = await get_exchange_rate(curr_code)
  deposit_usd = 50
  local_amount = round(deposit_usd * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  text = (
      "✅ <b>Анкета тренера успешно заполнена!</b>\n\n"
      "Для верификации аккаунта в системе клуба и получения доступа к клиентской базе необходимо внести страховой взнос в"
      " размере <b>50$</b> <i>(≈ "
      f"{local_str} {curr_symbol})</i>.\n\n"
      "⚠️ Взнос возвратный. После проверки с вами свяжется старший тренер."
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="💳 Внести взнос (Оплатить 50$)",
                  callback_data="vacancy_pay",
              )
          ]
      ]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(VacancyForm.photo, ~F.photo)
async def process_vacancy_photo_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию</b> (картинкой), текстовые"
      " сообщения не принимаются!"
  )


@router.callback_query(F.data == "vacancy_pay")
async def process_vacancy_pay(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  curr_code = data.get("curr_code", "USD")
  curr_symbol = data.get("curr_symbol", "USD")
  rate = await get_exchange_rate(curr_code)
  deposit_usd = 50
  local_amount = round(deposit_usd * rate)
  local_str = f"{local_amount:,}".replace(",", " ")

  text = (
      "💳 <b>ОПЛАТА ВЗНОСА ДЛЯ ТРЕНЕРОВ</b>\n\n"
      f"• <b>Сумма к оплате:</b> {deposit_usd}$ <i>(≈ {local_str}"
      f" {curr_symbol})</i>\n"
      "• <b>Выберите способ оплаты:</b>"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔴 USDT (TRC-20)", callback_data="vpay_trc20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟢 USDT (BEP-20)", callback_data="vpay_bep20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔵 TON", callback_data="vpay_ton"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟠 Bitcoin (BTC)", callback_data="vpay_btc"
              )
          ],
          [
              InlineKeyboardButton(
                  text="⬅️ На главную", callback_data="back_home"
              )
          ],
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "vpay_trc20")
async def vpay_trc20(callback: CallbackQuery):
  text = (
      "💳 <b>ОПЛАТА ВЗНОСА (TRC-20)</b>\n\n"
      "• <b>Сумма:</b> 50$\n"
      "• <b>Сеть:</b> TRC-20\n"
      "• <b>Реквизиты для перевода:</b>\n"
      "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "После перевода обязательно нажмите кнопку ниже и отправьте скриншот"
      " чека!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="vacancy_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "vpay_bep20")
async def vpay_bep20(callback: CallbackQuery):
  text = (
      "💳 <b>ОПЛАТА ВЗНОСА (BEP-20)</b>\n\n"
      "• <b>Сумма:</b> 50$\n"
      "• <b>Сеть:</b> BEP-20\n"
      "• <b>Реквизиты для перевода:</b>\n"
      "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "После перевода обязательно нажмите кнопку ниже и отправьте скриншот"
      " чека!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="vacancy_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "vpay_ton")
async def vpay_ton(callback: CallbackQuery):
  text = (
      "💳 <b>ОПЛАТА ВЗНОСА (TON)</b>\n\n"
      "• <b>Сумма:</b> 50$\n"
      "• <b>Сеть:</b> TON\n"
      "• <b>Реквизиты для перевода:</b>\n"
      "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "После перевода обязательно нажмите кнопку ниже и отправьте скриншот"
      " чека!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="vacancy_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "vpay_btc")
async def vpay_btc(callback: CallbackQuery):
  text = (
      "💳 <b>ОПЛАТА ВЗНОСА (BITCOIN)</b>\n\n"
      "• <b>Сумма:</b> 50$\n"
      "• <b>Сеть:</b> Bitcoin\n"
      "• <b>Реквизиты для перевода:</b>\n"
      "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "После перевода обязательно нажмите кнопку ниже и отправьте скриншот"
      " чека!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="vacancy_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "vacancy_ask_receipt")
async def vacancy_ask_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(VacancyForm.receipt)
  text = (
      "🧾 <b>Подтверждение взноса</b>\n\n"
      "Пожалуйста, отправьте в чат <b>фотографию или скриншот чека</b> об"
      " успешном переводе."
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


@router.message(VacancyForm.receipt, F.photo)
async def process_vacancy_receipt(message: Message, state: FSMContext):
  user = message.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  admin_vacancy_text = (
      f"💼 <b>Новый взнос от тренера!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Чек прикреплен ниже."
  )
  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(
          chat_id=ADMIN_ID,
          photo=photo_id,
          caption=admin_vacancy_text,
          parse_mode="HTML",
      )
    except Exception as e:
      logger.error(f"Не удалось отправить залог вакансии админу: {e}")

  final_text = (
      "✅ <b>Анкета тренера и чек успешно приняты!</b>\n\n"
      "Ваши данные переданы руководству клуба на проверку. В ближайшее время с вами"
      " свяжется администратор."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home"
              )
          ]
      ]
  )
  await message.answer(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.message(VacancyForm.receipt, ~F.photo)
async def process_vacancy_receipt_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию или скриншот чека</b>"
      " (картинкой)!"
  )


# =====================================================================
# === ПОКУПКА РЕКЛАМЫ (СОТРУДНИЧЕСТВО) =================================
# =====================================================================


@router.callback_query(F.data == "start_ads")
async def process_start_ads(callback: CallbackQuery, state: FSMContext):
  await state.clear()
  await state.set_state(AdForm.platform)
  text = (
      "📢 <b>ПОКУПКА РЕКЛАМЫ И СОТРУДНИЧЕСТВО</b>\n\n"
      "Выберите площадку для размещения вашей рекламы:"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🤖 Реклама в боте",
                  callback_data="adplat_bot",
              )
          ],
          [
              InlineKeyboardButton(
                  text="📢 Реклама в канале",
                  callback_data="adplat_channel",
              )
          ],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(AdForm.platform, F.data.startswith("adplat_"))
async def process_ad_platform(callback: CallbackQuery, state: FSMContext):
  plat_code = callback.data.split("_")[1]
  platform_name = (
      "Реклама в боте" if plat_code == "bot" else "Реклама в канале"
  )
  await state.update_data(platform=platform_name, plat_code=plat_code)
  await state.set_state(AdForm.duration)

  total_users = 1384

  kb_list = [
      [InlineKeyboardButton(text="24 часа = 5$", callback_data="addur_24h_5")],
      [InlineKeyboardButton(text="3 дня = 10$", callback_data="addur_3d_10")],
      [InlineKeyboardButton(text="7 дней = 25$", callback_data="addur_7d_25")],
      [InlineKeyboardButton(text="30 дней = 100$", callback_data="addur_30d_100")],
  ]
  kb_list.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="start_ads")])
  keyboard = InlineKeyboardMarkup(inline_keyboard=kb_list)

  if plat_code == "bot":
    text = (
        f"🤖 <b>Площадка: Реклама в боте</b>\n"
        f"👥 Аудитория: <b>{total_users} активных спортсменов</b>\n\n"
        "<b>Выберите срок размещения рекламы:</b>"
    )
  else:
    text = (
        f"📢 <b>Площадка: Реклама в канале</b>\n\n"
        "<b>Выберите срок размещения рекламы:</b>"
    )

  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(AdForm.duration, F.data.startswith("addur_"))
async def process_ad_duration(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  duration_code = parts[1]
  price = int(parts[2])

  dur_map = {
      "24h": "24 часа",
      "3d": "3 дня",
      "7d": "7 дней",
      "30d": "30 дней",
  }
  duration_str = dur_map.get(duration_code, duration_code)

  await state.update_data(duration=duration_str, ad_price=price)
  await state.set_state(AdForm.niche)

  text = (
      f"📢 <b>Выбрано: {duration_str} — {price}$</b>\n\n"
      "<b>Шаг 3: Тематика проекта</b>\n"
      "Введите спортивную тематику или нишу вашего проекта текстом:"
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


@router.message(AdForm.niche, F.text)
async def process_ad_niche(message: Message, state: FSMContext):
  await state.update_data(niche=message.text.strip())
  await state.set_state(AdForm.link)
  await message.answer(
      "📢 <b>Шаг 4: Ссылка</b>\n\nВведите ссылку на ваш ресурс или канал:",
      parse_mode="HTML",
  )


@router.message(AdForm.link, F.text)
async def process_ad_link(message: Message, state: FSMContext):
  await state.update_data(link=message.text.strip())
  await state.set_state(AdForm.creative)
  await message.answer(
      "📢 <b>Шаг 5: Креатив</b>\n\n"
      "Отправьте рекламный креатив (изображение, текст или видео), который будет"
      " опубликован:",
      parse_mode="HTML",
  )


@router.message(AdForm.creative)
async def process_ad_creative(message: Message, state: FSMContext):
  if message.photo:
    creative_desc = f"[Фотография] {message.caption or ''}"
  elif message.video:
    creative_desc = f"[Видео] {message.caption or ''}"
  else:
    creative_desc = message.text or "[Текст]"

  await state.update_data(creative=creative_desc)
  data = await state.get_data()
  price = data.get("ad_price", 0)

  text = (
      "🎉 <b>Рекламная заявка успешно сформирована!</b>\n\n"
      f"• Площадка: <b>{data.get('platform')}</b>\n"
      f"• Срок: <b>{data.get('duration')}</b>\n"
      f"• Тематика: <b>{data.get('niche')}</b>\n"
      f"• Ссылка: <b>{data.get('link')}</b>\n"
      f"• Креатив: <b>{creative_desc}</b>\n\n"
      f"💵 <b>К оплате: {price}$</b>\n\n"
      "⚠️ <b>Оплата принимается через платежные шлюзы или криптовалюту!</b>\n"
      "Нажмите кнопку ниже для выбора способа оплаты 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="💳 Оплатить рекламу", callback_data="ad_pay_select"
              )
          ]
      ]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "ad_pay_select")
async def process_ad_pay_select(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  price = data.get("ad_price", 0)

  text = (
      f"💳 <b>ОПЛАТА РЕКЛАМЫ ({price}$)</b>\n\n"
      "Выберите способ оплаты:"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🔴 USDT (TRC-20)", callback_data="adpay_trc20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟢 USDT (BEP-20)", callback_data="adpay_bep20"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🔵 TON", callback_data="adpay_ton"
              )
          ],
          [
              InlineKeyboardButton(
                  text="🟠 Bitcoin (BTC)", callback_data="adpay_btc"
              )
          ],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "adpay_trc20")
async def adpay_trc20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  price = data.get("ad_price", 0)
  text = (
      f"💳 <b>ОПЛАТА РЕКЛАМЫ (TRC-20)</b>\n\n"
      f"• <b>Сумма:</b> {price}$\n"
      "• <b>Сеть:</b> TRC-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "Переводите строго точную сумму. После оплаты нажмите кнопку ниже и"
      " прикрепите чек!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ad_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "adpay_bep20")
async def adpay_bep20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  price = data.get("ad_price", 0)
  text = (
      f"💳 <b>ОПЛАТА РЕКЛАМЫ (BEP-20)</b>\n\n"
      f"• <b>Сумма:</b> {price}$\n"
      "• <b>Сеть:</b> BEP-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "Переводите строго точную сумму. После оплаты нажмите кнопку ниже и"
      " прикрепите чек!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ad_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "adpay_ton")
async def adpay_ton(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  price = data.get("ad_price", 0)
  text = (
      f"💳 <b>ОПЛАТА РЕКЛАМЫ (TON)</b>\n\n"
      f"• <b>Сумма:</b> {price}$\n"
      "• <b>Сеть:</b> TON\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "Переводите строго точную сумму. После оплаты нажмите кнопку ниже и"
      " прикрепите чек!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ad_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "adpay_btc")
async def adpay_btc(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  price = data.get("ad_price", 0)
  text = (
      f"💳 <b>ОПЛАТА РЕКЛАМЫ (BITCOIN)</b>\n\n"
      f"• <b>Сумма:</b> {price}$\n"
      "• <b>Сеть:</b> Bitcoin\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "Переводите строго точную сумму. После оплаты нажмите кнопку ниже и"
      " прикрепите чек!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="✅ Я оплатил(-а), прикрепить чек",
                  callback_data="ad_ask_receipt",
              )
          ]
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "ad_ask_receipt")
async def ad_ask_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(AdForm.receipt)
  text = (
      "🧾 <b>Подтверждение оплаты рекламы</b>\n\n"
      "Пожалуйста, отправьте в чат <b>фотографию или скриншот чека</b> о"
      " переводе."
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


@router.message(AdForm.receipt, F.photo)
async def process_ad_receipt(message: Message, state: FSMContext):
  user = message.from_user
  user_info = f"@{user.username}" if user.username else f"ID: {user.id}"
  if user.full_name:
    user_info = f"{user.full_name} ({user_info})"

  data = await state.get_data()
  admin_ad_text = (
      f"📢 <b>Новая оплата рекламы!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Площадка: {data.get('platform')}\n"
      f"• Срок: {data.get('duration')}\n"
      f"• Сумма: <b>{data.get('ad_price')}$</b>"
  )
  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(
          chat_id=ADMIN_ID,
          photo=photo_id,
          caption=admin_ad_text,
      )
    except Exception as e:
      logger.error(f"Не удалось отправить оплату рекламы админу: {e}")

  final_text = (
      "✅ <b>Заявка на рекламу и чек успешно приняты!</b>\n\n"
      "Ожидайте, модератор проверит поступление средств в течение <b>15"
      " минут</b> и свяжется с вами."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🏠 На главную", callback_data="back_home"
              )
          ]
      ]
  )
  await message.answer(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.message(AdForm.receipt, ~F.photo)
async def process_ad_receipt_invalid(message: Message):
  await message.answer(
      "⚠️ Пожалуйста, отправьте именно <b>фотографию чека</b> (картинкой)!"
  )


# === ВЕБ-СЕРВЕР ДЛЯ ЗАЩИТЫ ОТ СНА СЕРВЕРА (RENDER) ===
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
  asyncio.create_task(
      scheduled_broadcast(bot)
  )
  await bot.delete_webhook(drop_pending_updates=True)
  await dp.start_polling(bot)


if __name__ == "__main__":
  asyncio.run(main())
