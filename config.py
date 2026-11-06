import os

# Telegram Bot Token (получить у @BotFather)
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN_HERE")

# Настройки товара и оплаты
PRICE_USD = 1.5
STARS_AMOUNT = 99  # Оплата звездами Telegram (99 Stars)

# Ссылки автовыдачи после оплаты
PAID_CHANNEL_LINK = os.getenv("PAID_CHANNEL_LINK", "https://t.me/+your_private_channel_link")
CRYPTO_BOT_PAY_LINK = os.getenv("CRYPTO_BOT_PAY_LINK", "https://t.me/CryptoBot?start=pay_xxx")

# Ссылка на Telegra.ph статью "Что входит?"
TELEGRAPH_ARTICLE_LINK = os.getenv("TELEGRAPH_ARTICLE_LINK", "https://telegra.ph/INFORMACIONNAYA-STATYA-11-02")
