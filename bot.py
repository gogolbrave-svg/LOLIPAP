import asyncio
import datetime
import html
import logging
import os
import random
import warnings
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import ClientSession, web
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
SAFARIPAY_MERCHANT_ID = os.getenv("SAFARIPAY_MERCHANT_ID")
SAFARIPAY_API_KEY = os.getenv("SAFARIPAY_API_KEY")

if not BOT_TOKEN:
  raise ValueError("❌ BOT_TOKEN не найден! Проверьте переменные окружения на Render.")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Подавление предупреждений unclosed connector
warnings.filterwarnings("ignore", category=ResourceWarning, message="unclosed connector")

admin_env = os.getenv("ADMIN_ID")
ADMIN_ID = int(admin_env) if admin_env and admin_env.isdigit() else None

# Глобальная сессия aiohttp для предотвращения ошибок коннектора
http_session: ClientSession = None


class OrderForm(StatesGroup):
  service = State()
  addon = State()
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
KNOWN_USERS = {}
PAYMENT_TIMERS = {}
ABANDONED_CHECKOUTS = {}
SAFARIPAY_PREFETCH_CACHE = {}

router = Router()


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
      f"Напоминаем, что скидка <b>-20%</b> на разработку любых цифровых услуг действует только до"
      f" <b>{target_day} {month_name}</b>!\n\n"
      "Успейте заказать Telegram-бота, сайт или мини-приложение на лучших условиях. Гарантируем чистый код, сроки и техподдержку.\n\n"
      "Ждем вас в боте 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💻 Заказать со скидкой -20%", callback_data="start_order")],
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
          "Мы заметили, что вы остановились на этапе оплаты, но так и не завершили заказ.\n"
          "Возникли вопросы по ТЗ или оплате? Наша поддержка всегда на связи!\n\n"
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
            chat_id=chat_id, message_id=message_id,
            text=current_text, reply_markup=keyboard, parse_mode="HTML"
        )
      except Exception:
        pass
      
      await asyncio.sleep(1)
      total_seconds -= 1
      
    expired_text = (
        "❌ <b>Время на оплату заказа истекло!</b>\n\n"
        "Бронь заказа была отменена. Пожалуйста, оформите заявку заново."
    )
    expired_kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🛒 Сделать новый заказ", callback_data="start_order")]]
    )
    try:
      await bot.edit_message_text(
          chat_id=chat_id, message_id=message_id,
          text=expired_text, reply_markup=expired_kb, parse_mode="HTML"
      )
    except Exception:
      pass
      
    if task_key in PAYMENT_TIMERS:
      del PAYMENT_TIMERS[task_key]
  except asyncio.CancelledError:
    pass


async def create_safaripay_invoice(amount: float, method: str) -> str:
  global http_session
  if not SAFARIPAY_API_KEY:
    logger.error("❌ SAFARIPAY_API_KEY не задан в переменных окружения!")
    return ""

  if not http_session or http_session.closed:
    http_session = ClientSession()

  url = "https://api.safaripay.pro/v1/invoices"
  headers = {"X-API-Key": SAFARIPAY_API_KEY, "Content-Type": "application/json"}
  payload = {"amount": float(amount), "method": method}
  if SAFARIPAY_MERCHANT_ID:
    payload["merchant_id"] = str(SAFARIPAY_MERCHANT_ID)

  for attempt in range(1, 4):
    try:
      async with http_session.post(f"{url}?wait=20", json=payload, headers=headers, timeout=15) as resp:
        if resp.status in (200, 201):
          data = await resp.json()
          link = data.get("link") or data.get("url") or data.get("pay_url", "")
          if link:
            return link
        else:
          resp_text = await resp.text()
          logger.warning(f"SafariPay вернул статус {resp.status} на попытке {attempt}: {resp_text}")
    except Exception as e:
      logger.error(f"Попытка {attempt}/3 ошибки запроса SafariPay ({method}): {e}")
    
    if attempt < 3:
      await asyncio.sleep(attempt)
  return ""


