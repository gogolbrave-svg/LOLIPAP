import os

# Telegram Bot Token (получить у @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# Crypto Pay API Token (получить в @CryptoBot -> Crypto Pay -> Создать приложение)
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "YOUR_CRYPTO_BOT_API_TOKEN_HERE")

# Настройки товара
PRICE_USD = "1.5"  # Сумма в USDT
STARS_AMOUNT = 99   # Оплата звездами Telegram

# Ссылка на закрытый канал / автовыдачу после оплаты
PAID_CHANNEL_LINK = os.getenv("PAID_CHANNEL_LINK", "https://t.me/+your_private_channel_link")

# Ссылка на Telegra.ph статью "Что входит?"
TELEGRAPH_ARTICLE_LINK = os.getenv("TELEGRAPH_ARTICLE_LINK", "https://telegra.ph/INFORMACIONNAYA-STATYA-11-02")
