import os
import time
import ccxt
import numpy as np
import pandas as pd
import requests
from datetime import datetime, timedelta

# Получение токена и чата из переменных окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Настройка биржи (Bybit Futures)
exchange = ccxt.bybit({
    "enableRateLimit": True,
    "options": {"defaultType": "future"}
})

# Порог для пампа и дампа (в процентах)
THRESHOLD = 3  

# Период для анализа (минуты)
INTERVAL = "1m"
LOOKBACK = 20  # количество свечей для истории

def send_telegram_message(message: str):
    """Отправка сообщений в Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, data=payload)
    except Exception as e:
        print(f"Ошибка отправки в Telegram: {e}")

def fetch_symbols():
    """Загрузка списка торговых пар (USDT perpetual futures)"""
    markets = exchange.load_markets()
    symbols = [s for s in markets if "USDT" in s and "PERP" in s]
    return symbols

def analyze_symbol(symbol):
    """Анализ конкретного символа"""
    try:
        # Загружаем последние свечи
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=INTERVAL, limit=LOOKBACK)
        df = pd.DataFrame(ohlcv, columns=["time", "open", "high", "low", "close", "volume"])
        df["time"] = pd.to_datetime(df["time"], unit="ms")

        # Считаем изменение за последнюю минуту
        last_close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2]
        change_pct = (last_close - prev_close) / prev_close * 100

        # История движения (мин-макс за последние LOOKBACK свечей)
        min_price = df["low"].min()
        max_price = df["high"].max()

        # Простая вероятность отскока: если был памп — шанс падения, если дамп — шанс роста
        if change_pct >= THRESHOLD:
            rebound_prob = np.clip((last_close - min_price) / (max_price - min_price + 1e-6), 0, 1)
            direction = "🚀 ПАМП"
            prob_text = f"Вероятность падения: {round((1 - rebound_prob) * 100, 1)}%"
        elif change_pct <= -THRESHOLD:
            rebound_prob = np.clip((max_price - last_close) / (max_price - min_price + 1e-6), 0, 1)
            direction = "📉 ДАМП"
            prob_text = f"Вероятность роста: {round(rebound_prob * 100, 1)}%"
        else:
            return  # нет сильного движения

        message = (
            f"<b>{direction} на {symbol}</b>\n\n"
            f"Изменение: {round(change_pct, 2)}%\n"
            f"Цена: {last_close}\n\n"
            f"{prob_text}\n\n"
            f"История: min={round(min_price, 4)}, max={round(max_price, 4)}\n"
            f"Время: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"
        )
        send_telegram_message(message)

    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")

def main():
    send_telegram_message("✅ Бот запущен. Мониторинг Bybit Futures (пампы и дампы).")
    symbols = fetch_symbols()
    send_telegram_message(f"📊 Отслеживается {len(symbols)} пар.")

    while True:
        for symbol in symbols:
            analyze_symbol(symbol)
            time.sleep(0.2)  # ограничение скорости
        time.sleep(5)

if __name__ == "__main__":
    main()
