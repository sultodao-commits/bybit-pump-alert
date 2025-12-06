#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Pump & Dump Scanner - 15min
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()  # ВАЖНО: Добавь это

# ========================= НАСТРОЙКИ СКАНЕРА =========================

# PUMP/DUMP DETECTION
PRICE_CHANGE_THRESHOLD = 7.0      # Минимальное изменение цены в % за 15 минут
VOLUME_SPIKE_THRESHOLD = 2.5      # Минимальный Z-score объема
MIN_ABSOLUTE_VOLUME = 50000       # Минимальный объем в USDT

# FILTERS
REQUIRE_VOLUME_CONFIRMATION = True  # Требовать всплеск объема

POLL_INTERVAL_SEC = 60            # Интервал сканирования
SIGNAL_COOLDOWN_MIN = 30          # Кулдаун на монету (минут)

# ========================= ИНДИКАТОРЫ =========================

def calculate_volume_zscore(volumes: List[float], period: int) -> float:
    """Расчет Z-score объема"""
    if len(volumes) < period:
        return 0.0
    recent_volumes = volumes[-period:]
    mean_vol = np.mean(recent_volumes)
    std_vol = np.std(recent_volumes)
    if std_vol == 0:
        return 0.0
    return (volumes[-1] - mean_vol) / std_vol

def calculate_15min_price_change(ohlcv: List) -> float:
    """Расчет изменения цены за последнюю 15-минутную свечу"""
    if len(ohlcv) < 2:
        return 0.0
    
    current_candle = ohlcv[-1]
    previous_candle = ohlcv[-2]
    
    current_close = float(current_candle[4])
    previous_close = float(previous_candle[4])
    
    if previous_close == 0:
        return 0.0
    
    return ((current_close - previous_close) / previous_close) * 100

# ========================= ЛОГИКА СКАНЕРА PUMP/DUMP =========================

def analyze_pump_dump(symbol: str, ohlcv: List) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < 20:
            return None

        closes = [float(c[4]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]
        
        # Текущие значения
        current_volume = volumes[-1]
        current_close = closes[-1]
        
        # Расчет изменения цены за 15 минут
        price_change = calculate_15min_price_change(ohlcv)
        
        # Расчет Z-score объема
        volume_zscore = calculate_volume_zscore(volumes[:-1], 15)  # Используем предыдущие свечи для сравнения
        
        # Проверка абсолютного объема
        volume_pass = current_volume >= MIN_ABSOLUTE_VOLUME
        
        # Определение типа движения
        is_pump = price_change >= PRICE_CHANGE_THRESHOLD
        is_dump = price_change <= -PRICE_CHANGE_THRESHOLD
        
        if not (is_pump or is_dump):
            return None
        
        # Проверка объема (если требуется)
        volume_confirm = True
        if REQUIRE_VOLUME_CONFIRMATION:
            volume_confirm = volume_zscore >= VOLUME_SPIKE_THRESHOLD
        
        if not (volume_pass and volume_confirm):
            return None
        
        # Определение силы сигнала
        if abs(price_change) >= 15:
            confidence = 95
            strength = "💥 СИЛЬНЫЙ"
        elif abs(price_change) >= 10:
            confidence = 85
            strength = "🚨 СРЕДНИЙ"
        else:
            confidence = 75
            strength = "📈 СЛАБЫЙ"
        
        signal_type = "PUMP" if is_pump else "DUMP"
        
        print(f"🎯 {symbol}: {signal_type} | Изменение: {price_change:+.1f}% | Объем Z={volume_zscore:.1f}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "price_change": price_change,
            "volume_zscore": volume_zscore,
            "volume_usdt": current_volume,
            "current_price": current_close,
            "confidence": confidence,
            "strength": strength,
            "timestamp": time.time()
        }

    except Exception as e:
        print(f"❌ Ошибка анализа {symbol}: {e}")
        return None

# ========================= TELEGRAM =========================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не указан")
        return
    
    if not TELEGRAM_CHAT_ID:
        print("❌ TELEGRAM_CHAT_ID не указан")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }
    
    try:
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code == 200:
            print(f"✅ Сообщение отправлено в Telegram")
            return True
        else:
            print(f"❌ Ошибка Telegram API: {response.status_code}")
            print(f"❌ Ответ: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Ошибка отправки в Telegram: {e}")
        return False

