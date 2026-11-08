import asyncio
import hashlib
import logging
import sys
from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.foutils import *  # noqa
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

# ==================== НАСТРОЙКИ ====================
TELEGRAM_BOT_TOKEN = "ТВОЙ_ТОКЕН_БОТА_ОТ_BOTFATHER"

# Данные из личного кабинета AnyPay
ANYPAY_PROJECT_ID = "ТВОЙ_PROJECT_ID"
ANYPAY_SECRET_KEY = "ТВОЙ_SECRET_KEY_1"
# ===================================================

dp = Dispatcher()


def generate_anypay_url(order_id: int, amount: float, currency: str = "RUB") -> str:
  """Генерация защищенной ссылки на оплату через AnyPay"""
  sign_str = f"{ANYPAY_PROJECT_ID}:{amount}:{ANYPAY_SECRET_KEY}:{currency}:{order_id}"
  sign = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

  url = (
      f"https://anypay.io/merchant?"
      f"merchant_id={ANYPAY_PROJECT_ID}&"
      f"amount={amount}&"
      f"currency={currency}&"
      f"pay_id={order_id}&"
      f"sign={sign}&"
      f"desc=Order_{order_id}"
  )
  return url


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
  """Главное меню: выбор услуги разработки"""
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🤖 Telegram Бот (Базовый) — 5 000 RUB",
                  callback_data="buy_base",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⚡️ Telegram Mini App / WebApp — 15 000 RUB",
                  callback_data="buy_webapp",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🛒 Магазин / Платежный бот — 10 000 RUB",
                  callback_data="buy_shop",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🌐 Оплатить из других стран (Крипта)",
                  callback_data="buy_crypto",
              )
          ],
      ]
  )

  text = (
      f"Привет, {html.bold(message.from_user.full_name)}! 👋\n\n"
      "Я занимаюсь разработкой **Telegram-ботов и софта под ключ**.\n"
      "Выберите нужную услугу для заказа:"
  )
  await message.answer(text, reply_markup=keyboard)


@dp.callback_query(F.data.startswith("buy_"))
async def process_service_choice(callback: CallbackQuery):
  """Обработка выбора услуги и генерация счета"""
  action = callback.data
  await callback.answer()

  user_id = callback.from_user.id
  order_id = user_id * 1000 + int(asyncio.get_event_loop().time() % 100)

  # Цены под каждую услугу
  prices = {
      "buy_base": (5000.0, "Базовый Telegram Бот"),
      "buy_webapp": (15000.0, "Telegram Mini App / WebApp"),
      "buy_shop": (10000.0, "Магазин / Платежный бот"),
  }

  if action in prices:
    amount, service_name = prices[action]

    # Генерируем ссылку AnyPay (карты РФ, СБП, Казахстан, Украина, Беларусь)
    pay_url = generate_anypay_url(
        order_id=order_id, amount=amount, currency="RUB"
    )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="💳 Оплатить картой / СБП (СНГ)", url=pay_url
                )
            ],
            [
                InlineKeyboardButton(
                    text="« Назад к списку услуг", callback_data="back_to_start"
                )
            ],
        ]
    )

    await callback.message.edit_text(
        f"🛠 Вы выбрали: {html.bold(service_name)}\n"
        f"🛒 Заказ: #{order_id}\n"
        f"💰 К оплате: {amount} RUB\n\n"
        "Нажмите кнопку ниже для безопасной оплаты:",
        reply_markup=keyboard,
    )

  elif action == "buy_crypto":
    # Меню для тех, кто платит криптой из других стран
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🤖 Оплатить через @CryptoBot",
                    url="https://t.me/CryptoBot",
                )
            ],
            [
                InlineKeyboardButton(
                    text="« Назад к списку услуг", callback_data="back_to_start"
                )
            ],
        ]
    )

    await callback.message.edit_text(
        "🌐 Для оплаты из других стран или криптовалютой (USDT / TON)"
        " воспользуйтесь официальным кошельком Telegram (@CryptoBot).\n\nНажмите"
        " кнопку ниже для перевода:",
        reply_markup=keyboard,
    )


@dp.callback_query(F.data == "back_to_start")
async def back_to_start(callback: CallbackQuery):
  """Возврат в главное меню услуг"""
  await callback.answer()
  keyboard = InlineKeyboardMarkup(
      inline_keyboard=[
          [
              InlineKeyboardButton(
                  text="🤖 Telegram Бот (Базовый) — 5 000 RUB",
                  callback_data="buy_base",
              )
          ],
          [
              InlineKeyboardButton(
                  text="⚡️ Telegram Mini App / WebApp — 15 000 RUB",
                  callback_data="buy_webapp",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🛒 Магазин / Платежный бот — 10 000 RUB",
                  callback_data="buy_shop",
              )
          ],
          [
              InlineKeyboardButton(
                  text="🌐 Оплатить из других стран (Крипта)",
                  callback_data="buy_crypto",
              )
          ],
      ]
  )
  await callback.message.edit_text(
      "Выберите нужную услугу для заказа:", reply_markup=keyboard
  )


async def main() -> None:
  bot = Bot(
      Token=TELEGRAM_BOT_TOKEN,
      default=DefaultBotProperties(parse_mode=ParseMode.HTML),
  )
  await dp.start_polling(bot)


if __name__ == "__main__":
  logging.basicConfig(level=logging.INFO, stream=sys.stdout)
  asyncio.run(main())