async def get_reputation_text_and_keyboard():
  current_date = datetime.date.today()
  base_date = datetime.date(2026, 1, 1)
  days_passed = max(0, (current_date - base_date).days)
  completed = min(142 + days_passed * 2, 500)

  rng = random.Random(current_date.toordinal())
  in_progress = rng.randint(5, 15)
  new_requests = rng.randint(3, 9)

  text = (
      "📊 <b>Наша статистика и репутация:</b>\n\n"
      f"• Успешных проектов: <b>{completed}</b>\n"
      f"• Проектов в работе: <b>{in_progress}</b>\n"
      f"• Заявок сегодня: <b>{new_requests}</b>\n\n"
      "<i>Мы создаем надежные IT-решения для бизнеса с чистым кодом и гарантией результата.</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]]
  )
  return text, keyboard


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
  await state.clear()
  user_id = message.from_user.id
  if user_id not in KNOWN_USERS:
    KNOWN_USERS[user_id] = datetime.datetime.now()

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="💻 Заказать услугу", callback_data="start_order")],
          [
              InlineKeyboardButton(text="📋 Мои заказы", callback_data="my_orders"),
              InlineKeyboardButton(text="📊 Наша репутация", callback_data="reputation"),
          ],
          [InlineKeyboardButton(text="🤝 Сотрудничество (Реклама)", callback_data="start_ads")],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin")],
      ]
  )
  text = (
      "<b>Добро пожаловать в студию разработки цифровых услуг!</b> 🚀\n\n"
      "Создаем Telegram-ботов, веб-сайты, лендинги, мини-приложения (Mini Apps) "
      "и автоматизируем бизнес-процессы под ключ.\n\n"
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
    text = "📋 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных заказов."
  else:
    text = "📋 <b>Ваши заказы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Заказ #{idx}</b>\n• Услуга: {html.escape(str(ord_data['service_name']))}\n"
          f"• Сумма: {ord_data['total_price']}$\n• Статус: {html.escape(str(ord_data['status']))}\n\n"
      )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(Command("order", "make_order"))
