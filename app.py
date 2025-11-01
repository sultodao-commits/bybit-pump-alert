#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - СТРОГАЯ ЛОГИКА RSI + BB (ОБА УСЛОВИЯ)
Полное сканирование рынка
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# ========================= СТРОГИЕ НАСТРОЙКИ =========================

# CORE 
RSI_LENGTH = 14
EMA_LENGTH = 50
BB_LENGTH = 20
BB_MULTIPLIER = 1.8
RSI_PANIC_THRESHOLD = 35
RSI_FOMO_THRESHOLD = 65
MIN_VOLUME_ZSCORE = 1.0
MIN_BODY_PCT = 0.25
REQUIRE_BOTH_TRIGGERS = True
POLL_INTERVAL_SEC = 60
SIGNAL_COOLDOWN_MIN = 420

# Глобальная переменная для отслеживания последнего сообщения
last_update_id = 0

# ========================= ПРОСТОЙ TELEGRAM =========================

def send_telegram_message(chat_id: str, text: str):
    """Простая отправка сообщения в конкретный чат"""
    if not TELEGRAM_BOT_TOKEN:
        return False
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except:
        return False

def get_active_chats():
    """Получаем список активных чатов"""
    if not TELEGRAM_BOT_TOKEN:
        return []
        
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok') and data.get('result'):
                chats = set()
                for update in data['result']:
                    if 'message' in update:
                        chat_id = str(update['message']['chat']['id'])
                        chats.add(chat_id)
                return list(chats)
    except Exception as e:
        print(f"❌ Ошибка получения чатов: {e}")
    return []

def process_telegram_messages():
    """Обрабатываем только новые входящие сообщения"""
    if not TELEGRAM_BOT_TOKEN:
        return
        
    global last_update_id
    
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {}
    
    # Если есть последний update_id, запрашиваем только новые сообщения
    if last_update_id:
        params = {'offset': last_update_id + 1}
    
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get('ok') and data.get('result'):
                for update in data['result']:
                    # Обновляем последний ID
                    last_update_id = update['update_id']
                    
                    if 'message' in update and 'text' in update['message']:
                        chat_id = update['message']['chat']['id']
                        text = update['message']['text']
                        
                        # Отвечаем на команды
                        if text.startswith('/'):
                            if text in ['/start', '/status', '/help']:
                                send_telegram_message(chat_id, "бот работает")
    except Exception as e:
        print(f"❌ Ошибка обработки сообщений: {e}")

def broadcast_to_all_chats(text: str):
    """Отправляем сообщение во все активные чаты"""
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не указан")
        return
        
    active_chats = get_active_chats()
    if not active_chats:
        print("⚠️ Нет активных чатов для отправки")
        return
        
    success_count = 0
    for chat_id in active_chats:
        if send_telegram_message(chat_id, text):
            success_count += 1
    
    print(f"📤 Сообщение отправлено в {success_count}/{len(active_chats)} чатов")

def format_signal_message(signal: Dict) -> str:
    if signal["type"] == "LONG":
        arrows = "↗️" * 4
    else:
        arrows = "↘️" * 4
    
    symbol_parts = signal['symbol'].split('/')
    ticker = symbol_parts[0] if symbol_parts else signal['symbol']
    
    return f"{arrows}\n\n<b>{ticker}</b>"

# ========================= ИНДИКАТОРЫ =========================

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains = [d for d in deltas if d > 0]
    losses = [-d for d in deltas if d < 0]
    if not gains and not losses:
        return 50.0
    avg_gain = sum(gains[-period:]) / period if gains else 0
    avg_loss = sum(losses[-period:]) / period if losses else 0.0001
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return min(max(rsi, 0), 100)

def calculate_ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices[-period:], weights, mode='valid')[-1]

def calculate_bollinger_bands(prices: List[float], period: int, mult: float) -> tuple:
    if len(prices) < period:
        basis = prices[-1] if prices else 0
        return basis, basis, basis
    basis = np.mean(prices[-period:])
    dev = mult * np.std(prices[-period:])
    upper = basis + dev
    lower = basis - dev
    return basis, upper, lower

def calculate_volume_zscore(volumes: List[float], period: int) -> float:
    if len(volumes) < period:
        return 0.0
    recent_volumes = volumes[-period:]
    mean_vol = np.mean(recent_volumes)
    std_vol = np.std(recent_volumes)
    if std_vol == 0:
        return 0.0
    return (volumes[-1] - mean_vol) / std_vol

