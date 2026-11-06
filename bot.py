import logging
import asyncio
aiohttp_imported = False
try:
    import aiohttp
except ImportError:
    pass

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

# --- ФУНКЦИИ CRYPTO PAY API ---

async def create_crypto_invoice(amount: str, payload: str):
    """Создание динамического инвойса через Crypto Pay API"""
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": config.CRYPTO_BOT_TOKEN}
    data = {
        "asset": "USDT",
        "amount": amount,
        "description": "Доступ к эксклюзивному контенту (630 ГБ)",
        "payload": payload,
        "paid_btn_name": "openBot",
        "paid_btn_url": f"https://t.me/{(await bot.get_me()).username}"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=data) as response:
            result = await response.json()
            if result.get("ok"):
                return result["result"]["pay_url"], result["result"]["invoice_id"]
            else:
                logging.error(f"CryptoBot API Error: {result}")
                return None, None

async def check_crypto_invoice(invoice_id: int):
    """Проверка статуса инвойса через Crypto Pay API"""
    url = f"https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": config.CRYPTO_BOT_TOKEN}
    params = {"invoice_ids": invoice_id}
    
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as response:
            result = await response.json()
            if result.get("ok") and result["result"]["items"]:
                status = result["result"]["items"][0]["status"]
                return status == "paid" # Возвращает True, если оплачено
            return False

# --- КЛАВИАТУРЫ ---

def main_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="🟩 КУПИТЬ ВХОД", callback_data="buy_access")],
        [InlineKeyboardButton(text="🟪 ЧТО ВХОДИТ? ↗️", url=config.TELEGRAPH_ARTICLE_LINK)],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def buy_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text=f"🟩 Stars {config.STARS_AMOUNT} ⭐", callback_data="pay_stars")],
        [InlineKeyboardButton(text="🟪 CryptoBot (USDT)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def what_inside_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="🟪 ИНСТРУКЦИЯ", callback_data="show_instruction")],
        [InlineKeyboardButton(text="🟪 СКИНИТЬ ФОТО", callback_data="send_photo_sample")],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def instruction_keyboard() -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text="🟩 ОТКРЫТЬ ИНСТРУКЦИЮ ↗️", url=config.TELEGRAPH_ARTICLE_LINK)],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="what_inside")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def payment_crypto_keyboard(pay_url: str, invoice_id: int) -> InlineKeyboardMarkup:
    """Клавиатура с живой ссылкой на оплату и кнопкой проверки"""
    kb = [
        [InlineKeyboardButton(text="💳 Оплатить в CryptoBot ↗️", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_pay_{invoice_id}")],
        [InlineKeyboardButton(text="🟥 НАЗАД", callback_data="buy_access")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ТЕКСТЫ ---

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
    f"Цена: ${config.PRICE_USD} USDT / ({config.STARS_AMOUNT} ⭐️)\n\n"
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
    await message.answer(text=MAIN_TEXT, parse_mode="Markdown", reply_markup=main_keyboard())

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: types.CallbackQuery):
    await call.message.edit_text(text=MAIN_TEXT, parse_mode="Markdown", reply_markup=main_keyboard())
    await call.answer()

@dp.callback_query(F.data == "buy_access")
async def cb_buy_access(call: types.CallbackQuery):
    await call.message.edit_text(text=BUY_TEXT, parse_mode="Markdown", reply_markup=buy_keyboard())
    await call.answer()

@dp.callback_query(F.data == "what_inside")
async def cb_what_inside(call: types.CallbackQuery):
    await call.message.edit_text(text=WHAT_INSIDE_TEXT, parse_mode="Markdown", reply_markup=what_inside_keyboard())
    await call.answer()

@dp.callback_query(F.data == "show_instruction")
async def cb_show_instruction(call: types.CallbackQuery):
    await call.message.edit_text(text=INSTRUCTION_TEXT, parse_mode="Markdown", reply_markup=instruction_keyboard())
    await call.answer()

@dp.callback_query(F.data == "send_photo_sample")
async def cb_send_photo_sample(call: types.CallbackQuery):
    await call.answer("Отправка фото...")
    await call.message.answer_photo(
        photo="https://picsum.photos/800/600",
        caption="📸 Пример материала из закрытого пака."
    )

# --- ОПЛАТА ЧЕРЕЗ CRYPTO PAY API ---

@dp.callback_query(F.data == "pay_crypto")
async def process_pay_crypto(call: types.CallbackQuery):
    """Генерация индивидуального счета на оплату USDT через API"""
    await call.answer("Создаю счет для оплаты...")
    
    pay_url, invoice_id = await create_crypto_invoice(
        amount=config.PRICE_USD, 
        payload=str(call.from_user.id)
    )
    
    if not pay_url:
        await call.message.answer("❌ Ошибка создания счета. Попробуй позже или выберите другой способ оплаты.")
        return

    text = (
        f"💳 **Счет на оплату CryptoBot**\n\n"
        f"Сумма: **{config.PRICE_USD} USDT**\n"
        f"Нажми кнопку ниже для оплаты. После оплаты нажми «Проверить оплату»."
    )
    
    await call.message.edit_text(
        text=text,
        parse_mode="Markdown",
        reply_markup=payment_crypto_keyboard(pay_url, invoice_id)
    )

@dp.callback_query(F.data.startswith("check_pay_"))
async def process_check_payment(call: types.CallbackQuery):
    """Проверка статуса оплаты пользователем"""
    invoice_id = int(call.data.split("_")[2])
    
    is_paid = await check_crypto_invoice(invoice_id)
    
    if is_paid:
        await call.message.edit_text(
            f"✅ **Оплата успешно получена!**\n\n"
            f"Вот твоя ссылка на доступ к каналу:\n{config.PAID_CHANNEL_LINK}",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    else:
        await call.answer("⏳ Оплата еще не поступила. Если ты уже оплатил, подожди пару секунд и попробуй снова.", show_alert=True)

# --- ОПЛАТА ЗВЕЗДАМИ (TELEGRAM STARS) ---

@dp.callback_query(F.data == "pay_stars")
async def process_pay_stars(call: types.CallbackQuery):
    prices = [LabeledPrice(label="Доступ к 630 ГБ контента", amount=config.STARS_AMOUNT)]
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title="Доступ к эксклюзивному контенту",
        description="Автоматический доступ в приватный канал сразу после оплаты.",
        payload="buy_channel_access_stars",
        provider_token="",
        currency="XTR",
        prices=prices
    )
    await call.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
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