async def cmd_order(message: Message, state: FSMContext):
  await state.clear()
  await state.set_state(OrderForm.service)
  
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🎨 UI/UX Дизайн макета (10$)", callback_data="srv_UI_UX_Дизайн_макета_10")],
          [InlineKeyboardButton(text="🌐 Лендинг / Сайт-визитка (20$)", callback_data="srv_Лендинг_Сайт_визитка_20")],
          [InlineKeyboardButton(text="🤖 Telegram Бот под ключ (50$)", callback_data="srv_Telegram_Бот_под_ключ_50")],
          [InlineKeyboardButton(text="⚙️ Автоматизация процессов (70$)", callback_data="srv_Автоматизация_процессов_70")],
          [InlineKeyboardButton(text="⚡ Telegram Mini App (90$)", callback_data="srv_Telegram_Mini_App_90")],
          [InlineKeyboardButton(text="🛍 Интернет-магазин (110$)", callback_data="srv_Интернет_магазин_110")],
          [InlineKeyboardButton(text="📊 CRM-система под ключ (130$)", callback_data="srv_CRM_система_под_ключ_130")],
          [InlineKeyboardButton(text="🛡 Защита и безопасность (150$)", callback_data="srv_Защита_и_безопасность_150")],
          [InlineKeyboardButton(text="🚀 SEO и Продвижение (180$)", callback_data="srv_SEO_и_Продвижение_180")],
          [InlineKeyboardButton(text="💎 Комплексный проект VIP (200$)", callback_data="srv_Комплексный_проект_VIP_200")],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await message.answer(
      "<b>Шаг 1 из 3: Выбор цифровой услуги</b>\n\nВыберите нужную услугу для вашего проекта:",
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
      inline_keyboard=[[InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")]]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 1 из 2: Причина обращения</b>\n"
      "Пожалуйста, опишите ваш вопрос (техническое задание, консультация, вопрос по оплате и т.д.):"
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.message(SupportForm.reason, F.text)
async def process_support_reason(message: Message, state: FSMContext):
  await state.update_data(reason=message.text.strip())
  await state.set_state(SupportForm.photo)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⏩ Пропустить (Без скриншота)", callback_data="skip_support_photo")],
          [InlineKeyboardButton(text="⬅️ Отмена", callback_data="back_home")],
      ]
  )
  text = (
      "💬 <b>Служба поддержки</b>\n\n"
      "<b>Шаг 2 из 2: Прикрепить файл / скриншот</b>\n"
      "📸 Отправьте скриншот или пример (если есть), либо нажмите кнопку ниже, чтобы пропустить:"
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.message(SupportForm.photo, F.photo)
async def process_support_photo(message: Message, state: FSMContext):
  photo_id = message.photo[-1].file_id
  data = await state.get_data()
  reason = html.escape(data.get("reason", ""))
  user = message.from_user

  user_username = f"@{html.escape(user.username)}" if user.username else f"ID: {user.id}"
  user_full = html.escape(user.full_name) if user.full_name else ""
  user_info = f"{user_full} ({user_username})" if user_full else user_username

  admin_text = (
      f"📩 <b>Новое обращение в поддержку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {reason}\n\n"
      f"💬 <i>Чтобы ответить пользователю, просто сделайте Reply (ответ) на это сообщение в боте.</i>"
  )

  if ADMIN_ID:
    try:
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Ваше обращение успешно отправлено!</b>\n\n"
      "Студия разработки рассмотрит его и ответит вам в ближайшее время."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]
  )
  await message.answer(success_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.callback_query(SupportForm.photo, F.data == "skip_support_photo")
async def skip_support_photo(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  reason = html.escape(data.get("reason", ""))
  user = callback.from_user

  user_username = f"@{html.escape(user.username)}" if user.username else f"ID: {user.id}"
  user_full = html.escape(user.full_name) if user.full_name else ""
  user_info = f"{user_full} ({user_username})" if user_full else user_username

  admin_text = (
      f"📩 <b>Новое обращение в поддержку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Вопрос: {reason}\n\n"
      f"💬 <i>Чтобы ответить пользователю, просто сделайте Reply (ответ) на это сообщение в боте.</i>"
  )

  if ADMIN_ID:
    try:
      await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить обращение админу: {e}")

  success_text = (
      "✅ <b>Ваше обращение успешно отправлено!</b>\n\n"
      "Студия разработки рассмотрит его и ответит вам в ближайшее время."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]
  )
  await callback.message.edit_text(success_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()
  await callback.answer()


@router.message(SupportForm.photo, ~F.photo)
async def process_support_photo_invalid(message: Message):
  await message.answer("⚠️ Пожалуйста, отправьте именно <b>фотографию (картинкой)</b> или нажмите кнопку «Пропустить»!")


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
    await message.reply("❌ Не удалось определить ID пользователя из сообщения, на которое вы ответили.")
    return

  try:
    answer_text = (
        "💬 <b>Ответ от студии разработки:</b>\n\n"
        f"{html.escape(message.text or message.caption or '')}"
    )
    await message.bot.send_message(chat_id=target_user_id, text=answer_text, parse_mode="HTML")
    await message.reply("✅ Ответ успешно доставлен пользователю!")
  except Exception as e:
    await message.reply(f"❌ Ошибка отправки сообщения пользователю: {e}")


@router.callback_query(F.data == "my_orders")
async def show_my_orders(callback: CallbackQuery):
  user_id = callback.from_user.id
  orders = USER_ORDERS.get(user_id, [])

  if not orders:
    text = "📋 <b>Мои заказы</b>\n\nУ вас пока нет активных или завершенных заказов."
  else:
    text = "📋 <b>Ваши заказы:</b>\n\n"
    for idx, ord_data in enumerate(orders, 1):
      text += (
          f"<b>Заказ #{idx}</b>\n• Услуга: {html.escape(str(ord_data['service_name']))}\n"
          f"• Сумма: {ord_data['total_price']}$\n• Статус: {html.escape(str(ord_data['status']))}\n\n"
      )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_home")]]
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
          [InlineKeyboardButton(text="💻 Заказать услугу", callback_data="start_order")],
          [
              InlineKeyboardButton(text="📋 Мои заказы", callback_data="my_orders"),
              InlineKeyboardButton(text="📊 Наша репутация", callback_data="reputation"),
          ],
          [InlineKeyboardButton(text="🤝 Сотрудничество (Реклама)", callback_data="start_ads")],
          [InlineKeyboardButton(text="💬 Поддержка", callback_data="contact_admin")],
      ]
  )
  text = (
      "<b>Добро пожаловать в студию разработки цифровых услуг!</b> 🚀\n\n"
      "Создаем Telegram-ботов, веб-сайты, лендинги, мини-приложения (Mini Apps) "
      "и автоматизируем бизнес-процессы под ключ.\n\n"
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

  await state.set_state(OrderForm.service)
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🎨 UI/UX Дизайн макета (10$)", callback_data="srv_UI_UX_Дизайн_макета_10")],
          [InlineKeyboardButton(text="🌐 Лендинг / Сайт-визитка (20$)", callback_data="srv_Лендинг_Сайт_визитка_20")],
          [InlineKeyboardButton(text="🤖 Telegram Бот под ключ (50$)", callback_data="srv_Telegram_Бот_под_ключ_50")],
          [InlineKeyboardButton(text="⚙️ Автоматизация процессов (70$)", callback_data="srv_Автоматизация_процессов_70")],
          [InlineKeyboardButton(text="⚡ Telegram Mini App (90$)", callback_data="srv_Telegram_Mini_App_90")],
          [InlineKeyboardButton(text="🛍 Интернет-магазин (110$)", callback_data="srv_Интернет_магазин_110")],
          [InlineKeyboardButton(text="📊 CRM-система под ключ (130$)", callback_data="srv_CRM_система_под_ключ_130")],
          [InlineKeyboardButton(text="🛡 Защита и безопасность (150$)", callback_data="srv_Защита_и_безопасность_150")],
          [InlineKeyboardButton(text="🚀 SEO и Продвижение (180$)", callback_data="srv_SEO_и_Продвижение_180")],
          [InlineKeyboardButton(text="💎 Комплексный проект VIP (200$)", callback_data="srv_Комплексный_проект_VIP_200")],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(
      "<b>Шаг 1 из 3: Выбор цифровой услуги</b>\n\nВыберите нужную услугу для вашего проекта:",
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
          [InlineKeyboardButton(text="📄 Стандартное ТЗ (Бесплатно)", callback_data="addon_standard_0")],
          [InlineKeyboardButton(text="🚀 Приоритетная разработка (+50$)", callback_data="addon_priority_50")],
      ]
  )
  await callback.message.edit_text(
      f"<b>Шаг 2 из 3: Дополнительные опции</b>\n\nВыбрана услуга: <b>{html.escape(service_name)}</b>"
      f" ({price}$)\n\nВыберите скорость разработки:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.addon, F.data.startswith("addon_"))
async def process_addon(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  addon_price = int(parts[-1])
  addon_type = "Приоритетная разработка" if addon_price > 0 else "Стандартное ТЗ"

  await state.update_data(addon_type=addon_type, addon_price=addon_price)
  await state.set_state(OrderForm.urgency)

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⚡ Срочный старт (сегодня) (+30$)", callback_data="urg_fast_30")],
          [InlineKeyboardButton(text="🕐 В течение 3 дней (+15$)", callback_data="urg_soon_15")],
          [InlineKeyboardButton(text="📅 Стандартный срок (Бесплатно)", callback_data="urg_standard_0")],
      ]
  )
  await callback.message.edit_text(
      "<b>Шаг 3 из 3: Сроки и старт</b>\n\nВыберите желаемый график выполнения:",
      reply_markup=keyboard,
      parse_mode="HTML",
  )
  await callback.answer()


