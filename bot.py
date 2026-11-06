import logging
import asyncio
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    LabeledPrice,
    PreCheckoutQuery
)
import config

# Логирование
logging.basicConfig(level=logging.INFO)

# Инициализация
bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher()

# --- КЛАВИАТУРЫ ---

def main_keyboard() -> InlineKeyboardMarkup:
    """Главное меню"""
    kb = [
        [InlineKeyboardButton(text="🟩 КУПИТЬ ВХОД", callback_data="buy_access")],
        [InlineKeyboardButton(text="🟪 ЧТО ВХОДИТ? ↗️", url=config.TELEGRAPH_ARTICLE_LINK)],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def buy_keyboard() -> InlineKeyboardMarkup:
    """Экран выбора оплаты"""
    kb = [
        [InlineKeyboardButton(text=f"🟩 Stars {config.STARS_AMOUNT} ⭐ ↗️", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🟪 CryptoBot", url=config.CRYPTO_BOT_PAY_LINK)],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def what_inside_keyboard() -> InlineKeyboardMarkup:
    """Раздел «Что внутри?» (из второго экрана)"""
    kb = [
        [InlineKeyboardButton(text="🟪 ИНСТРУКЦИЯ", callback_data="show_instruction")],
        [InlineKeyboardButton(text="🟪 СКИНИТЬ ФОТО", callback_data="send_photo_sample")],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def instruction_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура для инструкции"""
    kb = [
        [InlineKeyboardButton(text="🟩 ОТКРЫТЬ ИНСТРУКЦИЮ ↗️", url=config.TELEGRAPH_ARTICLE_LINK)],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="what_inside")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ТЕКСТЫ СООБЩЕНИЙ ---

MAIN_TEXT = (
    "💭 **630 ГБ ЭКСКЛЮЗИВНОГО КОНТЕНТА**\n\n"
    "• Активное пополнение\n"
    "• Свежий контент\n"
    "• Доступ сразу после оплаты\n\n"
    "🗣 *Перед покупкой советую ознакомиться с разделом «что входит?».*"
)

WHAT_INSIDE_TEXT = (
    "**Что внутри:**\n"
    "• 🔥 Свежий материал\n"
    "• ⚡ Автовыдача товара\n"
    "• 💧 Без воды, только топ\n\n"
    "130 ГБ бесплатно по инструкции.\n\n"
    "**Подробнее о составе**"
)

BUY_TEXT = (
    "**Купить вход**\n\n"
    f"Цена: ${config.PRICE_USD} ({config.STARS_AMOUNT} ⭐️)\n\n"
    "Автовыдача включена.\n\n"
    "Выбери способ оплаты:"
)

INSTRUCTION_TEXT = (
    "**ИНСТРУКЦИЯ**\n\n"
    "Нажми кнопку ниже, чтобы открыть полную инструкцию\n"
    "Бесплатный пак - 150ГБ 👇"
)

# --- ХЕНДЛЕРЫ ---

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    """Стартовая команда /start"""
    await message.answer(
        text=MAIN_TEXT,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: types.CallbackQuery):
    """Возврат в главное меню"""
    await call.message.edit_text(
        text=MAIN_TEXT,
        parse_mode="Markdown",
        reply_markup=main_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "buy_access")
async def cb_buy_access(call: types.CallbackQuery):
    """Экран покупки / оплаты"""
    await call.message.edit_text(
        text=BUY_TEXT,
        parse_mode="Markdown",
        reply_markup=buy_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "what_inside")
async def cb_what_inside(call: types.CallbackQuery):
    """Экран «Что внутри»"""
    await call.message.edit_text(
        text=WHAT_INSIDE_TEXT,
        parse_mode="Markdown",
        reply_markup=what_inside_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "show_instruction")
async def cb_show_instruction(call: types.CallbackQuery):
    """Экран инструкции"""
    await call.message.edit_text(
        text=INSTRUCTION_TEXT,
        parse_mode="Markdown",
        reply_markup=instruction_keyboard()
    )
    await call.answer()

@dp.callback_query(F.data == "send_photo_sample")
async def cb_send_photo_sample(call: types.CallbackQuery):
    """Скинуть фото при нажатии кнопки 'СКИНИТЬ ФОТО'"""
    await call.answer("Отправка фото...", show_alert=False)
    # Пример отправки заглушки-фото (можно подставить свой photo_id или URL)
    await call.message.answer_photo(
        photo="https://picsum.photos/800/600",
        caption="📸 Пример материала из закрытого пака."
    )

# --- ОПЛАТА ЗВЕЗДАМИ (TELEGRAM STARS) ---

@dp.callback_query(F.data == "pay_stars")
async def process_pay_stars(call: types.CallbackQuery):
    """Выставление счета в Telegram Stars"""
    prices = [LabeledPrice(label="Доступ к 630 ГБ контента", amount=config.STARS_AMOUNT)]
    
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="Доступ к эксклюзивному контенту",
        description="Автоматический доступ в приватный канал сразу после оплаты.",
        payload="buy_channel_access_stars",
        provider_token="",  # Для Stars token опускается (пустая строка)
        currency="XTR",     # Код валюты Telegram Stars
        prices=prices
    )
    await call.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    """Подтверждение готовности принять платеж"""
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    """Успешная оплата -> Автовыдача доступа"""
    await message.answer(
        f"✅ **Оплата прошла успешно!**\n\n"
        f"Вот твоя ссылка на доступ к каналу:\n{config.PAID_CHANNEL_LINK}",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

# --- ЗАПУСК БОТА ---

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
