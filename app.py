#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Pump & Dump Scanner - 5min
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# ========================= НАСТРОЙКИ СКАНЕРА =========================

# PUMP/DUMP DETECTION
PRICE_CHANGE_THRESHOLD = 5.0      # Минимальное изменение цены в % за 5 минут
VOLUME_SPIKE_THRESHOLD = 3.0      # Минимальный Z-score объема
MIN_ABSOLUTE_VOLUME = 75000       # Минимальный объем в USDT

# FILTERS
REQUIRE_VOLUME_CONFIRMATION = True  # Требовать всплеск объема

POLL_INTERVAL_SEC = 30            # Интервал сканирования (меньше для 5min)
SIGNAL_COOLDOWN_MIN = 15          # Кулдаун на монету (минут)

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

def calculate_price_change(ohlcv: List) -> float:
    """Расчет изменения цены за последнюю 5-минутную свечу"""
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
        if len(ohlcv) < 25:  # Больше данных для 5min
            return None

        closes = [float(c[4]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]
        
        # Текущие значения
        current_volume = volumes[-1]
        current_close = closes[-1]
        
        # Расчет изменения цены за 5 минут
        price_change = calculate_price_change(ohlcv)
        
        # Расчет Z-score объема (больше период для стабильности)
        volume_zscore = calculate_volume_zscore(volumes[:-1], 20)
        
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
        
        # Определение силы сигнала (скорректировано для 5min)
        if abs(price_change) >= 8:
            confidence = 90
            strength = "💥 СИЛЬНЫЙ"
        elif abs(price_change) >= 6:
            confidence = 80
            strength = "🚨 СРЕДНИЙ"
        else:
            confidence = 70
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
        return
    
    # Получаем все активные чаты из getUpdates
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok') and data.get('result'):
                chats = set()
                for update in data['result']:
                    if 'message' in update:
                        chat_id = update['message']['chat']['id']
                        chats.add(chat_id)
                
                # Отправляем сообщение в каждый чат
                for chat_id in chats:
                    send_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
                    try:
                        requests.post(send_url, json=payload, timeout=5)
                    except:
                        pass
    except:
        pass

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
    
    return f"""{emoji} <b>ПАМП/ДАМП СИГНАЛ (5min)</b> {emoji}

{color} <b>{ticker}</b> | {direction}
📊 Изменение: <b>{change:+.1f}%</b> за 5мин
📈 Объем: <b>Z={volume_z:.1f}</b>
💪 Сила: <b>{signal['strength']}</b>

⏰ Время: {time.strftime('%H:%M:%S')}"""

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК СКАНЕРА ПАМПОВ/ДАМПОВ - 5 МИНУТ")
    print(f"🔍 Отслеживание движений от {PRICE_CHANGE_THRESHOLD}% за 5 минут")
    
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Укажи TELEGRAM_BOT_TOKEN!")
        return

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
    send_telegram(f"🤖 Сканер пампов/дампов запущен | 5min ТФ | Монет: {total_symbols}")

    signal_count = 0

    while True:
        try:
            print(f"\n⏱️ Сканирование 5min свечей... | Сигналов: {signal_count}")
            current_time = time.time()

            for symbol in symbols:
                try:
                    if symbol in recent_signals:
                        time_since_last_signal = current_time - recent_signals[symbol]
                        if time_since_last_signal < SIGNAL_COOLDOWN_MIN * 60:
                            continue

                    # Используем 5-минутный таймфрейм
                    ohlcv = exchange.fetch_ohlcv(symbol, '5m', limit=25)
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