@router.callback_query(OrderForm.urgency, F.data.startswith("urg_"))
async def process_urgency(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  urg_type = parts[1]
  urg_price = int(parts[2])

  urg_map = {
      "fast": "⚡ Срочный старт (сегодня)",
      "soon": "🕐 В течение 3 дней",
      "standard": "📅 Стандартный срок",
  }
  urgency = urg_map.get(urg_type, "📅 Стандартный срок")

  await state.update_data(urgency=urgency, urgency_price=urg_price)

  data = await state.get_data()
  service_price = data.get("service_price", 0)
  addon_price = data.get("addon_price", 0)
  total_price = service_price + addon_price + urg_price

  await state.update_data(total_price=total_price)

  urgency_display = f"{urgency} (+{urg_price}$)" if urg_price > 0 else f"{urgency} (Бесплатно)"

  summary_text = (
      "🎉 <b>Ваш заказ успешно сформирован!</b>\n\n"
      "📋 <b>Сводка заказа:</b>\n"
      f"• Услуга: <b>{html.escape(str(data.get('service_name')))}</b> ({service_price}$)\n"
      f"• Опция: <b>{html.escape(str(data.get('addon_type')))}</b> ({addon_price}$)\n"
      f"• График: <b>{html.escape(urgency_display)}</b>\n\n"
      f"💵 <b>Итого к оплате:</b> <b>{total_price}$</b>\n\n"
      "⚠️ <b>Оплата принимается банковской картой, СБП, криптовалютой или Telegram Stars!</b>\n"
      "Нажмите кнопку ниже для перехода к оплате 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить заказ", callback_data="go_to_payment")]]
  )

  await callback.message.edit_text(summary_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "go_to_payment")
