#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - СТРОГАЯ ЛОГИКА RSI + BB (ОБА УСЛОВИЯ)
"""

import os
import time
import requests
import ccxt
import numpy as np
from typing import List, Dict, Any, Optional

# ========================= НАСТРОЙКИ =========================

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# ========================= СТРОГИЕ НАСТРОЙКИ =========================

# CORE 
RSI_LENGTH = 14
EMA_LENGTH = 50
BB_LENGTH = 20
BB_MULTIPLIER = 1.8

# THRESHOLDS (СТРОГИЕ) - ИЗМЕНЕНО НА 35 И 65
RSI_PANIC_THRESHOLD = 35    # LONG: RSI < 35
RSI_FOMO_THRESHOLD = 65     # SHORT: RSI > 65

# FILTERS (СТРОГИЕ)
USE_EMA_SIDE_FILTER = False
MIN_VOLUME_ZSCORE = 1.0     
REQUIRE_RETURN_BB = True    
REQUIRE_CANDLE_CONFIRM = True
MIN_BODY_PCT = 0.25         
REQUIRE_BOTH_TRIGGERS = True  # ✅ ВАЖНОЕ ИЗМЕНЕНИЕ: Требуем ОБА условия

POLL_INTERVAL_SEC = 60
SIGNAL_COOLDOWN_MIN = 420   # КУЛДАУН 7 ЧАСОВ

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

# ========================= СТРОГАЯ ЛОГИКА СИГНАЛОВ (ОБА УСЛОВИЯ) =========================

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

        # Индикаторы
        rsi = calculate_rsi(closes, RSI_LENGTH)
        ema = calculate_ema(closes, EMA_LENGTH)
        basis, bb_upper, bb_lower = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)
        volume_zscore = calculate_volume_zscore(volumes, BB_LENGTH)
        
        # ФИЛЬТРЫ
        volume_pass = volume_zscore >= MIN_VOLUME_ZSCORE
        
        candle_range = max(current_high - current_low, 0.0001)
        body = abs(current_close - current_open)
        body_pct = body / candle_range
        bull_candle_ok = (current_close > current_open) and (body_pct >= MIN_BODY_PCT)
        bear_candle_ok = (current_close < current_open) and (body_pct >= MIN_BODY_PCT)

        # Условия RSI (СТРОГИЕ)
        long_rsi = rsi < RSI_PANIC_THRESHOLD  # RSI < 35
        short_rsi = rsi > RSI_FOMO_THRESHOLD  # RSI > 65
        
        # Условия BB (возврат от границ)
        long_bb = (prev_close <= bb_lower) and (current_close > bb_lower)
        short_bb = (prev_close >= bb_upper) and (current_close < bb_upper)

        # ✅ ВАЖНОЕ ИЗМЕНЕНИЕ: Требуем ОБА условия для сигнала
        long_signal = long_rsi and long_bb and bull_candle_ok and volume_pass
        short_signal = short_rsi and short_bb and bear_candle_ok and volume_pass

        if not long_signal and not short_signal:
            return None

        # Определяем тип сигнала
        if long_signal:
            signal_type = "LONG"
            confidence = 90  # Высокая уверенность при выполнении обоих условий
        else:
            signal_type = "SHORT" 
            confidence = 90

        # Определяем какие триггеры сработали (всегда оба)
        triggers = ["RSI", "BB"]
        trigger_text = "+".join(triggers)

        print(f"🎯 {symbol}: {signal_type} | Триггеры: {trigger_text} | RSI={rsi:.1f} | Объем Z={volume_zscore:.2f} | Тело={body_pct:.1%}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "rsi": rsi,
            "ema": ema,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "volume_zscore": volume_zscore,
            "body_pct": body_pct,
            "confidence": confidence,
            "triggers": triggers,
            "timestamp": time.time()
        }

    except Exception as e:
        print(f"❌ Ошибка анализа {symbol}: {e}")
        return None

# ========================= TELEGRAM =========================

def send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
    except:
        pass

def format_signal_message(signal: Dict) -> str:
    if signal["type"] == "LONG":
        arrows = "↗️" * 8  # 8 стрелок вверх
    else:
        arrows = "↘️" * 8  # 8 стрелок вниз
    
    # Извлекаем только название тикера (убираем /USDT)
    symbol_parts = signal['symbol'].split('/')
    ticker = symbol_parts[0] if symbol_parts else signal['symbol']
    
    return f"{arrows}\n\n<b>{ticker}</b>"

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА: СТРОГАЯ ЛОГИКА RSI + BB (35/65) - ОБА ТРИГГЕРА ОБЯЗАТЕЛЬНЫ")
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Укажи TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID!")
        return

    exchange = ccxt.bybit({"enableRateLimit": True})
    recent_signals = {}

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
    print(f"🔍 Найдено монет: {total_symbols}")
    send_telegram(f"🤖 Бот запущен | {total_symbols} монет | Строгая логика RSI+BB")

    signal_count = 0

    while True:
        try:
            print(f"\n⏱️ Сканирование... | Сигналов: {signal_count}")
            current_time = time.time()

            for symbol in symbols:
                try:
                    # ПРОВЕРКА КУЛДАУНА 7 ЧАСОВ
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

                    # СОХРАНЯЕМ ВРЕМЯ СИГНАЛА
                    recent_signals[symbol] = current_time
                    signal_count += 1
                    
                    # Отправляем сигнал
                    message = format_signal_message(signal)
                    send_telegram(message)
                    
                    print(f"🎯 СИГНАЛ #{signal_count}: {symbol} | Триггеры: {'+'.join(signal['triggers'])} | Следующий сигнал через 7 часов")

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