# ========================= ЛОГИКА СИГНАЛОВ =========================

def analyze_tv_signals(symbol: str, ohlcv: List) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < 25:
            return None

        closes = [float(c[4]) for c in ohlcv]
        opens = [float(c[1]) for c in ohlcv]
        highs = [float(c[2]) for c in ohlcv]
        lows = [float(c[3]) for c in ohlcv]
        volumes = [float(c[5]) for c in ohlcv]

        current_close = closes[-1]
        current_open = opens[-1]
        current_high = highs[-1]
        current_low = lows[-1]
        prev_close = closes[-2] if len(closes) > 1 else current_close

        rsi = calculate_rsi(closes, RSI_LENGTH)
        ema = calculate_ema(closes, EMA_LENGTH)
        basis, bb_upper, bb_lower = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)
        volume_zscore = calculate_volume_zscore(volumes, BB_LENGTH)
        
        volume_pass = volume_zscore >= MIN_VOLUME_ZSCORE
        
        candle_range = max(current_high - current_low, 0.0001)
        body = abs(current_close - current_open)
        body_pct = body / candle_range
        bull_candle_ok = (current_close > current_open) and (body_pct >= MIN_BODY_PCT)
        bear_candle_ok = (current_close < current_open) and (body_pct >= MIN_BODY_PCT)

        long_rsi = rsi < RSI_PANIC_THRESHOLD
        short_rsi = rsi > RSI_FOMO_THRESHOLD
        
        long_bb = (prev_close <= bb_lower) and (current_close > bb_lower)
        short_bb = (prev_close >= bb_upper) and (current_close < bb_upper)

        long_signal = long_rsi and long_bb and bull_candle_ok and volume_pass
        short_signal = short_rsi and short_bb and bear_candle_ok and volume_pass

        if not long_signal and not short_signal:
            return None

        if long_signal:
            signal_type = "LONG"
            confidence = 90
        else:
            signal_type = "SHORT" 
            confidence = 90

        triggers = ["RSI", "BB"]
        print(f"🎯 {symbol}: {signal_type} | RSI={rsi:.1f} | Объем Z={volume_zscore:.2f}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "rsi": rsi,
            "confidence": confidence,
            "triggers": triggers,
            "timestamp": time.time()
        }

    except Exception as e:
        return None

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА")
    print("📱 Бот")
    
    if not TELEGRAM_BOT_TOKEN:
        print("❌ TELEGRAM_BOT_TOKEN не указан")
        print("💡 Сигналы будут только в консоли")
    else:
        print("✅ TELEGRAM_BOT_TOKEN найден")
        print("💡 Напиши боту /start для активации")

    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}

    # ЗАГРУЗКА ВСЕХ ФЬЮЧЕРСНЫХ ПАР USDT
    markets = exchange.load_markets()
    symbols = []

    for symbol, market in markets.items():
        try:
            if (market.get("type") == "swap" and market.get("linear") and
                market.get("settle") == "USDT" and "USDT" in symbol and "/" in symbol):
                symbols.append(symbol)
        except:
            continue

    total_symbols = len(symbols)
    print(f"🔍 Найдено монет для сканирования: {total_symbols}")

    signal_count = 0

    while True:
        try:
            # Обрабатываем сообщения каждый цикл
            process_telegram_messages()
            
            print(f"\n⏱️ Сканирование {total_symbols} пар... | Сигналов: {signal_count}")
            current_time = time.time()

            for symbol in symbols:
                try:
                    if symbol in recent_signals:
                        time_since_last_signal = current_time - recent_signals[symbol]
                        if time_since_last_signal < SIGNAL_COOLDOWN_MIN * 60:
                            continue

                    ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=25)
                    if not ohlcv or len(ohlcv) < 20:
                        continue

                    signal = analyze_tv_signals(symbol, ohlcv)
                    if not signal:
                        continue

                    recent_signals[symbol] = current_time
                    signal_count += 1
                    
                    # Отправляем сигнал
                    message = format_signal_message(signal)
                    broadcast_to_all_chats(message)
                    
                    print(f"🎯  #{signal_count}: {symbol}")

                except Exception as e:
                    continue

            # Очистка старых записей
            current_time = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() 
                            if current_time - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