async def process_go_to_payment(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id
  
  if user_id in ABANDONED_CHECKOUTS:
    ABANDONED_CHECKOUTS[user_id].cancel()
  ABANDONED_CHECKOUTS[user_id] = asyncio.create_task(delayed_abandoned_cart_reminder(callback.bot, user_id))

  data = await state.get_data()
  total_price = data.get("total_price", 150)
  
  rub_amount = max(100, int(total_price * 90))
  stars_amount = max(1, rub_amount)

  SAFARIPAY_PREFETCH_CACHE[user_id] = {
      "card": asyncio.create_task(create_safaripay_invoice(rub_amount, "card")),
      "stars": asyncio.create_task(create_safaripay_invoice(stars_amount, "stars"))
  }

  base_text = (
      "💳 <b>ОПЛАТА ЗАКАЗА</b>\n\n"
      f"• <b>Сумма к оплате:</b> {total_price}$ (≈ {rub_amount} ₽ / {stars_amount} ⭐)\n\n"
      "⚠️ Выберите удобный способ оплаты ниже 👇"
  )

  def get_payment_keyboard(timer_active=False, time_left="20:00"):
    kb_buttons = [
        [InlineKeyboardButton(text=f"💳 Карта / СБП (~{rub_amount} ₽)", callback_data="pay_card")],
        [InlineKeyboardButton(text=f"⭐ Telegram Stars ({stars_amount} ⭐)", callback_data="pay_safari_stars")],
        [InlineKeyboardButton(text="🪙 Крипта USDT / TON", callback_data="crypto_expert")]
    ]
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


@router.callback_query(F.data.in_({"pay_card", "pay_sbp"}))
async def process_pay_card(callback: CallbackQuery, state: FSMContext):
  user_id = callback.from_user.id
  chat_id = callback.message.chat.id
  message_id = callback.message.message_id

  task_key = (chat_id, message_id)
  if task_key in PAYMENT_TIMERS:
    PAYMENT_TIMERS[task_key].cancel()
    del PAYMENT_TIMERS[task_key]
  ABANDONED_CHECKOUTS.pop(user_id, None)

  data = await state.get_data()
  total_price = data.get("total_price", 150)
  rub_amount = max(100, int(total_price * 90))

  user_cache = SAFARIPAY_PREFETCH_CACHE.get(user_id, {})
  pref_task = user_cache.get("card")
  
  pay_url = ""
  if pref_task:
    try:
      pay_url = await pref_task
    except Exception:
      pass

  if not pay_url:
    pay_url = await create_safaripay_invoice(rub_amount, "card")
    
  SAFARIPAY_PREFETCH_CACHE.pop(user_id, None)

  if not pay_url:
    fail_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать еще раз", callback_data="pay_card")],
            [InlineKeyboardButton(text="💬 Написать в поддержку", callback_data="contact_admin")],
            [InlineKeyboardButton(text="⬅️ Назад к выбору", callback_data="go_to_payment")]
        ]
    )
    await callback.message.edit_text(
        "⚠️ <b>Временная задержка платежного шлюза</b>\n\n"
        "Не удалось получить реквизиты от SafariPay. Нажмите «Попробовать еще раз» или обратитесь в поддержку.",
        reply_markup=fail_kb, parse_mode="HTML"
    )
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔗 Оплатить картой / СБП (Safari Pay)", url=pay_url)],
          [InlineKeyboardButton(text="✅ Я оплатил(-а), проверить", callback_data="safaripay_check_success")],
          [InlineKeyboardButton(text="⬅️ Назад к выбору", callback_data="go_to_payment")],
      ]
  )
  text = (
      "💳 <b>Оплата картой / СБП (Safari Pay)</b>\n\n"
      f"Сумма: <b>{rub_amount} ₽</b>\n\n"
      "Реквизиты и ссылка успешно загружены! Нажмите кнопку ниже для перевода."
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


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
  total_price = data.get("total_price", 150)
  rub_amount = max(100, int(total_price * 90))
  stars_amount = max(1, rub_amount)

  user_cache = SAFARIPAY_PREFETCH_CACHE.get(user_id, {})
  pref_task = user_cache.get("stars")
  
  pay_url = ""
  if pref_task:
    try:
      pay_url = await pref_task
    except Exception:
      pass

  if not pay_url:
    pay_url = await create_safaripay_invoice(stars_amount, "stars")
    
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
        "Не удалось сгенерировать счет. Пожалуйста, повторите попытку через секунду или выберите другой способ оплаты.",
        reply_markup=fail_kb, parse_mode="HTML"
    )
    return

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="⭐ Оплатить звёздами (Safari Pay)", url=pay_url)],
          [InlineKeyboardButton(text="✅ Я оплатил(-а), проверить", callback_data="safaripay_check_success")],
          [InlineKeyboardButton(text="⬅️ Назад к выбору", callback_data="go_to_payment")],
      ]
  )
  text = (
      "⭐ <b>Оплата Telegram Звёздами (Safari Pay)</b>\n\n"
      f"Сумма: <b>{stars_amount} ⭐</b>\n\n"
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

  user_username = f"@{html.escape(user.username)}" if user.username else f"ID: {user.id}"
  admin_order_text = (
      f"💳 <b>Оплачен заказ через Safari Pay!</b>\n\n"
      f"• От кого: {user_username}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Услуга: <b>{html.escape(str(data.get('service_name')))}</b>\n"
      f"• Сумма: <b>{data.get('total_price')}$</b>"
  )
  if ADMIN_ID:
    try:
      await callback.bot.send_message(chat_id=ADMIN_ID, text=admin_order_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить уведомление админу: {e}")

  final_text = (
      "✅ <b>Платеж обрабатывается!</b>\n\n"
      "Заказ принят. Менеджер студии проверит поступление средств и свяжется с вами в течение <b>15 минут</b> для обсуждения ТЗ."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]
  )
  await callback.message.edit_text(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


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

  text = "💳 <b>ВЫБОР КРИПТОВАЛЮТНОЙ СЕТИ</b>\n\nПожалуйста, выберите сеть для перевода средств:"
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔴 USDT (TRC-20)", callback_data="pay_trc20")],
          [InlineKeyboardButton(text="🟢 USDT (BEP-20)", callback_data="pay_bep20")],
          [InlineKeyboardButton(text="🔵 TON", callback_data="pay_ton")],
          [InlineKeyboardButton(text="🟠 Bitcoin (BTC)", callback_data="pay_btc")],
          [InlineKeyboardButton(text="⬅️ Назад", callback_data="go_to_payment")],
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "pay_trc20")
async def process_pay_trc20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 150)
  payment_text = (
      "💳 <b>ОПЛАТА USDT (TRC-20)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> TRC-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети TRC-20. После оплаты обязательно нажмите кнопку ниже и отправьте чек!</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ask_for_receipt")],
          [InlineKeyboardButton(text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert")],
      ]
  )
  await callback.message.edit_text(payment_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "pay_bep20")
