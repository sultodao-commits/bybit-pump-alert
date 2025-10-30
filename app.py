#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bybit Futures Signals Bot - TradingView Logic
ТЕСТОВЫЙ РЕЖИМ - БЕЗ ФИЛЬТРОВ
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

# ========================= ТЕСТОВЫЕ НАСТРОЙКИ =========================

# CORE
RSI_LENGTH = 14
EMA_LENGTH = 50
BB_LENGTH = 20
BB_MULTIPLIER = 1.8

# THRESHOLDS (ОЧЕНЬ МЯГКИЕ)
RSI_PANIC_THRESHOLD = 45    # ОЧЕНЬ мягко
RSI_FOMO_THRESHOLD = 55     # ОЧЕНЬ мягко

# ВСЕ ФИЛЬТРЫ ВЫКЛЮЧЕНЫ
USE_EMA_SIDE_FILTER = False
USE_SLOPE_FILTER = False
MIN_VOLUME_ZSCORE = -10.0   # Фактически отключен
REQUIRE_RETURN_BB = False
REQUIRE_CANDLE_CONFIRM = False  # ВЫКЛЮЧЕНО
MIN_BODY_PCT = 0.0          # Любая свеча
USE_HTF_CONFIRM = False

POLL_INTERVAL_SEC = 25
SIGNAL_COOLDOWN_MIN = 5     # Уменьшил кулдаун для теста
CHUNK_SIZE = 50             # Меньше монет для быстрого теста

# ========================= ИНДИКАТОРЫ =========================

def calculate_rsi(prices: List[float], period: int = 14) -> float:
    """Расчет RSI"""
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
    """Расчет EMA"""
    if len(prices) < period:
        return prices[-1] if prices else 0
    weights = np.exp(np.linspace(-1., 0., period))
    weights /= weights.sum()
    return np.convolve(prices[-period:], weights, mode='valid')[-1]

def calculate_bollinger_bands(prices: List[float], period: int, mult: float) -> tuple:
    """Расчет полос Боллинджера"""
    if len(prices) < period:
        basis = prices[-1] if prices else 0
        return basis, basis, basis
    
    basis = np.mean(prices[-period:])
    dev = mult * np.std(prices[-period:])
    upper = basis + dev
    lower = basis - dev
    return basis, upper, lower

# ========================= ЛОГИКА СИГНАЛОВ =========================

def analyze_tv_signals(symbol: str, ohlcv: List) -> Optional[Dict[str, Any]]:
    try:
        if len(ohlcv) < 20:
            return None

        # Извлекаем данные
        closes = [float(c[4]) for c in ohlcv]
        highs = [float(c[2]) for c in ohlcv]
        lows = [float(c[3]) for c in ohlcv]

        current_close = closes[-1]
        current_high = highs[-1]
        current_low = lows[-1]

        # Рассчитываем индикаторы
        rsi = calculate_rsi(closes, RSI_LENGTH)
        ema = calculate_ema(closes, EMA_LENGTH)
        basis, bb_upper, bb_lower = calculate_bollinger_bands(closes, BB_LENGTH, BB_MULTIPLIER)

        # ПРОСТЕЙШАЯ ЛОГИКА - ТОЛЬКО RSI И BB
        long_rsi = rsi < RSI_PANIC_THRESHOLD
        short_rsi = rsi > RSI_FOMO_THRESHOLD
        
        long_bb = current_low <= bb_lower
        short_bb = current_high >= bb_upper

        long_signal = long_rsi or long_bb
        short_signal = short_rsi or short_bb

        if not long_signal and not short_signal:
            return None

        # Определяем тип сигнала
        if long_signal:
            signal_type = "LONG"
            trigger_source = "RSI" if long_rsi else "BB"
            confidence = 70
        else:
            signal_type = "SHORT" 
            trigger_source = "RSI" if short_rsi else "BB"
            confidence = 70

        print(f"🎯 {symbol}: {signal_type} | RSI={rsi:.1f} | Close={current_close} | BB={bb_lower:.4f}-{bb_upper:.4f}")

        return {
            "symbol": symbol,
            "type": signal_type,
            "rsi": rsi,
            "ema": ema,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "trigger": trigger_source,
            "confidence": confidence,
            "timestamp": time.time()
        }

    except Exception as e:
        print(f"Ошибка анализа {symbol}: {e}")
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
        emoji = "🟢"
        action = "LONG"
    else:
        emoji = "🔴" 
        action = "SHORT"
    
    return (
        f"{emoji} <b>ТЕСТ {action}</b>\n\n"
        f"<b>Монета:</b> {signal['symbol']}\n"
        f"<b>RSI:</b> {signal['rsi']:.1f}\n"
        f"<b>Триггер:</b> {signal['trigger']}\n"
        f"<b>Цена:</b> {signal['ema']:.4f}\n\n"
        f"<i>🧪 ТЕСТОВЫЙ РЕЖИМ - проверка работы</i>"
    )

# ========================= ОСНОВНОЙ ЦИКЛ =========================

def main():
    print("🚀 ЗАПУСК БОТА: ТЕСТОВЫЙ РЕЖИМ (БЕЗ ФИЛЬТРОВ)")
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

    # Берем только первые 50 монет для теста
    test_symbols = symbols[:50]
    print(f"🔍 Тестовое сканирование: {len(test_symbols)} монет")

    send_telegram(f"🤖 <b>ТЕСТ ЗАПУЩЕН</b>: {len(test_symbols)} монет | РЕЖИМ БЕЗ ФИЛЬТРОВ")

    signal_count = 0

    while True:
        try:
            print(f"\n⏱️ Сканирование {len(test_symbols)} монет... | Сигналов: {signal_count}")

            for symbol in test_symbols:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, '15m', limit=20)
                    if not ohlcv:
                        continue

                    signal = analyze_tv_signals(symbol, ohlcv)
                    if not signal:
                        continue

                    now = time.time()
                    if symbol in recent_signals and (now - recent_signals[symbol]) < SIGNAL_COOLDOWN_MIN * 60:
                        continue

                    # Отправляем сигнал
                    recent_signals[symbol] = now
                    send_telegram(format_signal_message(signal))
                    signal_count += 1
                    print(f"🎯 СИГНАЛ #{signal_count}: {symbol}")

                except Exception as e:
                    print(f"Ошибка {symbol}: {e}")
                    continue

            # Очистка старых сигналов
            now = time.time()
            recent_signals = {k: v for k, v in recent_signals.items() if now - v < SIGNAL_COOLDOWN_MIN * 60 * 2}

        except Exception as e:
            print(f"💥 Ошибка цикла: {e}")
            time.sleep(10)

        print(f"⏰ Следующий цикл через {POLL_INTERVAL_SEC} сек...")
        time.sleep(POLL_INTERVAL_SEC)

if __name__ == "__main__":
    main()