def format_signal_message(signal: Dict) -> str:
    symbol_parts = signal['symbol'].split('/')
    ticker = symbol_parts[0] if symbol_parts else signal['symbol']
    
    if signal["type"] == "PUMP":
        emoji = "🚀"
        direction = "ВВЕРХ"
        color = "🟢"
    else:
        emoji = "💥"
        direction = "ВНИЗ"
        color = "🔴"
    
    change = signal['price_change']
    volume_z = signal['volume_zscore']
    
    return f"""{emoji} <b>ПАМП/ДАМП СИГНАЛ</b> {emoji}

{color} <b>{ticker}</b> | {direction}
📊 Изменение: <b>{change:+.1f}%</b> за 15мин
📈 Объем: <b>Z={volume_z:.1f}</b>
💪 Сила: <b>{signal['strength']}</b>

⏰ Время: {time.strftime('%H:%M:%S')}"""

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК СКАНЕРА ПАМПОВ/ДАМПОВ - 15 МИНУТ")
    print(f"🔍 Отслеживание движений от {PRICE_CHANGE_THRESHOLD}% за 15 минут")
    
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Укажи TELEGRAM_BOT_TOKEN в переменных окружения!")
        return

    if not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_CHAT_ID в переменных окружения!")
        print("ℹ️ Как получить CHAT_ID:")
        print("1. Напиши боту в Telegram")
        print("2. Перейди по ссылке: https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates")
        print("3. Найди 'chat_id' в ответе")
        return

    # Тестовое сообщение
    send_telegram("🤖 Бот запущен на Scalingo. Проверка связи...")

    exchange = ccxt.bybit({
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap"  # фьючерсный рынок (перпетуалы)
        }
    })

    recent_signals = {}

    markets = exchange.load_markets()
    symbols = []

    for symbol in markets:
        if (
            markets[symbol]['active']
            and symbol.endswith(':USDT')  # только бессрочные контракты с USDT
        ):
            symbols.append(symbol)

    total_symbols = len(symbols)
    print(f"🔍 Найдено монет: {total_symbols}")
    send_telegram(f"🤖 Сканер пампов/дампов запущен | Монет: {total_symbols}")
    send_telegram("✅ Бот работает, все норм!")

    signal_count = 0

    while True:
        try:
            print(f"\n⏱️ Сканирование 15min свечей... | Сигналов: {signal_count}")
            current_time = time.time()

            for symbol in symbols:
                try:
                    if symbol in recent_signals:
                        time_since_last_signal = current_time - recent_signals[symbol]
                        if time_since_last_signal < SIGNAL_COOLDOWN_MIN * 60:
                            continue

                    ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=20)
                    if not ohlcv or len(ohlcv) < 5:
                        continue

                    signal = analyze_pump_dump(symbol, ohlcv)
                    if not signal:
                        continue

                    recent_signals[symbol] = current_time
                    signal_count += 1
                    
                    message = format_signal_message(signal)
                    send_telegram(message)
                    
                    print(f"🎯 СИГНАЛ #{signal_count}: {symbol} | {signal['type']} | {signal['price_change']:+.1f}% | Объем Z={signal['volume_zscore']:.1f}")

                except Exception as e:
                    continue

            # Очистка старых сигналов
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("⏹️ Сканер остановлен")
    except Exception as e:
        print(f"💥 Критическая ошибка: {e}")
        print("🔄 Перезапуск через 10 секунд...")
        time.sleep(10)
        main()