async def process_pay_bep20(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 150)
  payment_text = (
      "💳 <b>ОПЛАТА USDT (BEP-20)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> BEP-20\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети BEP-20. После оплаты обязательно нажмите кнопку ниже и отправьте чек!</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ask_for_receipt")],
          [InlineKeyboardButton(text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert")],
      ]
  )
  await callback.message.edit_text(payment_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "pay_ton")
async def process_pay_ton(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 150)
  payment_text = (
      "💳 <b>ОПЛАТА TON</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> TON\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети TON. После оплаты обязательно нажмите кнопку ниже и отправьте чек!</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ask_for_receipt")],
          [InlineKeyboardButton(text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert")],
      ]
  )
  await callback.message.edit_text(payment_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "pay_btc")
async def process_pay_btc(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  total_price = data.get("total_price", 150)
  payment_text = (
      "💳 <b>ОПЛАТА BITCOIN (BTC)</b>\n\n"
      f"• <b>Сумма:</b> {total_price}$\n"
      "• <b>Сеть:</b> Bitcoin\n"
      "• <b>Реквизиты для оплаты:</b>\n"
      "<code>bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\n"
      "<i>Переводите строго точную сумму в сети Bitcoin. После оплаты обязательно нажмите кнопку ниже и отправьте чек!</i>"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ask_for_receipt")],
          [InlineKeyboardButton(text="⬅️ Назад к выбору способа оплаты", callback_data="crypto_expert")],
      ]
  )
  await callback.message.edit_text(payment_text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "ask_for_receipt")
async def ask_for_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(OrderForm.receipt)
  text = (
      "🧾 <b>Подтверждение оплаты</b>\n\n"
      "Пожалуйста, отправьте в чат <b>фотографию или скриншот чека</b> о переводе."
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


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

  user_username = f"@{html.escape(user.username)}" if user.username else f"ID: {user.id}"
  user_full = html.escape(user.full_name) if user.full_name else ""
  user_info = f"{user_full} ({user_username})" if user_full else user_username

  admin_order_text = (
      f"🛒 <b>Новый оплаченный заказ на разработку!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Услуга: <b>{html.escape(str(data.get('service_name')))}</b>\n"
      f"• Сумма: <b>{data.get('total_price')}$</b>\n"
      f"• Опция: {html.escape(str(data.get('addon_type')))}\n"
      f"• График: {html.escape(str(data.get('urgency')))}"
  )

  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_order_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить заказ админу: {e}")

  final_text = (
      "✅ <b>Заявка и чек успешно приняты!</b>\n\n"
      "Менеджер студии проверяет поступление средств. С вами свяжутся в течение <b>15 минут</b>."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]
  )
  await message.answer(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.message(OrderForm.receipt, ~F.photo)
async def process_receipt_invalid(message: Message):
  await message.answer("⚠️ Пожалуйста, отправьте именно <b>фотографию или скриншот чека</b> (картинкой)!")


@router.callback_query(F.data == "start_ads")
async def process_start_ads(callback: CallbackQuery, state: FSMContext):
  await state.clear()
  await state.set_state(AdForm.platform)
  text = "📢 <b>ПОКУПКА РЕКЛАМЫ И СОТРУДНИЧЕСТВО</b>\n\nВыберите площадку для размещения вашей рекламы:"
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🤖 Реклама в боте", callback_data="adplat_bot")],
          [InlineKeyboardButton(text="📢 Реклама в канале", callback_data="adplat_channel")],
          [InlineKeyboardButton(text="⬅️ На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(AdForm.platform, F.data.startswith("adplat_"))
async def process_ad_platform(callback: CallbackQuery, state: FSMContext):
  plat_code = callback.data.split("_")[1]
  platform_name = "Реклама в боте" if plat_code == "bot" else "Реклама в канале"
  await state.update_data(platform=platform_name, plat_code=plat_code)
  await state.set_state(AdForm.duration)

  kb_list = [
      [InlineKeyboardButton(text="24 часа = 5$", callback_data="addur_24h_5")],
      [InlineKeyboardButton(text="3 дня = 10$", callback_data="addur_3d_10")],
      [InlineKeyboardButton(text="7 дней = 25$", callback_data="addur_7d_25")],
      [InlineKeyboardButton(text="30 дней = 100$", callback_data="addur_30d_100")],
      [InlineKeyboardButton(text="⬅️ Назад", callback_data="start_ads")],
  ]
  keyboard = InlineKeyboardMarkup(inline_keyboard=kb_list)
  text = f"📢 <b>Площадка: {platform_name}</b>\n\n<b>Выберите срок размещения рекламы:</b>"

  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(AdForm.duration, F.data.startswith("addur_"))
async def process_ad_duration(callback: CallbackQuery, state: FSMContext):
  parts = callback.data.split("_")
  duration_code = parts[1]
  price = int(parts[2])

  dur_map = {"24h": "24 часа", "3d": "3 дня", "7d": "7 дней", "30d": "30 дней"}
  duration_str = dur_map.get(duration_code, duration_code)

  await state.update_data(duration=duration_str, ad_price=price)
  await state.set_state(AdForm.niche)

  text = (
      f"📢 <b>Выбрано: {duration_str} — {price}$</b>\n\n"
      "<b>Шаг 3: Тематика проекта</b>\nВведите тематику или нишу вашего проекта текстом:"
  )
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


@router.message(AdForm.niche, F.text)
async def process_ad_niche(message: Message, state: FSMContext):
  await state.update_data(niche=message.text.strip())
  await state.set_state(AdForm.link)
  await message.answer("📢 <b>Шаг 4: Ссылка</b>\n\nВведите ссылку на ваш ресурс или канал:", parse_mode="HTML")


@router.message(AdForm.link, F.text)
async def process_ad_link(message: Message, state: FSMContext):
  await state.update_data(link=message.text.strip())
  await state.set_state(AdForm.creative)
  await message.answer("📢 <b>Шаг 5: Креатив</b>\n\nОтправьте рекламный креатив (изображение, текст или видео):", parse_mode="HTML")


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
      f"• Площадка: <b>{html.escape(str(data.get('platform')))}</b>\n"
      f"• Срок: <b>{html.escape(str(data.get('duration')))}</b>\n"
      f"• Тематика: <b>{html.escape(str(data.get('niche')))}</b>\n"
      f"• Ссылка: <b>{html.escape(str(data.get('link')))}</b>\n"
      f"• Креатив: <b>{html.escape(str(creative_desc))}</b>\n\n"
      f"💵 <b>К оплате: {price}$</b>\n\n"
      "Нажмите кнопку ниже для выбора способа оплаты 👇"
  )

  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="💳 Оплатить рекламу", callback_data="ad_pay_select")]]
  )
  await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "ad_pay_select")
async def process_ad_pay_select(callback: CallbackQuery, state: FSMContext):
  data = await state.get_data()
  price = data.get("ad_price", 0)
  text = f"💳 <b>ОПЛАТА РЕКЛАМЫ ({price}$)</b>\n\nВыберите способ оплаты:"
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [InlineKeyboardButton(text="🔴 USDT (TRC-20)", callback_data="adpay_trc20")],
          [InlineKeyboardButton(text="🟢 USDT (BEP-20)", callback_data="adpay_bep20")],
          [InlineKeyboardButton(text="🔵 TON", callback_data="adpay_ton")],
          [InlineKeyboardButton(text="🟠 Bitcoin (BTC)", callback_data="adpay_btc")],
          [InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")],
      ]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data.startswith("adpay_"))
async def adpay_crypto_handler(callback: CallbackQuery, state: FSMContext):
  network = callback.data.split("_")[1].upper()
  data = await state.get_data()
  price = data.get("ad_price", 0)

  wallets = {
      "TRC20": "TYd1ekJgTHnFfgAVYXsi7689GaG5oZN3Xe",
      "BEP20": "0x0A6C6634f8FD920a5E9E0d241CD46d6C15377f39",
      "TON": "UQDwk7PZoWr0zHv-BItBEGxnq4uyj81z6BlIw74heDgF-Shd",
      "BTC": "bc1qj7xy0645vcza7l3et5s8nuahllua9a439c0795"
  }
  address = wallets.get(network, "")

  text = (
      f"💳 <b>ОПЛАТА РЕКЛАМЫ ({network})</b>\n\n"
      f"• <b>Сумма:</b> {price}$\n"
      f"• <b>Сеть:</b> {network}\n"
      f"• <b>Реквизиты для оплаты:</b>\n"
      f"<code>{address}</code>\n\n"
      "⚠️ <b>ВНИМАНИЕ!</b>\nПереводите строго точную сумму. После оплаты нажмите кнопку ниже и прикрепите чек!"
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="✅ Я оплатил(-а), прикрепить чек", callback_data="ad_ask_receipt")]]
  )
  await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
  await callback.answer()


@router.callback_query(F.data == "ad_ask_receipt")
async def ad_ask_receipt(callback: CallbackQuery, state: FSMContext):
  await state.set_state(AdForm.receipt)
  text = "🧾 <b>Подтверждение оплаты рекламы</b>\n\nПожалуйста, отправьте в чат <b>фотографию или скриншот чека</b> о переводе."
  await callback.message.edit_text(text, parse_mode="HTML")
  await callback.answer()


@router.message(AdForm.receipt, F.photo)
async def process_ad_receipt(message: Message, state: FSMContext):
  user = message.from_user
  user_username = f"@{html.escape(user.username)}" if user.username else f"ID: {user.id}"
  user_full = html.escape(user.full_name) if user.full_name else ""
  user_info = f"{user_full} ({user_username})" if user_full else user_username

  data = await state.get_data()
  admin_ad_text = (
      f"📢 <b>Новая оплата рекламы!</b>\n\n"
      f"• От кого: {user_info}\n"
      f"• User ID: <code>{user.id}</code>\n"
      f"• Площадка: {html.escape(str(data.get('platform')))}\n"
      f"• Срок: {html.escape(str(data.get('duration')))}\n"
      f"• Сумма: <b>{data.get('ad_price')}$</b>"
  )
  if ADMIN_ID:
    try:
      photo_id = message.photo[-1].file_id
      await message.bot.send_photo(chat_id=ADMIN_ID, photo=photo_id, caption=admin_ad_text, parse_mode="HTML")
    except Exception as e:
      logger.error(f"Не удалось отправить оплату рекламы админу: {e}")

  final_text = (
      "✅ <b>Заявка на рекламу и чек успешно приняты!</b>\n\n"
      "Ожидайте, модератор проверит поступление средств в течение <b>15 минут</b> и свяжется с вами."
  )
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[[InlineKeyboardButton(text="🏠 На главную", callback_data="back_home")]]
  )
  await message.answer(final_text, reply_markup=keyboard, parse_mode="HTML")
  await state.clear()


@router.message(AdForm.receipt, ~F.photo)
async def process_ad_receipt_invalid(message: Message):
  await message.answer("⚠️ Пожалуйста, отправьте именно <b>фотографию чека</b> (картинкой)!")


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
  global http_session
  http_session = ClientSession()
  
  bot = Bot(token=BOT_TOKEN)
  dp = Dispatcher()
  dp.include_router(router)
  
  asyncio.create_task(start_web_server())
  asyncio.create_task(scheduled_broadcast(bot))
  
  await bot.delete_webhook(drop_pending_updates=True)
  try:
    await dp.start_polling(bot)
  finally:
    if http_session and not http_session.closed:
      await http_session.close()
      await asyncio.sleep(0.250)


if __name__ == "__main__":
  asyncio.run(main())
